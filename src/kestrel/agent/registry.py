"""The tool registry — Ask KESTREL's entire reach.

Every capability the assistant has is declared here, once. Two things follow from
that, and both matter more than the tool list itself:

**Permissions are structural, not advisory.** Each tool carries a
``Permission``. ``AUTO`` tools run freely; ``CONFIRM`` tools *cannot execute* —
the agent may only propose them, and execution requires a separate call carrying
an explicit human decision. An assistant that can enable a detection rule or
launch an aircraft needs that boundary to be enforced by the code path rather
than by prompt wording, because prompt wording is not a security control.

**The schema is the contract with the frontend.** ``export_schema()`` emits JSON
Schema for every tool, which the web app uses to render each result with the right
component. Declaring the tool once and deriving both sides means the agent and the
UI cannot drift apart.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Permission(StrEnum):
    AUTO = "auto"        # read-only; the agent runs it without asking
    CONFIRM = "confirm"  # changes state or moves an aircraft; needs a human click


class ToolClass(StrEnum):
    RETRIEVE = "retrieve"
    ANALYSE = "analyse"
    AUTHOR = "author"
    ACT = "act"
    OPERATE = "operate"
    NAVIGATE = "navigate"
    FLEET = "fleet"
    EXPLAIN = "explain"


@dataclass
class Tool:
    name: str
    description: str
    tool_class: ToolClass
    permission: Permission
    parameters: dict[str, Any]           # JSON Schema for the arguments
    handler: Callable[..., Any]
    # Which React component renders this tool's result. Part of the generative-UI
    # contract: the agent answers with live UI, not walls of text.
    renders_as: str = "text"
    # Shown on the confirmation card for CONFIRM tools, so a human approving an
    # action can see what it will do before it happens.
    consequence: str = ""
    examples: list[str] = field(default_factory=list)

    def spec(self) -> dict[str, Any]:
        """OpenAI-style function spec for the model."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "class": self.tool_class.value,
            "permission": self.permission.value,
            "description": self.description,
            "parameters": self.parameters,
            "renders_as": self.renders_as,
            "consequence": self.consequence,
            "examples": self.examples,
        }


class ToolRegistry:
    """Holds the tools and enforces the permission boundary."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self.calls: list[dict[str, Any]] = []

    # ── registration ─────────────────────────────────────────────────────
    def register(
        self,
        name: str,
        description: str,
        tool_class: ToolClass,
        permission: Permission,
        parameters: dict[str, Any],
        *,
        renders_as: str = "text",
        consequence: str = "",
        examples: list[str] | None = None,
    ):
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._tools[name] = Tool(
                name=name, description=description, tool_class=tool_class,
                permission=permission, parameters=parameters, handler=fn,
                renders_as=renders_as, consequence=consequence,
                examples=examples or [],
            )
            return fn

        return deco

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    # ── access ───────────────────────────────────────────────────────────
    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    @property
    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    def specs(self, *, permission: Permission | None = None) -> list[dict[str, Any]]:
        return [
            t.spec() for t in self._tools.values()
            if permission is None or t.permission is permission
        ]

    def by_class(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for t in self._tools.values():
            out.setdefault(t.tool_class.value, []).append(t.describe())
        return out

    def export_schema(self) -> dict[str, Any]:
        """The contract the frontend consumes to render tool results."""
        return {
            "version": 1,
            "tools": [t.describe() for t in self._tools.values()],
            "classes": {c.value: [] for c in ToolClass}
            | {k: [t["name"] for t in v] for k, v in self.by_class().items()},
            "permissions": {
                "auto": [t.name for t in self._tools.values()
                         if t.permission is Permission.AUTO],
                "confirm": [t.name for t in self._tools.values()
                            if t.permission is Permission.CONFIRM],
            },
            "note": (
                "Tools listed under 'confirm' cannot be executed by the agent. "
                "They may only be proposed; execution requires an explicit human "
                "decision through a separate endpoint, and every such decision is "
                "written to the hash-chained audit ledger."
            ),
        }

    # ── execution ────────────────────────────────────────────────────────
    async def invoke(
        self, name: str, arguments: dict[str, Any], *, approved: bool = False
    ) -> dict[str, Any]:
        """Run a tool.

        ``approved`` may only be set by the human-decision endpoint. The agent loop
        never passes it, so a CONFIRM tool invoked by the model always returns a
        proposal rather than performing the action. This is the security boundary,
        and it is asserted in ``tests/test_agent.py``.
        """
        tool = self._tools.get(name)
        if tool is None:
            return {
                "ok": False,
                "error": f"unknown tool '{name}'",
                "available": sorted(self._tools),
            }

        if tool.permission is Permission.CONFIRM and not approved:
            record = {
                "ok": True,
                "requires_confirmation": True,
                "tool": name,
                "arguments": arguments,
                "consequence": tool.consequence,
                "renders_as": "confirmation_card",
                "message": (
                    f"'{name}' changes system state and needs your approval before it "
                    f"runs. {tool.consequence}"
                ),
            }
            self.calls.append({"tool": name, "arguments": arguments, "gated": True})
            return record

        arguments = _reconcile_arguments(tool, arguments)

        try:
            result = tool.handler(**arguments)
            if inspect.isawaitable(result):
                result = await result
        except TypeError as e:
            return {"ok": False, "error": f"bad arguments for '{name}': {e}",
                    "expected": tool.parameters}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}

        self.calls.append({"tool": name, "arguments": arguments, "gated": False})
        payload = result if isinstance(result, dict) else {"result": result}
        return {"ok": True, "tool": name, "renders_as": tool.renders_as, **payload}

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "registered": len(self._tools),
            "auto": sum(1 for t in self._tools.values() if t.permission is Permission.AUTO),
            "confirm": sum(1 for t in self._tools.values()
                           if t.permission is Permission.CONFIRM),
            "invocations": len(self.calls),
            "gated": sum(1 for c in self.calls if c["gated"]),
        }


# ── common parameter shapes ──────────────────────────────────────────────────
def obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required or [],
        "additionalProperties": False,
    }


STR = {"type": "string"}
INT = {"type": "integer"}
NUM = {"type": "number"}
BOOL = {"type": "boolean"}


def enum(*values: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


# ── argument reconciliation ──────────────────────────────────────────────────
#: Names a model reasonably reaches for, mapped to the parameter actually declared.
#: These are all cases observed in practice rather than speculative.
_ALIASES: dict[str, tuple[str, ...]] = {
    "start": ("start_time", "start_ts", "from", "since", "begin", "after"),
    "end": ("end_time", "end_ts", "to", "until", "before"),
    "hours": ("hours_back", "last_hours", "window_hours", "period_hours"),
    "limit": ("count", "n", "max_results", "top_k"),
    "site_id": ("site",),
    "zone_id": ("zone",),
    "entity_id": ("entity",),
    "rule_id": ("rule",),
    "query": ("q", "text", "search"),
}

#: alias -> canonical, built once.
_ALIAS_TO_CANONICAL: dict[str, str] = {
    alias: canonical for canonical, aliases in _ALIASES.items() for alias in aliases
}


def _reconcile_arguments(tool: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """Map near-miss argument names onto the ones a tool actually declares.

    A model asked for "what happened last night" and called `summarize_window`
    with `start_time`. The parameter is `start`. The call was rejected, the
    operator saw a red error card, and the question went unanswered because of a
    five-character difference in a name.

    Strict rejection is right for a *wrong* argument and wrong for a synonym of a
    correct one. Only aliases of parameters the tool genuinely declares are
    translated, and only when the real name was not supplied, so this cannot
    invent a parameter or silently override an explicit one.
    """
    declared = set((tool.parameters or {}).get("properties", {}))
    if not declared:
        # The tool takes no arguments at all, so anything supplied is a model slip.
        # Passing it through would raise a TypeError for a call that is otherwise
        # perfectly valid.
        return {}

    out = dict(arguments)
    for given in list(out):
        if given in declared:
            continue
        canonical = _ALIAS_TO_CANONICAL.get(given)
        if canonical and canonical in declared and canonical not in out:
            out[canonical] = out.pop(given)

    # Anything still unrecognised is dropped rather than passed through to a
    # TypeError: an extra argument is a model slip, not a reason to fail a call
    # whose required parameters are all present.
    out = {k: v for k, v in out.items() if k in declared}
    return _reconcile_enums(tool, out)


#: What a model writes when it means "do not filter on this". None is a declared
#: enum value anywhere, so these are unambiguous.
_WILDCARDS = frozenset({"all", "any", "*", "none", "null", "everything", "both"})


def _reconcile_enums(tool: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """Drop enum arguments whose value is not one of the declared options.

    Asked to "show me the most recent alerts", a model called `list_alerts` with
    `status="all", severity="all"`. Neither is a declared option. They went
    straight into a SQL equality test and a list comprehension, so the query
    became `WHERE status = 'all'`, matched nothing, and the operator was told
    "there are no recent alerts" while four open alerts sat in the table.

    That is the worst failure shape available: not an error, but a confident and
    plausible falsehood. Declaring an enum in the schema has to mean something at
    the boundary, not just in the prompt the model may ignore.

    A wildcard means "no filter", which is what the model intended, so the key is
    dropped and the tool's own default applies. Any other invalid value is also
    dropped rather than raising: the alternative is failing a call whose intent
    was clear, and the tool result still reports what it actually filtered on.
    """
    props = (tool.parameters or {}).get("properties", {})
    out = dict(arguments)
    for key, value in list(out.items()):
        options = (props.get(key) or {}).get("enum")
        if not options or not isinstance(value, str):
            continue
        if value in options:
            continue
        lowered = value.strip().lower()
        if lowered in _WILDCARDS or lowered not in {str(o).lower() for o in options}:
            out.pop(key)
        else:
            # Right option, wrong casing: keep the intent, fix the value.
            out[key] = next(o for o in options if str(o).lower() == lowered)
    return out
