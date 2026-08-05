"""Memory — what persists beyond a single frame.

Three complementary stores, because "remember" means three different things here:

    entities.py   *who* — identities that persist across frames, sessions and days
    pyramid.py    *what happened* — hierarchical compression of the observation stream
    baseline.py   *what is normal* — the statistical model that makes novelty measurable
"""

from kestrel.memory.baseline import BaselineModel, Deviation, combine_confidence
from kestrel.memory.entities import EntityResolver, attributes_from_scene, kind_of
from kestrel.memory.pyramid import FrameNote, MemoryPyramid, events_from_notes

__all__ = [
    "BaselineModel",
    "Deviation",
    "EntityResolver",
    "FrameNote",
    "MemoryPyramid",
    "attributes_from_scene",
    "combine_confidence",
    "events_from_notes",
    "kind_of",
]
