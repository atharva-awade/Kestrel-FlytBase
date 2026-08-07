"use client";

import {
  Activity, AlertTriangle, Check, Clock, Globe2, Navigation, Network, Plane,
  Radio, ScrollText, X,
} from "lucide-react";

import { Pill, SeverityChip } from "@/components/ui/primitives";
import { bearing, coords, metres, time } from "@/lib/format";

/**
 * The eight tool results that used to fall through to a raw JSON dump.
 *
 * Every tool in the Python registry declares a `renders_as`, and the switch in
 * `ToolResult.tsx` maps that to a component. Eight values had no case, so asking
 * about telemetry, a zone profile, pipeline stats or a rule change answered with
 * a wall of JSON. That is not "generative UI", it is a stack trace with better
 * punctuation.
 *
 * These read defensively from several shapes, because a tool's payload is the
 * shape its Python handler returns and that is allowed to differ between them.
 */

export function Frame({
  tool, icon: Icon, children, tone = "muted", meta,
}: {
  tool: string;
  icon: any;
  children: React.ReactNode;
  tone?: "muted" | "accent" | "danger";
  meta?: string;
}) {
  return (
    <div
      className="overflow-hidden rounded-xl border"
      style={{
        borderColor:
          tone === "danger"
            ? "color-mix(in oklab, var(--sev-critical) 30%, transparent)"
            : tone === "accent"
              ? "color-mix(in oklab, var(--accent) 30%, transparent)"
              : "var(--line)",
        background: "var(--surface-2)",
      }}
    >
      <div
        className="flex items-center gap-1.5 border-b px-2.5 py-1.5"
        style={{ borderColor: "var(--line)" }}
      >
        <Icon size={11} style={{ color: "var(--accent)" }} />
        <span className="mono text-[10px] text-[var(--ink-3)]">{tool}</span>
        {meta && <span className="mono ml-auto text-[10px] text-[var(--ink-4)]">{meta}</span>}
      </div>
      <div className="p-2.5">{children}</div>
    </div>
  );
}

function Cell({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-lg px-2.5 py-1.5" style={{ background: "var(--surface)" }}>
      <div className="mono text-[9px] tracking-[0.14em] text-[var(--ink-4)] uppercase">{label}</div>
      <div className="tnum mt-0.5 text-[12.5px] font-semibold text-[var(--ink)]">{value}</div>
    </div>
  );
}

export function TelemetryPanel({ tool, r }: { tool: string; r: any }) {
  const t = r.telemetry ?? r.samples?.[0] ?? r;
  return (
    <Frame tool={tool} icon={Radio} meta={t?.ts ? time(t.ts) : undefined}>
      <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-4">
        <Cell label="Altitude" value={t?.altitude_m != null ? `${t.altitude_m} m` : "n/a"} />
        <Cell label="Battery" value={t?.battery_pct != null ? `${t.battery_pct}%` : "n/a"} />
        <Cell label="Wind" value={t?.wind_mps != null ? `${t.wind_mps} m/s` : "n/a"} />
        <Cell label="Light" value={t?.light_lux != null ? `${t.light_lux} lux` : "n/a"} />
        <Cell label="Position" value={t?.lat != null ? coords(t.lat, t.lon) : "n/a"} />
        <Cell
          label="Gimbal"
          value={t?.gimbal_pitch_deg != null ? `${t.gimbal_pitch_deg}° pitch` : "n/a"}
        />
        <Cell label="Heading" value={t?.heading_deg != null ? bearing(t.heading_deg) : "n/a"} />
        <Cell label="GPS HDOP" value={t?.gps_hdop ?? "n/a"} />
      </div>
      <p className="mt-2 text-[11px] text-[var(--ink-4)]">
        Telemetry is simulated: there is no aircraft.
      </p>
    </Frame>
  );
}

export function ZoneProfile({ tool, r }: { tool: string; r: any }) {
  const hours: Record<string, number> = r.by_hour ?? r.hours ?? {};
  const peak = Math.max(1, ...Object.values(hours).map(Number));
  return (
    <Frame tool={tool} icon={Clock} meta={r.zone_id ?? r.zone}>
      <div className="flex h-16 items-stretch gap-[2px]">
        {Array.from({ length: 24 }, (_, h) => {
          const n = Number(hours[String(h)] ?? 0);
          const night = h >= 22 || h < 5;
          return (
            <div key={h} className="flex h-full flex-1 flex-col justify-end">
              <div
                className="w-full rounded-t"
                title={`${String(h).padStart(2, "0")}:00 · ${n}`}
                style={{
                  height: `${n === 0 ? 3 : Math.max(8, (n / peak) * 100)}%`,
                  background:
                    n === 0 ? "var(--surface-3)" : night ? "var(--sev-high)" : "var(--accent)",
                }}
              />
            </div>
          );
        })}
      </div>
      <p className="mt-1.5 text-[11px] leading-relaxed text-[var(--ink-4)]">
        Mean activity by hour. Amber bars are 22:00 to 05:00, the window in which presence is
        what the baseline flags.
      </p>
    </Frame>
  );
}

export function RuleCard({ tool, r }: { tool: string; r: any }) {
  const enabled = r.enabled ?? r.rule?.enabled;
  return (
    <Frame tool={tool} icon={ScrollText} tone={enabled ? "accent" : "muted"}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[13px] font-semibold">
          {r.name ?? r.rule?.name ?? r.rule_id ?? "rule"}
        </span>
        <Pill tone={enabled ? "ok" : "muted"}>{enabled ? "enabled" : "disabled"}</Pill>
      </div>
      {(r.description ?? r.rule?.description) && (
        <p className="mt-1.5 text-[12px] leading-relaxed text-[var(--ink-3)]">
          {r.description ?? r.rule?.description}
        </p>
      )}
      {r.message && <p className="mt-1.5 text-[11.5px] text-[var(--ink-4)]">{r.message}</p>}
    </Frame>
  );
}

export function AlertCard({ tool, r }: { tool: string; r: any }) {
  const a = r.alert ?? r;
  return (
    <Frame tool={tool} icon={AlertTriangle} tone="danger">
      <div className="flex flex-wrap items-center gap-2">
        {a.severity && <SeverityChip severity={a.severity} />}
        <span className="text-[13px] font-semibold">{a.title ?? r.message ?? "alert"}</span>
        {a.status && <Pill tone="muted">{a.status}</Pill>}
      </div>
      {a.location?.lat != null && (
        <div className="mono mt-1.5 text-[11px] text-[var(--ink-4)]">
          {coords(a.location.lat, a.location.lon)}
          {a.location.accuracy_m != null && ` ± ${metres(a.location.accuracy_m)}`}
        </div>
      )}
      {r.message && a.title && (
        <p className="mt-1.5 text-[11.5px] text-[var(--ink-4)]">{r.message}</p>
      )}
    </Frame>
  );
}

export function MissionExecution({ tool, r }: { tool: string; r: any }) {
  const flown = r.executed ?? r.flown;
  return (
    <Frame tool={tool} icon={Plane} tone={flown ? "accent" : "muted"}>
      <div className="flex items-center gap-2">
        {flown ? (
          <Check size={13} style={{ color: "var(--ok)" }} />
        ) : (
          <X size={13} style={{ color: "var(--ink-4)" }} />
        )}
        <span className="text-[13px] font-semibold">
          {flown ? "Mission authorised" : "Mission not executed"}
        </span>
      </div>
      <p className="mt-1.5 text-[12px] leading-relaxed text-[var(--ink-3)]">
        {r.message ?? r.outcome ?? "The decision is recorded in the audit ledger."}
      </p>
      {r.confidence_delta != null && (
        <p className="mono mt-1.5 text-[11px] text-[var(--ink-4)]">
          confidence revised by {r.confidence_delta > 0 ? "+" : ""}
          {r.confidence_delta}
        </p>
      )}
    </Frame>
  );
}

export function PatternList({ tool, r }: { tool: string; r: any }) {
  const items = r.patterns ?? r.matches ?? [];
  return (
    <Frame tool={tool} icon={Network} meta={`${items.length} pattern(s)`}>
      {items.length === 0 ? (
        <p className="text-[12px] text-[var(--ink-4)]">
          No region is alerting inside a common window.
        </p>
      ) : (
        <div className="space-y-1.5">
          {items.slice(0, 6).map((p: any, i: number) => (
            <div
              key={i}
              className="rounded-lg px-2.5 py-1.5"
              style={{ background: "var(--surface)" }}
            >
              <div className="text-[12.5px] font-medium">
                {p.region ?? p.country_name ?? p.label ?? "region"}
              </div>
              <div className="mono mt-0.5 text-[10.5px] text-[var(--ink-4)]">
                {p.sites ?? p.site_count ?? "?"} sites {"·"} {p.alerts ?? "?"} alerts
                {p.window_hours ? ` · ${p.window_hours} h window` : ""}
              </div>
            </div>
          ))}
        </div>
      )}
    </Frame>
  );
}

export function GlobeFocus({ tool, r }: { tool: string; r: any }) {
  return (
    <Frame tool={tool} icon={Globe2} tone="accent">
      <div className="flex items-center gap-2 text-[12.5px]">
        <Navigation size={12} style={{ color: "var(--accent)" }} />
        Focused the globe on{" "}
        <strong>{r.site_name ?? r.site_id ?? r.region ?? r.country ?? "the target"}</strong>
      </div>
      {r.lat != null && (
        <div className="mono mt-1 text-[11px] text-[var(--ink-4)]">{coords(r.lat, r.lon)}</div>
      )}
    </Frame>
  );
}

export function StatsPanel({ tool, r }: { tool: string; r: any }) {
  const g = r.gate ?? r.frames ?? {};
  return (
    <Frame tool={tool} icon={Activity}>
      <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-4">
        <Cell label="Frames seen" value={g.seen ?? r.frames_total ?? "n/a"} />
        <Cell label="Analysed" value={g.analysed ?? r.frames_analysed ?? "n/a"} />
        <Cell label="Skipped" value={g.skipped ?? r.frames_skipped ?? "n/a"} />
        <Cell
          label="Gate efficiency"
          value={g.gate_efficiency != null ? `${Math.round(g.gate_efficiency * 100)}%` : "n/a"}
        />
        <Cell label="Detector" value={r.detector?.backend ?? r.backend ?? "n/a"} />
        <Cell label="Device" value={r.detector?.device ?? "n/a"} />
        <Cell label="Escalations" value={r.escalations ?? r.escalated ?? "n/a"} />
        <Cell
          label="Modelled cost"
          value={r.cost?.modelled_usd != null ? `$${r.cost.modelled_usd.toFixed(5)}` : "n/a"}
        />
      </div>
    </Frame>
  );
}
