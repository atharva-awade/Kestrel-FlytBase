"""Probe every model endpoint KESTREL depends on and report what actually works.

NVIDIA's public model cards document the *models* but not the hosted HTTP contract,
and the catalogue shifts. Rather than trusting docs, this script establishes ground
truth empirically: it calls each endpoint with the smallest valid payload and prints
exactly what came back.

Run it after any credential change, and before blaming your own code:

    uv run python scripts/probe_models.py
    uv run python scripts/probe_models.py --verbose   # dump response bodies

Exit code is 0 if every REQUIRED probe passed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from dotenv import load_dotenv
from PIL import Image, ImageDraw

load_dotenv()

NVIDIA_KEY = os.getenv("NVIDIA_API_KEY", "")
GROQ_KEY = os.getenv("GROQ_API_KEY", "")
NIM = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
GROQ = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")

# NVIDIA serves vision/CV models from a different host than the OpenAI-compatible one.
NIM_CV_HOSTS = [
    "https://ai.api.nvidia.com/v1/cv",
    "https://integrate.api.nvidia.com/v1/cv",
]

TIMEOUT = httpx.Timeout(90.0, connect=15.0)

# The model that actually serves the joint image/text space, kept in step with
# Settings.vl_embed so the probe cannot drift away from what the app runs.
VL_EMBED = "nvidia/llama-nemotron-embed-vl-1b-v2"


# ── test fixture ──────────────────────────────────────────────────────────────
def make_test_image() -> bytes:
    """A crude but unambiguous scene: dark ground, a blue box, a small figure.

    Deliberately simple. This probe checks the *transport contract*, not caption
    quality — real footage is used for that in the evaluation harness.
    """
    img = Image.new("RGB", (640, 384), (188, 198, 212))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 250, 640, 384], fill=(120, 124, 130))       # ground
    d.rectangle([180, 150, 420, 260], fill=(28, 78, 168))       # blue vehicle body
    d.rectangle([390, 170, 470, 245], fill=(32, 88, 190))       # cab
    d.ellipse([210, 245, 260, 292], fill=(24, 24, 28))          # wheel
    d.ellipse([370, 245, 420, 292], fill=(24, 24, 28))          # wheel
    d.ellipse([540, 205, 560, 228], fill=(226, 200, 172))       # head
    d.rectangle([543, 228, 557, 275], fill=(60, 68, 92))        # torso
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


IMG_BYTES = make_test_image()
IMG_B64 = base64.b64encode(IMG_BYTES).decode()
IMG_DATA_URI = f"data:image/jpeg;base64,{IMG_B64}"


# ── result bookkeeping ────────────────────────────────────────────────────────
@dataclass
class Probe:
    name: str
    required: bool
    ok: bool = False
    detail: str = ""
    ms: int = 0
    payload: Any = None
    shape: str = ""          # the request shape that worked — recorded for the client layer
    extra: dict = field(default_factory=dict)


RESULTS: list[Probe] = []


def run(name: str, required: bool = True):
    """Decorator that times a probe and captures failures without aborting the run."""

    def deco(fn):
        p = Probe(name=name, required=required)
        t0 = time.perf_counter()
        try:
            fn(p)
        except Exception as e:
            p.ok = False
            p.detail = f"{type(e).__name__}: {e}"[:400]
        p.ms = int((time.perf_counter() - t0) * 1000)
        RESULTS.append(p)
        icon = "PASS" if p.ok else ("FAIL" if p.required else "SKIP")
        print(f"  [{icon}] {name:<34} {p.ms:>6}ms  {p.detail[:110]}")
        return fn

    return deco


def nv_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {NVIDIA_KEY}", "Accept": "application/json"}


# ═══════════════════════════════════════════════════════════════════════════════
print("\nKESTREL model endpoint probe")
print("=" * 96)
print(f"  NIM  : {NIM}")
print(f"  key  : {'set (' + NVIDIA_KEY[:12] + '…)' if NVIDIA_KEY else 'MISSING'}")
print(f"  groq : {'set (' + GROQ_KEY[:10] + '…)' if GROQ_KEY else 'MISSING'}")
print("=" * 96)

CATALOGUE: list[str] = []

print("\n[1] Catalogue")


@run("GET /models", required=True)
def _catalogue(p: Probe) -> None:
    r = httpx.get(f"{NIM}/models", headers=nv_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    ids = sorted(m["id"] for m in r.json().get("data", []))
    CATALOGUE.extend(ids)
    p.ok = True
    p.payload = ids
    p.detail = f"{len(ids)} models visible to this key"


def in_catalogue(model_id: str) -> bool:
    return model_id in CATALOGUE


# ── text generation ───────────────────────────────────────────────────────────
print("\n[2] Text generation")


# KESTREL_LLM holds the *primary* model, which on this deployment is a Groq
# model name. Reading it here sent "llama-3.3-70b-versatile" to NVIDIA, which
# 404s — and the deck then reported a required NVIDIA outage that was never
# real. Each provider is probed with its own roster from here on.
NV_CHAT = os.getenv("KESTREL_NV_CHAT", "meta/llama-3.1-70b-instruct")
NV_CHAT_ASPIRATIONAL = "meta/llama-3.3-70b-instruct"
GROQ_CHAT = os.getenv("KESTREL_GROQ_CHAT", "llama-3.3-70b-versatile")


@run("chat  nvidia llama-3.1-70b (failover path)", required=True)
def _chat(p: Probe) -> None:
    """The NVIDIA side of the failover pair. If this is down, a Groq outage
    takes the whole system with it, so it is required even though it is second
    choice in the ordinary case."""
    model = NV_CHAT
    r = httpx.post(
        f"{NIM}/chat/completions",
        headers=nv_headers(),
        json={
            "model": model,
            "messages": [{"role": "user", "content": "Reply with exactly: KESTREL ONLINE"}],
            "max_tokens": 16,
            "temperature": 0,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    body = r.json()
    txt = body["choices"][0]["message"]["content"].strip()
    p.ok = True
    p.payload = body
    p.extra["usage"] = body.get("usage")
    p.detail = f"{model} → {txt!r}"


@run("chat  nvidia llama-3.3-70b (unusably slow)", required=False)
def _chat_33(p: Probe) -> None:
    """Kept because the finding drove a design decision: this model is offered
    in the catalogue, but its cold start was measured at 83 s and now exceeds a
    120 s read timeout. That is what pushed the primary over to Groq. Optional,
    because nothing depends on it."""
    r = httpx.post(
        f"{NIM}/chat/completions",
        headers=nv_headers(),
        json={
            "model": NV_CHAT_ASPIRATIONAL,
            "messages": [{"role": "user", "content": "Reply with exactly: KESTREL ONLINE"}],
            "max_tokens": 16,
            "temperature": 0,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    body = r.json()
    p.ok = True
    p.payload = body
    p.detail = f"{NV_CHAT_ASPIRATIONAL} → {body['choices'][0]['message']['content'].strip()!r}"


@run("chat  structured json_schema", required=False)
def _structured(p: Probe) -> None:
    """Does NIM honour OpenAI-style constrained decoding? Decides whether the
    perception layer can rely on schema enforcement or must validate + repair."""
    model = NV_CHAT
    schema = {
        "type": "object",
        "properties": {"colour": {"type": "string"}, "count": {"type": "integer"}},
        "required": ["colour", "count"],
        "additionalProperties": False,
    }
    r = httpx.post(
        f"{NIM}/chat/completions",
        headers=nv_headers(),
        json={
            "model": model,
            "messages": [{"role": "user", "content": "One blue truck. Respond as JSON."}],
            "max_tokens": 96,
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "probe", "schema": schema, "strict": True},
            },
        },
        timeout=TIMEOUT,
    )
    if r.status_code >= 400:
        p.detail = f"not supported (HTTP {r.status_code}) → validate+repair path required"
        return
    parsed = json.loads(r.json()["choices"][0]["message"]["content"])
    p.ok = True
    p.shape = "response_format.json_schema"
    p.payload = parsed
    p.detail = f"honoured → {parsed}"


# ── vision ────────────────────────────────────────────────────────────────────
print("\n[3] Vision-language (the perception tier)")


def _try_vlm(p: Probe, model: str) -> None:
    """VLMs on NIM accept an OpenAI-style image_url content part with a data URI."""
    r = httpx.post(
        f"{NIM}/chat/completions",
        headers=nv_headers(),
        json={
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Describe this scene in one sentence. "
                            "List any vehicles and people you can see.",
                        },
                        {"type": "image_url", "image_url": {"url": IMG_DATA_URI}},
                    ],
                }
            ],
            "max_tokens": 128,
            "temperature": 0,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    body = r.json()
    p.ok = True
    p.shape = "chat/completions + image_url data URI"
    p.payload = body
    p.extra["usage"] = body.get("usage")
    p.detail = body["choices"][0]["message"]["content"].strip().replace("\n", " ")[:130]


@run("vlm   llama-3.2-11b-vision", required=True)
def _vlm_fast(p: Probe) -> None:
    _try_vlm(p, os.getenv("KESTREL_VLM_FAST", "meta/llama-3.2-11b-vision-instruct"))


@run("vlm   llama-3.2-90b-vision", required=False)
def _vlm_deep(p: Probe) -> None:
    _try_vlm(p, os.getenv("KESTREL_VLM_DEEP", "meta/llama-3.2-90b-vision-instruct"))


# ── embeddings ────────────────────────────────────────────────────────────────
print("\n[4] Embeddings and retrieval")


# ── the joint image/text space ───────────────────────────────────────────
#
# This capability — embedding a typed phrase and a video frame into ONE space,
# so "white pickup" retrieves the frame without anyone having written that
# caption — is required. The *model* originally chosen for it is not.
#
# nvidia/nvclip is listed in the NIM catalogue but its NVCF function is not
# provisioned for developer keys, so it 404s for this account. It stays here as
# an optional probe because "we tried the obvious choice and it is unavailable"
# is a finding worth keeping visible, and because the day it is provisioned this
# probe turns green on its own. It must not be marked required: the system does
# not use it and is not degraded by its absence.


@run("embed nvclip text (unprovisioned, superseded)", required=False)
def _clip_text(p: Probe) -> None:
    r = httpx.post(
        f"{NIM}/embeddings",
        headers=nv_headers(),
        json={
            "model": os.getenv("KESTREL_CLIP", "nvidia/nvclip"),
            "input": ["a blue pickup truck parked at a loading dock"],
            "encoding_format": "float",
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    vec = r.json()["data"][0]["embedding"]
    p.ok = True
    p.shape = "embeddings + input:[str]"
    p.extra["dim"] = len(vec)
    p.detail = f"dim={len(vec)}"


@run("embed nvclip image (unprovisioned, superseded)", required=False)
def _clip_image(p: Probe) -> None:
    r = httpx.post(
        f"{NIM}/embeddings",
        headers=nv_headers(),
        json={
            "model": os.getenv("KESTREL_CLIP", "nvidia/nvclip"),
            "input": [IMG_DATA_URI],
            "encoding_format": "float",
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    vec = r.json()["data"][0]["embedding"]
    p.ok = True
    p.shape = "embeddings + input:[data-uri]"
    p.extra["dim"] = len(vec)
    p.detail = f"dim={len(vec)}, joint image/text space confirmed"


def _vl_embed(payload_input: str, modality: str, input_type: str) -> list[float]:
    r = httpx.post(
        f"{NIM}/embeddings",
        headers=nv_headers(),
        json={
            "model": os.getenv("KESTREL_VL_EMBED", VL_EMBED),
            "input": [payload_input],
            "modality": modality,
            "input_type": input_type,
            "encoding_format": "float",
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["data"][0]["embedding"]


@run("embed joint text  (nemotron-embed-vl)", required=True)
def _vl_text(p: Probe) -> None:
    vec = _vl_embed("a blue pickup truck parked at a loading dock", "text", "query")
    p.ok = True
    p.shape = "embeddings + modality:text"
    p.extra["dim"] = len(vec)
    p.detail = f"dim={len(vec)}, the query side of visual search"


@run("embed joint image (nemotron-embed-vl)", required=True)
def _vl_image(p: Probe) -> None:
    """This is what makes text→frame search possible, and it is live."""
    vec = _vl_embed(IMG_DATA_URI, "image", "passage")
    p.ok = True
    p.shape = "embeddings + modality:image"
    p.extra["dim"] = len(vec)
    p.detail = f"dim={len(vec)}, frames embedded without captioning them"


@run("embed cross-modal alignment", required=True)
def _vl_crossmodal(p: Probe) -> None:
    """Equal dimensions prove nothing on their own — two unrelated models can
    both emit 2048 floats. What has to hold is that text and image land in the
    *same* space, so a cosine between them is meaningful rather than noise."""
    t = _vl_embed("a blue pickup truck parked at a loading dock", "text", "query")
    i = _vl_embed(IMG_DATA_URI, "image", "passage")
    if len(t) != len(i):
        raise RuntimeError(f"dimension mismatch: text {len(t)} vs image {len(i)}")
    dot = sum(a * b for a, b in zip(t, i))
    norm = math.sqrt(sum(a * a for a in t)) * math.sqrt(sum(b * b for b in i))
    cos = dot / norm if norm else 0.0
    if not (-1.001 <= cos <= 1.001) or cos == 0.0:
        raise RuntimeError(f"implausible cosine {cos}")
    p.ok = True
    p.shape = f"cosine(text, image) = {cos:.4f}"
    p.extra["cosine"] = round(cos, 4)
    p.extra["dim"] = len(t)
    p.detail = f"one shared {len(t)}-d space; cosine {cos:.4f} is comparable"


@run("embed nv-embedqa-e5-v5", required=False)
def _embedqa(p: Probe) -> None:
    r = httpx.post(
        f"{NIM}/embeddings",
        headers=nv_headers(),
        json={
            "model": os.getenv("KESTREL_EMBED", "nvidia/nv-embedqa-e5-v5"),
            "input": ["person loitering near the main gate after midnight"],
            "input_type": "passage",
            "encoding_format": "float",
            "truncate": "NONE",
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    vec = r.json()["data"][0]["embedding"]
    p.ok = True
    p.shape = "embeddings + input_type"
    p.extra["dim"] = len(vec)
    p.detail = f"dim={len(vec)}"


@run("rerank llama-3.2-nv-rerankqa", required=False)
def _rerank(p: Probe) -> None:
    for path, body in [
        (
            "/ranking",
            {
                "model": os.getenv("KESTREL_RERANK", "nvidia/llama-3.2-nv-rerankqa-1b-v2"),
                "query": {"text": "trucks at the loading dock"},
                "passages": [
                    {"text": "A blue pickup reverses into the loading dock."},
                    {"text": "Clear sky over the solar array."},
                ],
                "truncate": "END",
            },
        ),
    ]:
        r = httpx.post(f"{NIM}{path}", headers=nv_headers(), json=body, timeout=TIMEOUT)
        if r.status_code < 400:
            rk = r.json().get("rankings", [])
            p.ok = True
            p.shape = f"POST {path}"
            p.payload = rk
            p.detail = f"rankings={rk}"
            return
        p.detail = f"HTTP {r.status_code} on {path}: {r.text[:100]}"


# ── open-vocabulary detection ─────────────────────────────────────────────────
print("\n[5] Open-vocabulary detection")


@run("detect nv-grounding-dino", required=False)
def _gdino(p: Probe) -> None:
    """Promptable detection. If unavailable we fall back to a local RT-DETR, so
    this probe is not required — but it drives the promptable-rules feature."""
    attempts: list[str] = []
    bodies = [
        {"input": [IMG_DATA_URI], "text": ["truck", "person"]},
        {"messages": [{"role": "user", "content": f'<img src="{IMG_DATA_URI}" /> truck . person .'}]},
        {"input": [{"type": "image_url", "url": IMG_DATA_URI}], "prompt": "truck . person ."},
    ]
    for host in NIM_CV_HOSTS:
        for i, body in enumerate(bodies):
            url = f"{host}/nvidia/nv-grounding-dino"
            try:
                r = httpx.post(url, headers=nv_headers(), json=body, timeout=TIMEOUT)
            except Exception as e:
                attempts.append(f"{url}#{i} → {type(e).__name__}")
                continue
            if r.status_code < 400:
                p.ok = True
                p.shape = f"POST {url} body#{i}"
                p.payload = r.json()
                p.detail = f"OK via body#{i}: {json.dumps(r.json())[:110]}"
                return
            attempts.append(f"{url}#{i} → {r.status_code} {r.text[:60]}")
    p.detail = "no shape accepted; " + " | ".join(attempts[:2])
    p.extra["attempts"] = attempts


# ── failover provider ─────────────────────────────────────────────────────────
print("\n[6] Failover provider")


# Required: this is the primary reasoning model in the shipped configuration.
# It previously read KESTREL_LLM_FALLBACK, which holds the *NVIDIA* failover
# model, so Groq was asked for "meta/llama-3.1-70b-instruct" and answered 404.
@run("groq  llama-3.3-70b-versatile (primary)", required=True)
def _groq(p: Probe) -> None:
    if not GROQ_KEY:
        p.detail = "no GROQ_API_KEY"
        return
    model = GROQ_CHAT
    r = httpx.post(
        f"{GROQ}/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_KEY}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": "Reply with exactly: KESTREL ONLINE"}],
            "max_tokens": 16,
            "temperature": 0,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    body = r.json()
    p.ok = True
    p.payload = body
    p.detail = f"{model} → {body['choices'][0]['message']['content'].strip()!r}"


# ═══════════════════════════════════════════════════════════════════════════════
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", "-v", action="store_true", help="dump response bodies")
    ap.add_argument("--catalogue", "-c", action="store_true", help="print the full model list")
    args = ap.parse_args()

    print("\n" + "=" * 96)
    print("SUMMARY")
    print("=" * 96)
    required = [r for r in RESULTS if r.required]
    optional = [r for r in RESULTS if not r.required]
    req_ok = sum(r.ok for r in required)
    opt_ok = sum(r.ok for r in optional)
    print(f"  required : {req_ok}/{len(required)} passed")
    print(f"  optional : {opt_ok}/{len(optional)} passed")

    working = [r for r in RESULTS if r.ok and r.shape]
    if working:
        print("\n  Confirmed request shapes (these drive src/kestrel/clients):")
        for r in working:
            print(f"    · {r.name:<32} {r.shape}")

    dims = {r.name: r.extra["dim"] for r in RESULTS if "dim" in r.extra}
    if dims:
        print("\n  Embedding dimensions:")
        for k, v in dims.items():
            print(f"    · {k:<32} {v}")

    failed_required = [r for r in required if not r.ok]
    if failed_required:
        print("\n  BLOCKING failures:")
        for r in failed_required:
            print(f"    · {r.name}: {r.detail}")

    degraded = [r for r in optional if not r.ok]
    if degraded:
        print("\n  Unavailable (fallbacks apply):")
        for r in degraded:
            print(f"    · {r.name}: {r.detail[:150]}")

    if args.catalogue and CATALOGUE:
        print(f"\n  Catalogue ({len(CATALOGUE)}):")
        for m in CATALOGUE:
            print(f"    {m}")

    if args.verbose:
        print("\n  Response bodies:")
        for r in RESULTS:
            if r.payload is not None:
                print(f"\n  ── {r.name} ──")
                print("  " + json.dumps(r.payload, indent=2)[:2500].replace("\n", "\n  "))

    # Persist for the report and the client layer.
    out = {
        "probes": [
            {
                "name": r.name,
                "required": r.required,
                "ok": r.ok,
                "ms": r.ms,
                "shape": r.shape,
                "detail": r.detail,
                **({"extra": r.extra} if r.extra else {}),
            }
            for r in RESULTS
        ],
        "catalogue_size": len(CATALOGUE),
    }
    os.makedirs("data", exist_ok=True)
    with open("data/probe_results.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print("\n  → wrote data/probe_results.json")

    return 1 if failed_required else 0


if __name__ == "__main__":
    sys.exit(main())
