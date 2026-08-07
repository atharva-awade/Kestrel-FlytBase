"""Ask KESTREL — the conversational control plane.

Not a chat page bolted onto a dashboard: a stateful loop with a permission model,
wired to every capability in the system, that answers with structured results the
UI renders as live components.

The loop is `route → plan → tool-loop → verify → respond`, and each stage exists
for a reason:

**route** — a cheap model classifies intent. Most questions are lookups and do not
need a 70B model deliberating over them. This mirrors the perception cascade's cost
discipline, applied to cognition, and it is the difference between a 200 ms answer
and a 3 s one.

**tool-loop** — bounded iterations of tool calls. Bounded because an agent that
can loop indefinitely will, and a runaway loop against a rate-limited provider is
worse than a wrong answer.

**verify** — every factual claim must resolve to something a tool actually
returned. This is the guard against the one failure that would discredit the whole
submission: a confident, fabricated answer about what a camera saw. When evidence
is absent the correct response is "I have no frames from that window", and
refusing to guess is demonstrated on purpose.

**action gate** — the model can *propose* a state change but never perform one.
Enforcement lives in the tool registry, not in the prompt.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from kestrel.agent.registry import ToolRegistry
from kestrel.obs.meter import Stage

#: How often the stream emits a keepalive while the model is working.
HEARTBEAT_SECONDS = 4.0

MAX_TOOL_ROUNDS = 5
MAX_TOOLS_PER_ROUND = 4


SYSTEM = """You are KESTREL, an autonomous drone security analyst for {site_name}.

You are speaking to a security operator. Be direct and concrete. They are often
dealing with something live, at night, alone.

HOW YOU WORK
- Answer from tools, never from memory or assumption. If you have not called a
  tool, you do not know.
- Cite evidence. When you state a fact, reference the frame, alert or entity id it
  came from, in square brackets: [frm_...], [alr_...], [ENT-...].
- If the tools return nothing relevant, SAY SO plainly: "I have no frames from
  that window." Never invent an observation. A wrong answer about what a camera
  saw is worse than no answer.
- Distinguish what you observed from what you infer. "A person was detected at the
  gate" is an observation; "someone was casing the site" is not.
- Sites flagged simulated do not carry a live feed. Say so when you report on them.

ACTIONS
Some tools change system state or move an aircraft. You cannot execute those — you
may only propose them. The operator approves. When a tool returns
requires_confirmation, present what would happen and its consequence, then stop and
wait. Do not claim an action has been taken.

STYLE
- Lead with the answer. Context after.
- Times as HH:MM:SS, distances in metres, coordinates to 6 decimal places.
- No preamble, no "I'd be happy to". Short paragraphs.
- If asked how you work, explain honestly, including the limitations.

CONTEXT
Current time: {now}
Active site: {site_name} ({site_id})
Zones: {zones}
{selection}"""


ROUTER = """Classify this operator question into exactly one category. Reply with the \
category word only.

LOOKUP    — a specific fact retrievable in one query (an entity, an alert, a count)
RESEARCH  — needs several retrievals and synthesis ("what happened last night")
AUTHOR    — wants to create or change a rule
ACTION    — wants to dispatch a drone or change an alert's state
EXPLAIN   — asks how the system itself works or why it decided something
CHITCHAT  — greeting or small talk

Question: {question}
Category:"""


@dataclass
class AgentContext:
    """Everything the tools need. Assembled once per process."""

    site: Any
    db: Any
    ledger: Any
    client: Any
    engine: Any
    baseline: Any
    search: Any
    compiler: Any
    fleet: Any
    resolver: Any = None

    def entity_vectors(self) -> dict:
        """Entity vectors for cross-site correlation, real plus seeded."""
        import numpy as np

        from kestrel.fleet.fleet import synthetic_entity_vectors

        real: dict[str, tuple] = {}
        if self.resolver is not None:
            for e in self.resolver.entities:
                vecs = self.resolver.embeddings_for(e.id)
                if vecs:
                    real[e.id] = (
                        self.site.id, e.descriptor or e.label, e.kind.value,
                        np.mean(np.vstack(vecs), axis=0), e.last_seen,
                    )
        return synthetic_entity_vectors(
            list(self.fleet.sites.values()), plant_vectors=real
        )


@dataclass
class Turn:
    """One exchange, with the trace that produced it."""

    question: str
    answer: str = ""
    intent: str = "LOOKUP"
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    pending_confirmation: dict[str, Any] | None = None
    verified: bool = True
    verification_note: str = ""
    ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "intent": self.intent,
            "tool_calls": self.tool_calls,
            "citations": self.citations,
            "pending_confirmation": self.pending_confirmation,
            "verified": self.verified,
            "verification_note": self.verification_note,
            "ms": round(self.ms, 1),
        }


CITATION = re.compile(r"\[((?:frm|alr|det|msn|mem|ENT)[A-Za-z0-9_\-]{2,})\]")

#: Removed from both sides of the scope test. Rule and zone names are ordinary
#: English ("Loitering at a sensitive location"), so without this the derived
#: vocabulary contains "the", "a", "at" and matches literally any question.
STOPWORDS = frozenset("""
a an and any are as at be been but by can could did do does for from had has
have how i if in into is it its me my no not of on or our so than that the
their them then there these they this to was were what when where which while
who why will with would you your s t
""".split())

#: A question that names a time is asking about the timeline, and this system is
#: fundamentally an index over one. "Was anyone here at 4am on the 3rd?" shares no
#: nouns with the domain vocabulary and is squarely in scope, so a time reference
#: alone is enough to accept it.
TIME_HINT = re.compile(
    r"(\d{1,2}\s?[ap]\.?m\.?|\d{1,2}:\d{2}|\d{1,2}(?:st|nd|rd|th)"
    r"|last|past|recent|recently|since|before|after|during|between|ago|earlier"
    r"|week|weeks|month|months|day|days|hour|hours|minute|minutes|o'clock)",
    re.I,
)

#: Vocabulary this system could plausibly know something about. Generous on
#: purpose: it is used only to detect questions with *zero* domain overlap, and a
#: wrongly refused question is worse than a wrongly accepted one. Site names, zone
#: names and rule names are added at runtime from the live configuration.
DOMAIN_TERMS = frozenset("""
alert alerts alarm anomaly anomalous baseline breach camera cameras caption
confidence detect detected detection detections dock docked drone drones dwell
entity entities event events evidence feed fence fleet footage frame frames gate
gated geofence hour hours incident index intruder intrusion ledger loiter
loitering mission missions monitor monitoring night nights object objects
observed operator patrol perimeter person people rule rules scene security
sighting site sites subject substation surveillance suspicious telemetry
threat time today tonight track tracked tracking truck trucks vehicle vehicles
video visit visits watch yard zone zones yesterday morning evening afternoon
midnight happen happened happening seen saw sighted
count summary summarise summarize report
anyone anybody someone somebody everyone nobody here
car cars van vans pickup pickups lorry bike bikes bicycle bicycles motorcycle
bus buses forklift trailer suitcase backpack bag ladder cone box package parcel
dog dogs cat cats animal animals wildlife bird worker workers staff visitor
guard driver contractor delivery courier
blue red white black green yellow orange grey gray silver dark light hi-vis
back again returned return repeat repeated same new unknown unfamiliar
kestrel system architecture pipeline model models cost latency accuracy
approve approved dispatch deploy launch fly flight waypoint altitude battery
escalate escalates escalated escalation tier tiers cascade
gate gating skip skipped skipping sample sampled sampling
memory memories remember recall forget compress compression
embed embedding embeddings vector vectors similarity retrieval retrieve search
verify verified verification cite cited citation citations grounded hallucinate
suppress suppressed suppression precision recall positive negative
decide decides decision decisions reason reasoning judgement judgment
scale scalable scalability throughput budget quota limit limits rate
replay cassette cassettes offline deterministic
audit chain tamper evidence trail provenance
permission permissions approval confirm confirmation gatekeeper
prompt prompts tool tools capability capabilities
work works working behave behaviour behavior
""".split())

#: The same id shapes, unbracketed, for finding evidence *inside a tool result*.
#: Used to tell "the tools found nothing to cite" from "the answer ignored what
#: they found" - only the second is a grounding failure.
EVIDENCE_ID = re.compile(r"\"(?:frm|alr|det|msn|mem|ENT)[A-Za-z0-9_\-]{2,}\"")


# Questions about KESTREL's own operation, mapped to the self-knowledge topic
# that actually answers them. Order matters: the first topic with a matching
# keyword wins, so the more specific mechanisms are listed before the general.
SELF_TOPICS: tuple[tuple[str, frozenset[str]], ...] = (
    ("gate", frozenset("gate gating gated skip skipped skipping sample sampled "
                       "sampling frame frames cheap filter".split())),
    # The scaling *argument* is the cascade, not the fleet view: the constraint
    # that shapes this system is frames-per-shift against requests-per-minute.
    # "fleet" describes the portfolio, and answering "what are your scalability
    # limits?" from it produced a reply that discussed neither.
    ("cascade", frozenset("escalate escalates escalated escalation tier tiers "
                          "cascade pipeline perception vlm caption stage "
                          "scale scalable scalability throughput limit limits "
                          "budget quota rate volume load".split())),
    ("memory", frozenset("memory memories remember recall forget compress "
                         "compression pyramid summarise summarize baseline".split())),
    ("retrieval", frozenset("retrieval retrieve search index indexes embedding "
                            "embeddings vector semantic citation cite verify "
                            "verified grounded hallucinate".split())),
    ("security", frozenset("permission permissions approve approval unapproved "
                           "audit ledger tamper chain security safe guard "
                           "authorise authorize".split())),
    ("actions", frozenset("mission missions dispatch deploy action actions "
                          "waypoint respond response drone fly".split())),
    ("rules", frozenset("rule rules threshold suppress suppressed alerting "
                        "trigger triggers condition".split())),
    ("fleet", frozenset("fleet portfolio sites multiple".split())),
    ("models", frozenset("model models provider providers cost latency token "
                         "tokens accuracy failover".split())),
)

# A self-referential question is about *this system*, not about the site.
SELF_REFERENCE = frozenset("you your yours yourself kestrel system it its".split())

# "How does the gate work?" names no pronoun but is still a question about a
# mechanism, and the ambiguity is real: this site has a main-gate zone. The
# phrasing is what separates them. "How does the gate work" asks how something
# operates; "was anyone at the gate" asks what was observed.
MECHANISM_PHRASING = re.compile(
    r"\bhow (?:do(?:es)?|did|can|would)\b.*\bwork(?:s|ed)?\b"
    r"|\bhow (?:do(?:es)?|did) .* (?:decide|choose|handle|deal|cope|scale)\b"
    r"|\bexplain how\b|\bwhat happens when\b|\bwhy (?:do|does|did) (?:you|it|the system)\b",
    re.I,
)


def self_knowledge_topic(question: str) -> str | None:
    """The architecture topic a question is really asking about, if any.

    Returns None for questions about the *site* rather than the system, so
    "was anyone at the gate?" is never answered with a design essay.
    """
    words = set(re.findall(r"[a-z]+", question.lower()))
    mechanism = bool(MECHANISM_PHRASING.search(question))
    if not (words & SELF_REFERENCE) and not mechanism:
        return None
    for topic, keywords in SELF_TOPICS:
        if words & keywords:
            return topic
    # A bare mechanism question with no recognisable subject is not necessarily
    # about this system at all, so only an explicit self-reference earns the
    # general overview.
    return "overview" if words & SELF_REFERENCE else None


class AskKestrel:
    """The control plane."""

    # Why the last planning round produced no tools, when the cause was a
    # failure rather than a decision. Class-level so paths that answer without
    # ever planning (chitchat, refusals) can still read it safely.
    plan_failure: str = ""

    def __init__(self, ctx: AgentContext, registry: ToolRegistry | None = None) -> None:
        self.ctx = ctx
        from kestrel.agent.tools import build_registry

        self.registry = registry or build_registry(ctx)
        self.history: list[dict[str, str]] = []
        # Learned vocabulary — "the north fence" → zone id. Populated as the
        # operator uses their own words, so the second conversation is better
        # than the first.
        self.preferences: dict[str, str] = {}
        self.turns: list[Turn] = []

    # ── prompt ───────────────────────────────────────────────────────────
    def _system(self, selection: dict[str, Any] | None) -> str:
        sel = ""
        if selection:
            bits = [f"{k}={v}" for k, v in selection.items() if v]
            if bits:
                sel = (
                    "\nThe operator is currently looking at: " + ", ".join(bits)
                    + ". Resolve 'this' and 'that' against it."
                )
        if self.preferences:
            sel += "\nOperator vocabulary: " + ", ".join(
                f'"{k}" means {v}' for k, v in list(self.preferences.items())[:8]
            )
        return SYSTEM.format(
            site_name=self.ctx.site.name,
            site_id=self.ctx.site.id,
            now=self._now(),
            zones=", ".join(z.id for z in self.ctx.site.zones),
            selection=sel,
        )

    def _now(self) -> str:
        """The clock this agent reasons against: the last thing it observed.

        This was `datetime.now()` to the second, which was wrong twice over.

        Semantically, "what happened last night" is a question about the footage,
        and the footage has its own clock -- the demo deliberately compresses the
        site clock, so the operator's wall clock can be hours or days away from
        the last frame actually indexed. Answering against the laptop's time made
        every relative-time question quietly reason over the wrong window.

        Practically, a timestamp that changes every second made every system
        prompt unique, so no /api/ask cassette could ever match its recording.
        The agent's cassettes were write-only: replay mode recorded hundreds of
        them and then failed every question, which looked like a broken agent
        rather than an uncacheable prompt.

        The observation clock is stable while the data is stable, so a recording
        replays exactly, and it moves when new footage arrives, which is the
        dependency that was wanted in the first place.
        """
        try:
            rows = self.ctx.db.query("SELECT MAX(ts) AS t FROM frames")
            latest = rows[0]["t"] if rows else None
        except Exception:
            latest = None
        if not latest:
            # Nothing indexed yet: there is no observation clock to use, so fall
            # back to the wall clock and accept that this turn will not cache.
            return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return str(latest).replace("T", " ")[:19]

    # ── routing ──────────────────────────────────────────────────────────
    async def route(self, question: str) -> str:
        # Scope is decided locally, before the router runs, for three reasons: it
        # needs no model call, it works with the network unplugged, and it cannot
        # be talked out of by the question itself. A router prompt asking a model
        # to police its own scope is exactly the kind of guard that fails when it
        # matters.
        if self.out_of_scope(question):
            return "OUT_OF_SCOPE"

        try:
            raw = await self.ctx.client.chat(
                [{"role": "user", "content": ROUTER.format(question=question)}],
                stage=Stage.REASON, max_tokens=8, router=True,
            )
        except Exception:
            return "RESEARCH"
        word = (raw or "").strip().upper().split()[0] if raw.strip() else "RESEARCH"
        return word if word in {
            "LOOKUP", "RESEARCH", "AUTHOR", "ACTION", "EXPLAIN", "CHITCHAT",
        } else "RESEARCH"

    def out_of_scope(self, question: str) -> bool:
        """Does this question reference anything this system could know about?

        Deliberately conservative: it refuses only when the question overlaps the
        domain vocabulary *not at all*. A false refusal is more damaging than a
        false accept, because the tool loop and the citation check are still there
        to catch a wrong answer, whereas a refused legitimate question is simply
        a broken assistant.

        The vocabulary is drawn from the system itself - the site, its zones, its
        rules - so it stays correct as those change, rather than being a list that
        silently goes stale.
        """
        if TIME_HINT.search(question):
            return False              # a question about *when* is a question about the index

        if self_knowledge_topic(question) is not None:
            # A question about this system is in scope by definition. Checked
            # against the domain vocabulary alone, "tell me about yourself" was
            # refused: none of its words name a site, a zone or an object, which
            # is exactly what makes it a question about the system instead.
            return False

        words = set(re.findall(r"[a-z0-9']+", question.lower())) - STOPWORDS
        if len(words) < 2:
            return False              # greetings and one-word follow-ups: let the router judge

        vocab = set(DOMAIN_TERMS)
        site = getattr(self.ctx, "site", None)
        if site is not None:
            vocab |= set(re.findall(r"[a-z0-9]+", site.name.lower()))
            for z in getattr(site, "zones", []):
                vocab |= set(re.findall(r"[a-z0-9]+", f"{z.id} {z.name}".lower()))
        engine = getattr(self.ctx, "engine", None)
        for rule in getattr(engine, "rules", []) or []:
            vocab |= set(re.findall(r"[a-z0-9]+", f"{rule.id} {rule.name}".lower()))
        vocab |= set(self.preferences.values())

        return not (words & (vocab - STOPWORDS))

    # ── the loop ─────────────────────────────────────────────────────────
    async def ask(
        self,
        question: str,
        *,
        selection: dict[str, Any] | None = None,
        on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> Turn:
        """Answer a question, optionally reporting progress as it goes.

        `on_event` exists so the streaming endpoint can show work in progress
        without a second implementation of this loop. Duplicating it would mean
        duplicating the permission gate, and the gate is the one thing in this
        file that must have exactly one code path.
        """
        import time

        async def emit(event: dict[str, Any]) -> None:
            if on_event is not None:
                await on_event(event)

        t0 = time.perf_counter()
        turn = Turn(question=question)
        turn.intent = await self.route(question)
        await emit({"type": "intent", "intent": turn.intent})

        if turn.intent == "OUT_OF_SCOPE":
            # Refuse here, before any tool runs.
            #
            # Previously an off-topic question fell through to RESEARCH and
            # entered the five-round tool loop. The planner would pick something
            # plausible, `search_frames("capital of France")`, and the model would
            # then compose an answer over irrelevant tool output. That is the
            # vague, confident non-answer an operator cannot tell from a real one.
            #
            # A refusal has to be concrete about what it cannot do and what it
            # can, or it is only a different kind of unhelpful.
            turn.answer = (
                "That is outside what I can answer. I am the security analyst for "
                f"{self.ctx.site.name}, and I only answer from this system's own "
                "evidence.\n\n"
                "I can tell you what the cameras saw and when, look up a person or "
                "vehicle across days, explain why an alert fired or was suppressed, "
                "write and backtest a rule, report on the fleet, or walk you through "
                "how any part of KESTREL works."
            )
            turn.verified = True
            turn.verification_note = "refused: outside the system's evidence"
            turn.ms = (time.perf_counter() - t0) * 1000
            self.turns.append(turn)
            return turn

        if turn.intent == "CHITCHAT":
            turn.answer = (
                f"KESTREL, monitoring {self.ctx.site.name}. Ask me what happened, "
                f"search the footage, check an entity, write a rule, or ask how I work."
            )
            turn.ms = (time.perf_counter() - t0) * 1000
            self.turns.append(turn)
            return turn

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system(selection)},
            *self.history[-6:],
            {"role": "user", "content": question},
        ]
        specs = self.registry.specs()
        collected: list[dict[str, Any]] = []

        for _round in range(MAX_TOOL_ROUNDS):
            await emit({"type": "planning", "round": _round + 1})
            plan = await self._plan_tools(messages, specs, turn.intent)
            # A question about how KESTREL itself works always has a right
            # answer available, so it should not depend on the planner choosing
            # well. "How do you decide what to escalate?" routed to EXPLAIN
            # correctly and then picked no tool at all, leaving the system
            # unable to describe its own central mechanism.
            #
            # The topic has to be chosen too: describe_architecture defaults to
            # "overview", so calling it bare answered "how does your memory
            # compression work?" with a general summary that mentioned none of
            # it, and the composer honestly reported having nothing to go on.
            if _round == 0 and not collected:
                topic = self_knowledge_topic(question)
                chosen = {c.get("name") for c in plan}
                if topic and "describe_architecture" not in chosen:
                    # Not only when the planner picked nothing. "How does the
                    # gate work?" was answered with search_frames, which dutifully
                    # returned photographs of the site's physical gate -- the
                    # wrong sense of the word, and a confident non-answer. The
                    # self-knowledge source is added alongside whatever else was
                    # chosen rather than replacing it, so a question that is
                    # genuinely about both still gets both.
                    plan = [{"name": "describe_architecture",
                             "arguments": {"topic": topic}}, *plan]
            if not plan:
                break

            round_results = []
            for call in plan[:MAX_TOOLS_PER_ROUND]:
                name = call.get("name")
                args = call.get("arguments") or {}
                if not isinstance(args, dict):
                    args = {}
                await emit({"type": "tool_start", "tool": name, "arguments": args})
                result = await self.registry.invoke(str(name), args)
                record = {"tool": name, "arguments": args, "result": result}
                collected.append(record)
                turn.tool_calls.append(record)
                round_results.append(record)
                await emit({
                    "type": "tool",
                    "tool": name,
                    "arguments": args,
                    "renders_as": (result or {}).get("renders_as", "text"),
                    "result": result,
                })

                if result.get("requires_confirmation"):
                    # The gate fired. Stop and hand the decision to the human.
                    turn.pending_confirmation = {
                        "tool": name, "arguments": args,
                        "consequence": result.get("consequence", ""),
                        "message": result.get("message", ""),
                    }

            if turn.pending_confirmation:
                break

            messages.append({
                "role": "assistant",
                "content": "Called: " + ", ".join(str(c.get("name")) for c in plan),
            })
            messages.append({
                "role": "user",
                "content": "Tool results:\n" + _compact(round_results),
            })

            # A lookup is done after one productive round.
            if turn.intent in ("LOOKUP", "EXPLAIN") and round_results:
                break

        await emit({"type": "composing"})
        turn.answer = await self._respond(messages, collected, turn)
        turn.citations = sorted(set(CITATION.findall(turn.answer)))
        self._verify(turn, collected)
        self._learn(question)

        self.history += [
            {"role": "user", "content": question},
            {"role": "assistant", "content": turn.answer[:1200]},
        ]
        turn.ms = (time.perf_counter() - t0) * 1000
        self.turns.append(turn)
        return turn

    async def _plan_tools(
        self, messages: list[dict[str, Any]], specs: list[dict[str, Any]], intent: str
    ) -> list[dict[str, Any]]:
        """Decide which tools to call next.

        Tool selection is done as constrained JSON rather than provider-native tool
        calling, because support varies across the two providers we fail over
        between and a single code path is worth more than native syntax.
        """
        catalogue = "\n".join(
            f"- {s['function']['name']}: {s['function']['description'][:150]}"
            for s in specs
        )
        ask = (
            "Choose the tools to call next to answer the operator. Return ONLY:\n"
            '{"calls":[{"name":"tool_name","arguments":{...}}]}\n'
            'Return {"calls":[]} when you already have what you need, and also '
            "when no tool can answer the question at all. Do not force an "
            "unrelated tool onto a question this system cannot answer.\n\n"
            f"Available tools:\n{catalogue}"
        )
        try:
            raw = await self.ctx.client.chat(
                [*messages, {"role": "user", "content": ask}],
                stage=Stage.REASON,
                max_tokens=420,
                router=(intent == "LOOKUP"),
            )
        except Exception as e:
            # A planner that cannot be reached is not the same thing as a planner
            # that considered the question and chose nothing, but both used to
            # return [] and surface as "I could not find a tool that answers
            # that". The system then looked incapable when it was merely offline
            # -- in replay, one unrecorded question made every deck look broken.
            self.plan_failure = f"{type(e).__name__}: {e}"[:200]
            return []
        self.plan_failure = ""
        from kestrel.clients.models import _loads_lenient

        payload = _loads_lenient(raw) or {}
        calls = payload.get("calls")
        return [c for c in calls if isinstance(c, dict) and c.get("name")] if isinstance(calls, list) else []

    async def _respond(
        self, messages: list[dict[str, Any]], collected: list[dict[str, Any]], turn: Turn
    ) -> str:
        if turn.pending_confirmation:
            p = turn.pending_confirmation
            return (
                f"This needs your approval before I can do it.\n\n"
                f"**{p['tool']}**: {p['consequence']}\n\n"
                f"Arguments: `{json.dumps(p['arguments'])}`\n\n"
                f"Approve or decline below."
            )
        if not collected and self.plan_failure:
            # Say which it is. Reporting a reachability failure as "no tool fits"
            # is the kind of quiet mislabelling that makes a working system look
            # broken and sends the reader debugging the wrong layer.
            return (
                "I could not reach the model that selects tools, so I have not "
                "answered rather than guessing.\n\n"
                f"Cause: `{self.plan_failure}`\n\n"
                "In replay mode this means the question has no recorded "
                "response yet. The rest of the system is unaffected."
            )
        if not collected:
            return (
                "I could not find a tool that answers that. Try asking about what "
                "was seen, an entity, an alert, the rules, the fleet, or how I work."
            )
        try:
            return await self.ctx.client.chat(
                [
                    *messages,
                    {
                        "role": "user",
                        "content": (
                            "Answer the operator's question now, using ONLY the tool "
                            "results above. Cite ids in square brackets. If the "
                            "results do not answer the question, say so plainly "
                            "rather than guessing."
                        ),
                    },
                ],
                stage=Stage.REASON,
                max_tokens=600,
            )
        except Exception as e:
            return f"I retrieved the data but could not compose an answer ({type(e).__name__})."

    # ── verification ─────────────────────────────────────────────────────
    def _verify(self, turn: Turn, collected: list[dict[str, Any]]) -> None:
        """Check that cited ids actually appear in tool results.

        This is the guard against the failure that would discredit everything else:
        a confident answer citing a frame that does not exist. It does not verify
        that the *reasoning* is sound — only that the evidence is real, which is
        the part that can be checked mechanically.
        """
        if not turn.citations:
            # An uncited answer used to be marked verified unconditionally, which
            # meant a hallucination - which by construction cites nothing - earned
            # a green "grounded" badge. That badge was worse than no badge: it
            # certified the one class of answer it could not check.
            #
            # But silence is not always suspicious. "I have no alerts for that
            # window" is a report of an *absence*, and there is nothing to cite.
            # The distinction that matters is whether the tools returned citable
            # evidence which the answer then failed to reference.
            factual = turn.intent in ("LOOKUP", "RESEARCH", "ACTION")
            evidence = EVIDENCE_ID.search(json.dumps(collected, default=str))
            if factual and evidence:
                turn.verified = False
                turn.verification_note = (
                    "the tools returned evidence but the answer cited none of it, "
                    "so it could not be checked"
                )
            else:
                turn.verified = True
                turn.verification_note = "no ids cited"
            return

        available: set[str] = set()
        blob = json.dumps(collected, default=str)
        for cid in turn.citations:
            if cid in blob:
                available.add(cid)

        fabricated = [c for c in turn.citations if c not in available]
        if fabricated:
            turn.verified = False
            turn.verification_note = (
                f"{len(fabricated)} cited id(s) do not appear in any tool result: "
                f"{fabricated[:4]}"
            )
            turn.answer += (
                f"\n\n_Note: {len(fabricated)} reference(s) in this answer could not "
                f"be matched to retrieved evidence and should be treated as unverified._"
            )
        else:
            turn.verified = True
            turn.verification_note = f"all {len(turn.citations)} citations resolved"

    def _learn(self, question: str) -> None:
        """Pick up the operator's own vocabulary for site locations."""
        low = question.lower()
        for z in self.ctx.site.zones:
            for phrase in (f"the {z.kind.value}", z.name.lower()):
                if phrase in low and phrase not in self.preferences:
                    self.preferences[phrase] = z.id

    # ── confirmed actions ────────────────────────────────────────────────
    async def confirm(self, tool: str, arguments: dict[str, Any], *, approve: bool) -> dict[str, Any]:
        """Execute a gated tool after an explicit human decision.

        The only path by which `approved=True` reaches the registry. The agent loop
        never calls this.
        """
        if not approve:
            from kestrel.storage.ledger import LedgerKind

            self.ctx.ledger.append(
                LedgerKind.AGENT_ACTION,
                {"tool": tool, "arguments": arguments, "decision": "declined"},
                site_id=self.ctx.site.id, actor="operator",
            )
            return {"ok": True, "executed": False, "message": "declined by operator"}

        result = await self.registry.invoke(tool, arguments, approved=True)
        from kestrel.storage.ledger import LedgerKind

        self.ctx.ledger.append(
            LedgerKind.AGENT_ACTION,
            {"tool": tool, "arguments": arguments, "decision": "approved",
             "result_ok": result.get("ok", False)},
            site_id=self.ctx.site.id, actor="operator",
        )
        return {"ok": True, "executed": True, "result": result}

    # ── streaming ────────────────────────────────────────────────────────
    async def stream(
        self, question: str, *, selection: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Emit progress events so the console can show the agent working.

        The previous version awaited `ask()` in one go, which meant two events
        and then total silence for the whole tool loop. On a RESEARCH question
        that is several model round-trips, so the interface sat on "thinking"
        with no way to tell work in progress from a dead connection.

        `ask()` now reports progress through a callback, drained here by a queue.
        A heartbeat every few seconds keeps the connection warm and, more
        importantly, lets the client distinguish slow from broken.
        """
        yield {"type": "start", "question": question}

        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def push(event: dict[str, Any]) -> None:
            await queue.put(event)

        task = asyncio.create_task(
            self.ask(question, selection=selection, on_event=push)
        )
        task.add_done_callback(lambda _: queue.put_nowait(None))

        waited = 0.0
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except TimeoutError:
                waited += HEARTBEAT_SECONDS
                yield {"type": "heartbeat", "waited_s": round(waited, 1)}
                continue
            if event is None:
                break
            yield event

        try:
            full = await task
        except Exception as e:
            yield {"type": "error", "error": f"{type(e).__name__}: {e}"[:200]}
            return

        if full.pending_confirmation:
            yield {"type": "confirmation", **full.pending_confirmation}
        yield {"type": "answer", "text": full.answer, "citations": full.citations,
               "verified": full.verified, "verification_note": full.verification_note}
        yield {"type": "done", "ms": full.ms, "turn": full.to_dict()}

    # ── proactive ────────────────────────────────────────────────────────
    async def morning_brief(self) -> str:
        """A shift-change summary the operator did not have to ask for."""
        alerts = self.registry.get("list_alerts")
        summary = self.registry.get("summarize_window")
        parts = []
        if alerts:
            parts.append(await self.registry.invoke("list_alerts", {"limit": 10}))
        if summary:
            parts.append(await self.registry.invoke("summarize_window", {"hours": 12}))
        try:
            return await self.ctx.client.chat(
                [
                    {"role": "system", "content": self._system(None)},
                    {
                        "role": "user",
                        "content": (
                            "Write a shift-change brief for the incoming operator. "
                            "Lead with anything still open. Be brief and specific. "
                            "If the night was quiet, say so in one sentence.\n\n"
                            + _compact(parts)
                        ),
                    },
                ],
                stage=Stage.REASON, max_tokens=380,
            )
        except Exception:
            return "Unable to generate a brief. The reasoning model is unavailable."

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "turns": len(self.turns),
            "registry": self.registry.stats,
            "verified_turns": sum(1 for t in self.turns if t.verified),
            "gated_actions": sum(1 for t in self.turns if t.pending_confirmation),
            "learned_vocabulary": self.preferences,
        }


def _compact(items: Any, limit: int = 6000) -> str:
    """Serialise tool results small enough to fit in a prompt."""
    text = json.dumps(items, default=str, separators=(",", ":"))
    return text[:limit] + ("… (truncated)" if len(text) > limit else "")
