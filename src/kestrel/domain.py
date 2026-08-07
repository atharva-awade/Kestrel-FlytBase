"""Domain contracts.

These Pydantic models are the spine of the system: they validate what the VLM
returns, define what the database stores, serialise the API, and generate the JSON
Schema the agent's tool registry hands to the frontend. Defining them once and
deriving everything else is what keeps the agent and the UI from drifting apart.

Coordinates follow one convention throughout, because mixing them is the classic
way to lose an afternoon:

*   **pixel space** — ``BBox``, origin top-left, ``(x1, y1, x2, y2)``
*   **world space** — ``lat``/``lon`` in WGS-84 decimal degrees
*   **site space**  — metres east/north of the site origin, used for geometry
"""

from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Geometry
# ═══════════════════════════════════════════════════════════════════════════════
class BBox(Base):
    """Axis-aligned box in pixel space, origin top-left."""

    x1: float
    y1: float
    x2: float
    y2: float

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2

    @property
    def w(self) -> float:
        return max(0.0, self.x2 - self.x1)

    @property
    def h(self) -> float:
        return max(0.0, self.y2 - self.y1)

    @property
    def area(self) -> float:
        return self.w * self.h

    def iou(self, other: BBox) -> float:
        ix1, iy1 = max(self.x1, other.x1), max(self.y1, other.y1)
        ix2, iy2 = min(self.x2, other.x2), min(self.y2, other.y2)
        inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
        union = self.area + other.area - inter
        return inter / union if union > 0 else 0.0

    @property
    def foot(self) -> tuple[float, float]:
        """Bottom-centre — where the object meets the ground.

        Projecting the box centre would place a tall object behind its true
        position; the ground contact point is the one that geo-locates correctly.
        """
        return (self.cx, self.y2)

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x1, self.y1, self.x2, self.y2)


class LatLon(Base):
    lat: float
    lon: float

    def haversine_m(self, other: LatLon) -> float:
        r = 6_371_000.0
        p1, p2 = math.radians(self.lat), math.radians(other.lat)
        dp = p2 - p1
        dl = math.radians(other.lon - self.lon)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * r * math.asin(min(1.0, math.sqrt(a)))


# ═══════════════════════════════════════════════════════════════════════════════
# Site
# ═══════════════════════════════════════════════════════════════════════════════
class ZoneKind(StrEnum):
    GATE = "gate"
    DOCK = "dock"
    YARD = "yard"
    BUILDING = "building"
    FENCE = "fence"
    SUBSTATION = "substation"
    RESTRICTED = "restricted"
    PARKING = "parking"
    ROAD = "road"
    PERIMETER = "perimeter"


class Zone(Base):
    """A named region of the site.

    ``polygon`` is a closed ring of world coordinates. Zone membership is resolved
    with supervision's PolygonZone in pixel space where a homography exists, and by
    point-in-polygon in world space otherwise.
    """

    id: str
    name: str
    kind: ZoneKind
    polygon: list[LatLon]
    # Multiplies rule severity — a fence breach matters more than a car park.
    priority: float = 1.0
    # Hours during which presence here is normal (local time, inclusive start).
    normal_hours: tuple[int, int] | None = None
    notes: str = ""

    def contains(self, p: LatLon) -> bool:
        """Ray casting. Sufficient for site-scale polygons, no dependency needed."""
        inside = False
        n = len(self.polygon)
        for i in range(n):
            a, b = self.polygon[i], self.polygon[(i + 1) % n]
            if (a.lat > p.lat) != (b.lat > p.lat):
                x = (b.lon - a.lon) * (p.lat - a.lat) / (b.lat - a.lat + 1e-15) + a.lon
                if p.lon < x:
                    inside = not inside
        return inside

    @property
    def centroid(self) -> LatLon:
        return LatLon(
            lat=sum(p.lat for p in self.polygon) / len(self.polygon),
            lon=sum(p.lon for p in self.polygon) / len(self.polygon),
        )


class Site(Base):
    """A monitored property. The fleet is a collection of these."""

    id: str
    name: str
    kind: str = "industrial"
    origin: LatLon
    timezone: str = "Asia/Kolkata"
    country: str = "IN"
    country_name: str = "India"
    zones: list[Zone] = Field(default_factory=list)
    dock: LatLon | None = None
    geofence: list[LatLon] = Field(default_factory=list)
    max_altitude_m: float = 120.0
    # True when real footage runs through the real pipeline for this site. False
    # sites are driven by the seeded fleet generator and are labelled SIMULATED in
    # the UI — we never imply we have more live drones than we do.
    live_footage: bool = False
    notes: str = ""

    def zone_at(self, p: LatLon) -> Zone | None:
        """Innermost matching zone. Smaller area wins so a gate inside a yard
        resolves to the gate."""
        hits = [z for z in self.zones if z.contains(p)]
        if not hits:
            return None
        return min(hits, key=lambda z: _ring_area(z.polygon))

    def zone_by_id(self, zone_id: str) -> Zone | None:
        return next((z for z in self.zones if z.id == zone_id), None)

    def zones_nested(self, a: str | None, b: str | None) -> bool:
        """True when one zone contains the other, or they are the same.

        Needed because a detection near a boundary oscillates between a zone and
        the zone nested inside it — the restricted core sits inside the substation,
        and projection jitter flips between them frame to frame. Treating that as
        the subject *leaving* would reset every dwell timer and make loitering
        undetectable exactly where it matters most.
        """
        if a == b:
            return True
        if a is None or b is None:
            return False
        za, zb = self.zone_by_id(a), self.zone_by_id(b)
        if za is None or zb is None:
            return False
        return za.contains(zb.centroid) or zb.contains(za.centroid)


def _ring_area(ring: list[LatLon]) -> float:
    """Shoelace area in squared degrees — only used for relative comparison."""
    a = 0.0
    n = len(ring)
    for i in range(n):
        p, q = ring[i], ring[(i + 1) % n]
        a += p.lon * q.lat - q.lon * p.lat
    return abs(a) / 2


# ═══════════════════════════════════════════════════════════════════════════════
# Telemetry
# ═══════════════════════════════════════════════════════════════════════════════
class DroneState(StrEnum):
    DOCKED = "docked"
    LAUNCHING = "launching"
    TRANSIT = "transit"
    HOVER = "hover"
    ORBIT = "orbit"
    TRACKING = "tracking"
    RETURNING = "returning"
    LANDING = "landing"
    CHARGING = "charging"

    @property
    def airborne(self) -> bool:
        return self not in (DroneState.DOCKED, DroneState.CHARGING)


class Telemetry(Base):
    """One telemetry sample, joined to frames on a shared clock.

    Telemetry is not decoration: altitude and gimbal angles drive the pixel→world
    projection, illuminance and speed feed the perception-confidence score, and
    battery gates which missions are feasible.
    """

    ts: datetime
    site_id: str
    lat: float
    lon: float
    alt_m: float
    heading_deg: float
    gimbal_pitch_deg: float = -90.0   # -90 is straight down
    gimbal_yaw_deg: float = 0.0
    speed_mps: float = 0.0
    battery_pct: float = 100.0
    gps_satellites: int = 14
    gps_hdop: float = 0.7
    wind_mps: float = 2.0
    illuminance_lux: float = 10_000.0
    state: DroneState = DroneState.HOVER
    signal_pct: float = 96.0

    @property
    def position(self) -> LatLon:
        return LatLon(lat=self.lat, lon=self.lon)

    @property
    def gps_ok(self) -> bool:
        return self.gps_satellites >= 8 and self.gps_hdop <= 2.0

    @property
    def is_night(self) -> bool:
        return self.illuminance_lux < 50.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def perception_confidence(self) -> float:
        """How much the optics can be trusted right now, in [0, 1].

        Degrades with altitude (smaller pixels on target), motion (blur), darkness
        and wind (gimbal jitter). Detections inherit this, so a low-confidence
        sighting at 90 m in the dark cannot trigger a critical alert on its own.
        """
        alt = max(0.0, 1.0 - max(0.0, self.alt_m - 25.0) / 120.0)
        motion = max(0.0, 1.0 - self.speed_mps / 18.0)
        light = min(1.0, 0.35 + math.log10(max(1.0, self.illuminance_lux)) / 5.0)
        wind = max(0.0, 1.0 - max(0.0, self.wind_mps - 6.0) / 14.0)
        gps = 1.0 if self.gps_ok else 0.75
        return round(max(0.05, min(1.0, alt * 0.3 + motion * 0.2 + light * 0.3 + wind * 0.1 + gps * 0.1)), 3)


# ═══════════════════════════════════════════════════════════════════════════════
# Perception
# ═══════════════════════════════════════════════════════════════════════════════
class FrameSourceKind(StrEnum):
    VIDEO = "video"        # real footage — the hero path
    SCRIPTED = "scripted"  # text-described frames, per the assignment's literal spec
    CASSETTE = "cassette"  # replay of a recorded run


class Frame(Base):
    id: str
    site_id: str
    seq: int
    ts: datetime
    source: FrameSourceKind
    path: str | None = None            # on-disk JPEG, when pixels exist
    width: int = 0
    height: int = 0
    phash: str | None = None
    text: str | None = None            # scripted-source description
    telemetry: Telemetry | None = None

    @property
    def has_pixels(self) -> bool:
        return self.path is not None


class GateVerdict(Base):
    """Tier-0 decision. Recorded for every frame, including the skips —
    the skip rate is the headline scalability number."""

    analyse: bool
    reason: str
    novelty: float = 0.0
    priority: float = 0.5
    phash_distance: int | None = None
    pixel_delta: float | None = None
    embed_similarity: float | None = None


class Detection(Base):
    """One object in one frame, from the local detector."""

    id: str
    frame_id: str
    label: str
    confidence: float
    bbox: BBox
    track_id: int | None = None
    entity_id: str | None = None
    zone_id: str | None = None
    world: LatLon | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    embedding_id: str | None = None


class SceneObject(Base):
    """An object as the VLM understands it — richer than a detector class."""

    label: str
    colour: str | None = None
    kind: str | None = None            # e.g. "pickup truck", "delivery van"
    activity: str | None = None
    count: int = 1
    confidence: float = 0.5
    notes: str | None = None


class SceneGraph(Base):
    """Structured VLM output. This is what tier 3 must produce, not free prose.

    ``model_config`` deliberately allows missing optional fields: VLMs under-fill
    schemas routinely, and rejecting a good caption because ``weather`` was absent
    would trade real signal for schema purity.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    caption: str
    objects: list[SceneObject] = Field(default_factory=list)
    activities: list[str] = Field(default_factory=list)
    lighting: Literal["daylight", "dusk", "night", "artificial", "unknown"] = "unknown"
    weather: str | None = None
    visibility: Literal["clear", "reduced", "poor", "unknown"] = "unknown"
    anomalies: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    tier: Literal["fast", "deep"] = "fast"

    @property
    def has_person(self) -> bool:
        return any("person" in o.label.lower() or "man" in o.label.lower() for o in self.objects)

    @property
    def has_vehicle(self) -> bool:
        keys = ("truck", "car", "van", "vehicle", "pickup", "lorry", "motorcycle", "forklift")
        return any(any(k in o.label.lower() for k in keys) for o in self.objects)


# ═══════════════════════════════════════════════════════════════════════════════
# Memory
# ═══════════════════════════════════════════════════════════════════════════════
class EntityKind(StrEnum):
    PERSON = "person"
    VEHICLE = "vehicle"
    ANIMAL = "animal"
    OBJECT = "object"
    UNKNOWN = "unknown"


class Entity(Base):
    """Something that persists across frames, sessions and days.

    This is the difference between "a person was detected" and "the same vehicle
    has now visited seven times, and tonight is the first time after midnight".
    """

    id: str
    site_id: str
    kind: EntityKind
    label: str
    descriptor: str = ""               # human-readable: "blue Ford F-150"
    attributes: dict[str, str] = Field(default_factory=dict)
    first_seen: datetime
    last_seen: datetime
    visit_count: int = 1
    frame_count: int = 1
    zones_seen: list[str] = Field(default_factory=list)
    sites_seen: list[str] = Field(default_factory=list)   # fleet-wide correlation
    threat_score: float = 0.0
    notes: str = ""


class EventKind(StrEnum):
    ENTRY = "entry"
    EXIT = "exit"
    DWELL = "dwell"
    TRANSIT = "transit"
    APPEARANCE = "appearance"
    INTERACTION = "interaction"
    ANOMALY = "anomaly"


class Event(Base):
    """A bounded episode. The L2 layer of the memory pyramid."""

    id: str
    site_id: str
    kind: EventKind
    entity_id: str | None = None
    zone_id: str | None = None
    start_ts: datetime
    end_ts: datetime
    frame_ids: list[str] = Field(default_factory=list)
    summary: str = ""
    salience: float = 0.5

    @property
    def duration_s(self) -> float:
        return max(0.0, (self.end_ts - self.start_ts).total_seconds())


class MemoryLevel(StrEnum):
    """The temporal memory pyramid. Each level compresses the one below under a
    token budget, which is how 8 hours of video becomes a queryable context."""

    FRAME = "L0_frame"
    CLIP = "L1_clip"
    EVENT = "L2_event"
    SHIFT = "L3_shift"
    DAY = "L4_day"


class MemoryNode(Base):
    id: str
    site_id: str
    level: MemoryLevel
    start_ts: datetime
    end_ts: datetime
    summary: str
    child_ids: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
    tokens: int = 0
    salience: float = 0.5


# ═══════════════════════════════════════════════════════════════════════════════
# Alerting
# ═══════════════════════════════════════════════════════════════════════════════
class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def weight(self) -> float:
        return {"info": 0.1, "low": 0.3, "medium": 0.55, "high": 0.8, "critical": 1.0}[self.value]


class AlertStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    INVESTIGATING = "investigating"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"
    FALSE_POSITIVE = "false_positive"


class Evidence(Base):
    """One link in an alert's chain of reasoning.

    Every alert must be able to show its work: which frames, which telemetry,
    which rule clause, which baseline deviation. Alerts without evidence are
    exactly the ones operators learn to ignore.
    """

    kind: Literal["frame", "detection", "telemetry", "rule", "baseline", "entity", "vlm"]
    ref_id: str
    caption: str = ""
    weight: float = 1.0
    detail: dict[str, Any] = Field(default_factory=dict)


class AlertLocation(Base):
    """Where the alert *is*, in terms a drone can be dispatched to.

    An alert that says "person at the main gate" is a notification. An alert that
    carries navigable coordinates, a bearing and a distance from the dock, an
    altitude recommendation and a geofence check is something an operator — or the
    mission planner — can act on without a second lookup.

    ``source`` matters as much as the numbers. A position derived from
    geo-projecting the detection is accurate to a few metres; a fall-back to the
    zone centroid is accurate to the size of the zone. Dispatching on the second
    while believing the first is how a drone ends up orbiting the wrong corner of
    a yard, so the provenance travels with the coordinates.
    """

    lat: float | None = None
    lon: float | None = None
    zone_id: str | None = None
    zone_name: str | None = None
    source: Literal["geo-projection", "zone-centroid", "drone-position", "unknown"] = "unknown"
    accuracy_m: float | None = None
    confidence: float = 0.0

    # Navigation, relative to the dock the responding aircraft launches from.
    distance_from_dock_m: float | None = None
    bearing_from_dock_deg: float | None = None
    eta_seconds: float | None = None
    recommended_altitude_m: float = 25.0
    within_geofence: bool = True

    # Where the aircraft was when this was observed — the start of any response.
    drone_lat: float | None = None
    drone_lon: float | None = None
    drone_alt_m: float | None = None
    dock_lat: float | None = None
    dock_lon: float | None = None

    @property
    def navigable(self) -> bool:
        """Do we have a position good enough to fly to?"""
        return (
            self.lat is not None
            and self.lon is not None
            and self.within_geofence
            and self.source != "unknown"
        )

    @property
    def summary(self) -> str:
        if self.lat is None or self.lon is None:
            return "no navigable position, dispatch on zone description only"
        bits = [f"{self.lat:.6f}, {self.lon:.6f}"]
        if self.zone_name:
            bits.append(self.zone_name)
        if self.distance_from_dock_m is not None:
            bits.append(f"{self.distance_from_dock_m:.0f} m from dock")
        if self.bearing_from_dock_deg is not None:
            bits.append(f"bearing {self.bearing_from_dock_deg:.0f}°")
        if self.eta_seconds is not None:
            bits.append(f"ETA {self.eta_seconds:.0f}s")
        return " · ".join(bits)


class Alert(Base):
    id: str
    site_id: str
    rule_id: str
    rule_name: str
    severity: Severity
    title: str
    narrative: str = ""
    ts: datetime
    zone_id: str | None = None
    # Everything needed to send an aircraft here. See AlertLocation.
    location: AlertLocation = Field(default_factory=AlertLocation)
    entity_ids: list[str] = Field(default_factory=list)
    frame_ids: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    # Composite of perception confidence x rule strength x baseline deviation.
    confidence: float = 0.5
    baseline_deviation: float = 0.0
    status: AlertStatus = AlertStatus.OPEN
    suppressed_reason: str | None = None
    mission_id: str | None = None
    operator_feedback: str | None = None


# ═══════════════════════════════════════════════════════════════════════════════
# Action
# ═══════════════════════════════════════════════════════════════════════════════
class MissionStepKind(StrEnum):
    LAUNCH = "launch"
    GOTO = "goto"
    ORBIT = "orbit"
    TRACK = "track"
    HOVER = "hover"
    SWEEP = "sweep"
    RETURN = "return"
    LAND = "land"


class MissionStep(Base):
    kind: MissionStepKind
    target: LatLon | None = None
    zone_id: str | None = None
    entity_id: str | None = None
    altitude_m: float = 30.0
    radius_m: float = 20.0
    duration_s: float = 30.0
    note: str = ""


class Feasibility(Base):
    """Why a mission can or cannot fly. Shown to the operator before they approve —
    an approval button without the constraints behind it is theatre."""

    feasible: bool
    battery_required_pct: float
    battery_available_pct: float
    distance_m: float
    duration_s: float
    within_geofence: bool
    wind_ok: bool
    altitude_ok: bool
    daylight: bool
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class MissionStatus(StrEnum):
    PROPOSED = "proposed"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    COMPLETED = "completed"
    ABORTED = "aborted"


class Mission(Base):
    id: str
    site_id: str
    alert_id: str | None = None
    rationale: str
    steps: list[MissionStep]
    feasibility: Feasibility
    status: MissionStatus = MissionStatus.PROPOSED
    created_ts: datetime
    decided_ts: datetime | None = None
    decided_by: str | None = None
    outcome: str | None = None
    # Set when the new vantage point changed what we believed — the closed loop.
    confidence_delta: float | None = None
