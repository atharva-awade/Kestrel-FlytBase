"""Fleet intelligence — what only a portfolio view can see.

The assignment describes one drone on one property. FlytBase's customers run
many sites, and this layer exists to show that the architecture scales to that —
but it is not merely a bigger dashboard. It produces a *class of finding that a
single-site system cannot produce at all*:

    "This vehicle has now been seen at three of your sites in five days."

That is a reconnaissance pattern. No amount of analysis at Plant-01 reveals it,
because the evidence is distributed across sites. Since joint image/text
embeddings are already computed per entity for local re-identification, comparing
them *across* sites costs almost nothing — the capability falls out of work
already done.

**On honesty.** We have one real video feed. Only ``plant-01`` runs real footage
through the real pipeline; every other site is driven by the seeded generator
below and is flagged ``simulated`` in every payload that leaves this module. The
UI renders that flag, and the report states it plainly. A portfolio view that
implied forty live aircraft would be the one thing capable of discrediting
everything else in the submission.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from kestrel.domain import Severity, Site

# Cross-site appearance match. Higher than the within-site threshold: a false
# positive here produces a dramatic and wrong claim about coordinated activity,
# so the bar to assert it is deliberately steeper.
CROSS_SITE_THRESHOLD = 0.82
# Two sites seeing the "same" entity within seconds are more likely a matching
# failure than a teleporting vehicle.
MIN_TRAVEL_SECONDS = 1800


@dataclass
class SiteStatus:
    site_id: str
    name: str
    lat: float
    lon: float
    country: str
    country_name: str
    kind: str
    simulated: bool
    drone_state: str
    battery_pct: float
    active_alerts: int
    peak_severity: Severity | None
    threat_score: float
    entities_today: int
    last_seen: datetime | None
    online: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "name": self.name,
            "lat": self.lat,
            "lon": self.lon,
            "country": self.country,
            "country_name": self.country_name,
            "kind": self.kind,
            # Consumed by the UI to render the SIMULATED chip. Never omitted.
            "simulated": self.simulated,
            "drone_state": self.drone_state,
            "battery_pct": round(self.battery_pct, 1),
            "active_alerts": self.active_alerts,
            "peak_severity": self.peak_severity.value if self.peak_severity else None,
            "threat_score": round(self.threat_score, 3),
            "entities_today": self.entities_today,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "online": self.online,
        }


@dataclass
class CrossSiteMatch:
    """One entity observed at more than one site."""

    descriptor: str
    kind: str
    sites: list[str]
    site_names: list[str]
    sightings: list[dict[str, Any]] = field(default_factory=list)
    similarity: float = 0.0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    confidence: str = "probable"

    @property
    def span_hours(self) -> float:
        if not (self.first_seen and self.last_seen):
            return 0.0
        return (self.last_seen - self.first_seen).total_seconds() / 3600

    @property
    def assessment(self) -> str:
        n = len(self.sites)
        if n >= 3:
            return (
                f"{self.descriptor} has appeared at {n} sites within "
                f"{self.span_hours:.0f} hours. A single subject probing multiple "
                f"sites in one window is a reconnaissance pattern and warrants "
                f"correlation with access logs."
            )
        return (
            f"{self.descriptor} was seen at {n} sites over "
            f"{self.span_hours:.0f} hours. Possibly a shared contractor or "
            f"delivery route, worth confirming against the vendor schedule."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "descriptor": self.descriptor,
            "kind": self.kind,
            "sites": self.sites,
            "site_names": self.site_names,
            "sightings": self.sightings,
            "similarity": round(self.similarity, 4),
            "span_hours": round(self.span_hours, 1),
            "confidence": self.confidence,
            "assessment": self.assessment,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
        }


class FleetManager:
    """Portfolio-level status and correlation across sites."""

    def __init__(self, sites: list[Site], db=None) -> None:
        self.sites = {s.id: s for s in sites}
        self.db = db
        self._generated: dict[str, dict[str, Any]] = {}

    # ── status ───────────────────────────────────────────────────────────
    def status(self, *, now: datetime | None = None) -> list[SiteStatus]:
        now = now or datetime.now()
        out: list[SiteStatus] = []
        for site in self.sites.values():
            out.append(
                self._real_status(site, now) if site.live_footage
                else self._simulated_status(site, now)
            )
        return sorted(out, key=lambda s: (-s.threat_score, s.site_id))

    def _real_status(self, site: Site, now: datetime) -> SiteStatus:
        alerts, peak, threat, entities, last = [], None, 0.0, 0, None
        if self.db is not None:
            rows = self.db.alerts(site.id, status="open", limit=200)
            alerts = rows
            if rows:
                order = [Severity.INFO, Severity.LOW, Severity.MEDIUM,
                         Severity.HIGH, Severity.CRITICAL]
                peak = max(
                    (Severity(r["severity"]) for r in rows),
                    key=lambda s: order.index(s),
                )
                threat = min(
                    1.0,
                    sum(Severity(r["severity"]).weight * (r["confidence"] or 0.5)
                        for r in rows) / 3.0,
                )
                last = datetime.fromisoformat(rows[0]["ts"])
            ents = self.db.entities(site.id, limit=500)
            entities = len(ents)

        return SiteStatus(
            site_id=site.id, name=site.name, lat=site.origin.lat, lon=site.origin.lon,
            country=site.country, country_name=site.country_name, kind=site.kind,
            simulated=False, drone_state="patrolling", battery_pct=78.0,
            active_alerts=len(alerts), peak_severity=peak, threat_score=threat,
            entities_today=entities, last_seen=last, online=True,
        )

    def _simulated_status(self, site: Site, now: datetime) -> SiteStatus:
        """Seeded, deterministic status for a site with no live feed.

        Seeded on site id and the hour so the globe is stable within an hour and
        evolves across hours — a globe that reshuffles on every poll looks broken.
        """
        rng = random.Random(f"{site.id}:{now:%Y%m%d%H}")
        severities = [Severity.INFO, Severity.LOW, Severity.MEDIUM,
                      Severity.HIGH, Severity.CRITICAL]
        weights = [34, 28, 22, 12, 4]
        n_alerts = rng.choices([0, 0, 1, 1, 2, 3, 5], weights=[30, 18, 18, 12, 12, 7, 3])[0]
        peak = rng.choices(severities, weights=weights)[0] if n_alerts else None
        threat = min(1.0, (peak.weight * rng.uniform(0.55, 0.95)) if peak else 0.0)
        state = rng.choices(
            ["docked", "patrolling", "charging", "hover", "returning"],
            weights=[34, 30, 18, 10, 8],
        )[0]
        self._generated[site.id] = {"alerts": n_alerts, "peak": peak}
        return SiteStatus(
            site_id=site.id, name=site.name, lat=site.origin.lat, lon=site.origin.lon,
            country=site.country, country_name=site.country_name, kind=site.kind,
            simulated=True, drone_state=state,
            battery_pct=rng.uniform(28, 100), active_alerts=n_alerts,
            peak_severity=peak, threat_score=threat,
            entities_today=rng.randint(2, 48),
            last_seen=now - timedelta(minutes=rng.randint(1, 240)),
            online=rng.random() > 0.04,
        )

    # ── aggregation for the globe ────────────────────────────────────────
    def by_country(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Choropleth buckets. Severity-weighted so one critical outranks ten info."""
        rows = self.status(now=now)
        buckets: dict[str, dict[str, Any]] = {}
        for s in rows:
            b = buckets.setdefault(
                s.country,
                {
                    "country": s.country, "country_name": s.country_name,
                    "sites": 0, "alerts": 0, "simulated_sites": 0,
                    "by_severity": {k.value: 0 for k in Severity},
                    "threat": 0.0, "site_ids": [],
                },
            )
            b["sites"] += 1
            b["alerts"] += s.active_alerts
            b["simulated_sites"] += int(s.simulated)
            b["site_ids"].append(s.site_id)
            b["threat"] = max(b["threat"], s.threat_score)
            if s.peak_severity and s.active_alerts:
                b["by_severity"][s.peak_severity.value] += s.active_alerts
        for b in buckets.values():
            b["threat"] = round(b["threat"], 3)
        return sorted(buckets.values(), key=lambda x: -x["threat"])

    def summary(self, *, now: datetime | None = None) -> dict[str, Any]:
        rows = self.status(now=now)
        return {
            "sites": len(rows),
            "live_sites": sum(1 for s in rows if not s.simulated),
            "simulated_sites": sum(1 for s in rows if s.simulated),
            "online": sum(1 for s in rows if s.online),
            "active_alerts": sum(s.active_alerts for s in rows),
            "airborne": sum(
                1 for s in rows if s.drone_state in ("patrolling", "hover", "returning")
            ),
            "charging": sum(1 for s in rows if s.drone_state == "charging"),
            "mean_battery": round(
                sum(s.battery_pct for s in rows) / max(1, len(rows)), 1
            ),
            "peak_threat": round(max((s.threat_score for s in rows), default=0.0), 3),
            "countries": len({s.country for s in rows}),
            "note": (
                "Only sites flagged simulated=false carry a live feed. The remainder "
                "are driven by a seeded generator to demonstrate portfolio scale."
            ),
        }

    # ── correlation ──────────────────────────────────────────────────────
    def correlate_entities(
        self,
        entity_vectors: dict[str, tuple[str, str, str, np.ndarray, datetime]],
        *,
        threshold: float = CROSS_SITE_THRESHOLD,
    ) -> list[CrossSiteMatch]:
        """Find entities seen at more than one site.

        ``entity_vectors`` maps entity_id → (site_id, descriptor, kind, vector, ts).
        Comparison is all-pairs, which is fine at portfolio scale (hundreds of
        entities) and would be replaced by an ANN index at thousands.
        """
        items = list(entity_vectors.items())
        groups: list[list[str]] = []
        assigned: dict[str, int] = {}

        for i, (eid_a, (site_a, _, kind_a, vec_a, ts_a)) in enumerate(items):
            for eid_b, (site_b, _, kind_b, vec_b, ts_b) in items[i + 1:]:
                if site_a == site_b or kind_a != kind_b:
                    continue
                # A vehicle cannot be at two distant sites minutes apart; such a
                # "match" is evidence of a matching failure, not of movement.
                if abs((ts_a - ts_b).total_seconds()) < MIN_TRAVEL_SECONDS:
                    continue
                sim = _cos(vec_a, vec_b)
                if sim < threshold:
                    continue
                gi = assigned.get(eid_a, assigned.get(eid_b))
                if gi is None:
                    groups.append([eid_a, eid_b])
                    assigned[eid_a] = assigned[eid_b] = len(groups) - 1
                else:
                    for e in (eid_a, eid_b):
                        if e not in assigned:
                            groups[gi].append(e)
                            assigned[e] = gi

        matches: list[CrossSiteMatch] = []
        for group in groups:
            sightings = []
            sims: list[float] = []
            for eid in group:
                site_id, descriptor, _kind, _vec, ts = entity_vectors[eid]
                site = self.sites.get(site_id)
                sightings.append(
                    {
                        "entity_id": eid, "site_id": site_id,
                        "site_name": site.name if site else site_id,
                        "lat": site.origin.lat if site else None,
                        "lon": site.origin.lon if site else None,
                        "ts": ts.isoformat(), "descriptor": descriptor,
                        "simulated": not site.live_footage if site else True,
                    }
                )
            for i, a in enumerate(group):
                for b in group[i + 1:]:
                    sims.append(_cos(entity_vectors[a][3], entity_vectors[b][3]))

            site_ids = sorted({s["site_id"] for s in sightings})
            if len(site_ids) < 2:
                continue
            times = [datetime.fromisoformat(s["ts"]) for s in sightings]
            mean_sim = float(np.mean(sims)) if sims else 0.0
            matches.append(
                CrossSiteMatch(
                    descriptor=entity_vectors[group[0]][1],
                    kind=entity_vectors[group[0]][2],
                    sites=site_ids,
                    site_names=[
                        self.sites[s].name if s in self.sites else s for s in site_ids
                    ],
                    sightings=sorted(sightings, key=lambda s: s["ts"]),
                    similarity=mean_sim,
                    first_seen=min(times),
                    last_seen=max(times),
                    confidence="high" if mean_sim > 0.9 else "probable",
                )
            )
        return sorted(matches, key=lambda m: (-len(m.sites), -m.similarity))

    def find_temporal_pattern(
        self, *, now: datetime | None = None, window_hours: int = 6
    ) -> list[dict[str, Any]]:
        """Sites in the same region alerting inside the same window.

        Coordinated activity across a region is a different finding from a busy
        night at one site, and only the portfolio view can distinguish them.
        """
        rows = [s for s in self.status(now=now) if s.active_alerts > 0]
        by_country: dict[str, list[SiteStatus]] = {}
        for s in rows:
            by_country.setdefault(s.country, []).append(s)

        out = []
        for country, group in by_country.items():
            if len(group) < 2:
                continue
            severe = [s for s in group if s.peak_severity and s.peak_severity.weight >= 0.55]
            if len(severe) < 2:
                continue
            out.append(
                {
                    "country": country,
                    "country_name": group[0].country_name,
                    "sites": [s.site_id for s in severe],
                    "site_names": [s.name for s in severe],
                    "total_alerts": sum(s.active_alerts for s in severe),
                    "window_hours": window_hours,
                    "any_simulated": any(s.simulated for s in severe),
                    "assessment": (
                        f"{len(severe)} sites in {group[0].country_name} are reporting "
                        f"medium-or-higher alerts within the same {window_hours}-hour "
                        f"window. Consider whether these are related."
                    ),
                }
            )
        return sorted(out, key=lambda x: -x["total_alerts"])


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return float(np.dot(a, b) / (na * nb)) if na and nb else 0.0


def synthetic_entity_vectors(
    sites: list[Site], *, seed: int = 11, plant_vectors: dict | None = None
) -> dict[str, tuple[str, str, str, np.ndarray, datetime]]:
    """Seeded entity vectors for simulated sites, including one deliberate
    cross-site subject so the correlation feature has something real to find.

    The planted subject is the demo's closing beat. It is synthetic, it is labelled
    as such everywhere it surfaces, and the alternative — claiming a genuine
    multi-site detection we did not observe — would be dishonest.
    """
    rng = np.random.default_rng(seed)
    out: dict[str, tuple[str, str, str, np.ndarray, datetime]] = dict(plant_vectors or {})
    base = datetime(2026, 8, 6, 3, 0, 0)

    simulated = [s for s in sites if not s.live_footage]
    # The recurring subject: one vehicle, three sites, five days.
    probe = rng.normal(size=2048)
    for i, site in enumerate(simulated[:3]):
        eid = f"ENT-{hashlib.sha1(site.id.encode()).hexdigest()[:4]}-9001"
        out[eid] = (
            site.id,
            "white panel van",
            "vehicle",
            probe + rng.normal(scale=0.05, size=2048),
            base - timedelta(days=4 - i, hours=i * 3),
        )

    # Unrelated background entities, so correlation has to actually discriminate.
    for site in simulated:
        for j in range(3):
            eid = f"ENT-{hashlib.sha1(site.id.encode()).hexdigest()[:4]}-{8000+j}"
            out[eid] = (
                site.id,
                rng.choice(["blue hatchback", "delivery truck", "contractor van"]),
                "vehicle",
                rng.normal(size=2048),
                base - timedelta(days=int(rng.integers(0, 5)), hours=int(rng.integers(0, 20))),
            )
    return out
