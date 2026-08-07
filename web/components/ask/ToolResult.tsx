"use client";

import { motion } from "framer-motion";
import {
  AlertTriangle, Boxes, Check, Compass, Gauge, Globe2, Image as ImageIcon,
  Map as MapIcon, Network, Plane, ScrollText, Search, ShieldCheck, X,
} from "lucide-react";
import Link from "next/link";
import { api } from "@/lib/api";
import { bearing, cn, coords, metres, seconds, sevClass, time } from "@/lib/format";
import { Pill, SeverityChip, SimulatedBadge } from "@/components/ui/primitives";
import {
  AlertCard, GlobeFocus, MissionExecution, PatternList, RuleCard, StatsPanel,
  TelemetryPanel, ZoneProfile,
} from "@/components/ask/ToolPanels";

/**
 * Generative UI.
 *
 * Each tool declares a `renders_as` in the Python registry; this maps that to a
 * component. Declaring the tool once and deriving both the agent's contract and
 * the UI's rendering is what stops the two drifting apart: add a tool in Python
 * and it renders correctly here without a second registration.
 *
 * The result is that the conversation *is* an interface: a search returns a frame
 * strip you can click, a mission returns a flight plan, an entity returns a
 * dossier, not a paragraph describing one.
 */

export function ToolResult({ event }: { event: any }) {
  const { tool, result } = event;
  if (!result?.ok && !result?.requires_confirmation) {
    return (
      <Frame tool={tool} icon={X} tone="danger">
        <span className="text-[12px] text-[var(--sev-critical)]">
          {result?.error ?? "tool failed"}
        </span>
      </Frame>
    );
  }
  if (result.requires_confirmation) return null; // handled by the rail's gate card

  switch (result.renders_as ?? "text") {
    case "frame_strip": return <FrameStrip tool={tool} r={result} />;
    case "entity_card": return <EntityCard tool={tool} r={result} />;
    case "entity_list": return <EntityList tool={tool} r={result} />;
    case "alert_list": return <AlertList tool={tool} r={result} />;
    case "evidence_chain": return <EvidenceChain tool={tool} r={result} />;
    case "mission_card": return <MissionCard tool={tool} r={result} />;
    case "rule_preview": return <RulePreview tool={tool} r={result} />;
    case "rule_list": return <RuleList tool={tool} r={result} />;
    case "narrative_block": return <NarrativeBlock tool={tool} r={result} />;
    case "baseline_panel": return <BaselinePanel tool={tool} r={result} />;
    case "fleet_table": return <FleetTable tool={tool} r={result} />;
    case "globe_arcs": return <CorrelationCard tool={tool} r={result} />;
    case "ledger_panel": return <LedgerPanel tool={tool} r={result} />;
    case "architecture_panel": return <ArchitecturePanel tool={tool} r={result} />;
    case "cost_panel": return <CostPanel tool={tool} r={result} />;
    case "navigation": return <NavigationNote tool={tool} r={result} />;
    case "decision_trace": return <DecisionTrace tool={tool} r={result} />;
    case "site_list": return <SiteList tool={tool} r={result} />;
    case "telemetry_panel": return <TelemetryPanel tool={tool} r={result} />;
    case "zone_profile": return <ZoneProfile tool={tool} r={result} />;
    case "rule_card": return <RuleCard tool={tool} r={result} />;
    case "alert_card": return <AlertCard tool={tool} r={result} />;
    case "mission_execution": return <MissionExecution tool={tool} r={result} />;
    case "pattern_list": return <PatternList tool={tool} r={result} />;
    case "globe_focus": return <GlobeFocus tool={tool} r={result} />;
    case "stats_panel": return <StatsPanel tool={tool} r={result} />;
    default: return <GenericResult tool={tool} r={result} />;
  }
}

// ── shell ─────────────────────────────────────────────────────────────────
function Frame({
  tool, icon: Icon, children, tone = "muted", meta,
}: {
  tool: string; icon: any; children: React.ReactNode;
  tone?: "muted" | "accent" | "danger"; meta?: string;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
      className="overflow-hidden rounded-xl border"
      style={{
        borderColor: tone === "danger"
          ? "color-mix(in oklab, var(--sev-critical) 30%, transparent)"
          : "var(--line)",
        background: "var(--surface-2)",
      }}
    >
      <div className="flex items-center gap-1.5 border-b px-2.5 py-1.5"
           style={{ borderColor: "var(--line)" }}>
        <Icon size={11} className="text-[var(--ink-4)]" />
        <span className="mono text-[10.5px] text-[var(--ink-3)]">{tool}</span>
        {meta && <span className="ml-auto text-[10px] text-[var(--ink-4)]">{meta}</span>}
      </div>
      <div className="p-2.5">{children}</div>
    </motion.div>
  );
}

// ── renderers ─────────────────────────────────────────────────────────────
function FrameStrip({ tool, r }: { tool: string; r: any }) {
  const hits = r.hits ?? [];
  return (
    <Frame tool={tool} icon={Search} meta={`${r.count ?? hits.length} results · ${r.took_ms}ms`}>
      {r.plan_steps?.length > 0 && (
        <ol className="mb-2 space-y-0.5">
          {r.plan_steps.map((s: string, i: number) => (
            <li key={i} className="flex gap-1.5 text-[11px] text-[var(--ink-3)]">
              <span className="text-[var(--ink-4)]">{i + 1}.</span>
              {s}
            </li>
          ))}
        </ol>
      )}
      {hits.length === 0 ? (
        <p className="text-[12px] text-[var(--ink-4)]">
          No frames matched. Nothing was invented to fill the gap.
        </p>
      ) : (
        <div className="-mx-1 flex gap-1.5 overflow-x-auto px-1 pb-1">
          {hits.slice(0, 12).map((h: any) => (
            <Link key={h.frame_id} href={`/investigate?frame=${h.frame_id}`}
                  className="group w-32 shrink-0">
              <div className="relative h-20 w-32 overflow-hidden rounded-lg border"
                   style={{ borderColor: "var(--line)", background: "var(--surface-3)" }}>
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={api.frameImage(h.frame_id)} alt=""
                     className="h-full w-full object-cover transition-transform group-hover:scale-105"
                     onError={(e) => { (e.target as HTMLImageElement).style.opacity = "0"; }} />
                <div className="absolute bottom-0 left-0 right-0 flex gap-0.5 bg-gradient-to-t from-black/70 to-transparent px-1 py-0.5">
                  {(h.sources ?? []).map((s: string) => (
                    <span key={s} className="rounded bg-white/20 px-1 text-[8px] text-white">
                      {s[0].toUpperCase()}
                    </span>
                  ))}
                </div>
              </div>
              <div className="mono mt-1 text-[9.5px] text-[var(--ink-4)]">{time(h.ts)}</div>
              <div className="line-clamp-2 text-[10.5px] leading-snug text-[var(--ink-2)]">
                {h.caption}
              </div>
            </Link>
          ))}
        </div>
      )}
    </Frame>
  );
}

function EntityCard({ tool, r }: { tool: string; r: any }) {
  if (!r.found) {
    return <Frame tool={tool} icon={Boxes}><span className="text-[12px] text-[var(--ink-4)]">{r.message}</span></Frame>;
  }
  const e = r.entity;
  return (
    <Frame tool={tool} icon={Boxes} meta={`${r.sightings?.length ?? 0} sightings`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[14px] font-semibold">{e.descriptor || e.label}</div>
          <div className="mono text-[10.5px] text-[var(--ink-4)]">{e.id}</div>
        </div>
        <Pill tone="accent">{e.kind}</Pill>
      </div>
      <div className="mt-2 grid grid-cols-3 gap-2 text-center">
        {[["visits", e.visit_count], ["frames", e.frame_count], ["zones", e.zones?.length ?? 0]].map(
          ([k, v]) => (
            <div key={k as string} className="rounded-lg border py-1.5"
                 style={{ borderColor: "var(--line)", background: "var(--surface)" }}>
              <div className="tnum text-[16px] font-semibold" style={{ color: "var(--accent)" }}>
                {v as number}
              </div>
              <div className="eyebrow">{k as string}</div>
            </div>
          ))}
      </div>
      <div className="mt-2 space-y-0.5 text-[11.5px] text-[var(--ink-3)]">
        <div>first seen {time(e.first_seen)}</div>
        <div>last seen {time(e.last_seen)}</div>
        {e.zones?.length > 0 && <div>zones: {e.zones.join(", ")}</div>}
      </div>
    </Frame>
  );
}

function EntityList({ tool, r }: { tool: string; r: any }) {
  return (
    <Frame tool={tool} icon={Boxes} meta={`${r.count} entities`}>
      <div className="space-y-1">
        {(r.entities ?? []).slice(0, 8).map((e: any) => (
          <Link key={e.id} href={`/entities?entity=${e.id}`}
                className="flex items-center gap-2 rounded-lg px-1.5 py-1 hover:bg-[var(--surface-3)]">
            <span className="flex-1 truncate text-[12px]">{e.descriptor || e.label}</span>
            <span className="tnum text-[11px] text-[var(--ink-4)]">{e.visit_count} visits</span>
          </Link>
        ))}
      </div>
    </Frame>
  );
}

function AlertList({ tool, r }: { tool: string; r: any }) {
  const alerts = r.alerts ?? [];
  return (
    <Frame tool={tool} icon={AlertTriangle} meta={`${r.count} alerts`}>
      {alerts.length === 0 ? (
        <p className="text-[12px] text-[var(--ink-4)]">No alerts match.</p>
      ) : (
        <div className="space-y-1.5">
          {alerts.slice(0, 6).map((a: any) => (
            <div key={a.id} className={cn(sevClass(a.severity), "sev-bar rounded-r-lg py-1 pl-2")}>
              <div className="flex items-center gap-1.5">
                <SeverityChip severity={a.severity} />
                <span className="mono text-[10px] text-[var(--ink-4)]">{time(a.ts)}</span>
              </div>
              <div className="mt-0.5 text-[12px] leading-snug">{a.title}</div>
              {a.location?.lat != null && (
                <div className="mono mt-0.5 text-[10px] text-[var(--accent-ink)]">
                  {coords(a.location.lat, a.location.lon)} · {metres(a.location.distance_from_dock_m)} from dock
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </Frame>
  );
}

function EvidenceChain({ tool, r }: { tool: string; r: any }) {
  if (!r.found) return <Frame tool={tool} icon={ShieldCheck}><span className="text-[12px] text-[var(--ink-4)]">Alert not found.</span></Frame>;
  return (
    <Frame tool={tool} icon={ShieldCheck} meta={`${r.evidence?.length ?? 0} links`}>
      <div className="text-[12.5px] font-medium">{r.alert.title}</div>
      <ul className="mt-2 space-y-1">
        {(r.evidence ?? []).map((e: any, i: number) => (
          <li key={i} className="flex gap-1.5 text-[11.5px] leading-snug">
            <span className="mono shrink-0 text-[9.5px] uppercase text-[var(--ink-4)]"
                  style={{ minWidth: 58 }}>{e.kind}</span>
            <span className={e.weight === 0 ? "text-[var(--ink-4)] line-through" : "text-[var(--ink-2)]"}>
              {e.caption}
            </span>
          </li>
        ))}
      </ul>
    </Frame>
  );
}

function MissionCard({ tool, r }: { tool: string; r: any }) {
  if (!r.ok) return <Frame tool={tool} icon={Plane}><span className="text-[12px] text-[var(--ink-4)]">{r.error}</span></Frame>;
  const f = r.feasibility ?? {};
  return (
    <Frame tool={tool} icon={Plane} meta={r.status}>
      <p className="whitespace-pre-wrap text-[12px] leading-relaxed text-[var(--ink-2)]">
        {r.rationale}
      </p>
      <div className="mt-2 space-y-1">
        {(r.steps ?? []).map((s: any, i: number) => (
          <div key={i} className="flex items-center gap-2 rounded-lg border px-2 py-1"
               style={{ borderColor: "var(--line)", background: "var(--surface)" }}>
            <span className="mono w-14 shrink-0 text-[9.5px] font-semibold uppercase"
                  style={{ color: "var(--accent)" }}>{s.kind}</span>
            <span className="tnum text-[10.5px] text-[var(--ink-4)]">{s.altitude_m}m</span>
            {s.target && (
              <span className="mono ml-auto text-[9.5px] text-[var(--ink-4)]">
                {s.target.lat.toFixed(5)}, {s.target.lon.toFixed(5)}
              </span>
            )}
          </div>
        ))}
      </div>
      <div className="mt-2 flex flex-wrap gap-1.5">
        <Pill tone={f.feasible ? "ok" : "danger"}>
          {f.feasible ? "feasible" : "cannot fly"}
        </Pill>
        <Pill tone="muted">battery {f.battery_required_pct}% / {f.battery_available_pct}%</Pill>
        <Pill tone="muted">{metres(f.distance_m)}</Pill>
        <Pill tone="muted">{seconds(f.duration_s)}</Pill>
        {!f.daylight && <Pill tone="warn">night</Pill>}
      </div>
      {(f.blockers ?? []).length > 0 && (
        <ul className="mt-1.5 space-y-0.5">
          {f.blockers.map((b: string, i: number) => (
            <li key={i} className="text-[11px] text-[var(--sev-critical)]">✕ {b}</li>
          ))}
        </ul>
      )}
    </Frame>
  );
}

function RulePreview({ tool, r }: { tool: string; r: any }) {
  if (!r.ok) return <Frame tool={tool} icon={ScrollText} tone="danger"><span className="text-[12px]">{r.error}</span></Frame>;
  const bt = r.backtest ?? {};
  return (
    <Frame tool={tool} icon={ScrollText} meta={r.rule?.severity}>
      <div className="text-[13px] font-semibold">{r.rule?.name}</div>
      <ul className="mt-1.5 space-y-0.5">
        {(r.explanation ?? []).map((c: string, i: number) => (
          <li key={i} className="flex gap-1.5 text-[11.5px] text-[var(--ink-2)]">
            <span style={{ color: "var(--accent)" }}>·</span>{c}
          </li>
        ))}
      </ul>
      {r.rule?.visual_predicate && (
        <div className="mt-1.5 rounded-lg border px-2 py-1 text-[11px] text-[var(--ink-3)]"
             style={{ borderColor: "var(--line)" }}>
          visual predicate: “{r.rule.visual_predicate}”
        </div>
      )}
      <div className="mt-2 rounded-lg border p-2"
           style={{
             borderColor: bt.fire_count > 0 ? "color-mix(in oklab, var(--sev-medium) 30%, transparent)" : "var(--line)",
             background: "var(--surface)",
           }}>
        <div className="eyebrow mb-1">Backtest against history</div>
        <div className="text-[12px] leading-relaxed text-[var(--ink-2)]">{bt.verdict}</div>
        <div className="mt-1 flex gap-3 text-[10.5px] text-[var(--ink-4)]">
          <span className="tnum">{bt.frames_replayed} frames</span>
          <span className="tnum">{bt.days_covered} days</span>
          <span className="tnum">{bt.fire_count} fires</span>
        </div>
      </div>
      <p className="mt-1.5 text-[10.5px] text-[var(--ink-4)]">{r.next_step}</p>
    </Frame>
  );
}

function RuleList({ tool, r }: { tool: string; r: any }) {
  return (
    <Frame tool={tool} icon={ScrollText} meta={`${r.count} rules`}>
      <div className="space-y-1">
        {(r.rules ?? []).map((x: any) => (
          <div key={x.id} className="flex items-center gap-2">
            <SeverityChip severity={x.severity} />
            <span className="flex-1 truncate text-[12px]">{x.name}</span>
            {x.fires > 0 && <span className="tnum text-[10.5px] text-[var(--ink-4)]">{x.fires}×</span>}
            {!x.enabled && <Pill tone="muted">off</Pill>}
          </div>
        ))}
      </div>
    </Frame>
  );
}

function NarrativeBlock({ tool, r }: { tool: string; r: any }) {
  if (!r.found) return <Frame tool={tool} icon={MapIcon}><span className="text-[12px] text-[var(--ink-4)]">{r.message}</span></Frame>;
  return (
    <Frame tool={tool} icon={MapIcon} meta={`${r.nodes?.length ?? 0} memory nodes`}>
      <div className="space-y-1.5">
        {(r.nodes ?? []).slice(0, 6).map((n: any, i: number) => (
          <div key={i} className="border-l-2 pl-2" style={{ borderColor: "var(--accent-3)" }}>
            <div className="flex items-center gap-1.5">
              <span className="eyebrow">{n.level?.replace(/^L\d_/, "")}</span>
              <span className="mono text-[9.5px] text-[var(--ink-4)]">
                {time(n.start)}–{time(n.end)}
              </span>
            </div>
            <div className="text-[12px] leading-relaxed text-[var(--ink-2)]">{n.summary}</div>
          </div>
        ))}
      </div>
    </Frame>
  );
}

function BaselinePanel({ tool, r }: { tool: string; r: any }) {
  return (
    <Frame tool={tool} icon={Gauge} meta={r.confident ? "confident" : "insufficient history"}>
      <div className="flex items-center gap-2">
        <Pill tone={r.anomalous ? "danger" : "ok"}>
          {r.anomalous ? "anomalous" : "within normal"}
        </Pill>
        {r.first_ever && <Pill tone="warn">first ever</Pill>}
      </div>
      <p className="mt-1.5 text-[12px] leading-relaxed text-[var(--ink-2)]">{r.explanation}</p>
    </Frame>
  );
}

function FleetTable({ tool, r }: { tool: string; r: any }) {
  const s = r.summary ?? {};
  return (
    <Frame tool={tool} icon={Globe2} meta={`${s.sites} sites · ${s.countries} countries`}>
      <div className="grid grid-cols-4 gap-1.5 text-center">
        {[["live", s.live_sites], ["sim", s.simulated_sites],
          ["alerts", s.active_alerts], ["airborne", s.airborne]].map(([k, v]) => (
          <div key={k as string} className="rounded-lg border py-1"
               style={{ borderColor: "var(--line)", background: "var(--surface)" }}>
            <div className="tnum text-[15px] font-semibold">{v as number}</div>
            <div className="eyebrow">{k as string}</div>
          </div>
        ))}
      </div>
      <div className="mt-2 space-y-0.5">
        {(r.sites ?? []).slice(0, 6).map((x: any) => (
          <Link key={x.site_id} href={`/site/${x.site_id}`}
                className="flex items-center gap-1.5 rounded px-1 py-0.5 hover:bg-[var(--surface-3)]">
            <span className={cn(sevClass(x.peak_severity), "sev-dot")} style={{ boxShadow: "none" }} />
            <span className="flex-1 truncate text-[11.5px]">{x.name}</span>
            {x.simulated && <SimulatedBadge compact />}
            <span className="tnum text-[10.5px] text-[var(--ink-4)]">{x.active_alerts}</span>
          </Link>
        ))}
      </div>
      <p className="mt-1.5 text-[10px] leading-relaxed text-[var(--ink-4)]">{s.note}</p>
    </Frame>
  );
}

function CorrelationCard({ tool, r }: { tool: string; r: any }) {
  return (
    <Frame tool={tool} icon={Compass} meta={`${r.count} correlations`}>
      {r.count === 0 ? (
        <p className="text-[12px] text-[var(--ink-4)]">
          No subject has been seen at more than one site.
        </p>
      ) : (
        <div className="space-y-2">
          {(r.matches ?? []).slice(0, 3).map((m: any, i: number) => (
            <div key={i} className="rounded-lg border p-2"
                 style={{
                   borderColor: "color-mix(in oklab, var(--sev-high) 32%, transparent)",
                   background: "color-mix(in oklab, var(--sev-high) 6%, transparent)",
                 }}>
              <div className="flex items-center gap-1.5">
                <span className="text-[12.5px] font-semibold">{m.descriptor}</span>
                <Pill tone="warn">{m.sites.length} sites</Pill>
              </div>
              <p className="mt-1 text-[11.5px] leading-relaxed text-[var(--ink-2)]">
                {m.assessment}
              </p>
              <div className="mt-1 flex flex-wrap gap-1">
                {m.site_names.map((n: string) => (
                  <span key={n} className="rounded border px-1 text-[9.5px] text-[var(--ink-3)]"
                        style={{ borderColor: "var(--line)" }}>{n}</span>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
      <p className="mt-1.5 text-[10px] text-[var(--ink-4)]">{r.note}</p>
    </Frame>
  );
}

function LedgerPanel({ tool, r }: { tool: string; r: any }) {
  const v = r.verification ?? {};
  return (
    <Frame tool={tool} icon={ShieldCheck} meta={`${v.entries} entries`}>
      <div className="flex items-center gap-1.5">
        {v.valid ? <Check size={13} style={{ color: "var(--ok)" }} />
                 : <X size={13} style={{ color: "var(--sev-critical)" }} />}
        <span className="text-[12.5px] font-medium"
              style={{ color: v.valid ? "var(--ok)" : "var(--sev-critical)" }}>
          {v.valid ? "Chain verified" : "CHAIN BROKEN"}
        </span>
      </div>
      <p className="mt-1 text-[11.5px] leading-relaxed text-[var(--ink-3)]">
        {v.note ?? v.reason}
      </p>
    </Frame>
  );
}

function ArchitecturePanel({ tool, r }: { tool: string; r: any }) {
  return (
    <Frame tool={tool} icon={Network} meta={r.topic}>
      <div className="whitespace-pre-wrap text-[11.5px] leading-relaxed text-[var(--ink-2)]">
        {r.explanation}
      </div>
    </Frame>
  );
}

function CostPanel({ tool, r }: { tool: string; r: any }) {
  const c = r.cost ?? {};
  return (
    <Frame tool={tool} icon={Gauge}>
      <div className="grid grid-cols-2 gap-2">
        <div>
          <div className="eyebrow">modelled</div>
          <div className="tnum text-[18px] font-semibold" style={{ color: "var(--accent)" }}>
            ${(c.modelled_usd ?? 0).toFixed(5)}
          </div>
        </div>
        <div>
          <div className="eyebrow">per drone-hour</div>
          <div className="tnum text-[18px] font-semibold">
            {c.per_drone_hour_usd != null ? `$${c.per_drone_hour_usd.toFixed(4)}` : "n/a"}
          </div>
        </div>
      </div>
      <p className="mt-1.5 text-[10px] leading-relaxed text-[var(--ink-4)]">{r.basis}</p>
    </Frame>
  );
}

function NavigationNote({ tool, r }: { tool: string; r: any }) {
  const n = r.navigate ?? {};
  return (
    <Frame tool={tool} icon={Compass}>
      <span className="text-[12px] text-[var(--ink-2)]">
        Moved your view to <strong>{n.view}</strong>
        {n.site_id ? ` · ${n.site_id}` : ""}
      </span>
    </Frame>
  );
}

function DecisionTrace({ tool, r }: { tool: string; r: any }) {
  return (
    <Frame tool={tool} icon={Network}>
      {r.gate_explanation && (
        <p className="text-[11.5px] leading-relaxed text-[var(--ink-2)]">{r.gate_explanation}</p>
      )}
      {r.triage_explanation && (
        <p className="mt-1.5 text-[11.5px] leading-relaxed text-[var(--ink-2)]">
          {r.triage_explanation}
        </p>
      )}
      {!r.gate_explanation && !r.triage_explanation && (
        <span className="text-[12px] text-[var(--ink-4)]">{r.message ?? "nothing to explain"}</span>
      )}
    </Frame>
  );
}

function SiteList({ tool, r }: { tool: string; r: any }) {
  return (
    <Frame tool={tool} icon={Globe2} meta={`${r.count} sites`}>
      <div className="space-y-0.5">
        {(r.sites ?? []).map((s: any) => (
          <Link key={s.id} href={`/site/${s.id}`}
                className="flex items-center gap-1.5 rounded px-1 py-0.5 hover:bg-[var(--surface-3)]">
            <span className="flex-1 truncate text-[11.5px]">{s.name}</span>
            <span className="text-[10px] text-[var(--ink-4)]">{s.country}</span>
            {s.simulated ? <SimulatedBadge compact /> : <Pill tone="ok">live</Pill>}
          </Link>
        ))}
      </div>
    </Frame>
  );
}

function GenericResult({ tool, r }: { tool: string; r: any }) {
  const { ok, renders_as, ...rest } = r;
  return (
    <Frame tool={tool} icon={ImageIcon}>
      <pre className="mono max-h-40 overflow-auto text-[10px] leading-relaxed text-[var(--ink-3)]">
        {JSON.stringify(rest, null, 2).slice(0, 1200)}
      </pre>
    </Frame>
  );
}
