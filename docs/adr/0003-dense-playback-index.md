# ADR 0003 — A dense playback index, and why it does not contradict the cost cascade

**Status:** accepted
**Date:** 2026-08-07

## Context

The console showed still JPEGs sampled at 2 fps. That is an accurate picture of
what the *gate* does and a poor picture of what the *system* does, and the
assignment is judged substantially on a demo video:

> "Submit one or more videos showing **video processing**, context summaries,
> agent recommendations, scalability test, and innovative features."

An operator watches video. So does an interviewer. Two 0.5-second-apart
screenshots do not demonstrate tracking, dwell, or a rule firing on a timeline.

Worse, an audit found the live overlay had **never drawn a box**: the SSE payload
emits `bbox: [x1,y1,x2,y2]` while the overlay read `d.x1/d.y1/d.x2/d.y2`.

## The tension

The architecture's central claim is a cost cascade: ~57,600 frames per shift
against a free tier of ~40 model requests per minute, so a tier-0 gate decides
what is worth spending on. Running detection on *every* frame looks, at first
glance, like abandoning that claim.

## Decision

Build a dense per-clip index ahead of time, and **let the gate govern the hosted
tiers only**.

The resolution is that the gate was never protecting GPU time. It protects
**model spend** — hosted VLM calls are rate-limited and billed. Local detection is
neither. Measured on this machine:

| tier | cost | measured |
|---|---|---|
| YOLO11s detect | no API budget, local GPU | **12–15 ms/frame** |
| VLM caption | rate-limited, metered | 1.28 s |
| deep re-look | rate-limited, metered | 57–84 s |

So detection runs on every sampled frame, the gate's verdict is **recorded and
displayed**, and the hosted tiers stay gated. The console renders that split as a
ribbon under the video: you watch the gate skip frames while the boxes keep
tracking. The distinction becomes visible rather than merely asserted.

## Consequences

**Sampling.** Native frame rate capped at 15 fps. Four of the six clips are
natively 10–12.5 fps, so they are indexed frame-for-frame and nothing is
interpolated. The two 59.94 fps clips are sampled at 15, and between samples the
overlay follows `track_id` — which is what a track id is for. The UI labels those
frames `interp` rather than implying a detection ran on each one.

**Detector choice.** YOLO11s is pinned for this pass. Grounding DINO is the better
detector for open-vocabulary rules and the wrong one here on both counts: ~540
ms/frame, and a box threshold of 0.55 that exists because it grounds spurious
phrases in the 0.35–0.50 band. That threshold protects precision on a promptable
query and destroys recall on a dense sweep — on one 377-frame clip it found three
cars. YOLO11 is closed-set COCO, which is the vocabulary a security console needs.

**Tracker rate.** `PerceptionPipeline` hardcoded `analysis_fps = 2`. ByteTrack's
lost-track buffer is counted in frames, so at 12 fps a buffer sized for 2 fps
silently shrinks from 30 seconds of tolerated occlusion to five. The indexer
passes the real rate.

**Normalised boxes.** Stored 0–1 of frame size. Detections were previously written
in 960-wide space while worker-zone plays at 1920 — a latent 2× error waiting for
whoever drew them.

**Cost.** ~3 minutes of GPU time for all six clips, ~1.1 MB of JSON, committed. A
missing index rebuilds on demand.

**Uploads.** The same code path serves operator-supplied footage
(`kestrel.playback.build_upload_index`), anchored to an ad-hoc site at the
coordinates they give. Nothing about the bundled clips is special-cased.

## Alternatives rejected

- **Live inference during playback.** Caps at ~19 fps at 1080p, cannot seek, and
  pins the GPU for the whole demo.
- **Keeping the gate in front of detection.** Produces ~1.2 boxes per second of
  video against 60 rendered frames: boxes jump ~0.8 s apart and vanish through
  gated stretches. It measures the gate honestly and shows the system badly.
- **Interpolating everything to look smoother.** Rejected where it would misstate
  what ran. Interpolated frames are labelled.

## Honesty notes

Telemetry remains simulated; there is no aircraft. Uploaded clips widen the
accuracy radius because the projection assumes flat ground and a nominal
altitude, and they are labelled so they cannot be mistaken for the live site.
