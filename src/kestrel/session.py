"""The session runner — where every layer is wired together.

One class owns the full path from a frame arriving to an alert being raised:

    ingest → perception cascade → geo-projection → entity resolution
           → baseline → rules → triage → narrative → mission proposal
           → storage + ledger

Everything below it is independently testable; this is the part that has to be
correct about *ordering*. Two orderings matter and are easy to get wrong:

*   **Entity resolution must precede rule evaluation.** Dwell is a property of an
    entity, so a rule evaluated before identity is assigned has nothing to
    accumulate against and loitering can never fire.
*   **Baseline observation must follow rule evaluation for the same frame.**
    Otherwise the frame being judged is already folded into the "normal" it is
    being judged against, and nothing is ever anomalous.

Events are emitted as they happen so the console can stream a live session rather
than poll for a finished one.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from kestrel.actions.missions import MissionRecommender
from kestrel.clients.models import ModelClient, get_client
from kestrel.config import Settings, get_settings
from kestrel.domain import Alert, Mission, Site
from kestrel.ingest.sources import RawFrame
from kestrel.memory.baseline import BaselineModel
from kestrel.memory.entities import EntityResolver, attributes_from_scene
from kestrel.memory.pyramid import FrameNote, MemoryPyramid, events_from_notes
from kestrel.obs.meter import METER
from kestrel.perception.pipeline import PerceptionPipeline, PerceptionResult
from kestrel.rules.dsl import Rule
from kestrel.rules.engine import Observation, RuleEngine
from kestrel.rules.pack import default_rules
from kestrel.rules.triage import NarrativeBuilder, ThreatNarrative, Triage
from kestrel.storage.db import Database, get_db
from kestrel.storage.ledger import Ledger, LedgerKind


@dataclass
class SessionEvent:
    """One thing that happened, streamed to the console as it happens."""

    kind: str          # frame | alert | narrative | mission | entity | stats
    ts: datetime
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "ts": self.ts.isoformat(), "payload": self.payload}


@dataclass
class SessionStats:
    frames_seen: int = 0
    frames_analysed: int = 0
    detections: int = 0
    entities: int = 0
    alerts_raised: int = 0
    alerts_suppressed: int = 0
    missions_proposed: int = 0
    narratives: int = 0
    started: datetime | None = None
    finished: datetime | None = None
    errors: list[str] = field(default_factory=list)


class Session:
    """A single monitoring run over one site."""

    def __init__(
        self,
        site: Site,
        *,
        settings: Settings | None = None,
        db: Database | None = None,
        client: ModelClient | None = None,
        rules: list[Rule] | None = None,
        enable_vlm: bool = True,
        enable_embeddings: bool = True,
        enable_missions: bool = True,
        save_frames: bool = True,
        on_event: Callable[[SessionEvent], None] | None = None,
    ) -> None:
        self.site = site
        self.s = settings or get_settings()
        self.db = db or get_db()
        self.client = client or get_client()
        self.ledger = Ledger(self.db)

        self.pipeline = PerceptionPipeline(
            site, settings=self.s, client=self.client,
            enable_vlm=enable_vlm, enable_embeddings=enable_embeddings,
        )
        self.resolver = EntityResolver(site.id)
        self.baseline = BaselineModel(site.id, db=self.db)
        self.rules = rules or default_rules()
        self.engine = RuleEngine(site, self.rules, baseline=self.baseline)
        self.triage = Triage(site)
        self.narrator = NarrativeBuilder(site, client=self.client)
        self.recommender = MissionRecommender(site) if enable_missions else None
        self.pyramid = MemoryPyramid(
            site.id,
            client=self.client,
            high_priority_zones={z.id for z in site.zones if z.priority >= 1.8},
        )

        self.save_frames = save_frames
        self.on_event = on_event
        self.stats = SessionStats()
        self.alerts: list[Alert] = []
        self.missions: list[Mission] = []
        self.narratives: list[ThreatNarrative] = []
        self._notes: list[FrameNote] = []
        self._queue: asyncio.Queue[SessionEvent] | None = None

    # ── events ───────────────────────────────────────────────────────────
    def _emit(self, kind: str, payload: dict[str, Any], ts: datetime | None = None) -> None:
        ev = SessionEvent(kind, ts or datetime.now(), payload)
        if self.on_event is not None:
            with contextlib.suppress(Exception):
                self.on_event(ev)
        if self._queue is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(ev)

    # ── the main path ────────────────────────────────────────────────────
    async def process(self, raw: RawFrame) -> PerceptionResult:
        """Run one frame through every layer, in the order that matters."""
        frame = raw.frame
        self.stats.frames_seen += 1

        result = await self.pipeline.process(raw)

        if frame.telemetry is not None:
            self.db.add_telemetry(frame.telemetry)

        # A skipped frame is still recorded — the skip rate is the scalability
        # claim, and a claim that cannot be audited is not evidence.
        if not result.analysed:
            self.db.add_frame(frame, result.gate, None)
            self._emit("frame", {
                "frame_id": frame.id, "ts": frame.ts.isoformat(), "analysed": False,
                "gate_reason": result.gate.reason,
            }, frame.ts)
            return result

        self.stats.frames_analysed += 1

        if self.save_frames and result.jpeg is not None:
            path = Path(self.s.frame_dir) / self.site.id / f"{frame.id}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(result.jpeg)
            frame.path = str(path.relative_to(Path(self.s.frame_dir).parent.parent))

        self.db.add_frame(frame, result.gate, result.scene)

        # ── entity resolution, BEFORE rules ──────────────────────────────
        pconf = (
            frame.telemetry.perception_confidence if frame.telemetry is not None else 1.0
        )
        observations: list[Observation] = []
        for det in result.detections:
            attrs = attributes_from_scene(result.scene, det.label)
            emb = result.crop_embeddings.get(det.id)
            entity = self.resolver.resolve(det, frame.ts, embedding=emb, attributes=attrs)
            try:
                proj_conf = float(det.attributes.get("projection_confidence", 0.0))
            except (TypeError, ValueError):
                proj_conf = 0.0
            observations.append(
                Observation(
                    ts=frame.ts, frame_id=frame.id, entity_id=entity.id,
                    label=det.label, confidence=det.confidence, zone_id=det.zone_id,
                    attributes={**attrs, **det.attributes}, detection_id=det.id,
                    perception_confidence=pconf,
                    # Carried through so the alert can be dispatched to, not just read.
                    world=det.world,
                    projection_confidence=proj_conf,
                )
            )
            self.db.upsert_entity(entity)

        self.stats.detections += len(result.detections)
        self.stats.entities = self.resolver.stats["entities"]
        if result.detections:
            self.db.add_detections(result.detections, self.site.id, frame.ts)

        # ── embeddings into the index ────────────────────────────────────
        if result.frame_embedding:
            self.db.add_embedding(
                f"emb_frame_{frame.id}", self.site.id, "frame", frame.id,
                frame.ts, result.frame_embedding,
            )
        for det_id, vec in result.crop_embeddings.items():
            self.db.add_embedding(
                f"emb_crop_{det_id}", self.site.id, "crop", det_id, frame.ts, vec
            )
        if result.scene and result.scene.caption:
            with contextlib.suppress(Exception):
                cap_vec = await self.client.embed_text(
                    result.scene.caption, kind="passage", joint=False
                )
                self.db.add_embedding(
                    f"emb_cap_{frame.id}", self.site.id, "caption", frame.id,
                    frame.ts, cap_vec,
                )

        # ── rules, THEN baseline ─────────────────────────────────────────
        raised: list[Alert] = []
        for obs in observations:
            for res in self.engine.evaluate(obs, scene=result.scene, telemetry=frame.telemetry):
                if not res.fired:
                    continue
                alert = self.engine.to_alert(
                    res, scene=result.scene, telemetry=frame.telemetry
                )
                decision = self.triage.assess(
                    alert, scene_caption=result.scene.caption if result.scene else ""
                )
                if decision.keep:
                    raised.append(alert)
                    self.alerts.append(alert)
                    self.stats.alerts_raised += 1
                    self.ledger.append(
                        LedgerKind.ALERT_RAISED,
                        {
                            "rule_id": alert.rule_id, "severity": alert.severity.value,
                            "title": alert.title, "confidence": alert.confidence,
                            "zone": alert.zone_id, "frame_id": frame.id,
                        },
                        site_id=self.site.id, ref_id=alert.id,
                    )
                else:
                    self.stats.alerts_suppressed += 1
                self.db.add_alert(alert)

        # Observed only now: folding this frame into the baseline before judging it
        # would make it part of the normal it is measured against.
        for obs in observations:
            self.baseline.observe(obs.zone_id, obs.ts, obs.label)

        # ── memory ───────────────────────────────────────────────────────
        note = FrameNote(
            frame_id=frame.id, ts=frame.ts,
            caption=result.scene.caption if result.scene else result.summary,
            labels=[d.label for d in result.detections],
            zones=[d.zone_id for d in result.detections if d.zone_id],
            entity_ids=[o.entity_id for o in observations if o.entity_id],
        )
        self.pyramid.add_frame(note)
        self._notes.append(note)

        self._emit("frame", {
            "frame_id": frame.id, "ts": frame.ts.isoformat(), "analysed": True,
            "gate_reason": result.gate.reason,
            "caption": result.scene.caption if result.scene else "",
            "detections": [
                {
                    "id": d.id, "label": d.label, "confidence": round(d.confidence, 3),
                    "bbox": d.bbox.as_tuple(), "track_id": d.track_id,
                    "entity_id": d.entity_id, "zone_id": d.zone_id,
                    "lat": d.world.lat if d.world else None,
                    "lon": d.world.lon if d.world else None,
                }
                for d in result.detections
            ],
            "telemetry": frame.telemetry.model_dump(mode="json") if frame.telemetry else None,
            "escalated": result.escalated,
            "path": frame.path,
        }, frame.ts)

        # ── alerts, narrative, missions ──────────────────────────────────
        for alert in raised:
            self._emit("alert", alert.model_dump(mode="json"), frame.ts)
            if self.recommender is not None:
                mission = self.recommender.propose(alert, frame.telemetry)
                self.missions.append(mission)
                self.stats.missions_proposed += 1
                alert.mission_id = mission.id
                self.db.add_mission(mission)
                self.db.add_alert(alert)
                self.ledger.append(
                    LedgerKind.MISSION_PROPOSED,
                    {
                        "alert_id": alert.id, "steps": len(mission.steps),
                        "feasible": mission.feasibility.feasible,
                        "blockers": mission.feasibility.blockers,
                    },
                    site_id=self.site.id, ref_id=mission.id,
                )
                self._emit("mission", mission.model_dump(mode="json"), frame.ts)

        if raised:
            groups = self.narrator.group(self.alerts[-12:])
            latest = groups[-1] if groups else []
            if len(latest) >= 2:
                narrative = await self.narrator.build(latest)
                if narrative is not None:
                    self.narratives.append(narrative)
                    self.stats.narratives += 1
                    self._emit("narrative", narrative.to_dict(), frame.ts)

        self.db.commit()
        return result

    # ── running ──────────────────────────────────────────────────────────
    async def run(self, source, *, limit: int | None = None) -> SessionStats:
        self.stats.started = datetime.now()
        self.ledger.append(
            LedgerKind.SESSION_START,
            {"site": self.site.id, "mode": self.s.effective_mode.value},
            site_id=self.site.id,
        )
        try:
            for i, raw in enumerate(source):
                if limit is not None and i >= limit:
                    break
                try:
                    await self.process(raw)
                except Exception as e:
                    # One bad frame must not end a shift.
                    self.stats.errors.append(f"frame {raw.frame.seq}: {type(e).__name__}: {e}"[:200])
        finally:
            await self.finalise()
        return self.stats

    async def stream(self, source, *, limit: int | None = None) -> AsyncIterator[SessionEvent]:
        """Run and yield events as they occur, for the live console."""
        self._queue = asyncio.Queue(maxsize=2000)
        task = asyncio.create_task(self.run(source, limit=limit))
        try:
            while not task.done() or not self._queue.empty():
                try:
                    yield await asyncio.wait_for(self._queue.get(), timeout=0.4)
                except TimeoutError:
                    continue
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._queue = None

    async def finalise(self) -> None:
        """Wait for background work, build memory, and persist the results."""
        if self.pipeline.escalator is not None:
            await self.pipeline.escalator.drain(timeout=180)
            for frame_id, graph in self.pipeline.deep_results.items():
                self.db.conn.execute(
                    "UPDATE frames SET caption = ?, scene_json = ? WHERE id = ?",
                    (graph.caption, graph.model_dump_json(), frame_id),
                )
                self.ledger.append(
                    LedgerKind.ESCALATION,
                    {"frame_id": frame_id, "tier": graph.tier, "caption": graph.caption[:200]},
                    site_id=self.site.id, ref_id=frame_id,
                )

        await self.pyramid.build()
        for nodes in self.pyramid.nodes.values():
            for n in nodes:
                self.db.add_memory_node(n)
        for ev in events_from_notes(self.site.id, self._notes):
            self.db.add_event(ev)

        self.stats.finished = datetime.now()
        self.ledger.append(
            LedgerKind.SESSION_END,
            {
                "frames": self.stats.frames_seen,
                "analysed": self.stats.frames_analysed,
                "alerts": self.stats.alerts_raised,
                "suppressed": self.stats.alerts_suppressed,
            },
            site_id=self.site.id,
        )
        self.db.commit()
        self._emit("stats", self.summary())

    # ── read-out ─────────────────────────────────────────────────────────
    def entity_vectors(self) -> dict[str, tuple[str, str, str, np.ndarray, datetime]]:
        """Entity vectors in the shape the fleet correlator expects."""
        out: dict[str, tuple[str, str, str, np.ndarray, datetime]] = {}
        for e in self.resolver.entities:
            vecs = self.resolver.embeddings_for(e.id)
            if not vecs:
                continue
            out[e.id] = (
                self.site.id, e.descriptor or e.label, e.kind.value,
                np.mean(np.vstack(vecs), axis=0), e.last_seen,
            )
        return out

    def summary(self) -> dict[str, Any]:
        elapsed = (
            (self.stats.finished - self.stats.started).total_seconds()
            if self.stats.started and self.stats.finished else 0.0
        )
        return {
            "site": {"id": self.site.id, "name": self.site.name},
            "frames": {
                "seen": self.stats.frames_seen,
                "analysed": self.stats.frames_analysed,
                "skipped": self.stats.frames_seen - self.stats.frames_analysed,
                "gate_efficiency": round(self.pipeline.gate.efficiency, 4),
            },
            "detections": self.stats.detections,
            "entities": self.resolver.stats,
            "alerts": {
                "raised": self.stats.alerts_raised,
                "suppressed": self.stats.alerts_suppressed,
                "triage": self.triage.stats,
            },
            "missions": {
                "proposed": self.stats.missions_proposed,
                "feasible": sum(1 for m in self.missions if m.feasibility.feasible),
            },
            "narratives": self.stats.narratives,
            "memory": self.pyramid.stats,
            "baseline": self.baseline.stats,
            "rules": self.engine.stats,
            "perception": self.pipeline.stats,
            "ledger": self.ledger.stats,
            "storage": self.db.stats,
            "meter": METER.snapshot(observed_seconds=elapsed),
            "errors": self.stats.errors,
            "wall_seconds": round(elapsed, 1),
        }
