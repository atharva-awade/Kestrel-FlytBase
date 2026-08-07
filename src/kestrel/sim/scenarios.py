"""Scripted scenarios.

These serve three distinct purposes and are written to serve all three at once:

1.  **Literal specification compliance.** The assignment names two expected
    outputs — "Blue Ford F150 spotted at garage, 12:00" and "Person loitering at
    main gate, 00:01". ``delivery_daytime`` and ``loiter_midnight`` reproduce
    exactly those, so the brief can be ticked off against a running system.
2.  **Rule coverage.** Every rule in the default pack has at least one scenario
    that should fire it and, more importantly, at least one that should *not* —
    a rule engine is only as good as its false-positive behaviour.
3.  **Deterministic evaluation.** Each scenario carries ground-truth expectations,
    so precision and recall are measured rather than asserted.

Times are expressed on the site clock. A scenario pins its own timestamps where the
hour is the point of the test — you cannot demonstrate a midnight rule at 14:00.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


@dataclass
class Scenario:
    id: str
    title: str
    description: str
    frames: list[dict[str, Any]]
    # What a correct system should conclude. Drives the eval harness.
    expect_alerts: list[str] = field(default_factory=list)
    expect_no_alerts: list[str] = field(default_factory=list)
    expect_entities: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "expect_alerts": self.expect_alerts,
            "expect_no_alerts": self.expect_no_alerts,
            "expect_entities": self.expect_entities,
            "tags": self.tags,
            "frames": self.frames,
        }


def _seq(day: str, start: str, texts: list[str], step_s: int = 30) -> list[dict[str, Any]]:
    """Build a frame list from a start time and a list of descriptions."""
    t0 = datetime.fromisoformat(f"{day}T{start}")
    return [
        {"at": (t0 + timedelta(seconds=i * step_s)).isoformat(), "text": txt}
        for i, txt in enumerate(texts)
    ]


DAY1, DAY2, DAY3, DAY4 = "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06"


# ═══════════════════════════════════════════════════════════════════════════════
def delivery_daytime() -> Scenario:
    """The assignment's first named output, reproduced literally."""
    return Scenario(
        id="delivery-daytime",
        title="Routine daytime delivery",
        description=(
            "A blue Ford F-150 arrives at the loading dock at midday, a driver "
            "unloads, and the vehicle departs. Nothing here should alert; this is "
            "the scenario that proves the system is not merely alarm-happy."
        ),
        frames=_seq(
            DAY4,
            "11:58:00",
            [
                "Frame: Empty loading dock, six bays clear, overcast midday light.",
                "Frame: A blue Ford F-150 pickup approaches the main gate and stops at the barrier.",
                "Frame: Blue Ford F-150 passes the raised barrier and turns onto the access road.",
                "Frame: Blue Ford F-150 reverses into loading dock bay 3 at the garage.",
                "Frame: Driver in a high-visibility vest steps out of the blue Ford F-150 at the dock.",
                "Frame: Driver unloads two pallets onto the dock apron beside the pickup.",
                "Frame: Driver returns to the cab of the blue Ford F-150.",
                "Frame: Blue Ford F-150 pulls away from bay 3 towards the access road.",
                "Frame: Blue Ford F-150 exits through the main gate; barrier lowers.",
                "Frame: Loading dock quiet, two pallets stacked on the apron.",
            ],
            step_s=45,
        ),
        expect_alerts=[],
        expect_no_alerts=["loitering", "after-hours-vehicle", "perimeter-breach"],
        expect_entities=["blue Ford F-150", "person"],
        tags=["baseline", "spec-example", "true-negative"],
    )


def loiter_midnight() -> Scenario:
    """The assignment's second named output, reproduced literally."""
    return Scenario(
        id="loiter-midnight",
        title="Person loitering at the main gate after midnight",
        description=(
            "A person on foot arrives at the main gate at 00:01 and remains for "
            "over four minutes, pacing and looking through the gate. No vehicle, no "
            "badge, no scheduled activity. This is the canonical alert."
        ),
        frames=_seq(
            DAY4,
            "00:00:30",
            [
                "Frame: Main gate closed and unlit apart from a single sodium lamp; no activity.",
                "Frame: A person on foot approaches the main gate from the access road.",
                "Frame: Person stands at the main gate, looking through the bars into the site.",
                "Frame: Person still at the main gate, walking slowly along the fence to the left.",
                "Frame: Person returns to the main gate and stands motionless.",
                "Frame: Person at the main gate, reaching towards the gate latch.",
                "Frame: Person still present at the main gate, now crouching near the post.",
                "Frame: Person at the main gate looking back over their shoulder repeatedly.",
                "Frame: Person walks away from the main gate along the fence line.",
                "Frame: Main gate empty; sodium lamp flickering.",
            ],
            step_s=35,
        ),
        expect_alerts=["loitering"],
        expect_no_alerts=[],
        expect_entities=["person"],
        tags=["alert", "spec-example", "night", "true-positive"],
    )


def fence_breach_night() -> Scenario:
    return Scenario(
        id="fence-breach-night",
        title="Fence-line approach and climb attempt",
        description=(
            "A vehicle parks on the perimeter road with lights off, one occupant "
            "walks the fence line, pauses at a camera blind spot, then attempts to "
            "climb. Severity should escalate across the sequence rather than "
            "arriving fully formed at the first frame."
        ),
        frames=_seq(
            DAY4,
            "02:14:00",
            [
                "Frame: Dark perimeter road outside the fence line; no activity.",
                "Frame: A dark grey sedan stops on the perimeter road with headlights switched off.",
                "Frame: A person exits the dark grey sedan and stands beside the fence line.",
                "Frame: Person walks slowly along the perimeter fence, looking upward at the posts.",
                "Frame: Person pauses at the fence line beside the substation corner.",
                "Frame: Person places both hands on the perimeter fence and pulls, testing it.",
                "Frame: Person begins climbing the perimeter fence near the substation.",
                "Frame: Person at the top of the perimeter fence beside the substation.",
                "Frame: Person drops inside the fence line and moves towards the substation.",
                "Frame: Person at the electrical substation, crouched beside a transformer housing.",
            ],
            step_s=40,
        ),
        expect_alerts=["perimeter-breach", "restricted-zone", "after-hours-vehicle"],
        expect_no_alerts=[],
        expect_entities=["person", "dark grey sedan"],
        tags=["alert", "night", "critical", "escalation", "true-positive"],
    )


def repeat_visitor() -> Scenario:
    """Cross-day entity memory: the same vehicle, four days, then an odd hour.

    This is the scenario that separates an analyst from a caption bot. Nothing in
    any single frame is alarming; the seventh visit at 02:00 is only notable
    against the memory of the previous six.
    """
    frames: list[dict[str, Any]] = []
    for day, hour in [(DAY1, "09:12:00"), (DAY1, "16:40:00"), (DAY2, "09:05:00"),
                      (DAY2, "17:02:00"), (DAY3, "08:58:00"), (DAY3, "15:31:00")]:
        frames += _seq(
            day,
            hour,
            [
                "Frame: A blue Ford F-150 pickup arrives at the main gate.",
                "Frame: Blue Ford F-150 parked at the loading dock, driver unloading.",
                "Frame: Blue Ford F-150 departs through the main gate.",
            ],
            step_s=120,
        )
    frames += _seq(
        DAY4,
        "02:03:00",
        [
            "Frame: A blue Ford F-150 pickup approaches the main gate with headlights off.",
            "Frame: Blue Ford F-150 stops short of the main gate; engine running, no one exits.",
            "Frame: Blue Ford F-150 reverses and parks facing the exit beside the fence line.",
            "Frame: Blue Ford F-150 stationary at the fence line, occupant visible in cab.",
        ],
        step_s=60,
    )
    return Scenario(
        id="repeat-visitor",
        title="Known vehicle, unknown hour",
        description=(
            "A blue Ford F-150 visits on six occasions across three days, always "
            "during working hours. On the fourth day it returns at 02:03 with lights "
            "off and does not unload. Each individual frame is unremarkable."
        ),
        frames=frames,
        expect_alerts=["baseline-anomaly", "after-hours-vehicle"],
        expect_no_alerts=[],
        expect_entities=["blue Ford F-150"],
        tags=["memory", "baseline", "entity-reid", "multi-day", "true-positive"],
    )


def wildlife_false_positive() -> Scenario:
    """A rule engine that cannot decline is not useful. This must stay quiet."""
    return Scenario(
        id="wildlife-false-positive",
        title="Animal movement at the fence line",
        description=(
            "A stray dog moves along the fence line at 03:00 and lingers. Motion, "
            "night-time, high-priority zone, extended dwell: every surface signal "
            "of an intrusion. The system must not raise a perimeter breach."
        ),
        frames=_seq(
            DAY3,
            "03:02:00",
            [
                "Frame: Fence line quiet under yard lighting; a light breeze moves vegetation.",
                "Frame: A stray dog trots along the inside of the perimeter fence.",
                "Frame: The dog stops beside the fence line and sniffs at the base of a post.",
                "Frame: The dog remains at the fence line, lying down beside the post.",
                "Frame: The dog is still at the fence line, moving occasionally.",
                "Frame: The dog stands and continues along the fence line out of view.",
            ],
            step_s=45,
        ),
        expect_alerts=[],
        expect_no_alerts=["perimeter-breach", "loitering", "restricted-zone"],
        expect_entities=["dog"],
        tags=["false-positive", "night", "true-negative"],
    )


def unattended_package() -> Scenario:
    return Scenario(
        id="unattended-package",
        title="Package left at the gate",
        description=(
            "A courier leaves a package beside the main gate and departs. The "
            "package remains for a prolonged period. Tests the 'object appears and "
            "persists without an owner' pattern, which needs entity persistence "
            "rather than a per-frame rule."
        ),
        frames=_seq(
            DAY4,
            "19:40:00",
            [
                "Frame: Main gate area clear at dusk.",
                "Frame: A white van stops on the access road beside the main gate.",
                "Frame: A courier carries a cardboard box from the van towards the main gate.",
                "Frame: Courier sets the cardboard box down beside the main gate post.",
                "Frame: Courier returns to the white van without the box.",
                "Frame: White van departs; cardboard box remains beside the main gate.",
                "Frame: Cardboard box still beside the main gate; no people present.",
                "Frame: Cardboard box still beside the main gate under gate lighting.",
                "Frame: Cardboard box unattended at the main gate.",
            ],
            step_s=90,
        ),
        expect_alerts=["unattended-object"],
        expect_no_alerts=["perimeter-breach"],
        expect_entities=["white van", "person", "cardboard box"],
        tags=["alert", "dusk", "object-persistence", "true-positive"],
    )


def shift_change_busy() -> Scenario:
    """Load and suppression: many benign detections at once must not flood."""
    texts = [
        "Frame: Staff parking filling as the shift changes; several cars arriving.",
        "Frame: Six people walking from staff parking towards warehouse A.",
        "Frame: A silver hatchback parks in staff parking bay 12.",
        "Frame: Four people at the warehouse A entrance, badging in.",
        "Frame: Two forklifts moving pallets in the yard.",
        "Frame: A white van arrives at the loading dock for a scheduled collection.",
        "Frame: Eight people crossing the access road towards the car park.",
        "Frame: A red hatchback departs staff parking towards the main gate.",
        "Frame: Yard busy with two forklifts and three people in high-visibility vests.",
        "Frame: Loading dock activity: van loading, two staff assisting.",
        "Frame: Staff parking half empty; people walking towards the main gate.",
        "Frame: Quiet yard; one forklift parked, no people visible.",
    ]
    return Scenario(
        id="shift-change-busy",
        title="Shift change: high benign volume",
        description=(
            "Twelve frames of heavy but entirely normal activity during the 18:00 "
            "shift change. Exercises alert suppression and cooldowns: a system that "
            "raises twenty alerts here would be turned off within a week."
        ),
        frames=_seq(DAY4, "17:52:00", texts, step_s=40),
        expect_alerts=[],
        expect_no_alerts=["loitering", "perimeter-breach", "after-hours-vehicle"],
        expect_entities=["person", "forklift", "white van"],
        tags=["load", "suppression", "true-negative"],
    )


def tailgating() -> Scenario:
    return Scenario(
        id="tailgating",
        title="Vehicle tailgates through the gate",
        description=(
            "An authorised truck is admitted; an unauthorised car follows through "
            "the closing barrier. Requires a sequence rule, 'B within N seconds of "
            "A', which a per-frame rule cannot express."
        ),
        frames=_seq(
            DAY4,
            "21:14:00",
            [
                "Frame: Main gate barrier closed; a white box truck waits at the reader.",
                "Frame: Barrier rises; the white box truck moves forward through the gate.",
                "Frame: White box truck clears the gate onto the access road.",
                "Frame: A dark grey sedan accelerates through the gate behind the truck before the barrier lowers.",
                "Frame: Dark grey sedan on the access road following the white box truck.",
                "Frame: Barrier lowered; dark grey sedan continues towards the yard.",
                "Frame: Dark grey sedan parked at the far end of the yard, lights off.",
            ],
            step_s=20,
        ),
        expect_alerts=["tailgating", "after-hours-vehicle"],
        expect_no_alerts=[],
        expect_entities=["white box truck", "dark grey sedan"],
        tags=["alert", "sequence-rule", "evening", "true-positive"],
    )


ALL: list[Scenario] = [
    delivery_daytime(),
    loiter_midnight(),
    fence_breach_night(),
    repeat_visitor(),
    wildlife_false_positive(),
    unattended_package(),
    shift_change_busy(),
    tailgating(),
]


def by_id(scenario_id: str) -> Scenario:
    s = next((x for x in ALL if x.id == scenario_id), None)
    if s is None:
        raise KeyError(f"unknown scenario: {scenario_id} (have: {[x.id for x in ALL]})")
    return s


def write_scenarios(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for s in ALL:
        p = out_dir / f"{s.id}.json"
        p.write_text(json.dumps(s.to_dict(), indent=2), encoding="utf-8")
        paths.append(p)
    idx = out_dir / "index.json"
    idx.write_text(
        json.dumps(
            {
                "scenarios": [
                    {
                        "id": s.id,
                        "title": s.title,
                        "frames": len(s.frames),
                        "tags": s.tags,
                        "expect_alerts": s.expect_alerts,
                    }
                    for s in ALL
                ]
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    paths.append(idx)
    return paths
