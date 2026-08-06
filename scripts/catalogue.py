"""Dump the NIM model catalogue visible to our key, grouped by role.

The hosted catalogue changes over time and differs per key, so model selection in
KESTREL is grounded in this output rather than in documentation.
"""

from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

key = os.getenv("NVIDIA_API_KEY", "")
base = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")

if not key:
    sys.exit("NVIDIA_API_KEY not set")

r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=60)
r.raise_for_status()
ids = sorted(m["id"] for m in r.json()["data"])

GROUPS: dict[str, list[str]] = {
    "EMBEDDING / CLIP / RERANK": ["embed", "clip", "rerank", "rank", "retriev", "cosmos"],
    "VISION / VLM / DETECTION": [
        "vision", "vila", "florence", "dino", "ocr", "paligemma",
        "phi-3.5-v", "nemoretriever", "-vl", "vl-", "llava", "qwen2-vl", "qwen2.5-vl",
    ],
    "REASONING (large text)": ["70b", "nemotron", "deepseek", "405b", "253b", "49b"],
}

seen: set[str] = set()
for title, keys in GROUPS.items():
    hits = [i for i in ids if any(k in i.lower() for k in keys)]
    seen.update(hits)
    print(f"\n=== {title}  ({len(hits)}) ===")
    for i in hits:
        print(f"   {i}")

print(f"\n=== EVERYTHING ELSE  ({len(ids) - len(seen)}) ===")
for i in ids:
    if i not in seen:
        print(f"   {i}")

print(f"\nTOTAL: {len(ids)} models")
