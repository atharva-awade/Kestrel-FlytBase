"""Tier 0 — the cost gate.

This is the smallest component in KESTREL and the one that decides whether the
system is deployable. A patrol drone streaming 2 fps for an eight-hour shift
produces ~57,600 frames. At NIM's measured ~40 requests/minute, captioning all of
them would take twenty-four hours and cost more than the guard it replaces. Almost
all of those frames show the same empty yard.

So the gate answers one question per frame — *is there any reason to spend a model
call on this?* — using only CPU arithmetic, and it is deliberately conservative:
the cost of a wasted VLM call is a fraction of a cent, and the cost of a missed
intruder is the entire product.

Three independent signals, cheapest first, short-circuiting as soon as one fires:

    structural   perceptual-hash distance    catches layout change
    photometric  mean absolute pixel delta   catches motion the hash smooths over
    semantic     embedding cosine distance   catches "same pixels, new meaning"

The semantic check is the interesting one and it is *optional*: it costs an
embedding call, which is two orders of magnitude cheaper than a VLM call but not
free, so it only runs when the cheap signals are ambiguous.

A heartbeat guarantees a frame is analysed at least every ``max_skip_seconds``
regardless of how static the scene looks, because "nothing has changed for twenty
minutes" is a claim that should be re-verified rather than assumed.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from kestrel.config import Settings, get_settings
from kestrel.domain import GateVerdict, Site, Telemetry
from kestrel.ingest.sources import hamming
from kestrel.obs.meter import METER, Call, Stage


@dataclass(slots=True)
class _Ref:
    """What the gate remembers about the recent past."""

    phash: str | None = None
    gray: np.ndarray | None = None
    ts: datetime | None = None
    embeddings: deque[np.ndarray] = field(default_factory=lambda: deque(maxlen=24))


class CostGate:
    """Decides which frames deserve to reach a model."""

    def __init__(
        self,
        site: Site | None = None,
        settings: Settings | None = None,
        *,
        embed_fn=None,
    ) -> None:
        self.s = settings or get_settings()
        self.site = site
        # Injected rather than imported so the gate stays unit-testable without a
        # network, and so callers can disable semantic checking by passing None.
        self._embed_fn = embed_fn
        self._ref = _Ref()
        # Consecutive frames skipped since the last analysis. Bounds the heartbeat
        # independently of the site clock.
        self._skips = 0
        self.stats = {
            "seen": 0,
            "analysed": 0,
            "by_reason": {},
            "embed_calls": 0,
        }

    # ── priors ───────────────────────────────────────────────────────────
    # Ceiling on how far context may lower the gate's threshold. Priors multiply
    # (zone x night x hovering x off-hours), which compounds to ~6x over a
    # restricted zone at night — and dividing the threshold by 6 makes the gate so
    # twitchy that sensor noise trips it and nothing is gated at all. Capping keeps
    # the gate meaningfully more sensitive where it matters without switching it
    # off. Measured effect: restricted-zone-at-night gating went from 33% to a
    # useful rate without losing any true detection in the golden set.
    MAX_SENSITIVITY = 2.5

    def _zone_prior(self, tel: Telemetry | None) -> tuple[float, str]:
        """Context can lower the bar for spending a call.

        Hovering over the substation at 03:00 is not the same situation as
        transiting the car park at noon, and the gate should not pretend it is.
        """
        if tel is None or self.site is None:
            return 1.0, ""
        prior, notes = 1.0, []

        zone = self.site.zone_at(tel.position)
        if zone is not None and zone.priority > 1.0:
            prior *= zone.priority
            notes.append(f"zone={zone.id}(x{zone.priority:g})")

        if tel.is_night:
            prior *= 1.5
            notes.append("night(x1.5)")

        # A stationary drone is looking at something on purpose.
        if tel.state.value in ("orbit", "hover", "tracking"):
            prior *= 1.25
            notes.append(f"{tel.state.value}(x1.25)")

        # Outside a zone's normal hours, presence is itself information.
        if zone is not None and zone.normal_hours is not None:
            lo, hi = zone.normal_hours
            if not (lo <= tel.ts.hour < hi):
                prior *= 1.6
                notes.append("off-hours(x1.6)")

        return prior, " ".join(notes)

    # ── main entry ───────────────────────────────────────────────────────
    async def decide(
        self,
        *,
        image: np.ndarray | None,
        phash: str | None,
        ts: datetime,
        telemetry: Telemetry | None = None,
        is_text_frame: bool = False,
    ) -> GateVerdict:
        t0 = time.perf_counter()
        self.stats["seen"] += 1
        prior, prior_note = self._zone_prior(telemetry)

        verdict = await self._evaluate(
            image=image, phash=phash, ts=ts, prior=prior, is_text_frame=is_text_frame
        )
        if prior_note:
            verdict.reason = f"{verdict.reason} [{prior_note}]"

        # Record the decision — including the skips. The skip rate is the number
        # the scalability argument rests on, so it is measured, not estimated.
        self.stats["analysed"] += int(verdict.analyse)
        key = verdict.reason.split(" ")[0].split("[")[0]
        self.stats["by_reason"][key] = self.stats["by_reason"].get(key, 0) + 1
        METER.note_frame(verdict.analyse)
        METER.record(
            Call(
                Stage.GATE,
                "cpu:gate",
                (time.perf_counter() - t0) * 1000,
                ok=True,
                local=True,
                skipped=not verdict.analyse,
            )
        )

        if verdict.analyse:
            self._skips = 0
            self._ref.phash = phash
            self._ref.ts = ts
            if image is not None:
                self._ref.gray = _gray_small(image)
        else:
            self._skips += 1
        return verdict

    async def _evaluate(
        self,
        *,
        image: np.ndarray | None,
        phash: str | None,
        ts: datetime,
        prior: float,
        is_text_frame: bool,
    ) -> GateVerdict:
        # Scripted text frames carry no pixels to compare, and each one is an
        # authored event rather than a sample of a continuous stream — so every
        # one is meaningful by construction.
        if is_text_frame:
            return GateVerdict(analyse=True, reason="scripted-frame", novelty=1.0, priority=prior)

        # First frame of a session establishes the reference.
        if self._ref.phash is None and self._ref.gray is None:
            return GateVerdict(analyse=True, reason="first-frame", novelty=1.0, priority=prior)

        # Heartbeat: never let a static scene go unverified indefinitely. Bounded
        # by elapsed time AND consecutive skips, whichever trips first — a demo may
        # compress the site clock, and a time-only bound would then fire every
        # frame and gate nothing at all.
        if self._skips >= self.s.gate_max_skip_frames:
            return GateVerdict(
                analyse=True,
                reason=f"heartbeat({self._skips}f)",
                novelty=0.35,
                priority=prior,
            )
        if self._ref.ts is not None:
            gap = (ts - self._ref.ts).total_seconds()
            if gap >= self.s.gate_max_skip_seconds:
                return GateVerdict(
                    analyse=True,
                    reason=f"heartbeat({gap:.0f}s)",
                    novelty=0.35,
                    priority=prior,
                )

        # Context raises sensitivity, but only up to a ceiling — see MAX_SENSITIVITY.
        sensitivity = min(max(1.0, prior), self.MAX_SENSITIVITY)

        # ── signal 1: structural ─────────────────────────────────────────
        dist = hamming(phash, self._ref.phash) if phash and self._ref.phash else None
        threshold = max(3, round(self.s.gate_phash_distance / sensitivity))
        if dist is not None and dist >= threshold:
            return GateVerdict(
                analyse=True,
                reason=f"phash-change(d={dist}>={threshold})",
                novelty=min(1.0, dist / 32.0),
                priority=prior,
                phash_distance=dist,
            )

        # ── signal 2: photometric ────────────────────────────────────────
        delta = None
        if image is not None and self._ref.gray is not None:
            g = _gray_small(image)
            if g.shape == self._ref.gray.shape:
                delta = float(np.mean(np.abs(g.astype(np.int16) - self._ref.gray.astype(np.int16))) / 255.0)
                if delta >= self.s.gate_pixel_delta / sensitivity:
                    return GateVerdict(
                        analyse=True,
                        reason=f"pixel-delta({delta:.4f})",
                        novelty=min(1.0, delta * 12),
                        priority=prior,
                        phash_distance=dist,
                        pixel_delta=delta,
                    )

        # ── signal 3: semantic ───────────────────────────────────────────
        # Only consulted when the cheap signals are borderline. Catches the case
        # the other two structurally cannot: a scene whose pixels barely moved but
        # whose meaning changed — a person now facing the gate rather than away.
        borderline = dist is not None and dist >= max(1, threshold // 2)
        if borderline and self._embed_fn is not None and image is not None:
            try:
                vec = np.asarray(await self._embed_fn(image), dtype=np.float32)
                self.stats["embed_calls"] += 1
                if self._ref.embeddings:
                    sims = [_cos(vec, r) for r in self._ref.embeddings]
                    best = max(sims)
                    self._ref.embeddings.append(vec)
                    if best < self.s.gate_embed_similarity:
                        return GateVerdict(
                            analyse=True,
                            reason=f"semantic-novelty(sim={best:.3f})",
                            novelty=float(1.0 - best),
                            priority=prior,
                            phash_distance=dist,
                            pixel_delta=delta,
                            embed_similarity=float(best),
                        )
                else:
                    self._ref.embeddings.append(vec)
            except Exception:
                # A gate that crashes stops the pipeline; a gate that cannot reach
                # the embedding service should simply fall back to the cheap
                # signals it already computed.
                pass

        return GateVerdict(
            analyse=False,
            reason=f"static(d={dist},delta={delta:.4f})" if delta is not None else f"static(d={dist})",
            novelty=0.0,
            priority=prior,
            phash_distance=dist,
            pixel_delta=delta,
        )

    # ── read-out ─────────────────────────────────────────────────────────
    @property
    def efficiency(self) -> float:
        seen = self.stats["seen"]
        return 1.0 - (self.stats["analysed"] / seen) if seen else 0.0

    def summary(self) -> dict:
        return {
            **self.stats,
            "skipped": self.stats["seen"] - self.stats["analysed"],
            "efficiency": round(self.efficiency, 4),
        }

    def reset(self) -> None:
        self._ref = _Ref()
        self._skips = 0


def _gray_small(img: np.ndarray, size: int = 64) -> np.ndarray:
    """Downscaled greyscale. Small enough that the comparison is free, large
    enough that a person-sized change survives the resize."""
    import cv2

    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    return cv2.resize(g, (size, size), interpolation=cv2.INTER_AREA)


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return float(np.dot(a, b) / (na * nb)) if na and nb else 0.0
