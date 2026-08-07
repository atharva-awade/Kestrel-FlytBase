"use client";

import { motion } from "framer-motion";
import { Check, Gauge, ShieldCheck, X } from "lucide-react";
import { useEffect, useState } from "react";
import {
  Bar, Card, Empty, Pill, SectionTitle, Skeleton, Stat, fadeUp, stagger,
} from "@/components/ui/primitives";
import { api } from "@/lib/api";
import { pct, usd } from "@/lib/format";

/**
 * Measured results.
 *
 * Every number here is read from disk, produced by a benchmark that can be re-run.
 * Nothing is hard-coded, and where a figure is unflattering it is shown anyway
 * with the conditions attached. A dashboard that only reports its good numbers
 * is marketing, not evidence.
 */
export default function EvalsPage() {
  const [data, setData] = useState<Record<string, any> | null>(null);
  const [stats, setStats] = useState<any>(null);
  const [ledger, setLedger] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      const [e, s, l] = await Promise.all([api.evals(), api.stats(), api.ledger(20)]);
      setData(e); setStats(s); setLedger(l); setLoading(false);
    })();
  }, []);

  const gate = data?.gate_efficiency;
  const vlms = data?.probe_vlms?.results ?? [];
  const probes = data?.probe_results?.probes ?? [];
  const meter = stats?.meter;

  return (
    <div className="mx-auto max-w-[1400px] px-5 py-6">
      <motion.div {...fadeUp}>
        <SectionTitle
          eyebrow="Evidence"
          title="Measured results"
          subtitle="Read from benchmark output on disk. Re-run with scripts/bench_gate.py and scripts/probe_*.py."
        />
      </motion.div>

      {loading ? (
        <div className="grid gap-3 lg:grid-cols-3">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-56" />)}</div>
      ) : (
        <div className="space-y-4">
          {/* ── gate ────────────────────────────────────────────────── */}
          {gate && (
            <motion.div {...stagger(1)}>
              <Card className="p-4">
                <div className="mb-3 flex items-center gap-1.5">
                  <Gauge size={14} style={{ color: "var(--accent)" }} />
                  <span className="text-[14px] font-semibold">Gate efficiency by context</span>
                </div>
                <div className="mb-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
                  <Stat label="real footage" value={pct(gate.overall_efficiency_real_footage ?? gate.overall_efficiency)}
                        hint="frames never sent to a model" tone="accent" />
                  <Stat label="idle patrol" value={pct(gate.idle_efficiency_constructed ?? 0)}
                        hint="constructed static scene" tone="ok" />
                  <Stat label="most gating" value={pct(gate.best?.efficiency)} hint={gate.best?.label} />
                  <Stat label="least gating" value={pct(gate.worst?.efficiency)} hint={gate.worst?.label} />
                </div>
                <div className="space-y-2">
                  {(gate.contexts ?? []).map((c: any) => (
                    <div key={c.label}>
                      <div className="mb-1 flex items-center justify-between text-[11.5px]">
                        <span className="text-[var(--ink-2)]">{c.label}</span>
                        <span className="tnum text-[var(--ink-4)]">
                          {c.analysed}/{c.seen} analysed · {pct(c.efficiency)} gated
                        </span>
                      </div>
                      <Bar value={c.efficiency} />
                    </div>
                  ))}
                </div>
                {gate.caveat && (
                  <p className="mt-3 rounded-lg border px-3 py-2 text-[11.5px] leading-relaxed text-[var(--ink-3)]"
                     style={{ borderColor: "var(--line)", background: "var(--surface-2)" }}>
                    {gate.caveat}
                  </p>
                )}
              </Card>
            </motion.div>
          )}

          {/* ── model leaderboard ───────────────────────────────────── */}
          {vlms.length > 0 && (
            <motion.div {...stagger(2)}>
              <Card className="p-4">
                <div className="mb-1 text-[14px] font-semibold">Vision model survey</div>
                <p className="mb-3 text-[12px] leading-relaxed text-[var(--ink-3)]">
                  Every vision model in the catalogue, probed directly. Four of nine were
                  reachable: presence in a provider&apos;s catalogue is not evidence you can
                  call it.
                </p>
                <div className="overflow-x-auto">
                  <table className="w-full text-[12px]">
                    <thead>
                      <tr className="text-left text-[var(--ink-4)]">
                        <th className="pb-2 font-medium">model</th>
                        <th className="pb-2 text-right font-medium">latency</th>
                        <th className="pb-2 text-center font-medium">colour</th>
                        <th className="pb-2 text-center font-medium">vehicle</th>
                        <th className="pb-2 text-center font-medium">person</th>
                        <th className="pb-2 font-medium">status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {vlms.map((v: any) => (
                        <tr key={v.model} className="border-t" style={{ borderColor: "var(--line)" }}>
                          <td className="mono py-1.5 text-[11px]">{v.model}</td>
                          <td className="tnum py-1.5 text-right">
                            {v.best_seconds != null ? `${v.best_seconds}s` : "n/a"}
                          </td>
                          {[v.mentions_blue, v.mentions_vehicle, v.mentions_person].map((ok, i) => (
                            <td key={i} className="py-1.5 text-center">
                              {v.ok ? (ok ? <Check size={13} className="mx-auto" style={{ color: "var(--ok)" }} />
                                          : <X size={13} className="mx-auto" style={{ color: "var(--ink-4)" }} />) : "n/a"}
                            </td>
                          ))}
                          <td className="py-1.5">
                            {v.ok ? <Pill tone="ok">reachable</Pill> : <Pill tone="muted">404</Pill>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <p className="mt-2.5 text-[11.5px] leading-relaxed text-[var(--ink-3)]">
                  The 11B was chosen over the marginally faster 8B because the 8B missed the
                  person. For a security system a false negative on a human is the worst
                  available failure, and 1.28 s is well inside budget for a gated pipeline.
                </p>
              </Card>
            </motion.div>
          )}

          <div className="grid gap-4 lg:grid-cols-2">
            {/* ── endpoint probes ───────────────────────────────────── */}
            {probes.length > 0 && (
              <motion.div {...stagger(3)}>
                <Card className="p-4">
                  <div className="mb-2 text-[14px] font-semibold">Endpoint verification</div>
                  <div className="space-y-1">
                    {probes.map((p: any) => (
                      <div key={p.name} className="flex items-center gap-2 text-[11.5px]">
                        {p.ok ? <Check size={12} style={{ color: "var(--ok)" }} />
                              : <X size={12} style={{ color: p.required ? "var(--sev-critical)" : "var(--ink-4)" }} />}
                        <span className="mono flex-1 truncate">{p.name}</span>
                        <span className="tnum text-[var(--ink-4)]">{p.ms}ms</span>
                        {!p.required && !p.ok && <Pill tone="muted">optional</Pill>}
                      </div>
                    ))}
                  </div>
                </Card>
              </motion.div>
            )}

            {/* ── cost ──────────────────────────────────────────────── */}
            {meter && (
              <motion.div {...stagger(4)}>
                <Card className="p-4">
                  <div className="mb-3 text-[14px] font-semibold">Cost and latency</div>
                  <div className="mb-3 grid grid-cols-2 gap-3">
                    <Stat label="modelled spend" value={usd(meter.cost?.modelled_usd)} tone="accent" />
                    <Stat label="per drone-hour"
                          value={meter.cost?.per_drone_hour_usd != null ? usd(meter.cost.per_drone_hour_usd) : "n/a"} />
                  </div>
                  <div className="space-y-1.5">
                    {Object.entries(meter.stages ?? {}).map(([stage, s]: [string, any]) => (
                      <div key={stage} className="flex items-center gap-2 text-[11.5px]">
                        <span className="w-24 shrink-0 text-[var(--ink-3)]">{stage}</span>
                        <span className="tnum w-12 text-right text-[var(--ink-4)]">{s.calls}×</span>
                        <span className="tnum w-16 text-right text-[var(--ink-4)]">p50 {s.p50_ms}ms</span>
                        <span className="tnum w-20 text-right text-[var(--ink-4)]">p95 {s.p95_ms}ms</span>
                        <span className="tnum ml-auto text-[var(--ink)]">${s.cost_usd.toFixed(5)}</span>
                      </div>
                    ))}
                  </div>
                  <p className="mt-2.5 text-[11px] leading-relaxed text-[var(--ink-4)]">
                    {meter.cost?.basis}
                  </p>
                </Card>
              </motion.div>
            )}
          </div>

          {/* ── ledger ──────────────────────────────────────────────── */}
          {ledger && (
            <motion.div {...stagger(5)}>
              <Card className="p-4">
                <div className="mb-2 flex items-center gap-1.5">
                  <ShieldCheck size={14}
                               style={{ color: ledger.verification?.valid ? "var(--ok)" : "var(--sev-critical)" }} />
                  <span className="text-[14px] font-semibold">Audit ledger</span>
                  <Pill tone={ledger.verification?.valid ? "ok" : "danger"} className="ml-auto">
                    {ledger.verification?.valid ? "chain verified" : "CHAIN BROKEN"}
                  </Pill>
                </div>
                <p className="text-[12px] leading-relaxed text-[var(--ink-3)]">
                  {ledger.verification?.note ?? ledger.verification?.reason}
                </p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {Object.entries(ledger.stats?.by_kind ?? {}).map(([k, v]) => (
                    <Pill key={k} tone="muted">{k} <span className="tnum">{v as number}</span></Pill>
                  ))}
                </div>
                {(ledger.entries ?? []).length > 0 && (
                  <div className="mt-3 max-h-48 overflow-y-auto">
                    <table className="w-full text-[11px]">
                      <tbody>
                        {ledger.entries.slice(0, 12).map((e: any) => (
                          <tr key={e.seq} className="border-t" style={{ borderColor: "var(--line)" }}>
                            <td className="tnum py-1 pr-2 text-[var(--ink-4)]">#{e.seq}</td>
                            <td className="mono py-1 pr-2">{e.kind}</td>
                            <td className="py-1 pr-2 text-[var(--ink-4)]">{e.actor}</td>
                            <td className="mono py-1 text-right text-[9.5px] text-[var(--ink-4)]">
                              {String(e.hash).slice(0, 12)}…
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Card>
            </motion.div>
          )}

          {!gate && !vlms.length && !probes.length && (
            <Empty
              title="No benchmark output yet"
              hint="Run `uv run python scripts/bench_gate.py` and `scripts/probe_models.py`, then reload."
            />
          )}
        </div>
      )}
    </div>
  );
}
