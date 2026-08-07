"""Telemetry simulation for a docked patrol drone.

The video is real; the telemetry is not, because there is no actual aircraft. This
module is therefore written to be *honest and useful* rather than merely plausible:
every quantity it emits is one the downstream pipeline genuinely consumes.

    altitude + gimbal   → pixel→world projection (perception/project.py)
    illuminance         → perception confidence, and night-time rule windows
    speed               → motion-blur term in perception confidence
    battery             → mission feasibility; a proposal that cannot fly is refused
    GPS quality         → degrades projection confidence, drives a chaos scenario
    wind                → gimbal jitter term, and a mission blocker above threshold

Everything is seeded, so a given site and clock time always produce the same flight.
Determinism matters here for the same reason it matters for cassettes: a demo and a
test must both be repeatable.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterator
from datetime import datetime, timedelta

from kestrel.domain import DroneState, LatLon, Site, Telemetry
from kestrel.sim.sites import offset

# Sunrise/sunset used for the illuminance curve. Fixed rather than computed from
# an ephemeris: the site set spans latitudes where a real solar model would be
# better, but the pipeline only needs day/dusk/night to be coherent.
SUNRISE_H = 6.2
SUNSET_H = 18.8


def illuminance_at(hour_float: float, cloud: float = 0.15) -> float:
    """Approximate horizontal illuminance in lux for a time of day.

    Full daylight is ~10-25 klux under cloud, civil twilight ~100-1000, and a lit
    industrial yard at night is ~5-20 lux. The pipeline treats <50 lux as night.
    """
    if SUNRISE_H <= hour_float <= SUNSET_H:
        noon = (SUNRISE_H + SUNSET_H) / 2
        span = (SUNSET_H - SUNRISE_H) / 2
        elevation = math.cos((hour_float - noon) / span * (math.pi / 2))
        return max(200.0, 26_000.0 * max(0.0, elevation) * (1.0 - 0.65 * cloud))
    # Twilight shoulders, then yard lighting overnight.
    edge = min(abs(hour_float - SUNRISE_H), abs(hour_float - SUNSET_H))
    if edge < 0.75:
        return max(12.0, 900.0 * (1.0 - edge / 0.75))
    return 9.0


class PatrolSimulator:
    """Flies a repeatable patrol and emits telemetry on a fixed cadence.

    The default behaviour is a docked drone that launches for a perimeter patrol,
    orbits points of interest, and returns to charge — the duty cycle of a real
    drone-in-a-box deployment, and the reason battery is a live constraint.
    """

    def __init__(
        self,
        site: Site,
        start: datetime,
        *,
        seed: int = 7,
        hz: float = 1.0,
        patrol_altitude_m: float = 45.0,
        cruise_mps: float = 8.0,
    ) -> None:
        self.site = site
        self.start = start
        self.hz = hz
        self.rng = random.Random(f"{site.id}:{seed}:{start.isoformat()}")
        self.patrol_altitude = patrol_altitude_m
        self.cruise = cruise_mps

        self.dock = site.dock or site.origin
        self.waypoints = self._build_route()
        self._cloud = self.rng.uniform(0.05, 0.45)
        self._wind_base = self.rng.uniform(1.0, 5.5)
        self._battery = 100.0
        self._t = 0.0

    # ── route ────────────────────────────────────────────────────────────
    def _build_route(self) -> list[tuple[LatLon, str, float]]:
        """(position, zone_id, dwell_seconds) — high-priority zones get longer looks."""
        route: list[tuple[LatLon, str, float]] = []
        ordered = sorted(self.site.zones, key=lambda z: -z.priority)
        for z in ordered:
            if z.kind.value in ("building", "road"):
                continue
            dwell = 20.0 + 25.0 * min(2.0, z.priority)
            route.append((z.centroid, z.id, dwell))
        if not route:
            route = [(self.dock, "dock", 30.0)]
        return route

    # ── per-sample state ─────────────────────────────────────────────────
    def _phase(self, t: float) -> tuple[LatLon, float, DroneState, float]:
        """Position, altitude, state and speed at elapsed time ``t``.

        The cycle is: launch → visit each waypoint (transit, then orbit) → return
        → charge. Charging is not skipped; a drone that never recharges would make
        the battery constraint meaningless.
        """
        launch_s, land_s, charge_s = 25.0, 25.0, 240.0

        legs: list[tuple[float, str, LatLon, LatLon, float]] = []
        cursor = 0.0
        prev = self.dock
        for pos, zone_id, dwell in self.waypoints:
            dist = prev.haversine_m(pos)
            transit = max(6.0, dist / self.cruise)
            legs.append((cursor, f"transit:{zone_id}", prev, pos, transit))
            cursor += transit
            legs.append((cursor, f"orbit:{zone_id}", pos, pos, dwell))
            cursor += dwell
            prev = pos
        back = max(8.0, prev.haversine_m(self.dock) / self.cruise)
        legs.append((cursor, "return", prev, self.dock, back))
        cursor += back

        total = launch_s + cursor + land_s + charge_s
        tc = t % total

        if tc < launch_s:
            frac = tc / launch_s
            return self.dock, self.patrol_altitude * frac, DroneState.LAUNCHING, 2.0

        tc -= launch_s
        if tc < cursor:
            for begin, tag, a, b, dur in legs:
                if begin <= tc < begin + dur:
                    frac = (tc - begin) / max(dur, 1e-6)
                    if tag.startswith("orbit"):
                        # Circle the point of interest at 18 m radius.
                        ang = frac * 2 * math.pi
                        pos = offset(a, 18 * math.cos(ang), 18 * math.sin(ang))
                        return pos, self.patrol_altitude, DroneState.ORBIT, 3.5
                    pos = LatLon(
                        lat=a.lat + (b.lat - a.lat) * frac,
                        lon=a.lon + (b.lon - a.lon) * frac,
                    )
                    state = DroneState.RETURNING if tag == "return" else DroneState.TRANSIT
                    return pos, self.patrol_altitude, state, self.cruise

        tc -= cursor
        if tc < land_s:
            frac = tc / land_s
            return self.dock, self.patrol_altitude * (1 - frac), DroneState.LANDING, 1.5

        return self.dock, 0.0, DroneState.CHARGING, 0.0

    def sample(self, t_seconds: float) -> Telemetry:
        ts = self.start + timedelta(seconds=t_seconds)
        pos, alt, state, speed = self._phase(t_seconds)

        hour = ts.hour + ts.minute / 60 + ts.second / 3600
        lux = illuminance_at(hour, self._cloud)

        # Battery: discharge in flight, recharge on the pad. Rates are chosen so a
        # patrol cycle is survivable but not free, which is what makes the mission
        # feasibility check meaningful rather than always-yes.
        dt = 1.0 / self.hz
        if state is DroneState.CHARGING:
            self._battery = min(100.0, self._battery + 0.09 * dt)
        else:
            drain = 0.020 if state in (DroneState.ORBIT, DroneState.HOVER) else 0.028
            self._battery = max(4.0, self._battery - drain * dt)

        # Wind: slow diurnal swell plus gusts. Gusts above ~11 m/s block missions.
        wind = (
            self._wind_base
            + 2.4 * math.sin(t_seconds / 420.0)
            + self.rng.gauss(0, 0.5)
        )
        gust = 5.5 if self.rng.random() < 0.015 else 0.0
        wind = max(0.0, wind + gust)

        # GPS quality dips occasionally near structures — this feeds a chaos test
        # and demonstrates that projection confidence responds to it.
        degraded = self.rng.random() < 0.02
        sats = self.rng.randint(5, 7) if degraded else self.rng.randint(11, 18)
        hdop = round(self.rng.uniform(2.2, 4.0) if degraded else self.rng.uniform(0.5, 1.2), 2)

        # Gimbal: nadir while orbiting a target, shallower while transiting.
        pitch = -90.0 if state is DroneState.ORBIT else -55.0 + self.rng.gauss(0, 3)
        bearing = self._bearing_to_centre(pos)

        return Telemetry(
            ts=ts,
            site_id=self.site.id,
            lat=pos.lat,
            lon=pos.lon,
            alt_m=round(alt, 2),
            heading_deg=round(bearing % 360, 1),
            gimbal_pitch_deg=round(max(-90.0, min(-10.0, pitch)), 1),
            gimbal_yaw_deg=round((bearing + self.rng.gauss(0, 2)) % 360, 1),
            speed_mps=round(max(0.0, speed + self.rng.gauss(0, 0.35)), 2),
            battery_pct=round(self._battery, 2),
            gps_satellites=sats,
            gps_hdop=hdop,
            wind_mps=round(wind, 2),
            illuminance_lux=round(lux, 1),
            state=state,
            signal_pct=round(max(45.0, 99.0 - self.rng.random() * 12), 1),
        )

    def _bearing_to_centre(self, pos: LatLon) -> float:
        c = self.site.origin
        dy = c.lat - pos.lat
        dx = (c.lon - pos.lon) * math.cos(math.radians(pos.lat))
        return math.degrees(math.atan2(dx, dy))

    def stream(self, duration_s: float) -> Iterator[Telemetry]:
        step = 1.0 / self.hz
        t = 0.0
        while t < duration_s:
            yield self.sample(t)
            t += step

    def at(self, ts: datetime) -> Telemetry:
        """Telemetry for an absolute timestamp — used to join against frames."""
        return self.sample((ts - self.start).total_seconds())
