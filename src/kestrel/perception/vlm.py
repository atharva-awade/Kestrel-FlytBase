"""Tier 3/4 — semantic perception.

The detector answers *where* and *what class*. It cannot answer "a blue Ford F-150
reversing into bay 3", and the assignment's own example output is exactly that kind
of sentence. This module gets it, as **structured data** rather than prose.

Three design decisions worth stating:

*   **Structured, not free text.** A caption is unqueryable. A ``SceneGraph`` —
    objects with colour, kind and activity, plus lighting and anomalies — can be
    indexed, filtered, and fed to rules. The caption is a *field* of that object,
    generated for humans, not the output itself.
*   **Detector context is injected into the prompt.** The VLM is told what the
    local detector already found. This measurably reduces hallucinated objects and
    anchors counts, because the model is asked to *describe and enrich* known
    detections rather than to inventory the scene from scratch.
*   **Validation is never skipped.** Constrained decoding is requested but the
    probe found provider support inconclusive, and models under-fill schemas
    routinely. Every response is parsed leniently, validated against Pydantic, and
    repaired once. If it still fails we degrade to a caption-only scene graph
    rather than dropping the frame — a partial observation beats none.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

import numpy as np
from pydantic import ValidationError

from kestrel.domain import SceneGraph, Telemetry
from kestrel.ingest.sources import to_jpeg_bytes
from kestrel.obs.meter import METER, Call, Stage
from kestrel.perception.track import TrackedDetection

# Placeholder values models return when they mean "nothing here". Treated as
# absence rather than content — an anomaly list containing "none" would otherwise
# raise alerts about nothing at all.
_NULLISH = {
    "", "none", "null", "n/a", "na", "unknown", "nothing", "no anomalies",
    "not applicable", "not specified", "no", "-", "undefined", "nil",
}

SCENE_SCHEMA = {
    "type": "object",
    "properties": {
        "caption": {"type": "string"},
        "objects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "colour": {"type": "string"},
                    "kind": {"type": "string"},
                    "activity": {"type": "string"},
                    "count": {"type": "integer"},
                    "confidence": {"type": "number"},
                },
                "required": ["label"],
            },
        },
        "activities": {"type": "array", "items": {"type": "string"}},
        "lighting": {
            "type": "string",
            "enum": ["daylight", "dusk", "night", "artificial", "unknown"],
        },
        "weather": {"type": "string"},
        "visibility": {"type": "string", "enum": ["clear", "reduced", "poor", "unknown"]},
        "anomalies": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
    "required": ["caption", "objects"],
}


SYSTEM = """You are the perception module of an autonomous drone security analyst \
monitoring an industrial site. You describe what a camera sees, precisely and without \
embellishment.

You will be given a frame, the site context, and the objects a detector has already \
localised. Your job is to ENRICH those detections with the attributes a detector \
cannot produce — colour, make, sub-type, and what each thing is doing — and to note \
anything genuinely unusual.

Return ONLY a JSON object with this shape, no prose, no code fence:
{
  "caption": "<one factual sentence a security operator would find useful>",
  "objects": [
    {"label":"<person|truck|car|...>", "colour":"<colour or null>",
     "kind":"<specific type, e.g. 'pickup truck', 'high-visibility worker'>",
     "activity":"<what it is doing>", "count":<int>, "confidence":<0.0-1.0>}
  ],
  "activities": ["<scene-level activities>"],
  "lighting": "daylight|dusk|night|artificial|unknown",
  "weather": "<or null>",
  "visibility": "clear|reduced|poor|unknown",
  "anomalies": ["<anything out of place; empty list if nothing is>"],
  "confidence": <0.0-1.0 overall>
}

Rules that matter:
- Describe ONLY what is visible. Never infer intent you cannot see.
- If the detector found N people and you can see N people, say N. Do not inflate.
- "anomalies" is for genuinely unusual things, not routine activity. An empty list \
is the correct answer most of the time.
- Prefer "unknown" over a confident guess. Downstream rules weight your confidence."""


@dataclass(slots=True)
class SceneRequest:
    image: np.ndarray
    detections: list[TrackedDetection]
    telemetry: Telemetry | None
    zone_hint: str | None = None
    site_name: str | None = None


def _context_block(req: SceneRequest) -> str:
    lines: list[str] = []
    if req.site_name:
        lines.append(f"Site: {req.site_name}")
    if req.zone_hint:
        lines.append(f"Camera is over zone: {req.zone_hint}")
    if req.telemetry is not None:
        t = req.telemetry
        lines.append(
            f"Time: {t.ts:%Y-%m-%d %H:%M:%S} | altitude {t.alt_m:.0f} m | "
            f"gimbal {t.gimbal_pitch_deg:.0f}° | illuminance {t.illuminance_lux:.0f} lux "
            f"({'night' if t.is_night else 'day'})"
        )
        if t.perception_confidence < 0.55:
            lines.append(
                f"NOTE: optical conditions are poor (confidence {t.perception_confidence:.2f}). "
                "Be conservative and prefer 'unknown' over guessing."
            )
    if req.detections:
        counts: dict[str, int] = {}
        for d in req.detections:
            counts[d.label] = counts.get(d.label, 0) + 1
        found = ", ".join(f"{n}x {lab}" for lab, n in sorted(counts.items()))
        lines.append(f"Detector localised: {found}")
        lines.append(
            "Enrich these with colour/type/activity. If you can see something the "
            "detector missed, add it. If you cannot see something it reported, omit it."
        )
    else:
        lines.append("Detector localised nothing. Report the scene as you find it.")
    return "\n".join(lines)


def _downscale_for_vlm(image: np.ndarray, max_width: int) -> np.ndarray:
    """Shrink a frame before it reaches a VLM.

    The detector works on full resolution because box precision depends on it. The
    semantic tier does not: scene-level description is unchanged at 640px, while
    prompt tokens and round-trip time fall sharply. Measured: 960px cost ~6.4k
    prompt tokens and ~6s; 640px is materially faster for the same captions.
    """
    import cv2

    if image.shape[1] <= max_width:
        return image
    scale = max_width / image.shape[1]
    return cv2.resize(
        image, (max_width, int(image.shape[0] * scale)), interpolation=cv2.INTER_AREA
    )


async def describe_scene(
    req: SceneRequest,
    *,
    deep: bool = False,
    max_tokens: int | None = None,
) -> SceneGraph:
    """Produce a validated ``SceneGraph`` for one frame."""
    from kestrel.clients.models import get_client
    from kestrel.config import get_settings

    s = get_settings()
    client = get_client()
    max_tokens = max_tokens or s.vlm_max_tokens
    prompt = f"{SYSTEM}\n\n--- CONTEXT ---\n{_context_block(req)}\n\nAnalyse the frame."
    jpeg = to_jpeg_bytes(_downscale_for_vlm(req.image, s.vlm_max_width), quality=84)

    try:
        raw = await client.describe(jpeg, prompt, deep=deep, max_tokens=max_tokens)
    except Exception as e:
        METER.record(
            Call(
                Stage.PERCEIVE_DEEP if deep else Stage.PERCEIVE,
                "scene-graph",
                0.0,
                ok=False,
                error=f"{type(e).__name__}",
            )
        )
        return _degraded(f"perception unavailable: {type(e).__name__}", deep)

    graph = _parse(raw, deep)
    if graph is not None:
        return graph

    # One repair attempt with the malformed output handed back.
    try:
        raw2 = await client.chat(
            [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": raw[:1500]},
                {
                    "role": "user",
                    "content": (
                        "That was not valid JSON for the required schema. Return ONLY "
                        "the JSON object — no prose, no code fence."
                    ),
                },
            ],
            stage=Stage.PERCEIVE,
            max_tokens=max_tokens,
        )
        graph = _parse(raw2, deep)
        if graph is not None:
            return graph
    except Exception:
        pass

    # Last resort: keep the prose as a caption. A partial observation is worth more
    # than a dropped frame, and the low confidence marks it as such.
    text = (raw or "").strip().replace("\n", " ")[:300]
    return _degraded(text or "unparseable perception response", deep, confidence=0.25)


def _parse(raw: str, deep: bool) -> SceneGraph | None:
    from kestrel.clients.models import _loads_lenient

    payload = _loads_lenient(raw)
    if not payload:
        return None

    # Models emit null for optional strings and occasionally a bare string where a
    # list belongs. Normalise before validation rather than rejecting good data.
    payload.setdefault("caption", "")
    objs = payload.get("objects")
    if isinstance(objs, dict):
        objs = [objs]
    if not isinstance(objs, list):
        objs = []

    cleaned: list[dict] = []
    for o in objs:
        if not isinstance(o, dict):
            continue
        label = str(o.get("label") or "").strip().lower()
        # Models emit placeholder labels when they have nothing to report. An
        # object called "unknown" is the absence of an object, and letting it
        # through would pollute every downstream count and query.
        if not label or label in _NULLISH:
            continue
        cleaned.append(
            {
                "label": label,
                "colour": _s(o.get("colour")),
                "kind": _s(o.get("kind")),
                "activity": _s(o.get("activity")),
                "count": _i(o.get("count"), 1),
                "confidence": _f(o.get("confidence"), 0.5),
            }
        )
    payload["objects"] = cleaned

    for key in ("activities", "anomalies"):
        v = payload.get(key)
        items = v if isinstance(v, list) else ([v] if v else [])
        # Same problem: models answer "no anomalies" by putting the string "none"
        # in the list rather than returning an empty one. An anomaly list that
        # contains "none" would raise alerts about nothing.
        payload[key] = [
            str(x).strip()
            for x in items
            if x is not None and str(x).strip().lower() not in _NULLISH
        ]

    if payload.get("lighting") not in {"daylight", "dusk", "night", "artificial", "unknown"}:
        payload["lighting"] = "unknown"
    if payload.get("visibility") not in {"clear", "reduced", "poor", "unknown"}:
        payload["visibility"] = "unknown"
    payload["weather"] = _s(payload.get("weather"))
    payload["confidence"] = _f(payload.get("confidence"), 0.5)
    payload["tier"] = "deep" if deep else "fast"

    try:
        return SceneGraph.model_validate(payload)
    except ValidationError:
        return None


def _degraded(caption: str, deep: bool, confidence: float = 0.1) -> SceneGraph:
    return SceneGraph(
        caption=caption,
        objects=[],
        confidence=confidence,
        tier="deep" if deep else "fast",
    )


def _s(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in ("", "null", "none", "n/a", "unknown") else s


def _i(v, default: int) -> int:
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return default


def _f(v, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return default


# ═══════════════════════════════════════════════════════════════════════════════
class DeepEscalator:
    """Runs tier-4 re-looks off the critical path.

    The deep VLM was measured at 57-84 s (ADR 0001). Awaiting that inside frame
    processing would stall the pipeline for a minute per escalation, so instead the
    frame is written at tier-3 confidence immediately and a background task upgrades
    it when the deeper answer arrives.

    A bounded queue is deliberate: if escalations are being requested faster than
    they can be served, the right behaviour is to drop the least urgent and say so,
    not to grow an unbounded backlog that never drains.
    """

    def __init__(self, max_concurrent: int = 2, max_queued: int = 32) -> None:
        self._sem = asyncio.Semaphore(max_concurrent)
        self._queue: asyncio.Queue[tuple[SceneRequest, str]] = asyncio.Queue(max_queued)
        self._tasks: set[asyncio.Task] = set()
        self.completed = 0
        self.dropped = 0
        self.on_result = None  # set by the pipeline: (frame_id, SceneGraph) -> None

    def submit(self, req: SceneRequest, frame_id: str) -> bool:
        """Queue a deep re-look. Returns False if the backlog is full."""
        try:
            self._queue.put_nowait((req, frame_id))
        except asyncio.QueueFull:
            self.dropped += 1
            return False
        task = asyncio.create_task(self._drain())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _drain(self) -> None:
        try:
            req, frame_id = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        async with self._sem:
            graph = await describe_scene(req, deep=True)
            self.completed += 1
            if self.on_result is not None:
                with contextlib.suppress(Exception):
                    self.on_result(frame_id, graph)

    async def drain(self, timeout: float = 300.0) -> None:
        """Wait for outstanding escalations — used by batch runs and tests."""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)

    @property
    def stats(self) -> dict:
        return {
            "completed": self.completed,
            "dropped": self.dropped,
            "in_flight": len(self._tasks),
            "queued": self._queue.qsize(),
        }
