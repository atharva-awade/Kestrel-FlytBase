"""Pixel → world projection.

Geometry code fails silently: a sign error still returns a plausible-looking
coordinate, and the only symptom is detections quietly attributed to the wrong
zone. So this is tested against cases whose answers are known analytically rather
than by comparing against the implementation's own output.
"""

from __future__ import annotations

import math

import pytest

from kestrel.domain import BBox, DroneState, Telemetry
from kestrel.perception.project import DEFAULT_HFOV_DEG, GroundProjector
from kestrel.sim.sites import build_plant_01

W, H = 960, 540


@pytest.fixture(scope="module")
def site():
    return build_plant_01()


@pytest.fixture(scope="module")
def projector(site):
    return GroundProjector(site)


def tel(site, *, alt=50.0, pitch=-90.0, yaw=0.0, lat=None, lon=None, sats=14, hdop=0.7):
    p = site.dock
    from datetime import datetime

    return Telemetry(
        ts=datetime(2026, 8, 6, 12, 0, 0),
        site_id=site.id,
        lat=lat if lat is not None else p.lat,
        lon=lon if lon is not None else p.lon,
        alt_m=alt,
        heading_deg=0.0,
        gimbal_pitch_deg=pitch,
        gimbal_yaw_deg=yaw,
        state=DroneState.ORBIT,
        gps_satellites=sats,
        gps_hdop=hdop,
    )


def centre_box() -> BBox:
    """A box whose ground-contact point is the exact image centre."""
    return BBox(x1=W / 2 - 10, y1=H / 2 - 20, x2=W / 2 + 10, y2=H / 2)


def test_nadir_centre_projects_directly_beneath(site, projector):
    """The single most important known answer: looking straight down, the centre
    pixel is the point directly below the aircraft."""
    t = tel(site, alt=50.0, pitch=-90.0)
    p = projector.project(centre_box(), t, W, H)
    assert p.world is not None
    assert p.world.haversine_m(t.position) < 0.5
    assert p.ground_range_m < 0.5


def test_nadir_offset_pixel_scales_with_altitude(site, projector):
    """Ground distance for a fixed pixel offset must be linear in altitude —
    doubling height doubles the footprint."""
    box = BBox(x1=W / 2 + 190, y1=H / 2 - 20, x2=W / 2 + 210, y2=H / 2)
    near = projector.project(box, tel(site, alt=50.0), W, H)
    far = projector.project(box, tel(site, alt=100.0), W, H)
    assert near.world and far.world
    ratio = far.ground_range_m / near.ground_range_m
    assert ratio == pytest.approx(2.0, rel=0.02)


def test_nadir_ground_range_matches_pinhole_geometry(site, projector):
    """Closed-form check: for a pixel offset dx from centre, ground offset is
    alt * dx / fx, with fx = (W/2) / tan(hfov/2)."""
    dx_px = 200.0
    alt = 60.0
    box = BBox(x1=W / 2 + dx_px - 10, y1=H / 2 - 20, x2=W / 2 + dx_px + 10, y2=H / 2)
    p = projector.project(box, tel(site, alt=alt), W, H)
    fx = (W / 2) / math.tan(math.radians(DEFAULT_HFOV_DEG) / 2)
    assert p.ground_range_m == pytest.approx(alt * dx_px / fx, rel=0.02)


def test_yaw_rotates_the_projected_point(site, projector):
    """A pixel to the image right must land east at yaw 0 and south at yaw 90."""
    box = BBox(x1=W / 2 + 190, y1=H / 2 - 20, x2=W / 2 + 210, y2=H / 2)
    t0 = tel(site, alt=50.0, yaw=0.0)
    t90 = tel(site, alt=50.0, yaw=90.0)
    p0 = projector.project(box, t0, W, H)
    p90 = projector.project(box, t90, W, H)
    assert p0.world and p90.world

    # yaw 0 → displacement is essentially pure east.
    assert p0.world.lon > t0.lon
    assert abs(p0.world.lat - t0.lat) < abs(p0.world.lon - t0.lon) * 0.2

    # yaw 90 → the same pixel now points south.
    assert p90.world.lat < t90.lat
    assert abs(p90.world.lon - t90.lon) < abs(p90.world.lat - t90.lat) * 0.2

    # Rotating the camera must not change how far away the point is.
    assert p0.ground_range_m == pytest.approx(p90.ground_range_m, rel=0.01)


def test_ray_above_horizon_is_refused(site, projector):
    """A shallow gimbal with the point high in frame never meets the ground.
    Returning a coordinate anyway would be a fabricated position."""
    t = tel(site, alt=40.0, pitch=-10.0)
    high = BBox(x1=W / 2 - 10, y1=2, x2=W / 2 + 10, y2=6)
    p = projector.project(high, t, W, H)
    assert p.world is None
    assert "horizon" in p.reason


def test_not_airborne_is_refused(site, projector):
    p = projector.project(centre_box(), tel(site, alt=0.0), W, H)
    assert p.world is None
    assert p.confidence == 0.0


def test_confidence_is_highest_at_nadir(site, projector):
    """Obliquity should dominate the confidence model."""
    box = centre_box()
    nadir = projector.project(box, tel(site, alt=50, pitch=-90), W, H)
    oblique = projector.project(box, tel(site, alt=50, pitch=-30), W, H)
    assert nadir.world and oblique.world
    assert nadir.confidence > oblique.confidence


def test_degraded_gps_lowers_confidence(site, projector):
    good = projector.project(centre_box(), tel(site, sats=16, hdop=0.6), W, H)
    bad = projector.project(centre_box(), tel(site, sats=5, hdop=3.4), W, H)
    assert bad.confidence < good.confidence


def test_projection_resolves_to_the_expected_zone(site, projector):
    """End-to-end: hover over the substation, and a centre detection must be
    attributed to the substation — this is the whole point of the module."""
    sub = site.zone_by_id("substation")
    t = tel(site, alt=45.0, pitch=-90.0, lat=sub.centroid.lat, lon=sub.centroid.lon)
    p = projector.project(centre_box(), t, W, H)
    assert p.world is not None
    # Nested restricted core is an acceptable, more specific answer.
    assert p.zone_id in {"substation", "restricted-core"}


def test_footprint_is_a_quadrilateral_containing_nadir(site, projector):
    from kestrel.domain import Zone, ZoneKind

    t = tel(site, alt=60.0, pitch=-90.0)
    pts = projector.footprint(t, W, H)
    assert len(pts) == 4
    poly = Zone(id="fp", name="fp", kind=ZoneKind.PERIMETER, polygon=pts)
    assert poly.contains(t.position), "nadir point must lie inside the camera footprint"


def test_object_further_down_the_frame_is_nearer_the_drone(site, projector):
    """With a forward-tilted camera, lower in the image means closer to the
    aircraft. Getting this backwards would invert every distance estimate."""
    t = tel(site, alt=50.0, pitch=-45.0)
    lower = BBox(x1=W / 2 - 10, y1=H * 0.80, x2=W / 2 + 10, y2=H * 0.88)
    upper = BBox(x1=W / 2 - 10, y1=H * 0.52, x2=W / 2 + 10, y2=H * 0.58)
    p_low = projector.project(lower, t, W, H)
    p_up = projector.project(upper, t, W, H)
    assert p_low.world and p_up.world
    assert p_low.ground_range_m < p_up.ground_range_m
