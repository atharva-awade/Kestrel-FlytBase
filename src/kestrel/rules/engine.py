"""Rule evaluation.

Rules are declarative; this is what runs them. The engine keeps a small amount of
per-entity state so that temporal conditions â€” dwell, sequences, counts within a
window â€” can be decided at all, and it records *why* each rule did or did not fire
so that an alert can show its work.

Design notes worth stating:

*   **Evaluation is per-entity, not per-frame.** "This person has been at the gate
    for two minutes" is a fact about the person, and a frame-oriented engine has
    nowhere to put it.
*   **Every evaluation produces a trace**, including failures. "Rule did not fire
    because dwell was 74 s of the required 120 s" is exactly what an operator needs
    when they ask why nothing alerted, and it is what makes the backtest legible.
*   **Cooldowns are per entity by default.** Two intruders should both alert; one
    intruder should not alert forty times.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from kestrel.domain import (
    Alert,
    AlertLocation,
    AlertStatus,
    Detection,
    Evidence,
    LatLon,
    SceneGraph,
    Severity,
    Site,
    Telemetry,
)
from kestrel.memory.baseline import BaselineModel, Deviation, combine_confidence
from kestrel.rules.dsl import ConditionKind, Rule


@dataclass
class Observation:
    """One entity, as seen in one frame, with everything a rule may ask about."""

    ts: datetime
    frame_id: str
    entity_id: str | None
    label: str
    confidence: float
    zone_id: str | None
    attributes: dict[str, str] = field(default_factory=dict)
    detection_id: str | None = None
    perception_confidence: float = 1.0
    # Geo-projected position of the subject, when the projection succeeded. This
    # is what makes an alert dispatchable rather than merely descriptive.
    world: LatLon | None = None
    projection_confidence: float = 0.0


# Vehicle and person synonyms, normalised to the canonical class a rule names.
#
# A detector says "truck"; a vision-language model describing the same object says
# "pickup", "sedan" or "panel van". Without normalisation a rule listing
# ["car", "truck", "van"] silently fails to match a "sedan", and the failure is
# invisible â€” the rule looks correct, the detection looks correct, and nothing
# fires. This was the cause of two missed scenarios (tailgating, repeat-visitor).
#
# Normalising here rather than enumerating synonyms inside every rule keeps rules
# readable and means a new synonym is fixed in one place.
LABEL_SYNONYMS: dict[str, str] = {
    "sedan": "car", "hatchback": "car", "saloon": "car", "suv": "car",
    "estate": "car", "coupe": "car", "taxi": "car", "automobile": "car",
    "pickup": "truck", "pick-up": "truck", "lorry": "truck", "hgv": "truck",
    "tractor": "truck", "trailer": "truck", "semi": "truck", "flatbed": "truck",
    "panel van": "van", "minivan": "van", "transit": "van", "box truck": "truck",
    "motorbike": "motorcycle", "scooter": "motorcycle", "moped": "motorcycle",
    "man": "person", "woman": "person", "worker": "person", "pedestrian": "person",
    "guard": "person", "driver": "person", "courier": "person", "figure": "person",
    "cyclist": "person", "individual": "person",
    "hound": "dog", "puppy": "dog", "stray": "dog",
    "rucksack": "backpack", "bag": "backpack", "holdall": "backpack",
    "package": "cardboard box", "parcel": "cardboard box", "carton": "cardboard box",
}


def canonical_labels(label: str) -> set[str]:
    """Every canonical class a raw label should be considered an instance of.

    Returns a set because a compound label like "pickup truck" legitimately maps to
    both its own words and the canonical form.
    """
    low = label.lower().strip()
    out = {low}
    if low in LABEL_SYNONYMS:
        out.add(LABEL_SYNONYMS[low])
    # Multi-word labels: "white box truck" should match "truck", "blue pickup" â†’ "truck".
    for word in low.replace("-", " ").split():
        out.add(word)
        if word in LABEL_SYNONYMS:
            out.add(LABEL_SYNONYMS[word])
    for phrase, canon in LABEL_SYNONYMS.items():
        if " " in phrase and phrase in low:
            out.add(canon)
    return out


# Generic labels that are supertypes of the specific classes rules name.
#
# A vision-language model does not describe the same object at a consistent level
# of specificity: the pickup that was a "pickup" at 09:00 is a "vehicle" at 02:00
# when it is dark and further away. A rule asking for ["car","truck","van"] must
# match "vehicle", because "vehicle" asserts *less*, not something different.
#
# This is the other half of the specificity problem — LABEL_SYNONYMS handles labels
# more specific than the rule ("sedan"), this handles labels more general
# ("vehicle"). Missing it cost the repeat-visitor scenario its after-hours alert,
# and the failure was invisible: the detection was right, the rule was right, and
# nothing fired.
SUPERTYPES: dict[str, set[str]] = {
    "vehicle": {"car", "truck", "van", "bus", "motorcycle", "bicycle", "forklift"},
    "car": {"sedan", "hatchback", "suv"},
    "truck": {"pickup", "lorry", "box truck", "flatbed"},
    "person": {"man", "woman", "worker", "pedestrian", "courier", "driver"},
    "animal": {"dog", "cat", "bird", "horse"},
    "object": {"backpack", "suitcase", "cardboard box", "handbag"},
}


def label_matches(observed: str, wanted: list[str]) -> bool:
    """Does an observed label satisfy a rule's class list?

    Matches in three ways, because a label can be more specific than the rule, less
    specific, or phrased differently:

      1. synonym       "sedan" satisfies a rule asking for "car"
      2. supertype     "vehicle" satisfies a rule asking for "car" or "truck"
      3. substring     "person walking" satisfies a rule asking for "person"
    """
    forms = canonical_labels(observed)
    low = observed.lower().strip()

    for w in wanted:
        wl = w.lower().strip()
        if wl in forms:
            return True
        if wl in low or low in wl:
            return True

    # Supertype: the observation is a generic parent of something the rule wants.
    for form in forms:
        children = SUPERTYPES.get(form)
        if children and any(w.lower().strip() in children for w in wanted):
            return True
    return False


# Consecutive observations in a genuinely different zone required before we accept
# that the subject has moved and restart its dwell clock.
#
# Two is the right number, and the reasoning matters. The dominant source of zone
# flicker â€” nesting and unresolved projections â€” is handled structurally by
# `zones_nested` and by carrying the last known zone through a `None`, so this
# only has to absorb a single stray frame that projects somewhere unrelated.
# Setting it higher delays detecting a *real* departure: at a 20 s observation
# interval, three confirmations means a minute of walking away still counts as
# standing still.
ZONE_CHANGE_CONFIRM = 2


@dataclass
class EntityState:
    """Per-entity temporal state â€” the memory that makes dwell and sequences work."""

    first_ts: datetime
    last_ts: datetime
    zone_id: str | None
    label: str
    seen: int = 1
    zone_entered_ts: datetime | None = None
    last_alert: dict[str, datetime] = field(default_factory=dict)
    # Debounce state for the zone the subject *might* be moving into.
    pending_zone: str | None = None
    pending_count: int = 0

    @property
    def dwell_seconds(self) -> float:
        base = self.zone_entered_ts or self.first_ts
        return max(0.0, (self.last_ts - base).total_seconds())


@dataclass
class ClauseResult:
    kind: str
    passed: bool
    detail: str


@dataclass
class RuleResult:
    rule: Rule
    fired: bool
    clauses: list[ClauseResult]
    observation: Observation
    dwell_seconds: float = 0.0
    deviation: Deviation | None = None
    suppressed: str | None = None

    @property
    def failed_clauses(self) -> list[ClauseResult]:
        return [c for c in self.clauses if not c.passed]

    def why_not(self) -> str:
        if self.fired:
            return ""
        if self.suppressed:
            return self.suppressed
        f = self.failed_clauses
        return f[0].detail if f else "no clause failed but the rule did not fire"


class RuleEngine:
    """Evaluates a rule pack against a stream of observations for one site."""

    def __init__(
        self,
        site: Site,
        rules: list[Rule],
        *,
        baseline: BaselineModel | None = None,
        history_window: timedelta = timedelta(minutes=15),
    ) -> None:
        self.site = site
        self.rules = rules
        self.baseline = baseline
        self.history_window = history_window

        self._entities: dict[str, EntityState] = {}
        self._recent: deque[Observation] = deque(maxlen=4000)
        self._last_person_ts: datetime | None = None
        self._fire_counts: dict[str, int] = defaultdict(int)
        self._eval_counts: dict[str, int] = defaultdict(int)
        self._alert_seq = 0

    # â”€â”€ state â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def _update_state(self, obs: Observation) -> EntityState | None:
        if "person" in obs.label.lower():
            self._last_person_ts = obs.ts
        self._recent.append(obs)

        if obs.entity_id is None:
            return None
        st = self._entities.get(obs.entity_id)
        if st is None:
            st = EntityState(
                first_ts=obs.ts, last_ts=obs.ts, zone_id=obs.zone_id,
                label=obs.label, zone_entered_ts=obs.ts,
            )
            self._entities[obs.entity_id] = st
            return st

        # A gap long enough that this is a new presence, not continuous dwell.
        if (obs.ts - st.last_ts) > self.history_window:
            st.first_ts = obs.ts
            st.zone_entered_ts = obs.ts
            st.zone_id = obs.zone_id
            st.pending_zone, st.pending_count = None, 0
        else:
            self._apply_zone_change(st, obs)

        st.last_ts = obs.ts
        st.label = obs.label
        st.seen += 1
        return st

    def _apply_zone_change(self, st: EntityState, obs: Observation) -> None:
        """Decide whether the subject has genuinely moved, with hysteresis.

        Leaving a zone must restart the dwell clock â€” someone who walks away and
        returns has not loitered throughout. But naively comparing zone ids frame
        to frame is wrong twice over, and both cases occur constantly in practice:

        *   **Unresolved zones.** A projection can fail (ray above horizon,
            degraded GPS) and yield ``None``. That is missing information, not
            departure, and treating it as departure erases the dwell.
        *   **Nested and adjacent zones.** The restricted core sits inside the
            substation, so jitter flips between them frame to frame. Observed on
            real footage: ``None â†’ substation â†’ restricted-core â†’ substation â†’
            None`` across ten frames, which reset the clock four times and made
            loitering undetectable in the highest-priority zone on the site.

        So a change is accepted only when the new zone is genuinely different and
        has been seen ``ZONE_CHANGE_CONFIRM`` times in a row.
        """
        new = obs.zone_id

        # No reading â€” carry the last known zone forward.
        if new is None:
            st.pending_zone, st.pending_count = None, 0
            return

        # Same zone, or one nested inside the other: not a departure.
        if self.site.zones_nested(st.zone_id, new):
            # Adopt the more specific zone so rules keyed to the inner zone match,
            # without disturbing the dwell clock.
            if st.zone_id != new and new is not None:
                st.zone_id = new
            st.pending_zone, st.pending_count = None, 0
            return

        # A genuinely different zone â€” require confirmation before believing it.
        if st.pending_zone == new:
            st.pending_count += 1
        else:
            st.pending_zone, st.pending_count = new, 1

        if st.pending_count >= ZONE_CHANGE_CONFIRM:
            st.zone_id = new
            st.zone_entered_ts = obs.ts
            st.pending_zone, st.pending_count = None, 0

    # â”€â”€ evaluation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def evaluate(
        self,
        obs: Observation,
        *,
        scene: SceneGraph | None = None,
        telemetry: Telemetry | None = None,
    ) -> list[RuleResult]:
        st = self._update_state(obs)
        results: list[RuleResult] = []

        for rule in self.rules:
            if not rule.enabled:
                continue
            self._eval_counts[rule.id] += 1
            res = self._evaluate_one(rule, obs, st, scene)
            if res.fired and st is not None:
                key = obs.entity_id if rule.cooldown_per_entity else "__rule__"
                last = st.last_alert.get(f"{rule.id}:{key}")
                if last is not None and (obs.ts - last).total_seconds() < rule.cooldown_seconds:
                    res.fired = False
                    remaining = rule.cooldown_seconds - (obs.ts - last).total_seconds()
                    res.suppressed = f"cooldown active for another {remaining:.0f}s"
                else:
                    st.last_alert[f"{rule.id}:{key}"] = obs.ts
                    self._fire_counts[rule.id] += 1
            results.append(res)
        return results

    def _evaluate_one(
        self, rule: Rule, obs: Observation, st: EntityState | None, scene: SceneGraph | None
    ) -> RuleResult:
        clauses: list[ClauseResult] = []
        dwell = st.dwell_seconds if st else 0.0
        deviation: Deviation | None = None

        # Zone conditions use the debounced zone, falling back to the entity's last
        # confirmed one. A frame whose projection failed reports ``None``, and
        # judging the rule on that would drop the subject out of the zone it is
        # demonstrably standing in.
        zone_id = obs.zone_id or (st.zone_id if st else None)

        for c in rule.conditions:
            k = c.kind

            if k is ConditionKind.CLASS_IS:
                ok = label_matches(obs.label, c.labels)
                clauses.append(ClauseResult(k, ok, f"label '{obs.label}' vs {c.labels}"))

            elif k is ConditionKind.ZONE_IN:
                # A nested zone satisfies a rule naming its parent: someone in the
                # restricted core is, factually, in the substation.
                ok = zone_id in c.zones or any(
                    self.site.zones_nested(zone_id, z) for z in c.zones
                )
                clauses.append(ClauseResult(k, ok, f"zone '{zone_id}' vs {c.zones}"))

            elif k is ConditionKind.ZONE_KIND_IN:
                z = self.site.zone_by_id(zone_id) if zone_id else None
                ok = z is not None and z.kind.value in c.zone_kinds
                clauses.append(
                    ClauseResult(k, ok, f"zone kind '{z.kind.value if z else None}' vs {c.zone_kinds}")
                )

            elif k is ConditionKind.DWELL:
                ok = dwell >= c.seconds
                clauses.append(ClauseResult(k, ok, f"dwell {dwell:.0f}s of {c.seconds:.0f}s required"))

            elif k is ConditionKind.TIME_BETWEEN:
                h = obs.ts.hour
                ok = (
                    c.start_hour <= h < c.end_hour
                    if c.start_hour <= c.end_hour
                    else (h >= c.start_hour or h < c.end_hour)   # wraps midnight
                )
                clauses.append(
                    ClauseResult(k, ok, f"hour {h:02d} vs {c.start_hour:02d}-{c.end_hour:02d}")
                )

            elif k is ConditionKind.OUTSIDE_NORMAL_HOURS:
                z = self.site.zone_by_id(zone_id) if zone_id else None
                if z is None:
                    ok, detail = False, "no zone resolved for this observation"
                elif z.normal_hours is None:
                    # A zone that declares no operating hours has no hours during
                    # which presence is routine â€” a fence line and a restricted core
                    # are never "open". Treating a missing schedule as "always
                    # within hours" was a bug: it silently disabled the after-hours
                    # rule at precisely the zones it exists to protect.
                    ok = True
                    detail = f"{z.id} declares no operating hours â€” presence is never routine"
                else:
                    lo, hi = z.normal_hours
                    ok = not (lo <= obs.ts.hour < hi)
                    detail = f"hour {obs.ts.hour:02d} vs normal {lo:02d}-{hi:02d} for {z.id}"
                clauses.append(ClauseResult(k, ok, detail))

            elif k is ConditionKind.COUNT_IN_WINDOW:
                cutoff = obs.ts - timedelta(seconds=c.window_seconds)
                n = sum(1 for o in self._recent if o.ts >= cutoff and label_matches(o.label, c.labels))
                ok = n >= c.min_count
                clauses.append(
                    ClauseResult(k, ok, f"{n} of {c.min_count} within {c.window_seconds:.0f}s")
                )

            elif k is ConditionKind.SEQUENCE:
                ok, detail = self._check_sequence(c, obs)
                clauses.append(ClauseResult(k, ok, detail))

            elif k is ConditionKind.BASELINE_ANOMALY:
                if self.baseline is None:
                    clauses.append(ClauseResult(k, False, "no baseline model available"))
                else:
                    deviation = self.baseline.evaluate(obs.zone_id, obs.ts, obs.label)
                    ok = deviation.confident and (
                        deviation.first_ever if c.require_first_ever
                        else abs(deviation.z) >= c.min_z
                    )
                    clauses.append(ClauseResult(k, ok, deviation.explanation))

            elif k is ConditionKind.ABSENCE_OF_PERSON:
                if self._last_person_ts is None:
                    ok, detail = True, "no person has ever been seen"
                else:
                    gap = (obs.ts - self._last_person_ts).total_seconds()
                    ok = gap >= c.seconds
                    detail = f"last person {gap:.0f}s ago, need {c.seconds:.0f}s"
                clauses.append(ClauseResult(k, ok, detail))

            elif k is ConditionKind.ATTRIBUTE_IS:
                val = (obs.attributes.get(c.key) or "").lower()
                ok = any(v.lower() in val for v in c.values) if val else False
                clauses.append(ClauseResult(k, ok, f"{c.key}='{val}' vs {c.values}"))

            elif k is ConditionKind.MIN_CONFIDENCE:
                ok = obs.confidence >= c.value
                clauses.append(ClauseResult(k, ok, f"confidence {obs.confidence:.2f} vs {c.value:.2f}"))

            else:
                clauses.append(ClauseResult(str(k), False, "unrecognised condition"))

        return RuleResult(
            rule=rule,
            fired=all(c.passed for c in clauses),
            clauses=clauses,
            observation=obs,
            dwell_seconds=dwell,
            deviation=deviation,
        )

    def _check_sequence(self, c, obs: Observation) -> tuple[bool, str]:
        """Did ``first`` occur, then ``then``, inside the window?

        ``obs`` must be the *then* half â€” the sequence completes on the second
        event, which is when the alert should fire.
        """
        if not label_matches(obs.label, c.then_labels):
            return False, f"current '{obs.label}' is not one of {c.then_labels}"
        cutoff = obs.ts - timedelta(seconds=c.within_seconds)
        for o in reversed(self._recent):
            if o.ts < cutoff:
                break
            if o is obs or o.entity_id == obs.entity_id:
                continue
            if not label_matches(o.label, c.first_labels):
                continue
            if c.same_zone and o.zone_id != obs.zone_id:
                continue
            gap = (obs.ts - o.ts).total_seconds()
            return True, f"'{o.label}' seen {gap:.0f}s earlier in {o.zone_id}"
        return False, f"no {c.first_labels} within the preceding {c.within_seconds:.0f}s"

    # â”€â”€ alerts â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def to_alert(
        self,
        res: RuleResult,
        *,
        scene: SceneGraph | None = None,
        telemetry: Telemetry | None = None,
        detection: Detection | None = None,
    ) -> Alert:
        """Turn a fired rule into an alert carrying its whole chain of reasoning."""
        obs = res.observation
        self._alert_seq += 1
        aid = "alr_" + hashlib.sha1(
            f"{res.rule.id}{obs.entity_id}{obs.ts}{self._alert_seq}".encode()
        ).hexdigest()[:12]

        zone = self.site.zone_by_id(obs.zone_id) if obs.zone_id else None
        where = zone.name if zone else (obs.zone_id or "an unresolved location")
        subject = obs.attributes.get("colour", "")
        subject = f"{subject} {obs.label}".strip() if subject else obs.label

        evidence: list[Evidence] = [
            Evidence(kind="detection", ref_id=obs.detection_id or obs.frame_id,
                     caption=f"{subject} detected in {where}",
                     detail={"confidence": obs.confidence, "label": obs.label}),
            Evidence(kind="frame", ref_id=obs.frame_id,
                     caption=f"Frame at {obs.ts:%H:%M:%S}"),
        ]
        for c in res.clauses:
            evidence.append(
                Evidence(kind="rule", ref_id=res.rule.id, caption=c.detail,
                         weight=1.0 if c.passed else 0.0, detail={"clause": c.kind})
            )
        if res.deviation is not None and res.deviation.confident:
            evidence.append(
                Evidence(kind="baseline", ref_id=f"{obs.zone_id}:{obs.ts.hour}",
                         caption=res.deviation.explanation,
                         detail={"z": res.deviation.z, "days": res.deviation.days_of_history})
            )
        if telemetry is not None:
            evidence.append(
                Evidence(kind="telemetry", ref_id=f"tel:{telemetry.ts.isoformat()}",
                         caption=(
                             f"Drone at {telemetry.alt_m:.0f} m, battery "
                             f"{telemetry.battery_pct:.0f}%, "
                             f"{'night' if telemetry.is_night else 'day'}, "
                             f"optical confidence {telemetry.perception_confidence:.2f}"
                         ),
                         detail=telemetry.model_dump(mode="json"))
            )
        if scene is not None:
            evidence.append(
                Evidence(kind="vlm", ref_id=obs.frame_id, caption=scene.caption,
                         detail={"confidence": scene.confidence, "tier": scene.tier})
            )

        deviation = res.deviation
        confidence = combine_confidence(
            obs.perception_confidence,
            res.rule.strength,
            deviation if deviation is not None else _null_deviation(),
        )

        severity = res.rule.severity
        # A zone can escalate severity: the same behaviour matters more at a
        # substation than in a car park.
        if zone is not None and zone.priority >= 2.0 and severity is not Severity.CRITICAL:
            order = [Severity.INFO, Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
            severity = order[min(len(order) - 1, order.index(severity) + 1)]

        location = self._locate(obs, zone, telemetry)
        if location.lat is not None:
            evidence.insert(
                1,
                Evidence(
                    kind="telemetry",
                    ref_id=f"loc:{location.lat:.6f},{location.lon:.6f}",
                    caption=f"Dispatch position â€” {location.summary}",
                    detail=location.model_dump(mode="json"),
                ),
            )

        return Alert(
            id=aid,
            site_id=self.site.id,
            rule_id=res.rule.id,
            rule_name=res.rule.name,
            severity=severity,
            title=f"{res.rule.name}: {subject} at {where}",
            narrative="",  # filled by the narrative builder
            ts=obs.ts,
            zone_id=obs.zone_id or (zone.id if zone else None),
            location=location,
            entity_ids=[obs.entity_id] if obs.entity_id else [],
            frame_ids=[obs.frame_id],
            evidence=evidence,
            confidence=confidence,
            baseline_deviation=float(deviation.score) if deviation else 0.0,
            status=AlertStatus.OPEN,
        )

    # â”€â”€ dispatch position â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def _locate(self, obs: Observation, zone, telemetry: Telemetry | None) -> AlertLocation:
        """Work out where to send an aircraft, and how confident we are in it.

        Three sources, in descending order of accuracy, and the alert records which
        one was used. Flying to a zone centroid while believing it is a metre-
        accurate fix is how a responder ends up at the wrong end of a yard.
        """
        loc = AlertLocation(
            zone_id=obs.zone_id or (zone.id if zone else None),
            zone_name=zone.name if zone else None,
        )

        if obs.world is not None:
            loc.lat, loc.lon = obs.world.lat, obs.world.lon
            loc.source = "geo-projection"
            loc.confidence = obs.projection_confidence
            # Projection error grows as confidence falls; surface it as a radius
            # rather than implying a point fix.
            loc.accuracy_m = round(3.0 + (1.0 - obs.projection_confidence) * 25.0, 1)
        elif zone is not None:
            c = zone.centroid
            loc.lat, loc.lon = c.lat, c.lon
            loc.source = "zone-centroid"
            loc.confidence = 0.45
            # Half the zone's diagonal is the honest accuracy of a centroid.
            lats = [p.lat for p in zone.polygon]
            lons = [p.lon for p in zone.polygon]
            span = LatLon(lat=min(lats), lon=min(lons)).haversine_m(
                LatLon(lat=max(lats), lon=max(lons))
            )
            loc.accuracy_m = round(span / 2, 1)
        elif telemetry is not None:
            loc.lat, loc.lon = telemetry.lat, telemetry.lon
            loc.source = "drone-position"
            loc.confidence = 0.25
            loc.accuracy_m = round(max(10.0, telemetry.alt_m), 1)

        if telemetry is not None:
            loc.drone_lat, loc.drone_lon = telemetry.lat, telemetry.lon
            loc.drone_alt_m = telemetry.alt_m

        dock = self.site.dock or self.site.origin
        loc.dock_lat, loc.dock_lon = dock.lat, dock.lon

        if loc.lat is not None and loc.lon is not None:
            target = LatLon(lat=loc.lat, lon=loc.lon)
            loc.distance_from_dock_m = round(dock.haversine_m(target), 1)
            loc.bearing_from_dock_deg = round(_bearing(dock, target), 1)
            # Cruise speed from the flight model, plus launch and climb overhead.
            loc.eta_seconds = round(loc.distance_from_dock_m / 12.0 + 20.0, 1)

            if self.site.geofence:
                from kestrel.domain import Zone, ZoneKind

                fence = Zone(id="gf", name="gf", kind=ZoneKind.PERIMETER,
                             polygon=self.site.geofence)
                loc.within_geofence = fence.contains(target)

            # Fly lower for a closer look, but not so low that a 3 m fence or a
            # warehouse roof becomes a problem.
            priority = zone.priority if zone else 1.0
            loc.recommended_altitude_m = 18.0 if priority >= 2.0 else 25.0

        return loc

    # â”€â”€ read-out â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    @property
    def stats(self) -> dict[str, Any]:
        return {
            "rules": len(self.rules),
            "enabled": sum(1 for r in self.rules if r.enabled),
            "tracked_entities": len(self._entities),
            "evaluations": dict(self._eval_counts),
            "fires": dict(self._fire_counts),
        }

    def reset(self) -> None:
        self._entities.clear()
        self._recent.clear()
        self._last_person_ts = None
        self._fire_counts.clear()
        self._eval_counts.clear()


def _null_deviation() -> Deviation:
    return Deviation(0.0, 0.0, 0, 0.0, 0.0, 0, False, False, "no baseline")


def _bearing(origin: LatLon, target: LatLon) -> float:
    """Initial great-circle bearing in degrees from true north.

    Given to the operator alongside the distance so a heading is available even if
    the map is unavailable â€” which, on a site at 02:00, it sometimes is.
    """
    p1 = math.radians(origin.lat)
    p2 = math.radians(target.lat)
    dl = math.radians(target.lon - origin.lon)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0

