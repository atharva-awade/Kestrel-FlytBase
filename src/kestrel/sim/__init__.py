"""Simulation: sites, telemetry and scripted scenarios.

What is simulated and what is not, stated plainly because the distinction matters
for how the results should be read:

*   **Real** — the video, the detections, the tracks, the embeddings, the captions.
*   **Simulated** — the telemetry (there is no aircraft), the site geometry, the
    scripted scenarios, and every fleet site except ``plant-01``.
"""

from kestrel.sim.scenarios import ALL as SCENARIOS
from kestrel.sim.scenarios import Scenario, by_id, write_scenarios
from kestrel.sim.sites import (
    build_fleet,
    build_plant_01,
    load_fleet,
    load_site,
    offset,
    write_sites,
)
from kestrel.sim.telemetry import PatrolSimulator, illuminance_at

__all__ = [
    "SCENARIOS",
    "PatrolSimulator",
    "Scenario",
    "build_fleet",
    "build_plant_01",
    "by_id",
    "illuminance_at",
    "load_fleet",
    "load_site",
    "offset",
    "write_scenarios",
    "write_sites",
]
