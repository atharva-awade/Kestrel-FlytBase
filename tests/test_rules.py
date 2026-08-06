"""Rule engine, temporal conditions and triage.

The assignment names two expected outputs. Both are asserted here, along with the
cases that must *not* fire â€” which matter more, because a security system that
cries wolf is switched off and then protects nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from kestrel.domain import Severity
from kestrel.memory.baseline import BaselineModel
from kestrel.rules.dsl import (
    ClassIs,
    Rule,
    TimeBetween,
)
from kestrel.rules.engine import Observation, RuleEngine
from kestrel.rules.pack import default_rules, rule_by_id
from kestrel.rules.triage import NarrativeBuilder, Triage
from kestrel.sim.sites import build_plant_01

NIGHT = datetime(2026, 8, 6, 0, 1, 0)
NOON = datetime(2026, 8, 6, 12, 0, 0)


@pytest.fixture(scope="module")
def site():
    return build_plant_01()


def must_rule(rule_id: str) -> Rule:
    """`rule_by_id` returns Optional; every test here requires the rule to exist."""
    r = rule_by_id(rule_id)
    assert r is not None, f"default pack is missing rule '{rule_id}'"
    return r


def obs(ts, label="person", zone="main-gate", entity="ENT-1", conf=0.9, **kw):
    return Observation(
        ts=ts, frame_id=f"frm_{ts:%H%M%S}", entity_id=entity, label=label,
        confidence=conf, zone_id=zone, **kw,
    )


def feed(engine, observations):
    """Run a sequence and return every rule result that fired."""
    fired = []
    for o in observations:
        for r in engine.evaluate(o):
            if r.fired:
                fired.append(r)
    return fired


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# The assignment's named outputs
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
def test_person_loitering_at_main_gate_at_midnight_fires(site):
    """'Person loitering at main gate, 00:01' â€” the assignment's example."""
    engine = RuleEngine(site, [must_rule("loitering")])
    seq = [obs(NIGHT + timedelta(seconds=i * 30)) for i in range(8)]
    fired = feed(engine, seq)
    assert fired, "loitering did not fire on four minutes of presence at midnight"
    assert fired[0].rule.id == "loitering"
    assert fired[0].dwell_seconds >= 120


def test_daytime_delivery_raises_nothing(site):
    """The blue-F150-at-noon scenario must stay silent across the whole pack."""
    engine = RuleEngine(site, default_rules())
    seq = [
        obs(NOON + timedelta(seconds=i * 45), label="truck", zone="loading-dock",
            entity="ENT-F150", attributes={"colour": "blue", "kind": "pickup truck"})
        for i in range(10)
    ]
    assert feed(engine, seq) == [], "a routine midday delivery generated an alert"


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Temporal conditions
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
def test_brief_presence_does_not_satisfy_dwell(site):
    engine = RuleEngine(site, [must_rule("loitering")])
    seq = [obs(NIGHT + timedelta(seconds=i * 10)) for i in range(4)]  # 30s only
    assert feed(engine, seq) == []


def test_leaving_and_returning_restarts_the_dwell_clock(site):
    """Someone who walks away and comes back has not loitered continuously."""
    engine = RuleEngine(site, [must_rule("loitering")])
    seq = [obs(NIGHT + timedelta(seconds=i * 20)) for i in range(4)]          # 60s at gate
    seq += [obs(NIGHT + timedelta(seconds=80 + i * 20), zone="access-road")   # leaves
            for i in range(2)]
    seq += [obs(NIGHT + timedelta(seconds=120 + i * 20)) for i in range(4)]   # returns, 60s
    assert feed(engine, seq) == [], "dwell accumulated across a zone exit"


def test_loitering_does_not_fire_during_the_day(site):
    engine = RuleEngine(site, [must_rule("loitering")])
    seq = [obs(NOON + timedelta(seconds=i * 30)) for i in range(10)]
    assert feed(engine, seq) == []


def test_time_window_wraps_midnight(site):
    """22:00-05:00 must include 23:30 and 02:00, and exclude 12:00."""
    rule = Rule(
        id="t", name="t", severity=Severity.LOW,
        conditions=[ClassIs(labels=["person"]), TimeBetween(start_hour=22, end_hour=5)],
        cooldown_seconds=0,
    )
    engine = RuleEngine(site, [rule])
    for hour, expect in [(23, True), (2, True), (12, False), (21, False), (5, False)]:
        engine.reset()
        ts = datetime(2026, 8, 6, hour, 30)
        got = bool(feed(engine, [obs(ts, entity=f"E{hour}")]))
        assert got is expect, f"hour {hour} should be {expect}"


def test_sequence_condition_detects_tailgating(site):
    engine = RuleEngine(site, [must_rule("tailgating")])
    t = datetime(2026, 8, 6, 21, 14, 0)
    seq = [
        obs(t, label="truck", zone="main-gate", entity="ENT-TRUCK"),
        obs(t + timedelta(seconds=8), label="car", zone="main-gate", entity="ENT-CAR"),
    ]
    fired = feed(engine, seq)
    assert fired and fired[0].rule.id == "tailgating"


def test_sequence_does_not_fire_outside_the_window(site):
    engine = RuleEngine(site, [must_rule("tailgating")])
    t = datetime(2026, 8, 6, 21, 14, 0)
    seq = [
        obs(t, label="truck", zone="main-gate", entity="ENT-TRUCK"),
        obs(t + timedelta(seconds=90), label="car", zone="main-gate", entity="ENT-CAR"),
    ]
    assert feed(engine, seq) == []


def test_outside_normal_hours_uses_the_zone_schedule(site):
    """The loading dock is open 07:00-19:00, so 03:00 is outside and 12:00 is not."""
    engine = RuleEngine(site, [must_rule("after-hours-vehicle")])
    night = [obs(datetime(2026, 8, 6, 3, 0) + timedelta(seconds=i * 20),
                 label="truck", zone="loading-dock", entity="ENT-T") for i in range(4)]
    assert feed(engine, night)

    engine.reset()
    day = [obs(NOON + timedelta(seconds=i * 20), label="truck",
               zone="loading-dock", entity="ENT-T2") for i in range(4)]
    assert feed(engine, day) == []


def test_unattended_object_requires_the_person_to_be_gone(site):
    engine = RuleEngine(site, [must_rule("unattended-object")])
    t = datetime(2026, 8, 6, 19, 40)
    # Courier present alongside the box â€” not yet unattended.
    seq = [obs(t + timedelta(seconds=i * 30), label="person", zone="main-gate",
               entity="ENT-P") for i in range(3)]
    seq += [obs(t + timedelta(seconds=i * 30), label="cardboard box",
                zone="main-gate", entity="ENT-BOX") for i in range(3)]
    assert feed(engine, seq) == []

    # Person leaves; box persists past the absence threshold.
    later = [obs(t + timedelta(seconds=200 + i * 40), label="cardboard box",
                 zone="main-gate", entity="ENT-BOX") for i in range(6)]
    assert feed(engine, later)


def test_baseline_anomaly_rule_needs_history(site):
    """Without enough history the anomaly rule must abstain, not fire."""
    baseline = BaselineModel("plant-01")
    engine = RuleEngine(site, [must_rule("baseline-anomaly")], baseline=baseline)
    seq = [obs(NIGHT + timedelta(seconds=i * 30), label="truck", zone="loading-dock",
               entity="ENT-X") for i in range(4)]
    assert feed(engine, seq) == []


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Cooldown
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
def test_cooldown_suppresses_repeat_alerts_for_one_entity(site):
    """One person loitering must not produce one alert per frame.

    The sequence spans 570 s against a 300 s cooldown, so two alerts is the
    correct answer â€” the point is that it is nowhere near the 20 observations fed
    in, which is what an ungated rule would produce.
    """
    rule = must_rule("loitering")
    engine = RuleEngine(site, [rule])
    seq = [obs(NIGHT + timedelta(seconds=i * 30)) for i in range(20)]
    fired = feed(engine, seq)

    span = (seq[-1].ts - seq[0].ts).total_seconds()
    ceiling = int(span // rule.cooldown_seconds) + 1
    assert 1 <= len(fired) <= ceiling, f"expected at most {ceiling} alerts, got {len(fired)}"
    assert len(fired) < len(seq) / 4, "cooldown barely suppressed anything"


def test_two_intruders_both_alert(site):
    """Cooldown is per entity â€” one person loitering must not mask another."""
    engine = RuleEngine(site, [must_rule("loitering")])
    seq = []
    for i in range(8):
        seq.append(obs(NIGHT + timedelta(seconds=i * 30), entity="ENT-A"))
        seq.append(obs(NIGHT + timedelta(seconds=i * 30), entity="ENT-B"))
    fired = feed(engine, seq)
    assert {f.observation.entity_id for f in fired} == {"ENT-A", "ENT-B"}


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Explainability
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
def test_a_rule_that_did_not_fire_explains_itself(site):
    engine = RuleEngine(site, [must_rule("loitering")])
    results = engine.evaluate(obs(NIGHT))
    r = results[0]
    assert not r.fired
    assert "dwell" in r.why_not().lower()


def test_alert_carries_its_full_chain_of_evidence(site):
    engine = RuleEngine(site, [must_rule("loitering")])
    fired = feed(engine, [obs(NIGHT + timedelta(seconds=i * 30)) for i in range(8)])
    alert = engine.to_alert(fired[0])
    kinds = {e.kind for e in alert.evidence}
    assert {"detection", "frame", "rule"} <= kinds
    assert alert.confidence > 0
    assert alert.rule_id == "loitering"


def test_high_priority_zone_escalates_severity(site):
    """The same behaviour matters more at a substation than in a car park."""
    engine = RuleEngine(site, [must_rule("loitering")])
    fired = feed(
        engine,
        [obs(NIGHT + timedelta(seconds=i * 30), zone="restricted-core") for i in range(8)],
    )
    alert = engine.to_alert(fired[0])
    assert alert.severity is Severity.CRITICAL


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Triage
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
def _alert(site, engine, zone="main-gate", entity="ENT-1", start=NIGHT):
    fired = feed(engine, [obs(start + timedelta(seconds=i * 30), zone=zone, entity=entity)
                          for i in range(8)])
    return engine.to_alert(fired[0])


def test_triage_suppresses_a_duplicate(site):
    engine = RuleEngine(site, [must_rule("loitering")])
    t = Triage(site)
    a1 = _alert(site, engine)
    assert t.assess(a1).keep

    engine2 = RuleEngine(site, [must_rule("loitering")])
    a2 = _alert(site, engine2, start=NIGHT + timedelta(seconds=60))
    d = t.assess(a2)
    assert not d.keep and "duplicate" in d.reason


def test_triage_suppresses_wildlife_via_counterfactual(site):
    """The stray-dog scenario: every surface signal of an intrusion, no alert."""
    engine = RuleEngine(site, [must_rule("loitering")])
    t = Triage(site)
    a = _alert(site, engine, zone="fence-line", entity="ENT-DOG")
    d = t.assess(a, scene_caption="A stray dog lies beside the perimeter fence.")
    assert not d.keep
    assert d.counterfactual == "animal"


def test_ppe_never_excuses_a_critical_zone_breach(site):
    """A hi-vis vest is not an access credential."""
    engine = RuleEngine(site, [must_rule("loitering")])
    t = Triage(site)
    a = _alert(site, engine, zone="restricted-core", entity="ENT-W")
    assert a.severity is Severity.CRITICAL
    d = t.assess(a, scene_caption="A worker in a high-visibility vest and hard hat.")
    assert d.keep, "PPE suppressed a critical restricted-zone alert"


def test_operator_feedback_lowers_future_confidence(site):
    engine = RuleEngine(site, [must_rule("loitering")])
    t = Triage(site)
    a = _alert(site, engine)
    t.assess(a)
    effect = t.record_feedback(a, is_false_positive=True)
    assert effect["penalty"] > 0
    assert t.stats["learned_penalties"]


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Narrative
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
def test_threat_score_escalates_across_a_sequence(site):
    """An escalating incident must score above the sum of isolated events."""
    engine = RuleEngine(site, [must_rule("loitering")])
    alerts = []
    for i, zone in enumerate(["fence-line", "fence-line", "substation", "restricted-core"]):
        e = RuleEngine(site, [must_rule("loitering")])
        alerts.append(_alert(site, e, zone=zone, entity=f"ENT-{i}",
                             start=NIGHT + timedelta(minutes=i * 3)))
    score, traj = NarrativeBuilder(site).threat_score(alerts)
    assert 0 < score <= 1.0
    assert [v for _, v in traj] == sorted(v for _, v in traj), "score must be monotonic"
    assert traj[-1][1] > traj[0][1]


def test_narrative_groups_related_alerts_and_splits_unrelated(site):
    nb = NarrativeBuilder(site)
    engines = [RuleEngine(site, [must_rule("loitering")]) for _ in range(3)]
    near1 = _alert(site, engines[0], zone="fence-line", entity="ENT-A", start=NIGHT)
    near2 = _alert(site, engines[1], zone="fence-line", entity="ENT-B",
                   start=NIGHT + timedelta(minutes=5))
    # Must be a zone the loitering rule actually covers, or nothing fires and the
    # test measures the wrong thing.
    far = _alert(site, engines[2], zone="substation", entity="ENT-C",
                 start=NIGHT + timedelta(hours=3))
    groups = nb.group([near1, near2, far])
    assert len(groups) == 2
    assert len(groups[0]) == 2


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# DSL
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
def test_every_default_rule_round_trips_through_yaml():
    for r in default_rules():
        assert Rule.from_yaml(r.to_yaml()).model_dump() == r.model_dump()


def test_rules_explain_themselves_in_english():
    for r in default_rules():
        lines = r.explain()
        assert len(lines) == len(r.conditions)
        assert all(isinstance(x, str) and x for x in lines)


def test_default_pack_ids_are_unique():
    ids = [r.id for r in default_rules()]
    assert len(ids) == len(set(ids))


def test_rules_reference_only_real_zones():
    """A rule naming a zone that does not exist validates and then never fires â€”
    the worst failure mode, because it looks like it is working."""
    known = {z.id for z in build_plant_01().zones}
    for r in default_rules():
        for c in r.conditions:
            if c.kind.value == "zone_in":
                assert set(c.zones) <= known, f"{r.id} references unknown zones"

