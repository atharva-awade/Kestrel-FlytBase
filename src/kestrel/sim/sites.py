"""Site definitions — the monitored properties and the wider fleet.

The flagship site is an industrial plant in the Chakan MIDC belt north of Pune,
which is both a real manufacturing corridor and FlytBase's home region. Zone
geometry is authored in metres relative to a site origin and projected to WGS-84,
because reasoning about "the gate is 40 m south of the yard" is tractable and
reasoning about decimal degrees is not.

Only sites with ``live_footage=True`` run real video through the real pipeline.
Every other site is driven by the seeded fleet generator and is labelled SIMULATED
wherever it appears. That distinction is load-bearing: the portfolio view exists to
demonstrate that the architecture scales, not to imply a fleet we do not have.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from kestrel.domain import LatLon, Site, Zone, ZoneKind

# Metres per degree at the equator; latitude scaling applied per-site.
_M_PER_DEG_LAT = 111_320.0


def offset(origin: LatLon, east_m: float, north_m: float) -> LatLon:
    """Project a local metric offset onto WGS-84.

    Equirectangular approximation. Error is well under a metre across a few
    hundred metres of site, which is far below detection-projection accuracy.
    """
    dlat = north_m / _M_PER_DEG_LAT
    dlon = east_m / (_M_PER_DEG_LAT * math.cos(math.radians(origin.lat)))
    return LatLon(lat=round(origin.lat + dlat, 8), lon=round(origin.lon + dlon, 8))


def rect(origin: LatLon, e0: float, n0: float, e1: float, n1: float) -> list[LatLon]:
    """Axis-aligned rectangle from two metric corners, counter-clockwise."""
    return [
        offset(origin, e0, n0),
        offset(origin, e1, n0),
        offset(origin, e1, n1),
        offset(origin, e0, n1),
    ]


def ring(origin: LatLon, e0: float, n0: float, e1: float, n1: float, band: float) -> list[LatLon]:
    """A closed band just inside a rectangle — used for fence-line zones.

    Modelled as the outer rectangle traced then stepped inward, which gives a
    polygon that a point on the fence falls inside but a point in the middle of
    the yard does not.
    """
    o = rect(origin, e0, n0, e1, n1)
    i = rect(origin, e0 + band, n0 + band, e1 - band, n1 - band)
    return [*o, o[0], *reversed(i), i[-1]]


# ═══════════════════════════════════════════════════════════════════════════════
def build_plant_01() -> Site:
    """PLANT-01 — Chakan industrial plant, the flagship live-footage site.

    Layout (metres east/north of the south-west corner):

        N ▲   ┌──────────────── fence line ────────────────┐
          │   │  substation          yard                  │
          │   │  (300,180)        (150..320, 90..200)      │
          │   │                                            │
          │   │  warehouse-a        warehouse-b            │
          │   │  (40..150,90..170)  (170..280,90..170)     │
          │   │                                            │
          │   │  loading dock    [DOCK]      parking       │
          │   │  (30..140,20..80)  (200,45)  (250..360,20..70)
          │   └──────────── main gate (150..200, 0) ───────┘
          └────────────────────────────────────────────────▶ E
    """
    origin = LatLon(lat=18.75820, lon=73.85940)

    zones = [
        Zone(
            id="main-gate",
            name="Main Gate",
            kind=ZoneKind.GATE,
            polygon=rect(origin, 140, -8, 210, 30),
            priority=1.6,
            normal_hours=(6, 21),
            notes="Sole vehicle entry. Manned 06:00-21:00; unmanned overnight.",
        ),
        Zone(
            id="loading-dock",
            name="Loading Dock",
            kind=ZoneKind.DOCK,
            polygon=rect(origin, 25, 18, 145, 82),
            priority=1.4,
            normal_hours=(7, 19),
            notes="Six bays. Scheduled deliveries only outside 07:00-19:00.",
        ),
        # The yard sits north of the warehouses rather than around them. Zones must
        # not overlap except where nesting is deliberate (substation ⊃ restricted
        # core), because `zone_at` resolves ties by smallest area and an accidental
        # overlap silently mislabels every detection in it.
        Zone(
            id="yard",
            name="Storage Yard",
            kind=ZoneKind.YARD,
            polygon=rect(origin, 30, 182, 280, 238),
            priority=1.2,
            normal_hours=(6, 22),
            notes="Open container and pallet storage.",
        ),
        Zone(
            id="warehouse-a",
            name="Warehouse A",
            kind=ZoneKind.BUILDING,
            polygon=rect(origin, 38, 96, 152, 172),
            priority=1.0,
        ),
        Zone(
            id="warehouse-b",
            name="Warehouse B",
            kind=ZoneKind.BUILDING,
            polygon=rect(origin, 168, 96, 282, 172),
            priority=1.0,
        ),
        Zone(
            id="substation",
            name="Electrical Substation",
            kind=ZoneKind.SUBSTATION,
            polygon=rect(origin, 296, 176, 368, 228),
            priority=2.2,
            normal_hours=(9, 17),
            notes="High-value copper. Access by permit only, so any presence is notable.",
        ),
        Zone(
            id="parking",
            name="Staff Parking",
            kind=ZoneKind.PARKING,
            polygon=rect(origin, 245, 16, 362, 74),
            priority=0.7,
            normal_hours=(6, 22),
        ),
        Zone(
            id="access-road",
            name="Access Road",
            kind=ZoneKind.ROAD,
            polygon=rect(origin, 145, 28, 205, 92),
            priority=0.9,
        ),
        Zone(
            id="fence-line",
            name="Perimeter Fence",
            kind=ZoneKind.FENCE,
            polygon=ring(origin, -12, -12, 392, 250, 10),
            priority=2.0,
            notes="3 m palisade. Any dwell here is anomalous at any hour.",
        ),
        # Deliberately nested inside the substation: `zone_at` prefers the smaller
        # polygon, so a detection here resolves to the restricted core and inherits
        # its higher priority.
        Zone(
            id="restricted-core",
            name="Restricted Core",
            kind=ZoneKind.RESTRICTED,
            polygon=rect(origin, 304, 184, 360, 220),
            priority=2.5,
            notes="Substation interior. Escalate immediately on any human presence.",
        ),
    ]

    return Site(
        id="plant-01",
        name="Chakan Industrial Plant",
        kind="industrial",
        origin=origin,
        timezone="Asia/Kolkata",
        country="IN",
        country_name="India",
        zones=zones,
        dock=offset(origin, 200, 45),
        geofence=rect(origin, -30, -30, 410, 268),
        max_altitude_m=120.0,
        live_footage=True,
        notes="Flagship site. Real footage runs the full perception cascade here.",
    )


# ═══════════════════════════════════════════════════════════════════════════════
def _generic_site(
    site_id: str,
    name: str,
    lat: float,
    lon: float,
    kind: str,
    tz: str,
    country: str,
    country_name: str,
) -> Site:
    """A simulated fleet site with a plausible generic layout.

    Deliberately simpler than PLANT-01: these exist to populate the portfolio view
    and to exercise cross-site correlation, not to pretend at survey accuracy.
    """
    o = LatLon(lat=lat, lon=lon)
    zones = [
        Zone(id="main-gate", name="Main Gate", kind=ZoneKind.GATE,
             polygon=rect(o, 120, -8, 190, 28), priority=1.6, normal_hours=(6, 21)),
        Zone(id="yard", name="Yard", kind=ZoneKind.YARD,
             polygon=rect(o, 140, 80, 300, 190), priority=1.2, normal_hours=(6, 22)),
        Zone(id="loading-dock", name="Loading Dock", kind=ZoneKind.DOCK,
             polygon=rect(o, 25, 18, 140, 78), priority=1.4, normal_hours=(7, 19)),
        Zone(id="substation", name="Substation", kind=ZoneKind.SUBSTATION,
             polygon=rect(o, 275, 165, 340, 215), priority=2.2, normal_hours=(9, 17)),
        Zone(id="fence-line", name="Perimeter Fence", kind=ZoneKind.FENCE,
             polygon=ring(o, -12, -12, 375, 240, 10), priority=2.0),
    ]
    return Site(
        id=site_id, name=name, kind=kind, origin=o, timezone=tz,
        country=country, country_name=country_name, zones=zones,
        dock=offset(o, 190, 42), geofence=rect(o, -30, -30, 395, 258),
        live_footage=False,
        notes="Simulated fleet site: seeded event generator, no live footage.",
    )


# The portfolio. Geographic spread is intentional: the globe view is only
# meaningful if the fleet actually spans regions, and cross-site correlation is
# only interesting when sites are far enough apart that a shared visitor is odd.
FLEET_SPEC: list[tuple[str, str, float, float, str, str, str, str]] = [
    ("plant-02", "Sanand Auto Plant", 22.98700, 72.38200, "industrial", "Asia/Kolkata", "IN", "India"),
    ("plant-03", "Sriperumbudur Works", 12.96500, 79.94300, "industrial", "Asia/Kolkata", "IN", "India"),
    ("solar-01", "Bhadla Solar Park North", 27.53900, 71.91200, "solar", "Asia/Kolkata", "IN", "India"),
    ("port-01", "Mundra Port Terminal 3", 22.83900, 69.72800, "logistics", "Asia/Kolkata", "IN", "India"),
    ("dc-01", "Navi Mumbai Data Centre", 19.04100, 73.02800, "datacentre", "Asia/Kolkata", "IN", "India"),
    ("plant-04", "Rayong Assembly", 12.68100, 101.25600, "industrial", "Asia/Bangkok", "TH", "Thailand"),
    ("plant-05", "Selangor Logistics Hub", 3.05600, 101.44700, "logistics", "Asia/Kuala_Lumpur", "MY", "Malaysia"),
    ("solar-02", "Al Dhafra Solar", 24.05200, 54.13800, "solar", "Asia/Dubai", "AE", "UAE"),
    ("plant-06", "Rotterdam Chemical Park", 51.88500, 4.29100, "industrial", "Europe/Amsterdam", "NL", "Netherlands"),
    ("dc-02", "Dublin Data Campus", 53.42600, -6.24500, "datacentre", "Europe/Dublin", "IE", "Ireland"),
    ("plant-07", "Querétaro Aerospace", 20.61700, -100.18500, "industrial", "America/Mexico_City", "MX", "Mexico"),
    ("plant-08", "Laredo Distribution", 27.56100, -99.48700, "logistics", "America/Chicago", "US", "United States"),
    ("solar-03", "Mojave Solar Field", 35.01300, -117.55200, "solar", "America/Los_Angeles", "US", "United States"),
    ("port-02", "Santos Terminal 9", -23.95400, -46.33300, "logistics", "America/Sao_Paulo", "BR", "Brazil"),
    ("plant-09", "Gqeberha Assembly", -33.87600, 25.60100, "industrial", "Africa/Johannesburg", "ZA", "South Africa"),
]


def build_fleet() -> list[Site]:
    """The full portfolio: the live site first, then the simulated fleet."""
    return [build_plant_01()] + [_generic_site(*spec) for spec in FLEET_SPEC]


# ═══════════════════════════════════════════════════════════════════════════════
def write_sites(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    fleet = build_fleet()
    for site in fleet:
        p = out_dir / f"{site.id}.json"
        p.write_text(site.model_dump_json(indent=2), encoding="utf-8")
        written.append(p)
    index = out_dir / "index.json"
    index.write_text(
        json.dumps(
            {
                "sites": [
                    {
                        "id": s.id,
                        "name": s.name,
                        "kind": s.kind,
                        "lat": s.origin.lat,
                        "lon": s.origin.lon,
                        "country": s.country,
                        "country_name": s.country_name,
                        "timezone": s.timezone,
                        "zones": len(s.zones),
                        "live_footage": s.live_footage,
                    }
                    for s in fleet
                ]
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    written.append(index)
    return written


_CACHE: dict[str, Site] = {}


def load_site(site_id: str, sites_dir: Path | None = None) -> Site:
    """Load a site by id, preferring on-disk JSON so edits are picked up."""
    if site_id in _CACHE:
        return _CACHE[site_id]
    if sites_dir:
        p = Path(sites_dir) / f"{site_id}.json"
        if p.exists():
            site = Site.model_validate_json(p.read_text(encoding="utf-8"))
            _CACHE[site_id] = site
            return site
    site = next((s for s in build_fleet() if s.id == site_id), None)
    if site is None:
        raise KeyError(f"unknown site: {site_id}")
    _CACHE[site_id] = site
    return site


def load_fleet(sites_dir: Path | None = None) -> list[Site]:
    if sites_dir and Path(sites_dir).exists():
        files = sorted(Path(sites_dir).glob("*.json"))
        sites = [
            Site.model_validate_json(f.read_text(encoding="utf-8"))
            for f in files
            if f.name != "index.json"
        ]
        if sites:
            return sorted(sites, key=lambda s: (not s.live_footage, s.id))
    return build_fleet()
