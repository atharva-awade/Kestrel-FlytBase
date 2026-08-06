"""What KESTREL knows about itself.

This exists so a reviewer can interrogate the system instead of reading for it.
Ask *"explain your architecture"* or *"why did you escalate on frame 412?"* and get
a grounded answer with the reasoning behind the design — including the parts that
did not go to plan.

These are written as prose rather than pulled from docstrings deliberately: the
answer to "how does the gate work" is not the gate's implementation, it is the
argument for why a gate exists at all.
"""

from __future__ import annotations

ARCHITECTURE: dict[str, str] = {
    "overview": """
KESTREL is an autonomous security analyst for a docked patrol drone. It runs a
five-tier perception cascade over real video, remembers what it sees across days,
evaluates declarative temporal rules, and, when something matters, proposes a
concrete drone response with navigable coordinates for a human to approve.

The pipeline, in order:

  1. INGEST      real video, scripted text, or recorded cassettes, one interface
  2. GATE        tier 0, CPU only: is this frame worth spending anything on?
  3. DETECT      YOLO11 on the local GPU: boxes and classes
  4. TRACK       ByteTrack: identity that persists across frames
  5. PROJECT     pixel → world coordinates → named zone, using telemetry
  6. EMBED       joint image/text vectors for re-identification and search
  7. PERCEIVE    a vision-language model produces a structured scene graph
  8. REMEMBER    entity resolution, a temporal memory pyramid, a normalcy baseline
  9. DECIDE      temporal rules → triage → threat narrative
 10. ACT         mission proposal with feasibility, gated behind human approval

The organising constraint is cost. NVIDIA's free tier is rate-limited near 40
requests per minute, and an eight-hour patrol at 2 fps is about 57,600 frames.
Captioning all of them is impossible and pointless; almost all of them show the
same empty yard. Every architectural decision follows from that.
""",

    "gate": """
The tier-0 gate decides whether a frame deserves to reach a model, using only CPU
arithmetic. It is the smallest component in the system and the one that determines
whether it is deployable at all.

Three signals, cheapest first, short-circuiting as soon as one fires:

  structural   perceptual-hash distance, catches layout change
  photometric  mean absolute pixel delta, catches motion the hash smooths over
  semantic     embedding cosine distance, catches "same pixels, new meaning"

The semantic check is optional and only runs when the cheap signals are
ambiguous, because it costs an embedding call.

Context adjusts sensitivity: a high-priority zone, night, off-hours, or a
stationary drone all lower the bar for spending a call. That multiplier is capped,
which was a real bug: uncapped, the priors compounded to about 6× over a
restricted zone at night, making the gate so twitchy that sensor noise tripped it
and nothing was gated at all.

A heartbeat guarantees analysis at least every N frames or seconds, because
"nothing has changed for twenty minutes" is a claim worth re-verifying rather than
assuming.

Measured skip rate is 38.9% on the available footage and 96.7% on an idle patrol.
Both figures are reported, because the clips are computer-vision demo reels
authored for continuous motion, close to worst case for a gate, so the first
number is a floor, not a typical value.
""",

    "cascade": """
THE CONSTRAINT THIS DESIGN EXISTS TO SOLVE. One drone flying an eight-hour shift
at 2 fps produces about 57,600 frames. The free model tier allows roughly 40
requests per minute, or about 19,200 over the same shift. Sending every frame to
a vision model is therefore not slightly too expensive, it is arithmetically
impossible, and it gets worse linearly with every drone and every site added.

So the question is never "how fast is the model", it is "how few frames must
reach it". That is the scaling limit and the whole shape of the answer:

  what scales freely      gate and YOLO11 run locally, on device, per frame
  what must be rationed   every hosted model call, tiers 3 and 4
  measured result         38.9% of frames never reach a VLM on real footage,
                          and 96.7% on idle footage, so the ceiling moves with
                          how much is actually happening rather than with time
  the honest limit        a busier site spends more, and a site busy enough that
                          most frames are novel would exhaust the budget; the
                          gate lowers the cost of quiet, it does not remove it

Five tiers, each more expensive than the last, each run only when the one before
it justifies the spend:

  tier 0    gate            free      should we look at all?
  tier 1    YOLO11 (CUDA)   ~12 ms    boxes and classes, on-device
  tier 1.5  ByteTrack       ~free     persistent identity across frames
  tier 2    embeddings      cheap     vectors for re-ID and semantic search
  tier 3    11B vision VLM  ~1.3 s    structured scene graph
  tier 4    90B vision VLM  57-84 s   deep re-look, asynchronous only

Tier 1.5 matters more than it looks. Without a tracker, "a person" in frame 41 and
"a person" in frame 42 are unrelated observations, and every claim about duration
is a guess. Dwell, loitering, "the same vehicle returned": the entire temporal
rule vocabulary needs identity that persists.

Tier 4 is asynchronous because it was measured at 57-84 seconds. Awaiting that
inside frame processing would stall the pipeline for a minute per escalation, so
the frame is written at tier-3 confidence immediately and upgraded when the deeper
answer lands.

Escalation splits by the kind of doubt that triggered it. Needing to *see* better
routes to the big VLM asynchronously; needing to *think* better routes to the
reasoning model over the structured scene graph in about 230 ms. Most escalations
are cognitive, so the common case stays fast.
""",

    "memory": """
Three stores, because "remember" means three different things:

ENTITIES: identity that survives across frames, sessions and days. Detections are
matched to persistent entities using appearance embeddings, attribute agreement
and spatio-temporal plausibility. This is what turns "a person was detected" into
"the same vehicle, seventh visit, first time ever after midnight". Thresholds are
deliberately conservative: a wrongly merged entity produces a confidently false
history, which is worse than a fragmented one.

THE TEMPORAL PYRAMID: hierarchical compression, because an eight-hour shift is
about 1.7M tokens of raw observation and an operator asking "what happened last
night" expects an answer grounded in all of it.

  L0 frame → L1 clip → L2 event → L3 shift → L4 day

Compression is salience-weighted, not uniform: an uneventful shift collapses to a
sentence while the four minutes around an intrusion keep frame-level detail. Every
node keeps its children, so any summary can be expanded back to the frames that
support it; a summary you cannot audit is not evidence.

THE BASELINE: counts per (zone, hour, class), scored by departure from history.
"A vehicle at the loading dock" is unremarkable; "a vehicle at the loading dock at
03:00, never seen in fourteen days" is a finding, and no static rule produces the
second sentence. The model abstains below three days of history rather than
flooding an operator with false novelty on day one.
""",

    "rules": """
Rules are declarative data, not code, and temporal rather than per-frame.
"Loitering" is dwell over time; "tailgating" is one event following another within
a window; "unattended object" is a thing persisting after its owner leaves. No
per-frame predicate expresses any of them.

Because rules are data, two things follow:

They can be authored by a language model. An operator types "alert me if a truck
parks at the loading dock for over ten minutes after 9pm" and gets a validated
rule, checked against the real zone list, because a rule naming a zone that does
not exist validates and then silently never fires, which is the worst failure mode
since it looks like it is working.

They can be backtested. Evaluation is a pure function of indexed history, so a
proposed rule is replayed over past days *before* it is enabled and the operator
sees exactly what it would have done.

A rule may also carry a `visual_predicate`, its own detector prompt in plain
English, so it can detect things no fixed class list covers.

Triage sits after the engine. Suppression is a safety feature: an operator who
gets forty alerts a night learns to dismiss them all, and the system protects
nothing. Counterfactuals split into contradicting evidence (a dog is not a person,
so suppress) and mitigating evidence (rain, PPE, so reduce confidence). Getting that
distinction wrong was a real bug: scaling a wildlife alert by 0.35 left it just
above threshold and the stray-dog scenario still alerted.
""",

    "retrieval": """
Two indexes over one SQLite file, fused by rank.

Structured SQL answers what is genuinely a filter: "all truck events at the dock
between 22:00 and 04:00". Answering that with cosine similarity would be worse in
every respect.

Vector search answers what a filter cannot: "anything unusual near the dock", or
"a white pickup by the fence" where the caption never used those words. Frames are
embedded into a joint image/text space, so a text query retrieves on appearance.

Fusion is Reciprocal Rank Fusion, which combines rankings rather than scores, so
two retrievers with incomparable similarity scales can be merged without inventing
an arbitrary normalisation. No hosted cross-encoder was reachable, so an optional
LLM rerank refines the top-k when precision matters more than latency.

Every query returns its plan. A retrieval system that cannot explain why a result
surfaced is one an operator cannot audit.

SQLite was chosen over a vector database deliberately: a reviewer must be able to
clone the repository and run it, and a single file with no server is worth more
here than an index we would never load enough data to justify. If the sqlite-vec
extension will not load, a numpy brute-force scan takes over; correctness of
setup beats asymptotics at this scale.
""",

    "actions": """
An alert tells an operator something happened. It does not help them decide what
to do, and at 02:00 with one guard on shift that decision is the whole job.

So every alert carries a dispatch position: geo-projected coordinates with an
accuracy radius, the zone, distance and bearing from the dock, an ETA, a
recommended altitude and a geofence verdict. The source of the position travels
with it: a metre-accurate projection and a zone-centroid fallback are both
useful, but confusing them puts a responder at the wrong end of a yard.

From that, a mission is planned: LAUNCH → GOTO → ORBIT → TRACK → RETURN, with real
waypoint coordinates. It is then checked against battery (including reserve),
geofence, wind, altitude ceiling and daylight. Blockers make a mission
un-approvable; warnings let it through with the risk stated. A recommendation that
ignores whether the aircraft can actually fly is worse than none; it teaches the
operator that the system's suggestions cannot be trusted.

Nothing executes on its own. Proposal and approval are separate code paths, the
gate is enforced in the tool registry rather than in prompt wording, and every
decision is written to the audit ledger.

The loop closes because the new vantage point feeds back: a closer, better-lit
frame of the same subject re-enters perception, and the resulting confidence delta
is recorded against the mission.
""",

    "fleet": """
The assignment describes one drone on one property. The portfolio view exists
because a fleet produces a class of finding a single site cannot:

  "This vehicle has now been seen at three of your sites in five days."

That is a reconnaissance pattern, and no amount of analysis at one site reveals
it, because the evidence is distributed. Since entity embeddings already exist for
local re-identification, comparing them across sites costs almost nothing.

The cross-site threshold is higher than the within-site one: a false positive here
produces a dramatic and wrong claim about coordinated activity. Matches closer
together in time than plausible travel are rejected as matching failures rather
than treated as teleportation.

Only one site carries a live feed. Every other site is driven by a seeded
generator and is flagged simulated in every payload, rendered as a chip in the UI,
and stated in the report. A portfolio view implying forty live aircraft would be
the one thing capable of discrediting everything else here.
""",

    "models": """
Every model was chosen by probing the live API, not by reading documentation,
and four of five original choices turned out to be wrong. A model appearing in a
provider's catalogue does not mean you can call it: several return
"Function not found for account".

  perception (fast)   meta/llama-3.2-11b-vision-instruct    1.28 s
  perception (deep)   meta/llama-3.2-90b-vision-instruct    57-84 s, async only
  embeddings          nvidia/llama-nemotron-embed-vl-1b-v2  2048-d, joint space
  text embeddings     nvidia/nv-embedqa-e5-v5               1024-d
  reasoning           llama-3.3-70b-versatile (Groq)        227 ms

The 11B was chosen over a faster 8B model that missed a person in the probe image.
For a security system a false negative on a human is the worst available failure,
and 1.28 s is well inside budget for a gated pipeline.

Groq is primary for text at 227 ms against NIM's 83 s cold start for the same
model class, with NIM as failover.

Detection runs locally because no hosted detector is reachable at all. That turned
out better: detection is per-frame work and belongs at the edge, it costs no API
budget, and it keeps working with the network unplugged, which is how a
drone-in-a-box actually operates when the site link drops.
""",

    "security": """
Two boundaries are enforced structurally rather than by convention.

PERMISSIONS. Every tool carries a class. Read tools run freely. Tools that change
state or move an aircraft cannot be executed by the agent at all; it may only
propose them, and execution requires a separate call carrying an explicit human
decision. This is enforced in the tool registry, because prompt wording is not a
security control. There is a test asserting that no gated tool can execute without
approval.

THE AUDIT LEDGER. Every consequential event (an alert raised, a rule enabled, a
mission approved, an operator override) is appended to a hash chain, where each
entry commits to its predecessor. Altering any historical row invalidates every
hash after it. That does not make the log immutable, but it makes silent
modification detectable, which is the property chain of custody actually needs.
Verification is exposed as a tool and asserted in tests, because a tamper-evident
log nobody verifies is just a log.

Credentials are scoped: model provider keys are server-side only and never reach
the browser. The map tile key is necessarily public and is named with a prefix
that says so.
""",
}

LIMITATIONS = """
Stated plainly, because a system that overstates itself is harder to trust:

- The telemetry is simulated. There is no aircraft. Altitude, battery, wind and
  GPS quality are modelled, and every downstream quantity derived from them
  inherits that.
- Geo-projection assumes flat ground and uncalibrated camera intrinsics. On a site
  with a raised dock or a slope, elevated objects project long.
- Entity re-identification uses general-purpose embeddings, not a model trained
  with a re-identification objective. It confuses two similar white vans more
  readily than a dedicated model would.
- Open-vocabulary detection grounds the head noun and largely ignores qualifiers.
  Asked for "a person on a ladder" in a scene with a person and no ladder, it
  returns the person. It will also ground a phrase for an object that is absent
  entirely: "a traffic cone" scored 0.48 on a white pillar, which is why the
  confidence threshold sits at 0.55 and why every promptable rule is backtested
  before it is allowed to fire. Where the model cannot be loaded at all, this
  capability falls back to the VLM, whose boxes are coarser still and marked so.
- Only one site has real footage. The rest of the portfolio is seeded simulation,
  flagged everywhere it appears.
- The baseline needs three days of history before it will judge anything.
"""
