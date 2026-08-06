"""Trace exactly what a scenario produced and why each rule did or did not fire.

Rules fail silently — a label the engine does not recognise, a zone that did not
resolve, a dwell clock that reset — and none of those surface as errors. This
prints the observations and the per-clause verdicts so the cause is visible.

    uv run python scripts/debug_scenario.py tailgating
"""

from __future__ import annotations

import asyncio
import sys
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")

from kestrel.config import get_settings
from kestrel.ingest.sources import ScriptedSource
from kestrel.session import Session
from kestrel.sim.scenarios import by_id
from kestrel.sim.sites import load_site
from kestrel.storage.db import Database


async def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "tailgating"
    focus = sys.argv[2] if len(sys.argv) > 2 else None

    sc = by_id(name)
    site = load_site("plant-01", get_settings().sites_dir)
    db = Database(Path(f"data/eval/_dbg_{name}.db"))
    start = datetime.fromisoformat(sc.frames[0]["at"])
    src = ScriptedSource(sc.frames, site, start_clock=start)
    session = Session(site, db=db, save_frames=False, enable_embeddings=False)

    # Capture every rule evaluation, not just the ones that fired.
    trace: list[tuple] = []
    original = session.engine.evaluate

    def spy(obs, **kw):
        results = original(obs, **kw)
        for r in results:
            if focus is None or r.rule.id == focus:
                trace.append((obs, r))
        return results

    session.engine.evaluate = spy  # type: ignore[method-assign]

    await session.run(src)

    print(f"\n{sc.title}\n{'=' * 96}")
    print("\nOBSERVATIONS")
    seen: set = set()
    for obs, _ in trace:
        key = (obs.frame_id, obs.label, obs.entity_id)
        if key in seen:
            continue
        seen.add(key)
        print(f"  {obs.ts:%H:%M:%S}  label={obs.label:<20} zone={obs.zone_id!s:<16} "
              f"entity={str(obs.entity_id)[-10:]:<12} conf={obs.confidence:.2f}")

    print("\nRULE EVALUATIONS (failures only, one line per failed clause)")
    shown = 0
    for obs, r in trace:
        if r.fired:
            print(f"  [FIRED] {r.rule.id:<22} {obs.ts:%H:%M:%S} {obs.label}")
            continue
        failed = r.failed_clauses
        if not failed or shown > 60:
            continue
        # Only print rules that got at least halfway — the rest are irrelevant.
        passed = len(r.clauses) - len(failed)
        if passed < max(1, len(r.clauses) - 2):
            continue
        shown += 1
        print(f"  [no]    {r.rule.id:<22} {obs.ts:%H:%M:%S} {obs.label:<18} "
              f"({passed}/{len(r.clauses)} clauses) → {failed[0].kind}: {failed[0].detail[:70]}")

    print(f"\nALERTS RAISED: {[a.rule_id for a in session.alerts] or 'none'}")
    print(f"EXPECTED:      {sc.expect_alerts or 'none'}")
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
