"""Tamper-evident audit ledger.

Physical security evidence is only worth what its provenance is worth. If an alert
might have been edited after the fact, it proves nothing — and an autonomous system
that can *dispatch a drone* needs its decisions to be auditable by someone who does
not trust the operator, including the operator themselves.

So every consequential event — an alert raised, a rule enabled, a mission approved,
an operator overriding the system — is appended to a hash chain:

    hash(n) = SHA-256( hash(n-1) ‖ canonical_json(record(n)) )

Each entry commits to its predecessor, so altering any historical row invalidates
every hash after it. That does not make the log immutable — anyone with write
access can truncate it — but it makes silent *modification* detectable, which is
the property that matters for chain of custody.

This is deliberately simple. No Merkle trees, no external anchoring, no signatures.
Those are the right next steps for a production system and are named in the report
as such, rather than being half-implemented here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

GENESIS = "0" * 64


class LedgerKind(StrEnum):
    """What kinds of thing are worth committing to the chain.

    Not everything is: frames and detections are high-volume observations and live
    in ordinary tables. The ledger records *decisions and their consequences* —
    the things someone might later dispute.
    """

    ALERT_RAISED = "alert.raised"
    ALERT_STATUS = "alert.status_changed"
    ALERT_SUPPRESSED = "alert.suppressed"
    ALERT_FEEDBACK = "alert.operator_feedback"
    RULE_CREATED = "rule.created"
    RULE_ENABLED = "rule.enabled"
    RULE_DISABLED = "rule.disabled"
    MISSION_PROPOSED = "mission.proposed"
    MISSION_APPROVED = "mission.approved"
    MISSION_REJECTED = "mission.rejected"
    MISSION_COMPLETED = "mission.completed"
    AGENT_ACTION = "agent.action"
    ESCALATION = "perception.escalated"
    SESSION_START = "session.started"
    SESSION_END = "session.ended"


def canonical(payload: Any) -> str:
    """Deterministic JSON. Key order and separators must be stable or the same
    logical record would hash differently on different runs."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


class Ledger:
    """Append-only hash chain over the ``ledger`` table."""

    def __init__(self, db) -> None:
        self.db = db

    # ── append ───────────────────────────────────────────────────────────
    def append(
        self,
        kind: LedgerKind | str,
        payload: dict[str, Any],
        *,
        site_id: str | None = None,
        ref_id: str | None = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        kind_v = kind.value if isinstance(kind, LedgerKind) else str(kind)
        ts = datetime.now(UTC).isoformat()
        prev = self.head_hash()

        record = {
            "ts": ts,
            "site_id": site_id,
            "kind": kind_v,
            "ref_id": ref_id,
            "actor": actor,
            "payload": payload,
        }
        digest = hashlib.sha256(f"{prev}{canonical(record)}".encode()).hexdigest()

        self.db.conn.execute(
            """INSERT INTO ledger (ts, site_id, kind, ref_id, actor, payload_json,
                                   prev_hash, hash) VALUES (?,?,?,?,?,?,?,?)""",
            (ts, site_id, kind_v, ref_id, actor, canonical(payload), prev, digest),
        )
        self.db.commit()
        return {**record, "prev_hash": prev, "hash": digest}

    # ── read ─────────────────────────────────────────────────────────────
    def head_hash(self) -> str:
        rows = self.db.query("SELECT hash FROM ledger ORDER BY seq DESC LIMIT 1")
        return rows[0]["hash"] if rows else GENESIS

    def entries(
        self, *, site_id: str | None = None, limit: int = 200, kind: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM ledger WHERE 1=1"
        p: list[Any] = []
        if site_id:
            sql += " AND site_id = ?"
            p.append(site_id)
        if kind:
            sql += " AND kind = ?"
            p.append(kind)
        sql += " ORDER BY seq DESC LIMIT ?"
        p.append(limit)
        out = []
        for r in self.db.query(sql, tuple(p)):
            d = dict(r)
            try:
                d["payload"] = json.loads(d.pop("payload_json"))
            except (json.JSONDecodeError, KeyError):
                d["payload"] = {}
            out.append(d)
        return out

    def for_ref(self, ref_id: str) -> list[dict[str, Any]]:
        """Everything the chain records about one alert, mission or rule."""
        rows = self.db.query("SELECT * FROM ledger WHERE ref_id = ? ORDER BY seq", (ref_id,))
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.pop("payload_json"))
            except (json.JSONDecodeError, KeyError):
                d["payload"] = {}
            out.append(d)
        return out

    # ── verification ─────────────────────────────────────────────────────
    def verify(self) -> dict[str, Any]:
        """Recompute the whole chain and report the first break, if any.

        This is exercised as a test, and exposed in the UI, because a tamper-evident
        log nobody ever verifies is just a log.
        """
        rows = self.db.query("SELECT * FROM ledger ORDER BY seq")
        prev = GENESIS
        for r in rows:
            record = {
                "ts": r["ts"],
                "site_id": r["site_id"],
                "kind": r["kind"],
                "ref_id": r["ref_id"],
                "actor": r["actor"],
                "payload": json.loads(r["payload_json"]),
            }
            expected = hashlib.sha256(f"{prev}{canonical(record)}".encode()).hexdigest()
            if r["prev_hash"] != prev:
                return {
                    "valid": False,
                    "entries": len(rows),
                    "broken_at_seq": r["seq"],
                    "reason": "prev_hash does not match the preceding entry's hash",
                }
            if expected != r["hash"]:
                return {
                    "valid": False,
                    "entries": len(rows),
                    "broken_at_seq": r["seq"],
                    "reason": "recomputed hash does not match the stored hash; "
                              "this entry was modified after it was written",
                }
            prev = r["hash"]
        return {
            "valid": True,
            "entries": len(rows),
            "head": prev,
            "note": "Every entry commits to its predecessor; no modification detected.",
        }

    @property
    def stats(self) -> dict[str, Any]:
        rows = self.db.query("SELECT kind, COUNT(*) c FROM ledger GROUP BY kind ORDER BY c DESC")
        return {
            "entries": sum(int(r["c"]) for r in rows),
            "head": self.head_hash()[:16],
            "by_kind": {r["kind"]: int(r["c"]) for r in rows},
        }
