"""Tier 1.5 — multi-object tracking.

This tier is easy to skip and expensive to omit. Without it, "a person" in frame
41 and "a person" in frame 42 are two unrelated observations, and every claim the
system makes about duration is a guess. Dwell time, loitering, "the same vehicle
returned", the entire temporal rule vocabulary — all of it needs identity that
persists across frames.

ByteTrack (via ``supervision``, MIT) provides it. Its distinguishing idea is that
*low*-confidence detections are still worth associating: an object that becomes
briefly ambiguous — occluded by a pillar, half out of frame — keeps its identity
instead of being reborn with a new one when it reappears. On surveillance footage,
where occlusion is constant, that matters more than raw detector accuracy.

The tracker owns pixel-space identity only. Persisting an entity across a gap of
hours, or across sites, is a different problem solved by appearance embeddings in
``memory/entities.py``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from kestrel.domain import BBox
from kestrel.obs.meter import METER, Call, Stage
from kestrel.perception.detect import RawDetection


@dataclass(slots=True)
class TrackedDetection:
    """A detection that now knows who it is."""

    label: str
    confidence: float
    bbox: BBox
    track_id: int | None
    age: int = 1          # frames this track has been alive
    is_new: bool = False  # first frame we have seen this track


class Tracker:
    """ByteTrack wrapper with a nearest-neighbour fallback.

    The fallback exists for the same reason as the heuristic detector: the pipeline
    must run for a reviewer whose environment is missing a dependency. It is
    genuinely worse — IoU matching alone loses identity through occlusion — and the
    system reports which one produced a given track.
    """

    def __init__(
        self,
        *,
        frame_rate: int = 2,
        track_activation_threshold: float = 0.25,
        lost_track_buffer: int = 60,
        minimum_matching_threshold: float = 0.85,
    ) -> None:
        self.backend = "bytetrack"
        self._seen: dict[int, int] = {}
        self._fallback_next_id = 1
        self._fallback_prev: list[TrackedDetection] = []

        try:
            import supervision as sv

            self._sv = sv
            self._tracker = sv.ByteTrack(
                track_activation_threshold=track_activation_threshold,
                lost_track_buffer=lost_track_buffer,
                minimum_matching_threshold=minimum_matching_threshold,
                # Buffer is expressed in frames, so it must follow the *analysis*
                # rate, not the video's native rate. At 2 fps a 60-frame buffer is
                # 30 seconds of tolerated occlusion, which suits a walking person.
                frame_rate=frame_rate,
            )
        except Exception:
            self._sv = None
            self._tracker = None
            self.backend = "iou-fallback"

    # ── main entry ───────────────────────────────────────────────────────
    def update(self, detections: list[RawDetection]) -> list[TrackedDetection]:
        t0 = time.perf_counter()
        out = (
            self._update_bytetrack(detections)
            if self._tracker is not None
            else self._update_fallback(detections)
        )
        METER.record(
            Call(
                Stage.TRACK,
                f"local:{self.backend}",
                (time.perf_counter() - t0) * 1000,
                ok=True,
                local=True,
                meta={"in": len(detections), "out": len(out)},
            )
        )
        return out

    def _update_bytetrack(self, detections: list[RawDetection]) -> list[TrackedDetection]:
        sv = self._sv
        if not detections:
            # ByteTrack must still be stepped on empty frames, or its lost-track
            # buffer never ages and stale ids get reassigned to new objects.
            self._tracker.update_with_detections(sv.Detections.empty())
            return []

        xyxy = np.array([d.bbox.as_tuple() for d in detections], dtype=np.float32)
        conf = np.array([d.confidence for d in detections], dtype=np.float32)
        # supervision needs integer class ids; we keep the string labels alongside
        # and re-attach them after tracking.
        labels = [d.label for d in detections]
        vocab = {lab: i for i, lab in enumerate(sorted(set(labels)))}
        class_id = np.array([vocab[lab] for lab in labels], dtype=int)

        dets = sv.Detections(xyxy=xyxy, confidence=conf, class_id=class_id)
        tracked = self._tracker.update_with_detections(dets)

        inv = {i: lab for lab, i in vocab.items()}
        out: list[TrackedDetection] = []
        for i in range(len(tracked)):
            tid = int(tracked.tracker_id[i]) if tracked.tracker_id is not None else None
            box = tracked.xyxy[i]
            cid = int(tracked.class_id[i]) if tracked.class_id is not None else -1
            is_new = tid is not None and tid not in self._seen
            if tid is not None:
                self._seen[tid] = self._seen.get(tid, 0) + 1
            out.append(
                TrackedDetection(
                    label=inv.get(cid, "object"),
                    confidence=float(tracked.confidence[i]) if tracked.confidence is not None else 0.5,
                    bbox=BBox(x1=float(box[0]), y1=float(box[1]), x2=float(box[2]), y2=float(box[3])),
                    track_id=tid,
                    age=self._seen.get(tid, 1) if tid is not None else 1,
                    is_new=is_new,
                )
            )
        return out

    def _update_fallback(self, detections: list[RawDetection]) -> list[TrackedDetection]:
        """Greedy IoU association. Adequate for continuous motion, and only that."""
        out: list[TrackedDetection] = []
        used: set[int] = set()
        for d in detections:
            best, best_iou = None, 0.35
            for j, prev in enumerate(self._fallback_prev):
                if j in used or prev.label != d.label:
                    continue
                iou = d.bbox.iou(prev.bbox)
                if iou > best_iou:
                    best, best_iou = j, iou
            if best is not None:
                used.add(best)
                prev = self._fallback_prev[best]
                out.append(
                    TrackedDetection(d.label, d.confidence, d.bbox, prev.track_id, prev.age + 1, False)
                )
            else:
                tid = self._fallback_next_id
                self._fallback_next_id += 1
                out.append(TrackedDetection(d.label, d.confidence, d.bbox, tid, 1, True))
        self._fallback_prev = out
        return out

    @property
    def info(self) -> dict:
        return {
            "backend": self.backend,
            "degraded": self.backend != "bytetrack",
            "active_tracks": len(self._seen),
        }

    def reset(self) -> None:
        if self._tracker is not None:
            self._tracker.reset()
        self._seen.clear()
        self._fallback_prev = []
        self._fallback_next_id = 1
