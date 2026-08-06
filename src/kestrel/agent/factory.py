"""Assemble a live Ask KESTREL instance.

One place that knows how the subsystems fit together, so the API, the CLI and the
tests all get an identically-wired agent rather than three drifting variants.
"""

from __future__ import annotations

from pathlib import Path

from kestrel.agent.agent import AgentContext, AskKestrel
from kestrel.clients.models import get_client
from kestrel.config import get_settings
from kestrel.memory.baseline import BaselineModel
from kestrel.memory.entities import EntityResolver
from kestrel.retrieval.search import HybridSearch
from kestrel.rules.compiler import RuleCompiler
from kestrel.rules.engine import RuleEngine
from kestrel.rules.pack import default_rules
from kestrel.storage.db import get_db
from kestrel.storage.ledger import Ledger


def build_agent(site_id: str = "plant-01", *, db=None, client=None) -> AskKestrel:
    """Wire the whole system into a conversational control plane."""
    from kestrel.fleet.fleet import FleetManager
    from kestrel.sim.sites import load_fleet, load_site

    s = get_settings()
    db = db or get_db()
    client = client or get_client()
    site = load_site(site_id, s.sites_dir)

    baseline = BaselineModel(site.id, db=db)
    # Restore accumulated history so "first time in N days" survives a restart.
    try:
        rows = db.query(
            "SELECT zone_id, hour, label, day, count FROM baseline WHERE site_id = ?",
            (site.id,),
        )
        baseline.load([dict(r) for r in rows])
    except Exception:
        pass

    # Rules: shipped pack plus anything an operator authored and enabled.
    rules = default_rules()
    try:
        from kestrel.rules.dsl import Rule

        for r in db.query("SELECT yaml FROM rules WHERE enabled = 1"):
            try:
                rule = Rule.from_yaml(r["yaml"])
                if all(x.id != rule.id for x in rules):
                    rules.append(rule)
            except Exception:
                continue
    except Exception:
        pass

    resolver = EntityResolver(site.id)
    try:
        import numpy as np

        from kestrel.domain import Entity

        entities, vectors = [], {}
        for row in db.entities(site.id, limit=500):
            import json as _json

            entities.append(
                Entity(
                    id=row["id"], site_id=row["site_id"], kind=row["kind"],
                    label=row["label"], descriptor=row["descriptor"] or "",
                    attributes=_json.loads(row["attributes_json"] or "{}"),
                    first_seen=row["first_seen"], last_seen=row["last_seen"],
                    visit_count=row["visit_count"], frame_count=row["frame_count"],
                    zones_seen=_json.loads(row["zones_json"] or "[]"),
                    sites_seen=_json.loads(row["sites_json"] or "[]"),
                    threat_score=row["threat_score"] or 0.0,
                    notes=row["notes"] or "",
                )
            )
            vecs = db.query(
                """SELECT e.vec FROM embeddings e
                   JOIN detections d ON d.id = e.ref_id
                   WHERE d.entity_id = ? AND e.kind = 'crop' LIMIT 8""",
                (row["id"],),
            )
            if vecs:
                vectors[row["id"]] = [
                    np.frombuffer(v["vec"], dtype=np.float32) for v in vecs
                ]
        resolver.load(entities, vectors)
    except Exception:
        pass

    ctx = AgentContext(
        site=site,
        db=db,
        ledger=Ledger(db),
        client=client,
        engine=RuleEngine(site, rules, baseline=baseline),
        baseline=baseline,
        search=HybridSearch(db, site, client),
        compiler=RuleCompiler(site, client),
        fleet=FleetManager(load_fleet(s.sites_dir), db),
        resolver=resolver,
    )
    return AskKestrel(ctx)


def export_tool_schema(path: Path | None = None) -> dict:
    """Write the tool contract the frontend consumes.

    Declaring tools once in Python and generating the frontend's view of them is
    what keeps the agent and the UI from drifting apart.
    """
    import json

    agent = build_agent()
    schema = agent.registry.export_schema()
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
    return schema
