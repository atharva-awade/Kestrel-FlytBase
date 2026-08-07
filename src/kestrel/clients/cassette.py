"""Deterministic record/replay for model calls.

Why this exists, in order of importance:

1.  **A reviewer with no API key must be able to run the whole system.** Cassettes
    are committed, so ``KESTREL_MODE=replay`` reproduces a full session — real
    captions, real embeddings, real agent answers — with the network unplugged.
2.  **Tests must be deterministic.** Asserting on a live VLM's prose is a flaky
    test; asserting on a recorded response is a real one.
3.  **Demos must be repeatable.** A scripted walkthrough cannot depend on a
    provider's cold-start mood, and NIM's was measured at 57-84s.

Keys are content-addressed: the same logical request always resolves to the same
cassette, and any change to the request produces a miss rather than a silently
wrong hit. Images are hashed rather than embedded in the key so filenames stay
short while remaining exact.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Any

_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(s: str, limit: int = 28) -> str:
    return _SLUG.sub("-", s.lower()).strip("-")[:limit]


def _canonical(value: Any) -> Any:
    """Normalise a request payload so equivalent requests hash identically.

    Long base64 blobs (images) are replaced by a stable digest. Without this the
    key would embed megabytes of image data, and cassette filenames could not be
    written to disk.
    """
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    if isinstance(value, str):
        if value.startswith("data:") and ";base64," in value:
            head, b64 = value.split(";base64,", 1)
            digest = hashlib.sha256(b64.encode()).hexdigest()[:16]
            return f"{head};sha256:{digest}"
        if len(value) > 512:
            return f"sha256:{hashlib.sha256(value.encode()).hexdigest()[:16]}"
        return value
    return value


class CassetteStore:
    """Content-addressed JSON store for recorded provider responses."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.writes = 0

    # ── keys ─────────────────────────────────────────────────────────────
    @staticmethod
    def fingerprint(provider: str, endpoint: str, payload: dict[str, Any]) -> str:
        blob = json.dumps(
            {"provider": provider, "endpoint": endpoint, "payload": _canonical(payload)},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(blob.encode()).hexdigest()

    def path_for(
        self, provider: str, endpoint: str, payload: dict[str, Any], stage: str = "other"
    ) -> Path:
        fp = self.fingerprint(provider, endpoint, payload)
        model = _slug(str(payload.get("model", "unknown")))
        # Human-readable prefix keeps the directory browsable in a PR review;
        # the digest keeps it exact.
        return self.root / f"{_slug(stage,12)}__{model}__{fp[:12]}.json"

    # ── io ───────────────────────────────────────────────────────────────
    def get(
        self, provider: str, endpoint: str, payload: dict[str, Any], stage: str = "other"
    ) -> dict[str, Any] | None:
        p = self.path_for(provider, endpoint, payload, stage)
        if not p.exists():
            with self._lock:
                self.misses += 1
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            with self._lock:
                self.misses += 1
            return None
        with self._lock:
            self.hits += 1
        return data.get("response")

    def put(
        self,
        provider: str,
        endpoint: str,
        payload: dict[str, Any],
        response: dict[str, Any],
        stage: str = "other",
        overwrite: bool = False,
    ) -> Path:
        p = self.path_for(provider, endpoint, payload, stage)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and not overwrite:
            return p
        record = {
            # The request is stored redacted-but-legible so a reviewer can see
            # what produced a given response without replaying anything.
            "request": {
                "provider": provider,
                "endpoint": endpoint,
                "stage": stage,
                "payload": _canonical(payload),
            },
            "response": response,
        }
        p.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        with self._lock:
            self.writes += 1
        return p

    # ── introspection ────────────────────────────────────────────────────
    @property
    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self.hits + self.misses
            return {
                "hits": self.hits,
                "misses": self.misses,
                "writes": self.writes,
                "hit_rate": round(self.hits / total, 4) if total else 0.0,
                "count_on_disk": self.count,
            }

    @property
    def count(self) -> int:
        return len(list(self.root.glob("*.json"))) if self.root.exists() else 0

    def describe(self) -> list[dict[str, Any]]:
        """Inventory for the observability page and the report appendix."""
        out: list[dict[str, Any]] = []
        for p in sorted(self.root.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            req = d.get("request", {})
            out.append(
                {
                    "file": p.name,
                    "stage": req.get("stage"),
                    "provider": req.get("provider"),
                    "model": (req.get("payload") or {}).get("model"),
                    "bytes": p.stat().st_size,
                }
            )
        return out


def image_to_data_uri(data: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"
