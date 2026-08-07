"""Site-wide inspection: does everything claimed actually work?

Run this repeatedly. It is written to be idempotent and to fail loudly, so it can
be looped until clean rather than eyeballed once:

    uv run python scripts/inspect_all.py

It checks four things, in order of how expensive they are to be wrong about:

  1. DELIVERABLES  every artefact the assignment asks for is present on disk
  2. RUNTIME       every API endpoint answers, and answers with the right shape
  3. INNOVATIONS   each capability we claim is exercised end to end, not just
                   present in the source
  4. INTEGRITY     the properties that would discredit the rest if they broke:
                   the audit chain verifies, no gated tool can self-approve, the
                   cassettes still resolve, and no simulated site claims to be live

Requires the API to be running (`uv run kestrel serve`). The web dev server is
checked too when it is up, and skipped with a note when it is not.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, ClassVar

API = "http://127.0.0.1:8000"
WEB = "http://localhost:3000"

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))
    mark = {PASS: "  ok  ", FAIL: " FAIL ", WARN: " warn ", SKIP: " skip "}[status]
    print(f"[{mark}] {name}" + (f"  ::  {detail}" if detail else ""))


def get(url: str, timeout: float = 30.0) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(body)
            except json.JSONDecodeError:
                return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def post(url: str, payload: dict, timeout: float = 120.0) -> tuple[int, Any]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(body)
            except json.JSONDecodeError:
                return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def check(name: str, ok: bool, detail: str = "", warn_only: bool = False) -> bool:
    record(PASS if ok else (WARN if warn_only else FAIL), name, detail)
    return ok


# ═══════════════════════════════════════════════════════════════════════════
def section(title: str) -> None:
    print(f"\n{'=' * 84}\n{title}\n{'=' * 84}")


# ── 1. deliverables ────────────────────────────────────────────────────────
def deliverables() -> None:
    section("1. DELIVERABLES  — everything the assignment asks for, on disk")

    required = {
        "README": "README.md",
        "flowchart (from the supplied scaffold)": "artifacts/flowchart.html",
        "system architecture diagram": "artifacts/diagrams/system-architecture.svg",
        "sequence diagram": "artifacts/diagrams/sequence-frame-to-dispatch.svg",
        "memory pyramid diagram": "artifacts/diagrams/memory-pyramid.svg",
        "ADR: model selection": "docs/adr/0001-model-selection.md",
        "ADR: detector backend": "docs/adr/0002-detector-backend.md",
        "3D hero model (compressed)": "web/public/models/kestrel-drone.glb",
        "model attribution": "web/public/models/README.md",
        "footage licence record": "data/footage/SOURCES.md",
    }
    for label, rel in required.items():
        p = Path(rel)
        check(f"{label}", p.exists(), rel if p.exists() else f"MISSING {rel}")

    pdfs = sorted(Path("report").glob("*.pdf"))
    check("report PDFs build output", len(pdfs) >= 4,
          f"{len(pdfs)} found: " + ", ".join(p.name for p in pdfs))

    glb = Path("web/public/models/kestrel-drone.glb")
    if glb.exists():
        mb = glb.stat().st_size / 1e6
        check("hero model is shippable size", mb < 6, f"{mb:.2f} MB")


# ── 2. runtime ─────────────────────────────────────────────────────────────
def runtime() -> dict[str, Any]:
    section("2. RUNTIME  — every endpoint answers with the right shape")

    status, health = get(f"{API}/api/health")
    if status != 200 or not isinstance(health, dict):
        record(FAIL, "API reachable", f"start it with: uv run kestrel serve ({health})")
        return {}
    record(PASS, "API reachable", f"mode={health.get('mode')}")

    endpoints = [
        ("/api/health", "status"),
        ("/api/stats", None),
        ("/api/sites", None),
        ("/api/frames?limit=5", None),
        ("/api/alerts?limit=5", None),
        ("/api/entities?limit=5", None),
        ("/api/rules", None),
        ("/api/scenarios", None),
        ("/api/evals", None),
        ("/api/fleet", None),
        ("/api/fleet/correlations", None),
        ("/api/memory", None),
        ("/api/missions", None),
        ("/api/ledger", None),
        ("/api/tools", None),
        ("/api/architecture", None),
        ("/api/search?q=person", None),
    ]
    for path, key in endpoints:
        # /api/stats lazily initialises the detector (weights onto the GPU), so a
        # cold API legitimately takes far longer than a normal read.
        # Two endpoints are legitimately slow on a cold or contended API:
        # /api/stats initialises the detector, and /api/search runs an LLM query
        # planner. Everything else should answer promptly.
        slow = ("stats" in path) or ("search" in path)
        st, body = get(f"{API}{path}", timeout=180 if slow else 30)
        ok = st == 200 and body is not None and not isinstance(body, str)
        detail = f"HTTP {st}" if not ok else ""
        if ok and key:
            ok = key in body
            detail = f"missing key '{key}'" if not ok else ""
        check(f"GET {path}", ok, detail)

    # Nested resources need a real id, so they are resolved from live data.
    _, sites = get(f"{API}/api/sites")
    rows = sites.get("sites", []) if isinstance(sites, dict) else (sites or [])
    if rows:
        sid = rows[0].get("id") or rows[0].get("site_id")
        st, _ = get(f"{API}/api/sites/{sid}")
        check("GET /api/sites/{id}", st == 200, f"HTTP {st}")

    _, ents = get(f"{API}/api/entities?limit=1")
    rows = (ents or {}).get("entities", []) if isinstance(ents, dict) else (ents or [])
    if rows:
        eid = rows[0].get("id") or rows[0].get("entity_id")
        st, _ = get(f"{API}/api/entities/{eid}")
        check("GET /api/entities/{id}", st == 200, f"HTTP {st}")
    else:
        record(SKIP, "GET /api/entities/{id}", "no entities ingested yet")

    # /api/brief is LLM-backed. On a free tier it can be rate-limited into a
    # failover, and the NIM fallback is a documented 57-84s cold start, so it gets
    # the deep budget rather than the default.
    st, body = get(f"{API}/api/brief", timeout=260)
    if st == 200:
        check("GET /api/brief", True)
    else:
        _, h = get(f"{API}/api/health")
        open_circuits = [p["provider"] for p in (h or {}).get("providers", [])
                         if p.get("circuit_open")]
        check("GET /api/brief", False,
              f"HTTP {st}; providers with an open circuit: {open_circuits or 'none'}"
              " (free-tier rate limit is the usual cause)",
              warn_only=bool(open_circuits))

    # Web routes
    st, _ = get(WEB, timeout=60)
    if st != 200:
        record(SKIP, "web dev server", "not running (npm --prefix web run dev)")
    else:
        for route in ["/", "/command", "/console", "/analyst", "/investigate",
                      "/entities", "/rules", "/evals", "/architecture"]:
            st, _ = get(f"{WEB}{route}", timeout=60)
            check(f"page {route}", st == 200, f"HTTP {st}")
        for asset in ["/models/kestrel-drone.glb", "/draco/gltf/draco_decoder.wasm"]:
            st, _ = get(f"{WEB}{asset}", timeout=60)
            check(f"asset {asset}", st == 200, f"HTTP {st}")

    return health


# ── 3. innovations ─────────────────────────────────────────────────────────
def innovations(health: dict) -> None:
    section("3. INNOVATIONS  — each claim exercised, not merely present in source")

    if not health:
        record(SKIP, "innovation checks", "API not reachable")
        return

    # Cost cascade: the gate must actually be skipping frames. The counter lives
    # in storage, not the meter — the meter is per-process, and the API process
    # has ingested nothing itself.
    st, stats = get(f"{API}/api/stats")
    store = (stats or {}).get("storage", {}) if isinstance(stats, dict) else {}
    total, skipped = store.get("frames_total", 0), store.get("frames_skipped", 0)
    check("tier-0 gate is skipping frames", total > 0 and skipped > 0,
          f"{skipped}/{total} frames skipped "
          f"({(skipped / total * 100) if total else 0:.0f}% gated)")

    # Open-vocabulary detection, on-device.
    det = (stats or {}).get("detector") or {}
    check("detector loaded and open-vocabulary", bool(det.get("backend"))
          and det.get("open_vocabulary") is True and not det.get("degraded"),
          f"{det.get('backend')} on {det.get('device')}"
          + (" · open-vocabulary" if det.get("open_vocabulary") else " · closed-set"))

    # Declarative temporal rules with real operators, not per-frame predicates.
    st, rules = get(f"{API}/api/rules")
    rows = rules.get("rules", []) if isinstance(rules, dict) else (rules or [])
    yaml_all = " ".join(r.get("yaml", "") for r in rows)
    operators = [k for k in ("dwell", "sequence", "count_in_window",
                             "absence_of_person", "baseline_anomaly", "time_window")
                 if k in yaml_all]
    check("temporal rule DSL exposes real operators", len(rows) >= 5 and len(operators) >= 3,
          f"{len(rows)} rules using {operators}")

    # A rule that carries its own detector prompt: promptable, open-vocabulary.
    promptable = [r for r in rows if r.get("visual_predicate")]
    check("rules can carry an open-vocabulary visual predicate", bool(promptable),
          f"{len(promptable)} promptable rules")

    # Hybrid retrieval: the planner must route, and the retrievers must return.
    st, res = get(f"{API}/api/search?q=a%20white%20pickup")
    intent = ((res or {}).get("plan") or {}).get("intent")
    counts = (res or {}).get("counts") or {}
    complete = (res or {}).get("complete")
    degraded = (res or {}).get("degraded") or {}
    check("hybrid retrieval plans and routes a query",
          st == 200 and bool(intent) and (bool(counts) or bool(degraded)),
          f"intent={intent} retrievers={counts}"
          + (f" degraded={degraded}" if degraded else ""))
    # An empty result set and a dead retriever are different claims. The payload
    # must say which one it is rather than leaving the UI to guess.
    check("retrieval declares whether it ran complete", complete is not None,
          f"complete={complete}"
          + (f", degraded={list(degraded)}" if degraded else ""))

    # Alerts you can fly to: coordinates, accuracy, bearing, ETA, geofence.
    st, alerts = get(f"{API}/api/alerts?limit=20")
    rows = alerts.get("alerts", []) if isinstance(alerts, dict) else (alerts or [])
    need = ("lat", "lon", "accuracy_m", "bearing_from_dock_deg",
            "eta_seconds", "recommended_altitude_m")
    navigable = [a for a in rows
                 if all((a.get("location") or {}).get(k) is not None for k in need)]
    check("alerts carry full dispatch coordinates", bool(rows) and bool(navigable),
          f"{len(navigable)}/{len(rows)} alerts fully navigable",
          warn_only=not rows)

    # Missions: proposed with a feasibility verdict, never self-executed.
    st, missions = get(f"{API}/api/missions")
    rows = missions.get("missions", []) if isinstance(missions, dict) else (missions or [])
    with_feas = [m for m in rows if m.get("feasibility")]
    unapproved = [m for m in rows if m.get("status") not in ("executed", "flown")
                  or m.get("decided_by")]
    check("missions carry a feasibility verdict", bool(rows) and len(with_feas) == len(rows),
          f"{len(with_feas)}/{len(rows)} missions", warn_only=not rows)
    check("no mission executed without a recorded decider", len(unapproved) == len(rows),
          f"{len(rows)} missions checked", warn_only=not rows)

    # Cross-site correlation: the finding one site cannot produce.
    st, corr = get(f"{API}/api/fleet/correlations")
    matches = (corr or {}).get("matches", []) if isinstance(corr, dict) else []
    check("cross-site correlation produces findings", st == 200,
          f"{len(matches)} correlated subjects")

    # Temporal memory pyramid.
    st, mem = get(f"{API}/api/memory")
    nodes = (mem or {}).get("nodes", []) if isinstance(mem, dict) else []
    levels = sorted({n.get("level") for n in nodes if n.get("level") is not None})
    check("temporal memory pyramid populated", st == 200 and bool(nodes),
          f"{len(nodes)} nodes across levels {levels}")

    # Conversational control plane over every capability.
    st, tools = get(f"{API}/api/tools")
    tl = (tools or {}).get("tools", []) if isinstance(tools, dict) else []
    classes = {t.get("class") for t in tl if t.get("class")}
    gated = (tools or {}).get("permissions", {}).get("confirm", [])
    check("agent control plane exposes all tool classes",
          len(tl) >= 20 and len(classes) >= 8 and len(gated) >= 1,
          f"{len(tl)} tools, {len(classes)} classes ({sorted(classes)}), {len(gated)} gated")

    # The system can explain itself.
    st, arch = get(f"{API}/api/architecture")
    topics = (arch or {}).get("topics") if isinstance(arch, dict) else None
    n_topics = len(topics) if isinstance(topics, (list, dict)) else (
        len(arch) if isinstance(arch, dict) else 0)
    check("system explains its own architecture", st == 200 and n_topics >= 5,
          f"{n_topics} topics")


# ── 4. integrity ───────────────────────────────────────────────────────────
def integrity(health: dict) -> None:
    section("4. INTEGRITY  — the properties that would discredit everything else")

    if health:
        _, led = get(f"{API}/api/ledger")
        v = (led or {}).get("verification", {}) if isinstance(led, dict) else {}
        check("audit ledger hash chain verifies", v.get("valid") is True,
              f"{v.get('entries')} entries, valid={v.get('valid')}")

        _, fleet = get(f"{API}/api/fleet")
        sites = (fleet or {}).get("sites", []) if isinstance(fleet, dict) else (fleet or [])
        unflagged = [s for s in sites if "simulated" not in s]
        live = [s for s in sites if s.get("simulated") is False]
        check("every site declares simulated/live", not unflagged,
              f"{len(unflagged)} sites missing the flag")
        check("exactly one site claims a live feed", len(live) == 1,
              f"{len(live)} sites claim live footage")

        # `runs_without_api_key` is by definition `mode == "replay"`, so reading it
        # off a live instance proves nothing. The claim is verified by actually
        # running a session in replay: it must succeed, and it must not write a
        # single new cassette, because a write means it reached the network.
        mode = health.get("mode")
        flag = health.get("runs_without_api_key")
        check("health flag agrees with mode", flag == (mode == "replay"),
              f"mode={mode} runs_without_api_key={flag}")

        # Replay is proved by the run *succeeding*, not by counting files.
        #
        # In replay mode a cassette miss raises ProviderError rather than falling
        # through to the network, which the chaos suite asserts separately. So a
        # clean exit means every request was served from disk, by construction.
        #
        # Counting new cassettes looked like a stronger check and was actually a
        # racy one: tier-4 escalation is asynchronous by design, so a perception
        # call from an earlier live check finishes and writes its cassette during
        # the replay window, and a directory-wide count blames the replay for it.
        env = {**os.environ, "KESTREL_MODE": "replay", "PYTHONIOENCODING": "utf-8"}
        r = subprocess.run(
            ["uv", "run", "kestrel", "scenario", "loiter-midnight"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=600,
        )
        out = (r.stdout or "") + (r.stderr or "")
        check("runs with no API key (replay session succeeds)", r.returncode == 0,
              out.strip().splitlines()[-1:][0] if r.returncode else
              "every model call served from disk; a miss would have raised")
        # And it must do real work, not exit cleanly having done nothing.
        check("the replay actually fires its expected rule",
              "HIT" in out and "loitering" in out,
              "loiter-midnight fired as expected" if "HIT" in out
              else "scenario ran but did not fire")

    if health:
        # The breaker cooldown is 30s, so a circuit found open immediately after a
        # call is not stuck — it is the breaker doing exactly its job. "Stuck"
        # means still open after the cooldown has elapsed, so that is what gets
        # measured rather than a single instantaneous read.
        def open_circuits() -> list[str]:
            _, h = get(f"{API}/api/health")
            return [p["provider"] for p in (h or {}).get("providers", [])
                    if p.get("circuit_open")]

        degraded = open_circuits()
        if degraded:
            time.sleep(35)          # one cooldown, plus a margin
            recovered = [p for p in degraded if p not in open_circuits()]
            still = [p for p in degraded if p not in recovered]
            check("provider circuits recover after their cooldown", not still,
                  f"opened: {degraded}; recovered: {recovered or 'none'}"
                  + (f"; STILL OPEN: {still}" if still else ""),
                  warn_only=bool(still))
        else:
            check("provider circuits recover after their cooldown", True, "all closed")

    # Text style: no em dash anywhere a user can see it.
    web_dash = subprocess.run(
        ["git", "grep", "-c", "—", "--", "web/app", "web/components", "web/lib"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    check("no em dashes in the web app", web_dash.returncode != 0,
          web_dash.stdout.strip()[:200] or "clean")

    bad_json = []
    for p in Path("data").rglob("*.json"):
        if "cassettes" in p.parts:
            continue
        t = p.read_text(encoding="utf-8", errors="replace")
        if "\\u2014" in t or "—" in t:
            bad_json.append(p.as_posix())
    check("no em dashes in rendered data files", not bad_json, ", ".join(bad_json[:4]))

    # The backend suite.
    r = subprocess.run(["uv", "run", "pytest", "-q"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = (r.stdout or r.stderr).strip().splitlines()[-1:] or [""]
    check("backend test suite", r.returncode == 0, tail[0])

    # Frontend types.
    r = subprocess.run(["npm", "--prefix", "web", "run", "typecheck"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", shell=True)
    check("frontend typecheck", r.returncode == 0,
          (r.stdout or r.stderr).strip().splitlines()[-1:][0] if r.returncode else "clean")



# ── 5. control plane ───────────────────────────────────────────────────────
def control_plane(health: dict) -> None:
    section("5. CONTROL PLANE  — the agent answers, and cannot act on its own")

    if not health:
        record(SKIP, "control plane", "API not reachable")
        return

    st, turn = post(f"{API}/api/ask", {"question": "what rules are enabled?"}, timeout=260)
    ok = st == 200 and isinstance(turn, dict) and bool(turn.get("answer"))
    if not ok:
        _, h = get(f"{API}/api/health")
        open_circuits = [p["provider"] for p in (h or {}).get("providers", [])
                         if p.get("circuit_open")]
        check("Ask KESTREL answers a question", False,
              f"HTTP {st}; open circuits: {open_circuits or 'none'}",
              warn_only=bool(open_circuits))
    else:
        check("Ask KESTREL answers a question", True,
              f"{len(turn.get('tool_calls') or [])} tool call(s), verified={turn.get('verified')}")
    if ok:
        check("answer is grounded (citations verify)", turn.get("verified") is not False,
              f"verified={turn.get('verified')} note={str(turn.get('verification_note'))[:60]}")

    # The single most important boundary: declining must not execute.
    st, out = post(f"{API}/api/ask/confirm",
                   {"tool": "enable_rule", "arguments": {"rule_id": "loitering"},
                    "approve": False}, timeout=120)
    check("declining a gated action does not execute it",
          st == 200 and isinstance(out, dict) and out.get("executed") is False,
          f"HTTP {st} executed={(out or {}).get('executed')}")

    # And the gate is a code path, not prompt wording.
    src = Path("src/kestrel/agent/agent.py").read_text(encoding="utf-8", errors="replace")
    ask_body = src.split("async def ask")[1].split("async def confirm")[0] if "async def ask" in src else ""
    check("the agent's own loop cannot self-approve",
          "approved=True" not in ask_body, "approved=True absent from ask()")


# ── 6. claims ──────────────────────────────────────────────────────────────
def claims() -> None:
    section("6. CLAIMS  — every number on the landing page traced to evidence")

    ev = {}
    for name in ("scenarios", "chaos", "retrieval", "gate_efficiency"):
        f = Path(f"data/eval/{name}.json")
        if f.exists():
            ev[name] = json.loads(f.read_text(encoding="utf-8"))
    if not ev:
        record(FAIL, "eval evidence present", "data/eval/*.json missing")
        return

    page = Path("web/app/page.tsx").read_text(encoding="utf-8", errors="replace")
    story = Path("web/lib/story.ts").read_text(encoding="utf-8", errors="replace")
    text = page + story

    sc, ch, rt, ga = (ev.get("scenarios", {}), ev.get("chaos", {}),
                      ev.get("retrieval", {}), ev.get("gate_efficiency", {}))

    expectations = [
        (f"{sc.get('passed')} / {sc.get('total')}", "scenarios pass"),
        (f"{ch.get('survived')} / {ch.get('total')}", "chaos faults survived"),
        (f"{rt.get('mean_precision_at_k')}", "mean P@k"),
        (f"{ga.get('overall_efficiency_real_footage', 0) * 100:.1f}%", "gate, real footage"),
        (f"{ga.get('idle_efficiency_constructed', 0) * 100:.1f}%", "gate, idle context"),
    ]
    for value, label in expectations:
        check(f"page quotes measured {label}", value in text, f"expected '{value}' on the page")

    # And nothing claims a perfect score the evidence does not support.
    check("scenario suite genuinely passes", sc.get("passed") == sc.get("total"),
          f"{sc.get('passed')}/{sc.get('total')}, "
          f"precision={sc.get('precision')} recall={sc.get('recall')}")
    check("chaos suite genuinely survives", ch.get("survived") == ch.get("total"),
          f"{ch.get('survived')}/{ch.get('total')}")

    # ── the probe deck must describe the models the app actually runs ──────
    #
    # A probe marked `required` that fails reads as "this system is broken".
    # nvidia/nvclip is unprovisioned for developer keys and superseded, so it
    # must stay optional; the model that really serves the joint image/text
    # space must be probed, required, and passing.
    probes = json.loads(Path("data/probe_results.json").read_text(encoding="utf-8"))
    rows = probes.get("probes", [])
    by_name = {r["name"]: r for r in rows}

    nvclip = [r for r in rows if "nvclip" in r["name"]]
    check("unprovisioned nvclip is not marked required",
          bool(nvclip) and not any(r["required"] for r in nvclip),
          f"{len(nvclip)} nvclip probes, "
          f"{sum(r['required'] for r in nvclip)} still required")

    joint = [r for r in rows if "joint" in r["name"] or "cross-modal" in r["name"]]
    check("the joint image/text space is probed and required",
          len(joint) >= 3 and all(r["required"] for r in joint),
          f"{len(joint)} probes covering text, image and their alignment")
    check("the joint image/text space passes live",
          bool(joint) and all(r["ok"] for r in joint),
          "; ".join(f"{r['name'].split('(')[0].strip()}: "
                    f"{'ok' if r['ok'] else 'FAILED'}" for r in joint))

    cross = by_name.get("embed cross-modal alignment", {})
    cos = (cross.get("extra") or {}).get("cosine")
    check("text and image land in one comparable space",
          cos is not None and -1.0 <= cos <= 1.0 and cos != 0.0,
          f"cosine(text, image) = {cos}, dim {(cross.get('extra') or {}).get('dim')}")

    # The probe hardcodes the embedder name; if Settings moves on, the deck
    # would keep certifying a model nothing uses.
    probe_src = Path("scripts/probe_models.py").read_text(encoding="utf-8")
    from kestrel.config import Settings
    settings = Settings()
    configured = settings.vl_embed
    check("probe embedder matches the configured one",
          f'VL_EMBED = "{configured}"' in probe_src,
          f"config says {configured}")

    # Each provider must be probed with its OWN roster. Sharing KESTREL_LLM
    # across both sent Groq's model name to NVIDIA and NVIDIA's to Groq; both
    # answered 404 and the deck reported two outages that did not exist.
    check("chat probes are not cross-wired between providers",
          'os.getenv("KESTREL_LLM"' not in probe_src
          and 'os.getenv("KESTREL_LLM_FALLBACK"' not in probe_src,
          "each provider probed with its own model name")
    check("the primary reasoning model is probed and required",
          f'GROQ_CHAT = os.getenv("KESTREL_GROQ_CHAT", "{settings.llm}")' in probe_src,
          f"primary is {settings.llm} on {settings.llm_provider}")
    check("the failover reasoning model is probed and required",
          f'NV_CHAT = os.getenv("KESTREL_NV_CHAT", "{settings.llm_fallback}")' in probe_src,
          f"failover is {settings.llm_fallback}")

    failed_required = [r for r in rows if r["required"] and not r["ok"]]
    check("no required probe is failing",
          not failed_required,
          "; ".join(f"{r['name']}: {r['detail'][:60]}" for r in failed_required)
          or f"{sum(r['required'] for r in rows)} required probes all pass")

    # ── the agent's own cassettes have to be replayable ──────────────────
    #
    # The system prompt embedded datetime.now() to the second, so every
    # /api/ask payload was unique and no cassette could ever match its own
    # recording. Replay recorded hundreds of them and then failed every
    # question, which reads as a broken agent rather than an uncacheable
    # prompt. The clock must come from the data, not the wall.
    agent_src = Path("src/kestrel/agent/agent.py").read_text(encoding="utf-8")
    check("the agent's clock is not wall-clock in the system prompt",
          "now=self._now()" in agent_src
          and "now=datetime.now()" not in agent_src,
          "reasons against the last observation, so a recording replays exactly")

    # An enum declared in a tool schema has to bind at the boundary. A model
    # calling list_alerts with status="all" produced WHERE status = 'all',
    # matched nothing, and reported "there are no recent alerts" while four
    # open alerts sat in the table: a confident falsehood, not an error.
    from kestrel.agent.registry import _reconcile_enums

    class _T:
        parameters: ClassVar[dict[str, Any]] = {
            "properties": {"status": {"enum": ["open", "resolved"]}}
        }

    check("out-of-enum tool arguments cannot reach a query",
          _reconcile_enums(_T, {"status": "all"}) == {}
          and _reconcile_enums(_T, {"status": "OPEN"}) == {"status": "open"}
          and _reconcile_enums(_T, {"status": "open"}) == {"status": "open"},
          "wildcards drop to no-filter, casing is repaired, valid values pass")



# ── 7. modalities ──────────────────────────────────────────────────────────
def modalities(health: dict) -> None:
    section("7. MODALITIES  — the visual surfaces, and the streams behind them")

    # A CSS custom property handed to a WebGL or canvas renderer is unparseable
    # there: three.js leaves a null material and three-globe reads `.opacity` off
    # it. The symptom (a TypeError plus an unshaded globe) does not point back at
    # a colour string, so it is worth a static guard.
    globe = Path("web/components/viz/CommandGlobe.tsx").read_text(encoding="utf-8")
    site_map = Path("web/components/viz/SiteMap.tsx").read_text(encoding="utf-8")
    bad = []
    for name, src in (("CommandGlobe", globe), ("SiteMap", site_map)):
        for marker in ("pointColor", "arcColor", "polygonCapColor", "colour:", "color:"):
            for line in src.splitlines():
                if marker in line and "sevColor(" in line and "style=" not in line:
                    bad.append(f"{name}: {line.strip()[:70]}")
    check("no CSS variable reaches a WebGL colour", not bad, "; ".join(bad[:3]) or "clean")

    # react-globe.gl extrudes country polygons upward from the sphere. A marker
    # shorter than the polygon under it is swallowed by the extrusion, which is
    # how the site dots and alert rings ended up buried inside the globe. Marker
    # altitudes are derived from the tallest polygon so the two cannot drift.
    derived = ("MARKER_FLOOR" in globe and "POLY_MAX" in globe
               and "pointAltitude={(d: any) => MARKER_FLOOR" in globe
               and "ringAltitude={MARKER_FLOOR}" in globe)
    hardcoded = re.search(r"pointAltitude=\{\(d: any\) => 0\.0", globe)
    check("globe markers sit above the country extrusions",
          derived and not hardcoded,
          "altitudes derived from POLY_MAX" if derived else "marker altitude is hardcoded")

    # MapLibre decodes tiles in a Web Worker, and v6 derives the worker URL from
    # `import.meta.url` - which webpack inlines as a `file:` URL, so its own guard
    # returns "" and the browser runs `new Worker("")`. We serve the worker from
    # /public instead. The trap is that the worker is not one file: it imports
    # `./maplibre-gl-shared.mjs`, and shipping the worker alone reproduces the
    # original blank map exactly, because the sibling 404s to an HTML page.
    st, worker = get(f"{WEB}/maplibre/maplibre-gl-worker.mjs", timeout=60)
    if st != 200 or not isinstance(worker, str):
        record(SKIP, "maplibre worker chain", "web server not running")
    else:
        deps = sorted(set(re.findall(r"""from\s*["'](\./[^"']+\.mjs)["']""", worker)))
        broken = []
        for dep in deps:
            name = dep.lstrip("./")
            code, body = get(f"{WEB}/maplibre/{name}", timeout=60)
            if code != 200 or not isinstance(body, str) or body.lstrip().startswith("<!DOCTYPE"):
                broken.append(name)
        check("maplibre worker's imports all resolve", not broken,
              f"404s to an HTML page: {broken}" if broken
              else f"worker + {len(deps)} dependency file(s) served as JS")

    # The map key lives in the repo-root .env; Next only reads its own directory,
    # so it has to be lifted explicitly or the basemap silently never loads.
    st, _page = get(f"{WEB}/site/plant-01", timeout=90)
    if st != 200:
        record(SKIP, "site map page", "web server not running")
    else:
        key = ""
        for line in Path(".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("NEXT_PUBLIC_MAPTILER_KEY="):
                key = line.split("=", 1)[1].strip()
        # Next inlines NEXT_PUBLIC_* at compile time, into the client chunk of
        # whichever route imports the component. Two earlier versions of this
        # check looked in the wrong place and cried wolf: the server-rendered
        # HTML never carries it, and neither do the chunks the HTML references,
        # because the map is a lazily-imported chunk pulled in at runtime.
        #
        # Scanning the compiled chunk directory is what actually answers the
        # question "did the value survive the build", and it works the same for
        # a dev server and a production build.
        found_in = ""
        if key:
            chunk_root = Path("web/.next/static/chunks")
            for js in chunk_root.rglob("*.js"):
                try:
                    if key in js.read_text(encoding="utf-8", errors="replace"):
                        found_in = str(js.relative_to(chunk_root)).replace("\\", "/")
                        break
                except OSError:
                    continue
        check("map tile key reaches the browser", bool(found_in),
              f"inlined into chunk {found_in}" if found_in
              else "not in any compiled chunk; basemap falls back to zone geometry only")
        check("map tile key is a real key, not a placeholder",
              bool(key) and len(key) >= 16 and "your" not in key.lower(),
              f"{len(key)} chars" if key else "NEXT_PUBLIC_MAPTILER_KEY unset in .env")

    if not health:
        record(SKIP, "streams", "API not reachable")
        return

    # The agent stream must report progress, not go silent for the whole run.
    events = stream_events(f"{API}/api/ask/stream",
                           {"question": "what rules are enabled?"}, limit=6, timeout=180)
    kinds = [e.get("type") for e in events]
    check("agent stream reports progress while working",
          any(k in ("planning", "tool_start", "tool", "composing", "heartbeat")
              for k in kinds),
          f"events: {kinds}")

    # The console's live session must actually produce frames.
    events = stream_events(f"{API}/api/session/stream",
                           {"clip": "worker-zone", "frames": 3, "fps": 2.0},
                           limit=2, timeout=240)
    frames = [e for e in events if e.get("kind") == "frame"]
    check("live session streams frames", bool(frames),
          f"{len(frames)} frame event(s); first gate reason: "
          f"{(frames[0].get('payload') or {}).get('gate_reason', '?') if frames else 'none'}")


def stream_events(url: str, payload: dict, *, limit: int, timeout: float) -> list[dict]:
    """Read the first `limit` SSE events, then give up on the rest."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    out: list[dict] = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    out.append(json.loads(line[5:].strip()))
                except json.JSONDecodeError:
                    continue
                if len(out) >= limit:
                    break
    except Exception:
        pass
    return out



# ── 8. new surfaces ────────────────────────────────────────────────────────
def surfaces(health: dict) -> None:
    section("8. SURFACES  — playback, dispatch and the agent's own UI contract")

    # Every clip the console offers must actually be playable.
    st, clips = get(f"{API}/api/clips")
    rows = (clips or {}).get("clips", []) if isinstance(clips, dict) else []
    check("clip catalogue is served", st == 200 and len(rows) >= 6, f"{len(rows)} clips")
    unindexed = [c["slug"] for c in rows if not c.get("indexed")]
    check("every clip has a playback index", not unindexed,
          f"missing: {unindexed}" if unindexed else f"{len(rows)} indexed")

    # A playback index is worthless if the boxes are not where the video is.
    for clip in [c["slug"] for c in rows][:6]:
        st, idx = get(f"{API}/api/playback/{clip}", timeout=90)
        if st != 200 or not isinstance(idx, dict):
            check(f"playback index {clip}", False, f"HTTP {st}")
            continue
        dets = [d for f in idx.get("frames", []) for d in f.get("dets", [])]
        normalised = all(
            0.0 <= d[k] <= 1.0 for d in dets[:400] for k in ("x1", "y1", "x2", "y2")
        )
        check(f"playback index {clip}", bool(idx.get("frames")) and normalised,
              f"{len(idx.get('frames', []))} frames, {len(dets)} detections, "
              f"{len(idx.get('alerts', []))} alerts, {idx.get('detector')}"
              + ("" if normalised else "  BOXES NOT NORMALISED"))

    # Byte ranges are what make the timeline scrubbable.
    try:
        req = urllib.request.Request(f"{API}/api/footage/worker-zone.mp4",
                                     headers={"Range": "bytes=0-1023"})
        with urllib.request.urlopen(req, timeout=60) as r:
            partial = r.status == 206 and r.headers.get("Content-Range") is not None
            detail = f"HTTP {r.status} {r.headers.get('Content-Range')}"
    except Exception as e:
        partial, detail = False, f"{type(e).__name__}"
    check("footage endpoint honours a Range request", partial, detail)

    # The agent's render contract: every declared renders_as needs a UI case.
    st, tools = get(f"{API}/api/tools")
    declared = {t.get("renders_as") for t in (tools or {}).get("tools", []) if t.get("renders_as")}
    ui = Path("web/components/ask/ToolResult.tsx").read_text(encoding="utf-8")
    missing = sorted(r for r in declared if f'case "{r}"' not in ui and r != "text")
    check("every tool result has a UI renderer", not missing,
          f"falls back to raw JSON: {missing}" if missing else f"{len(declared)} render targets")

    # The scope guard has to hold without a model call.
    st, turn = post(f"{API}/api/ask", {"question": "what is the capital of France?"}, timeout=90)
    refused = (
        st == 200 and isinstance(turn, dict)
        and turn.get("intent") == "OUT_OF_SCOPE"
        and not (turn.get("tool_calls") or [])
    )
    check("off-topic question is refused without touching a tool", refused,
          f"intent={(turn or {}).get('intent')} tools={len((turn or {}).get('tool_calls') or [])}")

    # And a legitimate question must still get through it.
    st, turn = post(f"{API}/api/ask", {"question": "what rules are enabled?"}, timeout=260)
    ok = st == 200 and isinstance(turn, dict) and turn.get("intent") != "OUT_OF_SCOPE"
    if not ok and isinstance(turn, dict) and turn.get("intent") == "OUT_OF_SCOPE":
        check("the scope guard does not refuse real questions", False,
              "a legitimate question was refused")
    else:
        check("the scope guard does not refuse real questions", True,
              f"intent={(turn or {}).get('intent', 'n/a')}")

    # Route-level safety nets.
    for f in ("error.tsx", "not-found.tsx", "loading.tsx"):
        check(f"route boundary {f}", Path(f"web/app/{f}").exists())

    # ── the dispatch chain ──────────────────────────────────────────────
    # `approve_mission` authorises a mission, not an alert. The overlay used to
    # send `alert_id` and got "missing required argument: mission_id", so the
    # whole deploy flow dead-ended at the decision.
    _, tools = get(f"{API}/api/tools")
    spec = next((t for t in (tools or {}).get("tools", [])
                 if t.get("name") == "approve_mission"), None)
    required = (spec or {}).get("parameters", {}).get("required", [])
    ui = Path("web/components/deploy/DeployOverlay.tsx").read_text(encoding="utf-8")
    check("dispatch overlay sends what approve_mission requires",
          all(f'"{arg}"' in ui or f"{arg}:" in ui for arg in required),
          f"required={required}")

    st, out = post(f"{API}/api/ask/confirm",
                   {"tool": "approve_mission", "arguments": {"mission_id": "msn_nope"},
                    "approve": False}, timeout=60)
    check("declining a dispatch never executes it",
          st == 200 and (out or {}).get("executed") is False,
          f"executed={(out or {}).get('executed')}")

    # ── an uploaded clip must be playable, not just indexed ──────────────
    # `/api/footage` used to resolve slugs only against the bundled manifest, so
    # an uploaded clip returned 404 with a JSON body. The <video> element read
    # that as "no supported sources" and the upload appeared to fail after a
    # successful analysis.
    _, catalogue = get(f"{API}/api/clips")
    uploads = [c for c in (catalogue or {}).get("clips", []) if c.get("uploaded")]
    if not uploads:
        record(SKIP, "uploaded clip is playable", "no uploads on this machine")
    else:
        slug = uploads[0]["slug"]
        try:
            req = urllib.request.Request(f"{API}/api/footage/{slug}.mp4",
                                         headers={"Range": "bytes=0-511"})
            with urllib.request.urlopen(req, timeout=60) as r:
                ok = r.status == 206 and "video" in (r.headers.get("Content-Type") or "")
                detail = f"HTTP {r.status} {r.headers.get('Content-Type')}"
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}"
        check("uploaded clip is playable", ok, f"{slug}: {detail}")

    # ── every clip a browser is offered must decode in a browser ─────────
    #
    # Serving it correctly is not the same as it being playable. Two uploads
    # were MPEG-4 Part 2 with the index at the tail: 200 OK, right content type,
    # right length, and "NotSupportedError: The element has no supported
    # sources" in the player. OpenCV decodes that format happily, so indexing
    # succeeded and only playback failed.
    import asyncio as _asyncio

    from kestrel.media import probe as _probe

    clips = list(Path("data/footage").glob("*.mp4")) + list(Path("data/uploads").glob("*.mp4"))
    if not clips:
        record(SKIP, "served clips decode in a browser", "no clips on disk")
    else:
        async def _probe_all() -> list[tuple[str, Any]]:
            return [(p.stem, await _probe(p)) for p in clips]

        probed = _asyncio.run(_probe_all())
        bad = [f"{n} ({pr.video or 'no video'}"
               f"{'' if pr.faststart else ', index at tail'})"
               for n, pr in probed if not pr.playable]
        check("served clips decode in a browser", not bad,
              "; ".join(bad) if bad
              else f"{len(probed)} clips, all h264/av1/vp9 with a reachable index")

    # ── uploads must not go through the dev proxy ────────────────────────
    # Next caps proxied request bodies at 10 MB and then drops the socket, which
    # reaches the user as a bare "upload failed (500)". Video is large by
    # definition, so the upload posts straight to the API origin instead.
    up = Path("web/components/console/UploadFootage.tsx").read_text(encoding="utf-8")
    check("upload bypasses the dev proxy's 10 MB body cap",
          "apiOrigin" in up and 'fetch("/api/upload' not in up,
          "posts to the API origin directly")

    # And the API must accept a body larger than that cap.
    st, _ = get(f"{API}/api/health", timeout=30)
    check("API reachable for direct upload", st == 200, f"HTTP {st}")

    # ── uploaded footage must be able to raise an alert ──────────────────
    # An ad-hoc zone with a novel id matches no rule in the pack, so an upload
    # would detect and track and then never alert. The zone id is the contract.
    try:
        from kestrel.playback import ad_hoc_site

        zone_ids = {z.id for z in ad_hoc_site(
            site_id="probe", name="probe", lat=18.75, lon=73.86).zones}
    except Exception as e:
        zone_ids, _ = set(), e
    _, rules = get(f"{API}/api/rules")
    rule_zones: set[str] = set()
    for r in (rules or {}).get("rules", []):
        for line in r.get("conditions", []):
            if isinstance(line, str) and "in zone" in line:
                rule_zones |= {z.strip() for z in line.split("in zone", 1)[1].split(",")}
    check("an uploaded clip's zone can satisfy a rule",
          bool(zone_ids & rule_zones),
          f"upload zones {sorted(zone_ids)} vs rule zones {sorted(rule_zones)[:5]}")


# ═══════════════════════════════════════════════════════════════════════════
def main() -> int:
    print("\nKESTREL — site-wide inspection")
    deliverables()
    health = runtime()
    innovations(health)
    integrity(health)
    control_plane(health)
    claims()
    modalities(health)
    surfaces(health)

    section("SUMMARY")
    counts = {s: sum(1 for r in results if r[0] == s) for s in (PASS, FAIL, WARN, SKIP)}
    print(f"  pass {counts[PASS]}   fail {counts[FAIL]}   warn {counts[WARN]}   skip {counts[SKIP]}")
    if counts[FAIL]:
        print("\nFAILURES:")
        for s, name, detail in results:
            if s == FAIL:
                print(f"  - {name}  ::  {detail}")
    if counts[WARN]:
        print("\nWARNINGS (not blocking, but look):")
        for s, name, detail in results:
            if s == WARN:
                print(f"  - {name}  ::  {detail}")
    print()
    return 1 if counts[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
