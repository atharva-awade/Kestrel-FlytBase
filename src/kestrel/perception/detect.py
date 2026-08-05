"""Tier 1 — object detection, on-device.

No hosted detector is reachable on NIM (see ADR 0001), so detection runs locally.
That turned out to be the better architecture anyway: detection is per-frame work,
and per-frame work belongs at the edge. It costs no API budget, is not rate
limited, and keeps working with the network unplugged — which is how a drone-in-a-
box actually operates when the site link drops.

Three backends, selected by what the machine can actually do:

    GroundingDinoBackend   open-vocabulary. Detects from *text phrases*, so a rule
                           can carry its own visual predicate ("person on a
                           ladder") with no training and no fixed class list.
    RTDetrBackend          closed-set COCO. Faster, no prompt, Apache-2.0.
    HeuristicBackend       no torch at all — contour motion detection. Degraded but
                           honest: the pipeline still runs on a CPU-only reviewer
                           machine, and says so.

The backend is chosen once at startup and reported in the health endpoint, so the
UI can state plainly which one produced a given box.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from typing import Any

import numpy as np

from kestrel.config import Settings, get_settings
from kestrel.domain import BBox
from kestrel.obs.meter import METER, Call, Stage

# The default vocabulary. Open-vocabulary detection means this is a starting point
# rather than a ceiling — rules extend it at runtime with their own phrases.
DEFAULT_VOCABULARY = [
    "person",
    "truck",
    "car",
    "van",
    "motorcycle",
    "bicycle",
    "forklift",
    "bus",
    "dog",
    "backpack",
    "cardboard box",
    "ladder",
]

# COCO classes worth surfacing on a secure site. Everything else is noise here.
COCO_KEEP = {
    "person", "bicycle", "car", "motorcycle", "bus", "truck", "boat",
    "backpack", "handbag", "suitcase", "dog", "cat", "horse", "bird",
    "cell phone", "laptop", "umbrella",
}


class RawDetection:
    """A detection before it is given an id, a track or a zone."""

    __slots__ = ("bbox", "confidence", "label")

    def __init__(self, label: str, confidence: float, bbox: BBox) -> None:
        self.label = label
        self.confidence = confidence
        self.bbox = bbox

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"RawDetection({self.label!r}, {self.confidence:.2f}, {self.bbox.as_tuple()})"


class DetectorBackend(ABC):
    name: str = "abstract"
    open_vocabulary: bool = False
    device: str = "cpu"

    @abstractmethod
    def detect(self, image: np.ndarray, phrases: list[str] | None = None) -> list[RawDetection]: ...

    @property
    def info(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "device": self.device,
            "open_vocabulary": self.open_vocabulary,
        }


# ═══════════════════════════════════════════════════════════════════════════════
_HF_REACHABLE: bool | None = None


_HF_CACHED: dict[str, bool] = {}


def hf_model_cached(model_id: str) -> bool:
    """Is this model already in the local HuggingFace cache?

    This is the question that actually matters, and asking the *other* question
    first was a bug: a cached model needs no network at all, so gating it behind a
    reachability probe disabled open-vocabulary detection on a machine that had
    the weights sitting on disk.

    Once cached, a model is permanently available — which is why fetching it once,
    on any network, is a complete fix for a filtered one.
    """
    if model_id in _HF_CACHED:
        return _HF_CACHED[model_id]
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(model_id, local_files_only=True)
        _HF_CACHED[model_id] = True
    except Exception:
        _HF_CACHED[model_id] = False
    return _HF_CACHED[model_id]


def hf_reachable(timeout: float = 2.5) -> bool:
    """Can we reach the Hub to *download* something not already cached?

    Only consulted on a cache miss. On a network where the Hub is SNI-filtered
    this returns False in a couple of seconds instead of letting huggingface_hub
    retry five times with backoff on every process start.

    ``HF_ENDPOINT`` is honoured, so a mirror that is reachable when the canonical
    host is not gets probed instead.
    """
    global _HF_REACHABLE
    if _HF_REACHABLE is not None:
        return _HF_REACHABLE
    import os

    if os.getenv("HF_HUB_OFFLINE") == "1":
        _HF_REACHABLE = False
        return False
    endpoint = os.getenv("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    try:
        import httpx

        r = httpx.head(f"{endpoint}/api/models", timeout=timeout, follow_redirects=True)
        _HF_REACHABLE = r.status_code < 500
    except Exception:
        _HF_REACHABLE = False
    return _HF_REACHABLE


def hf_usable(model_id: str) -> tuple[bool, str]:
    """Can this Hub-backed model be loaded, and why or why not?

    Cached beats reachable: weights on disk make the network irrelevant.
    """
    if hf_model_cached(model_id):
        return True, "cached locally"
    if hf_reachable():
        return True, "hub reachable, will download"
    return False, "not cached and the hub is unreachable"


def _pick_device(preference: str = "auto") -> str:
    if preference != "auto":
        return preference
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


class GroundingDinoBackend(DetectorBackend):
    """Open-vocabulary detection. The backend that makes promptable rules work.

    Grounding DINO takes an image plus a period-separated phrase string and returns
    boxes for whatever matched — no training, no fixed class list. That is what
    lets the natural-language rule compiler invent a detector for "unattended bag"
    at runtime.
    """

    name = "grounding-dino"
    open_vocabulary = True

    def __init__(self, model_id: str, device: str = "auto", box_threshold: float = 0.32,
                 text_threshold: float = 0.25) -> None:
        import torch
        from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

        self.device = _pick_device(device)
        self.model_id = model_id
        self.box_threshold = box_threshold
        self.text_threshold = text_threshold
        self._torch = torch

        # When the weights are already cached, load them WITHOUT touching the
        # network. `from_pretrained` otherwise makes an etag request to check for
        # updates, and on a filtered network that request fails and takes the
        # whole load with it — even though every byte needed is on disk.
        local_only = hf_model_cached(model_id) and not hf_reachable()

        self._processor = AutoProcessor.from_pretrained(
            model_id, local_files_only=local_only
        )
        self._model = AutoModelForZeroShotObjectDetection.from_pretrained(
            model_id, local_files_only=local_only
        ).to(self.device)
        self._model.eval()
        # HF models are not thread-safe for concurrent forward passes.
        self._lock = threading.Lock()

    def detect(self, image: np.ndarray, phrases: list[str] | None = None) -> list[RawDetection]:
        import cv2

        vocab = phrases or DEFAULT_VOCABULARY
        # Grounding DINO expects lowercase phrases terminated by periods.
        prompt = ". ".join(p.strip().lower() for p in vocab) + "."
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

        with self._lock:
            inputs = self._processor(images=rgb, text=prompt, return_tensors="pt").to(self.device)
            with self._torch.no_grad():
                outputs = self._model(**inputs)
            results = self._processor.post_process_grounded_object_detection(
                outputs,
                inputs.input_ids,
                threshold=self.box_threshold,
                text_threshold=self.text_threshold,
                target_sizes=[rgb.shape[:2]],
            )[0]

        out: list[RawDetection] = []
        labels = results.get("text_labels", results.get("labels", []))
        for box, score, label in zip(results["boxes"], results["scores"], labels, strict=False):
            x1, y1, x2, y2 = (float(v) for v in box.tolist())
            name = label if isinstance(label, str) else str(label)
            name = name.strip().strip(".")
            if not name:
                continue
            out.append(
                RawDetection(name, float(score), BBox(x1=x1, y1=y1, x2=x2, y2=y2))
            )
        return out


class RTDetrBackend(DetectorBackend):
    """Closed-set COCO detection. Faster, Apache-2.0, no prompt.

    Used when open-vocabulary is unavailable or when a rule needs only standard
    classes and the extra latency of a grounded model is not justified.
    """

    name = "rt-detr"
    open_vocabulary = False

    def __init__(self, model_id: str, device: str = "auto", threshold: float = 0.45) -> None:
        import torch
        from transformers import AutoImageProcessor, AutoModelForObjectDetection

        self.device = _pick_device(device)
        self.model_id = model_id
        self.threshold = threshold
        self._torch = torch

        # Same reasoning as GroundingDinoBackend: cached weights must not depend
        # on a reachable Hub.
        local_only = hf_model_cached(model_id) and not hf_reachable()

        self._processor = AutoImageProcessor.from_pretrained(
            model_id, local_files_only=local_only
        )
        self._model = AutoModelForObjectDetection.from_pretrained(
            model_id, local_files_only=local_only
        ).to(self.device)
        self._model.eval()
        self._id2label = self._model.config.id2label
        self._lock = threading.Lock()

    def detect(self, image: np.ndarray, phrases: list[str] | None = None) -> list[RawDetection]:
        import cv2

        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        with self._lock:
            inputs = self._processor(images=rgb, return_tensors="pt").to(self.device)
            with self._torch.no_grad():
                outputs = self._model(**inputs)
            results = self._processor.post_process_object_detection(
                outputs, target_sizes=[rgb.shape[:2]], threshold=self.threshold
            )[0]

        wanted = {p.lower() for p in phrases} if phrases else None
        out: list[RawDetection] = []
        for score, label_id, box in zip(
            results["scores"], results["labels"], results["boxes"], strict=False
        ):
            label = str(self._id2label[int(label_id)]).lower()
            if label not in COCO_KEEP:
                continue
            if wanted and not any(w in label or label in w for w in wanted):
                continue
            x1, y1, x2, y2 = (float(v) for v in box.tolist())
            out.append(RawDetection(label, float(score), BBox(x1=x1, y1=y1, x2=x2, y2=y2)))
        return out


class YoloBackend(DetectorBackend):
    """YOLO11, closed-set COCO, CUDA-accelerated.

    This is the backend that works when the HuggingFace Hub is unreachable, because
    ultralytics serves its weights from GitHub releases instead. On the development
    machine that is not a hypothetical: HF is blocked at the network level here
    while GitHub is not (see ADR 0002).

    Closed-set, so it cannot satisfy a rule's custom visual predicate on its own —
    ``Detector.ground()`` handles that by routing open-vocabulary queries to the VLM.
    """

    name = "yolo11"
    open_vocabulary = False

    def __init__(self, weights: str = "yolo11s.pt", device: str = "auto",
                 threshold: float = 0.35) -> None:
        from ultralytics import YOLO

        self.device = _pick_device(device)
        self.threshold = threshold
        self.weights = weights
        self._model = YOLO(weights)
        self._model.to(self.device)
        self._names = self._model.names
        self._lock = threading.Lock()

    def detect(self, image: np.ndarray, phrases: list[str] | None = None) -> list[RawDetection]:
        with self._lock:
            results = self._model.predict(
                image, conf=self.threshold, device=self.device, verbose=False
            )
        wanted = {p.lower() for p in phrases} if phrases else None
        out: list[RawDetection] = []
        for r in results:
            boxes = getattr(r, "boxes", None)
            if boxes is None:
                continue
            for i in range(len(boxes)):
                label = str(self._names[int(boxes.cls[i])]).lower()
                if label not in COCO_KEEP:
                    continue
                if wanted and not any(w in label or label in w for w in wanted):
                    continue
                x1, y1, x2, y2 = (float(v) for v in boxes.xyxy[i].tolist())
                out.append(
                    RawDetection(
                        label,
                        float(boxes.conf[i]),
                        BBox(x1=x1, y1=y1, x2=x2, y2=y2),
                    )
                )
        return out


class HeuristicBackend(DetectorBackend):
    """No-torch fallback: background subtraction plus contour extraction.

    This will not identify *what* moved, only *that* something did and roughly
    where. It exists so that a reviewer on a CPU-only machine with no model
    downloads still gets a running pipeline — with the UI stating clearly that
    detection is degraded rather than quietly presenting guesses as classifications.
    """

    name = "heuristic-motion"
    open_vocabulary = False

    def __init__(self, min_area_frac: float = 0.0015) -> None:
        import cv2

        self.device = "cpu"
        self.min_area_frac = min_area_frac
        self._bg = cv2.createBackgroundSubtractorMOG2(
            history=120, varThreshold=28, detectShadows=False
        )
        self._lock = threading.Lock()

    def detect(self, image: np.ndarray, phrases: list[str] | None = None) -> list[RawDetection]:
        import cv2

        h, w = image.shape[:2]
        with self._lock:
            mask = self._bg.apply(image)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        mask = cv2.dilate(mask, np.ones((7, 7), np.uint8), iterations=2)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        out: list[RawDetection] = []
        for c in contours:
            x, y, cw, ch = cv2.boundingRect(c)
            if (cw * ch) / (w * h) < self.min_area_frac:
                continue
            aspect = ch / max(1, cw)
            # A tall narrow blob is more likely a person than a vehicle. This is a
            # heuristic and is labelled as such — never presented as a classification.
            label = "moving-object(person?)" if aspect > 1.4 else "moving-object"
            out.append(
                RawDetection(
                    label,
                    0.35,
                    BBox(x1=float(x), y1=float(y), x2=float(x + cw), y2=float(y + ch)),
                )
            )
        return out[:20]


# ═══════════════════════════════════════════════════════════════════════════════
class Detector:
    """Chooses a backend once, then presents one interface to the pipeline."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()
        self.backend: DetectorBackend = self._build()
        self.fallback_reason: str | None = getattr(self, "_reason", None)

    def _build(self) -> DetectorBackend:
        """Pick the best backend this machine can actually reach.

        Ordered by capability, not preference: an open-vocabulary local model is
        strictly better, but it depends on the HuggingFace Hub. YOLO is nearly as
        good for the fast path and its weights come from GitHub, so it survives a
        blocked Hub. The heuristic backend exists so that the pipeline never simply
        stops.
        """
        if not self.s.local_detector:
            self._reason = "local detection disabled by configuration"
            return HeuristicBackend()

        attempts: list[str] = []

        # A Hub-backed backend is worth attempting when its weights are *cached*,
        # regardless of whether the Hub is reachable — which is the common case on
        # a filtered network once the model has been fetched once. Reachability is
        # only consulted on a cache miss, and probing once is far cheaper than five
        # retries with backoff per model.
        gdino_ok, gdino_why = hf_usable(self.s.local_detector_model)
        rtdetr_ok, _ = hf_usable(self.s.local_detector_fallback)

        candidates: list[tuple[Any, str]] = []
        if gdino_ok:
            candidates.append(
                (
                    lambda: GroundingDinoBackend(
                        self.s.local_detector_model,
                        self.s.detector_device,
                        self.s.detector_box_threshold,
                        self.s.detector_text_threshold,
                    ),
                    "grounding-dino",
                )
            )
        else:
            attempts.append(f"grounding-dino: {gdino_why}")

        candidates.append(
            (lambda: YoloBackend(self.s.yolo_weights, self.s.detector_device), "yolo11")
        )

        if rtdetr_ok:
            candidates.append(
                (
                    lambda: RTDetrBackend(
                        self.s.local_detector_fallback, self.s.detector_device
                    ),
                    "rt-detr",
                )
            )

        for factory, note in candidates:
            try:
                backend = factory()
            except Exception as e:
                attempts.append(f"{note}: {type(e).__name__}: {str(e)[:110]}")
                continue
            self._reason = (
                None
                if not attempts
                else f"preferred backend unavailable ({attempts[0]}); using {backend.name}"
            )
            return backend

        self._reason = (
            "no model backend available (" + "; ".join(attempts) + "); "
            "detection degraded to motion heuristics"
        )
        return HeuristicBackend()

    # ── open-vocabulary ──────────────────────────────────────────────────
    async def ground(
        self, image: np.ndarray, phrases: list[str]
    ) -> list[RawDetection]:
        """Detect arbitrary text-described objects.

        If the local backend is open-vocabulary this is just ``detect``. Otherwise
        the query is routed to the VLM, which can be asked to localise things no
        COCO class covers — "a person on a ladder", "an unattended bag".

        This is what keeps promptable rules working regardless of which backend
        the machine could load. VLM boxes are coarser than a detector's, so they
        are marked as such and the caller weights them accordingly.
        """
        if self.backend.open_vocabulary:
            return self.detect(image, phrases)

        from kestrel.perception.grounding import vlm_ground

        return await vlm_ground(image, phrases)

    def detect(self, image: np.ndarray, phrases: list[str] | None = None) -> list[RawDetection]:
        t0 = time.perf_counter()
        try:
            dets = self.backend.detect(image, phrases)
            ok, err = True, None
        except Exception as e:
            dets, ok, err = [], False, f"{type(e).__name__}: {e}"[:160]
        METER.record(
            Call(
                Stage.DETECT,
                f"local:{self.backend.name}",
                (time.perf_counter() - t0) * 1000,
                ok=ok,
                local=True,
                error=err,
                meta={"n": len(dets)},
            )
        )
        return dets

    @property
    def info(self) -> dict[str, Any]:
        return {
            **self.backend.info,
            "degraded": isinstance(self.backend, HeuristicBackend),
            "fallback_reason": self.fallback_reason,
        }


_DETECTOR: Detector | None = None


def get_detector(settings: Settings | None = None) -> Detector:
    """Process-wide detector. Model weights load once, not per frame."""
    global _DETECTOR
    if _DETECTOR is None:
        _DETECTOR = Detector(settings)
    return _DETECTOR
