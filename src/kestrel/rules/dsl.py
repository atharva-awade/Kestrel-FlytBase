"""The rule language.

A security rule is almost never a property of a single frame. "Loitering" is dwell
over time; "tailgating" is one event following another within a window; "unattended
object" is a thing that persists after the person who left it has gone. An
`if detection.label == "person"` check cannot express any of them.

So rules are declarative and *temporal*, defined as data rather than code:

```yaml
id: loitering
name: Person loitering at a sensitive location
severity: high
conditions:
  - kind: class_is
    labels: [person]
  - kind: zone_in
    zones: [main-gate, fence-line, substation]
  - kind: dwell
    seconds: 120
  - kind: time_between
    start_hour: 22
    end_hour: 5
cooldown_seconds: 300
visual_predicate: "a person standing still near a gate at night"
```

Two consequences of being data:

*   **Rules can be authored by a language model.** ``compiler.py`` turns an English
    sentence into one of these and validates it against the schema, so a bad
    generation is rejected rather than executed.
*   **Rules can be backtested.** Because evaluation is a pure function of the
    indexed history, a proposed rule can be replayed over past days *before* it is
    enabled, and the operator is shown what it would have done.

``visual_predicate`` is the open-vocabulary hook: a rule may carry its own detector
prompt, letting it detect things no fixed class list covers.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from kestrel.domain import Severity


class ConditionKind(StrEnum):
    CLASS_IS = "class_is"
    ZONE_IN = "zone_in"
    ZONE_KIND_IN = "zone_kind_in"
    DWELL = "dwell"
    TIME_BETWEEN = "time_between"
    COUNT_IN_WINDOW = "count_in_window"
    SEQUENCE = "sequence"
    BASELINE_ANOMALY = "baseline_anomaly"
    ABSENCE_OF_PERSON = "absence_of_person"
    ATTRIBUTE_IS = "attribute_is"
    MIN_CONFIDENCE = "min_confidence"
    OUTSIDE_NORMAL_HOURS = "outside_normal_hours"


class _C(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClassIs(_C):
    kind: Literal[ConditionKind.CLASS_IS] = ConditionKind.CLASS_IS
    labels: list[str] = Field(min_length=1)


class ZoneIn(_C):
    kind: Literal[ConditionKind.ZONE_IN] = ConditionKind.ZONE_IN
    zones: list[str] = Field(min_length=1)


class ZoneKindIn(_C):
    kind: Literal[ConditionKind.ZONE_KIND_IN] = ConditionKind.ZONE_KIND_IN
    zone_kinds: list[str] = Field(min_length=1)


class Dwell(_C):
    """Continuous presence of one entity for at least ``seconds``."""

    kind: Literal[ConditionKind.DWELL] = ConditionKind.DWELL
    seconds: float = Field(gt=0)


class TimeBetween(_C):
    """Local-clock window. Wraps midnight when start > end (22 → 5 is 7 hours)."""

    kind: Literal[ConditionKind.TIME_BETWEEN] = ConditionKind.TIME_BETWEEN
    start_hour: int = Field(ge=0, le=23)
    end_hour: int = Field(ge=0, le=23)


class CountInWindow(_C):
    kind: Literal[ConditionKind.COUNT_IN_WINDOW] = ConditionKind.COUNT_IN_WINDOW
    labels: list[str] = Field(min_length=1)
    window_seconds: float = Field(gt=0)
    min_count: int = Field(ge=1)


class Sequence(_C):
    """``first`` observed, then ``then`` within ``within_seconds``.

    The condition tailgating needs and no per-frame predicate can express.
    """

    kind: Literal[ConditionKind.SEQUENCE] = ConditionKind.SEQUENCE
    first_labels: list[str] = Field(min_length=1)
    then_labels: list[str] = Field(min_length=1)
    within_seconds: float = Field(gt=0)
    same_zone: bool = True


class BaselineAnomaly(_C):
    kind: Literal[ConditionKind.BASELINE_ANOMALY] = ConditionKind.BASELINE_ANOMALY
    min_z: float = 2.0
    require_first_ever: bool = False


class AbsenceOfPerson(_C):
    """An object still present while no person has been seen for ``seconds``.

    "Unattended" is the absence of an owner, not a property of the object.
    """

    kind: Literal[ConditionKind.ABSENCE_OF_PERSON] = ConditionKind.ABSENCE_OF_PERSON
    seconds: float = Field(gt=0)


class AttributeIs(_C):
    kind: Literal[ConditionKind.ATTRIBUTE_IS] = ConditionKind.ATTRIBUTE_IS
    key: str
    values: list[str] = Field(min_length=1)


class MinConfidence(_C):
    kind: Literal[ConditionKind.MIN_CONFIDENCE] = ConditionKind.MIN_CONFIDENCE
    value: float = Field(ge=0, le=1)


class OutsideNormalHours(_C):
    """Uses the zone's own declared operating hours rather than a literal window."""

    kind: Literal[ConditionKind.OUTSIDE_NORMAL_HOURS] = ConditionKind.OUTSIDE_NORMAL_HOURS


Condition = Annotated[
    ClassIs | ZoneIn | ZoneKindIn | Dwell | TimeBetween | CountInWindow | Sequence
    | BaselineAnomaly | AbsenceOfPerson | AttributeIs | MinConfidence | OutsideNormalHours,
    Field(discriminator="kind"),
]


class Rule(BaseModel):
    """One declarative, temporal security rule."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str = ""
    severity: Severity = Severity.MEDIUM
    enabled: bool = True
    conditions: list[Condition] = Field(min_length=1)

    # Suppression. Without this, a person standing at a gate for ten minutes
    # generates an alert per frame and the operator stops reading them.
    cooldown_seconds: float = 300.0
    # Per-entity rather than per-rule cooldown, so two simultaneous intruders both
    # alert while one intruder does not alert twice.
    cooldown_per_entity: bool = True

    # Open-vocabulary detector prompt. Lets a rule detect something outside the
    # fixed class list — the mechanism behind promptable rules.
    visual_predicate: str | None = None

    # How much this rule's firing contributes to alert confidence. A heuristic
    # rule should not produce confident alerts merely because it matched.
    strength: float = Field(default=0.8, ge=0, le=1)

    # Provenance, so the UI can distinguish shipped rules from generated ones.
    origin: Literal["builtin", "natural_language", "operator"] = "builtin"
    source_text: str | None = None
    tags: list[str] = Field(default_factory=list)

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            self.model_dump(mode="json", exclude_defaults=False),
            sort_keys=False,
            allow_unicode=True,
        )

    @classmethod
    def from_yaml(cls, text: str) -> Rule:
        return cls.model_validate(yaml.safe_load(text))

    @property
    def needs_history(self) -> bool:
        """True when the rule cannot be decided from a single frame.

        Used by the engine to decide whether an entity needs a tracked state
        machine, and by the UI to explain why a rule did not fire immediately.
        """
        return any(
            c.kind
            in (
                ConditionKind.DWELL,
                ConditionKind.COUNT_IN_WINDOW,
                ConditionKind.SEQUENCE,
                ConditionKind.ABSENCE_OF_PERSON,
                ConditionKind.BASELINE_ANOMALY,
            )
            for c in self.conditions
        )

    def explain(self) -> list[str]:
        """Human-readable clauses, shown in the evidence panel beside what matched."""
        out: list[str] = []
        for c in self.conditions:
            out.append(_explain_condition(c))
        return out


def _explain_condition(c) -> str:
    k = c.kind
    if k is ConditionKind.CLASS_IS:
        return f"the object is one of: {', '.join(c.labels)}"
    if k is ConditionKind.ZONE_IN:
        return f"it is in zone {', '.join(c.zones)}"
    if k is ConditionKind.ZONE_KIND_IN:
        return f"the zone is of type {', '.join(c.zone_kinds)}"
    if k is ConditionKind.DWELL:
        return f"it has stayed for at least {c.seconds:.0f}s"
    if k is ConditionKind.TIME_BETWEEN:
        return f"the local time is between {c.start_hour:02d}:00 and {c.end_hour:02d}:00"
    if k is ConditionKind.COUNT_IN_WINDOW:
        return (
            f"at least {c.min_count} of {', '.join(c.labels)} appeared "
            f"within {c.window_seconds:.0f}s"
        )
    if k is ConditionKind.SEQUENCE:
        return (
            f"{', '.join(c.first_labels)} was followed by {', '.join(c.then_labels)} "
            f"within {c.within_seconds:.0f}s"
        )
    if k is ConditionKind.BASELINE_ANOMALY:
        return (
            "this is unprecedented for the time and place"
            if c.require_first_ever
            else f"activity deviates from normal by at least {c.min_z:.1f} sd"
        )
    if k is ConditionKind.ABSENCE_OF_PERSON:
        return f"no person has been seen for {c.seconds:.0f}s"
    if k is ConditionKind.ATTRIBUTE_IS:
        return f"its {c.key} is one of: {', '.join(c.values)}"
    if k is ConditionKind.MIN_CONFIDENCE:
        return f"detection confidence is at least {c.value:.2f}"
    if k is ConditionKind.OUTSIDE_NORMAL_HOURS:
        return "it is outside the zone's normal operating hours"
    return str(k)


# JSON Schema handed to the language model when compiling English into a rule.
# Generated from the models rather than written by hand, so it can never drift
# from what the engine will actually accept.
def rule_json_schema() -> dict:
    return Rule.model_json_schema()
