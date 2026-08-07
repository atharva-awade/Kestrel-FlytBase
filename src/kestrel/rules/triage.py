"""Alert triage and threat narrative.

A rule engine that fires is only half a security system. The other half is deciding
which firings are worth a human's attention, and presenting them as something a
person can act on.

**Triage** — suppression, deduplication and counterfactual checks. The failure mode
this exists to prevent is well documented in the industry: an operator who receives
forty alerts a night learns to dismiss them all, and the system that raised them
protects nothing. Suppression is therefore a safety feature, not a convenience.

**Narrative** — related alerts are stitched into one chronological account with a
rising threat score. Six isolated alerts saying "person detected" convey far less
than one paragraph saying a vehicle parked outside the fence, someone walked the
perimeter for four minutes, and then entered the substation.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from kestrel.domain import Alert, AlertStatus, Severity, Site
from kestrel.obs.meter import Stage

# Alerts closer together than this on the same rule+entity are the same event.
DEDUPE_WINDOW = timedelta(minutes=5)
# Alerts within this window are candidates for one narrative.
NARRATIVE_WINDOW = timedelta(minutes=20)

# Situations that look like intrusions and are not.
#
# Two distinct kinds, because they warrant different treatment:
#
#   CONTRADICTING — the evidence says the rule's premise is false. "A person is
#       loitering" versus a scene reading "a stray dog lies beside the fence" is
#       not a weaker alert, it is the wrong alert. These suppress outright.
#   MITIGATING — the evidence makes the alert less alarming without refuting it.
#       Rain produces spurious motion; PPE suggests an authorised worker. These
#       scale confidence down and let the threshold decide.
#
# Getting this distinction wrong was a real bug: scaling a wildlife alert by 0.35
# left it at 0.2975 against a 0.25 threshold, so the stray-dog scenario still
# alerted. Multiplying a confidence never expresses "this premise is false".
CONTRADICTING: list[tuple[str, str, str]] = [
    (
        "animal",
        "dog cat bird fox animal wildlife stray deer rodent",
        "the subject appears to be an animal, not a person",
    ),
    (
        "vegetation",
        "tree branch bush foliage leaves vegetation shadow",
        "the movement is consistent with wind-blown vegetation, not a person",
    ),
]

MITIGATING: list[tuple[str, str, str, float]] = [
    (
        "weather",
        "rain snow fog hail downpour",
        "conditions are degraded by weather, which produces spurious motion",
        0.5,
    ),
    (
        "worker",
        "high-visibility high visibility hard hat safety vest hi-vis reflective",
        "the person is wearing site PPE, consistent with authorised staff",
        0.35,
    ),
]

# Rules whose premise is "a person is here". A contradiction only contradicts
# something, so we check what the rule actually claimed.
_PERSON_SUBJECT_HINTS = ("person", "loiter", "intrud", "gather", "breach", "trespass")

_WORD = re.compile(r"[a-z]+")


def _words(text: str) -> set[str]:
    """Tokenise to whole words.

    Substring matching is wrong here and was an actual bug: `"cat"` is contained
    in `"location"`, so every alert whose title mentioned a "sensitive location"
    was classified as wildlife and suppressed. Keyword matching against free text
    must respect word boundaries.
    """
    return set(_WORD.findall(text.lower()))


@dataclass
class TriageDecision:
    keep: bool
    reason: str
    adjusted_confidence: float
    counterfactual: str | None = None


@dataclass
class ThreatNarrative:
    """A sequence of related alerts told as one story."""

    id: str
    site_id: str
    start_ts: datetime
    end_ts: datetime
    alert_ids: list[str]
    peak_severity: Severity
    threat_score: float
    text: str = ""
    trajectory: list[tuple[datetime, float]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "site_id": self.site_id,
            "start_ts": self.start_ts.isoformat(),
            "end_ts": self.end_ts.isoformat(),
            "alert_ids": self.alert_ids,
            "peak_severity": self.peak_severity.value,
            "threat_score": round(self.threat_score, 3),
            "text": self.text,
            "trajectory": [(t.isoformat(), round(v, 3)) for t, v in self.trajectory],
        }


class Triage:
    """Decides which alerts reach a human, and why."""

    def __init__(self, site: Site, *, min_confidence: float = 0.25) -> None:
        self.site = site
        self.min_confidence = min_confidence
        self._recent: list[Alert] = []
        # Operator corrections. A thumbs-down does not just dismiss one alert; it
        # lowers confidence for that rule/zone pairing thereafter.
        self._feedback_penalty: dict[tuple[str, str | None], float] = defaultdict(float)
        self.suppressed_count = 0
        self.kept_count = 0
        self.by_reason: dict[str, int] = defaultdict(int)

    def assess(self, alert: Alert, scene_caption: str = "") -> TriageDecision:
        # 1 — duplicate of something already raised
        for prev in reversed(self._recent[-80:]):
            if prev.rule_id != alert.rule_id:
                continue
            if abs(alert.ts - prev.ts) > DEDUPE_WINDOW:
                continue
            same_entity = bool(set(prev.entity_ids) & set(alert.entity_ids))
            if same_entity or (prev.zone_id == alert.zone_id and not alert.entity_ids):
                return self._reject(
                    alert, f"duplicate of {prev.id} raised {_ago(prev.ts, alert.ts)} ago"
                )

        # 2 — learned operator feedback
        penalty = self._feedback_penalty.get((alert.rule_id, alert.zone_id), 0.0)
        confidence = max(0.0, alert.confidence - penalty)
        if penalty > 0 and confidence < self.min_confidence:
            return self._reject(
                alert,
                f"operator has previously marked {alert.rule_id} in "
                f"{alert.zone_id} as a false positive",
            )

        # 3a — contradicting evidence: the rule's premise is false, so the alert
        # is wrong rather than merely weak. Only applies where the premise was
        # "a person is here" — a contradiction has to contradict something.
        #
        # Counterfactual evidence comes from the SCENE, not the alert title. The
        # title is generated from the rule name, so matching against it made rules
        # suppress themselves: "cat" appears inside "sensitive location", which
        # silently classified every such alert as wildlife.
        haystack = _words(scene_caption)
        claims_person = any(h in f"{alert.rule_id} {alert.rule_name}".lower()
                            for h in _PERSON_SUBJECT_HINTS)
        if claims_person:
            for name, keywords, explanation in CONTRADICTING:
                if haystack & set(keywords.split()):
                    return self._reject(alert, explanation, counterfactual=name)

        # 3b — mitigating evidence: still plausible, just less alarming.
        for name, keywords, explanation, factor in MITIGATING:
            if haystack & set(keywords.split()):
                # PPE lowers suspicion but never excuses a critical-zone breach —
                # a hi-vis vest is not an access credential.
                if name == "worker" and alert.severity is Severity.CRITICAL:
                    continue
                confidence *= factor
                if confidence < self.min_confidence:
                    return self._reject(alert, explanation, counterfactual=name)

        # 4 — too weak to be worth interrupting anyone
        if confidence < self.min_confidence:
            return self._reject(
                alert, f"confidence {confidence:.2f} below threshold {self.min_confidence:.2f}"
            )

        self._recent.append(alert)
        self.kept_count += 1
        alert.confidence = round(confidence, 3)
        return TriageDecision(True, "raised", confidence)

    def _reject(self, alert: Alert, reason: str, counterfactual: str | None = None) -> TriageDecision:
        self.suppressed_count += 1
        self.by_reason[reason.split(":")[0][:48]] += 1
        alert.status = AlertStatus.DISMISSED
        alert.suppressed_reason = reason
        return TriageDecision(False, reason, alert.confidence, counterfactual)

    def record_feedback(self, alert: Alert, is_false_positive: bool) -> dict[str, Any]:
        """Operator correction, fed back into future decisions.

        Deliberately conservative: each correction moves the penalty a little.
        One mistaken thumbs-down should not blind the system to a real rule.
        """
        key = (alert.rule_id, alert.zone_id)
        if is_false_positive:
            self._feedback_penalty[key] = min(0.6, self._feedback_penalty[key] + 0.15)
        else:
            self._feedback_penalty[key] = max(0.0, self._feedback_penalty[key] - 0.25)
        return {
            "rule_id": alert.rule_id,
            "zone_id": alert.zone_id,
            "penalty": round(self._feedback_penalty[key], 3),
            "effect": (
                f"future {alert.rule_id} alerts in {alert.zone_id} will need "
                f"{self._feedback_penalty[key]:.2f} more confidence to be raised"
            ),
        }

    @property
    def stats(self) -> dict[str, Any]:
        total = self.kept_count + self.suppressed_count
        return {
            "raised": self.kept_count,
            "suppressed": self.suppressed_count,
            "suppression_rate": round(self.suppressed_count / total, 3) if total else 0.0,
            "by_reason": dict(self.by_reason),
            "learned_penalties": {
                f"{r}@{z}": round(v, 3) for (r, z), v in self._feedback_penalty.items() if v
            },
        }


# ═══════════════════════════════════════════════════════════════════════════════
class NarrativeBuilder:
    """Stitches related alerts into one account with a rising threat score."""

    def __init__(self, site: Site, client=None) -> None:
        self.site = site
        self.client = client

    def group(self, alerts: list[Alert]) -> list[list[Alert]]:
        """Cluster by time proximity, and by shared entity or adjacent zone.

        A vehicle parking outside the fence and a person climbing it twenty
        minutes later are one incident, not two, even though the entities differ.
        """
        if not alerts:
            return []
        ordered = sorted(alerts, key=lambda a: a.ts)
        groups: list[list[Alert]] = [[ordered[0]]]
        for a in ordered[1:]:
            g = groups[-1]
            gap = a.ts - g[-1].ts
            shares_entity = bool(set(a.entity_ids) & {e for x in g for e in x.entity_ids})
            shares_zone = a.zone_id is not None and a.zone_id in {x.zone_id for x in g}
            if gap <= NARRATIVE_WINDOW and (shares_entity or shares_zone):
                g.append(a)
            else:
                groups.append([a])
        return groups

    @staticmethod
    def threat_score(alerts: list[Alert]) -> tuple[float, list[tuple[datetime, float]]]:
        """Cumulative score with escalation.

        Repeated and escalating activity scores above the sum of its parts, because
        that is what distinguishes a probe from an intrusion in progress.
        """
        score = 0.0
        traj: list[tuple[datetime, float]] = []
        for i, a in enumerate(sorted(alerts, key=lambda x: x.ts)):
            contribution = a.severity.weight * a.confidence
            escalation = 1.0 + 0.18 * i
            score = min(1.0, score + contribution * escalation * 0.45)
            traj.append((a.ts, score))
        return score, traj

    async def build(self, alerts: list[Alert]) -> ThreatNarrative | None:
        if not alerts:
            return None
        ordered = sorted(alerts, key=lambda a: a.ts)
        score, traj = self.threat_score(ordered)
        peak = max(ordered, key=lambda a: a.severity.weight).severity

        nid = f"nar_{ordered[0].site_id}_{int(ordered[0].ts.timestamp())}"
        nar = ThreatNarrative(
            id=nid,
            site_id=ordered[0].site_id,
            start_ts=ordered[0].ts,
            end_ts=ordered[-1].ts,
            alert_ids=[a.id for a in ordered],
            peak_severity=peak,
            threat_score=score,
            trajectory=traj,
        )
        nar.text = await self._write(ordered, score)
        return nar

    async def _write(self, alerts: list[Alert], score: float) -> str:
        timeline = "\n".join(
            f"[{a.ts:%H:%M:%S}] {a.severity.value.upper()}: {a.title} "
            f"(confidence {a.confidence:.2f})"
            + (f"\n    evidence: {a.evidence[0].caption}" if a.evidence else "")
            for a in alerts
        )
        if self.client is None:
            return self._extractive(alerts, score)
        try:
            return await self.client.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are a security analyst writing an incident summary for a "
                            "site manager. Given a timeline of alerts, write 2-4 sentences "
                            "describing what appears to have happened, in order.\n\n"
                            "Rules:\n"
                            "- State only what the alerts state. Never invent detail.\n"
                            "- Do not assert intent. 'consistent with' is acceptable; "
                            "'the intruder intended to' is not.\n"
                            "- If the sequence is ambiguous, say so.\n"
                            "- End with the single most useful next action.\n"
                            "- No preamble, no bullet points."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Site: {self.site.name}\nThreat score: {score:.2f}\n\n"
                            f"Alert timeline:\n{timeline}"
                        ),
                    },
                ],
                stage=Stage.REASON,
                max_tokens=260,
            )
        except Exception:
            return self._extractive(alerts, score)

    def _extractive(self, alerts: list[Alert], score: float) -> str:
        first, last = alerts[0], alerts[-1]
        span = (last.ts - first.ts).total_seconds() / 60
        zones = [a.zone_id for a in alerts if a.zone_id]
        where = ", ".join(dict.fromkeys(zones)) or "the site"
        return (
            f"{len(alerts)} related alert(s) between {first.ts:%H:%M} and {last.ts:%H:%M} "
            f"({span:.0f} min) affecting {where}. Peak severity "
            f"{max(a.severity.weight for a in alerts):.2f}, threat score {score:.2f}. "
            f"Began with: {first.title}. Most recent: {last.title}."
        )


def _ago(then: datetime, now: datetime) -> str:
    s = abs((now - then).total_seconds())
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s/60:.0f}m"
    return f"{s/3600:.1f}h"
