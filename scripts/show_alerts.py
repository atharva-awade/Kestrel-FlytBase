"""Print alerts with their dispatch data and mission plans.

Exists to make one thing checkable at a glance: that an alert carries everything
needed to actually send an aircraft to it — coordinates, accuracy, bearing,
distance, ETA, altitude and a geofence verdict — rather than only a description.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DB = Path("data/kestrel.db")


def main() -> None:
    if not DB.exists():
        print("no database — run: uv run kestrel ingest")
        return
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

    alerts = list(c.execute("SELECT * FROM alerts ORDER BY ts"))
    print(f"\n{len(alerts)} alert(s)\n" + "=" * 78)

    for r in alerts:
        ev = json.loads(r["evidence_json"] or "[]")
        loc = next(
            (e["detail"] for e in ev
             if e.get("kind") == "telemetry" and "Dispatch" in (e.get("caption") or "")),
            None,
        )
        print(f"\n[{r['severity'].upper()}] {r['title']}")
        print(f"  raised     {r['ts'][11:19]}   confidence {r['confidence']:.2f}"
              f"   zone {r['zone_id']}")
        if loc and loc.get("lat") is not None:
            print(f"  TARGET     {loc['lat']:.6f}, {loc['lon']:.6f}"
                  f"   ±{loc['accuracy_m']} m  ({loc['source']}, conf {loc['confidence']:.2f})")
            print(f"  DISPATCH   {loc['distance_from_dock_m']:.0f} m from dock"
                  f" · bearing {loc['bearing_from_dock_deg']:.0f}°"
                  f" · ETA {loc['eta_seconds']:.0f}s")
            print(f"  FLY AT     {loc['recommended_altitude_m']} m"
                  f"   geofence_ok={loc['within_geofence']}")
            print(f"  DOCK       {loc['dock_lat']:.6f}, {loc['dock_lon']:.6f}")
            if loc.get("drone_lat"):
                print(f"  DRONE WAS  {loc['drone_lat']:.6f}, {loc['drone_lon']:.6f}"
                      f" @ {loc['drone_alt_m']:.0f} m")
        else:
            print("  (no navigable position)")

        ev_kinds: dict[str, int] = {}
        for e in ev:
            ev_kinds[e["kind"]] = ev_kinds.get(e["kind"], 0) + 1
        print(f"  evidence   {ev_kinds}")

    missions = list(c.execute("SELECT * FROM missions ORDER BY created_ts"))
    print(f"\n\n{len(missions)} mission(s)\n" + "=" * 78)
    for m in missions:
        feas = json.loads(m["feasibility_json"])
        print(f"\n{m['id']}  [{m['status']}]  feasible={feas['feasible']}")
        print("  " + (m["rationale"] or "").replace("\n", "\n  "))
        print(f"  battery    needs {feas['battery_required_pct']}% of "
              f"{feas['battery_available_pct']}% available")
        print(f"  route      {feas['distance_m']:.0f} m · {feas['duration_s']:.0f}s"
              f" · geofence={feas['within_geofence']} wind_ok={feas['wind_ok']}"
              f" daylight={feas['daylight']}")
        if feas["blockers"]:
            print(f"  BLOCKERS   {feas['blockers']}")
        if feas["warnings"]:
            print(f"  warnings   {feas['warnings']}")
        print("  flight plan:")
        for s in json.loads(m["steps_json"]):
            t = s.get("target")
            coord = f"  → {t['lat']:.6f},{t['lon']:.6f}" if t else ""
            extra = ""
            if s.get("radius_m") and s["kind"] == "orbit":
                extra = f" r={s['radius_m']}m"
            if s.get("duration_s"):
                extra += f" {s['duration_s']:.0f}s"
            print(f"    {s['kind'].upper():<8} alt={s['altitude_m']:>4.0f}m{extra}{coord}")
            print(f"             {s['note']}")


if __name__ == "__main__":
    main()
