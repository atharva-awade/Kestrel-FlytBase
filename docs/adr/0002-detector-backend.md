# ADR 0002 — Detector backend selection, and working around a filtered Hub

**Status:** accepted
**Reproduce:** `uv run python scripts/diagnose_hf.py`, `uv run kestrel doctor`

## Context

No object detector is reachable on the hosted inference API at all (ADR 0001), so
detection must run on-device. That turned out to be the better architecture
regardless — detection is per-frame work, it belongs at the edge, it costs no API
budget, it is not rate limited, and it keeps working when the site link drops,
which is how a drone-in-a-box actually operates.

That left a second problem: the preferred model is distributed through the
HuggingFace Hub, and the Hub was unreachable on the development network.

## Diagnosing the block properly

Guessing here wastes hours, so the mechanism was measured. `scripts/diagnose_hf.py`
separates the three possibilities, because each needs a different remedy:

| Observation | Result |
|---|---|
| DNS for `huggingface.co` | ✅ resolves (13.225.5.30) — via system, Cloudflare and Google |
| TCP connect to port 443 | ✅ connects in 23 ms |
| **TLS handshake with SNI** | ❌ **`ConnectionResetError` — reset on ClientHello** |
| Control: `github.com` | ✅ full TLS handshake |
| `hf-mirror.com` | ✅ TLS handshake succeeds |

**Verdict: SNI-based DPI filtering.** The connection is established and then reset
the instant TLS reveals which host is being requested. Changing DNS cannot help,
because the name already resolves correctly — a fact worth establishing before
anyone spends an afternoon editing resolver settings.

## Decision

### 1. Fetch through a mirror, with stubborn retries

`hf-mirror.com` is not filtered. It is a single server and connections fail
intermittently — measured at roughly one success in three attempts — but the
failures are transient rather than a block, so retrying gets through.
`scripts/fetch_hf_models.py` retries with backoff and serial (not parallel)
downloads, because parallel connections to a flaky single host multiply the chance
that one fails and aborts the whole transfer.

### 2. Prefer the cache over reachability

This was a genuine bug. The original logic asked *"is the Hub reachable?"* before
attempting a Hub-backed model — which disabled open-vocabulary detection on a
machine that had the weights **already sitting on disk**.

The correct question is *"can this model be loaded?"*, and a cached model needs no
network at all:

```python
def hf_usable(model_id):
    if hf_model_cached(model_id):        # weights on disk — network irrelevant
        return True, "cached locally"
    if hf_reachable():                   # only consulted on a cache miss
        return True, "hub reachable, will download"
    return False, "not cached and the hub is unreachable"
```

Loading additionally passes `local_files_only=True` when cached-but-unreachable,
because `from_pretrained` otherwise makes an etag request to check for updates —
and on a filtered network that request fails and takes the whole load with it, even
though every byte needed is already local.

### 3. Four backends, selected by capability

Reported honestly in `/api/health` so the interface can state which one produced a
given box:

| Backend | Source | Open-vocab | Notes |
|---|---|---|---|
| **Grounding DINO** | HF Hub | ✅ | Preferred. Detects from text phrases. |
| **YOLO11** | GitHub releases | ❌ | Works when the Hub is filtered. ~12 ms on CUDA. |
| RT-DETR | HF Hub | ❌ | Apache-2.0 alternative. |
| Motion heuristics | none | ❌ | No torch at all. Degraded, and says so. |

## Measured behaviour of open-vocabulary grounding

Worth recording, because it shaped the threshold and it constrains how promptable
rules should be trusted.

| Prompt | Result on real footage |
|---|---|
| `person` | ✅ detected, **0.87** confidence |
| `a person wearing a high-visibility vest` | ✅ detected |
| `a traffic cone` (absent from the scene) | ❌ **grounded a white pillar at 0.48** |
| `a person on a ladder` (no ladder present) | ❌ **returns the person** |

Two real limitations:

1. **It will ground a phrase for an absent object.** Spurious groundings clustered
   in the 0.35–0.50 band against 0.87 for the genuine detection, so the threshold
   is set to **0.55** — which eliminated the traffic-cone false positive while
   keeping the true one comfortably.
2. **It grounds the head noun and largely ignores qualifiers.** "A person on a
   ladder" matches any person.

**This is precisely why a promptable rule is backtested before it can fire.** The
operator is shown what the phrase actually grounds on across indexed history,
rather than being asked to trust that the model understood the sentence.

## Consequences

- Open-vocabulary detection now runs **locally on the GPU** rather than routing
  through the VLM: ~385 ms per prompted query instead of a cloud round-trip, with
  no API budget and no rate limit.
- The system works identically on a filtered network, an unfiltered one, and one
  with no internet at all — the only difference is which backend loads.
- The cache is portable: fetching the model once on any network and copying
  `~/.cache/huggingface` is a permanent fix.

## For anyone reproducing this

```bash
uv run python scripts/diagnose_hf.py        # what kind of block is it?
uv run python scripts/fetch_hf_models.py    # fetch through the mirror
uv run kestrel doctor                       # expect: grounding-dino (cuda), open-vocabulary
```

If the mirror also fails, any of these is a complete fix: a mobile hotspot (a
different ISP is usually unfiltered), any VPN (it encrypts the SNI the filter keys
on), or running the fetch on another machine and copying the cache across.

None of it is required. KESTREL runs fully without the Hub — detection falls back
to YOLO11, and open-vocabulary queries route through the VLM.
