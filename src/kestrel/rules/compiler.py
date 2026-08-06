"""Natural language → validated rule, with a backtest before it goes live.

This is the feature the whole rule design exists to enable. An operator types:

    "alert me if a truck parks at the loading dock for more than 10 minutes after 9pm"

and gets back a compiled, schema-valid rule, *plus* a report of what that rule would
have done against the indexed history — how many times it would have fired, on
which frames, and whether those firings look right. Only then can it be enabled.

Three things make this trustworthy rather than a party trick:

1.  **The schema is generated from the engine's own models**, so a rule that
    validates is a rule the engine can actually run. There is no second
    interpretation of the language to drift out of sync.
2.  **Generation is checked, not trusted.** Invalid output is rejected and repaired
    once, with the validation error handed back to the model. Zone names are
    checked against the real site, because a rule referencing ``front-gate`` on a
    site whose zone is ``main-gate`` would validate and then never fire — the worst
    failure mode, since it looks like it is working.
3.  **Backtesting is the safety net.** A rule that would have fired 400 times
    yesterday is a bad rule, and the operator sees that before it reaches them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from kestrel.domain import Site
from kestrel.obs.meter import Stage
from kestrel.rules.dsl import Rule
from kestrel.rules.engine import Observation, RuleEngine

_SLUG = re.compile(r"[^a-z0-9]+")


SYSTEM = """You convert plain-English security requirements into KESTREL rules.

You will be given a JSON Schema and the site's real zones. Produce ONE rule object
that satisfies the schema. Return ONLY the JSON object — no prose, no code fence.

Hard requirements:
- `id` must be lowercase-kebab-case and describe the rule, e.g. "truck-parked-at-dock".
- Every zone you name in a `zone_in` condition MUST come from the provided zone list.
  Inventing a zone produces a rule that validates and then never fires.
- Prefer `outside_normal_hours` over a hardcoded `time_between` when the user says
  something like "after hours" or "when closed" — zones carry their own schedules.
- Translate durations exactly: "more than 10 minutes" is `dwell.seconds: 600`.
- Choose severity by consequence: critical = intrusion into a critical asset,
  high = probable intrusion, medium = policy violation, low/info = informational.
- Set `visual_predicate` to a short description of what the object looks like ONLY
  when the target is not a standard class (person, car, truck, van, bus, bicycle,
  motorcycle, dog, backpack, suitcase). It drives an open-vocabulary detector.
- Set `origin` to "natural_language" and copy the user's sentence into `source_text`.
- Include a `cooldown_seconds`: an alert that repeats every frame is ignored by
  operators. 300 is a sensible default.

Think about what the user actually means, not just the words. "Someone hanging
around the gate" means dwell, not mere presence."""


@dataclass
class BacktestHit:
    ts: datetime
    frame_id: str
    entity_id: str | None
    zone_id: str | None
    label: str
    detail: str


@dataclass
class BacktestReport:
    """What a rule would have done, before it is allowed to do it."""

    rule_id: str
    frames_replayed: int
    observations: int
    days_covered: int
    hits: list[BacktestHit] = field(default_factory=list)
    error: str | None = None

    @property
    def fire_count(self) -> int:
        return len(self.hits)

    @property
    def fires_per_day(self) -> float:
        return self.fire_count / max(1, self.days_covered)

    @property
    def verdict(self) -> str:
        """A blunt readability judgement — the operator still decides."""
        if self.error:
            return f"could not backtest: {self.error}"
        if self.frames_replayed == 0:
            return "no history available to test against, so enable with caution"
        if self.fire_count == 0:
            return (
                "would not have fired on any indexed history. Either the site has "
                "not seen this situation, or the rule is too strict to ever match."
            )
        if self.fires_per_day > 20:
            return (
                f"would have fired {self.fire_count} times "
                f"({self.fires_per_day:.0f}/day), almost certainly too noisy"
            )
        if self.fires_per_day > 5:
            return (
                f"would have fired {self.fire_count} times "
                f"({self.fires_per_day:.1f}/day), review before enabling"
            )
        return (
            f"would have fired {self.fire_count} times over {self.days_covered} day(s) "
            f"({self.fires_per_day:.1f}/day), a plausible rate"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "frames_replayed": self.frames_replayed,
            "observations": self.observations,
            "days_covered": self.days_covered,
            "fire_count": self.fire_count,
            "fires_per_day": round(self.fires_per_day, 2),
            "verdict": self.verdict,
            "error": self.error,
            "hits": [
                {
                    "ts": h.ts.isoformat(), "frame_id": h.frame_id,
                    "entity_id": h.entity_id, "zone_id": h.zone_id,
                    "label": h.label, "detail": h.detail,
                }
                for h in self.hits[:50]
            ],
        }


class RuleCompiler:
    """English in, validated rule out."""

    def __init__(self, site: Site, client=None) -> None:
        self.site = site
        self.client = client

    def _zone_block(self) -> str:
        lines = []
        for z in self.site.zones:
            hours = (
                f"open {z.normal_hours[0]:02d}:00-{z.normal_hours[1]:02d}:00"
                if z.normal_hours else "no declared hours"
            )
            lines.append(f'  - id: "{z.id}"  name: "{z.name}"  kind: {z.kind.value}  ({hours})')
        return "\n".join(lines)

    async def compile(self, text: str, *, repair_attempts: int = 1) -> Rule:
        """Compile one English sentence into a validated ``Rule``."""
        if self.client is None:
            raise RuntimeError("rule compilation requires a model client")

        schema = json.dumps(Rule.model_json_schema(), separators=(",", ":"))
        user = (
            f"Site: {self.site.name} ({self.site.id})\n\n"
            f"Available zones — use ONLY these ids:\n{self._zone_block()}\n\n"
            f"JSON Schema for a rule:\n{schema}\n\n"
            f"Requirement to translate:\n\"{text}\""
        )
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user},
        ]

        last_error = ""
        for attempt in range(repair_attempts + 1):
            raw = await self.client.chat(
                messages, stage=Stage.REASON, max_tokens=1100, temperature=0.0
            )
            try:
                return self._validate(raw, text)
            except (ValidationError, ValueError) as e:
                last_error = str(e)[:600]
                if attempt >= repair_attempts:
                    break
                messages = [
                    *messages,
                    {"role": "assistant", "content": raw[:1200]},
                    {
                        "role": "user",
                        "content": (
                            f"That rule was rejected:\n{last_error}\n\n"
                            "Return a corrected JSON object only."
                        ),
                    },
                ]
        raise ValueError(f"could not compile a valid rule: {last_error}")

    def _validate(self, raw: str, source_text: str) -> Rule:
        from kestrel.clients.models import _loads_lenient

        payload = _loads_lenient(raw)
        if not payload:
            raise ValueError("model did not return a JSON object")

        payload.setdefault("origin", "natural_language")
        payload.setdefault("source_text", source_text)
        if not payload.get("id"):
            payload["id"] = _SLUG.sub("-", source_text.lower())[:40].strip("-") or "generated-rule"
        payload["id"] = _SLUG.sub("-", str(payload["id"]).lower()).strip("-")

        rule = Rule.model_validate(payload)

        # The failure that looks like success: a rule naming a zone that does not
        # exist validates fine and then silently never matches anything.
        known = {z.id for z in self.site.zones}
        for c in rule.conditions:
            if c.kind.value == "zone_in":
                unknown = [z for z in c.zones if z not in known]
                if unknown:
                    raise ValueError(
                        f"rule references zones that do not exist on this site: {unknown}. "
                        f"Valid zone ids are: {sorted(known)}"
                    )
        return rule

    # ── backtest ─────────────────────────────────────────────────────────
    def backtest(
        self, rule: Rule, observations: list[Observation], *, baseline=None
    ) -> BacktestReport:
        """Replay a rule over recorded observations.

        Runs against an isolated engine containing only this rule, so cooldowns and
        entity state from the live pack cannot contaminate the result.
        """
        report = BacktestReport(
            rule_id=rule.id,
            frames_replayed=len({o.frame_id for o in observations}),
            observations=len(observations),
            days_covered=len({o.ts.date() for o in observations}) or 1,
        )
        if not observations:
            return report

        engine = RuleEngine(self.site, [rule], baseline=baseline)
        try:
            for obs in sorted(observations, key=lambda o: o.ts):
                for res in engine.evaluate(obs):
                    if res.fired:
                        report.hits.append(
                            BacktestHit(
                                ts=obs.ts, frame_id=obs.frame_id, entity_id=obs.entity_id,
                                zone_id=obs.zone_id, label=obs.label,
                                detail="; ".join(
                                    c.detail for c in res.clauses if c.passed
                                )[:220],
                            )
                        )
        except Exception as e:
            report.error = f"{type(e).__name__}: {e}"[:200]
        return report


def observations_from_db(db, site_id: str, *, limit: int = 20000) -> list[Observation]:
    """Rebuild observations from the index so a rule can be replayed over history."""
    rows = db.query(
        """SELECT d.*, f.ts AS f_ts
           FROM detections d JOIN frames f ON f.id = d.frame_id
           WHERE d.site_id = ? ORDER BY d.ts ASC LIMIT ?""",
        (site_id, limit),
    )
    out: list[Observation] = []
    for r in rows:
        try:
            attrs = json.loads(r["attributes_json"] or "{}")
        except json.JSONDecodeError:
            attrs = {}
        try:
            pconf = float(attrs.get("projection_confidence", 1.0))
        except (TypeError, ValueError):
            pconf = 1.0
        out.append(
            Observation(
                ts=datetime.fromisoformat(r["ts"]),
                frame_id=r["frame_id"],
                entity_id=r["entity_id"],
                label=r["label"],
                confidence=float(r["confidence"]),
                zone_id=r["zone_id"],
                attributes={k: str(v) for k, v in attrs.items()},
                detection_id=r["id"],
                perception_confidence=pconf,
            )
        )
    return out
