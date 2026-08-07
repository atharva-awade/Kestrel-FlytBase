"""The default rule pack.

Eight rules covering the scenarios in ``sim/scenarios.py``, including the two the
assignment names explicitly. They are written to be *legible* — a site manager
should be able to read one and agree or disagree with it — because a rule nobody
understands is a rule nobody trusts.

Every rule here has a scenario that should fire it and at least one that should
not. The false-negative cases matter more: a system that alerts on the delivery
driver and the stray dog gets switched off in a week, and then it protects nothing.
"""

from __future__ import annotations

from kestrel.domain import Severity
from kestrel.rules.dsl import (
    AbsenceOfPerson,
    BaselineAnomaly,
    ClassIs,
    CountInWindow,
    Dwell,
    MinConfidence,
    OutsideNormalHours,
    Rule,
    Sequence,
    TimeBetween,
    ZoneIn,
    ZoneKindIn,
)


def default_rules() -> list[Rule]:
    return [
        # ── the assignment's named example ────────────────────────────────
        Rule(
            id="loitering",
            name="Loitering at a sensitive location",
            description=(
                "A person remains at a gate, fence line or substation for over two "
                "minutes during the night. The dwell requirement is what separates "
                "this from someone simply walking past."
            ),
            severity=Severity.HIGH,
            conditions=[
                ClassIs(labels=["person"]),
                ZoneIn(zones=["main-gate", "fence-line", "substation", "restricted-core"]),
                Dwell(seconds=120),
                TimeBetween(start_hour=22, end_hour=5),
                MinConfidence(value=0.35),
            ],
            cooldown_seconds=300,
            visual_predicate="a person standing still or pacing near a gate or fence at night",
            strength=0.85,
            tags=["intrusion", "night", "spec-example"],
        ),
        # ── perimeter ─────────────────────────────────────────────────────
        Rule(
            id="perimeter-breach",
            name="Perimeter fence activity",
            description=(
                "Any person at the fence line. There is no legitimate reason to be "
                "there at any hour, so unlike loitering this needs no time window, "
                "only enough dwell to rule out a passing detection."
            ),
            severity=Severity.CRITICAL,
            conditions=[
                ClassIs(labels=["person"]),
                ZoneKindIn(zone_kinds=["fence"]),
                Dwell(seconds=20),
                MinConfidence(value=0.3),
            ],
            cooldown_seconds=180,
            visual_predicate="a person climbing or standing against a perimeter fence",
            strength=0.9,
            tags=["intrusion", "perimeter"],
        ),
        Rule(
            id="restricted-zone",
            name="Presence in a restricted area",
            description=(
                "Anyone inside the substation's restricted core outside permitted "
                "hours. High-value copper and live equipment: presence is the event."
            ),
            severity=Severity.CRITICAL,
            conditions=[
                ClassIs(labels=["person"]),
                ZoneIn(zones=["restricted-core", "substation"]),
                OutsideNormalHours(),
                Dwell(seconds=15),
            ],
            cooldown_seconds=240,
            visual_predicate="a person inside an electrical substation compound",
            strength=0.9,
            tags=["intrusion", "critical-asset"],
        ),
        # ── vehicles ──────────────────────────────────────────────────────
        Rule(
            id="after-hours-vehicle",
            name="Vehicle present outside operating hours",
            description=(
                "A vehicle in an operational zone when that zone is closed. Uses the "
                "zone's own declared hours rather than a fixed window, so the loading "
                "dock and the car park are judged by their own schedules."
            ),
            severity=Severity.MEDIUM,
            conditions=[
                ClassIs(labels=["car", "truck", "van", "bus", "motorcycle"]),
                ZoneIn(zones=["loading-dock", "main-gate", "yard", "access-road", "fence-line"]),
                OutsideNormalHours(),
                Dwell(seconds=30),
            ],
            cooldown_seconds=600,
            strength=0.75,
            tags=["vehicle", "after-hours"],
        ),
        Rule(
            id="tailgating",
            name="Vehicle followed another through the gate",
            description=(
                "A second vehicle passes the gate within twenty seconds of the first. "
                "Expressible only as a sequence; no per-frame predicate can see it."
            ),
            severity=Severity.HIGH,
            conditions=[
                Sequence(
                    first_labels=["truck", "bus", "van"],
                    then_labels=["car", "motorcycle", "van"],
                    within_seconds=25,
                    # Deliberately off. By the moment the second vehicle is at the
                    # barrier, the first has already cleared it onto the access
                    # road — that is what tailgating *is*. Requiring both to be in
                    # the identical zone at the same instant describes a queue, not
                    # a tailgate, and the ZoneIn clause below already constrains
                    # both events to the gate area.
                    same_zone=False,
                ),
                ZoneIn(zones=["main-gate", "access-road"]),
            ],
            cooldown_seconds=300,
            strength=0.7,
            tags=["vehicle", "access-control", "sequence"],
        ),
        # ── objects ───────────────────────────────────────────────────────
        Rule(
            id="unattended-object",
            name="Unattended object near an access point",
            description=(
                "A bag or package persists near the gate with no person seen for two "
                "minutes. 'Unattended' is the absence of an owner, so the rule is "
                "written as an absence condition rather than a property of the object."
            ),
            severity=Severity.HIGH,
            conditions=[
                ClassIs(labels=["backpack", "suitcase", "handbag", "cardboard box", "box"]),
                ZoneIn(zones=["main-gate", "access-road", "loading-dock"]),
                Dwell(seconds=90),
                AbsenceOfPerson(seconds=120),
            ],
            cooldown_seconds=900,
            visual_predicate="an unattended bag or package left on the ground",
            strength=0.7,
            tags=["object", "unattended"],
        ),
        # ── statistical ───────────────────────────────────────────────────
        Rule(
            id="baseline-anomaly",
            name="Unprecedented activity for this time and place",
            description=(
                "Something is happening that has never happened in this zone at this "
                "hour across the observed history. This is the rule nobody had to "
                "write in advance; it catches what the others were not designed for."
            ),
            severity=Severity.MEDIUM,
            conditions=[
                BaselineAnomaly(require_first_ever=True),
                Dwell(seconds=30),
                MinConfidence(value=0.4),
            ],
            cooldown_seconds=1800,
            strength=0.6,
            tags=["anomaly", "statistical"],
        ),
        # ── crowding ──────────────────────────────────────────────────────
        Rule(
            id="unusual-gathering",
            name="Unusual gathering outside working hours",
            description=(
                "Four or more people within two minutes in an operational zone when "
                "it should be empty. Deliberately not active during shift change, "
                "which is what the count-in-window plus hour condition achieves."
            ),
            severity=Severity.MEDIUM,
            conditions=[
                ClassIs(labels=["person"]),
                CountInWindow(labels=["person"], window_seconds=120, min_count=4),
                TimeBetween(start_hour=23, end_hour=5),
                ZoneIn(zones=["yard", "loading-dock", "main-gate"]),
            ],
            cooldown_seconds=600,
            strength=0.65,
            tags=["crowd", "night"],
        ),
    ]


def rule_by_id(rule_id: str) -> Rule | None:
    return next((r for r in default_rules() if r.id == rule_id), None)
