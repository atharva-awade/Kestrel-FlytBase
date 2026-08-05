"""Ingest — one frame interface over real video, scripted text and rendered pixels."""

from kestrel.ingest.sources import (
    FrameSource,
    RawFrame,
    ScriptedSource,
    SyntheticSource,
    VideoFileSource,
    crop,
    hamming,
    phash,
    save_jpeg,
    to_jpeg_bytes,
)

__all__ = [
    "FrameSource",
    "RawFrame",
    "ScriptedSource",
    "SyntheticSource",
    "VideoFileSource",
    "crop",
    "hamming",
    "phash",
    "save_jpeg",
    "to_jpeg_bytes",
]
