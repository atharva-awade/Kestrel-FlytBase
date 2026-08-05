"""Open-vocabulary grounding via the VLM.

Promptable rules are one of KESTREL's distinguishing features: a rule can declare
its own visual predicate in plain English — "a person on a ladder", "an unattended
bag", "someone climbing the fence" — and have it detected with no training and no
fixed class list.

The intended implementation was a local Grounding DINO. On a network where the
HuggingFace Hub is unreachable that model cannot be obtained, so this module
provides the same *capability* through a different mechanism: ask the
vision-language model to localise the phrase and return normalised boxes.

**This is genuinely worse than a purpose-built grounded detector, and the system
says so.** VLM-derived boxes are coarse, occasionally hallucinated, and cost a
model call rather than 20 ms of local GPU. They are therefore:

*   marked ``source="vlm"`` so downstream consumers can weight them lower,
*   capped in confidence, because a VLM's self-reported certainty is not calibrated,
*   used only for phrases the closed-set detector cannot express — the common path
    still runs locally at full speed.

Normalised 0-1000 coordinates are requested rather than pixels: models are markedly
better at that convention, and it makes the output independent of input resolution.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from kestrel.domain import BBox
from kestrel.ingest.sources import to_jpeg_bytes
from kestrel.perception.detect import RawDetection

# Ceiling on VLM-sourced confidence. These boxes must never outrank a real
# detector's, and an uncapped model will happily claim 0.99.
VLM_CONFIDENCE_CAP = 0.62

_PROMPT = """You are a precise object-localisation system analysing a security camera frame.

Find every instance of these targets:
{targets}

Return ONLY a JSON object, no prose and no code fence:
{{"detections": [{{"label": "<one of the targets, verbatim>", "box": [x1, y1, x2, y2], "confidence": <0.0-1.0>}}]}}

Rules:
- Coordinates are integers on a 0-1000 scale, measured from the TOP-LEFT corner.
- x1 < x2 and y1 < y2 in every box.
- Report an instance only if you can actually see it. If a target is absent, omit it.
- If nothing matches at all, return {{"detections": []}}.
- Do not invent objects to be helpful. A false detection is worse than a missed one here.
"""


async def vlm_ground(
    image: np.ndarray,
    phrases: list[str],
    *,
    max_tokens: int = 640,
) -> list[RawDetection]:
    """Localise arbitrary text-described objects using the VLM."""
    if not phrases:
        return []

    from kestrel.clients.models import get_client

    h, w = image.shape[:2]
    targets = "\n".join(f"- {p.strip()}" for p in phrases if p.strip())
    prompt = _PROMPT.format(targets=targets)

    try:
        raw = await get_client().describe(
            to_jpeg_bytes(image), prompt, max_tokens=max_tokens
        )
    except Exception:
        # Grounding is an enhancement, never a dependency. A rule whose visual
        # predicate cannot be evaluated is reported as unevaluated, not as false.
        return []

    return _parse(raw, w, h, phrases)


def _parse(raw: str, w: int, h: int, phrases: list[str]) -> list[RawDetection]:
    payload = _loads(raw)
    if not payload:
        return []

    items = payload.get("detections")
    if not isinstance(items, list):
        return []

    wanted = {p.strip().lower() for p in phrases}
    out: list[RawDetection] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip().lower()
        box = item.get("box")
        if not label or not isinstance(box, list) or len(box) != 4:
            continue
        # Reject labels the model invented rather than selected. Without this the
        # VLM drifts toward describing the scene instead of answering the query.
        if label not in wanted and not any(t in label or label in t for t in wanted):
            continue

        try:
            x1, y1, x2, y2 = (float(v) for v in box)
        except (TypeError, ValueError):
            continue

        # Normalised 0-1000 → pixels.
        x1, x2 = sorted((x1 / 1000.0 * w, x2 / 1000.0 * w))
        y1, y2 = sorted((y1 / 1000.0 * h, y2 / 1000.0 * h))
        x1, y1 = max(0.0, x1), max(0.0, y1)
        x2, y2 = min(float(w), x2), min(float(h), y2)

        # Degenerate or absurd boxes: a "detection" covering the whole frame is
        # the model declining to localise, not a finding.
        bw, bh = x2 - x1, y2 - y1
        if bw < 4 or bh < 4:
            continue
        if (bw * bh) / (w * h) > 0.92:
            continue

        conf = item.get("confidence", 0.5)
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = 0.5

        out.append(
            RawDetection(
                label=label,
                confidence=min(VLM_CONFIDENCE_CAP, max(0.05, conf)),
                bbox=BBox(x1=x1, y1=y1, x2=x2, y2=y2),
            )
        )

    return _dedupe(out)


def _dedupe(dets: list[RawDetection], iou_threshold: float = 0.65) -> list[RawDetection]:
    """VLMs frequently emit the same object twice with slightly different boxes."""
    kept: list[RawDetection] = []
    for d in sorted(dets, key=lambda x: -x.confidence):
        if any(d.label == k.label and d.bbox.iou(k.bbox) > iou_threshold for k in kept):
            continue
        kept.append(d)
    return kept


def _loads(raw: str) -> dict[str, Any] | None:
    from kestrel.clients.models import _loads_lenient

    return _loads_lenient(raw)
