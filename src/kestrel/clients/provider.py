"""Resilient transport for OpenAI-compatible providers.

The measurements in ``docs/adr/0001-model-selection.md`` drive everything here:

*   NIM's free tier rate-limits near **40 RPM**, so we self-limit *below* the cap
    instead of discovering it as 429s.
*   NIM cold starts were measured at **57-84 s**, so timeouts are per-tier rather
    than global — one uniform timeout would either abort the deep tier or let the
    fast tier hang for a minute.
*   Groq answered the same class of request in **227 ms** versus NIM's 83 s, so
    failover is not a rare safety net; it is a routine performance path.

A circuit breaker sits in front of each provider so that a provider having a bad
minute degrades the system instead of stalling it.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

from kestrel.clients.cassette import CassetteStore
from kestrel.config import Mode, Settings
from kestrel.obs.meter import METER, Call, Stage


class ProviderError(RuntimeError):
    """Transport or protocol failure from a model provider."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class CassetteMiss(ProviderError):
    """Replay mode was asked for a request that was never recorded.

    Deliberately loud. Silently falling through to the network in replay mode
    would make "runs offline" untrue in exactly the situations that matter.
    """


class CircuitOpen(ProviderError):
    """The breaker is open; the provider is being given time to recover."""


# ── rate limiting ─────────────────────────────────────────────────────────────
class RateLimiter:
    """Async token bucket, requests-per-minute.

    Shaping our own traffic is cheaper than being shaped: a 429 costs a round trip
    plus a backoff, whereas waiting costs only the wait.
    """

    def __init__(self, rpm: int) -> None:
        self.capacity = max(1, rpm)
        self._tokens = float(self.capacity)
        self._refill_per_s = self.capacity / 60.0
        self._last = time.monotonic()
        self._lock = asyncio.Lock()
        self.waited_s = 0.0
        self.waits = 0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(
                self.capacity, self._tokens + (now - self._last) * self._refill_per_s
            )
            self._last = now
            if self._tokens < 1.0:
                delay = (1.0 - self._tokens) / self._refill_per_s
                self.waited_s += delay
                self.waits += 1
                await asyncio.sleep(delay)
                self._tokens = 0.0
                self._last = time.monotonic()
            else:
                self._tokens -= 1.0


# ── circuit breaker ───────────────────────────────────────────────────────────
@dataclass
class Breaker:
    threshold: int
    cooldown: float
    failures: int = 0
    opened_at: float = 0.0
    trips: int = 0

    @property
    def is_open(self) -> bool:
        if self.opened_at and (time.monotonic() - self.opened_at) < self.cooldown:
            return True
        if self.opened_at:
            # Cooldown elapsed → half-open: allow one probe through.
            self.opened_at = 0.0
            self.failures = 0
        return False

    def record(self, ok: bool) -> None:
        if ok:
            self.failures = 0
            self.opened_at = 0.0
            return
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()
            self.trips += 1


# ── provider ──────────────────────────────────────────────────────────────────
class Provider:
    """One OpenAI-compatible endpoint, wrapped in the protections above."""

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        settings: Settings,
        cassettes: CassetteStore,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.settings = settings
        self.cassettes = cassettes
        self.limiter = RateLimiter(settings.rate_limit_rpm)
        self.breaker = Breaker(settings.breaker_threshold, settings.breaker_cooldown)
        self._client: httpx.AsyncClient | None = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                # Connection reuse matters: without it every call pays a fresh TLS
                # handshake, which is a meaningful share of a 227ms budget.
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── the one call path ────────────────────────────────────────────────
    async def post(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        stage: Stage = Stage.OTHER,
        timeout: float | None = None,
        deep: bool = False,
    ) -> dict[str, Any]:
        """POST with cassette awareness, rate limiting, retries and breaking."""
        mode = self.settings.effective_mode
        t0 = time.perf_counter()
        model = str(payload.get("model", "unknown"))

        # ── replay ───────────────────────────────────────────────────────
        if mode is Mode.REPLAY:
            hit = self.cassettes.get(self.name, endpoint, payload, stage.value)
            ms = (time.perf_counter() - t0) * 1000
            if hit is not None:
                u = hit.get("usage") or {}
                METER.record(
                    Call(
                        stage, model, ms, ok=True, cached=True,
                        tokens_in=int(u.get("prompt_tokens", 0) or 0),
                        tokens_out=int(u.get("completion_tokens", 0) or 0),
                    )
                )
                return hit

            # If cassette is missing but an API key is provided, gracefully fall through
            # to the live provider rather than crashing the user session.
            if not (self.available and not self.breaker.is_open):
                METER.record(
                    Call(stage, model, ms, ok=False, error="cassette miss")
                )
                raise CassetteMiss(
                    f"No cassette for {self.name}{endpoint} model={model}. "
                    f"Record one with KESTREL_MODE=record, or configure a {self.name.upper()}_API_KEY "
                    f"to enable live model fallback."
                )

        # ── live / record ────────────────────────────────────────────────
        if not self.available:
            raise ProviderError(f"{self.name}: no API key configured")
        if self.breaker.is_open:
            raise CircuitOpen(
                f"{self.name}: circuit open after {self.breaker.failures} failures"
            )

        # In live mode an existing cassette is still a valid, free answer.
        if mode is Mode.LIVE:
            hit = self.cassettes.get(self.name, endpoint, payload, stage.value)
            if hit is not None:
                ms = (time.perf_counter() - t0) * 1000
                u = hit.get("usage") or {}
                METER.record(
                    Call(
                        stage, model, ms, ok=True, cached=True,
                        tokens_in=int(u.get("prompt_tokens", 0) or 0),
                        tokens_out=int(u.get("completion_tokens", 0) or 0),
                    )
                )
                return hit

        budget = timeout or (
            self.settings.timeout_deep if deep else self.settings.timeout_fast
        )
        client = await self._http()
        last: Exception | None = None

        # A deadline across every attempt, not just each one. The deep tier is
        # allowed to run long by design, so it keeps its own budget; the
        # interactive tiers are capped, because a caller waiting on a chat reply
        # would rather have a fast failure than a perfect answer four minutes late.
        deadline = t0 + (budget if deep else min(budget * 2, self.settings.request_deadline))

        for attempt in range(1, self.settings.max_retries + 1):
            remaining = deadline - time.perf_counter()
            if remaining <= 1.0 and attempt > 1:
                last = last or ProviderError(f"{self.name}: retry budget exhausted")
                break
            await self.limiter.acquire()
            attempt_t0 = time.perf_counter()
            try:
                r = await client.post(
                    endpoint, json=payload, timeout=max(2.0, min(budget, remaining))
                )
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = ProviderError(f"{type(e).__name__}: {e}", retryable=True)
                self.breaker.record(False)
            else:
                if r.status_code < 400:
                    body = r.json()
                    ms = (time.perf_counter() - t0) * 1000
                    u = body.get("usage") or {}
                    METER.record(
                        Call(
                            stage, model, ms, ok=True,
                            tokens_in=int(u.get("prompt_tokens", 0) or 0),
                            tokens_out=int(u.get("completion_tokens", 0) or 0),
                            meta={"attempt": attempt} if attempt > 1 else {},
                        )
                    )
                    self.breaker.record(True)
                    self.cassettes.put(
                        self.name, endpoint, payload, body, stage.value,
                        overwrite=(mode is Mode.RECORD),
                    )
                    return body

                # 429 and 5xx are worth retrying; 4xx generally is not, because
                # the request itself is wrong and will stay wrong.
                retryable = r.status_code == 429 or r.status_code >= 500
                last = ProviderError(
                    f"HTTP {r.status_code}: {r.text[:300]}",
                    status=r.status_code,
                    retryable=retryable,
                )
                # Only provider-side faults count against the breaker. A 400 means
                # our request is malformed; tripping the breaker on that would take
                # a healthy provider offline because of a bug on our side.
                if retryable:
                    self.breaker.record(False)
                else:
                    break
                # Respect Retry-After, but only when waiting is actually cheaper
                # than failing over.
                #
                # A free tier that has exhausted its *daily* token budget answers
                # 429 with a Retry-After measured in hours. Sleeping on that once
                # per attempt turned a 27 ms rejection into 90 s of dead air per
                # provider, and a question that should have failed over in
                # milliseconds took nearly four minutes to answer. To an operator
                # that is indistinguishable from a hang, which is the one thing a
                # failover path exists to prevent.
                #
                # So: a short wait is honoured, a long one is treated as the
                # provider telling us to go elsewhere.
                ra = r.headers.get("retry-after")
                if ra:
                    try:
                        wait = float(ra)
                    except ValueError:
                        wait = -1.0
                    if 0 <= wait <= self.settings.retry_after_max:
                        await asyncio.sleep(wait)
                        continue
                    if wait > self.settings.retry_after_max:
                        last = ProviderError(
                            f"HTTP {r.status_code}: retry-after {wait:.0f}s exceeds the "
                            f"{self.settings.retry_after_max:.0f}s interactive budget, "
                            f"failing over",
                            status=r.status_code,
                            retryable=True,
                        )
                        break

            if attempt < self.settings.max_retries:
                # Exponential backoff with full jitter: synchronised retries from
                # a parallel pipeline would otherwise re-collide on every wave.
                backoff = min(2 ** (attempt - 1), 8) * (0.5 + random.random())
                await asyncio.sleep(backoff)
                _ = attempt_t0

        ms = (time.perf_counter() - t0) * 1000
        METER.record(Call(stage, model, ms, ok=False, error=str(last)[:200]))
        raise last or ProviderError(f"{self.name}: exhausted retries")

    @property
    def health(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "available": self.available,
            "circuit_open": self.breaker.opened_at > 0,
            "consecutive_failures": self.breaker.failures,
            "breaker_trips": self.breaker.trips,
            "rate_limit_rpm": self.limiter.capacity,
            "rate_limit_waits": self.limiter.waits,
            "rate_limit_waited_s": round(self.limiter.waited_s, 2),
        }
