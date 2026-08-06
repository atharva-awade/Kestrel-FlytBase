"""Resolve how to get joint image+text embeddings from NIM.

`nvidia/nvclip` is in the catalogue but 404s on /v1/embeddings, while
`nvidia/nv-embedqa-e5-v5` succeeds on that same path — so the 404 is model
routing, not a bad URL. The catalogue also offers two vision-language embedding
models that may serve the same purpose.

This probe finds a working image-embedding path by brute force, because
image→vector is load-bearing for CLIP-style frame search and entity re-ID.
"""

from __future__ import annotations

import base64
import io
import json
import os

import httpx
from dotenv import load_dotenv
from PIL import Image, ImageDraw

load_dotenv()

KEY = os.getenv("NVIDIA_API_KEY", "")
H = {"Authorization": f"Bearer {KEY}", "Accept": "application/json"}
T = httpx.Timeout(180.0, connect=15.0)   # NIM cold starts can exceed 80s


def img_uri() -> str:
    im = Image.new("RGB", (336, 336), (190, 200, 214))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 220, 336, 336], fill=(118, 122, 128))
    d.rectangle([90, 120, 240, 225], fill=(28, 78, 168))
    d.ellipse([105, 212, 140, 248], fill=(20, 20, 24))
    d.ellipse([200, 212, 235, 248], fill=(20, 20, 24))
    b = io.BytesIO()
    im.save(b, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()


URI = img_uri()

# Candidate (host+path) endpoints for embedding models.
PATHS = [
    "https://integrate.api.nvidia.com/v1/embeddings",
    "https://ai.api.nvidia.com/v1/retrieval/nvidia/nvclip/embeddings",
    "https://ai.api.nvidia.com/v1/retrieval/nvidia/nvclip",
    "https://ai.api.nvidia.com/v1/embeddings",
]

# Candidate models that might accept an image.
MODELS = [
    "nvidia/nvclip",
    "nvidia/llama-3.2-nemoretriever-1b-vlm-embed-v1",
    "nvidia/llama-nemotron-embed-vl-1b-v2",
]


def body_variants(model: str) -> list[tuple[str, dict]]:
    """Different NIM embedding families disagree on how images are passed."""
    return [
        ("input:[data-uri]", {"model": model, "input": [URI], "encoding_format": "float"}),
        (
            "input:[data-uri]+input_type",
            {
                "model": model,
                "input": [URI],
                "encoding_format": "float",
                "input_type": "passage",
                "truncate": "NONE",
            },
        ),
        (
            "input:[{type:image_url}]",
            {
                "model": model,
                "input": [{"type": "image_url", "image_url": {"url": URI}}],
                "encoding_format": "float",
                "input_type": "passage",
            },
        ),
        (
            "input:[{image:b64}]",
            {"model": model, "input": [{"image": URI}], "encoding_format": "float"},
        ),
    ]


print("\nProbing image-embedding paths")
print("=" * 100)

winners: list[dict] = []
notes: list[str] = []

for model in MODELS:
    print(f"\n── {model}")
    found = False
    for path in PATHS:
        if found:
            break
        for label, body in body_variants(model):
            try:
                r = httpx.post(path, headers=H, json=body, timeout=T)
            except Exception as e:
                notes.append(f"{model} {path} {label} → {type(e).__name__}")
                continue

            if r.status_code < 400:
                try:
                    vec = r.json()["data"][0]["embedding"]
                except Exception:
                    print(f"   [??]  {label:<26} 2xx but unexpected body: {r.text[:100]}")
                    continue
                print(f"   [OK]  {label:<26} dim={len(vec)}  @ {path}")
                winners.append(
                    {"model": model, "path": path, "shape": label, "dim": len(vec)}
                )
                found = True
                break
            else:
                msg = r.text[:110].replace("\n", " ")
                print(f"   [{r.status_code}] {label:<26} {msg}")
                notes.append(f"{model} {label} → {r.status_code} {msg}")

# Text side of the same model, to confirm a shared joint space.
print("\n── text embedding on the winning model(s)")
for w in winners:
    body = {"model": w["model"], "input": ["a blue pickup truck"], "encoding_format": "float"}
    if "input_type" in w["shape"]:
        body |= {"input_type": "query", "truncate": "NONE"}
    try:
        r = httpx.post(w["path"], headers=H, json=body, timeout=T)
        if r.status_code < 400:
            dim = len(r.json()["data"][0]["embedding"])
            same = "SAME SPACE" if dim == w["dim"] else f"MISMATCH (img {w['dim']})"
            print(f"   [OK]  {w['model']:<48} text dim={dim}  {same}")
            w["text_dim"] = dim
        else:
            print(f"   [{r.status_code}] {w['model']:<48} {r.text[:90]}")
    except Exception as e:
        print(f"   [ERR] {w['model']:<48} {type(e).__name__}")

print("\n" + "=" * 100)
if winners:
    print("USABLE IMAGE-EMBEDDING CONFIGURATIONS:")
    for w in winners:
        print(f"   · {w['model']}")
        print(f"       path  {w['path']}")
        print(f"       shape {w['shape']}   img_dim={w['dim']} text_dim={w.get('text_dim','?')}")
else:
    print("NO hosted image-embedding path worked → use a local CLIP on the GPU instead.")

with open("data/probe_embeddings.json", "w", encoding="utf-8") as f:
    json.dump({"winners": winners, "notes": notes[:60]}, f, indent=2)
print("\n→ wrote data/probe_embeddings.json")
