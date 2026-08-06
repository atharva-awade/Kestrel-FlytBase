"""Storage, ledger, entity resolution and the baseline model.

These carry the claims that are easiest to assert and hardest to notice breaking:
"the same vehicle, seventh visit", "first time ever at this hour", "this log has
not been altered". Each is tested against a constructed history with a known
answer.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest

from kestrel.domain import (
    BBox,
    Detection,
    Frame,
    FrameSourceKind,
    GateVerdict,
    SceneGraph,
    SceneObject,
)
from kestrel.memory.baseline import MIN_DAYS, BaselineModel, combine_confidence
from kestrel.memory.entities import EntityResolver, attributes_from_scene
from kestrel.storage.db import Database
from kestrel.storage.ledger import Ledger, LedgerKind

T0 = datetime(2026, 8, 6, 12, 0, 0)


@pytest.fixture
def db(tmp_path):
    """A throwaway database, closed on teardown.

    The close matters on Windows: `Database` holds a thread-local connection, and
    leaking one per test eventually exhausts handles or blocks pytest from
    removing the temporary directory — which surfaces as an intermittent
    `sqlite3.OperationalError` during *setup* of an unrelated test.
    """
    database = Database(tmp_path / "test.db")
    try:
        yield database
    finally:
        database.close()


def make_frame(seq: int, ts: datetime) -> Frame:
    return Frame(
        id=f"frm_test_{seq:06d}", site_id="plant-01", seq=seq, ts=ts,
        source=FrameSourceKind.VIDEO, width=960, height=540, phash="0" * 64,
    )


def make_det(frame_id: str, label: str, track_id: int | None = None, zone: str | None = None):
    return Detection(
        id=f"det_{frame_id}_{label}_{track_id}", frame_id=frame_id, label=label,
        confidence=0.9, bbox=BBox(x1=10, y1=10, x2=60, y2=110),
        track_id=track_id, zone_id=zone,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Storage
# ═══════════════════════════════════════════════════════════════════════════════
def test_database_initialises_with_a_vector_index_of_some_kind(db):
    """Either sqlite-vec loaded or the numpy fallback engaged. Never neither —
    'clone and run' must not depend on a native extension compiling."""
    assert db.stats["vector_index"] in ("sqlite-vec", "numpy-fallback")


def test_skipped_frames_are_persisted_too(db):
    """The gate's skip rate is the scalability claim, so it has to be auditable
    after the fact rather than only counted in memory."""
    db.add_frame(make_frame(0, T0), GateVerdict(analyse=True, reason="first-frame"))
    db.add_frame(make_frame(1, T0), GateVerdict(analyse=False, reason="static"))
    db.add_frame(make_frame(2, T0), GateVerdict(analyse=False, reason="static"))
    db.commit()
    s = db.stats
    assert s["frames_total"] == 3
    assert s["frames_analysed"] == 1
    assert s["frames_skipped"] == 2


def test_vector_search_returns_nearest_first(db):
    rng = np.random.default_rng(0)
    target = rng.normal(size=2048).astype(np.float32)
    for i in range(12):
        vec = target + rng.normal(scale=0.05 + i * 0.35, size=2048).astype(np.float32)
        db.add_embedding(f"emb{i}", "plant-01", "frame", f"frm{i}", T0, vec.tolist())
    db.commit()

    hits = db.vector_search(target.tolist(), kind="frame", site_id="plant-01", k=5)
    assert hits, "vector search returned nothing"
    assert hits[0][0] == "frm0", "closest vector was not ranked first"
    sims = [s for _, s in hits]
    assert sims == sorted(sims, reverse=True), "results are not ordered by similarity"


def test_vector_search_ignores_dimension_mismatch(db):
    db.add_embedding("e1", "plant-01", "frame", "f1", T0, [0.1] * 2048)
    db.commit()
    assert db.vector_search([0.1] * 16, kind="frame") == []


# ═══════════════════════════════════════════════════════════════════════════════
# Ledger
# ═══════════════════════════════════════════════════════════════════════════════
def test_empty_ledger_verifies(db):
    assert Ledger(db).verify()["valid"] is True


def test_ledger_chain_verifies_after_appends(db):
    led = Ledger(db)
    for i in range(6):
        led.append(LedgerKind.ALERT_RAISED, {"n": i}, site_id="plant-01", ref_id=f"a{i}")
    v = led.verify()
    assert v["valid"] is True
    assert v["entries"] == 6


def test_ledger_detects_a_modified_entry(db):
    """The whole point: an edit made after the fact must be detectable."""
    led = Ledger(db)
    for i in range(5):
        led.append(LedgerKind.MISSION_APPROVED, {"n": i}, ref_id=f"m{i}")
    assert led.verify()["valid"] is True

    # Someone rewrites history directly in the database.
    db.conn.execute(
        "UPDATE ledger SET payload_json = ? WHERE seq = 3", ('{"n":999}',)
    )
    db.commit()

    v = led.verify()
    assert v["valid"] is False
    assert v["broken_at_seq"] == 3
    assert "modified" in v["reason"]


def test_ledger_history_for_one_reference(db):
    led = Ledger(db)
    led.append(LedgerKind.ALERT_RAISED, {"x": 1}, ref_id="alert-7")
    led.append(LedgerKind.ALERT_STATUS, {"to": "investigating"}, ref_id="alert-7")
    led.append(LedgerKind.ALERT_RAISED, {"x": 2}, ref_id="alert-8")
    assert len(led.for_ref("alert-7")) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# Entity resolution
# ═══════════════════════════════════════════════════════════════════════════════
def test_same_track_id_always_resolves_to_the_same_entity():
    r = EntityResolver("plant-01")
    rng = np.random.default_rng(1)
    v = rng.normal(size=64)
    a = r.resolve(make_det("f1", "truck", track_id=7), T0, embedding=v.tolist())
    b = r.resolve(make_det("f2", "truck", track_id=7), T0 + timedelta(seconds=1),
                  embedding=(v + rng.normal(scale=2.0, size=64)).tolist())
    assert a.id == b.id, "tracker identity must be trusted within an observation"


def test_similar_appearance_reidentifies_across_a_gap():
    """The 'same vehicle returned' claim."""
    r = EntityResolver("plant-01")
    rng = np.random.default_rng(2)
    base = rng.normal(size=128)
    attrs = {"colour": "blue", "kind": "pickup truck"}

    first = r.resolve(make_det("f1", "truck", track_id=1), T0,
                      embedding=base.tolist(), attributes=attrs)
    later = r.resolve(
        make_det("f2", "truck", track_id=2), T0 + timedelta(hours=3),
        embedding=(base + rng.normal(scale=0.05, size=128)).tolist(), attributes=attrs,
    )
    assert later.id == first.id
    assert later.visit_count == 2, "a 3-hour gap should count as a second visit"


def test_dissimilar_appearance_creates_a_distinct_entity():
    """Guards the failure that matters most: a wrong merge fabricates history."""
    r = EntityResolver("plant-01")
    rng = np.random.default_rng(3)
    a = r.resolve(make_det("f1", "truck", track_id=1), T0,
                  embedding=rng.normal(size=128).tolist(),
                  attributes={"colour": "blue"})
    b = r.resolve(make_det("f2", "truck", track_id=2), T0 + timedelta(hours=2),
                  embedding=rng.normal(size=128).tolist(),
                  attributes={"colour": "red"})
    assert a.id != b.id


def test_people_and_vehicles_never_merge():
    r = EntityResolver("plant-01")
    v = np.ones(64)
    a = r.resolve(make_det("f1", "person", track_id=1), T0, embedding=v.tolist())
    b = r.resolve(make_det("f2", "truck", track_id=2), T0, embedding=v.tolist())
    assert a.id != b.id
    assert a.kind is not b.kind


def test_visits_accumulate_across_days():
    """The seven-visits-over-four-days claim, constructed and asserted."""
    r = EntityResolver("plant-01")
    rng = np.random.default_rng(4)
    base = rng.normal(size=128)
    attrs = {"colour": "blue", "kind": "pickup truck"}
    ent = None
    for day in range(4):
        for hour in (9, 16):
            ts = T0 + timedelta(days=day, hours=hour)
            ent = r.resolve(
                make_det(f"f{day}{hour}", "truck", track_id=100 + day * 2 + (hour > 12)),
                ts,
                embedding=(base + rng.normal(scale=0.04, size=128)).tolist(),
                attributes=attrs,
            )
    assert ent is not None
    assert r.stats["entities"] == 1, "the same vehicle fragmented into several entities"
    assert ent.visit_count == 8
    assert ent.descriptor == "blue pickup truck"


def test_attributes_are_lifted_from_the_scene_graph():
    scene = SceneGraph(
        caption="A blue pickup reverses into the dock.",
        objects=[SceneObject(label="truck", colour="Blue", kind="Pickup Truck",
                             activity="reversing")],
    )
    attrs = attributes_from_scene(scene, "truck")
    assert attrs == {"colour": "blue", "kind": "pickup truck", "activity": "reversing"}


# ═══════════════════════════════════════════════════════════════════════════════
# Baseline
# ═══════════════════════════════════════════════════════════════════════════════
def test_baseline_abstains_before_it_has_evidence():
    """Day one must not flood the operator with false novelty."""
    b = BaselineModel("plant-01")
    b.observe("loading-dock", T0, "truck")
    d = b.evaluate("loading-dock", T0, "truck")
    assert d.confident is False
    assert d.anomalous is False
    assert "too little to judge" in d.explanation


def test_baseline_flags_a_genuinely_unprecedented_observation():
    """'First vehicle at the dock at 03:00 in N days' — the headline claim."""
    b = BaselineModel("plant-01")
    for day in range(6):
        for _ in range(4):
            b.observe("loading-dock", T0.replace(hour=11) + timedelta(days=day), "truck")

    night = T0.replace(hour=3) + timedelta(days=7)
    b.observe("loading-dock", night, "truck")
    d = b.evaluate("loading-dock", night, "truck")

    assert d.confident and d.first_ever and d.anomalous
    assert "first time" in d.explanation and "03:00" in d.explanation


def test_baseline_treats_routine_activity_as_routine():
    b = BaselineModel("plant-01")
    for day in range(6):
        for _ in range(4):
            b.observe("loading-dock", T0.replace(hour=11) + timedelta(days=day), "truck")
    today = T0.replace(hour=11) + timedelta(days=6)
    for _ in range(4):
        b.observe("loading-dock", today, "truck")
    d = b.evaluate("loading-dock", today, "truck")
    assert d.confident and not d.anomalous


def test_baseline_needs_min_days():
    b = BaselineModel("plant-01")
    for day in range(MIN_DAYS - 1):
        b.observe("yard", T0 + timedelta(days=day), "person")
    assert b.evaluate("yard", T0 + timedelta(days=MIN_DAYS - 1), "person").confident is False


def test_confidence_is_dragged_down_by_its_weakest_input():
    """A rule firing perfectly on a barely-visible detection must not be confident."""
    b = BaselineModel("plant-01")
    neutral = b.evaluate(None, T0, "person")
    strong = combine_confidence(0.95, 0.95, neutral)
    weak_perception = combine_confidence(0.15, 0.95, neutral)
    weak_rule = combine_confidence(0.95, 0.15, neutral)
    assert strong > 0.8
    assert weak_perception < 0.3
    assert weak_rule < 0.3


def test_anomaly_raises_confidence_only_when_the_baseline_is_trusted():
    b = BaselineModel("plant-01")
    for day in range(6):
        b.observe("substation", T0.replace(hour=10) + timedelta(days=day), "person")
    night = T0.replace(hour=2) + timedelta(days=7)
    b.observe("substation", night, "person")
    dev = b.evaluate("substation", night, "person")

    with_anomaly = combine_confidence(0.7, 0.7, dev)
    without = combine_confidence(0.7, 0.7, BaselineModel("x").evaluate("z", T0, "person"))
    assert with_anomaly > without


# ═══════════════════════════════════════════════════════════════════════════════
# Retrieval degradation
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_unavailable_retriever_is_reported_not_hidden(db):
    """An empty result and a broken retriever are different claims.

    When the embedding provider is down, the semantic and visual retrievers return
    nothing. Reporting that as "no frames matched" tells the operator the yard was
    empty, which is a statement about the world rather than about the system. This
    is the failure mode that quietly destroys trust in an analyst, so it is
    asserted rather than assumed.
    """
    from kestrel.retrieval.search import HybridSearch
    from kestrel.sim.sites import build_plant_01

    class DeadEmbeddings:
        async def chat(self, *a, **kw):
            return '{"intent":"visual","semantic_text":"a white pickup"}'

        async def embed_text(self, *a, **kw):
            raise ConnectionError("provider unreachable")

        async def embed_image(self, *a, **kw):
            raise ConnectionError("provider unreachable")

    search = HybridSearch(db, build_plant_01(), DeadEmbeddings())
    result = await search.search("a white pickup")
    payload = result.to_dict()

    assert payload["hits"] == []
    assert payload["complete"] is False, "an incomplete search claimed to be complete"
    assert payload["degraded"], "a dead retriever was silently swallowed"
    assert any("unavailable" in why for why in payload["degraded"].values())


@pytest.mark.asyncio
async def test_healthy_search_reports_itself_complete(db):
    """The flag has to mean something in both directions.

    Otherwise "incomplete" becomes background noise the operator learns to ignore,
    which is the same end state as not reporting it at all.
    """
    from kestrel.retrieval.search import HybridSearch
    from kestrel.sim.sites import build_plant_01

    class LiveEmbeddings:
        async def chat(self, *a, **kw):
            return '{"intent":"visual","semantic_text":"person at the gate"}'

        async def embed_text(self, *a, **kw):
            return [0.1] * 2048

        async def embed_image(self, *a, **kw):
            return [0.1] * 2048

    search = HybridSearch(db, build_plant_01(), LiveEmbeddings())
    payload = (await search.search("person at the gate")).to_dict()
    assert payload["degraded"] == {}
    assert payload["complete"] is True


@pytest.mark.asyncio
async def test_no_model_client_is_itself_a_declared_degradation(db):
    """Running with no client at all is a real limitation, not a silent default."""
    from kestrel.retrieval.search import HybridSearch
    from kestrel.sim.sites import build_plant_01

    payload = (await HybridSearch(db, build_plant_01(), None).search("a white pickup")).to_dict()
    assert payload["complete"] is False
    assert "no model client configured" in " ".join(payload["degraded"].values())
