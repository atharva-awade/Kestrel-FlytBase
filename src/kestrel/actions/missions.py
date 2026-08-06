"""Mission recommendation — closing the loop from perception to action.

An alert tells an operator something happened. It does not help them decide what to
do about it, and at 02:00 with one guard on shift that decision is the whole job.
KESTREL therefore proposes a concrete flight:

    LAUNCH → GOTO(fence-west) → ORBIT(r=20 m, alt=25 m) → TRACK(ENT-0043) → RTD

and then — this is the part that matters — **checks whether it can actually fly**
before offering it. Battery, geofence, wind, altitude ceiling and daylight are all
real constraints, and a recommendation that ignores them is worse than none: it
teaches the operator that the system's suggestions cannot be trusted.

Nothing here executes on its own. ``propose`` returns a proposal; a human approves
it; only then does the executor run. That boundary is enforced in
``agent/registry.py`` as a permission class, written to the audit ledger, and
asserted in tests. An agent that can launch an aircraft needs a permission model,
and the absence of one is not a detail to be added later.

The loop closes because the new vantage point feeds back: a closer, better-lit
frame of the same subject re-enters perception, and the resulting confidence delta
is recorded against the mission.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from kestrel.domain import (
    Alert,
    Feasibility,
    LatLon,
    Mission,
    MissionStatus,
    MissionStep,
    MissionStepKind,
    Severity,
    Site,
    Telemetry,
)

# ── flight model ─────────────────────────────────────────────────────────────
# Approximate figures for a mid-size commercial inspection drone. They are not a
# specific airframe's published numbers, and the report says so — but they are
# realistic enough that the feasibility check genuinely constrains the answer.
CRUISE_MPS = 12.0
CLIMB_MPS = 4.0
BATTERY_PER_SEC_CRUISE = 0.030   # %/s
BATTERY_PER_SEC_HOVER = 0.022
BATTERY_LAUNCH_COST = 1.5
BATTERY_LAND_COST = 1.2
# Never plan to land below this. Reserve is what separates a margin from a crash.
BATTERY_RESERVE_PCT = 22.0
MAX_WIND_MPS = 11.0
WIND_CAUTION_MPS = 8.0


@dataclass
class MissionPlan:
    steps: list[MissionStep]
    rationale: str
    target: LatLon | None
    target_zone: str | None


class MissionRecommender:
    """Proposes a response flight for an alert, and refuses when it cannot fly."""

    def __init__(self, site: Site) -> None:
        self.site = site
        self.dock = site.dock or site.origin

    # ── planning ─────────────────────────────────────────────────────────
    def plan_for(self, alert: Alert, telemetry: Telemetry | None = None) -> MissionPlan:
        """Choose a response shape from the alert's severity and kind.

        Deliberately a small rule table rather than a model call: the mapping from
        "fence breach" to "orbit and track" is domain knowledge, it is auditable,
        and it must not vary between runs.
        """
        zone = self.site.zone_by_id(alert.zone_id) if alert.zone_id else None

        # Fly to the geo-projected subject when we have one. Falling back to the
        # zone centroid can be tens of metres off — enough to orbit the wrong
        # corner of a yard — so the precise fix is preferred whenever the
        # projection was confident enough to be worth trusting.
        loc = alert.location
        if loc.navigable and loc.source == "geo-projection" and loc.confidence >= 0.35:
            target = LatLon(lat=loc.lat, lon=loc.lon)  # type: ignore[arg-type]
            target_note = f"projected subject position (±{loc.accuracy_m:.0f} m)"
        elif zone is not None:
            target = zone.centroid
            target_note = f"{zone.name} centroid"
        else:
            target = self.site.origin
            target_note = "site origin, no better fix available"

        entity = alert.entity_ids[0] if alert.entity_ids else None
        airborne = telemetry is not None and telemetry.state.airborne

        steps: list[MissionStep] = []
        if not airborne:
            steps.append(
                MissionStep(kind=MissionStepKind.LAUNCH, altitude_m=30.0,
                            note="Break dock and climb to transit altitude")
            )

        if alert.severity in (Severity.CRITICAL, Severity.HIGH):
            # Get close, get a stable look, and hold on the subject.
            steps += [
                MissionStep(kind=MissionStepKind.GOTO, target=target, zone_id=alert.zone_id,
                            altitude_m=30.0,
                            note=f"Transit to {zone.name if zone else 'the alert location'}"),
                MissionStep(kind=MissionStepKind.ORBIT, target=target, zone_id=alert.zone_id,
                            altitude_m=22.0, radius_m=20.0, duration_s=90.0,
                            note="Orbit for multi-angle identification"),
            ]
            if entity:
                steps.append(
                    MissionStep(kind=MissionStepKind.TRACK, entity_id=entity,
                                target=target, altitude_m=22.0, duration_s=120.0,
                                note=f"Maintain visual on {entity}")
                )
            rationale = (
                f"{alert.severity.value.upper()} severity at "
                f"{zone.name if zone else 'an unresolved location'}. A closer orbit "
                f"resolves identity and intent that the patrol altitude cannot, and "
                f"holding visual preserves the option to guide a responder.\n"
                f"Navigating to {target_note}: {target.lat:.6f}, {target.lon:.6f}"
                + (
                    f" · {loc.distance_from_dock_m:.0f} m from dock, bearing "
                    f"{loc.bearing_from_dock_deg:.0f}°, ETA {loc.eta_seconds:.0f}s"
                    if loc.distance_from_dock_m is not None else ""
                )
            )
        elif alert.severity is Severity.MEDIUM:
            steps += [
                MissionStep(kind=MissionStepKind.GOTO, target=target, zone_id=alert.zone_id,
                            altitude_m=35.0, note="Reposition for a closer look"),
                MissionStep(kind=MissionStepKind.HOVER, target=target, zone_id=alert.zone_id,
                            altitude_m=28.0, duration_s=60.0,
                            note="Hold for confirmation"),
            ]
            rationale = (
                f"Medium severity at {zone.name if zone else 'the alert location'}. "
                "A brief confirmation pass is proportionate: enough to resolve the "
                "ambiguity without committing the aircraft to a long hold."
            )
        else:
            steps.append(
                MissionStep(kind=MissionStepKind.SWEEP, target=target, zone_id=alert.zone_id,
                            altitude_m=45.0, duration_s=45.0,
                            note="Low-priority sweep on the next patrol pass")
            )
            rationale = (
                "Low severity. Folded into the routine patrol rather than "
                "interrupting it; the cost of an immediate response exceeds the risk."
            )

        steps.append(
            MissionStep(kind=MissionStepKind.RETURN, target=self.dock, altitude_m=30.0,
                        note="Return to dock and recharge")
        )
        return MissionPlan(steps, rationale, target, alert.zone_id)

    # ── feasibility ──────────────────────────────────────────────────────
    def assess(self, plan: MissionPlan, telemetry: Telemetry | None) -> Feasibility:
        """Can this actually fly, right now, with this aircraft?

        Blockers make the mission un-approvable; warnings let it through with the
        risk stated. The distinction matters — an operator should be allowed to
        launch into a stiff breeze if they judge it worth it, but should not be
        allowed to launch without the battery to get home.
        """
        blockers: list[str] = []
        warnings: list[str] = []

        start = telemetry.position if telemetry else self.dock
        battery = telemetry.battery_pct if telemetry else 100.0
        wind = telemetry.wind_mps if telemetry else 0.0

        # Distance and duration over the whole route.
        distance = 0.0
        duration = 0.0
        cursor = start
        for s in plan.steps:
            if s.kind is MissionStepKind.LAUNCH:
                duration += s.altitude_m / CLIMB_MPS
                continue
            if s.target is not None:
                leg = cursor.haversine_m(s.target)
                distance += leg
                duration += leg / CRUISE_MPS
                cursor = s.target
            duration += s.duration_s

        # Energy: transit plus station-keeping plus launch and landing overhead.
        hover_s = sum(
            s.duration_s for s in plan.steps
            if s.kind in (MissionStepKind.ORBIT, MissionStepKind.HOVER,
                          MissionStepKind.TRACK, MissionStepKind.SWEEP)
        )
        transit_s = max(0.0, duration - hover_s)
        energy = (
            transit_s * BATTERY_PER_SEC_CRUISE
            + hover_s * BATTERY_PER_SEC_HOVER
            + BATTERY_LAUNCH_COST
            + BATTERY_LAND_COST
        )

        if battery - energy < BATTERY_RESERVE_PCT:
            blockers.append(
                f"insufficient battery: needs {energy:.0f}% plus a "
                f"{BATTERY_RESERVE_PCT:.0f}% reserve, but only {battery:.0f}% is available"
            )
        elif battery - energy < BATTERY_RESERVE_PCT + 12:
            warnings.append(
                f"battery margin is thin, {battery - energy - BATTERY_RESERVE_PCT:.0f}% "
                "above reserve on completion"
            )

        # Geofence.
        within = True
        if self.site.geofence:
            from kestrel.domain import Zone, ZoneKind

            fence = Zone(id="geofence", name="geofence", kind=ZoneKind.PERIMETER,
                         polygon=self.site.geofence)
            for s in plan.steps:
                if s.target is not None and not fence.contains(s.target):
                    within = False
                    blockers.append(
                        f"waypoint for {s.kind.value} lies outside the site geofence"
                    )
                    break

        # Wind.
        wind_ok = wind <= MAX_WIND_MPS
        if not wind_ok:
            blockers.append(f"wind {wind:.1f} m/s exceeds the {MAX_WIND_MPS:.0f} m/s limit")
        elif wind >= WIND_CAUTION_MPS:
            warnings.append(f"wind {wind:.1f} m/s, expect degraded gimbal stability")

        # Altitude ceiling.
        peak = max((s.altitude_m for s in plan.steps), default=0.0)
        alt_ok = peak <= self.site.max_altitude_m
        if not alt_ok:
            blockers.append(
                f"planned altitude {peak:.0f} m exceeds the site ceiling "
                f"{self.site.max_altitude_m:.0f} m"
            )

        # Light. Not a blocker — night response is the main use case — but the
        # operator should know identification will be harder.
        daylight = telemetry is None or not telemetry.is_night
        if not daylight:
            warnings.append(
                "night conditions, identification confidence will be reduced; "
                "consider illumination if fitted"
            )
        if telemetry is not None and not telemetry.gps_ok:
            warnings.append(
                f"GPS degraded ({telemetry.gps_satellites} sats, HDOP "
                f"{telemetry.gps_hdop:.1f}), position hold may drift"
            )

        return Feasibility(
            feasible=not blockers,
            battery_required_pct=round(energy, 1),
            battery_available_pct=round(battery, 1),
            distance_m=round(distance, 1),
            duration_s=round(duration, 1),
            within_geofence=within,
            wind_ok=wind_ok,
            altitude_ok=alt_ok,
            daylight=daylight,
            blockers=blockers,
            warnings=warnings,
        )

    # ── proposal ─────────────────────────────────────────────────────────
    def propose(self, alert: Alert, telemetry: Telemetry | None = None) -> Mission:
        """Build a mission proposal. Never executes — approval is a separate act."""
        plan = self.plan_for(alert, telemetry)
        feas = self.assess(plan, telemetry)
        mid = "msn_" + hashlib.sha1(
            f"{alert.id}{alert.ts}{len(plan.steps)}".encode()
        ).hexdigest()[:12]

        rationale = plan.rationale
        if not feas.feasible:
            rationale += (
                "\n\nCANNOT FLY AS PLANNED: " + "; ".join(feas.blockers)
                + ". Recommend dispatching a ground responder instead."
            )
        elif feas.warnings:
            rationale += "\n\nProceed with caution: " + "; ".join(feas.warnings) + "."

        return Mission(
            id=mid,
            site_id=self.site.id,
            alert_id=alert.id,
            rationale=rationale,
            steps=plan.steps,
            feasibility=feas,
            status=(
                MissionStatus.AWAITING_APPROVAL if feas.feasible else MissionStatus.PROPOSED
            ),
            created_ts=datetime.now(UTC).replace(tzinfo=None),
        )


# ═══════════════════════════════════════════════════════════════════════════════
class MissionExecutor:
    """Flies an approved mission against the simulator.

    There is no aircraft, so this integrates the flight rather than commanding one.
    It exists to make the loop observable: the map shows the drone moving, the
    telemetry stream reflects the new pose, and the closer vantage point produces
    the frame that updates the alert's confidence.
    """

    def __init__(self, site: Site) -> None:
        self.site = site
        self.dock = site.dock or site.origin

    def simulate(
        self, mission: Mission, start: Telemetry, *, step_s: float = 2.0
    ) -> list[Telemetry]:
        """Integrate the flight into a telemetry track the UI can animate."""
        from kestrel.domain import DroneState

        track: list[Telemetry] = []
        t = start.model_copy(deep=True)
        clock = start.ts
        pos = start.position
        battery = start.battery_pct

        def emit(state: DroneState, alt: float, speed: float) -> None:
            nonlocal clock, battery
            drain = (
                BATTERY_PER_SEC_CRUISE if speed > 1 else BATTERY_PER_SEC_HOVER
            ) * step_s
            battery = max(0.0, battery - drain)
            clock = clock + timedelta(seconds=step_s)
            track.append(
                t.model_copy(
                    update={
                        "ts": clock, "lat": pos.lat, "lon": pos.lon, "alt_m": alt,
                        "battery_pct": round(battery, 2), "state": state,
                        "speed_mps": speed,
                        "gimbal_pitch_deg": -90.0 if speed < 1 else -55.0,
                    }
                )
            )

        for step in mission.steps:
            if step.kind is MissionStepKind.LAUNCH:
                for i in range(int(step.altitude_m / CLIMB_MPS / step_s) + 1):
                    emit(DroneState.LAUNCHING, min(step.altitude_m, i * CLIMB_MPS * step_s), 2.0)

            elif step.kind in (MissionStepKind.GOTO, MissionStepKind.RETURN):
                dest = step.target or self.dock
                legs = max(1, int(pos.haversine_m(dest) / CRUISE_MPS / step_s))
                a, b = pos, dest
                for i in range(1, legs + 1):
                    f = i / legs
                    pos = LatLon(lat=a.lat + (b.lat - a.lat) * f,
                                 lon=a.lon + (b.lon - a.lon) * f)
                    emit(
                        DroneState.RETURNING if step.kind is MissionStepKind.RETURN
                        else DroneState.TRANSIT,
                        step.altitude_m, CRUISE_MPS,
                    )

            elif step.kind in (MissionStepKind.ORBIT, MissionStepKind.SWEEP):
                centre = step.target or pos
                n = max(1, int(step.duration_s / step_s))
                from kestrel.sim.sites import offset

                for i in range(n):
                    ang = (i / n) * 2 * math.pi
                    pos = offset(centre, step.radius_m * math.cos(ang),
                                 step.radius_m * math.sin(ang))
                    emit(DroneState.ORBIT, step.altitude_m, 3.0)

            elif step.kind in (MissionStepKind.HOVER, MissionStepKind.TRACK):
                pos = step.target or pos
                for _ in range(max(1, int(step.duration_s / step_s))):
                    emit(
                        DroneState.TRACKING if step.kind is MissionStepKind.TRACK
                        else DroneState.HOVER,
                        step.altitude_m, 0.5,
                    )

            elif step.kind is MissionStepKind.LAND:
                for i in range(int(30 / step_s)):
                    emit(DroneState.LANDING, max(0.0, 30 - i * step_s * 2), 1.0)

        return track

    @staticmethod
    def confidence_gain(before: float, telemetry_after: Telemetry) -> float:
        """How much the closer vantage point should improve identification.

        This is the measurable payoff of the loop: the aircraft moved, the optics
        improved, and the alert's confidence is revised on that basis rather than
        on assertion.
        """
        after = telemetry_after.perception_confidence
        return round(max(0.0, after - before), 3)
