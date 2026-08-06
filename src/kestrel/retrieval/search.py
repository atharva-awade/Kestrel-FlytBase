"""Hybrid retrieval — the cross-domain requirement, done properly.

The assignment asks for frames "queryable by time or object". That is a WHERE
clause. The questions operators actually ask are not:

    "show me all truck events"                    → structured
    "anything unusual near the dock last night"   → semantic
    "a white pickup by the fence"                 → visual, and the caption may
                                                    never have used those words

No single index answers all three. So KESTREL runs two and fuses them:

*   **structured** — SQL over labels, zones, times, entities. Exact, fast, and the
    right tool when the query genuinely is a filter.
*   **semantic** — vector search over caption embeddings and, separately, over
    joint image/text embeddings. The latter is what makes "white pickup" find a
    frame whose caption said "light-coloured vehicle".

Fusion is **Reciprocal Rank Fusion**, chosen because no hosted cross-encoder was
reachable (ADR 0001) and because RRF needs no model at all: it combines rankings
rather than scores, so it is unaffected by the two retrievers producing
incomparable similarity scales. An optional LLM rerank refines the top-k when
precision matters more than latency.

Every query returns its **plan** alongside its results. A retrieval system that
cannot explain why a result surfaced is one an operator cannot audit.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from kestrel.obs.meter import Stage

# RRF constant. 60 is the value from the original paper and behaves well when one
# retriever returns far more results than the other.
RRF_K = 60


@dataclass
class QueryPlan:
    """What the planner decided to do, in a form the UI can render."""

    original: str
    intent: Literal["structured", "semantic", "visual", "hybrid"] = "hybrid"
    labels: list[str] = field(default_factory=list)
    zones: list[str] = field(default_factory=list)
    entity_ids: list[str] = field(default_factory=list)
    start_ts: datetime | None = None
    end_ts: datetime | None = None
    semantic_text: str | None = None
    min_confidence: float = 0.0
    limit: int = 30
    reasoning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "original": self.original,
            "intent": self.intent,
            "labels": self.labels,
            "zones": self.zones,
            "entity_ids": self.entity_ids,
            "start_ts": self.start_ts.isoformat() if self.start_ts else None,
            "end_ts": self.end_ts.isoformat() if self.end_ts else None,
            "semantic_text": self.semantic_text,
            "min_confidence": self.min_confidence,
            "limit": self.limit,
            "reasoning": self.reasoning,
        }

    def describe(self) -> list[str]:
        """Human-readable plan steps, shown beside the results."""
        out = []
        if self.labels:
            out.append(f"filter to objects: {', '.join(self.labels)}")
        if self.zones:
            out.append(f"restrict to zones: {', '.join(self.zones)}")
        if self.entity_ids:
            out.append(f"restrict to entities: {', '.join(self.entity_ids)}")
        if self.start_ts or self.end_ts:
            a = f"{self.start_ts:%d %b %H:%M}" if self.start_ts else "the beginning"
            b = f"{self.end_ts:%d %b %H:%M}" if self.end_ts else "now"
            out.append(f"time window: {a} → {b}")
        if self.semantic_text:
            out.append(f'semantic + visual search for "{self.semantic_text}"')
        if not out:
            out.append("no filters, returning the most recent analysed frames")
        return out


@dataclass
class SearchHit:
    frame_id: str
    ts: datetime
    caption: str
    zone_id: str | None
    labels: list[str]
    score: float
    sources: list[str] = field(default_factory=list)
    structured_rank: int | None = None
    caption_rank: int | None = None
    visual_rank: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "ts": self.ts.isoformat(),
            "caption": self.caption,
            "zone_id": self.zone_id,
            "labels": self.labels,
            "score": round(self.score, 5),
            "sources": self.sources,
            "ranks": {
                "structured": self.structured_rank,
                "caption": self.caption_rank,
                "visual": self.visual_rank,
            },
        }


@dataclass
class SearchResult:
    plan: QueryPlan
    hits: list[SearchHit]
    counts: dict[str, int]
    #: Retrievers that could not run, keyed by kind, with the reason. Empty on a
    #: healthy search. Never omitted from the payload: an empty result set with no
    #: explanation reads as "nothing was there", which is a different claim.
    degraded: dict[str, str] = field(default_factory=dict)
    took_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "plan_steps": self.plan.describe(),
            "hits": [h.to_dict() for h in self.hits],
            "counts": self.counts,
            "degraded": self.degraded,
            "complete": not self.degraded,
            "took_ms": round(self.took_ms, 1),
        }


PLANNER_SYSTEM = """You translate an operator's question about drone security footage \
into a structured retrieval plan.

Return ONLY a JSON object:
{
  "intent": "structured" | "semantic" | "visual" | "hybrid",
  "labels": ["person","truck",...],
  "zones": ["main-gate",...],
  "entity_ids": ["ENT-..."],
  "relative_hours": <number or null>,
  "start_iso": "<ISO or null>",
  "end_iso": "<ISO or null>",
  "semantic_text": "<the visual/semantic part of the query, or null>",
  "reasoning": "<one sentence on how you split the query>"
}

Guidance:
- "structured" when the query is purely a filter ("all truck events").
- "visual" when it describes appearance the caption may not have used
  ("a white pickup", "someone in a red jacket").
- "hybrid" when it is both ("trucks near the dock that look abandoned").
- Use ONLY zone ids from the provided list. Inventing one silently returns nothing.
- "last night" is roughly relative_hours 12; "today" 24; "this week" 168.
- semantic_text should be the descriptive phrase alone, not the whole sentence."""


class HybridSearch:
    """Plans a query, runs both retrievers, and fuses the rankings."""

    def __init__(self, db, site, client=None) -> None:
        self.db = db
        self.site = site
        self.client = client
        # Retrievers that could not run on the most recent search, and why. Reset
        # per query in `search`, so it always describes the result being returned.
        self._degraded: dict[str, str] = {}

    # ── planning ─────────────────────────────────────────────────────────
    async def plan(self, query: str, *, now: datetime | None = None) -> QueryPlan:
        now = now or datetime.now()
        plan = QueryPlan(original=query, semantic_text=query)

        if self.client is None:
            return self._heuristic_plan(query, now)

        zones = ", ".join(f'"{z.id}"' for z in self.site.zones)
        try:
            raw = await self.client.chat(
                [
                    {"role": "system", "content": PLANNER_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            f"Current time: {now.isoformat()}\n"
                            f"Available zone ids: {zones}\n\n"
                            f'Question: "{query}"'
                        ),
                    },
                ],
                stage=Stage.RETRIEVE,
                max_tokens=380,
                router=True,   # planning is cheap work; do not wake the 70B
            )
        except Exception:
            return self._heuristic_plan(query, now)

        from kestrel.clients.models import _loads_lenient

        payload = _loads_lenient(raw)
        if not payload:
            return self._heuristic_plan(query, now)

        known = {z.id for z in self.site.zones}
        plan.intent = payload.get("intent") if payload.get("intent") in (
            "structured", "semantic", "visual", "hybrid"
        ) else "hybrid"
        plan.labels = [str(x).lower() for x in (payload.get("labels") or []) if x]
        plan.zones = [z for z in (payload.get("zones") or []) if z in known]
        plan.entity_ids = [str(x) for x in (payload.get("entity_ids") or []) if x]
        plan.semantic_text = payload.get("semantic_text") or (
            None if plan.intent == "structured" else query
        )
        plan.reasoning = str(payload.get("reasoning") or "")

        hours = payload.get("relative_hours")
        if isinstance(hours, (int, float)) and hours > 0:
            plan.start_ts = now - timedelta(hours=float(hours))
            plan.end_ts = now
        else:
            for key, attr in (("start_iso", "start_ts"), ("end_iso", "end_ts")):
                v = payload.get(key)
                if isinstance(v, str):
                    with contextlib.suppress(ValueError):
                        setattr(plan, attr, datetime.fromisoformat(v.replace("Z", "")))
        return plan

    def _heuristic_plan(self, query: str, now: datetime) -> QueryPlan:
        """Model-free planning. Worse, but the search still works offline."""
        q = query.lower()
        plan = QueryPlan(original=query, semantic_text=query, intent="hybrid")
        for lab in ("person", "people", "truck", "car", "van", "bus", "bicycle",
                    "motorcycle", "dog", "backpack", "forklift"):
            if lab in q:
                plan.labels.append("person" if lab == "people" else lab)
        for z in self.site.zones:
            if z.id.replace("-", " ") in q or z.name.lower() in q:
                plan.zones.append(z.id)
        if "last night" in q or "overnight" in q:
            plan.start_ts, plan.end_ts = now - timedelta(hours=14), now
        elif "today" in q:
            plan.start_ts, plan.end_ts = now - timedelta(hours=24), now
        elif "week" in q:
            plan.start_ts, plan.end_ts = now - timedelta(days=7), now
        plan.reasoning = "heuristic plan, no model client available"
        return plan

    # ── retrieval ────────────────────────────────────────────────────────
    def _structured(self, plan: QueryPlan) -> list[str]:
        sql = ["SELECT DISTINCT f.id, f.ts FROM frames f"]
        where = ["f.site_id = ?", "f.analysed = 1"]
        params: list[Any] = [self.site.id]

        if plan.labels or plan.zones or plan.entity_ids:
            sql.append("JOIN detections d ON d.frame_id = f.id")
            if plan.labels:
                where.append("(" + " OR ".join("d.label LIKE ?" for _ in plan.labels) + ")")
                params += [f"%{lab}%" for lab in plan.labels]
            if plan.zones:
                where.append("d.zone_id IN (" + ",".join("?" * len(plan.zones)) + ")")
                params += plan.zones
            if plan.entity_ids:
                where.append("d.entity_id IN (" + ",".join("?" * len(plan.entity_ids)) + ")")
                params += plan.entity_ids
        if plan.start_ts:
            where.append("f.ts >= ?")
            params.append(plan.start_ts.isoformat())
        if plan.end_ts:
            where.append("f.ts <= ?")
            params.append(plan.end_ts.isoformat())

        sql.append("WHERE " + " AND ".join(where))
        sql.append("ORDER BY f.ts DESC LIMIT ?")
        params.append(plan.limit * 3)
        return [r["id"] for r in self.db.query(" ".join(sql), tuple(params))]

    async def _semantic(self, plan: QueryPlan, kind: str) -> list[str]:
        if not plan.semantic_text or self.client is None:
            if self.client is None:
                self._degraded[kind] = "no model client configured"
            return []
        try:
            # `joint=True` puts the query in the same space as the image vectors,
            # which is what allows a text query to retrieve on appearance.
            vec = await self.client.embed_text(
                plan.semantic_text, kind="query", joint=(kind == "frame")
            )
        except Exception as e:
            # Returning [] alone would be a silent failure: the operator sees "no
            # results", which is indistinguishable from "nothing was there". For a
            # system whose whole claim is knowing what it does not know, an
            # unavailable retriever has to be reported, not hidden.
            self._degraded[kind] = f"embedding provider unavailable ({type(e).__name__})"
            return []
        hits = self.db.vector_search(vec, kind=kind, site_id=self.site.id, k=plan.limit * 3)
        return [ref for ref, _ in hits]

    @staticmethod
    def _rrf(rankings: dict[str, list[str]]) -> dict[str, tuple[float, list[str], dict]]:
        """Reciprocal Rank Fusion.

        Combines *rankings*, not scores — so a retriever returning cosine
        similarities in [0,1] and one returning L2 distances can be merged without
        inventing a normalisation that would be arbitrary either way.
        """
        scores: dict[str, float] = {}
        sources: dict[str, list[str]] = {}
        ranks: dict[str, dict] = {}
        for name, ids in rankings.items():
            for i, fid in enumerate(ids):
                scores[fid] = scores.get(fid, 0.0) + 1.0 / (RRF_K + i + 1)
                sources.setdefault(fid, []).append(name)
                ranks.setdefault(fid, {})[name] = i + 1
        return {fid: (scores[fid], sources[fid], ranks[fid]) for fid in scores}

    async def search(self, query: str, *, limit: int = 30,
                     now: datetime | None = None) -> SearchResult:
        import time

        t0 = time.perf_counter()
        self._degraded = {}
        plan = await self.plan(query, now=now)
        plan.limit = limit

        rankings: dict[str, list[str]] = {}
        if plan.intent in ("structured", "hybrid", "semantic", "visual"):
            rankings["structured"] = self._structured(plan)
        if plan.intent in ("semantic", "hybrid"):
            rankings["caption"] = await self._semantic(plan, "caption")
        if plan.intent in ("visual", "hybrid"):
            rankings["visual"] = await self._semantic(plan, "frame")

        rankings = {k: v for k, v in rankings.items() if v}
        fused = self._rrf(rankings)
        top = sorted(fused.items(), key=lambda kv: -kv[1][0])[:limit]

        hits: list[SearchHit] = []
        for fid, (score, sources, ranks) in top:
            rows = self.db.query(
                "SELECT id, ts, caption, scene_json FROM frames WHERE id = ?", (fid,)
            )
            if not rows:
                continue
            r = rows[0]
            labels = [
                d["label"]
                for d in self.db.query(
                    "SELECT DISTINCT label FROM detections WHERE frame_id = ?", (fid,)
                )
            ]
            zone_rows = self.db.query(
                "SELECT zone_id FROM detections WHERE frame_id = ? AND zone_id IS NOT NULL LIMIT 1",
                (fid,),
            )
            hits.append(
                SearchHit(
                    frame_id=fid,
                    ts=datetime.fromisoformat(r["ts"]),
                    caption=r["caption"] or "",
                    zone_id=zone_rows[0]["zone_id"] if zone_rows else None,
                    labels=labels,
                    score=score,
                    sources=sources,
                    structured_rank=ranks.get("structured"),
                    caption_rank=ranks.get("caption"),
                    visual_rank=ranks.get("visual"),
                )
            )

        return SearchResult(
            plan=plan,
            hits=hits,
            counts={k: len(v) for k, v in rankings.items()},
            degraded=dict(self._degraded),
            took_ms=(time.perf_counter() - t0) * 1000,
        )

    # ── rerank ───────────────────────────────────────────────────────────
    async def rerank(self, query: str, hits: list[SearchHit], top_k: int = 10) -> list[SearchHit]:
        """Optional LLM rerank of the top-k.

        No hosted cross-encoder is available to developer keys, so relevance is
        judged by the reasoning model over captions. Slower than a cross-encoder
        and used only where precision beats latency.
        """
        if self.client is None or len(hits) <= 1:
            return hits
        head = hits[:top_k]
        listing = "\n".join(
            f"{i}. [{h.ts:%d %b %H:%M}] {h.zone_id or 'unknown zone'} — {h.caption[:150]}"
            for i, h in enumerate(head)
        )
        try:
            raw = await self.client.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "Rank search results by how well they answer the question. "
                            'Return ONLY {"order": [indices, most relevant first]}. '
                            "Include every index exactly once."
                        ),
                    },
                    {"role": "user", "content": f'Question: "{query}"\n\nResults:\n{listing}'},
                ],
                stage=Stage.RETRIEVE,
                max_tokens=180,
            )
            from kestrel.clients.models import _loads_lenient

            payload = _loads_lenient(raw) or {}
            order = [int(i) for i in payload.get("order", []) if isinstance(i, (int, float))]
            seen, out = set(), []
            for i in order:
                if 0 <= i < len(head) and i not in seen:
                    seen.add(i)
                    out.append(head[i])
            out += [h for i, h in enumerate(head) if i not in seen]
            return out + hits[top_k:]
        except Exception:
            return hits


def json_default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"not JSON serialisable: {type(o)}")


def dumps(obj) -> str:
    return json.dumps(obj, default=json_default)
