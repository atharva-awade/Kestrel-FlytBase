"""Verify the local detection + tracking tiers actually work on this machine.

Checks three things that the pipeline assumes and that can each fail silently:
  1. a detector backend loads, and reports honestly which one it is
  2. open-vocabulary prompting responds to the phrases it is given
  3. ByteTrack holds identity across frames as an object moves
"""

from __future__ import annotations

import time
from datetime import datetime

from kestrel.ingest.sources import SyntheticSource
from kestrel.perception.detect import get_detector
from kestrel.perception.track import Tracker
from kestrel.sim.sites import build_plant_01


def main() -> None:
    print("\n[1] Loading detector (first run downloads weights)")
    t0 = time.perf_counter()
    det = get_detector()
    print(f"    loaded in {time.perf_counter()-t0:.1f}s")
    print(f"    {det.info}")
    if det.info["degraded"]:
        print(f"    NOTE: {det.info['fallback_reason']}")

    site = build_plant_01()
    src = SyntheticSource(site, start_clock=datetime(2026, 8, 6, 12, 0, 0), n=12)
    frames = list(src)
    print(f"\n[2] Detecting on {len(frames)} synthetic frames")

    tracker = Tracker(frame_rate=2)
    print(f"    tracker backend: {tracker.info['backend']}")

    seen_ids: dict[int, int] = {}
    total_ms = 0.0
    for i, rf in enumerate(frames):
        t = time.perf_counter()
        dets = det.detect(rf.image, ["person", "truck", "car", "vehicle"])
        ms = (time.perf_counter() - t) * 1000
        total_ms += ms
        tracked = tracker.update(dets)
        for td in tracked:
            if td.track_id is not None:
                seen_ids[td.track_id] = seen_ids.get(td.track_id, 0) + 1
        if i < 4 or i == len(frames) - 1:
            labels = ", ".join(
                f"{t_.label}#{t_.track_id}({t_.confidence:.2f})" for t_ in tracked
            ) or "none"
            print(f"    frame {i:>2}  {ms:>7.0f}ms  {len(dets)} det  → {labels}")

    print("\n[3] Results")
    print(f"    mean detect latency : {total_ms/len(frames):.0f}ms/frame")
    print(f"    distinct track ids  : {len(seen_ids)}")
    print(f"    track persistence   : {sorted(seen_ids.values(), reverse=True)[:6]}")

    longest = max(seen_ids.values()) if seen_ids else 0
    if longest >= len(frames) * 0.5:
        print(f"    → identity HELD across {longest}/{len(frames)} frames")
    elif seen_ids:
        print(f"    → identity fragmented (longest run {longest}/{len(frames)})")
    else:
        print("    → nothing tracked")

    # Open-vocabulary check: a phrase the default vocabulary does not contain.
    if det.info["open_vocabulary"]:
        print("\n[4] Open-vocabulary prompting")
        img = frames[6].image
        for phrase in (["person"], ["blue truck"], ["traffic cone"]):
            d = det.detect(img, phrase)
            print(f"    {phrase!s:<20} → {len(d)} box(es) "
                  f"{[f'{x.label}:{x.confidence:.2f}' for x in d][:3]}")
    else:
        print("\n[4] Open-vocabulary prompting unavailable on this backend")


if __name__ == "__main__":
    main()
