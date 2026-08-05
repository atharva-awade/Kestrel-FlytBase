"""Entity re-identification — the difference between a caption bot and an analyst.

A tracker gives identity *within* a continuous observation. It cannot tell you that
the vehicle at the gate this morning is the one that was here on Tuesday, because
the track ended when the vehicle left the frame.

Entity resolution closes that gap. Each detection is matched against known entities
using three independent signals, because no single one is reliable on its own:

    appearance   cosine similarity of the joint image/text embedding
    attributes   colour, type, and other VLM-extracted descriptors
    spatiotemporal  how recently and how near the candidate was last seen

The combination is what makes ``ENT-0043 · blue Ford F-150 · 7th visit · first ever
seen at 02:00`` possible — and that sentence is the entire point of the system.

**Where this is weaker than a production re-ID system, stated plainly:** these are
general-purpose embeddings, not a model trained with a re-identification objective
on vehicles or people. They confuse two similar white vans more readily than a
dedicated model would. The thresholds are therefore set conservatively — the system
prefers to create a new entity over merging two distinct ones, because a wrongly
merged entity produces a confidently false history, which is worse than a
fragmented one.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from kestrel.domain import Detection, Entity, EntityKind, SceneGraph

# Two sightings separated by more than this are counted as separate *visits*
# rather than one continuous presence.
VISIT_GAP = timedelta(minutes=20)

# Match acceptance. Set high deliberately: a wrong merge fabricates history.
MATCH_THRESHOLD = 0.72
# Below this, no amount of attribute agreement rescues the match.
APPEARANCE_FLOOR = 0.55

VEHICLE_WORDS = ("car", "truck", "van", "bus", "pickup", "lorry", "motorcycle", "vehicle")
PERSON_WORDS = ("person", "man", "woman", "worker", "pedestrian", "people")
ANIMAL_WORDS = ("dog", "cat", "bird", "horse", "animal")


def kind_of(label: str) -> EntityKind:
    low = label.lower()
    if any(w in low for w in PERSON_WORDS):
        return EntityKind.PERSON
    if any(w in low for w in VEHICLE_WORDS):
        return EntityKind.VEHICLE
    if any(w in low for w in ANIMAL_WORDS):
        return EntityKind.ANIMAL
    return EntityKind.OBJECT


@dataclass
class _Known:
    """An entity plus the running state needed to match against it."""

    entity: Entity
    embeddings: list[np.ndarray] = field(default_factory=list)
    last_track_id: int | None = None

    def centroid(self) -> np.ndarray | None:
        if not self.embeddings:
            return None
        m = np.vstack(self.embeddings)
        c = m.mean(axis=0)
        n = np.linalg.norm(c)
        return c / n if n else None


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return float(np.dot(a, b) / (na * nb)) if na and nb else 0.0


def _attribute_score(a: dict[str, str], b: dict[str, str]) -> float:
    """Agreement over shared descriptive keys. Neutral when nothing overlaps."""
    keys = {"colour", "kind", "make"} & set(a) & set(b)
    if not keys:
        return 0.5
    hits = sum(1 for k in keys if a[k].lower() == b[k].lower())
    return hits / len(keys)


class EntityResolver:
    """Maintains persistent identities for one site."""

    def __init__(self, site_id: str, *, match_threshold: float = MATCH_THRESHOLD) -> None:
        self.site_id = site_id
        self.match_threshold = match_threshold
        self._known: dict[str, _Known] = {}
        self._by_track: dict[int, str] = {}
        self._seq = 0
        self.merges = 0
        self.creations = 0

    # ── loading ──────────────────────────────────────────────────────────
    def load(self, entities: list[Entity], embeddings: dict[str, list[np.ndarray]]) -> None:
        """Restore state so identity survives a restart — otherwise "7th visit"
        would reset to 1 every time the process bounced."""
        for e in entities:
            self._known[e.id] = _Known(entity=e, embeddings=embeddings.get(e.id, []))
            n = int(e.id.rsplit("-", 1)[-1]) if e.id.rsplit("-", 1)[-1].isdigit() else 0
            self._seq = max(self._seq, n)

    # ── matching ─────────────────────────────────────────────────────────
    def resolve(
        self,
        det: Detection,
        ts: datetime,
        *,
        embedding: list[float] | None = None,
        attributes: dict[str, str] | None = None,
    ) -> Entity:
        attrs = {k: v for k, v in (attributes or {}).items() if v}
        kind = kind_of(det.label)
        vec = np.asarray(embedding, dtype=np.float32) if embedding else None

        # The tracker already asserted identity within this observation. Trusting
        # it is both cheaper and more reliable than re-deriving from appearance.
        if det.track_id is not None and det.track_id in self._by_track:
            eid = self._by_track[det.track_id]
            if eid in self._known:
                return self._touch(self._known[eid], det, ts, vec, attrs)

        best_id, best_score = None, 0.0
        for eid, k in self._known.items():
            if k.entity.kind is not kind:
                continue
            score = self._score(k, det, ts, vec, attrs)
            if score > best_score:
                best_id, best_score = eid, score

        if best_id is not None and best_score >= self.match_threshold:
            self.merges += 1
            if det.track_id is not None:
                self._by_track[det.track_id] = best_id
            return self._touch(self._known[best_id], det, ts, vec, attrs)

        return self._create(det, ts, kind, vec, attrs)

    def _score(
        self, k: _Known, det: Detection, ts: datetime,
        vec: np.ndarray | None, attrs: dict[str, str],
    ) -> float:
        centroid = k.centroid()

        if vec is not None and centroid is not None:
            appearance = _cos(vec, centroid)
            # A weak appearance match is disqualifying regardless of how well the
            # attributes agree — "blue" and "truck" describe a great many trucks.
            if appearance < APPEARANCE_FLOOR:
                return 0.0
        else:
            # With no vectors on either side we cannot claim a visual match. Return
            # a value that can only clear the threshold with strong attribute and
            # recency agreement, never on its own.
            appearance = 0.45

        attribute = _attribute_score(k.entity.attributes, attrs)

        gap = abs((ts - k.entity.last_seen).total_seconds())
        recency = 1.0 if gap < 120 else max(0.15, 1.0 - gap / (6 * 3600))

        return appearance * 0.6 + attribute * 0.22 + recency * 0.18

    # ── mutation ─────────────────────────────────────────────────────────
    def _touch(
        self, k: _Known, det: Detection, ts: datetime,
        vec: np.ndarray | None, attrs: dict[str, str],
    ) -> Entity:
        e = k.entity
        if ts - e.last_seen > VISIT_GAP:
            e.visit_count += 1
        e.last_seen = max(e.last_seen, ts)
        e.frame_count += 1
        if det.zone_id and det.zone_id not in e.zones_seen:
            e.zones_seen.append(det.zone_id)
        if self.site_id not in e.sites_seen:
            e.sites_seen.append(self.site_id)
        for key, val in attrs.items():
            e.attributes.setdefault(key, val)
        if vec is not None:
            # Bounded history: an entity seen 500 times should not carry 500
            # vectors, and the most recent appearances are the most useful.
            k.embeddings.append(vec)
            if len(k.embeddings) > 12:
                k.embeddings.pop(0)
        e.descriptor = _describe(e)
        det.entity_id = e.id
        if det.track_id is not None:
            k.last_track_id = det.track_id
            self._by_track[det.track_id] = e.id
        return e

    def _create(
        self, det: Detection, ts: datetime, kind: EntityKind,
        vec: np.ndarray | None, attrs: dict[str, str],
    ) -> Entity:
        self._seq += 1
        self.creations += 1
        # Stable, readable id. The site hash keeps ids unique across the fleet
        # without a central allocator.
        site_tag = hashlib.sha1(self.site_id.encode()).hexdigest()[:4]
        eid = f"ENT-{site_tag}-{self._seq:04d}"
        e = Entity(
            id=eid,
            site_id=self.site_id,
            kind=kind,
            label=det.label,
            attributes=dict(attrs),
            first_seen=ts,
            last_seen=ts,
            visit_count=1,
            frame_count=1,
            zones_seen=[det.zone_id] if det.zone_id else [],
            sites_seen=[self.site_id],
        )
        e.descriptor = _describe(e)
        k = _Known(entity=e, embeddings=[vec] if vec is not None else [])
        self._known[eid] = k
        det.entity_id = eid
        if det.track_id is not None:
            self._by_track[det.track_id] = eid
        return e

    # ── read-out ─────────────────────────────────────────────────────────
    @property
    def entities(self) -> list[Entity]:
        return [k.entity for k in self._known.values()]

    def get(self, entity_id: str) -> Entity | None:
        k = self._known.get(entity_id)
        return k.entity if k else None

    def embeddings_for(self, entity_id: str) -> list[np.ndarray]:
        k = self._known.get(entity_id)
        return list(k.embeddings) if k else []

    @property
    def stats(self) -> dict:
        return {
            "entities": len(self._known),
            "created": self.creations,
            "matched": self.merges,
            "match_threshold": self.match_threshold,
            "by_kind": {
                kind.value: sum(1 for k in self._known.values() if k.entity.kind is kind)
                for kind in EntityKind
            },
        }


def _describe(e: Entity) -> str:
    """A human-readable label: 'blue Ford F-150' rather than 'ENT-3f2a-0007'."""
    bits = [e.attributes.get("colour"), e.attributes.get("make"), e.attributes.get("kind")]
    text = " ".join(b for b in bits if b).strip()
    return text or e.label


def attributes_from_scene(scene: SceneGraph | None, label: str) -> dict[str, str]:
    """Pull the attributes for a given detection out of the scene graph.

    The detector says "truck"; the VLM says "blue pickup truck, reversing". This is
    where those two views are joined, and it is what gives an entity a descriptor a
    person would recognise.
    """
    if scene is None:
        return {}
    low = label.lower()
    for o in scene.objects:
        ol = o.label.lower()
        if ol == low or ol in low or low in ol:
            out: dict[str, str] = {}
            if o.colour:
                out["colour"] = o.colour.lower()
            if o.kind:
                out["kind"] = o.kind.lower()
            if o.activity:
                out["activity"] = o.activity.lower()
            return out
    return {}
