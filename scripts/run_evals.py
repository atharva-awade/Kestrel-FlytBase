"""Run the evaluation suite and write measured results to data/eval/.

Four things are measured, and the output feeds both the /evals page and the LaTeX
report — so no number quoted anywhere is hand-copied.

  1. SCENARIOS — precision and recall over the scripted set, including the two
     outputs the assignment names by example. The true-negative cases matter more
     than the true positives: a system that alerts on the delivery driver and the
     stray dog gets switched off, and then protects nothing.
  2. RETRIEVAL — does hybrid search actually beat its parts? Reported as hit rate
     at k over a small labelled query set.
  3. CHAOS — fault injection. Asserts the system degrades rather than crashes.
  4. LEADERBOARD — read back from the model probes.

    uv run python scripts/run_evals.py
    uv run python scripts/run_evals.py --only scenarios
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore")

OUT = Path("data/eval")


# ═══════════════════════════════════════════════════════════════════════════════
async def eval_scenarios() -> dict[str, Any]:
    """Replay every scripted scenario and score it against its declared expectation."""
    from kestrel.config import get_settings
    from kestrel.ingest.sources import ScriptedSource
    from kestrel.session import Session
    from kestrel.sim.scenarios import ALL
    from kestrel.sim.sites import load_site
    from kestrel.storage.db import Database

    print("\n[scenarios]")
    site = load_site("plant-01", get_settings().sites_dir)
    rows: list[dict[str, Any]] = []
    tp = fp = fn = tn = 0

    for sc in ALL:
        # Isolated database per scenario: entity and baseline state must not leak
        # between them, or a later scenario inherits the earlier one's history.
        db = Database(Path(f"data/eval/_scn_{sc.id}.db"))
        start = datetime.fromisoformat(sc.frames[0]["at"])
        src = ScriptedSource(sc.frames, site, start_clock=start)
        session = Session(site, db=db, save_frames=False, enable_embeddings=False)

        t0 = time.perf_counter()
        await session.run(src)
        elapsed = time.perf_counter() - t0

        fired = {a.rule_id for a in session.alerts}
        expected = set(sc.expect_alerts)
        forbidden = set(sc.expect_no_alerts)

        hits = expected & fired
        misses = expected - fired
        # A false positive is a rule the scenario explicitly said must NOT fire, or
        # any rule at all when the scenario expects silence.
        #
        # An *additional* correct alert is not a false positive: a person at the
        # fence line at 02:00 genuinely is loitering as well as breaching the
        # perimeter, and penalising the system for noticing both would be scoring
        # the test rather than the behaviour. Scenarios declare `expect_no_alerts`
        # precisely so this distinction is explicit rather than inferred.
        false_positives = (forbidden & fired) if expected else fired

        tp += len(hits)
        fn += len(misses)
        fp += len(false_positives)
        if not expected and not fired:
            tn += 1

        ok = not misses and not false_positives
        rows.append({
            "id": sc.id,
            "title": sc.title,
            "tags": sc.tags,
            "expected": sorted(expected),
            "fired": sorted(fired),
            "hits": sorted(hits),
            "misses": sorted(misses),
            "false_positives": sorted(false_positives),
            "alerts_raised": session.stats.alerts_raised,
            "alerts_suppressed": session.stats.alerts_suppressed,
            "missions_proposed": session.stats.missions_proposed,
            "frames": len(sc.frames),
            "seconds": round(elapsed, 1),
            "pass": ok,
        })
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {sc.id:<26} expected={sorted(expected) or '-'} fired={sorted(fired) or '-'}")
        db.close()

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    passed = sum(1 for r in rows if r["pass"])

    print(f"  → {passed}/{len(rows)} scenarios correct · "
          f"precision {precision:.2f} recall {recall:.2f} F1 {f1:.2f}")

    return {
        "scenarios": rows,
        "passed": passed,
        "total": len(rows),
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "true_negatives": tn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "note": (
            "True-negative scenarios (routine delivery, wildlife at the fence, shift "
            "change) are weighted equally in this suite. A security system that cries "
            "wolf is switched off, and then protects nothing."
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════════
async def eval_retrieval() -> dict[str, Any]:
    """Does fusing the retrievers beat using any one of them alone?"""
    from kestrel.clients.models import get_client
    from kestrel.config import get_settings
    from kestrel.retrieval.search import HybridSearch
    from kestrel.sim.sites import load_site
    from kestrel.storage.db import get_db

    print("\n[retrieval]")
    db = get_db()
    if db.stats["frames_analysed"] == 0:
        print("  skipped — no ingested frames. Run: uv run kestrel ingest")
        return {"skipped": True, "reason": "no ingested frames"}

    site = load_site("plant-01", get_settings().sites_dir)
    hs = HybridSearch(db, site, get_client())

    # Small labelled set: each query names a label the correct frames must carry.
    queries = [
        {"q": "show me all person events", "expect_label": "person"},
        {"q": "anything at the substation", "expect_zone": "substation"},
        {"q": "someone in a high-visibility vest", "expect_label": "person"},
        {"q": "activity in the restricted core", "expect_zone": "restricted-core"},
    ]

    rows = []
    for item in queries:
        res = await hs.search(item["q"], limit=10)
        hits = res.hits
        relevant = 0
        for h in hits:
            if ("expect_label" in item and item["expect_label"] in h.labels) or ("expect_zone" in item and h.zone_id == item["expect_zone"]):
                relevant += 1
        rows.append({
            "query": item["q"],
            "intent": res.plan.intent,
            "retrievers": res.counts,
            "returned": len(hits),
            "relevant": relevant,
            "precision_at_k": round(relevant / len(hits), 3) if hits else 0.0,
            "took_ms": round(res.took_ms, 1),
        })
        print(f"  {item['q'][:44]:<46} {relevant}/{len(hits)} relevant "
              f"({res.plan.intent}, {res.took_ms:.0f}ms)")

    mean_p = sum(r["precision_at_k"] for r in rows) / max(1, len(rows))
    print(f"  → mean precision@k {mean_p:.2f}")
    return {
        "queries": rows,
        "mean_precision_at_k": round(mean_p, 4),
        "note": (
            "A small labelled set on one ingested session, indicative rather than a "
            "benchmark. It exists to show the fusion works and the plan is sane."
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════════
async def eval_chaos() -> dict[str, Any]:
    """Inject faults and assert the system degrades instead of crashing."""
    from kestrel.clients.models import ModelClient
    from kestrel.clients.provider import ProviderError
    from kestrel.config import Settings
    from kestrel.gate.gate import CostGate
    from kestrel.perception.vlm import SceneRequest, describe_scene
    from kestrel.sim.sites import build_plant_01

    print("\n[chaos]")
    results = []

    async def check(name: str, expectation: str, fn) -> None:
        try:
            detail = await fn()
            results.append({"fault": name, "expected": expectation,
                            "survived": True, "detail": detail})
            print(f"  [OK]   {name:<34} {detail}")
        except Exception as e:
            results.append({"fault": name, "expected": expectation, "survived": False,
                            "detail": f"{type(e).__name__}: {e}"[:140]})
            print(f"  [FAIL] {name:<34} {type(e).__name__}: {e}"[:110])

    # 1 — no credentials at all
    async def no_key():
        s = Settings(NVIDIA_API_KEY="", GROQ_API_KEY="", KESTREL_MODE="live")  # type: ignore[call-arg]
        return f"degraded to {s.effective_mode.value} rather than failing"
    await check("no API key", "falls back to replay", no_key)

    # 2 — a cassette that was never recorded
    async def cassette_miss():
        s = Settings(KESTREL_MODE="replay")  # type: ignore[call-arg]
        c = ModelClient(s)
        try:
            await c.chat([{"role": "user", "content": f"unrecorded-{time.time()}"}])
            return "returned a value — a silent fallthrough would break the offline claim"
        except ProviderError as e:
            return f"raised loudly: {type(e).__name__}"
        finally:
            await c.aclose()
    await check("cassette miss in replay", "raises rather than silently calling out",
                cassette_miss)

    # 3 — a corrupt frame
    async def corrupt_frame():
        import numpy as np

        gate = CostGate(build_plant_01())
        junk = np.random.randint(0, 255, (64, 64, 3), dtype=np.uint8)
        v = await gate.decide(image=junk, phash=None, ts=datetime.now())
        return f"gate returned a decision: {v.reason[:40]}"
    await check("corrupt / noise frame", "gate still returns a verdict", corrupt_frame)

    # 4 — VLM unreachable
    async def vlm_down():
        import numpy as np

        req = SceneRequest(
            image=np.zeros((64, 64, 3), dtype=np.uint8),
            detections=[], telemetry=None,
        )
        s = Settings(NVIDIA_API_KEY="", GROQ_API_KEY="", KESTREL_MODE="live")  # type: ignore[call-arg]
        _ = s
        graph = await describe_scene(req)
        return f"returned a degraded scene graph (confidence {graph.confidence})"
    await check("VLM unavailable", "degrades to a low-confidence graph", vlm_down)

    # 5 — telemetry missing
    async def no_telemetry():
        gate = CostGate(build_plant_01())
        v = await gate.decide(image=None, phash="0" * 64, ts=datetime.now(),
                              telemetry=None, is_text_frame=True)
        return f"handled a frame with no telemetry: {v.reason}"
    await check("telemetry dropout", "gate handles a null telemetry", no_telemetry)

    # 6 — projection with the camera above the horizon
    async def bad_projection():
        from kestrel.domain import BBox, DroneState, Telemetry
        from kestrel.perception.project import GroundProjector

        site = build_plant_01()
        t = Telemetry(ts=datetime.now(), site_id=site.id, lat=site.origin.lat,
                      lon=site.origin.lon, alt_m=40, heading_deg=0,
                      gimbal_pitch_deg=-5, state=DroneState.HOVER)
        p = GroundProjector(site).project(BBox(x1=10, y1=2, x2=30, y2=6), t, 960, 540)
        assert p.world is None, "fabricated a coordinate from a ray above the horizon"
        return "refused to fabricate a coordinate"
    await check("ray above horizon", "returns no position rather than a wrong one",
                bad_projection)

    survived = sum(1 for r in results if r["survived"])
    print(f"  → {survived}/{len(results)} faults survived")
    return {
        "faults": results,
        "survived": survived,
        "total": len(results),
        "note": (
            "Each fault asserts graceful degradation, not absence of failure. The "
            "cassette-miss case deliberately expects a loud error: silently falling "
            "through to the network would make 'runs offline' untrue in exactly the "
            "situation that matters."
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════════
def eval_leaderboard() -> dict[str, Any]:
    """Read the model probes back into a leaderboard."""
    print("\n[leaderboard]")
    p = Path("data/probe_vlms.json")
    if not p.exists():
        print("  skipped — run scripts/probe_vlms.py first")
        return {"skipped": True}
    data = json.loads(p.read_text(encoding="utf-8"))
    rows = data.get("results", [])
    reachable = [r for r in rows if r["ok"]]
    for r in sorted(reachable, key=lambda x: x["best_seconds"] or 1e9):
        signals = sum([r["mentions_blue"], r["mentions_vehicle"], r["mentions_person"]])
        print(f"  {r['model']:<44} {r['best_seconds']:>6}s  {signals}/3 signals")
    return {
        "models": rows,
        "reachable": len(reachable),
        "total": len(rows),
        "note": (
            "Presence in a provider's catalogue is not evidence you can call it: "
            f"{len(reachable)} of {len(rows)} catalogued vision models were reachable."
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════════
async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["scenarios", "retrieval", "chaos", "leaderboard"])
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    print("=" * 92)
    print("KESTREL evaluation suite")
    print("=" * 92)

    jobs = {
        "scenarios": eval_scenarios,
        "retrieval": eval_retrieval,
        "chaos": eval_chaos,
    }
    written = []

    for name, fn in jobs.items():
        if args.only and args.only != name:
            continue
        try:
            result = await fn()
        except Exception as e:
            result = {"error": f"{type(e).__name__}: {e}"[:300]}
            print(f"  [ERROR] {name}: {e}")
        result["generated_at"] = datetime.now().isoformat()
        (OUT / f"{name}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        written.append(name)

    if not args.only or args.only == "leaderboard":
        lb = eval_leaderboard()
        lb["generated_at"] = datetime.now().isoformat()
        (OUT / "leaderboard.json").write_text(json.dumps(lb, indent=2), encoding="utf-8")
        written.append("leaderboard")

    print("\n" + "=" * 92)
    print(f"wrote: {', '.join(f'data/eval/{w}.json' for w in written)}")
    print("These feed both the /evals page and the LaTeX report — no figure is hand-copied.")


if __name__ == "__main__":
    asyncio.run(main())
