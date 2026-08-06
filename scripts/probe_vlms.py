"""Survey every vision-language model in the catalogue.

Two jobs:
  1. Decide which VLM serves the fast perception tier and which serves escalation.
  2. Seed the model leaderboard the report uses to justify that choice with data
     rather than opinion.

Latency here is wall-clock against a warm-or-cold hosted endpoint, so a slow first
result may be a cold start rather than a slow model. Each model therefore gets two
attempts, and both timings are recorded.
"""

from __future__ import annotations

import base64
import io
import json
import os
import time

import httpx
from dotenv import load_dotenv
from PIL import Image, ImageDraw

load_dotenv()

KEY = os.getenv("NVIDIA_API_KEY", "")
BASE = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
H = {"Authorization": f"Bearer {KEY}", "Accept": "application/json"}
T = httpx.Timeout(240.0, connect=15.0)

CANDIDATES = [
    "meta/llama-3.2-11b-vision-instruct",
    "meta/llama-3.2-90b-vision-instruct",
    "nvidia/vila",
    "nvidia/nemotron-nano-12b-v2-vl",
    "nvidia/llama-3.1-nemotron-nano-vl-8b-v1",
    "microsoft/phi-3-vision-128k-instruct",
    "microsoft/kosmos-2",
    "nvidia/neva-22b",
    "adept/fuyu-8b",
]

PROMPT = (
    "You are a security camera analyst. Describe this scene in one sentence, "
    "then list every vehicle and person visible with their colour."
)


def scene() -> str:
    im = Image.new("RGB", (640, 384), (186, 196, 210))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 250, 640, 384], fill=(116, 120, 126))
    d.rectangle([180, 148, 420, 258], fill=(28, 78, 168))
    d.rectangle([388, 168, 470, 245], fill=(32, 88, 190))
    d.ellipse([208, 244, 258, 292], fill=(22, 22, 26))
    d.ellipse([368, 244, 418, 292], fill=(22, 22, 26))
    d.ellipse([540, 204, 561, 227], fill=(226, 200, 172))
    d.rectangle([543, 227, 558, 276], fill=(58, 66, 90))
    b = io.BytesIO()
    im.save(b, format="JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()


URI = scene()


def call(model: str) -> tuple[bool, float, str, dict]:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": URI}},
                ],
            }
        ],
        "max_tokens": 160,
        "temperature": 0,
    }
    t0 = time.perf_counter()
    try:
        r = httpx.post(f"{BASE}/chat/completions", headers=H, json=payload, timeout=T)
    except Exception as e:
        return False, time.perf_counter() - t0, f"{type(e).__name__}", {}
    dt = time.perf_counter() - t0
    if r.status_code >= 400:
        return False, dt, f"HTTP {r.status_code}: {r.text[:120]}", {}
    body = r.json()
    try:
        txt = body["choices"][0]["message"]["content"]
    except Exception:
        return False, dt, f"unexpected body: {json.dumps(body)[:120]}", {}
    return True, dt, (txt or "").strip(), body.get("usage") or {}


print("\nVLM survey — 2 attempts each (attempt 1 may include cold start)")
print("=" * 108)

rows: list[dict] = []
for m in CANDIDATES:
    print(f"\n── {m}")
    attempts = []
    ok = False
    text = ""
    usage: dict = {}
    for i in (1, 2):
        o, dt, out, u = call(m)
        attempts.append({"attempt": i, "ok": o, "seconds": round(dt, 2)})
        status = "OK " if o else "ERR"
        print(f"   [{status}] attempt {i}: {dt:6.2f}s  {out[:96].replace(chr(10), ' ')}")
        if o:
            ok, text, usage = True, out, u
            if dt < 8:          # already warm; a second timing adds nothing
                break
    rows.append(
        {
            "model": m,
            "ok": ok,
            "attempts": attempts,
            "best_seconds": min([a["seconds"] for a in attempts if a["ok"]], default=None),
            "caption": text[:600],
            "usage": usage,
            # Cheap sanity signals for the leaderboard: did it see the salient things?
            "mentions_blue": "blue" in text.lower(),
            "mentions_vehicle": any(
                w in text.lower() for w in ("truck", "vehicle", "car", "van", "pickup")
            ),
            "mentions_person": any(w in text.lower() for w in ("person", "man", "woman", "people", "pedestrian", "figure")),
        }
    )

print("\n" + "=" * 108)
print(f"{'MODEL':<46}{'OK':<5}{'BEST':<9}{'BLUE':<6}{'VEH':<5}{'PERSON':<7}")
print("-" * 108)
for r in rows:
    print(
        f"{r['model']:<46}"
        f"{('y' if r['ok'] else 'n'):<5}"
        f"{(str(r['best_seconds']) + 's' if r['best_seconds'] else '-'):<9}"
        f"{('y' if r['mentions_blue'] else '-'):<6}"
        f"{('y' if r['mentions_vehicle'] else '-'):<5}"
        f"{('y' if r['mentions_person'] else '-'):<7}"
    )

working = [r for r in rows if r["ok"]]
print(f"\n{len(working)}/{len(rows)} vision models reachable")
if working:
    fastest = min(working, key=lambda r: r["best_seconds"] or 1e9)
    print(f"fastest: {fastest['model']} @ {fastest['best_seconds']}s")

os.makedirs("data", exist_ok=True)
with open("data/probe_vlms.json", "w", encoding="utf-8") as f:
    json.dump({"prompt": PROMPT, "results": rows}, f, indent=2)
print("→ wrote data/probe_vlms.json")
