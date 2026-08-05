"""Perception — the five-tier cascade from raw frame to structured meaning."""

from kestrel.perception.detect import Detector, RawDetection, get_detector
from kestrel.perception.grounding import vlm_ground
from kestrel.perception.pipeline import PerceptionPipeline, PerceptionResult
from kestrel.perception.project import GroundProjector, Projection
from kestrel.perception.track import TrackedDetection, Tracker
from kestrel.perception.vlm import DeepEscalator, SceneRequest, describe_scene

__all__ = [
    "DeepEscalator",
    "Detector",
    "GroundProjector",
    "PerceptionPipeline",
    "PerceptionResult",
    "Projection",
    "RawDetection",
    "SceneRequest",
    "TrackedDetection",
    "Tracker",
    "describe_scene",
    "get_detector",
    "vlm_ground",
]
