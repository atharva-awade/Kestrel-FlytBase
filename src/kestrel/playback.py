"""Dense playback indexing, shared by the build script and the upload endpoint.

A playback index is what lets the console play a clip at its own frame rate with
every detection drawn in the right place at the right moment. It is produced by
the same detector, tracker, geo-projection and rule engine the live pipeline
uses, so what an operator watches is a replay of a real analysis rather than an
animation of one.

**Detection runs on every sampled frame; the gate does not suppress it.** The
tier-0 gate exists to protect *model* spend, because hosted VLM calls are
rate-limited and billed. Local detection is neither: YOLO11s measures ~12-15 ms
per frame on this machine's GPU. So the gate's verdict is recorded for display
and governs the hosted tiers, while detection runs regardless. The console shows
both, which makes the distinction visible rather than merely claimed.

Boxes are normalised to 0-1 of frame size so the overlay never needs to know what
resolution the frame was stored at.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

#: Classes worth showing on a security console. A probe on an industrial yard
#: surfaced a spurious `refrigerator @ 0.26`; a class filter plus a confidence
#: floor removes that whole family of embarrassment.
KEEP = {
    "person", "bicycle", "car", "motorcycle", "bus", "truck", "train",
    "backpack", "handbag", "suitcase", "dog", "cat", "bird", "boat",
}
MIN_CONF = 0.35

#: Sampling ceiling. Four of the six bundled clips are natively 10-12.5 fps, so
#: they are indexed frame-for-frame and need no interpolation at all.
MAX_SAMPLE_FPS = 15.0

ProgressFn = Callable[[int, int], None]


def ad_hoc_site(*, site_id: str, name: str, lat: float, lon: float) -> Any:
    """A one-zone site centred on an operator-supplied location.

    Uploaded footage has no site behind it, but every downstream stage, zone
    membership, geo-projection, rule severity, dispatch coordinates, is written
    against a `Site`. Synthesising one means the uploaded clip runs the real
    pipeline unchanged instead of a reduced copy of it, and its alerts come out
    with coordinates near the place the operator named.

    The zone is a ~120 m box around that point, and it deliberately carries the
    id ``restricted-core``.

    That is not a hack, it is the contract. The rule pack keys on zone *ids*
    (`restricted-core`, `substation`, `main-gate`), because rules are written
    against a real site's real geography. An ad-hoc zone with a novel id like
    ``upload-area`` matches no rule in the pack, so an uploaded clip would
    produce detections and tracks and then never raise a single alert - which
    silently breaks the one thing the feature promises. Someone uploading footage
    of a place they want watched is declaring it a monitored area, so mapping it
    onto the pack's restricted-area semantics is also the honest reading.

    It carries no ``normal_hours``, which means presence there is never routine.
    """
    from kestrel.domain import LatLon, Site, Zone, ZoneKind

    d = 0.00055  # ~60 m at the equator; close enough for a demo footprint
    ring = [
        LatLon(lat=lat - d, lon=lon - d),
        LatLon(lat=lat - d, lon=lon + d),
        LatLon(lat=lat + d, lon=lon + d),
        LatLon(lat=lat + d, lon=lon - d),
    ]
    return Site(
        id=site_id,
        name=name,
        kind="uploaded",
        origin=LatLon(lat=lat, lon=lon),
        zones=[
            Zone(
                id="restricted-core",
                name="Monitored area",
                kind=ZoneKind.RESTRICTED,
                polygon=ring,
                priority=1.8,
                notes="Footprint inferred from the location given at upload time.",
            )
        ],
        dock=LatLon(lat=lat, lon=lon),
        geofence=ring,
        live_footage=False,
        notes="Ad-hoc site created for uploaded footage. Telemetry is simulated.",
    )


async def build_index(
    *,
    path: Path,
    slug: str,
    title: str,
    site: Any,
    fps_cap: float = MAX_SAMPLE_FPS,
    start: datetime | None = None,
    zone_id: str | None = None,
    uploaded: bool = False,
    location: dict[str, float] | None = None,
    on_progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Decode, detect, track, project and evaluate rules over a whole clip."""
    import cv2

    from kestrel.config import get_settings
    from kestrel.gate.gate import CostGate
    from kestrel.perception.detect import Detector, YoloBackend
    from kestrel.perception.project import GroundProjector
    from kestrel.perception.track import Tracker
    from kestrel.rules.engine import Observation, RuleEngine
    from kestrel.rules.pack import default_rules
    from kestrel.sim.telemetry import PatrolSimulator

    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist")

    cap = cv2.VideoCapture(str(path))
    native_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if not width or not height:
        cap.release()
        raise ValueError(f"{path.name} has no decodable video stream")

    stride = max(1, round(native_fps / min(native_fps, fps_cap)))
    effective_fps = native_fps / stride
    expected = max(1, total // stride)

    settings = get_settings()

    # YOLO11 is preferred for dense sweeps. If ultralytics or weights are absent
    # (e.g. in a lightweight cloud CPU container), fall back gracefully to the
    # configured detector rather than raising an unhandled ModuleNotFoundError.
    try:
        detector = YoloBackend(threshold=MIN_CONF)
    except Exception:
        detector = Detector(settings=settings).backend

    # ByteTrack's lost-track buffer is counted in frames, so the tracker has to be
    # told the real rate. At 12 fps a buffer sized for 2 fps silently shrinks from
    # 30 seconds of tolerated occlusion to five.
    tracker = Tracker(frame_rate=max(1, round(effective_fps)))
    projector = GroundProjector(site)
    gate = CostGate(settings=settings)
    engine = RuleEngine(site, default_rules())

    # Place the clip on the site clock at night, when the interesting rules apply.
    start = start or datetime.fromisoformat("2026-08-06T02:10:00")
    telemetry_sim = PatrolSimulator(site, start)
    zone = site.zone_by_id(zone_id) if zone_id else (site.zones[0] if site.zones else None)
    if zone is not None:
        telemetry_sim.waypoints = [(zone.centroid, zone.id, 7200.0)]

    frames_out: list[dict[str, Any]] = []
    alerts_out: list[dict[str, Any]] = []
    tracks: dict[str, dict[str, Any]] = {}
    analysed = skipped = 0
    detect_ms = 0.0
    idx = seq = 0

    while True:
        ok, image = cap.read()
        if not ok:
            break
        if idx % stride:
            idx += 1
            continue

        video_t = idx / native_fps
        ts = start + timedelta(seconds=video_t)
        telemetry = telemetry_sim.at(ts)
        frame_id = f"pbk_{slug}_{seq:06d}"

        verdict = await gate.decide(image=image, phash=None, ts=ts, telemetry=telemetry)
        if verdict.analyse:
            analysed += 1
        else:
            skipped += 1

        t0 = time.perf_counter()
        raw = [
            d for d in detector.detect(image)
            if d.confidence >= MIN_CONF and d.label in KEEP
        ]
        tracked = tracker.update(raw)
        detect_ms += (time.perf_counter() - t0) * 1000

        dets: list[dict[str, Any]] = []
        for td in tracked:
            proj = projector.project(td.bbox, telemetry, width, height)
            b = td.bbox
            dets.append({
                "x1": round(b.x1 / width, 4), "y1": round(b.y1 / height, 4),
                "x2": round(b.x2 / width, 4), "y2": round(b.y2 / height, 4),
                "label": td.label,
                "conf": round(float(td.confidence), 3),
                "track": td.track_id,
                "zone": proj.zone_id,
            })
            if td.track_id is not None:
                node = tracks.setdefault(
                    str(td.track_id),
                    {"label": td.label, "first_t": round(video_t, 3), "frames": 0},
                )
                node["last_t"] = round(video_t, 3)
                node["frames"] += 1

            # Rules see every detection, so dwell and sequence operators get the
            # temporal resolution they were designed for.
            obs = Observation(
                ts=ts,
                frame_id=frame_id,
                entity_id=f"trk-{td.track_id}" if td.track_id is not None else None,
                label=td.label,
                confidence=td.confidence,
                zone_id=proj.zone_id,
                perception_confidence=td.confidence,
                world=proj.world,
            )
            for res in engine.evaluate(obs, telemetry=telemetry):
                if not res.fired:
                    continue
                alert = engine.to_alert(res, telemetry=telemetry)
                alerts_out.append({
                    "t": round(video_t, 2),
                    "id": alert.id,
                    "severity": alert.severity.value,
                    "title": alert.title,
                    "rule_id": res.rule.id,
                    "confidence": round(float(alert.confidence), 3),
                    "zone_id": obs.zone_id,
                    "location": (
                        alert.location.model_dump(mode="json") if alert.location else None
                    ),
                })

        frames_out.append({
            "t": round(video_t, 3),
            "analysed": verdict.analyse,
            "gate_reason": verdict.reason,
            "dets": dets,
        })
        seq += 1
        idx += 1
        if on_progress and seq % 10 == 0:
            on_progress(seq, expected)

    cap.release()
    if on_progress:
        on_progress(seq, max(seq, expected))

    sampled = analysed + skipped
    return {
        "clip": slug,
        "title": title,
        "file": path.name,
        "width": width,
        "height": height,
        "fps": round(native_fps, 3),
        "duration_s": round(total / native_fps, 2) if native_fps else 0,
        "sampled_fps": round(effective_fps, 2),
        "sampled_frames": sampled,
        "detector": detector.name,
        "detector_device": detector.device,
        "mean_detect_ms": round(detect_ms / max(1, sampled), 1),
        "gate": {
            "analysed": analysed,
            "skipped": skipped,
            "efficiency": round(skipped / sampled, 4) if sampled else 0.0,
        },
        "telemetry": "simulated",
        "site_id": site.id,
        "uploaded": uploaded,
        "location": location,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "frames": frames_out,
        "alerts": alerts_out,
        "tracks": tracks,
        "note": (
            "Boxes are normalised to 0-1 of frame width and height. Detection runs "
            "on every sampled frame because it is local and costs no API budget; "
            "the gate verdict is recorded for display and governs the hosted tiers. "
            "Telemetry is simulated - there is no aircraft."
        ),
    }


async def build_upload_index(
    *,
    path: Path,
    slug: str,
    label: str,
    lat: float,
    lon: float,
    on_progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Index an operator's own footage against a real location."""
    site = ad_hoc_site(site_id=slug, name=label or "Uploaded footage", lat=lat, lon=lon)
    return await build_index(
        path=path,
        slug=slug,
        title=label or "Uploaded footage",
        site=site,
        uploaded=True,
        location={"lat": lat, "lon": lon},
        on_progress=on_progress,
    )
