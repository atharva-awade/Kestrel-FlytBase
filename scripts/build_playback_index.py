"""Build a dense, frame-accurate playback index for each demo clip.

    uv run python scripts/build_playback_index.py --all
    uv run python scripts/build_playback_index.py --clip worker-zone --fps 15

**Why this exists.** The console used to show still JPEGs sampled at 2 fps, which
is a fair picture of what the *cost cascade* does and a poor picture of what the
*system* does. An operator watches video. So does an interviewer.

**Why a dense pass does not contradict the cost cascade.** The gate exists to
protect *model* spend: hosted VLM calls are rate-limited and billed. Local
detection is neither. YOLO11s measures ~52 ms/frame at 1080p on this machine, so
detecting every sampled frame costs GPU time we already own and no API budget at
all. This pass therefore runs the gate and *records* its verdict for display,
while letting detection run regardless. The gate still governs the expensive
tiers. That split is the honest version of the architecture, and the index makes
it visible: you can watch the gate skip frames while the boxes keep tracking.

Output is one JSON per clip in `data/playback/`, with boxes **normalised to 0-1**
so the overlay never needs to know the frame's stored resolution.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

OUT_DIR = REPO / "data" / "playback"
FOOTAGE = REPO / "data" / "footage"

#: Detection classes worth showing on a security console. The probe surfaced a
#: spurious `refrigerator @ 0.26` on an industrial yard; a class filter plus a
#: confidence floor removes that whole family of embarrassment.
KEEP = {
    "person", "bicycle", "car", "motorcycle", "bus", "truck", "train",
    "backpack", "handbag", "suitcase", "dog", "cat", "bird", "boat",
}
MIN_CONF = 0.35

#: Sampling ceiling. Four of the six clips are natively 10-12.5 fps, so they are
#: indexed frame-for-frame and need no interpolation at all.
MAX_SAMPLE_FPS = 15.0


def load_manifest() -> list[dict[str, Any]]:
    return json.loads((FOOTAGE / "manifest.json").read_text(encoding="utf-8"))


async def build(entry: dict[str, Any], *, fps_cap: float) -> dict[str, Any]:
    """Index one bundled clip against the flagship site."""
    from kestrel.playback import build_index
    from kestrel.sim.sites import build_plant_01

    path = FOOTAGE / entry["file"]
    if not path.exists():
        raise FileNotFoundError(f"{path} missing; run scripts/fetch_footage.py")

    return await build_index(
        path=path,
        slug=entry["slug"],
        title=entry.get("scenario", entry["slug"])[:110],
        site=build_plant_01(),
        fps_cap=fps_cap,
        zone_id="substation",
        on_progress=lambda done, total: (
            print(f"    {done}/{total} frames", flush=True) if done % 200 == 0 else None
        ),
    )


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clip", help="one slug from data/footage/manifest.json")
    ap.add_argument("--all", action="store_true", help="every clip in the manifest")
    ap.add_argument("--fps", type=float, default=MAX_SAMPLE_FPS, help="sampling ceiling")
    args = ap.parse_args()

    manifest = load_manifest()
    if args.all:
        targets = manifest
    elif args.clip:
        targets = [m for m in manifest if m["slug"] == args.clip]
        if not targets:
            print(f"unknown clip '{args.clip}'. Known: "
                  f"{', '.join(m['slug'] for m in manifest)}")
            return 1
    else:
        ap.print_help()
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\nBuilding playback indexes -> {OUT_DIR.relative_to(REPO)}")
    print("=" * 78)

    for entry in targets:
        t0 = time.perf_counter()
        try:
            index = await build(entry, fps_cap=args.fps)
        except Exception as e:
            print(f"  [FAIL] {entry['slug']}: {type(e).__name__}: {e}")
            continue
        out = OUT_DIR / f"{entry['slug']}.json"
        out.write_text(json.dumps(index, separators=(",", ":")), encoding="utf-8")
        mb = out.stat().st_size / 1e6
        el = time.perf_counter() - t0
        print(f"  [ok] {entry['slug']:<20} {index['sampled_frames']:>5} frames  "
              f"{len(index['alerts']):>3} alerts  {mb:.2f} MB  in {el:.0f}s")

    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
