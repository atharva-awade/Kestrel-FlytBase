"""Measure gate efficiency honestly, across contexts.

The gate's skip rate is KESTREL's headline scalability number, so quoting a single
figure would be misleading: the gate is *context-adaptive by design*. Hovering over
a restricted zone at 02:00 it analyses almost everything, because that is when
missing something is expensive. Over a car park at midday it skips almost
everything, because nothing there is worth a model call.

Both behaviours are correct, and the report quotes the range with the conditions
attached rather than a flattering single number.

    uv run python scripts/bench_gate.py
"""

from __future__ import annotations

import asyncio
import json
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")

from kestrel.gate.gate import CostGate
from kestrel.ingest.sources import VideoFileSource
from kestrel.sim.sites import load_site
from kestrel.sim.telemetry import PatrolSimulator

# (label, clip, zone the drone hovers over, site-clock start)
CONTEXTS = [
    ("restricted zone, 02:00", "worker-zone", "restricted-core", "2026-08-06T02:10:00"),
    ("substation, 02:00", "worker-zone", "substation", "2026-08-06T02:10:00"),
    ("yard, 14:00", "worker-zone", "yard", "2026-08-06T14:10:00"),
    ("staff parking, 14:00", "worker-zone", "parking", "2026-08-06T14:10:00"),
    ("parking, quiet clip, 14:00", "store-aisle", "parking", "2026-08-06T14:10:00"),
    ("yard, quiet clip, 11:00", "car-detection", "yard", "2026-08-06T11:00:00"),
]

FRAMES = 60
FPS = 2.0


async def run_one(clip: str, zone_id: str, start_iso: str) -> dict:
    site = load_site("plant-01", Path("data/sites"))
    start = datetime.fromisoformat(start_iso)
    zone = site.zone_by_id(zone_id)

    tel = PatrolSimulator(site, start)
    tel.waypoints = [(zone.centroid, zone_id, 3600.0)]

    src = VideoFileSource(
        Path(f"data/footage/{clip}.mp4"),
        site,
        start_clock=start,
        sample_fps=FPS,
        clock_scale=1.0,
        max_frames=FRAMES,
        telemetry=tel,
    )
    # Embeddings disabled: this measures the free CPU tiers in isolation, so the
    # numbers are not confounded by network latency or a cloud call.
    gate = CostGate(site, embed_fn=None)

    reasons: dict[str, int] = {}
    for raw in src:
        v = await gate.decide(
            image=raw.image,
            phash=raw.frame.phash,
            ts=raw.frame.ts,
            telemetry=raw.frame.telemetry,
        )
        key = v.reason.split("(")[0].split(" ")[0]
        reasons[key] = reasons.get(key, 0) + 1

    s = gate.summary()
    return {
        "clip": clip,
        "zone": zone_id,
        "start": start_iso,
        "seen": s["seen"],
        "analysed": s["analysed"],
        "skipped": s["skipped"],
        "efficiency": s["efficiency"],
        "reasons": reasons,
    }


async def run_idle() -> dict:
    """The other end of the range: a patrol hovering over a quiet scene.

    Every clip we have is a computer-vision demo reel authored to contain constant
    motion, which is close to adversarial for a gate. Real patrol footage is
    overwhelmingly uneventful — a drone stares at an empty yard for minutes at a
    time. This context measures that case using a deterministic static render, and
    it is labelled as constructed wherever it is quoted. Both numbers are reported;
    neither is presented as the headline on its own.
    """
    from kestrel.ingest.sources import SyntheticSource

    site = load_site("plant-01", Path("data/sites"))
    start = datetime.fromisoformat("2026-08-06T14:10:00")
    zone = site.zone_by_id("yard")
    tel = PatrolSimulator(site, start)
    tel.waypoints = [(zone.centroid, "yard", 3600.0)]

    src = SyntheticSource(
        site, start_clock=start, n=FRAMES, seconds_per_frame=1 / FPS,
        moving=False, telemetry=tel,
    )
    gate = CostGate(site, embed_fn=None)
    reasons: dict[str, int] = {}
    for raw in src:
        v = await gate.decide(
            image=raw.image, phash=raw.frame.phash, ts=raw.frame.ts,
            telemetry=raw.frame.telemetry,
        )
        key = v.reason.split("(")[0].split(" ")[0]
        reasons[key] = reasons.get(key, 0) + 1
    s = gate.summary()
    return {
        "clip": "synthetic-idle", "zone": "yard", "start": start.isoformat(),
        "seen": s["seen"], "analysed": s["analysed"], "skipped": s["skipped"],
        "efficiency": s["efficiency"], "reasons": reasons, "constructed": True,
    }


async def main() -> None:
    rows = []
    print(f"\nGate efficiency by context  ({FRAMES} frames @ {FPS} fps each)")
    print("=" * 104)
    print(f"{'context':<30}{'clip':<18}{'analysed':>10}{'skipped':>9}{'skip rate':>11}  top reason")
    print("-" * 104)

    for label, clip, zone, start in CONTEXTS:
        try:
            r = await run_one(clip, zone, start)
        except FileNotFoundError:
            print(f"{label:<30}{clip:<18}  (clip not fetched — skipping)")
            continue
        r["label"] = label
        rows.append(r)
        top = max(r["reasons"].items(), key=lambda x: -x[1])[0] if r["reasons"] else "-"
        print(
            f"{label:<30}{clip:<18}{r['analysed']:>10}{r['skipped']:>9}"
            f"{r['efficiency']*100:>10.1f}%  {top}"
        )

    idle = await run_idle()
    idle["label"] = "idle patrol, quiet yard (constructed)"
    print(
        f"{idle['label']:<30}{idle['clip']:<18}{idle['analysed']:>10}{idle['skipped']:>9}"
        f"{idle['efficiency']*100:>10.1f}%  {max(idle['reasons'], key=idle['reasons'].get)}"
    )

    if not rows:
        print("\nno clips available — run scripts/fetch_footage.py first")
        return

    best = max(rows, key=lambda r: r["efficiency"])
    worst = min(rows, key=lambda r: r["efficiency"])
    total_seen = sum(r["seen"] for r in rows)
    total_analysed = sum(r["analysed"] for r in rows)
    overall = 1 - total_analysed / total_seen

    print("=" * 104)
    print(f"\n  measured on real footage : {overall*100:.1f}% of frames never reached a model")
    print(f"    most gating  : {best['efficiency']*100:.1f}%  ({best['label']})")
    print(f"    least gating : {worst['efficiency']*100:.1f}%  ({worst['label']})")
    print(f"  idle patrol (constructed): {idle['efficiency']*100:.1f}%")
    print(
        "\n  Read these together, not separately. Every clip available is a\n"
        "  computer-vision demo reel authored to contain continuous motion, which is\n"
        "  close to worst-case for a gate — so the real-footage figure is a FLOOR,\n"
        "  not a typical value. A patrol drone over an actual site is idle most of\n"
        "  the shift, which is what the constructed idle context measures.\n"
        "  We report both and claim neither as the headline alone."
    )

    out = Path("data/eval")
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "frames_per_context": FRAMES,
        "sample_fps": FPS,
        "contexts": rows,
        "idle_constructed": idle,
        "overall_efficiency_real_footage": round(overall, 4),
        "idle_efficiency_constructed": round(idle["efficiency"], 4),
        "best": {"label": best["label"], "efficiency": best["efficiency"]},
        "worst": {"label": worst["label"], "efficiency": worst["efficiency"]},
        "caveat": (
            "Real-footage figures are a floor, not a typical value: every available "
            "clip is a CV demo reel authored for continuous motion, which is close to "
            "worst-case for a gate. The idle context is a constructed static scene "
            "representing patrol idle time and is labelled as such."
        ),
    }
    (out / "gate_efficiency.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\n  → wrote {out/'gate_efficiency.json'}")


if __name__ == "__main__":
    asyncio.run(main())
