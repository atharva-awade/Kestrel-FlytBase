"""Run the full perception cascade over real footage and report what it produced.

This is the end-to-end check that the pieces compose: gate → detect → track →
project → embed → scene graph → escalation, on actual video, with real models.
"""

from __future__ import annotations

import argparse
import asyncio
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")

from kestrel.ingest.sources import VideoFileSource
from kestrel.obs.meter import METER
from kestrel.perception.pipeline import PerceptionPipeline
from kestrel.sim.sites import load_site
from kestrel.sim.telemetry import PatrolSimulator


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clip", default="worker-zone")
    ap.add_argument("--frames", type=int, default=24)
    ap.add_argument("--fps", type=float, default=1.0)
    ap.add_argument("--start", default="2026-08-06T02:10:00", help="site clock start")
    ap.add_argument("--no-vlm", action="store_true")
    args = ap.parse_args()

    site = load_site("plant-01", Path("data/sites"))
    start = datetime.fromisoformat(args.start)

    # Park the drone over the substation so projected detections land in a
    # high-priority zone — this exercises the escalation trigger.
    sub = site.zone_by_id("substation")
    tel = PatrolSimulator(site, start)
    tel.waypoints = [(sub.centroid, "substation", 600.0)]

    src = VideoFileSource(
        Path(f"data/footage/{args.clip}.mp4"),
        site,
        start_clock=start,
        sample_fps=args.fps,
        clock_scale=30.0,  # compress the clip onto a night-shift timeline
        max_frames=args.frames,
        telemetry=tel,
    )

    pipe = PerceptionPipeline(site, enable_vlm=not args.no_vlm)
    print(f"\nclip      : {args.clip}.mp4  ({src.native_fps:.0f} fps native, {src.duration_s:.0f}s)")
    print(f"detector  : {pipe.detector.info['backend']} on {pipe.detector.info['device']}")
    print(f"tracker   : {pipe.tracker.info['backend']}")
    print(f"site clock: {start:%Y-%m-%d %H:%M} (clip time x30)")
    print("=" * 118)
    print(f"{'#':>3} {'clock':<9} {'gate':<30} {'det':>4} {'zone':<16} caption")
    print("-" * 118)

    results = []
    for raw in src:
        r = await pipe.process(raw)
        results.append(r)
        zone = next((d.zone_id for d in r.detections if d.zone_id), "") or ""
        cap = (r.scene.caption if r.scene else r.summary)[:52]
        mark = "*" if r.escalated else " "
        print(
            f"{raw.frame.seq:>3} {raw.frame.ts:%H:%M:%S} "
            f"{r.gate.reason[:29]:<30} {len(r.detections):>4} {zone:<16} {mark}{cap}"
        )

    if pipe.escalator is not None and pipe.escalator.stats["in_flight"]:
        print(f"\nwaiting for {pipe.escalator.stats['in_flight']} deep escalation(s)…")
        await pipe.escalator.drain(timeout=300)

    print("=" * 118)
    st = pipe.stats
    print(f"\nGATE       {st['frames_analysed']}/{st['frames_seen']} analysed "
          f"→ {st['gate_efficiency']*100:.1f}% of frames never reached a model")
    for reason, n in sorted(st["gate"]["by_reason"].items(), key=lambda x: -x[1]):
        print(f"           {reason:<28} {n}")

    analysed = [r for r in results if r.analysed]
    if analysed:
        print(f"\nSTAGE LATENCY (mean ms over {len(analysed)} analysed frames)")
        keys = ("gate", "detect_track", "project", "embed", "perceive")
        for k in keys:
            vals = [r.stage_ms[k] for r in analysed if k in r.stage_ms]
            if vals:
                print(f"           {k:<16} {sum(vals)/len(vals):>8.1f} ms")

    dets = [d for r in results for d in r.detections]
    labels: dict[str, int] = {}
    tracks: set[int] = set()
    for d in dets:
        labels[d.label] = labels.get(d.label, 0) + 1
        if d.track_id is not None:
            tracks.add(d.track_id)
    print(f"\nDETECTION  {len(dets)} detections, {len(tracks)} distinct tracks")
    print(f"           {sorted(labels.items(), key=lambda x: -x[1])}")
    geo = [d for d in dets if d.world is not None]
    print(f"           {len(geo)}/{len(dets)} geo-projected")
    zones: dict[str, int] = {}
    for d in dets:
        if d.zone_id:
            zones[d.zone_id] = zones.get(d.zone_id, 0) + 1
    print(f"           zones: {zones or 'none resolved'}")

    scenes = [r.scene for r in results if r.scene]
    if scenes:
        ok = [s for s in scenes if s.objects]
        print(f"\nSCENE      {len(scenes)} graphs, {len(ok)} with structured objects")
        print(f"           mean confidence {sum(s.confidence for s in scenes)/len(scenes):.2f}")
        anom = [a for s in scenes for a in s.anomalies]
        print(f"           anomalies flagged: {anom[:4] or 'none'}")
        print("\n           sample captions:")
        for s in scenes[:4]:
            print(f'             "{s.caption[:96]}"')
            if s.objects:
                o = s.objects[0]
                print(f"               → {o.label} colour={o.colour} kind={o.kind} activity={o.activity}")

    if pipe.escalator:
        print(f"\nESCALATION {pipe.escalator.stats}")
        for fid, g in list(pipe.deep_results.items())[:2]:
            print(f"           {fid[-12:]} → [{g.tier}] {g.caption[:80]}")

    embs = [r for r in results if r.frame_embedding]
    crops = sum(len(r.crop_embeddings) for r in results)
    print(f"\nEMBEDDING  {len(embs)} frame vectors, {crops} crop vectors")

    snap = METER.snapshot(observed_seconds=src.duration_s * 30)
    print(f"\nCOST       modelled ${snap['cost']['modelled_usd']:.6f} this run")
    if snap["cost"]["per_drone_hour_usd"] is not None:
        print(f"           ${snap['cost']['per_drone_hour_usd']:.4f} per drone-hour at this rate")
    print(f"           {snap['cost']['basis']}")


if __name__ == "__main__":
    asyncio.run(main())
