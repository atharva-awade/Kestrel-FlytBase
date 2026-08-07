"""The single door every model call in KESTREL goes through.

Nothing else in the codebase talks to a provider directly. That gives us one place
to meter cost, enforce rate limits, record cassettes, and fail over between
providers — and it means the perception cascade can be reasoned about without
knowing which vendor is behind any given tier.

The four capabilities, mapped to the cascade:

    chat()          tier 3 cognitive escalation, rules, narrative, the agent
    describe()      tier 3 semantic perception (fast VLM)
    describe_deep() tier 4 — async only; measured at 57-84s
    embed_image()   tier 2 — joint image/text space, for re-ID and CLIP search
    embed_text()    tier 2 — same space as embed_image(), verified
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from kestrel.clients.cassette import CassetteStore, image_to_data_uri
from kestrel.clients.provider import (
    CassetteMiss,
    CircuitOpen,
    Provider,
    ProviderError,
)
from kestrel.config import Settings, get_settings
from kestrel.obs.meter import Stage

CHAT = "/chat/completions"
EMBEDDINGS = "/embeddings"


class ModelClient:
    """Capability-oriented facade over the configured providers."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()
        self.cassettes = CassetteStore(self.settings.cassette_dir)

        self.nvidia = Provider(
            "nvidia",
            self.settings.nvidia_base_url,
            self.settings.nvidia_api_key,
            self.settings,
            self.cassettes,
        )
        self.groq = Provider(
            "groq",
            self.settings.groq_base_url,
            self.settings.groq_api_key,
            self.settings,
            self.cassettes,
        )

    async def aclose(self) -> None:
        await asyncio.gather(self.nvidia.aclose(), self.groq.aclose())

    # ── provider routing ─────────────────────────────────────────────────
    def _text_chain(self) -> list[tuple[Provider, str]]:
        """Ordered (provider, model) attempts for text generation.

        Groq leads on measured latency — 227ms against NIM's 83s cold start for
        the same model class — with NIM as the failover. Only vision and embedding
        work is NIM-exclusive, because Groq does not host those.
        """
        primary = (
            (self.groq, self.settings.llm)
            if self.settings.llm_provider == "groq"
            else (self.nvidia, self.settings.llm)
        )
        secondary = (
            (self.nvidia, self.settings.llm_fallback)
            if self.settings.llm_provider == "groq"
            else (self.groq, self.settings.llm_fallback)
        )
        return [primary, secondary]

    # ── text ─────────────────────────────────────────────────────────────
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        stage: Stage = Stage.REASON,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        json_schema: dict[str, Any] | None = None,
        model: str | None = None,
        router: bool = False,
    ) -> str:
        """Generate text, failing over between providers.

        ``router=True`` selects the small fast model — used by the agent's intent
        classifier so that trivial requests never wake the 70B.
        """
        chain = self._text_chain()
        if model:
            chain = [(self.groq if self.settings.llm_provider == "groq" else self.nvidia, model)]
        elif router:
            chain = [(self.groq, self.settings.llm_router), *chain]

        errors: list[str] = []
        for provider, model_id in chain:
            if not provider.available and self.settings.effective_mode.value != "replay":
                continue
            payload: dict[str, Any] = {
                "model": model_id,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            if json_schema is not None:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "kestrel_response",
                        "schema": json_schema,
                        "strict": True,
                    },
                }
            try:
                body = await provider.post(CHAT, payload, stage=stage)
                return (body["choices"][0]["message"]["content"] or "").strip()
            except (ProviderError, CircuitOpen, CassetteMiss, KeyError, IndexError) as e:
                errors.append(f"{provider.name}/{model_id}: {e}")
                # If schema-constrained decoding is unsupported, retry the same
                # provider without it — the caller validates and repairs anyway.
                if json_schema is not None:
                    try:
                        payload.pop("response_format", None)
                        body = await provider.post(CHAT, payload, stage=stage)
                        return (body["choices"][0]["message"]["content"] or "").strip()
                    except Exception as e2:
                        errors.append(f"{provider.name}/{model_id} (no schema): {e2}")
                continue
        raise ProviderError("all text providers failed → " + " | ".join(errors[:4]))

    async def chat_json(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        *,
        stage: Stage = Stage.REASON,
        max_tokens: int = 1024,
        repair_attempts: int = 1,
    ) -> dict[str, Any]:
        """Chat that must return JSON matching ``schema``.

        Constrained decoding is requested, but never trusted: the probe found NIM's
        ``response_format`` support inconclusive, and models emit prose fences even
        when told not to. So we parse defensively and, on failure, hand the error
        back to the model once with the malformed output attached.
        """
        raw = await self.chat(
            messages, stage=stage, max_tokens=max_tokens, json_schema=schema
        )
        for attempt in range(repair_attempts + 1):
            parsed = _loads_lenient(raw)
            if parsed is not None:
                return parsed
            if attempt == repair_attempts:
                break
            raw = await self.chat(
                [
                    *messages,
                    {"role": "assistant", "content": raw[:2000]},
                    {
                        "role": "user",
                        "content": (
                            "That was not valid JSON for the required schema. "
                            "Return ONLY the JSON object, no prose, no code fence."
                        ),
                    },
                ],
                stage=stage,
                max_tokens=max_tokens,
            )
        raise ProviderError(f"model did not return parseable JSON: {raw[:300]}")

    # ── vision ───────────────────────────────────────────────────────────
    async def describe(
        self,
        image: bytes | str,
        prompt: str,
        *,
        deep: bool = False,
        max_tokens: int = 512,
        temperature: float = 0.0,
        model: str | None = None,
    ) -> str:
        """Caption or interrogate a frame.

        Vision is NIM-only — Groq hosts no VLM — so there is no cross-provider
        failover here. The confirmed transport is a chat completion carrying an
        ``image_url`` content part with a base64 data URI.
        """
        uri = image if isinstance(image, str) else image_to_data_uri(image)
        model_id = model or (self.settings.vlm_deep if deep else self.settings.vlm_fast)
        payload = {
            "model": model_id,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": uri}},
                    ],
                }
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        body = await self.nvidia.post(
            CHAT,
            payload,
            stage=Stage.PERCEIVE_DEEP if deep else Stage.PERCEIVE,
            deep=deep,
        )
        return (body["choices"][0]["message"]["content"] or "").strip()

    async def describe_deep(self, image: bytes | str, prompt: str, **kw: Any) -> str:
        """Tier-4 re-look. Measured at 57-84s, so callers must treat this as
        background enrichment and never await it on a request path."""
        return await self.describe(image, prompt, deep=True, **kw)

    # ── embeddings ───────────────────────────────────────────────────────
    async def embed_image(self, image: bytes | str) -> list[float]:
        """Embed a frame or crop into the joint image/text space (2048-d).

        ``input_type`` is mandatory: this model family is asymmetric and rejects
        the request without it. That is not documented; it came from the probe.
        """
        uri = image if isinstance(image, str) else image_to_data_uri(image)
        body = await self.nvidia.post(
            EMBEDDINGS,
            {
                "model": self.settings.vl_embed,
                "input": [uri],
                "encoding_format": "float",
                "input_type": "passage",
                "truncate": "NONE",
            },
            stage=Stage.EMBED,
        )
        return body["data"][0]["embedding"]

    async def embed_text(
        self, text: str, *, kind: Literal["query", "passage"] = "query", joint: bool = True
    ) -> list[float]:
        """Embed text.

        ``joint=True`` uses the vision-language model so the vector lands in the
        *same* space as ``embed_image`` — that shared space is what makes
        text→image search work. ``joint=False`` uses the text-only retrieval model
        for caption/summary search, which is a different, 1024-d index.
        """
        model_id = self.settings.vl_embed if joint else self.settings.text_embed
        body = await self.nvidia.post(
            EMBEDDINGS,
            {
                "model": model_id,
                "input": [text],
                "encoding_format": "float",
                "input_type": kind,
                "truncate": "NONE",
            },
            stage=Stage.EMBED,
        )
        return body["data"][0]["embedding"]

    async def embed_batch(
        self, texts: list[str], *, kind: Literal["query", "passage"] = "passage",
        joint: bool = True,
    ) -> list[list[float]]:
        """Batch text embedding. One request beats N when indexing a shift."""
        if not texts:
            return []
        model_id = self.settings.vl_embed if joint else self.settings.text_embed
        body = await self.nvidia.post(
            EMBEDDINGS,
            {
                "model": model_id,
                "input": texts,
                "encoding_format": "float",
                "input_type": kind,
                "truncate": "NONE",
            },
            stage=Stage.EMBED,
        )
        rows = sorted(body["data"], key=lambda d: d.get("index", 0))
        return [r["embedding"] for r in rows]

    # ── health ───────────────────────────────────────────────────────────
    @property
    def health(self) -> dict[str, Any]:
        return {
            "mode": self.settings.effective_mode.value,
            "requested_mode": self.settings.mode.value,
            "providers": [self.nvidia.health, self.groq.health],
            "cassettes": self.cassettes.stats,
            "roster": {
                "vlm_fast": self.settings.vlm_fast,
                "vlm_deep": self.settings.vlm_deep,
                "vl_embed": f"{self.settings.vl_embed} ({self.settings.vl_embed_dim}d)",
                "text_embed": f"{self.settings.text_embed} ({self.settings.text_embed_dim}d)",
                "llm": f"{self.settings.llm_provider}/{self.settings.llm}",
                "llm_router": self.settings.llm_router,
            },
        }


def _loads_lenient(raw: str) -> dict[str, Any] | None:
    """Parse JSON that a language model produced.

    Handles the three things models actually do: wrap JSON in a ``json`` fence,
    prepend an explanatory sentence, and append a trailing comment. Anything
    weirder is a genuine failure and returns None so the caller can repair.
    """
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1] if "\n" in s else s
        s = s.rsplit("```", 1)[0]
        s = s.removeprefix("json").strip()
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost brace-balanced span.
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(s[start:], start=start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    v = json.loads(s[start : i + 1])
                    return v if isinstance(v, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


_CLIENT: ModelClient | None = None


def get_client() -> ModelClient:
    """Process-wide client. Reuses HTTP connections across the whole pipeline."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = ModelClient()
    return _CLIENT
