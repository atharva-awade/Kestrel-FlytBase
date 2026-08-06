"""Exercise the client layer end to end: live call → cassette → replay.

This proves the property the whole submission leans on — that KESTREL runs with no
API key — by doing it: record against live providers, then re-run the identical
requests with the network disabled and confirm the answers are byte-identical.
"""

from __future__ import annotations

import asyncio
import io
import time

from PIL import Image, ImageDraw

from kestrel.clients import ModelClient
from kestrel.config import Mode, Settings
from kestrel.obs import METER


def frame() -> bytes:
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
    return b.getvalue()


IMG = frame()

SCENE_SCHEMA = {
    "type": "object",
    "properties": {
        "caption": {"type": "string"},
        "objects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "colour": {"type": "string"},
                },
                "required": ["label", "colour"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["caption", "objects"],
    "additionalProperties": False,
}


def cos(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return num / (na * nb) if na and nb else 0.0


async def run(mode: Mode) -> dict:
    s = Settings(KESTREL_MODE=mode.value)  # type: ignore[call-arg]
    c = ModelClient(s)
    out: dict = {}
    print(f"\n{'=' * 90}\nMODE = {mode.value}  (effective: {s.effective_mode.value})\n{'=' * 90}")

    # 1 — text, with provider failover
    t0 = time.perf_counter()
    out["chat"] = await c.chat(
        [{"role": "user", "content": "Reply with exactly: KESTREL ONLINE"}], max_tokens=16
    )
    print(f"  chat            {(time.perf_counter()-t0)*1000:8.0f}ms  {out['chat']!r}")

    # 2 — router tier (small model)
    t0 = time.perf_counter()
    out["router"] = await c.chat(
        [{"role": "user", "content": "Classify: 'show me all trucks'. One word: LOOKUP or ACTION."}],
        router=True,
        max_tokens=8,
    )
    print(f"  chat(router)    {(time.perf_counter()-t0)*1000:8.0f}ms  {out['router']!r}")

    # 3 — VLM perception
    t0 = time.perf_counter()
    out["vlm"] = await c.describe(
        IMG, "Describe this security camera frame in one sentence.", max_tokens=96
    )
    print(f"  describe        {(time.perf_counter()-t0)*1000:8.0f}ms  {out['vlm'][:70]!r}")

    # 4 — structured output with validate-and-repair
    t0 = time.perf_counter()
    out["json"] = await c.chat_json(
        [
            {
                "role": "user",
                "content": (
                    "A blue pickup truck is parked; a person stands beside it. "
                    "Return JSON with keys caption and objects[{label,colour}]."
                ),
            }
        ],
        SCENE_SCHEMA,
        max_tokens=256,
    )
    print(f"  chat_json       {(time.perf_counter()-t0)*1000:8.0f}ms  {out['json']}")

    # 5 — the joint embedding space, which text→image search depends on
    t0 = time.perf_counter()
    vi = await c.embed_image(IMG)
    vt_match = await c.embed_text("a blue pickup truck on a road")
    vt_other = await c.embed_text("a bowl of hot noodle soup")
    dt = (time.perf_counter() - t0) * 1000
    sim_match, sim_other = cos(vi, vt_match), cos(vi, vt_other)
    out["dims"] = (len(vi), len(vt_match))
    out["sim_match"], out["sim_other"] = sim_match, sim_other
    print(f"  embeddings      {dt:8.0f}ms  img={len(vi)}d text={len(vt_match)}d")
    print(f"    cos(img, 'blue pickup truck') = {sim_match:+.4f}")
    print(f"    cos(img, 'noodle soup')       = {sim_other:+.4f}")
    verdict = "DISCRIMINATES" if sim_match > sim_other else "NO SIGNAL"
    print(f"    → {verdict} (margin {sim_match - sim_other:+.4f})")

    print(f"\n  cassettes: {c.cassettes.stats}")
    await c.aclose()
    return out


async def main() -> None:
    live = await run(Mode.LIVE)
    replay = await run(Mode.REPLAY)

    print(f"\n{'=' * 90}\nDETERMINISM CHECK — live vs replay\n{'=' * 90}")
    ok = True
    for k in ("chat", "vlm", "json"):
        same = live[k] == replay[k]
        ok &= same
        print(f"  {k:<10} {'identical' if same else 'DIFFERS'}")
    print(f"\n  {'PASS — replay reproduces live exactly' if ok else 'FAIL — replay diverged'}")

    print(f"\n{'=' * 90}\nMETER\n{'=' * 90}")
    snap = METER.snapshot(observed_seconds=60.0)
    for stage, st in snap["stages"].items():
        print(
            f"  {stage:<14} calls={st['calls']:<4} cached={st['cached']:<4} "
            f"p50={st['p50_ms']:>8.0f}ms p95={st['p95_ms']:>8.0f}ms "
            f"tok={st['tokens_in']}/{st['tokens_out']} ${st['cost_usd']:.6f}"
        )
    print(f"\n  modelled cost this run: ${snap['cost']['modelled_usd']:.6f}")
    print(f"  basis: {snap['cost']['basis']}")


if __name__ == "__main__":
    asyncio.run(main())
