"""The perception cascade, assembled.

    frame
      │
      ├─ tier 0  gate        CPU     should we spend anything on this at all?
      │            └── no ──▶ record the skip and stop. This is the common case.
      ├─ tier 1  detect      local   boxes + classes
      ├─ tier 1.5 track      local   persistent identity across frames
      ├─ tier 2  project     CPU     pixel → world → named zone
      ├─ tier 2b embed       cloud   appearance vectors for re-ID and search
      ├─ tier 3  perceive    cloud   structured scene graph
      └─ tier 4  deep        cloud   asynchronous re-look, off the critical path

Each stage is individually skippable and individually degradable, which is what
lets the whole thing keep running when a model is unavailable rather than failing
closed. The frame record always gets written; what varies is how much is in it.

The output is a ``PerceptionResult`` per frame — everything the memory, rules and
retrieval layers need, and nothing they have to reach back into the pipeline for.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from kestrel.clients.models import ModelClient, get_client
from kestrel.config import Settings, get_settings
from kestrel.domain import (
    BBox,
    Detection,
    Frame,
    GateVerdict,
    LatLon,
    SceneGraph,
    Site,
)
from kestrel.gate.gate import CostGate
from kestrel.ingest.sources import RawFrame, crop, to_jpeg_bytes
from kestrel.perception.detect import Detector, get_detector
from kestrel.perception.project import GroundProjector
from kestrel.perception.track import Tracker
from kestrel.perception.vlm import DeepEscalator, SceneRequest, describe_scene

# Colloquial ways a description refers to a zone. Needed because scripted frames
# are written the way a person would narrate them — "accelerates through the gate",
# not "enters zone main-gate" — and a zone that never resolves means every
# zone-scoped rule silently fails to match.
ZONE_ALIASES: dict[str, list[str]] = {
    "main-gate": ["the gate", "gate", "barrier", "entrance", "entry"],
    "loading-dock": ["dock", "bay", "loading bay", "garage", "apron"],
    "yard": ["storage yard", "compound"],
    "parking": ["car park", "staff parking", "parking bay"],
    "fence-line": ["fence", "perimeter", "perimeter fence", "fence line"],
    "substation": ["transformer", "electrical substation"],
    "restricted-core": ["restricted", "restricted area", "restricted core"],
    "access-road": ["access road", "road", "driveway"],
    "warehouse-a": ["warehouse a"],
    "warehouse-b": ["warehouse b"],
}


@dataclass
class PerceptionResult:
    """Everything learned about one frame."""

    frame: Frame
    gate: GateVerdict
    detections: list[Detection] = field(default_factory=list)
    scene: SceneGraph | None = None
    frame_embedding: list[float] | None = None
    crop_embeddings: dict[str, list[float]] = field(default_factory=dict)
    escalated: bool = False
    stage_ms: dict[str, float] = field(default_factory=dict)
    jpeg: bytes | None = None

    @property
    def analysed(self) -> bool:
        return self.gate.analyse

    @property
    def summary(self) -> str:
        if not self.analysed:
            return f"[skipped] {self.gate.reason}"
        if self.scene is not None:
            return self.scene.caption
        if self.detections:
            counts: dict[str, int] = {}
            for d in self.detections:
                counts[d.label] = counts.get(d.label, 0) + 1
            return ", ".join(f"{n}x {lab}" for lab, n in counts.items())
        return "nothing detected"


class PerceptionPipeline:
    """Runs the cascade for one site."""

    def __init__(
        self,
        site: Site,
        *,
        settings: Settings | None = None,
        client: ModelClient | None = None,
        detector: Detector | None = None,
        enable_vlm: bool = True,
        enable_embeddings: bool = True,
        enable_deep: bool = True,
        detect_phrases: list[str] | None = None,
        analysis_fps: int = 2,
    ) -> None:
        self.site = site
        self.s = settings or get_settings()
        self.client = client or get_client()
        self.detector = detector or get_detector(self.s)
        self.tracker = Tracker(frame_rate=analysis_fps)
        self.projector = GroundProjector(site)
        self.enable_vlm = enable_vlm
        self.enable_embeddings = enable_embeddings
        self.detect_phrases = detect_phrases

        # The gate's semantic tier reuses the same embedding model as the index, so
        # a frame that survives the gate has already paid for its vector.
        self.gate = CostGate(
            site,
            self.s,
            embed_fn=self._embed_frame if enable_embeddings else None,
        )

        self.escalator = DeepEscalator() if enable_deep else None
        self._deep_results: dict[str, SceneGraph] = {}
        if self.escalator is not None:
            self.escalator.on_result = self._on_deep

        self.frames_seen = 0
        self.frames_analysed = 0
        self._det_seq = 0

    # ── helpers ──────────────────────────────────────────────────────────
    async def _embed_frame(self, image: np.ndarray) -> list[float]:
        return await self.client.embed_image(to_jpeg_bytes(image, quality=80))

    def _on_deep(self, frame_id: str, graph: SceneGraph) -> None:
        """Tier-4 result landing after the fact. Consumers poll ``deep_results``."""
        self._deep_results[frame_id] = graph

    @property
    def deep_results(self) -> dict[str, SceneGraph]:
        return self._deep_results

    def _should_escalate(self, scene: SceneGraph | None, gate: GateVerdict,
                         dets: list[Detection]) -> bool:
        """Escalate only when a deeper look could plausibly change the answer.

        Escalating everything would be pointless spend; escalating nothing would
        waste the capability. The triggers are: the fast tier was unsure, the scene
        flagged something anomalous, the frame was highly novel, or a person is
        present in a high-priority zone — the case where being wrong is expensive.
        """
        if scene is None:
            return False
        if scene.confidence < 0.45:
            return True
        if scene.anomalies:
            return True
        if gate.novelty > 0.75:
            return True
        for d in dets:
            if "person" in d.label and d.zone_id:
                z = self.site.zone_by_id(d.zone_id)
                if z is not None and z.priority >= 2.0:
                    return True
        return False

    # ── main entry ───────────────────────────────────────────────────────
    async def process(self, raw: RawFrame) -> PerceptionResult:
        frame = raw.frame
        image = raw.image
        self.frames_seen += 1
        timings: dict[str, float] = {}

        # ── tier 0 ───────────────────────────────────────────────────────
        t = time.perf_counter()
        verdict = await self.gate.decide(
            image=image,
            phash=frame.phash,
            ts=frame.ts,
            telemetry=frame.telemetry,
            is_text_frame=image is None and frame.text is not None,
        )
        timings["gate"] = (time.perf_counter() - t) * 1000

        if not verdict.analyse:
            return PerceptionResult(frame=frame, gate=verdict, stage_ms=timings)

        self.frames_analysed += 1

        # ── scripted frames bypass vision entirely ───────────────────────
        # They carry a description instead of pixels, so the cascade's vision tiers
        # have nothing to operate on. Everything downstream must still receive the
        # same shapes, which means synthesising detections from the description —
        # without them the rule engine has no observations and can never fire.
        if image is None:
            result = PerceptionResult(frame=frame, gate=verdict, stage_ms=timings)
            if frame.text:
                result.scene = await self._scene_from_text(frame)
                result.detections = self._detections_from_text(frame, result.scene)
                if self.enable_embeddings and result.scene is not None:
                    with contextlib.suppress(Exception):
                        result.frame_embedding = await self.client.embed_text(
                            result.scene.caption, kind="passage"
                        )
            return result

        # ── tier 1 + 1.5 ─────────────────────────────────────────────────
        t = time.perf_counter()
        raw_dets = self.detector.detect(image, self.detect_phrases)
        tracked = self.tracker.update(raw_dets)
        timings["detect_track"] = (time.perf_counter() - t) * 1000

        # ── tier 2: geo-projection ───────────────────────────────────────
        t = time.perf_counter()
        detections: list[Detection] = []
        for td in tracked:
            world: LatLon | None = None
            zone_id: str | None = None
            attrs: dict[str, str] = {}
            if frame.telemetry is not None and frame.width and frame.height:
                proj = self.projector.project(td.bbox, frame.telemetry, frame.width, frame.height)
                world, zone_id = proj.world, proj.zone_id
                attrs["projection_confidence"] = f"{proj.confidence:.3f}"
                if proj.reason:
                    attrs["projection_note"] = proj.reason
            self._det_seq += 1
            detections.append(
                Detection(
                    id=f"det_{frame.id}_{self._det_seq:05d}",
                    frame_id=frame.id,
                    label=td.label,
                    confidence=td.confidence,
                    bbox=td.bbox,
                    track_id=td.track_id,
                    zone_id=zone_id,
                    world=world,
                    attributes=attrs,
                )
            )
        timings["project"] = (time.perf_counter() - t) * 1000

        result = PerceptionResult(
            frame=frame, gate=verdict, detections=detections, stage_ms=timings
        )
        result.jpeg = to_jpeg_bytes(image, quality=86)

        # ── tier 2b: embeddings ──────────────────────────────────────────
        if self.enable_embeddings:
            t = time.perf_counter()
            with contextlib.suppress(Exception):
                result.frame_embedding = await self._embed_frame(image)
            # Crops only for things that can be re-identified later. Embedding a
            # traffic cone costs the same as embedding a vehicle and is worth less.
            for d in detections[:6]:
                if not any(k in d.label for k in ("person", "car", "truck", "van", "bus", "motorcycle", "bicycle")):
                    continue
                try:
                    patch = crop(image, d.bbox.as_tuple())
                    if patch.size == 0:
                        continue
                    vec = await self.client.embed_image(to_jpeg_bytes(patch, quality=82))
                    result.crop_embeddings[d.id] = vec
                    d.embedding_id = d.id
                except Exception:
                    continue
            timings["embed"] = (time.perf_counter() - t) * 1000

        # ── tier 3: scene graph ──────────────────────────────────────────
        if self.enable_vlm:
            t = time.perf_counter()
            zone_hint = None
            if frame.telemetry is not None:
                z = self.site.zone_at(frame.telemetry.position)
                zone_hint = z.name if z else None
            req = SceneRequest(
                image=image,
                detections=tracked,
                telemetry=frame.telemetry,
                zone_hint=zone_hint,
                site_name=self.site.name,
            )
            result.scene = await describe_scene(req)
            timings["perceive"] = (time.perf_counter() - t) * 1000

            # ── tier 4: async escalation ─────────────────────────────────
            if self.escalator is not None and self._should_escalate(result.scene, verdict, detections):
                result.escalated = self.escalator.submit(req, frame.id)

        return result

    def _detections_from_text(self, frame: Frame, scene: SceneGraph | None) -> list[Detection]:
        """Turn a scripted description into detections the rest of the stack accepts.

        The scripted source is the assignment's literal specification — "simulate
        video frames with text descriptions" — and it has to exercise the same
        downstream path as real video, or it proves nothing about the system. There
        are no pixels, so:

        *   objects come from the scene graph the description was parsed into,
        *   the zone is read from the description itself where it names one
            ("at the main gate"), falling back to whatever the drone is over,
        *   boxes are nominal placeholders, and confidence is inherited from the
            scene graph rather than invented.

        Track ids are stable per (label, zone) so that a subject described across
        consecutive frames resolves to one entity and can accumulate dwell.
        """
        if scene is None or not scene.objects:
            return []

        text = (frame.text or "").lower()
        # Prefer a zone the description actually names. Longest match wins so
        # "main gate" is not shadowed by the bare alias "gate", and aliases are
        # needed because people write "through the gate", not "at the main gate".
        named: str | None = None
        best_len = 0
        for z in self.site.zones:
            tokens = [z.name.lower(), z.id.replace("-", " ")]
            tokens += ZONE_ALIASES.get(z.id, [])
            for token in tokens:
                if token in text and len(token) > best_len:
                    named, best_len = z.id, len(token)

        fallback = None
        if frame.telemetry is not None:
            z = self.site.zone_at(frame.telemetry.position)
            fallback = z.id if z else None
        zone_id = named or fallback

        out: list[Detection] = []
        for i, obj in enumerate(scene.objects):
            for n in range(max(1, min(obj.count, 8))):
                self._det_seq += 1
                # Deterministic pseudo-track: same label in the same zone across
                # frames is the same subject, which is what dwell requires.
                track = abs(hash((obj.label.lower(), zone_id or "", n))) % 100_000
                out.append(
                    Detection(
                        id=f"det_{frame.id}_{self._det_seq:05d}",
                        frame_id=frame.id,
                        label=obj.label.lower(),
                        confidence=max(0.4, obj.confidence),
                        # Nominal box: there are no pixels, and a fabricated
                        # bounding box would imply a localisation we do not have.
                        bbox=BBox(x1=0.0, y1=0.0, x2=1.0, y2=1.0),
                        track_id=track,
                        zone_id=zone_id,
                        attributes={
                            k: v for k, v in (
                                ("colour", obj.colour or ""),
                                ("kind", obj.kind or ""),
                                ("activity", obj.activity or ""),
                            ) if v
                        } | {"source": "scripted"},
                    )
                )
                _ = i
        return out

    async def _scene_from_text(self, frame: Frame) -> SceneGraph:
        """Turn a scripted description into the same structured form as a VLM would.

        This is what keeps the scripted source a first-class citizen: the rules,
        memory and retrieval layers receive an identical ``SceneGraph`` regardless
        of whether the frame had pixels.
        """
        from kestrel.obs.meter import Stage
        from kestrel.perception.vlm import SCENE_SCHEMA

        try:
            payload = await self.client.chat_json(
                [
                    {
                        "role": "system",
                        "content": (
                            "You convert a security camera frame description into structured JSON. "
                            "Extract only what the description states. Do not invent detail."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Site: {self.site.name}\n"
                            f"Time: {frame.ts:%Y-%m-%d %H:%M:%S}\n"
                            f"Frame description: {frame.text}\n\n"
                            "Return JSON with keys: caption, objects[{label,colour,kind,activity,count,confidence}], "
                            "activities[], lighting, weather, visibility, anomalies[], confidence."
                        ),
                    },
                ],
                SCENE_SCHEMA,
                stage=Stage.PERCEIVE,
                max_tokens=600,
            )
        except Exception:
            return SceneGraph(caption=frame.text or "", confidence=0.4)

        import json as _json

        from kestrel.perception.vlm import _parse

        graph = _parse(_json.dumps(payload), deep=False)
        return graph or SceneGraph(caption=frame.text or "", confidence=0.4)

    # ── read-out ─────────────────────────────────────────────────────────
    @property
    def stats(self) -> dict[str, Any]:
        return {
            "frames_seen": self.frames_seen,
            "frames_analysed": self.frames_analysed,
            "frames_skipped": self.frames_seen - self.frames_analysed,
            "gate_efficiency": round(self.gate.efficiency, 4),
            "gate": self.gate.summary(),
            "detector": self.detector.info,
            "tracker": self.tracker.info,
            "escalation": self.escalator.stats if self.escalator else None,
        }

    def reset(self) -> None:
        self.gate.reset()
        self.tracker.reset()
        self.frames_seen = 0
        self.frames_analysed = 0
