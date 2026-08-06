"""Ask KESTREL — the control plane, tested like code rather than trusted like prose.

The two properties that matter most are asserted here, because both are the kind
of thing that silently stops being true:

*   **No gated tool can execute without an explicit human decision.** An agent that
    can launch an aircraft needs that boundary enforced by the code path, not by
    prompt wording, and prompt wording is what usually enforces it.
*   **Citations must resolve to real evidence.** A confident answer citing a frame
    that does not exist would discredit everything else in the system.

These run offline against a stub client — no network, no key, deterministic.
"""

from __future__ import annotations

import json

import pytest

from kestrel.agent.agent import AgentContext, AskKestrel
from kestrel.agent.registry import (
    Permission,
    ToolClass,
)
from kestrel.agent.tools import build_registry
from kestrel.memory.baseline import BaselineModel
from kestrel.rules.engine import RuleEngine
from kestrel.rules.pack import default_rules
from kestrel.sim.sites import build_fleet, build_plant_01
from kestrel.storage.db import Database
from kestrel.storage.ledger import Ledger


class StubClient:
    """A scripted model. Returns queued replies in order, then a default."""

    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies or [])
        self.calls: list[list[dict]] = []

    async def chat(self, messages, **kw):
        self.calls.append(messages)
        return self.replies.pop(0) if self.replies else '{"calls":[]}'

    async def embed_text(self, *a, **kw):
        return [0.0] * 8

    async def embed_image(self, *a, **kw):
        return [0.0] * 8


@pytest.fixture
def ctx(tmp_path):
    from kestrel.fleet.fleet import FleetManager
    from kestrel.retrieval.search import HybridSearch
    from kestrel.rules.compiler import RuleCompiler

    site = build_plant_01()
    db = Database(tmp_path / "agent.db")
    client = StubClient()
    baseline = BaselineModel(site.id, db=db)
    context = AgentContext(
        site=site, db=db, ledger=Ledger(db), client=client,
        engine=RuleEngine(site, default_rules(), baseline=baseline),
        baseline=baseline,
        search=HybridSearch(db, site, client),
        compiler=RuleCompiler(site, client),
        fleet=FleetManager(build_fleet(), db),
    )
    try:
        yield context
    finally:
        # Leaked SQLite handles surface as an intermittent OperationalError during
        # setup of a later, unrelated test — see the note in test_storage_memory.
        db.close()


# ═══════════════════════════════════════════════════════════════════════════════
# The permission boundary
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.agent
async def test_no_gated_tool_executes_without_approval(ctx):
    """The single most important assertion in the suite.

    Every CONFIRM tool must refuse to act when invoked the way the agent invokes
    tools. If this ever passes silently, an agent could launch a drone on its own.
    """
    reg = build_registry(ctx)
    gated = [t for t in reg.tools if t.permission is Permission.CONFIRM]
    assert gated, "no gated tools registered — the permission model is not wired up"

    for tool in gated:
        result = await reg.invoke(tool.name, {}, approved=False)
        assert result.get("requires_confirmation") is True, (
            f"{tool.name} did not demand confirmation"
        )
        assert result.get("renders_as") == "confirmation_card"
        assert tool.consequence, f"{tool.name} has no stated consequence"


@pytest.mark.agent
async def test_agent_loop_never_passes_approved(ctx):
    """`approved=True` must be reachable only through the human-decision path."""
    import inspect

    from kestrel.agent import agent as agent_module

    source = inspect.getsource(agent_module.AskKestrel.ask)
    assert "approved=True" not in source, (
        "the agent's own loop can set approved=True — the gate is bypassable"
    )
    confirm_src = inspect.getsource(agent_module.AskKestrel.confirm)
    assert "approved=True" in confirm_src


@pytest.mark.agent
async def test_declining_an_action_does_not_execute_it(ctx):
    agent = AskKestrel(ctx)
    out = await agent.confirm("enable_rule", {"rule_id": "loitering"}, approve=False)
    assert out["executed"] is False
    entries = ctx.ledger.entries(limit=5)
    assert any(e["payload"].get("decision") == "declined" for e in entries)


@pytest.mark.agent
async def test_approval_is_written_to_the_audit_ledger(ctx):
    agent = AskKestrel(ctx)
    await agent.confirm("enable_rule", {"rule_id": "loitering"}, approve=True)
    entries = ctx.ledger.entries(limit=10)
    assert any(e["payload"].get("decision") == "approved" for e in entries)
    assert ctx.ledger.verify()["valid"] is True


@pytest.mark.agent
async def test_read_tools_run_freely(ctx):
    reg = build_registry(ctx)
    result = await reg.invoke("list_rules", {})
    assert result["ok"] is True
    assert result["count"] > 0
    assert "requires_confirmation" not in result


# ═══════════════════════════════════════════════════════════════════════════════
# Registry integrity
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.agent
def test_registry_covers_every_tool_class(ctx):
    reg = build_registry(ctx)
    present = {t.tool_class for t in reg.tools}
    for required in (ToolClass.RETRIEVE, ToolClass.ANALYSE, ToolClass.AUTHOR,
                     ToolClass.ACT, ToolClass.OPERATE, ToolClass.NAVIGATE,
                     ToolClass.FLEET, ToolClass.EXPLAIN):
        assert required in present, f"no tools in class {required.value}"


@pytest.mark.agent
def test_exported_schema_is_json_serialisable_and_complete(ctx):
    """The frontend consumes this. If it drifts, the UI renders the wrong thing."""
    reg = build_registry(ctx)
    schema = reg.export_schema()
    json.dumps(schema)   # must not raise
    assert schema["tools"]
    names = {t["name"] for t in schema["tools"]}
    assert set(schema["permissions"]["auto"]) | set(schema["permissions"]["confirm"]) == names
    for t in schema["tools"]:
        assert t["renders_as"], f"{t['name']} has no render target"
        assert t["parameters"]["type"] == "object"


@pytest.mark.agent
def test_every_tool_has_a_description_and_parameters(ctx):
    for t in build_registry(ctx).tools:
        assert len(t.description) > 20, f"{t.name} description is too thin"
        assert "properties" in t.parameters


@pytest.mark.agent
async def test_unknown_tool_fails_gracefully(ctx):
    reg = build_registry(ctx)
    out = await reg.invoke("no_such_tool", {})
    assert out["ok"] is False
    assert "available" in out


@pytest.mark.agent
async def test_bad_arguments_do_not_crash(ctx):
    reg = build_registry(ctx)
    out = await reg.invoke("get_entity", {"wrong_arg": 1})
    assert out["ok"] is False
    assert "expected" in out


# ═══════════════════════════════════════════════════════════════════════════════
# Grounding
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.agent
async def test_fabricated_citations_are_flagged(ctx):
    """A cited id that appears in no tool result must be marked unverified."""
    ctx.client = StubClient([
        "LOOKUP",
        '{"calls":[{"name":"list_rules","arguments":{}}]}',
        "The intruder was seen at [frm_totally_made_up_00001].",
    ])
    agent = AskKestrel(ctx)
    turn = await agent.ask("what happened?")
    assert turn.verified is False
    assert "frm_totally_made_up_00001" in turn.verification_note
    assert "unverified" in turn.answer.lower()


@pytest.mark.agent
async def test_answer_without_citations_is_not_penalised(ctx):
    """Explanations and refusals legitimately cite nothing."""
    ctx.client = StubClient([
        "EXPLAIN",
        '{"calls":[{"name":"describe_architecture","arguments":{"topic":"gate"}}]}',
        "The gate uses three cheap CPU signals before spending a model call.",
    ])
    agent = AskKestrel(ctx)
    turn = await agent.ask("how does the gate work?")
    assert turn.verified is True
    assert turn.citations == []


@pytest.mark.agent
async def test_refusal_when_no_evidence_exists(ctx):
    """The database is empty; the agent must not invent an answer."""
    ctx.client = StubClient([
        "LOOKUP",
        '{"calls":[{"name":"list_alerts","arguments":{"limit":5}}]}',
        "I have no alerts recorded for that window.",
    ])
    agent = AskKestrel(ctx)
    turn = await agent.ask("was anyone here at 4am on the 3rd?")
    assert "no alerts" in turn.answer.lower()
    assert turn.verified is True


# ═══════════════════════════════════════════════════════════════════════════════
# Routing and context
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.agent
async def test_router_recognises_intents(ctx):
    for reply, expected in [("LOOKUP", "LOOKUP"), ("ACTION", "ACTION"),
                            ("EXPLAIN", "EXPLAIN"), ("nonsense", "RESEARCH")]:
        ctx.client = StubClient([reply])
        agent = AskKestrel(ctx)
        assert await agent.route("anything") == expected


@pytest.mark.agent
async def test_chitchat_short_circuits_without_tools(ctx):
    ctx.client = StubClient(["CHITCHAT"])
    agent = AskKestrel(ctx)
    turn = await agent.ask("hello")
    assert turn.tool_calls == []
    assert ctx.site.name in turn.answer


@pytest.mark.agent
async def test_selection_context_reaches_the_prompt(ctx):
    agent = AskKestrel(ctx)
    prompt = agent._system({"entity_id": "ENT-0a15-0001", "zone": "substation"})
    assert "ENT-0a15-0001" in prompt
    assert "Resolve 'this' and 'that'" in prompt


@pytest.mark.agent
async def test_agent_learns_operator_vocabulary(ctx):
    ctx.client = StubClient(["LOOKUP", '{"calls":[]}', "ok"])
    agent = AskKestrel(ctx)
    await agent.ask("anything at the substation tonight?")
    assert agent.preferences, "no vocabulary learned"
    assert any(v == "substation" for v in agent.preferences.values())


# ═══════════════════════════════════════════════════════════════════════════════
# Self-knowledge
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.agent
async def test_system_can_explain_itself(ctx):
    reg = build_registry(ctx)
    for topic in ("overview", "gate", "cascade", "memory", "rules",
                  "retrieval", "actions", "fleet", "models", "security"):
        out = await reg.invoke("describe_architecture", {"topic": topic})
        assert out["ok"] is True
        assert len(out["explanation"]) > 200, f"topic '{topic}' is too thin"


@pytest.mark.agent
async def test_explain_decision_handles_missing_ids(ctx):
    reg = build_registry(ctx)
    out = await reg.invoke("explain_decision", {"frame_id": "frm_does_not_exist"})
    assert out["ok"] is True
    assert out.get("found") is False or "frame" not in out


@pytest.mark.agent
def test_limitations_are_documented():
    from kestrel.agent.selfknowledge import LIMITATIONS

    for expected in ("telemetry is simulated", "flat ground", "re-identification",
                     "seeded simulation"):
        assert expected in LIMITATIONS, f"limitation not disclosed: {expected}"


# ═══════════════════════════════════════════════════════════════════════════════
# Fleet honesty
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.agent
async def test_fleet_marks_simulated_sites(ctx):
    """Every simulated site must carry the flag, in every payload."""
    reg = build_registry(ctx)
    out = await reg.invoke("get_fleet_status", {})
    assert out["ok"] is True
    sites = out["sites"]
    assert sites
    for s in sites:
        assert "simulated" in s, f"{s['site_id']} has no simulated flag"
    live = [s for s in sites if not s["simulated"]]
    assert len(live) == 1, "more than one site claims a live feed"


@pytest.mark.agent
async def test_cross_site_correlation_reports_provenance(ctx):
    reg = build_registry(ctx)
    out = await reg.invoke("correlate_entity_across_sites", {})
    assert out["ok"] is True
    assert "simulated" in out["note"]
    for m in out["matches"]:
        assert len(m["sites"]) >= 2
        assert m["assessment"]


# ═══════════════════════════════════════════════════════════════════════════════
# Argument reconciliation
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.agent
async def test_synonym_arguments_are_reconciled_not_rejected(ctx):
    """A model calling `start_time` where the parameter is `start` must still work.

    This was a real failure: "what happened last night" produced a red error card
    because of a five-character difference in an argument name, and the question
    went unanswered. Strict rejection is right for a wrong argument and wrong for a
    synonym of a correct one.
    """
    reg = build_registry(ctx)
    out = await reg.invoke(
        "summarize_window", {"start_time": "2026-08-06T00:00:00", "end_time": "2026-08-06T06:00:00"}
    )
    assert out.get("ok") is not False, out.get("error")
    assert "bad arguments" not in str(out.get("error", ""))


@pytest.mark.agent
async def test_an_explicit_argument_is_never_overridden_by_an_alias(ctx):
    from kestrel.agent.registry import _reconcile_arguments

    tool = build_registry(ctx).get("summarize_window")
    assert tool is not None
    out = _reconcile_arguments(tool, {"start": "keep-me", "start_time": "discard-me"})
    assert out["start"] == "keep-me"


@pytest.mark.agent
async def test_unknown_arguments_are_dropped_rather_than_crashing(ctx):
    reg = build_registry(ctx)
    out = await reg.invoke("list_rules", {"totally_made_up": 1})
    assert out["ok"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# Scope: the agent answers about this system, or says it cannot
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.agent
async def test_off_topic_question_is_refused_without_touching_a_tool(ctx):
    """An off-topic question must not enter the tool loop at all.

    It used to fall through to RESEARCH, where the planner would pick something
    plausible-looking and the model would compose an answer over irrelevant tool
    output. That is the confident non-answer an operator cannot distinguish from
    a real one, and it is worse than a refusal.
    """
    ctx.client = StubClient(["OUT_OF_SCOPE"])
    agent = AskKestrel(ctx)
    turn = await agent.ask("what is the capital of France?")

    assert turn.tool_calls == [], "an off-topic question reached the tools"
    assert "outside what I can answer" in turn.answer
    assert ctx.site.name in turn.answer, "the refusal should say what it does cover"
    assert turn.verified is True


@pytest.mark.agent
async def test_a_refusal_says_what_the_system_can_do(ctx):
    """A refusal that only says no is a different kind of unhelpful."""
    ctx.client = StubClient(["OUT_OF_SCOPE"])
    turn = await AskKestrel(ctx).ask("write me a poem about the sea")
    for capability in ("cameras saw", "alert", "rule", "fleet"):
        assert capability in turn.answer.lower(), f"refusal never mentions {capability}"


@pytest.mark.agent
async def test_uncited_answer_over_real_evidence_is_not_marked_grounded(ctx):
    """The badge must not certify what it cannot check.

    Zero citations used to mean `verified=True` unconditionally, so a
    hallucination - which by construction cites nothing - earned a green
    "grounded" pill.
    """
    from kestrel.agent.agent import Turn

    agent = AskKestrel(ctx)
    turn = Turn(question="what happened at the substation?")
    turn.intent = "LOOKUP"
    turn.answer = "Someone was loitering by the substation for several minutes."
    turn.citations = []

    # The tools did return citable evidence; the answer simply ignored it.
    collected = [{
        "tool": "list_alerts",
        "arguments": {},
        "result": {"ok": True, "alerts": [
            {"id": "alr_0a15c9d2", "title": "Loitering", "frame_ids": ["frm_plant-01_000021"]},
        ]},
    }]
    agent._verify(turn, collected)

    assert turn.verified is False
    assert "cited none" in turn.verification_note


@pytest.mark.agent
async def test_reporting_an_absence_still_counts_as_grounded(ctx):
    """'I have no alerts for that window' has nothing to cite, and that is honest."""
    ctx.client = StubClient([
        "LOOKUP",
        '{"calls":[{"name":"list_alerts","arguments":{"limit":5}}]}',
        "I have no alerts recorded for that window.",
    ])
    turn = await AskKestrel(ctx).ask("anything at 4am?")
    assert turn.verified is True
