"""KESTREL command line.

    kestrel doctor                    check the environment and model reachability
    kestrel init                      write site and scenario data
    kestrel ingest --clip worker-zone run a monitoring session over footage
    kestrel scenario loiter-midnight  run a scripted scenario
    kestrel search "all truck events" query the index
    kestrel rule "alert me if ..."    compile a rule from English and backtest it
    kestrel serve                     start the API
    kestrel stats                     what is in the database
"""

from __future__ import annotations

import asyncio
import json
import warnings
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

warnings.filterwarnings("ignore")

app = typer.Typer(add_completion=False, help="KESTREL: autonomous drone security analyst")
console = Console()


def _site(site_id: str):
    from kestrel.config import get_settings
    from kestrel.sim.sites import load_site

    return load_site(site_id, get_settings().sites_dir)


# ═══════════════════════════════════════════════════════════════════════════════
@app.command()
def doctor() -> None:
    """Check the environment: models, GPU, database, footage."""
    from kestrel.clients.models import get_client
    from kestrel.config import get_settings
    from kestrel.storage.db import get_db

    s = get_settings()
    console.print(Panel.fit("[bold]KESTREL environment check[/bold]", border_style="cyan"))

    t = Table(show_header=True, header_style="bold")
    t.add_column("Component")
    t.add_column("Status")
    t.add_column("Detail")

    t.add_row("mode", s.effective_mode.value,
              "requested: " + s.mode.value + ("  (no key → replay)" if s.effective_mode != s.mode else ""))
    t.add_row("NVIDIA key", "set" if s.nvidia_api_key else "missing", s.nvidia_base_url)
    t.add_row("Groq key", "set" if s.groq_api_key else "missing", s.groq_base_url)

    try:
        from kestrel.perception.detect import get_detector

        info = get_detector(s).info
        t.add_row(
            "detector",
            f"{info['backend']} ({info['device']})",
            ("DEGRADED: " + (info["fallback_reason"] or "")[:70]) if info["degraded"]
            else ("open-vocabulary" if info["open_vocabulary"] else "closed-set"),
        )
    except Exception as e:
        t.add_row("detector", "error", f"{type(e).__name__}: {e}"[:70])

    try:
        import torch

        t.add_row("CUDA", "yes" if torch.cuda.is_available() else "no",
                  torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU only")
    except Exception:
        t.add_row("CUDA", "torch not installed", "install with: uv sync --extra local-gpu")

    db = get_db()
    st = db.stats
    t.add_row("database", st["vector_index"], f"{st['frames_total']} frames, {st['size_bytes']/1e6:.1f} MB")

    client = get_client()
    t.add_row("cassettes", str(client.cassettes.count), str(s.cassette_dir))

    footage = sorted(Path(s.footage_dir).glob("*.mp4"))
    t.add_row("footage", f"{len(footage)} clips",
              ", ".join(p.stem for p in footage[:4]) or "run: kestrel fetch-footage")

    console.print(t)
    if not footage:
        console.print("\n[yellow]No footage. Run:[/yellow] uv run python scripts/fetch_footage.py")


@app.command()
def init() -> None:
    """Write site definitions and scenario files."""
    from kestrel.config import get_settings
    from kestrel.sim import write_scenarios, write_sites

    s = get_settings()
    sites = write_sites(s.sites_dir)
    scen = write_scenarios(Path("data/scenarios"))
    console.print(f"[green]wrote[/green] {len(sites)} site files → {s.sites_dir}")
    console.print(f"[green]wrote[/green] {len(scen)} scenario files → data/scenarios")


@app.command()
def ingest(
    clip: str = typer.Option("worker-zone", help="footage stem in data/footage"),
    site: str = typer.Option("plant-01"),
    frames: int = typer.Option(40, help="maximum frames to analyse"),
    fps: float = typer.Option(2.0, help="analysis sample rate"),
    start: str = typer.Option("2026-08-06T02:10:00", help="site clock start"),
    clock_scale: float = typer.Option(20.0, help="site clock speed vs clip time"),
    zone: str = typer.Option("substation", help="zone the drone hovers over"),
    no_vlm: bool = typer.Option(False, "--no-vlm"),
) -> None:
    """Run a monitoring session over real footage."""
    from kestrel.ingest.sources import VideoFileSource
    from kestrel.session import Session
    from kestrel.sim.telemetry import PatrolSimulator

    st = _site(site)
    t0 = datetime.fromisoformat(start)
    z = st.zone_by_id(zone)
    tel = PatrolSimulator(st, t0)
    if z is not None:
        tel.waypoints = [(z.centroid, zone, 7200.0)]

    path = Path("data/footage") / f"{clip}.mp4"
    if not path.exists():
        console.print(f"[red]no such clip:[/red] {path}")
        console.print("run: uv run python scripts/fetch_footage.py")
        raise typer.Exit(1)

    src = VideoFileSource(path, st, start_clock=t0, sample_fps=fps,
                          clock_scale=clock_scale, max_frames=frames, telemetry=tel)
    session = Session(st, enable_vlm=not no_vlm)

    console.print(Panel.fit(
        f"[bold]{st.name}[/bold]\nclip {clip}.mp4 · {frames} frames @ {fps} fps · "
        f"site clock from {t0:%d %b %H:%M} (x{clock_scale:g}) · over {zone}",
        border_style="cyan",
    ))

    stats = asyncio.run(session.run(src))
    _print_summary(session.summary())
    if stats.errors:
        console.print(f"[yellow]{len(stats.errors)} frame error(s)[/yellow]: {stats.errors[:2]}")


@app.command()
def scenario(
    name: str = typer.Argument(..., help="scenario id, e.g. loiter-midnight"),
    site: str = typer.Option("plant-01"),
) -> None:
    """Run a scripted scenario — the assignment's literal frame-description mode."""
    from kestrel.ingest.sources import ScriptedSource
    from kestrel.session import Session
    from kestrel.sim.scenarios import ALL, by_id

    try:
        sc = by_id(name)
    except KeyError:
        console.print(f"[red]unknown scenario[/red]. Available: {[s.id for s in ALL]}")
        raise typer.Exit(1) from None

    st = _site(site)
    start = datetime.fromisoformat(sc.frames[0]["at"])
    src = ScriptedSource(sc.frames, st, start_clock=start)
    session = Session(st)

    console.print(Panel.fit(
        f"[bold]{sc.title}[/bold]\n{sc.description}\n\n"
        f"[dim]expects: {sc.expect_alerts or 'no alerts'}[/dim]",
        border_style="cyan",
    ))
    asyncio.run(session.run(src))

    fired = {a.rule_id for a in session.alerts}
    expected = set(sc.expect_alerts)
    forbidden = set(sc.expect_no_alerts) & fired

    for a in session.alerts:
        console.print(f"  [red]ALERT[/red] {a.ts:%H:%M:%S} [{a.severity.value}] {a.title} "
                      f"(confidence {a.confidence:.2f})")
    if not session.alerts:
        console.print("  [green]no alerts raised[/green]")

    console.print()
    if expected and not expected & fired:
        console.print(f"[red]MISS[/red] expected {sorted(expected)}, fired {sorted(fired)}")
    elif expected:
        console.print(f"[green]HIT[/green] fired expected rule(s): {sorted(expected & fired)}")
    if forbidden:
        console.print(f"[red]FALSE POSITIVE[/red] should not have fired: {sorted(forbidden)}")
    elif sc.expect_no_alerts:
        console.print(f"[green]correctly silent[/green] on {sc.expect_no_alerts}")

    _print_summary(session.summary())


@app.command()
def search(
    query: str = typer.Argument(...),
    site: str = typer.Option("plant-01"),
    limit: int = typer.Option(12),
) -> None:
    """Hybrid search over the frame index."""
    from kestrel.clients.models import get_client
    from kestrel.retrieval.search import HybridSearch
    from kestrel.storage.db import get_db

    st = _site(site)
    hs = HybridSearch(get_db(), st, get_client())
    res = asyncio.run(hs.search(query, limit=limit))

    console.print(Panel.fit(f'[bold]"{query}"[/bold]', border_style="cyan"))
    console.print(f"[dim]intent: {res.plan.intent} · {res.plan.reasoning}[/dim]")
    for step in res.plan.describe():
        console.print(f"  · {step}")
    console.print(f"[dim]retrievers: {res.counts} · fused in {res.took_ms:.0f}ms[/dim]\n")

    if not res.hits:
        console.print("[yellow]no results. Has a session been ingested?[/yellow]")
        return
    t = Table(show_header=True, header_style="bold")
    for col in ("when", "zone", "labels", "via"):
        t.add_column(col)
    t.add_column("caption", max_width=58)
    for h in res.hits:
        t.add_row(f"{h.ts:%d %b %H:%M:%S}", h.zone_id or "-",
                  ", ".join(h.labels[:3]) or "-", "+".join(h.sources), h.caption[:58])
    console.print(t)


@app.command()
def rule(
    text: str = typer.Argument(..., help="the rule, in plain English"),
    site: str = typer.Option("plant-01"),
) -> None:
    """Compile English into a rule, then backtest it against indexed history."""
    from kestrel.clients.models import get_client
    from kestrel.rules.compiler import RuleCompiler, observations_from_db
    from kestrel.storage.db import get_db

    st = _site(site)
    compiler = RuleCompiler(st, get_client())

    console.print(Panel.fit(f'[bold]"{text}"[/bold]', border_style="cyan"))
    try:
        compiled = asyncio.run(compiler.compile(text))
    except Exception as e:
        console.print(f"[red]compilation failed:[/red] {e}")
        raise typer.Exit(1) from None

    console.print(f"\n[green]compiled[/green] → [bold]{compiled.id}[/bold] "
                  f"({compiled.severity.value})")
    console.print(f"[dim]{compiled.description}[/dim]\n")
    for line in compiled.explain():
        console.print(f"  · {line}")
    if compiled.visual_predicate:
        console.print(f'\n  visual predicate: "{compiled.visual_predicate}"')

    obs = observations_from_db(get_db(), st.id)
    report = compiler.backtest(compiled, obs)
    console.print(Panel.fit(
        f"[bold]Backtest[/bold]\n"
        f"replayed {report.frames_replayed} frames / {report.observations} observations "
        f"over {report.days_covered} day(s)\n"
        f"would have fired [bold]{report.fire_count}[/bold] time(s)\n\n"
        f"{report.verdict}",
        border_style="yellow" if report.fire_count else "green",
    ))
    for h in report.hits[:6]:
        console.print(f"  [yellow]would fire[/yellow] {h.ts:%d %b %H:%M:%S} "
                      f"{h.label} in {h.zone_id or '-'}")
    console.print("\n[dim]YAML:[/dim]")
    console.print(compiled.to_yaml())


@app.command()
def stats() -> None:
    """What is currently in the database."""
    from kestrel.storage.db import get_db
    from kestrel.storage.ledger import Ledger

    db = get_db()
    t = Table(show_header=True, header_style="bold")
    t.add_column("Metric")
    t.add_column("Value", justify="right")
    for k, v in db.stats.items():
        t.add_row(k, str(v))
    console.print(t)

    v = Ledger(db).verify()
    console.print(Panel.fit(
        f"[bold]Audit ledger[/bold]\n{v['entries']} entries · "
        f"{'[green]chain verified[/green]' if v['valid'] else '[red]CHAIN BROKEN[/red]'}\n"
        f"{v.get('note') or v.get('reason', '')}",
        border_style="green" if v["valid"] else "red",
    ))


@app.command()
def fleet() -> None:
    """Portfolio status across all sites."""
    from kestrel.config import get_settings
    from kestrel.fleet.fleet import FleetManager
    from kestrel.sim.sites import load_fleet
    from kestrel.storage.db import get_db

    fm = FleetManager(load_fleet(get_settings().sites_dir), get_db())
    summary = fm.summary()
    console.print(Panel.fit(
        f"[bold]Fleet[/bold]  {summary['sites']} sites · {summary['live_sites']} live · "
        f"{summary['simulated_sites']} simulated · {summary['countries']} countries\n"
        f"{summary['active_alerts']} active alerts · {summary['airborne']} airborne · "
        f"mean battery {summary['mean_battery']}%\n\n[dim]{summary['note']}[/dim]",
        border_style="cyan",
    ))
    t = Table(show_header=True, header_style="bold")
    for c in ("site", "country", "state", "batt", "alerts", "peak", "threat", "feed"):
        t.add_column(c)
    for s in fm.status()[:20]:
        t.add_row(s.name[:26], s.country, s.drone_state, f"{s.battery_pct:.0f}%",
                  str(s.active_alerts), s.peak_severity.value if s.peak_severity else "-",
                  f"{s.threat_score:.2f}",
                  "[yellow]SIMULATED[/yellow]" if s.simulated else "[green]live[/green]")
    console.print(t)


@app.command()
def serve(
    host: str = typer.Option(None), port: int = typer.Option(None), reload: bool = typer.Option(False)
) -> None:
    """Start the API."""
    import uvicorn

    from kestrel.config import get_settings

    s = get_settings()
    uvicorn.run("kestrel.api.main:app", host=host or s.api_host,
                port=port or s.api_port, reload=reload)


# ═══════════════════════════════════════════════════════════════════════════════
def _print_summary(summary: dict) -> None:
    f = summary["frames"]
    a = summary["alerts"]
    console.print(Panel.fit(
        f"[bold]Session[/bold]\n"
        f"frames      {f['analysed']}/{f['seen']} analysed "
        f"([green]{f['gate_efficiency']*100:.0f}%[/green] gated)\n"
        f"detections  {summary['detections']} · entities {summary['entities']['entities']}\n"
        f"alerts      [red]{a['raised']} raised[/red] · {a['suppressed']} suppressed\n"
        f"missions    {summary['missions']['proposed']} proposed "
        f"({summary['missions']['feasible']} feasible)\n"
        f"memory      {summary['memory']['compression_ratio']}x compression "
        f"({summary['memory']['tokens_raw']} → {summary['memory']['tokens_compressed']} tokens)\n"
        f"cost        ${summary['meter']['cost']['modelled_usd']:.5f} modelled "
        f"({summary['wall_seconds']:.0f}s wall)",
        border_style="cyan",
    ))


@app.command("export-summary")
def export_summary(out: str = typer.Option("data/eval/last_session.json")) -> None:
    """Write the last session summary as JSON (consumed by the LaTeX report)."""
    from kestrel.storage.db import get_db

    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(get_db().stats, indent=2), encoding="utf-8")
    console.print(f"[green]wrote[/green] {out}")


if __name__ == "__main__":
    app()
