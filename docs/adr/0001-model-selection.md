# ADR 0001 — Model selection, decided by measurement

**Status:** accepted
**Date:** 2026-08-06
**Reproduce:** `uv run python scripts/probe_models.py`, `scripts/catalogue.py`, `scripts/probe_vlms.py`, `scripts/probe_embeddings.py`
**Raw data:** `data/probe_results.json`, `data/probe_vlms.json`, `data/probe_embeddings.json`

## Context

KESTREL needs five model capabilities: semantic captioning of frames, open-vocabulary
object detection, joint image/text embeddings, text embeddings, and a reasoning LLM.

The initial plan assigned each of these to a specific NVIDIA NIM model chosen from public
documentation and model cards. **Four of those five choices turned out to be wrong.** NVIDIA's
published model cards describe models that exist but do not necessarily expose a hosted
endpoint to a developer key, and the documentation does not state which is which.

Rather than discover this during integration, every endpoint was probed directly before any
dependent code was written. The probe scripts are committed so the finding is reproducible
and so a reviewer can re-verify it independently.

## What the catalogue actually offers

`GET /v1/models` returns **102 models** for a free developer key. Presence in that list does
*not* imply reachability: several models return
`404 {"detail": "Function '<uuid>': Not found for account"}`, meaning the NVCF function behind
them is not provisioned for developer keys.

### Vision-language models

| Model | Reachable | Best latency | Saw colour | Saw vehicle | Saw person |
|---|---|---|---|---|---|
| `meta/llama-3.2-11b-vision-instruct` | ✅ | **1.28 s** | ✅ | ✅ | ✅ |
| `nvidia/llama-3.1-nemotron-nano-vl-8b-v1` | ✅ | **0.86 s** | ✅ | ✅ | ❌ |
| `nvidia/nemotron-nano-12b-v2-vl` | ✅ | 29.2 s | ✅ | ✅ | ✅ |
| `meta/llama-3.2-90b-vision-instruct` | ✅ | 57.6 s | ✅ | ✅ | ✅ |
| `nvidia/vila` | ❌ 404 | — | — | — | — |
| `microsoft/phi-3-vision-128k-instruct` | ❌ 404 | — | — | — | — |
| `microsoft/kosmos-2` | ❌ 404 | — | — | — | — |
| `nvidia/neva-22b` | ❌ 404 | — | — | — | — |
| `adept/fuyu-8b` | ❌ 404 | — | — | — | — |

4 of 9 reachable. The three-signal check (colour / vehicle / person) used a synthetic probe
image; caption *quality* is measured separately against real footage in the eval harness.

### Everything else

| Capability | Planned | Result |
|---|---|---|
| Open-vocab detection | `nvidia/nv-grounding-dino` | ❌ **No detection model is hosted at all.** Not on `integrate.api.nvidia.com`, and `ai.api.nvidia.com/v1/cv/...` 404s |
| Joint image+text embed | `nvidia/nvclip` | ❌ 404 — in the catalogue, function not provisioned |
| Joint image+text embed | `nvidia/llama-nemotron-embed-vl-1b-v2` | ✅ **2048-d, image and text confirmed in the same space** — probed as three checks (text 478 ms, image 667 ms, and a cross-modal cosine of 0.2575 proving the two land in one comparable space rather than merely sharing a dimension count) |
| Text embed | `nvidia/nv-embedqa-e5-v5` | ✅ 1024-d, 353 ms |
| Reranker | `nvidia/llama-3.2-nv-rerankqa-1b-v2` | ❌ no `/v1/ranking` endpoint, model absent |
| Reasoning LLM | `meta/llama-3.3-70b-instruct` (NIM) | ❌ was **83 s** cold start; on re-probe it now exceeds a 90 s read timeout entirely |
| Reasoning LLM | `meta/llama-3.1-70b-instruct` (NIM) | ✅ **1.5 s** — the failover actually relied on |
| Reasoning LLM | `llama-3.3-70b-versatile` (Groq) | ✅ **227 ms** |

## Decisions

### 1. Fast perception tier → `meta/llama-3.2-11b-vision-instruct`

The 8B nemotron is 0.42 s faster but **missed the person** in the probe image. For a security
system, a false negative on a human is the worst available failure mode, and 1.28 s is well
inside budget for a gated pipeline. Accuracy wins over a sub-second latency edge.

### 2. Deep escalation splits in two, because 57 s cannot sit on the critical path

This is the most consequential finding. The plan assumed escalation meant "call a bigger
VLM," but a 57–84 s round trip makes that unusable interactively. Escalation is therefore
split by *what kind of doubt* triggered it:

- **Semantic escalation** — we need to *see* better (occlusion, poor light, ambiguous object).
  Routes to `llama-3.2-90b-vision-instruct` **asynchronously, off the critical path**. The
  record is written immediately at tier-3 confidence and *upgraded* when the deep result
  lands. The UI shows this happening.
- **Cognitive escalation** — we need to *think* better (is this pattern a threat given the
  last four days?). Routes to the reasoning LLM over the structured scene graph plus memory
  context — **227 ms, on the critical path**.

Most escalations are cognitive, not semantic, so the common case stays fast. This is a better
design than the original plan and it exists only because the latency was measured.

### 3. Reasoning LLM → Groq primary, NIM fallback

227 ms versus 83 s for the same model class, and NIM's free tier is additionally capped near
40 RPM. Groq becomes primary; NIM is the failover. The provider abstraction races or falls
back automatically, so neither is load-bearing alone.

### 4. Open-vocabulary detection moves to the local GPU — and this is an upgrade

No hosted detector exists, so promptable detection runs locally via
`IDEA-Research/grounding-dino-tiny` (Apache-2.0) on the available RTX 4050, falling back to
RT-DETR for closed-set detection and then to gate-only heuristics on a CPU-only machine.

The promptable-rules feature is fully preserved. Three things actually improve:

- no per-frame API cost and no rate limit on detection,
- the edge/cloud split becomes **real** — detection and tracking genuinely execute on-device
  while only semantics go to the cloud, which is how FlytBase's Edge Kit is deployed,
- detection keeps working with no network at all.

### 5. Retrieval reranking → Reciprocal Rank Fusion, optional LLM rerank

No hosted cross-encoder. RRF needs no model, is deterministic, and is cheap; an optional LLM
rerank pass handles the top-k when precision matters. Hybrid retrieval quality is reported as
Recall@k / MRR from the eval harness either way.

## Consequences

- The five-tier cascade is unchanged in shape, but tier 1 is local and tier 4 is asynchronous.
- Two provider integrations (NIM + Groq) are required rather than one; this was going to be
  needed for the failover story regardless.
- `scripts/probe_*.py` are permanent: they are the regression test for the assumption that
  these endpoints exist, and they re-run in CI-optional mode.

## Note for the report

The general lesson is worth stating plainly: **a model appearing in a provider's catalogue is
not evidence that you can call it.** Probing first cost roughly twenty minutes and changed
four of five model choices plus one architectural decision. Had integration proceeded from the
documentation, the same discoveries would have arrived tangled up in debugging half-built
components.

## Amendment, 2026-08-06: the fallback stopped answering

Re-probing during a site-wide inspection found `meta/llama-3.3-70b-instruct` on NIM no
longer returning at all. Not the 83 s cold start recorded above: three consecutive
attempts hit a 45 s read timeout with no response.

That matters more than it first appears, because this model was the *failover* target.
The free Groq tier allows roughly 30 requests a minute, and the moment that limit is
reached every request routes to the fallback and burns its entire timeout before
degrading. To an operator the assistant simply stops responding, which is precisely
what a failover path exists to prevent. A fallback that never returns is worse than no
fallback at all: it converts a fast, honest failure into an indefinite hang.

Measured alternatives on the same account, same minute:

| Model | Latency | Verdict |
|---|---|---|
| `meta/llama-3.3-70b-instruct` | 45 s timeout | dead, was the fallback |
| `meta/llama-3.1-70b-instruct` | **554 ms** | same class, now the fallback |
| `meta/llama-3.1-8b-instruct` | 213 ms | fast, smaller |
| `nvidia/llama-3.1-nemotron-nano-8b-v1` | 45 s timeout | dead |
| `microsoft/phi-3-mini-4k-instruct` | HTTP 404 | not provisioned |
| `mistralai/mistral-7b-instruct-v0.3` | HTTP 404 | not provisioned |

`KESTREL_LLM_FALLBACK` is now `meta/llama-3.1-70b-instruct`.

This is the same lesson as the original decision, one level deeper: a model that worked
when you probed it is not evidence that it works now. Hosted endpoints are deprecated and
de-provisioned without notice, which is an argument for keeping `scripts/probe_models.py`
runnable and for the circuit breaker that contained this failure rather than propagating it.
