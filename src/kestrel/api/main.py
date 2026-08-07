"""KESTREL API.

REST for state, SSE for streaming, and one deliberate asymmetry: the agent may
propose an action over the normal chat route, but executing it requires a POST to
a separate endpoint carrying an explicit decision. The permission boundary is a
different URL, not a different prompt.

No provider credential is ever returned by any route. Model calls proxy through
this process; the browser never holds a model key.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from kestrel.config import get_settings
from kestrel.obs.meter import METER
from kestrel.storage.db import get_db
from kestrel.storage.ledger import Ledger

settings = get_settings()

#: Prebuilt playback indexes, one JSON per clip, written by
#: `scripts/build_playback_index.py`. Committed, so the console has something to
#: play the moment the repo is cloned.
PLAYBACK_DIR = Path("data/playback")

app = FastAPI(
    title="KESTREL",
    description="Autonomous drone security analyst",
    version="0.1.0",
)
cors_origins = settings.cors_list
allow_all = "*" in cors_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=[] if allow_all else cors_origins,
    allow_origin_regex=r"^https?://.*" if allow_all else None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_agent = None
_fleet = None


def agent():
    global _agent
    if _agent is None:
        from kestrel.agent.factory import build_agent

        _agent = build_agent()
    return _agent


def fleet():
    global _fleet
    if _fleet is None:
        from kestrel.fleet.fleet import FleetManager
        from kestrel.sim.sites import load_fleet

        _fleet = FleetManager(load_fleet(settings.sites_dir), get_db())
    return _fleet


# ═══════════════════════════════════════════════════════════════════════════════
# System
# ═══════════════════════════════════════════════════════════════════════════════
@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "KESTREL API — Autonomous Drone Security Analyst",
        "status": "online",
        "mode": settings.mode.value,
        "docs": "/docs",
        "health": "/api/health",
    }


@app.get("/health")
@app.get("/api/health")
def health() -> dict[str, Any]:
    from kestrel.clients.models import get_client

    db = get_db()
    client = get_client()
    h = client.health
    # Never leak a credential. Report presence, never value.
    return {
        "status": "ok",
        "mode": h["mode"],
        "requested_mode": h["requested_mode"],
        "roster": h["roster"],
        "providers": [
            {k: v for k, v in p.items() if k != "api_key"} for p in h["providers"]
        ],
        "cassettes": h["cassettes"],
        "storage": db.stats,
        "ledger": Ledger(db).stats,
        "runs_without_api_key": h["mode"] == "replay",
    }


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    from kestrel.perception.detect import get_detector

    db = get_db()
    try:
        det = get_detector().info
    except Exception as e:
        det = {"backend": "unavailable", "error": str(e)[:120]}
    return {
        "meter": METER.snapshot(),
        "storage": db.stats,
        "detector": det,
        "ledger": Ledger(db).stats,
        "agent": agent().stats,
    }


@app.get("/api/tools")
def tools() -> dict[str, Any]:
    """The tool contract the frontend renders against."""
    return agent().registry.export_schema()


# ═══════════════════════════════════════════════════════════════════════════════
# Sites and fleet
# ═══════════════════════════════════════════════════════════════════════════════
@app.get("/api/sites")
def sites() -> dict[str, Any]:
    from kestrel.sim.sites import load_fleet

    out = []
    for s in load_fleet(settings.sites_dir):
        out.append({
            "id": s.id, "name": s.name, "kind": s.kind,
            "lat": s.origin.lat, "lon": s.origin.lon,
            "country": s.country, "country_name": s.country_name,
            "timezone": s.timezone, "live_footage": s.live_footage,
            "simulated": not s.live_footage,
            "zones": len(s.zones),
        })
    return {"count": len(out), "sites": out}


@app.get("/api/sites/{site_id}")
def site_detail(site_id: str) -> dict[str, Any]:
    from kestrel.sim.sites import load_site

    try:
        s = load_site(site_id, settings.sites_dir)
    except KeyError:
        raise HTTPException(404, f"unknown site {site_id}") from None
    return {
        **s.model_dump(mode="json"),
        "simulated": not s.live_footage,
        # GeoJSON so the map can render zones without a transform step.
        "geojson": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "id": z.id, "name": z.name, "kind": z.kind.value,
                        "priority": z.priority,
                        "normal_hours": list(z.normal_hours) if z.normal_hours else None,
                        "notes": z.notes,
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[p.lon, p.lat] for p in z.polygon]
                                        + [[z.polygon[0].lon, z.polygon[0].lat]]],
                    },
                }
                for z in s.zones
            ],
        },
    }


@app.get("/api/fleet")
def fleet_status() -> dict[str, Any]:
    f = fleet()
    return {
        "summary": f.summary(),
        "sites": [s.to_dict() for s in f.status()],
        "by_country": f.by_country(),
    }


@app.get("/api/fleet/correlations")
def fleet_correlations() -> dict[str, Any]:
    a = agent()
    matches = fleet().correlate_entities(a.ctx.entity_vectors())
    return {
        "count": len(matches),
        "matches": [m.to_dict() for m in matches],
        "patterns": fleet().find_temporal_pattern(),
        "note": "Sites flagged simulated do not carry a live feed.",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Frames, alerts, entities
# ═══════════════════════════════════════════════════════════════════════════════
@app.get("/api/frames")
def frames(site_id: str = "plant-01", limit: int = 60,
           analysed_only: bool = True) -> dict[str, Any]:
    db = get_db()
    rows = db.frames(site_id, limit=min(limit, 500), analysed_only=analysed_only)
    for r in rows:
        r["scene"] = json.loads(r.pop("scene_json") or "null")
        r["telemetry"] = json.loads(r.pop("telemetry_json") or "null")
        r["detections"] = [
            dict(d) for d in db.query(
                "SELECT * FROM detections WHERE frame_id = ?", (r["id"],))
        ]
    return {"count": len(rows), "frames": rows}


@app.get("/api/frames/{frame_id}/image")
def frame_image(frame_id: str):
    db = get_db()
    rows = db.query("SELECT path FROM frames WHERE id = ?", (frame_id,))
    if not rows or not rows[0]["path"]:
        raise HTTPException(404, "no image for that frame")
    p = Path(rows[0]["path"])
    if not p.is_absolute():
        p = settings.frame_dir.parent.parent / p
    if not p.exists():
        raise HTTPException(404, "image file missing")
    return FileResponse(p, media_type="image/jpeg")


# ═══════════════════════════════════════════════════════════════════════════════
# Playback: the video itself, and the index that annotates it
# ═══════════════════════════════════════════════════════════════════════════════
def _known_clips() -> dict[str, dict[str, Any]]:
    """Clip slugs from the footage manifest, used as an allow-list.

    The slug arrives from the URL, so it is never joined to a path without being
    checked against this map first.
    """
    manifest = settings.footage_dir / "manifest.json"
    if not manifest.exists():
        return {}
    try:
        entries = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {e["slug"]: e for e in entries if isinstance(e, dict) and e.get("slug")}


#: An upload slug is minted server-side as `upload-<uuid4 hex[:12]>`, so anything
#: that does not match this exactly is not one of ours.
UPLOAD_SLUG = re.compile(r"^upload-[0-9a-f]{12}$")


def _resolve_clip(clip: str) -> Path | None:
    """The file behind a clip slug, or None.

    Two sources, one resolver: the bundled manifest and the operator's own
    uploads. The slug always comes from a URL, so it is matched against an
    allow-list or a strict pattern before it is joined to anything, and the
    resolved path is then confirmed to sit inside its directory. A slug is never
    trusted to name a file on its own.
    """
    entry = _known_clips().get(clip)
    if entry is not None:
        path = (settings.footage_dir / entry["file"]).resolve()
        return path if path.is_file() else None

    if not UPLOAD_SLUG.match(clip):
        return None
    uploads = UPLOAD_DIR.resolve()
    path = (uploads / f"{clip}.mp4").resolve()
    # Belt and braces: the pattern already forbids separators, but a resolved
    # path escaping its directory is the failure worth being certain about.
    if uploads not in path.parents or not path.is_file():
        return None
    return path


def _uploaded_clips() -> list[dict[str, Any]]:
    """Uploaded clips that have finished indexing, newest first."""
    out: list[dict[str, Any]] = []
    if not PLAYBACK_DIR.exists():
        return out
    for f in PLAYBACK_DIR.glob("upload-*.json"):
        if _resolve_clip(f.stem) is None:
            continue          # index without its video: nothing to play
        try:
            idx = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "slug": idx.get("clip", f.stem),
            "title": idx.get("title") or "Uploaded footage",
            "width": idx.get("width"), "height": idx.get("height"),
            "fps": idx.get("fps"), "duration_s": idx.get("duration_s"),
            "primary": False, "uploaded": True,
            "location": idx.get("location"),
            "attribution": "operator upload", "licence": None,
            "indexed": True,
            "video_url": f"/api/footage/{idx.get('clip', f.stem)}.mp4",
            "built_at": idx.get("built_at", ""),
        })
    out.sort(key=lambda c: c.get("built_at", ""), reverse=True)
    return out


@app.get("/api/clips")
def clips() -> dict[str, Any]:
    """Every clip available to the console, and whether it has been indexed."""
    out = []
    for slug, entry in _known_clips().items():
        index = PLAYBACK_DIR / f"{slug}.json"
        out.append({
            "slug": slug,
            "title": entry.get("scenario", slug),
            "width": entry.get("width"),
            "height": entry.get("height"),
            "fps": entry.get("fps"),
            "duration_s": entry.get("duration_s"),
            "primary": bool(entry.get("primary")),
            "attribution": entry.get("attribution"),
            "licence": entry.get("licence"),
            "indexed": index.exists(),
            "uploaded": False,
            "video_url": f"/api/footage/{slug}.mp4",
        })
    out.sort(key=lambda c: (not c["primary"], c["slug"]))
    # Uploads go last so the bundled demo clips keep their order, and newest
    # upload first within that group.
    out.extend(_uploaded_clips())
    return {"count": len(out), "clips": out}


@app.get("/api/footage/{clip}.mp4")
def footage(clip: str):
    """Serve a demo clip.

    `FileResponse` handles `Range`, which is what makes the `<video>` element
    seekable. Without byte-range support the browser can play from the start and
    nothing else, so scrubbing the timeline would not work.
    """
    path = _resolve_clip(clip)
    if path is None:
        if clip in _known_clips():
            raise HTTPException(
                404,
                f"'{clip}' is not on disk. Run: uv run python scripts/fetch_footage.py",
            )
        raise HTTPException(404, f"unknown clip '{clip}'")
    return FileResponse(path, media_type="video/mp4", filename=f"{clip}.mp4")


@app.get("/api/playback/{clip}")
def playback(clip: str) -> dict[str, Any]:
    """The dense detection index that annotates a clip during playback."""
    if clip not in _known_clips() and not (PLAYBACK_DIR / f"{clip}.json").exists():
        raise HTTPException(404, f"unknown clip '{clip}'")
    path = PLAYBACK_DIR / f"{clip}.json"
    if not path.exists():
        raise HTTPException(
            404,
            f"'{clip}' has no playback index. Build one with: "
            f"uv run python scripts/build_playback_index.py --clip {clip}",
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise HTTPException(500, f"playback index for '{clip}' is unreadable: {e}") from None


# ── bring your own footage ────────────────────────────────────────────────────
UPLOAD_DIR = Path("data/uploads")
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_UPLOAD_SECONDS = 600

#: job id -> progress record. In-memory on purpose: an upload is scoped to the
#: session that made it, and a restart legitimately forgets it.
_UPLOAD_JOBS: dict[str, dict[str, Any]] = {}

#: Strong references to in-flight indexing tasks.
#:
#: The event loop holds only a *weak* reference to a task, so a bare
#: `asyncio.create_task(...)` whose result nobody keeps can be garbage collected
#: while it is still running. Indexing a clip takes minutes, which is a wide
#: window for that to happen, and the symptom would be an upload that stops
#: partway with no error anywhere: the job record simply never leaves "indexing".
_UPLOAD_TASKS: set[asyncio.Task[None]] = set()


@app.post("/api/upload/video")
async def upload_video(
    file: UploadFile = File(...),
    lat: float = Form(...),
    lon: float = Form(...),
    label: str = Form(""),
) -> dict[str, Any]:
    """Accept an operator's own footage and analyse it against a real location.

    The point is that nothing here is special-cased for the bundled clips. The
    uploaded video runs the same detector, tracker, geo-projection and rule
    engine, anchored at the coordinates the operator gives, so its alerts carry
    dispatchable positions near that place rather than near the demo site.

    Validation is deliberately strict and its refusals are specific: an upload
    that fails silently is worse than one that explains itself.
    """
    import cv2

    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise HTTPException(400, f"coordinates out of range: {lat}, {lon}")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex[:12]
    slug = f"upload-{job_id}"
    dest = UPLOAD_DIR / f"{slug}.mp4"

    size = 0
    try:
        with dest.open("wb") as fh:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    fh.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        413, f"file exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB"
                    )
                fh.write(chunk)
    except HTTPException:
        raise
    except OSError as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(500, f"could not store the upload: {e}") from None

    # Prove it is decodable video before promising to analyse it.
    cap = cv2.VideoCapture(str(dest))
    ok, first = cap.read()
    fps = cap.get(cv2.CAP_PROP_FPS) or 0
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    cap.release()

    if not ok or first is None or not width or not height:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "that file could not be decoded as video")
    duration = frames / fps if fps else 0
    if duration > MAX_UPLOAD_SECONDS:
        dest.unlink(missing_ok=True)
        raise HTTPException(
            413, f"clip is {duration / 60:.1f} min; the limit is "
                 f"{MAX_UPLOAD_SECONDS // 60} min"
        )

    _UPLOAD_JOBS[job_id] = {
        "job_id": job_id, "slug": slug, "state": "queued", "progress": 0.0,
        "message": "queued", "label": label or "Uploaded footage",
        "lat": lat, "lon": lon, "width": width, "height": height,
        "fps": round(fps, 3), "duration_s": round(duration, 2), "frames": frames,
    }

    def save_job() -> None:
        try:
            (UPLOAD_DIR / f"{job_id}.json").write_text(
                json.dumps(_UPLOAD_JOBS[job_id], separators=(",", ":")), encoding="utf-8"
            )
        except Exception:
            pass

    save_job()
    task = asyncio.create_task(_index_upload(job_id, dest))
    _UPLOAD_TASKS.add(task)
    task.add_done_callback(_UPLOAD_TASKS.discard)
    return {"ok": True, **_UPLOAD_JOBS[job_id]}


async def _index_upload(job_id: str, path: Path) -> None:
    """Run the dense index over an uploaded clip, reporting progress as it goes."""
    job = _UPLOAD_JOBS[job_id]

    def save_job() -> None:
        try:
            (UPLOAD_DIR / f"{job_id}.json").write_text(
                json.dumps(job, separators=(",", ":")), encoding="utf-8"
            )
        except Exception:
            pass

    try:
        # Playability first, indexing second. The detector and the browser accept
        # different things -- OpenCV reads MPEG-4 Part 2 that no browser can play
        # -- so a clip could index perfectly and still fail with "the element has
        # no supported sources". Converting before indexing also keeps the index
        # timestamps aligned with the file that is actually served.
        from kestrel.media import ensure_browser_playable

        job["state"] = "converting"
        job["message"] = "preparing for playback"
        save_job()
        conversion = await ensure_browser_playable(
            path, on_progress=lambda f: job.update(progress=round(f, 3))
        )
        job["conversion"] = conversion
        if conversion["action"] != "kept":
            job["message"] = str(conversion["reason"])
        save_job()

        job["state"] = "indexing"
        job["progress"] = 0.0
        job["message"] = "detecting objects"
        save_job()

        from kestrel.playback import build_upload_index

        def report(done: int, total: int) -> None:
            job["progress"] = round(done / total, 3) if total else 0.0
            job["message"] = f"{done} of {total} frames"
            save_job()

        index = await build_upload_index(
            path=path, slug=job["slug"], label=job["label"],
            lat=job["lat"], lon=job["lon"], on_progress=report,
        )
        PLAYBACK_DIR.mkdir(parents=True, exist_ok=True)
        (PLAYBACK_DIR / f"{job['slug']}.json").write_text(
            json.dumps(index, separators=(",", ":")), encoding="utf-8"
        )
        job.update(
            state="ready", progress=1.0,
            message=f"{index['sampled_frames']} frames, "
                    f"{sum(len(f['dets']) for f in index['frames'])} detections, "
                    f"{len(index['alerts'])} alerts",
            alerts=len(index["alerts"]),
        )
        save_job()
    except Exception as e:
        job.update(state="failed", message=f"{type(e).__name__}: {e}"[:200])
        save_job()


@app.get("/api/upload/{job_id}/progress")
def upload_progress(job_id: str) -> dict[str, Any]:
    job = _UPLOAD_JOBS.get(job_id)
    if job is not None:
        return job
    # Check disk cache in case container restarted
    p = UPLOAD_DIR / f"{job_id}.json"
    if p.exists():
        try:
            cached = json.loads(p.read_text(encoding="utf-8"))
            _UPLOAD_JOBS[job_id] = cached
            return cached
        except Exception:
            pass
    raise HTTPException(404, "unknown upload job")


@app.get("/api/alerts")
def alerts(site_id: str | None = None, status: str | None = None,
           limit: int = 50) -> dict[str, Any]:
    db = get_db()
    rows = db.alerts(site_id, status=status, limit=min(limit, 300))
    out = []
    for a in rows:
        ev = json.loads(a.pop("evidence_json") or "[]")
        a["evidence"] = ev
        a["entity_ids"] = json.loads(a.pop("entity_ids_json") or "[]")
        a["frame_ids"] = json.loads(a.pop("frame_ids_json") or "[]")
        a["location"] = next(
            (e["detail"] for e in ev
             if e.get("kind") == "telemetry" and "Dispatch" in (e.get("caption") or "")),
            None,
        )
        out.append(a)
    return {"count": len(out), "alerts": out}


@app.get("/api/entities")
def entities(site_id: str = "plant-01", limit: int = 100) -> dict[str, Any]:
    db = get_db()
    rows = db.entities(site_id, limit=min(limit, 500))
    for r in rows:
        r["attributes"] = json.loads(r.pop("attributes_json") or "{}")
        r["zones"] = json.loads(r.pop("zones_json") or "[]")
        r["sites"] = json.loads(r.pop("sites_json") or "[]")
    return {"count": len(rows), "entities": rows}


@app.get("/api/entities/{entity_id}")
def entity_detail(entity_id: str) -> dict[str, Any]:
    db = get_db()
    row = db.entity(entity_id)
    if not row:
        raise HTTPException(404, f"unknown entity {entity_id}")
    row["attributes"] = json.loads(row.pop("attributes_json") or "{}")
    row["zones"] = json.loads(row.pop("zones_json") or "[]")
    row["sites"] = json.loads(row.pop("sites_json") or "[]")
    sightings = [
        dict(s) for s in db.query(
            "SELECT * FROM detections WHERE entity_id = ? ORDER BY ts DESC LIMIT 200",
            (entity_id,))
    ]
    return {"entity": row, "sightings": sightings, "count": len(sightings)}


@app.get("/api/missions")
def missions(site_id: str = "plant-01", limit: int = 50) -> dict[str, Any]:
    db = get_db()
    rows = [
        dict(r) for r in db.query(
            "SELECT * FROM missions WHERE site_id=? ORDER BY created_ts DESC LIMIT ?",
            (site_id, limit))
    ]
    for r in rows:
        r["steps"] = json.loads(r.pop("steps_json"))
        r["feasibility"] = json.loads(r.pop("feasibility_json"))
    return {"count": len(rows), "missions": rows}


@app.get("/api/memory")
def memory(site_id: str = "plant-01", level: str | None = None) -> dict[str, Any]:
    db = get_db()
    sql = "SELECT * FROM memory_nodes WHERE site_id = ?"
    p: list[Any] = [site_id]
    if level:
        sql += " AND level = ?"
        p.append(level)
    sql += " ORDER BY start_ts DESC LIMIT 200"
    rows = [dict(r) for r in db.query(sql, tuple(p))]
    for r in rows:
        r["child_ids"] = json.loads(r.pop("child_ids_json") or "[]")
        r["entity_ids"] = json.loads(r.pop("entity_ids_json") or "[]")
    return {"count": len(rows), "nodes": rows}


@app.get("/api/rules")
def rules() -> dict[str, Any]:
    a = agent()
    return {
        "count": len(a.ctx.engine.rules),
        "rules": [
            {
                "id": r.id, "name": r.name, "description": r.description,
                "severity": r.severity.value, "enabled": r.enabled,
                "origin": r.origin, "tags": r.tags,
                "conditions": r.explain(),
                "visual_predicate": r.visual_predicate,
                "cooldown_seconds": r.cooldown_seconds,
                "yaml": r.to_yaml(),
                "fires": a.ctx.engine.stats["fires"].get(r.id, 0),
            }
            for r in a.ctx.engine.rules
        ],
    }


@app.get("/api/ledger")
def ledger(site_id: str | None = None, limit: int = 100) -> dict[str, Any]:
    led = Ledger(get_db())
    return {
        "verification": led.verify(),
        "stats": led.stats,
        "entries": led.entries(site_id=site_id, limit=min(limit, 500)),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Search
# ═══════════════════════════════════════════════════════════════════════════════
@app.get("/api/search")
async def search(q: str = Query(..., min_length=1), site_id: str = "plant-01",
                 limit: int = 24, rerank: bool = False) -> dict[str, Any]:
    from kestrel.clients.models import get_client
    from kestrel.retrieval.search import HybridSearch
    from kestrel.sim.sites import load_site

    site = load_site(site_id, settings.sites_dir)
    hs = HybridSearch(get_db(), site, get_client())
    res = await hs.search(q, limit=min(limit, 60))
    if rerank:
        res.hits = await hs.rerank(q, res.hits)
    return res.to_dict()


# ═══════════════════════════════════════════════════════════════════════════════
# Ask KESTREL
# ═══════════════════════════════════════════════════════════════════════════════
class AskBody(BaseModel):
    question: str
    selection: dict[str, Any] | None = None
    #: Opt in to carried conversation history. Omitted means a stateless turn.
    conversation_id: str | None = None


#: Conversation history, per conversation rather than per process.
_HISTORIES: dict[str, list[dict[str, str]]] = {}


def _scoped_history(conversation_id: str | None) -> list[dict[str, str]]:
    """Install the right history on the shared agent and return it.

    The agent is a module-level singleton, so `self.history` was accumulating
    across every request for the life of the process. That is wrong twice over.

    It is a correctness bug: two operators asking unrelated questions shared one
    conversation, and each could see the other's context folded into their
    prompt.

    It is also what made the agent's cassettes unreplayable. History goes into
    the prompt, the prompt is the cassette key, so question N's key depended on
    the answers to questions 1..N-1. A recording could only ever replay if the
    exact same questions were asked in the exact same order against a process
    with the exact same prior state -- which is not how anyone demonstrates a
    system, and not how the operator clicks.

    Without an explicit conversation id a turn is stateless, so its key depends
    only on the question, the selection and the data clock. Follow-up context is
    still available to any client that passes an id, and deixis ("this vehicle")
    is resolved from `selection` rather than from history in any case.
    """
    if conversation_id:
        return _HISTORIES.setdefault(conversation_id, [])
    return []


@app.post("/api/ask")
async def ask(body: AskBody) -> dict[str, Any]:
    a = agent()
    a.history = _scoped_history(body.conversation_id)
    turn = await a.ask(body.question, selection=body.selection)
    return turn.to_dict()


@app.post("/api/ask/stream")
async def ask_stream(body: AskBody):
    """SSE so the console can show the agent working rather than a spinner."""

    async def gen():
        try:
            a = agent()
            a.history = _scoped_history(body.conversation_id)
            async for event in a.stream(body.question, selection=body.selection):
                yield f"data: {json.dumps(event, default=str)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)[:200]})}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ConfirmBody(BaseModel):
    tool: str
    arguments: dict[str, Any]
    approve: bool


@app.post("/api/ask/confirm")
async def ask_confirm(body: ConfirmBody) -> dict[str, Any]:
    """The only route that can execute a gated tool.

    Deliberately separate from /api/ask: the agent cannot reach this, so a state
    change always requires a distinct, human-initiated request.
    """
    return await agent().confirm(body.tool, body.arguments, approve=body.approve)


@app.get("/api/brief")
async def brief() -> dict[str, Any]:
    return {"brief": await agent().morning_brief(),
            "generated_at": datetime.now().isoformat()}


# ═══════════════════════════════════════════════════════════════════════════════
# Live session
# ═══════════════════════════════════════════════════════════════════════════════
class SessionBody(BaseModel):
    clip: str = "worker-zone"
    site_id: str = "plant-01"
    frames: int = 30
    fps: float = 2.0
    start: str = "2026-08-06T02:10:00"
    clock_scale: float = 20.0
    zone: str = "substation"


@app.post("/api/session/stream")
async def session_stream(body: SessionBody):
    """Run a monitoring session and stream every event as it happens."""
    from kestrel.ingest.sources import VideoFileSource
    from kestrel.session import Session
    from kestrel.sim.sites import load_site
    from kestrel.sim.telemetry import PatrolSimulator

    site = load_site(body.site_id, settings.sites_dir)
    path = settings.footage_dir / f"{body.clip}.mp4"
    if not path.exists():
        raise HTTPException(404, f"no footage '{body.clip}'. Run scripts/fetch_footage.py")

    start = datetime.fromisoformat(body.start)
    tel = PatrolSimulator(site, start)
    z = site.zone_by_id(body.zone)
    if z is not None:
        tel.waypoints = [(z.centroid, body.zone, 7200.0)]

    src = VideoFileSource(path, site, start_clock=start, sample_fps=body.fps,
                          clock_scale=body.clock_scale,
                          max_frames=min(body.frames, 400), telemetry=tel)
    session = Session(site)

    async def gen():
        try:
            async for ev in session.stream(src):
                yield f"data: {json.dumps(ev.to_dict(), default=str)}\n\n"
            yield f"data: {json.dumps({'kind': 'complete', 'payload': session.summary()}, default=str)}\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            yield f"data: {json.dumps({'kind': 'error', 'payload': {'error': str(e)[:200]}})}\n\n"

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/scenarios")
def scenarios() -> dict[str, Any]:
    from kestrel.sim.scenarios import ALL

    return {
        "count": len(ALL),
        "scenarios": [
            {"id": s.id, "title": s.title, "description": s.description,
             "frames": len(s.frames), "tags": s.tags,
             "expect_alerts": s.expect_alerts, "expect_no_alerts": s.expect_no_alerts}
            for s in ALL
        ],
    }


@app.get("/api/architecture")
def architecture(topic: str = "overview") -> dict[str, Any]:
    from kestrel.agent.selfknowledge import ARCHITECTURE, LIMITATIONS

    return {
        "topic": topic,
        "explanation": ARCHITECTURE.get(topic, ARCHITECTURE["overview"]),
        "topics": sorted(ARCHITECTURE),
        "limitations": LIMITATIONS,
    }


@app.get("/api/evals")
def evals() -> dict[str, Any]:
    """Measured results, read from disk so the UI shows real numbers."""
    out: dict[str, Any] = {}
    eval_dir = Path("data/eval")
    for name in ("gate_efficiency", "scenarios", "leaderboard", "chaos", "retrieval"):
        p = eval_dir / f"{name}.json"
        if p.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                out[name] = json.loads(p.read_text(encoding="utf-8"))
    for name in ("probe_results", "probe_vlms", "probe_embeddings"):
        p = Path("data") / f"{name}.json"
        if p.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                out[name] = json.loads(p.read_text(encoding="utf-8"))
    return out
