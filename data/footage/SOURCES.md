# Footage sources and licences

KESTREL runs real video through a real perception pipeline, so the clips it
uses need unambiguous licensing. Every clip below is **CC BY 4.0** — free to
use, modify and redistribute, including commercially, with attribution.

## Attribution

> Video clips from [`intel-iot-devkit/sample-videos`](https://github.com/intel-iot-devkit/sample-videos) © Intel Corporation,
> licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

## Why not VisDrone or VIRAT

The obvious choices for aerial-surveillance work are the academic datasets.
They were considered and rejected: VisDrone is distributed under
**CC BY-NC-SA 3.0** — academic and non-commercial use only. This submission is
delivered to a company, so a non-commercial restriction is a live constraint
rather than a technicality, and no amount of convenience is worth the risk.

## Clips

| File | What it exercises | Resolution | FPS | Duration |
|---|---|---|---|---|
| `person-bicycle-car.mp4` ★ | Mixed traffic — pedestrians, cyclists and vehicles on one road. Exercises multi-class tracking and entity re-identification. | 768×432 | 12.0 | 53.9s |
| `worker-zone.mp4` ★ | Industrial zone with workers in high-visibility clothing. Primary clip for the plant-01 demo: person detection, zone dwell, restricted-area rules. | 1920×1080 | 59.94 | 75.9s |
| `car-detection.mp4` | Vehicle flow. Used for the gate/after-hours-vehicle rules and for vehicle entity persistence across visits. | 768×432 | 12.5 | 30.2s |
| `one-by-one-person.mp4` | People entering a scene sequentially. Tracker identity continuity and the tailgating sequence rule. | 768×432 | 10.0 | 139.4s |
| `people-detection.mp4` | Pedestrians at a shallow camera angle. Loitering and dwell-time rules, plus oblique-projection confidence. | 768×432 | 12.0 | 49.7s |
| `store-aisle.mp4` | Overhead interior view. Stands in for a warehouse aisle — top-down geometry closest to a nadir drone camera. | 720×404 | 59.94 | 65.4s |

★ = primary clips used in the recorded demo.

## Reproducing

Video files are deliberately **not** committed — they are large and
re-fetchable. Restore them with:

```bash
uv run python scripts/fetch_footage.py
```

`manifest.json` records the exact resolution, frame count and byte size of
each clip as downloaded, so a re-fetch can be verified against the run that
produced the results in the report.