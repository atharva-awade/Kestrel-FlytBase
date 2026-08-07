"""Runtime configuration.

Every model ID here was verified against the live API before being written down
(see ``docs/adr/0001-model-selection.md``). Where a default differs from what the
provider's documentation would suggest, there is a comment saying why — those are
the places where the docs and reality disagreed.
"""

from __future__ import annotations

import functools
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Mode(StrEnum):
    """How model calls are serviced.

    ``replay`` is the default for a reason: a reviewer with no API key must still
    be able to run the whole system. Cassettes are committed to the repo.
    """

    LIVE = "live"      # call providers; record responses as new cassettes
    REPLAY = "replay"  # serve committed cassettes only; never touch the network
    RECORD = "record"  # like live, but overwrite existing cassettes


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── credentials ───────────────────────────────────────────────────────
    # Server-side only. These must never be serialised toward the browser; the
    # API layer has no route that returns them and the web app never reads them.
    nvidia_api_key: str = Field(default="", alias="NVIDIA_API_KEY")
    nvidia_base_url: str = Field(
        default="https://integrate.api.nvidia.com/v1", alias="NVIDIA_BASE_URL"
    )
    groq_api_key: str = Field(default="", alias="GROQ_API_KEY")
    groq_base_url: str = Field(
        default="https://api.groq.com/openai/v1", alias="GROQ_BASE_URL"
    )

    # ── mode ──────────────────────────────────────────────────────────────
    mode: Mode = Field(default=Mode.REPLAY, alias="KESTREL_MODE")

    # ── perception ────────────────────────────────────────────────────────
    # Tier 3. Measured 1.28s and caught colour + vehicle + person. The 8B
    # nemotron-nano-vl is faster (0.86s) but missed the person, which is the
    # wrong direction to fail in for a security product.
    vlm_fast: str = Field(
        default="meta/llama-3.2-11b-vision-instruct", alias="KESTREL_VLM_FAST"
    )
    # Tier 4. Measured 57-84s → asynchronous only, never on the request path.
    vlm_deep: str = Field(
        default="meta/llama-3.2-90b-vision-instruct", alias="KESTREL_VLM_DEEP"
    )
    vlm_mid: str = Field(
        default="nvidia/nemotron-nano-12b-v2-vl", alias="KESTREL_VLM_MID"
    )
    # Frames are downscaled before they reach a VLM. A 960px frame costs ~6.4k
    # prompt tokens and ~6s round trip; 640px carries the same scene information
    # for scene-level description at a fraction of the prefill. The detector still
    # sees full resolution — only the semantic tier is downscaled.
    vlm_max_width: int = Field(default=640, alias="KESTREL_VLM_MAX_WIDTH")
    vlm_max_tokens: int = Field(default=420, alias="KESTREL_VLM_MAX_TOKENS")

    # ── embeddings ────────────────────────────────────────────────────────
    # Joint image+text space, 2048-d, verified same-space for both modalities.
    # nvidia/nvclip is listed in the catalogue but its NVCF function is not
    # provisioned for developer keys (404), so this is the working equivalent.
    vl_embed: str = Field(
        default="nvidia/llama-nemotron-embed-vl-1b-v2", alias="KESTREL_VL_EMBED"
    )
    vl_embed_dim: int = Field(default=2048, alias="KESTREL_VL_EMBED_DIM")
    text_embed: str = Field(default="nvidia/nv-embedqa-e5-v5", alias="KESTREL_EMBED")
    text_embed_dim: int = Field(default=1024, alias="KESTREL_EMBED_DIM")

    # ── reasoning ─────────────────────────────────────────────────────────
    # Groq is primary on measured latency: 227ms vs 83s for the same model class
    # on NIM, which additionally rate-limits near 40 RPM.
    llm: str = Field(default="llama-3.3-70b-versatile", alias="KESTREL_LLM")
    llm_provider: Literal["groq", "nvidia"] = Field(
        default="groq", alias="KESTREL_LLM_PROVIDER"
    )
    llm_fallback: str = Field(
        default="meta/llama-3.3-70b-instruct", alias="KESTREL_LLM_FALLBACK"
    )
    # Cheap router model — classifies intent so the 70B is only woken when needed.
    llm_router: str = Field(default="openai/gpt-oss-20b", alias="KESTREL_LLM_ROUTER")

    # No hosted cross-encoder is available, so fuse with RRF and optionally
    # rerank the top-k with the LLM above.
    rerank_strategy: Literal["rrf", "rrf+llm"] = Field(
        default="rrf+llm", alias="KESTREL_RERANK_STRATEGY"
    )

    # ── local detection (tiers 1 + 1.5) ───────────────────────────────────
    # No hosted detector exists on NIM at all, so open-vocabulary detection runs
    # on-device. This is what makes the edge/cloud split real instead of drawn.
    local_detector: bool = Field(default=True, alias="KESTREL_LOCAL_DETECTOR")
    local_detector_model: str = Field(
        default="IDEA-Research/grounding-dino-tiny", alias="KESTREL_LOCAL_DETECTOR_MODEL"
    )
    local_detector_fallback: str = Field(
        default="PekingU/rtdetr_r50vd_coco_o365", alias="KESTREL_LOCAL_DETECTOR_FALLBACK"
    )
    # YOLO11 weights are served from GitHub releases rather than the HuggingFace
    # Hub, which makes this the backend that still works on networks where the Hub
    # is blocked. See docs/adr/0002-detector-backend.md.
    yolo_weights: str = Field(default="yolo11s.pt", alias="KESTREL_YOLO_WEIGHTS")
    detector_device: str = Field(default="auto", alias="KESTREL_DETECTOR_DEVICE")
    # Open-vocabulary grounding will ground *any* phrase somewhere — asked for "a
    # traffic cone" in a scene containing none, Grounding DINO returns a box on a
    # white pillar at ~0.48. Measured separation on real footage: a genuine person
    # scores 0.87, spurious groundings sit in the 0.35-0.50 band. 0.55 keeps the
    # true detection comfortably and discards the confident-looking noise.
    #
    # This is why a promptable rule is backtested before it can fire: the operator
    # sees what the phrase actually grounds on, rather than trusting it.
    detector_box_threshold: float = Field(default=0.55, alias="KESTREL_DET_BOX_THRESHOLD")
    detector_text_threshold: float = Field(default=0.30, alias="KESTREL_DET_TEXT_THRESHOLD")

    # ── gate (tier 0) ─────────────────────────────────────────────────────
    # The gate is the whole scalability story: it decides which frames are worth
    # spending a model call on. Tuned so a static scene costs nothing.
    gate_phash_distance: int = Field(default=6, alias="KESTREL_GATE_PHASH_DISTANCE")
    gate_pixel_delta: float = Field(default=0.012, alias="KESTREL_GATE_PIXEL_DELTA")
    gate_embed_similarity: float = Field(default=0.955, alias="KESTREL_GATE_EMBED_SIM")
    gate_novelty_buffer: int = Field(default=24, alias="KESTREL_GATE_NOVELTY_BUFFER")
    # Heartbeat: re-verify a static scene periodically rather than trusting it
    # indefinitely. Bounded by BOTH elapsed site-clock time and consecutive skips,
    # whichever comes first. The frame bound matters because a demo may compress
    # the site clock (a 60s clip presented as a night shift), and a time-only
    # heartbeat would then fire on every single frame and gate nothing.
    gate_max_skip_seconds: float = Field(default=120.0, alias="KESTREL_GATE_MAX_SKIP_S")
    gate_max_skip_frames: int = Field(default=45, alias="KESTREL_GATE_MAX_SKIP_FRAMES")

    # ── storage ───────────────────────────────────────────────────────────
    db_path: Path = Field(default=Path("data/kestrel.db"), alias="KESTREL_DB")
    cassette_dir: Path = Field(default=Path("data/cassettes"), alias="KESTREL_CASSETTES")
    frame_dir: Path = Field(default=Path("data/frames"), alias="KESTREL_FRAMES")
    sites_dir: Path = Field(default=Path("data/sites"), alias="KESTREL_SITES")
    footage_dir: Path = Field(default=Path("data/footage"), alias="KESTREL_FOOTAGE")

    # ── api ───────────────────────────────────────────────────────────────
    api_host: str = Field(default="127.0.0.1", alias="KESTREL_API_HOST")
    api_port: int = Field(default=8000, alias="KESTREL_API_PORT")
    cors_origins: str = Field(
        default="http://localhost:3000,http://127.0.0.1:3000", alias="KESTREL_CORS"
    )

    # ── resilience ────────────────────────────────────────────────────────
    # NIM cold starts were measured at 57-84s, so timeouts are generous by tier
    # rather than uniform. A single global timeout would either fail the deep
    # tier or let the fast tier hang.
    timeout_fast: float = Field(default=45.0, alias="KESTREL_TIMEOUT_FAST")
    timeout_deep: float = Field(default=240.0, alias="KESTREL_TIMEOUT_DEEP")
    max_retries: int = Field(default=3, alias="KESTREL_MAX_RETRIES")
    breaker_threshold: int = Field(default=5, alias="KESTREL_BREAKER_THRESHOLD")
    breaker_cooldown: float = Field(default=30.0, alias="KESTREL_BREAKER_COOLDOWN")

    #: Longest `Retry-After` worth honouring before failing over instead.
    #: A free tier that has spent its daily token budget answers 429 with a wait
    #: measured in hours; sleeping on that is strictly worse than trying the other
    #: provider, which is sitting idle.
    retry_after_max: float = Field(default=5.0, alias="KESTREL_RETRY_AFTER_MAX")

    #: Total wall-clock a single provider may spend across all of its retries.
    #: Without this the budget is per-attempt, so three retries of a 45 s timeout
    #: is 135 s on one provider before the failover is even considered.
    request_deadline: float = Field(default=60.0, alias="KESTREL_REQUEST_DEADLINE")
    # Free tier is ~40 RPM; stay under it deliberately rather than discovering it.
    rate_limit_rpm: int = Field(default=30, alias="KESTREL_RATE_LIMIT_RPM")

    @field_validator("db_path", "cassette_dir", "frame_dir", "sites_dir", "footage_dir")
    @classmethod
    def _absolutise(cls, v: Path) -> Path:
        """Resolve relative paths against the repo root, not the process CWD.

        Without this, running the API from a different directory silently creates
        a second empty database.
        """
        return v if v.is_absolute() else (REPO_ROOT / v)

    # ── derived ───────────────────────────────────────────────────────────
    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def can_go_live(self) -> bool:
        """True when at least one provider credential is present."""
        return bool(self.nvidia_api_key or self.groq_api_key)

    @property
    def effective_mode(self) -> Mode:
        """Degrade to replay rather than failing when no credential is available.

        A reviewer who clones the repo and runs it with no ``.env`` gets a working
        system, not a stack trace.
        """
        if self.mode in (Mode.LIVE, Mode.RECORD) and not self.can_go_live:
            return Mode.REPLAY
        return self.mode

    def ensure_dirs(self) -> None:
        for p in (self.cassette_dir, self.frame_dir, self.db_path.parent):
            p.mkdir(parents=True, exist_ok=True)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor. Call this rather than constructing ``Settings`` directly."""
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
