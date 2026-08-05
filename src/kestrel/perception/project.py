"""Pixel → world projection.

A bounding box says an object is at (412, 260) in a 960×540 image. A security
operator needs to know it is *at the substation*. Bridging those is what turns a
computer-vision demo into something that can be reasoned about spatially — zone
membership, dwell in a named place, distance to the fence, a pin on a map.

The method is a flat-ground pinhole projection: telemetry gives the camera pose
(position, altitude, heading, gimbal angles), the ground is assumed planar at the
site's elevation, and each pixel ray is intersected with that plane.

**Where this is approximate, stated honestly:**

*   Flat ground. On a real site with a raised loading dock or a slope, objects on
    elevated surfaces project long. A DEM would fix this; for a yard it is minor.
*   The camera intrinsics are assumed from a nominal field of view rather than
    calibrated, because there is no physical camera to calibrate against.
*   The object's ground-contact point is taken as the box's bottom-centre, which
    is right for things standing on the ground and wrong for anything airborne.

Each projection therefore carries a confidence that degrades with obliquity,
altitude and GPS quality, and the pipeline treats a low-confidence position as a
weaker piece of evidence rather than a fact.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from kestrel.domain import BBox, LatLon, Site, Telemetry

# Nominal horizontal field of view for a typical drone payload. Not calibrated —
# see the module docstring.
DEFAULT_HFOV_DEG = 73.0

_M_PER_DEG_LAT = 111_320.0


@dataclass(slots=True)
class Projection:
    world: LatLon | None
    zone_id: str | None
    confidence: float
    ground_range_m: float
    reason: str = ""


class GroundProjector:
    """Projects image points onto the site ground plane using telemetry."""

    def __init__(self, site: Site, hfov_deg: float = DEFAULT_HFOV_DEG) -> None:
        self.site = site
        self.hfov = math.radians(hfov_deg)

    def project(
        self,
        bbox: BBox,
        telemetry: Telemetry,
        image_w: int,
        image_h: int,
    ) -> Projection:
        if image_w <= 0 or image_h <= 0:
            return Projection(None, None, 0.0, 0.0, "no image dimensions")
        if telemetry.alt_m < 1.0:
            # On the pad the camera sees no ground plane worth projecting to.
            return Projection(None, None, 0.0, 0.0, "drone not airborne")

        # ── camera intrinsics from the assumed FOV ───────────────────────
        fx = (image_w / 2) / math.tan(self.hfov / 2)
        fy = fx  # square pixels

        # ── the ground-contact point ─────────────────────────────────────
        px, py = bbox.foot
        # Offsets from principal point, in camera frame. y grows downward in an
        # image but upward in the camera's optical frame, hence the negation.
        dx = (px - image_w / 2) / fx
        dy = -(py - image_h / 2) / fy

        # ── rotate the ray by the gimbal ─────────────────────────────────
        # Camera frame: +X right, +Y up, +Z forward along the optical axis.
        # Telemetry reports gimbal pitch as negative-down (-90 = nadir), so the
        # tilt-down angle is its negation.
        tilt = math.radians(-telemetry.gimbal_pitch_deg)
        ct, st = math.cos(tilt), math.sin(tilt)

        # Rotation about X that tilts +Z toward the ground. At tilt = 90° the
        # optical axis (0,0,1) maps to (0,-1,0) — straight down, as required.
        rx = dx
        ry = dy * ct - 1.0 * st
        rz = dy * st + 1.0 * ct

        # +Y is up, so the downward component is -ry. A ray at or above the
        # horizon never meets the ground plane.
        down = -ry
        if down <= 1e-4:
            return Projection(None, None, 0.0, 0.0, "ray above horizon")

        scale = telemetry.alt_m / down
        east_cam = rx * scale     # camera-right
        north_cam = rz * scale    # camera-forward
        ground_range = math.hypot(east_cam, north_cam)

        # ── rotate into world frame by the gimbal yaw ────────────────────
        yaw = math.radians(telemetry.gimbal_yaw_deg)
        cy, sy = math.cos(yaw), math.sin(yaw)
        east = east_cam * cy + north_cam * sy
        north = -east_cam * sy + north_cam * cy

        # ── offset from the drone's own position ─────────────────────────
        lat = telemetry.lat + (north / _M_PER_DEG_LAT)
        lon = telemetry.lon + (
            east / (_M_PER_DEG_LAT * math.cos(math.radians(telemetry.lat)))
        )
        world = LatLon(lat=round(lat, 8), lon=round(lon, 8))

        conf, why = self._confidence(telemetry, ground_range, tilt)
        zone = self.site.zone_at(world)
        return Projection(world, zone.id if zone else None, conf, ground_range, why)

    def _confidence(
        self, telemetry: Telemetry, ground_range_m: float, tilt_rad: float
    ) -> tuple[float, str]:
        """How much to trust this position.

        Obliquity dominates: near nadir a small pointing error moves the projected
        point a little, but at a shallow angle the same error moves it a long way.
        """
        notes: list[str] = []

        # cos(tilt) is 0 at nadir (tilt = 90°) and → 1 toward the horizon.
        obliquity = abs(math.cos(tilt_rad))
        oblique_term = max(0.15, 1.0 - obliquity * 1.4)
        if obliquity > 0.5:
            notes.append("oblique view")

        # Error grows with how far the point is from directly beneath the drone.
        range_term = 1.0 / (1.0 + (ground_range_m / max(1.0, telemetry.alt_m)) * 0.35)
        if ground_range_m > telemetry.alt_m * 2:
            notes.append("far from nadir")

        gps_term = 1.0 if telemetry.gps_ok else 0.55
        if not telemetry.gps_ok:
            notes.append("degraded GPS")

        # Inherit the optical confidence — a position derived from a blurry box is
        # not more reliable than the box.
        optical = telemetry.perception_confidence

        conf = oblique_term * 0.4 + range_term * 0.25 + gps_term * 0.15 + optical * 0.2
        return round(max(0.05, min(1.0, conf)), 3), ", ".join(notes)

    def footprint(self, telemetry: Telemetry, image_w: int, image_h: int) -> list[LatLon]:
        """The ground quadrilateral the camera currently sees.

        Drawn on the map as the drone's live field of view, which makes it obvious
        why detections appear where they do.
        """
        corners = [(0, 0), (image_w, 0), (image_w, image_h), (0, image_h)]
        pts: list[LatLon] = []
        for cx, cy in corners:
            p = self.project(
                BBox(x1=cx, y1=cy, x2=cx, y2=cy), telemetry, image_w, image_h
            )
            if p.world is not None:
                pts.append(p.world)
        return pts
