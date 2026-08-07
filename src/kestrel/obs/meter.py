"""Call metering — the evidence behind every performance claim KESTREL makes.

Nothing in this project asserts a latency or cost number that was not produced
here. The gate's value ("N% of frames never reached a VLM"), the cascade's cost
per drone-hour, and the p50/p95 figures in the report are all read out of this
registry.

Two deliberate choices:

*   **Cost is modelled, not billed.** The NVIDIA developer tier bills nothing, so
    a literal spend figure would be ``$0.00`` and would say nothing useful about
    whether the architecture is affordable at scale. Instead each model carries a
    reference rate taken from comparable commercial hosting, and we report
    "what this workload would cost at commercial rates". The distinction is
    stated wherever a cost appears.
*   **Skips are recorded as first-class events.** A gate that declines to call a
    model is the most important thing the pipeline does, so it is measured with
    the same rigour as a call that happened.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Stage(StrEnum):
    """Pipeline stage a call belongs to. Mirrors the cascade tiers."""

    GATE = "gate"                  # tier 0  — CPU only, no model
    DETECT = "detect"              # tier 1  — local detector
    TRACK = "track"                # tier 1.5 — local tracker
    EMBED = "embed"                # tier 2  — vector encode
    PERCEIVE = "perceive"          # tier 3  — fast VLM
    PERCEIVE_DEEP = "perceive_deep"  # tier 4 — deep VLM, async
    REASON = "reason"              # agent / rules / narrative
    RETRIEVE = "retrieve"          # index + fusion
    OTHER = "other"


# Reference rates in USD per 1M tokens (input, output), for cost *modelling*.
# These approximate commercial hosting for each model class; they are not what we
# were charged, which was nothing.
REFERENCE_RATES: dict[str, tuple[float, float]] = {
    "meta/llama-3.2-11b-vision-instruct": (0.055, 0.055),
    "meta/llama-3.2-90b-vision-instruct": (0.35, 0.40),
    "nvidia/nemotron-nano-12b-v2-vl": (0.06, 0.06),
    "meta/llama-3.3-70b-instruct": (0.60, 0.70),
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant": (0.05, 0.08),
    "openai/gpt-oss-120b": (0.59, 0.79),
    "openai/gpt-oss-20b": (0.05, 0.08),
    "meta/llama-3.1-8b-instruct": (0.05, 0.08),
    "nvidia/llama-nemotron-embed-vl-1b-v2": (0.02, 0.0),
    "nvidia/nv-embedqa-e5-v5": (0.02, 0.0),
}
DEFAULT_RATE = (0.10, 0.20)

# Local inference has no per-token price. We attribute an energy-equivalent cost
# so the edge/cloud comparison is not trivially "local is free".
LOCAL_COST_PER_CALL = 0.0000045  # ~RTX-class GPU-second at typical grid rates


@dataclass(slots=True)
class Call:
    """One metered unit of work."""

    stage: Stage
    model: str
    ms: float
    ok: bool = True
    tokens_in: int = 0
    tokens_out: int = 0
    cached: bool = False       # served from a cassette
    local: bool = False        # ran on-device
    skipped: bool = False      # the gate declined to spend this call
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def cost_usd(self) -> float:
        """Modelled cost. Cached and skipped calls cost nothing by construction."""
        if self.cached or self.skipped:
            return 0.0
        if self.local:
            return LOCAL_COST_PER_CALL
        rin, rout = REFERENCE_RATES.get(self.model, DEFAULT_RATE)
        return (self.tokens_in * rin + self.tokens_out * rout) / 1_000_000


@dataclass(slots=True)
class StageStats:
    calls: int = 0
    ok: int = 0
    failed: int = 0
    cached: int = 0
    skipped: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latencies: list[float] = field(default_factory=list)

    def _pct(self, q: float) -> float:
        if not self.latencies:
            return 0.0
        s = sorted(self.latencies)
        # Nearest-rank percentile; exact enough at our sample sizes and avoids
        # interpolating between two very different cold/warm timings.
        i = min(len(s) - 1, max(0, round(q * (len(s) - 1))))
        return round(s[i], 1)

    def summary(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "ok": self.ok,
            "failed": self.failed,
            "cached": self.cached,
            "skipped": self.skipped,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": round(self.cost_usd, 6),
            "p50_ms": self._pct(0.50),
            "p95_ms": self._pct(0.95),
            "mean_ms": round(statistics.fmean(self.latencies), 1) if self.latencies else 0.0,
            "max_ms": round(max(self.latencies), 1) if self.latencies else 0.0,
        }


class Meter:
    """Thread-safe, process-local metering registry."""

    def __init__(self, window: int = 4000) -> None:
        self._lock = threading.Lock()
        self._stages: dict[Stage, StageStats] = defaultdict(StageStats)
        self._models: dict[str, StageStats] = defaultdict(StageStats)
        self._recent: deque[Call] = deque(maxlen=window)
        self._t0 = time.time()
        # Frames offered to the gate vs frames the gate let through. This ratio
        # is the headline scalability number, so it is counted explicitly rather
        # than derived from stage counts.
        self.frames_seen = 0
        self.frames_analysed = 0

    def record(self, call: Call) -> Call:
        with self._lock:
            for bucket in (self._stages[call.stage], self._models[call.model]):
                bucket.calls += 1
                bucket.ok += int(call.ok)
                bucket.failed += int(not call.ok)
                bucket.cached += int(call.cached)
                bucket.skipped += int(call.skipped)
                bucket.tokens_in += call.tokens_in
                bucket.tokens_out += call.tokens_out
                bucket.cost_usd += call.cost_usd
                if not call.skipped:
                    bucket.latencies.append(call.ms)
            self._recent.append(call)
        return call

    def note_frame(self, analysed: bool) -> None:
        with self._lock:
            self.frames_seen += 1
            self.frames_analysed += int(analysed)

    # ── read-out ─────────────────────────────────────────────────────────
    @property
    def gate_efficiency(self) -> float:
        """Fraction of frames that never reached a cloud model. 0.0 if no frames."""
        with self._lock:
            if not self.frames_seen:
                return 0.0
            return 1.0 - (self.frames_analysed / self.frames_seen)

    @property
    def total_cost(self) -> float:
        with self._lock:
            return sum(s.cost_usd for s in self._stages.values())

    def cost_per_drone_hour(self, observed_seconds: float) -> float:
        """Extrapolate modelled spend to a full drone-hour of observation."""
        if observed_seconds <= 0:
            return 0.0
        return self.total_cost * (3600.0 / observed_seconds)

    def snapshot(self, observed_seconds: float = 0.0) -> dict[str, Any]:
        with self._lock:
            stages = {k.value: v.summary() for k, v in self._stages.items()}
            models = {k: v.summary() for k, v in self._models.items()}
            frames_seen, frames_analysed = self.frames_seen, self.frames_analysed
            total_cost = sum(s.cost_usd for s in self._stages.values())
            uptime = time.time() - self._t0
        return {
            "uptime_s": round(uptime, 1),
            "frames": {
                "seen": frames_seen,
                "analysed": frames_analysed,
                "skipped": frames_seen - frames_analysed,
                "gate_efficiency": round(
                    (1.0 - frames_analysed / frames_seen) if frames_seen else 0.0, 4
                ),
            },
            "cost": {
                "modelled_usd": round(total_cost, 6),
                "per_drone_hour_usd": round(
                    total_cost * (3600.0 / observed_seconds), 4
                )
                if observed_seconds > 0
                else None,
                "basis": "reference commercial rates; developer tier billed $0",
            },
            "stages": stages,
            "models": models,
        }

    def recent(self, n: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._recent)[-n:]
        return [
            {
                "stage": c.stage.value,
                "model": c.model,
                "ms": round(c.ms, 1),
                "ok": c.ok,
                "cached": c.cached,
                "local": c.local,
                "skipped": c.skipped,
                "tokens_in": c.tokens_in,
                "tokens_out": c.tokens_out,
                "cost_usd": round(c.cost_usd, 8),
                "error": c.error,
                **({"meta": c.meta} if c.meta else {}),
            }
            for c in items
        ]

    def reset(self) -> None:
        with self._lock:
            self._stages.clear()
            self._models.clear()
            self._recent.clear()
            self.frames_seen = 0
            self.frames_analysed = 0
            self._t0 = time.time()


METER = Meter()
