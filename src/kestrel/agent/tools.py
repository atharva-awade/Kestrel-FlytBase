"""Ask KESTREL's tools.

Every capability in the system, reachable through conversation. Grouped by class,
and — where a tool changes state or moves an aircraft — gated behind an explicit
human decision.

The design principle throughout: **a tool returns evidence, not prose.** Each
result carries the ids a claim rests on, so the verifier can check that every
factual statement in the final answer is supported and the UI can render results
as live components with citations back to real frames.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from kestrel.agent.registry import (
    INT,
    NUM,
    STR,
    Permission,
    ToolClass,
    ToolRegistry,
    enum,
    obj,
)
from kestrel.domain import MissionStatus


def build_registry(ctx) -> ToolRegistry:
    """Construct the registry bound to a live system context."""
    r = ToolRegistry()

    # ═══════════════════════════════════════════════════════════════════════
    # RETRIEVE
    # ═══════════════════════════════════════════════════════════════════════
    @r.register(
        "search_frames",
        "Search indexed frames using hybrid structured + semantic + visual retrieval. "
        "Use for any question about what was seen, when, or where.",
        ToolClass.RETRIEVE, Permission.AUTO,
        obj({"query": STR, "limit": INT}, ["query"]),
        renders_as="frame_strip",
        examples=["show me all truck events", "anything near the dock after sunset"],
    )
    async def search_frames(query: str, limit: int = 12) -> dict[str, Any]:
        res = await ctx.search.search(query, limit=min(limit, 40))
        return {
            "query": query,
            "plan": res.plan.to_dict(),
            "plan_steps": res.plan.describe(),
            "retrievers": res.counts,
            "took_ms": round(res.took_ms, 1),
            "count": len(res.hits),
            "hits": [h.to_dict() for h in res.hits],
            # Explicit citation list — the verifier checks claims against this.
            "citations": [h.frame_id for h in res.hits],
        }

    @r.register(
        "get_entity",
        "Get the dossier for one persistent entity: description, visit history, "
        "zones, first and last seen.",
        ToolClass.RETRIEVE, Permission.AUTO,
        obj({"entity_id": STR}, ["entity_id"]),
        renders_as="entity_card",
        examples=["who is ENT-0a15-0001?"],
    )
    def get_entity(entity_id: str) -> dict[str, Any]:
        row = ctx.db.entity(entity_id)
        if not row:
            return {"found": False, "entity_id": entity_id,
                    "message": f"no entity {entity_id} in the index"}
        sightings = ctx.db.query(
            """SELECT d.ts, d.zone_id, d.label, d.frame_id, d.lat, d.lon
               FROM detections d WHERE d.entity_id = ? ORDER BY d.ts DESC LIMIT 60""",
            (entity_id,),
        )
        return {
            "found": True,
            "entity": {
                **{k: row[k] for k in ("id", "kind", "label", "descriptor",
                                       "first_seen", "last_seen", "visit_count",
                                       "frame_count", "threat_score")},
                "attributes": json.loads(row["attributes_json"] or "{}"),
                "zones": json.loads(row["zones_json"] or "[]"),
                "sites": json.loads(row["sites_json"] or "[]"),
            },
            "sightings": [dict(s) for s in sightings],
            "citations": [s["frame_id"] for s in sightings[:20]],
        }

    @r.register(
        "list_entities",
        "List entities seen at a site, most recent first.",
        ToolClass.RETRIEVE, Permission.AUTO,
        obj({"site_id": STR, "kind": STR, "limit": INT}),
        renders_as="entity_list",
    )
    def list_entities(site_id: str | None = None, kind: str | None = None,
                      limit: int = 25) -> dict[str, Any]:
        rows = ctx.db.entities(site_id or ctx.site.id, limit=min(limit, 100))
        if kind:
            rows = [x for x in rows if x["kind"] == kind]
        return {
            "count": len(rows),
            "entities": [
                {k: x[k] for k in ("id", "kind", "label", "descriptor",
                                   "visit_count", "first_seen", "last_seen")}
                for x in rows
            ],
        }

    @r.register(
        "list_alerts",
        "List security alerts, optionally filtered by status or severity. Each "
        "alert includes its dispatch coordinates.",
        ToolClass.RETRIEVE, Permission.AUTO,
        obj({"site_id": STR,
             "status": enum("open", "acknowledged", "investigating", "resolved",
                            "dismissed", "false_positive"),
             "severity": enum("info", "low", "medium", "high", "critical"),
             "limit": INT}),
        renders_as="alert_list",
        examples=["what alerts are open?", "show me critical alerts"],
    )
    def list_alerts(site_id: str | None = None, status: str | None = None,
                    severity: str | None = None, limit: int = 20) -> dict[str, Any]:
        rows = ctx.db.alerts(site_id or ctx.site.id, status=status, limit=min(limit, 100))
        if severity:
            rows = [a for a in rows if a["severity"] == severity]
        out = []
        for a in rows:
            ev = json.loads(a["evidence_json"] or "[]")
            loc = next((e["detail"] for e in ev
                        if e.get("kind") == "telemetry"
                        and "Dispatch" in (e.get("caption") or "")), None)
            out.append({
                "id": a["id"], "ts": a["ts"], "severity": a["severity"],
                "title": a["title"], "status": a["status"], "zone_id": a["zone_id"],
                "confidence": a["confidence"], "rule_id": a["rule_id"],
                "mission_id": a["mission_id"],
                "location": loc,
            })
        return {"count": len(out), "alerts": out,
                "citations": [a["id"] for a in out]}

    @r.register(
        "get_alert_evidence",
        "Get the full chain of evidence behind one alert: contributing frames, "
        "which rule clauses matched, telemetry, baseline deviation and the VLM "
        "description. Use when asked why an alert fired.",
        ToolClass.RETRIEVE, Permission.AUTO,
        obj({"alert_id": STR}, ["alert_id"]),
        renders_as="evidence_chain",
        examples=["why did that alert fire?"],
    )
    def get_alert_evidence(alert_id: str) -> dict[str, Any]:
        rows = ctx.db.query("SELECT * FROM alerts WHERE id = ?", (alert_id,))
        if not rows:
            return {"found": False, "alert_id": alert_id}
        a = dict(rows[0])
        evidence = json.loads(a.pop("evidence_json") or "[]")
        a["entity_ids"] = json.loads(a.pop("entity_ids_json") or "[]")
        a["frame_ids"] = json.loads(a.pop("frame_ids_json") or "[]")
        return {
            "found": True, "alert": a, "evidence": evidence,
            "ledger": ctx.ledger.for_ref(alert_id),
            "citations": a["frame_ids"] + [alert_id],
        }

    @r.register(
        "get_telemetry",
        "Get drone telemetry around a time: position, altitude, battery, wind, "
        "light level and the resulting perception confidence.",
        ToolClass.RETRIEVE, Permission.AUTO,
        obj({"site_id": STR, "at": STR, "window_minutes": INT}),
        renders_as="telemetry_panel",
    )
    def get_telemetry(site_id: str | None = None, at: str | None = None,
                      window_minutes: int = 5) -> dict[str, Any]:
        sid = site_id or ctx.site.id
        if at:
            try:
                centre = datetime.fromisoformat(at)
            except ValueError:
                centre = datetime.now()
            lo = (centre - timedelta(minutes=window_minutes)).isoformat()
            hi = (centre + timedelta(minutes=window_minutes)).isoformat()
            rows = ctx.db.query(
                "SELECT json FROM telemetry WHERE site_id=? AND ts BETWEEN ? AND ? "
                "ORDER BY ts LIMIT 200", (sid, lo, hi))
        else:
            rows = ctx.db.query(
                "SELECT json FROM telemetry WHERE site_id=? ORDER BY ts DESC LIMIT 40",
                (sid,))
        samples = [json.loads(r["json"]) for r in rows]
        return {"count": len(samples), "samples": samples[:40]}

    # ═══════════════════════════════════════════════════════════════════════
    # ANALYSE
    # ═══════════════════════════════════════════════════════════════════════
    @r.register(
        "summarize_window",
        "Summarise what happened over a period using the temporal memory pyramid. "
        "Use for 'what happened last night' style questions.",
        ToolClass.ANALYSE, Permission.AUTO,
        obj({"site_id": STR, "hours": NUM, "start": STR, "end": STR}),
        renders_as="narrative_block",
        examples=["what happened last night?", "summarise the last 6 hours"],
    )
    def summarize_window(site_id: str | None = None, hours: float | None = None,
                         start: str | None = None, end: str | None = None) -> dict[str, Any]:
        sid = site_id or ctx.site.id
        sql = "SELECT * FROM memory_nodes WHERE site_id = ?"
        params: list[Any] = [sid]
        if start:
            sql += " AND end_ts >= ?"
            params.append(start)
        if end:
            sql += " AND start_ts <= ?"
            params.append(end)
        elif hours:
            sql += " AND start_ts >= ?"
            params.append((datetime.now() - timedelta(hours=hours)).isoformat())
        sql += " ORDER BY level DESC, start_ts"
        rows = ctx.db.query(sql, tuple(params))

        # The requested window can legitimately miss everything: a session is
        # ingested against a *site* clock (the demo footage runs at 02:10), while
        # "the last 12 hours" is measured from the real one. Answering "nothing
        # happened" in that case is wrong twice over, because something did happen
        # and the operator is told the yard was quiet.
        #
        # So fall back to the most recent nodes on record and say plainly which
        # window is actually being summarised. Reporting a different window is
        # honest; reporting silence is not.
        fell_back = False
        if not rows:
            rows = ctx.db.query(
                "SELECT * FROM memory_nodes WHERE site_id = ? "
                "ORDER BY end_ts DESC, level DESC LIMIT 40",
                (sid,),
            )
            fell_back = bool(rows)

        if not rows:
            return {
                "found": False,
                "message": "no memory nodes for that window. Has a session been ingested?",
            }

        covered = sorted(r["start_ts"] for r in rows)
        note = None
        if fell_back:
            note = (
                "Nothing fell inside the window you asked about, so this is the most "
                f"recent activity on record instead: {covered[0][:16].replace('T', ' ')} "
                f"to {max(r['end_ts'] for r in rows)[:16].replace('T', ' ')}."
            )

        return {
            "found": True,
            "window_shifted": fell_back,
            "note": note,
            "covers": {"from": covered[0], "to": max(r["end_ts"] for r in rows)},
            "nodes": [
                {"level": r["level"], "start": r["start_ts"], "end": r["end_ts"],
                 "summary": r["summary"], "salience": r["salience"],
                 "entity_ids": json.loads(r["entity_ids_json"] or "[]")}
                for r in rows[:40]
            ],
            "citations": [r["id"] for r in rows[:20]],
        }

    @r.register(
        "check_baseline_anomaly",
        "Ask whether an observation is unusual for this zone and hour, against the "
        "learned normalcy model.",
        ToolClass.ANALYSE, Permission.AUTO,
        obj({"zone_id": STR, "label": STR, "hour": INT}, ["zone_id", "label"]),
        renders_as="baseline_panel",
        examples=["is a truck at the dock at 3am unusual?"],
    )
    def check_baseline_anomaly(zone_id: str, label: str, hour: int | None = None) -> dict[str, Any]:
        when = datetime.now().replace(hour=hour if hour is not None else datetime.now().hour)
        dev = ctx.baseline.evaluate(zone_id, when, label)
        return {
            "zone_id": zone_id, "label": label, "hour": when.hour,
            "anomalous": dev.anomalous, "confident": dev.confident,
            "first_ever": dev.first_ever, "z_score": None if dev.z in (float("inf"),) else dev.z,
            "observed": dev.observed, "mean": dev.mean, "stdev": dev.stdev,
            "days_of_history": dev.days_of_history,
            "explanation": dev.explanation,
        }

    @r.register(
        "get_zone_profile",
        "Get the normal activity profile for a zone by hour of day.",
        ToolClass.ANALYSE, Permission.AUTO,
        obj({"zone_id": STR}, ["zone_id"]),
        renders_as="zone_profile",
    )
    def get_zone_profile(zone_id: str) -> dict[str, Any]:
        zone = ctx.site.zone_by_id(zone_id)
        return {
            "zone_id": zone_id,
            "name": zone.name if zone else None,
            "kind": zone.kind.value if zone else None,
            "priority": zone.priority if zone else None,
            "normal_hours": list(zone.normal_hours) if zone and zone.normal_hours else None,
            "hourly_mean_activity": ctx.baseline.profile(zone_id),
            "quietest_hours": ctx.baseline.quietest_hours(zone_id),
        }

    # ═══════════════════════════════════════════════════════════════════════
    # AUTHOR — gated
    # ═══════════════════════════════════════════════════════════════════════
    @r.register(
        "compile_rule_from_text",
        "Translate a plain-English security requirement into a validated rule and "
        "backtest it against indexed history. Does NOT enable it.",
        ToolClass.AUTHOR, Permission.AUTO,   # compiling is read-only; enabling is not
        obj({"text": STR}, ["text"]),
        renders_as="rule_preview",
        examples=["alert me if a truck parks at the dock for over 10 minutes after 9pm"],
    )
    async def compile_rule_from_text(text: str) -> dict[str, Any]:
        from kestrel.rules.compiler import observations_from_db

        try:
            rule = await ctx.compiler.compile(text)
        except Exception as e:
            return {"ok": False, "error": f"could not compile: {e}"[:300]}
        obs = observations_from_db(ctx.db, ctx.site.id)
        report = ctx.compiler.backtest(rule, obs, baseline=ctx.baseline)
        return {
            "ok": True,
            "rule": rule.model_dump(mode="json"),
            "yaml": rule.to_yaml(),
            "explanation": rule.explain(),
            "backtest": report.to_dict(),
            "next_step": (
                f"Rule '{rule.id}' compiled and backtested but is NOT active. "
                f"Call enable_rule to activate it."
            ),
        }

    @r.register(
        "enable_rule",
        "Activate a compiled rule so it evaluates live frames.",
        ToolClass.AUTHOR, Permission.CONFIRM,
        obj({"rule_id": STR, "yaml": STR}, ["rule_id"]),
        renders_as="rule_card",
        consequence="This rule will begin evaluating every analysed frame and may "
                    "raise alerts to operators.",
    )
    def enable_rule(rule_id: str, yaml: str | None = None) -> dict[str, Any]:
        from kestrel.rules.dsl import Rule

        if yaml:
            rule = Rule.from_yaml(yaml)
        else:
            rule = next((x for x in ctx.engine.rules if x.id == rule_id), None)
            if rule is None:
                return {"ok": False, "error": f"no rule {rule_id}"}
        rule.enabled = True
        if all(x.id != rule.id for x in ctx.engine.rules):
            ctx.engine.rules.append(rule)
        ctx.db.conn.execute(
            """INSERT OR REPLACE INTO rules (id, site_id, name, yaml, enabled, origin, created_ts)
               VALUES (?,?,?,?,1,?,?)""",
            (rule.id, ctx.site.id, rule.name, rule.to_yaml(), rule.origin,
             datetime.now().isoformat()),
        )
        ctx.db.commit()
        from kestrel.storage.ledger import LedgerKind

        ctx.ledger.append(LedgerKind.RULE_ENABLED,
                          {"rule_id": rule.id, "name": rule.name, "origin": rule.origin},
                          site_id=ctx.site.id, ref_id=rule.id, actor="operator")
        return {"ok": True, "rule_id": rule.id, "enabled": True,
                "message": f"rule '{rule.name}' is now active"}

    @r.register(
        "disable_rule",
        "Deactivate a rule so it stops raising alerts.",
        ToolClass.AUTHOR, Permission.CONFIRM,
        obj({"rule_id": STR}, ["rule_id"]),
        renders_as="rule_card",
        consequence="This rule will stop evaluating frames. Any threat it currently "
                    "detects will go unreported.",
    )
    def disable_rule(rule_id: str) -> dict[str, Any]:
        rule = next((x for x in ctx.engine.rules if x.id == rule_id), None)
        if rule is None:
            return {"ok": False, "error": f"no rule {rule_id}"}
        rule.enabled = False
        from kestrel.storage.ledger import LedgerKind

        ctx.ledger.append(LedgerKind.RULE_DISABLED, {"rule_id": rule_id},
                          site_id=ctx.site.id, ref_id=rule_id, actor="operator")
        return {"ok": True, "rule_id": rule_id, "enabled": False}

    @r.register(
        "list_rules",
        "List the active rule pack with each rule's conditions in plain English.",
        ToolClass.RETRIEVE, Permission.AUTO,
        obj({}),
        renders_as="rule_list",
        examples=["what rules are active?"],
    )
    def list_rules() -> dict[str, Any]:
        return {
            "count": len(ctx.engine.rules),
            "rules": [
                {"id": x.id, "name": x.name, "severity": x.severity.value,
                 "enabled": x.enabled, "origin": x.origin,
                 "description": x.description, "conditions": x.explain(),
                 "visual_predicate": x.visual_predicate,
                 "fires": ctx.engine.stats["fires"].get(x.id, 0)}
                for x in ctx.engine.rules
            ],
        }

    # ═══════════════════════════════════════════════════════════════════════
    # ACT — gated
    # ═══════════════════════════════════════════════════════════════════════
    @r.register(
        "propose_mission",
        "Plan a drone response to an alert: waypoints, altitudes, and a feasibility "
        "check against battery, geofence, wind and daylight. Proposal only.",
        ToolClass.ACT, Permission.AUTO,   # planning is read-only; flying is not
        obj({"alert_id": STR}, ["alert_id"]),
        renders_as="mission_card",
        examples=["should we investigate that alert?"],
    )
    def propose_mission(alert_id: str) -> dict[str, Any]:
        rows = ctx.db.query("SELECT * FROM alerts WHERE id = ?", (alert_id,))
        if not rows:
            return {"ok": False, "error": f"no alert {alert_id}"}
        existing = ctx.db.query(
            "SELECT * FROM missions WHERE alert_id = ? ORDER BY created_ts DESC LIMIT 1",
            (alert_id,))
        if existing:
            m = dict(existing[0])
            return {
                "ok": True, "mission_id": m["id"], "status": m["status"],
                "rationale": m["rationale"],
                "steps": json.loads(m["steps_json"]),
                "feasibility": json.loads(m["feasibility_json"]),
                "next_step": "Call approve_mission to authorise this flight.",
            }
        return {"ok": False, "error": "no mission planned for that alert yet"}

    @r.register(
        "approve_mission",
        "Authorise a proposed mission and fly it.",
        ToolClass.ACT, Permission.CONFIRM,
        obj({"mission_id": STR, "note": STR}, ["mission_id"]),
        renders_as="mission_execution",
        consequence="This launches the aircraft and flies the planned route. The "
                    "decision is written to the tamper-evident audit ledger.",
    )
    def approve_mission(mission_id: str, note: str = "") -> dict[str, Any]:
        rows = ctx.db.query("SELECT * FROM missions WHERE id = ?", (mission_id,))
        if not rows:
            return {"ok": False, "error": f"no mission {mission_id}"}
        m = dict(rows[0])
        feas = json.loads(m["feasibility_json"])
        if not feas["feasible"]:
            return {"ok": False, "error": "mission is not feasible",
                    "blockers": feas["blockers"]}
        ctx.db.conn.execute(
            "UPDATE missions SET status=?, decided_ts=?, decided_by=?, outcome=? WHERE id=?",
            (MissionStatus.APPROVED.value, datetime.now().isoformat(), "operator",
             note or "approved via Ask KESTREL", mission_id))
        ctx.db.commit()
        from kestrel.storage.ledger import LedgerKind

        ctx.ledger.append(LedgerKind.MISSION_APPROVED,
                          {"mission_id": mission_id, "note": note},
                          site_id=ctx.site.id, ref_id=mission_id, actor="operator")
        return {"ok": True, "mission_id": mission_id, "status": "approved",
                "steps": json.loads(m["steps_json"]),
                "message": "mission approved and dispatched"}

    @r.register(
        "update_alert_status",
        "Change an alert's status, or mark it a false positive so the system learns.",
        ToolClass.ACT, Permission.CONFIRM,
        obj({"alert_id": STR,
             "status": enum("acknowledged", "investigating", "resolved",
                            "dismissed", "false_positive"),
             "note": STR}, ["alert_id", "status"]),
        renders_as="alert_card",
        consequence="Marking an alert a false positive lowers confidence for that "
                    "rule and zone in future, making similar alerts less likely.",
    )
    def update_alert_status(alert_id: str, status: str, note: str = "") -> dict[str, Any]:
        rows = ctx.db.query("SELECT * FROM alerts WHERE id = ?", (alert_id,))
        if not rows:
            return {"ok": False, "error": f"no alert {alert_id}"}
        ctx.db.conn.execute(
            "UPDATE alerts SET status=?, operator_feedback=? WHERE id=?",
            (status, note, alert_id))
        ctx.db.commit()
        from kestrel.storage.ledger import LedgerKind

        ctx.ledger.append(LedgerKind.ALERT_STATUS,
                          {"alert_id": alert_id, "status": status, "note": note},
                          site_id=ctx.site.id, ref_id=alert_id, actor="operator")
        return {"ok": True, "alert_id": alert_id, "status": status}

    # ═══════════════════════════════════════════════════════════════════════
    # FLEET
    # ═══════════════════════════════════════════════════════════════════════
    @r.register(
        "get_fleet_status",
        "Portfolio status across every site: alerts, drone states, threat scores.",
        ToolClass.FLEET, Permission.AUTO,
        obj({}),
        renders_as="fleet_table",
        examples=["how is the fleet doing?"],
    )
    def get_fleet_status() -> dict[str, Any]:
        return {
            "summary": ctx.fleet.summary(),
            "sites": [s.to_dict() for s in ctx.fleet.status()],
        }

    @r.register(
        "correlate_entity_across_sites",
        "Find subjects seen at more than one site. This is a reconnaissance "
        "indicator that no single site can detect on its own.",
        ToolClass.FLEET, Permission.AUTO,
        obj({}),
        renders_as="globe_arcs",
        examples=["has this vehicle been seen at any other site?"],
    )
    def correlate_entity_across_sites() -> dict[str, Any]:
        matches = ctx.fleet.correlate_entities(ctx.entity_vectors())
        return {
            "count": len(matches),
            "matches": [m.to_dict() for m in matches],
            "note": (
                "Sites flagged simulated=true do not carry a live feed; their "
                "entities come from a seeded generator."
            ),
        }

    @r.register(
        "find_cross_site_pattern",
        "Find regions where several sites are alerting in the same window.",
        ToolClass.FLEET, Permission.AUTO,
        obj({"window_hours": INT}),
        renders_as="pattern_list",
    )
    def find_cross_site_pattern(window_hours: int = 6) -> dict[str, Any]:
        return {"patterns": ctx.fleet.find_temporal_pattern(window_hours=window_hours)}

    # ═══════════════════════════════════════════════════════════════════════
    # NAVIGATE — drives the UI
    # ═══════════════════════════════════════════════════════════════════════
    @r.register(
        "navigate_to",
        "Move the operator's view to a page, site, entity, alert or frame.",
        ToolClass.NAVIGATE, Permission.AUTO,
        obj({"view": enum("command", "console", "investigate", "entities",
                          "rules", "analyst", "evals", "architecture", "site"),
             "site_id": STR, "entity_id": STR, "alert_id": STR, "frame_id": STR},
            ["view"]),
        renders_as="navigation",
        examples=["take me to the fence breach alert"],
    )
    def navigate_to(view: str, site_id: str | None = None, entity_id: str | None = None,
                    alert_id: str | None = None, frame_id: str | None = None) -> dict[str, Any]:
        return {"navigate": {"view": view, "site_id": site_id, "entity_id": entity_id,
                             "alert_id": alert_id, "frame_id": frame_id}}

    @r.register(
        "focus_globe_on",
        "Fly the 3D command globe to a region or site.",
        ToolClass.NAVIGATE, Permission.AUTO,
        obj({"country": STR, "site_id": STR, "lat": NUM, "lon": NUM}),
        renders_as="globe_focus",
    )
    def focus_globe_on(country: str | None = None, site_id: str | None = None,
                       lat: float | None = None, lon: float | None = None) -> dict[str, Any]:
        if site_id and site_id in ctx.fleet.sites:
            s = ctx.fleet.sites[site_id]
            lat, lon = s.origin.lat, s.origin.lon
        return {"globe": {"country": country, "site_id": site_id, "lat": lat, "lon": lon}}

    # ═══════════════════════════════════════════════════════════════════════
    # OPERATE
    # ═══════════════════════════════════════════════════════════════════════
    @r.register(
        "get_pipeline_stats",
        "Perception pipeline statistics: gate efficiency, latency per stage, "
        "detector backend, escalations.",
        ToolClass.OPERATE, Permission.AUTO,
        obj({}),
        renders_as="stats_panel",
        examples=["how many frames did you skip?"],
    )
    def get_pipeline_stats() -> dict[str, Any]:
        from kestrel.obs.meter import METER

        return {"database": ctx.db.stats, "meter": METER.snapshot(),
                "ledger": ctx.ledger.stats}

    @r.register(
        "get_cost_report",
        "Modelled cost of the analysis: tokens, per-stage spend, cost per drone-hour.",
        ToolClass.OPERATE, Permission.AUTO,
        obj({"observed_seconds": NUM}),
        renders_as="cost_panel",
    )
    def get_cost_report(observed_seconds: float = 3600.0) -> dict[str, Any]:
        from kestrel.obs.meter import METER

        snap = METER.snapshot(observed_seconds=observed_seconds)
        return {"cost": snap["cost"], "frames": snap["frames"], "stages": snap["stages"],
                "basis": "reference commercial rates; the developer tier billed nothing"}

    @r.register(
        "verify_audit_ledger",
        "Verify the tamper-evident audit chain and report whether any entry was "
        "modified after it was written.",
        ToolClass.OPERATE, Permission.AUTO,
        obj({}),
        renders_as="ledger_panel",
        examples=["has the audit log been tampered with?"],
    )
    def verify_audit_ledger() -> dict[str, Any]:
        return {"verification": ctx.ledger.verify(), "stats": ctx.ledger.stats,
                "recent": ctx.ledger.entries(limit=15)}

    # ═══════════════════════════════════════════════════════════════════════
    # EXPLAIN — the system accounts for itself
    # ═══════════════════════════════════════════════════════════════════════
    @r.register(
        "describe_architecture",
        "Explain how KESTREL works: the perception cascade, memory, rules, the "
        "action loop, and the design decisions behind them.",
        ToolClass.EXPLAIN, Permission.AUTO,
        obj({"topic": enum("overview", "cascade", "gate", "memory", "rules",
                           "retrieval", "actions", "fleet", "models", "security")}),
        renders_as="architecture_panel",
        examples=["explain your architecture", "how does the gate work?"],
    )
    def describe_architecture(topic: str = "overview") -> dict[str, Any]:
        from kestrel.agent.selfknowledge import ARCHITECTURE

        return {
            "topic": topic,
            "explanation": ARCHITECTURE.get(topic, ARCHITECTURE["overview"]),
            "available_topics": sorted(ARCHITECTURE),
        }

    @r.register(
        "explain_decision",
        "Explain a specific decision the system made: why a frame was skipped, why "
        "a model was escalated to, or why an alert was suppressed.",
        ToolClass.EXPLAIN, Permission.AUTO,
        obj({"frame_id": STR, "alert_id": STR}),
        renders_as="decision_trace",
        examples=["why did you escalate on that frame?", "why was that suppressed?"],
    )
    def explain_decision(frame_id: str | None = None,
                         alert_id: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if frame_id:
            rows = ctx.db.query(
                "SELECT id, ts, analysed, gate_reason, gate_novelty, caption "
                "FROM frames WHERE id = ?", (frame_id,))
            if rows:
                f = dict(rows[0])
                out["frame"] = f
                out["gate_explanation"] = (
                    f"The tier-0 gate {'analysed' if f['analysed'] else 'SKIPPED'} this "
                    f"frame. Reason: {f['gate_reason']}. Novelty score "
                    f"{f['gate_novelty']}. Skipping costs nothing and is the common "
                    f"case; analysing spends a model call."
                )
        if alert_id:
            rows = ctx.db.query(
                "SELECT id, status, suppressed_reason, confidence, rule_id "
                "FROM alerts WHERE id = ?", (alert_id,))
            if rows:
                a = dict(rows[0])
                out["alert"] = a
                out["triage_explanation"] = (
                    a["suppressed_reason"] or
                    f"Alert was raised with confidence {a['confidence']} and not suppressed."
                )
                out["ledger"] = ctx.ledger.for_ref(alert_id)
        if not out:
            return {"found": False,
                    "message": "provide a frame_id or alert_id to explain"}
        return out

    @r.register(
        "list_sites",
        "List every site in the portfolio, with which ones carry a live feed.",
        ToolClass.FLEET, Permission.AUTO,
        obj({}),
        renders_as="site_list",
    )
    def list_sites() -> dict[str, Any]:
        return {
            "count": len(ctx.fleet.sites),
            "sites": [
                {"id": s.id, "name": s.name, "kind": s.kind,
                 "country": s.country_name, "lat": s.origin.lat, "lon": s.origin.lon,
                 "zones": len(s.zones), "live_footage": s.live_footage,
                 "simulated": not s.live_footage}
                for s in ctx.fleet.sites.values()
            ],
        }

    return r
