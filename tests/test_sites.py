"""Site geometry.

Zone resolution is load-bearing: every detection is labelled with the zone it fell
in, and rules key off those labels. A silent overlap between two zones mislabels
every detection inside it and there is no downstream symptom that points back here
— so the invariants are asserted rather than assumed.
"""

from __future__ import annotations

import itertools

import pytest

from kestrel.domain import ZoneKind
from kestrel.sim.sites import build_fleet, build_plant_01, offset, rect

# The one intentional nesting on this site. Anything else overlapping is a bug.
ALLOWED_OVERLAPS = {frozenset({"substation", "restricted-core"})}


@pytest.fixture(scope="module")
def site():
    return build_plant_01()


def _bbox(zone):
    lats = [p.lat for p in zone.polygon]
    lons = [p.lon for p in zone.polygon]
    return min(lons), min(lats), max(lons), max(lats)


def _bbox_overlap(a, b) -> bool:
    ax1, ay1, ax2, ay2 = _bbox(a)
    bx1, by1, bx2, by2 = _bbox(b)
    return not (ax2 <= bx1 or bx2 <= ax1 or ay2 <= by1 or by2 <= ay1)


def test_every_zone_contains_its_own_centroid(site):
    """A polygon that fails this is malformed — self-intersecting or wound wrong."""
    for z in site.zones:
        if z.kind is ZoneKind.FENCE:
            continue  # a band's centroid is legitimately in the hole
        assert z.contains(z.centroid), f"{z.id} does not contain its own centroid"


def test_zone_centroid_resolves_to_itself(site):
    """The resolution a detection will actually get."""
    for z in site.zones:
        if z.kind is ZoneKind.FENCE:
            continue
        hit = site.zone_at(z.centroid)
        assert hit is not None, f"{z.id} centroid resolved to no zone"
        if frozenset({z.id, hit.id}) in ALLOWED_OVERLAPS:
            continue
        assert hit.id == z.id, f"{z.id} centroid resolved to {hit.id}"


def test_no_unintended_zone_overlap(site):
    """Catches the class of bug where a big zone silently swallows a small one."""
    areas = {z.id: z for z in site.zones if z.kind is not ZoneKind.FENCE}
    for a, b in itertools.combinations(areas.values(), 2):
        if frozenset({a.id, b.id}) in ALLOWED_OVERLAPS:
            continue
        if not _bbox_overlap(a, b):
            continue
        # Bounding boxes touch; check whether either centroid lands in the other.
        assert not b.contains(a.centroid), f"{a.id} centroid falls inside {b.id}"
        assert not a.contains(b.centroid), f"{b.id} centroid falls inside {a.id}"


def test_restricted_core_nests_inside_substation(site):
    """The nesting is deliberate and rules depend on it resolving inward."""
    core = site.zone_by_id("restricted-core")
    sub = site.zone_by_id("substation")
    assert sub.contains(core.centroid)
    assert site.zone_at(core.centroid).id == "restricted-core"
    assert core.priority > sub.priority


def test_fence_band_excludes_site_interior(site):
    """A fence zone that contains the whole yard would alert on all normal work."""
    fence = site.zone_by_id("fence-line")
    for zid in ("yard", "warehouse-a", "loading-dock", "parking"):
        z = site.zone_by_id(zid)
        assert not fence.contains(z.centroid), f"fence band wrongly contains {zid}"


def test_fence_band_contains_a_point_on_the_fence(site):
    """...but it must still contain the perimeter itself."""
    fence = site.zone_by_id("fence-line")
    on_fence = offset(site.origin, -7, 120)  # inside the -12..-2 west band
    assert fence.contains(on_fence)


def test_dock_and_geofence(site):
    assert site.dock is not None
    poly = rect(site.origin, -30, -30, 410, 268)
    assert len(site.geofence) == len(poly)
    from kestrel.domain import Zone

    fence_zone = Zone(id="gf", name="gf", kind=ZoneKind.PERIMETER, polygon=site.geofence)
    assert fence_zone.contains(site.dock), "dock must sit inside the geofence"


def test_point_outside_site_resolves_to_nothing(site):
    assert site.zone_at(offset(site.origin, 5_000, 5_000)) is None


def test_fleet_is_geographically_distinct():
    """The globe is only meaningful if sites are genuinely spread out, and
    cross-site correlation is only interesting if they are far apart."""
    fleet = build_fleet()
    assert len(fleet) >= 10
    for a, b in itertools.combinations(fleet, 2):
        assert a.id != b.id
        assert a.origin.haversine_m(b.origin) > 50_000


def test_only_the_flagship_claims_live_footage():
    """We must never imply more live drones than exist."""
    fleet = build_fleet()
    live = [s.id for s in fleet if s.live_footage]
    assert live == ["plant-01"]


def test_all_fleet_sites_have_a_dock_and_zones():
    for s in build_fleet():
        assert s.dock is not None, f"{s.id} has no dock"
        assert len(s.zones) >= 4, f"{s.id} has too few zones"
