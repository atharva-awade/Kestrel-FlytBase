"""The normalcy model.

Rules encode what someone thought to prohibit. A baseline encodes what the site
actually does, which is how the system catches the thing nobody wrote a rule for.

*"A vehicle is at the loading dock"* is unremarkable. *"A vehicle is at the loading
dock at 03:00, and in fourteen days of observation that has never happened"* is a
finding — and no static rule produces the second sentence.

The model is deliberately simple: counts per ``(zone, hour-of-day, class)``,
accumulated per day, scored by how far today departs from the historical
distribution. Simplicity is the point. An operator has to be able to read *why*
something scored as anomalous, and "we have seen this 0 times in 14 days, and the
usual count for this hour is 4.2 ± 1.1" is legible in a way that an autoencoder's
reconstruction error is not.

**Cold start is handled explicitly.** With two days of history, everything looks
unprecedented. Until ``MIN_DAYS`` of evidence exists the model reports low
confidence and the triage layer discounts it, rather than flooding an operator with
false novelty on day one.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime

# Below this, the baseline abstains rather than guessing.
MIN_DAYS = 3
# z-score at which a deviation is worth surfacing.
ANOMALY_Z = 2.0


@dataclass
class Deviation:
    """How unusual an observation is, and why — in words an operator can check."""

    score: float           # 0-1; magnitude of departure from normal
    z: float               # standard deviations from the mean
    observed: int
    mean: float
    stdev: float
    days_of_history: int
    first_ever: bool
    confident: bool
    explanation: str

    @property
    def anomalous(self) -> bool:
        return self.confident and (self.first_ever or abs(self.z) >= ANOMALY_Z)


class BaselineModel:
    """Per-zone, per-hour, per-class normalcy for one site."""

    def __init__(self, site_id: str, db=None) -> None:
        self.site_id = site_id
        self.db = db
        # (zone, hour, label) -> {day: count}
        self._counts: dict[tuple[str, int, str], dict[str, int]] = {}
        self._days: set[str] = set()

    # ── accumulate ───────────────────────────────────────────────────────
    def observe(self, zone_id: str | None, ts: datetime, label: str) -> None:
        if not zone_id:
            return
        day = ts.date().isoformat()
        key = (zone_id, ts.hour, label.lower())
        self._counts.setdefault(key, {})
        self._counts[key][day] = self._counts[key].get(day, 0) + 1
        self._days.add(day)
        if self.db is not None:
            self.db.bump_baseline(self.site_id, zone_id, ts.hour, label.lower(), day)

    def load(self, rows: list[dict]) -> None:
        """Restore from persisted counts so history survives a restart."""
        for r in rows:
            key = (r["zone_id"], int(r["hour"]), str(r["label"]).lower())
            self._counts.setdefault(key, {})
            self._counts[key][r["day"]] = int(r["count"])
            self._days.add(r["day"])

    # ── score ────────────────────────────────────────────────────────────
    def evaluate(self, zone_id: str | None, ts: datetime, label: str) -> Deviation:
        day = ts.date().isoformat()
        history_days = sorted(d for d in self._days if d != day)
        n_days = len(history_days)

        if not zone_id:
            return Deviation(0, 0, 0, 0, 0, n_days, False, False,
                             "no zone resolved for this observation")

        key = (zone_id, ts.hour, label.lower())
        per_day = self._counts.get(key, {})
        observed = per_day.get(day, 0)
        # Days with no sighting are real evidence of absence, so they count as
        # zeros rather than being omitted — otherwise the mean is biased upward.
        history = [per_day.get(d, 0) for d in history_days]

        if n_days < MIN_DAYS:
            return Deviation(
                0.25 if observed and not any(history) else 0.0,
                0.0, observed, 0.0, 0.0, n_days, False, False,
                f"only {n_days} day(s) of history, too little to judge "
                f"(need {MIN_DAYS}); reported without confidence",
            )

        mean = statistics.fmean(history) if history else 0.0
        stdev = statistics.pstdev(history) if len(history) > 1 else 0.0
        first_ever = observed > 0 and not any(history)

        if first_ever:
            return Deviation(
                0.9, float("inf") if stdev == 0 else 99.0, observed, mean, stdev,
                n_days, True, True,
                f"first time a '{label}' has been seen in {zone_id} at "
                f"{ts.hour:02d}:00 in {n_days} days of observation",
            )

        if stdev < 1e-6:
            # A perfectly regular history: any departure is meaningful, but with no
            # spread we cannot express it as a z-score, so bound it instead.
            z = 0.0 if observed == mean else 3.0
            expl = (
                f"count {observed} against a perfectly regular history of {mean:.0f}"
                if observed != mean
                else f"count {observed} matches the usual {mean:.0f}"
            )
        else:
            z = (observed - mean) / stdev
            expl = (
                f"count {observed} against a usual {mean:.1f} ± {stdev:.1f} "
                f"for {zone_id} at {ts.hour:02d}:00 over {n_days} days "
                f"({z:+.1f} sd)"
            )

        score = min(1.0, abs(z) / 4.0)
        return Deviation(score, z, observed, mean, stdev, n_days, False, True, expl)

    # ── read-out ─────────────────────────────────────────────────────────
    def profile(self, zone_id: str) -> dict[int, float]:
        """Mean activity by hour for a zone — drives the heat strip in the UI."""
        out: dict[int, float] = {}
        for (z, hour, _), per_day in self._counts.items():
            if z != zone_id:
                continue
            out[hour] = out.get(hour, 0.0) + sum(per_day.values())
        n = max(1, len(self._days))
        return {h: round(v / n, 2) for h, v in sorted(out.items())}

    def quietest_hours(self, zone_id: str, n: int = 3) -> list[int]:
        p = self.profile(zone_id)
        return [h for h, _ in sorted(p.items(), key=lambda x: x[1])[:n]]

    @property
    def stats(self) -> dict:
        return {
            "days_of_history": len(self._days),
            "confident": len(self._days) >= MIN_DAYS,
            "min_days_required": MIN_DAYS,
            "tracked_combinations": len(self._counts),
            "zones": len({k[0] for k in self._counts}),
            "labels": sorted({k[2] for k in self._counts})[:12],
        }


def combine_confidence(
    perception: float, rule_strength: float, deviation: Deviation
) -> float:
    """The composite confidence attached to an alert.

    Three independent sources of doubt, combined multiplicatively so that any one
    of them being weak drags the result down — which is the correct behaviour. A
    rule that fires perfectly on a detection the camera could barely see should not
    produce a confident alert.

    Baseline deviation *raises* confidence when the observation is unusual, but is
    ignored entirely when the baseline lacks the history to be trusted.
    """
    base = max(0.05, perception) * max(0.05, rule_strength)
    if deviation.confident and deviation.anomalous:
        base = base + (1 - base) * min(0.35, deviation.score * 0.4)
    return round(min(1.0, max(0.02, base)), 3)


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))
