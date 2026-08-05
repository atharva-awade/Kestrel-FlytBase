"""KESTREL — autonomous drone security analyst agent.

The package is organised along the runtime pipeline, so the module layout mirrors
the system architecture:

    ingest      → frame + telemetry sources (video, scripted text, cassette)
    gate        → tier-0 cost gate: does this frame deserve to be looked at?
    perception  → detector → tracker → CLIP → VLM scene graph → escalation
    memory      → entity re-identification, temporal memory pyramid, baselines
    retrieval   → hybrid structured ⊕ vector index and its query planner
    rules       → temporal rule DSL, compiler, NL→rule, triage
    actions     → mission recommendation, feasibility, simulated executor
    fleet       → multi-site model and cross-site correlation
    agent       → Ask KESTREL: tool registry and the LangGraph control plane
    obs         → metering, tracing, hash-chained audit ledger
    evals       → golden sets, judges, chaos, benchmarks
    api         → FastAPI surface (REST + SSE + WebSocket)
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
