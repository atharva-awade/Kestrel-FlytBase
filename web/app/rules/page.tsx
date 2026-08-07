"use client";

import { motion } from "framer-motion";
import { Eye, FlaskConical, ScrollText, Sparkles, Wand2 } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import {
  Card, Empty, Pill, SectionTitle, SeverityChip, Skeleton, fadeUp, stagger,
} from "@/components/ui/primitives";
import { api, type Rule } from "@/lib/api";

/**
 * Rules Studio.
 *
 * The headline capability: type a requirement in English, get a validated rule,
 * and see what it *would have done* against indexed history before it is allowed
 * to do anything. A rule that would have fired 400 times yesterday is a bad rule,
 * and the operator finds that out here rather than at 3am.
 */
export default function RulesPage() {
  const [rules, setRules] = useState<Rule[]>([]);
  const [loading, setLoading] = useState(true);
  const [text, setText] = useState("");
  const [compiling, setCompiling] = useState(false);
  const [draft, setDraft] = useState<any>(null);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      const r = await api.rules();
      setRules(r?.rules ?? []);
      setLoading(false);
    })();
  }, []);

  const compile = async () => {
    if (!text.trim()) return;
    setCompiling(true);
    setDraft(null);
    try {
      const turn = await api.ask(
        `Compile this into a rule and backtest it, but do not enable it: "${text}"`,
      );
      const call = turn.tool_calls.find((c) => c.tool === "compile_rule_from_text");
      if (call?.result?.ok) setDraft(call.result);
      else toast.error((call?.result?.error as string) ?? "Could not compile that rule");
    } catch {
      toast.error("Could not reach the API");
    } finally {
      setCompiling(false);
    }
  };

  const enable = async () => {
    if (!draft?.rule) return;
    try {
      const r = await api.confirm("enable_rule",
        { rule_id: draft.rule.id, yaml: draft.yaml }, true);
      if (r.executed) {
        toast.success(`Rule "${draft.rule.name}" is now active`);
        const fresh = await api.rules();
        setRules(fresh?.rules ?? []);
        setDraft(null);
        setText("");
      }
    } catch {
      toast.error("Could not enable the rule");
    }
  };

  const active = rules.find((r) => r.id === selected);

  return (
    <div className="mx-auto max-w-[1400px] px-5 py-6">
      <motion.div {...fadeUp}>
        <SectionTitle
          eyebrow="Declarative detection"
          title="Rules Studio"
          subtitle="Rules are data, not code, so a language model can author them and the engine can backtest them before they ever raise an alert."
          right={<Pill tone="accent">{rules.filter((r) => r.enabled).length} active</Pill>}
        />
      </motion.div>

      {/* ── compiler ─────────────────────────────────────────────────── */}
      <motion.div {...stagger(1)}>
        <Card className="p-4" style={{ borderColor: "color-mix(in oklab, var(--accent) 28%, transparent)" }}>
          <div className="mb-2 flex items-center gap-1.5">
            <Wand2 size={14} style={{ color: "var(--accent)" }} />
            <span className="text-[13.5px] font-semibold">Write a rule in plain English</span>
          </div>
          <div className="flex flex-col gap-2 sm:flex-row">
            <input
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && compile()}
              placeholder="alert me if a truck parks at the loading dock for more than 10 minutes after 9pm"
              className="flex-1 rounded-lg border px-3 py-2 text-[13px] outline-none placeholder:text-[var(--ink-4)]"
              style={{ borderColor: "var(--line)", background: "var(--surface-2)" }}
            />
            <button
              onClick={compile}
              disabled={compiling || !text.trim()}
              className="rounded-lg px-4 py-2 text-[12.5px] font-semibold text-white disabled:opacity-40"
              style={{ background: "var(--accent)" }}
            >
              {compiling ? "Compiling…" : "Compile & backtest"}
            </button>
          </div>
          <p className="mt-2 text-[11.5px] leading-relaxed text-[var(--ink-4)]">
            The compiled rule is validated against this site&apos;s real zone list. A rule
            naming a zone that does not exist would validate and then silently never
            fire, which looks exactly like working.
          </p>
        </Card>
      </motion.div>

      {compiling && <Skeleton className="mt-4 h-56" />}

      {draft && (
        <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} className="mt-4">
          <Card className="p-4">
            <div className="grid gap-4 lg:grid-cols-2">
              <div>
                <div className="mb-1.5 flex items-center gap-2">
                  <SeverityChip severity={draft.rule.severity} />
                  <span className="text-[14px] font-semibold">{draft.rule.name}</span>
                </div>
                <p className="text-[12.5px] leading-relaxed text-[var(--ink-3)]">
                  {draft.rule.description}
                </p>
                <div className="eyebrow mt-3 mb-1.5">fires when all of these hold</div>
                <ul className="space-y-1">
                  {(draft.explanation ?? []).map((c: string, i: number) => (
                    <li key={i} className="flex gap-2 text-[12px] text-[var(--ink-2)]">
                      <span style={{ color: "var(--accent)" }}>·</span>{c}
                    </li>
                  ))}
                </ul>
                {draft.rule.visual_predicate && (
                  <div className="mt-2.5 rounded-lg border px-2.5 py-2"
                       style={{ borderColor: "var(--line)", background: "var(--surface-2)" }}>
                    <div className="flex items-center gap-1.5">
                      <Eye size={11} style={{ color: "var(--accent)" }} />
                      <span className="eyebrow">open-vocabulary predicate</span>
                    </div>
                    <p className="mt-1 text-[12px] italic text-[var(--ink-2)]">
                      “{draft.rule.visual_predicate}”
                    </p>
                    <p className="mt-1 text-[10.5px] leading-relaxed text-[var(--ink-4)]">
                      Detected without training and without a fixed class list.
                    </p>
                  </div>
                )}
              </div>

              <div>
                <div className="mb-1.5 flex items-center gap-1.5">
                  <FlaskConical size={13} style={{ color: "var(--sev-medium)" }} />
                  <span className="text-[13px] font-semibold">Backtest against history</span>
                </div>
                <div className="rounded-lg border p-3"
                     style={{
                       borderColor: draft.backtest.fire_count > 0
                         ? "color-mix(in oklab, var(--sev-medium) 34%, transparent)"
                         : "var(--line)",
                       background: "var(--surface-2)",
                     }}>
                  <p className="text-[12.5px] leading-relaxed">{draft.backtest.verdict}</p>
                  <div className="mt-2 grid grid-cols-3 gap-2 text-center">
                    {[["frames", draft.backtest.frames_replayed],
                      ["days", draft.backtest.days_covered],
                      ["fires", draft.backtest.fire_count]].map(([k, v]) => (
                      <div key={k as string}>
                        <div className="tnum text-[17px] font-semibold">{v as number}</div>
                        <div className="eyebrow">{k as string}</div>
                      </div>
                    ))}
                  </div>
                </div>
                {(draft.backtest.hits ?? []).length > 0 && (
                  <div className="mt-2 max-h-32 space-y-0.5 overflow-y-auto">
                    {draft.backtest.hits.slice(0, 8).map((h: any, i: number) => (
                      <div key={i} className="mono text-[10.5px] text-[var(--ink-4)]">
                        would fire {new Date(h.ts).toLocaleString("en-GB")} · {h.label} in {h.zone_id ?? "?"}
                      </div>
                    ))}
                  </div>
                )}
                <div className="mt-3 flex gap-2">
                  <button onClick={enable}
                          className="flex-1 rounded-lg px-3 py-2 text-[12.5px] font-semibold text-white"
                          style={{ background: "var(--accent)" }}>
                    Enable this rule
                  </button>
                  <button onClick={() => setDraft(null)}
                          className="rounded-lg border px-3 py-2 text-[12.5px]"
                          style={{ borderColor: "var(--line)" }}>
                    Discard
                  </button>
                </div>
                <p className="mt-1.5 text-[10.5px] leading-relaxed text-[var(--ink-4)]">
                  Enabling is a gated action: KESTREL cannot do it, and your decision is
                  written to the audit ledger.
                </p>
              </div>
            </div>
          </Card>
        </motion.div>
      )}

      {/* ── pack ─────────────────────────────────────────────────────── */}
      <motion.div {...stagger(2)} className="mt-5 grid gap-4 lg:grid-cols-[1fr_380px]">
        <Card className="p-2.5">
          {loading ? (
            <div className="space-y-2">{[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-14" />)}</div>
          ) : rules.length === 0 ? (
            <Empty title="No rules loaded" hint="Start the API to load the default pack." />
          ) : (
            <div className="space-y-1">
              {rules.map((r) => (
                <button
                  key={r.id}
                  onClick={() => setSelected(r.id === selected ? null : r.id)}
                  className="w-full rounded-lg border p-2.5 text-left transition-colors hover:bg-[var(--surface-2)]"
                  style={{
                    borderColor: selected === r.id ? "var(--accent)" : "var(--line)",
                    opacity: r.enabled ? 1 : 0.55,
                  }}
                >
                  <div className="flex flex-wrap items-center gap-1.5">
                    <SeverityChip severity={r.severity} />
                    <span className="text-[13px] font-medium">{r.name}</span>
                    {r.origin === "natural_language" && <Pill tone="accent">from English</Pill>}
                    {!r.enabled && <Pill tone="muted">disabled</Pill>}
                    {r.fires > 0 && (
                      <span className="tnum ml-auto text-[11px] text-[var(--ink-4)]">
                        fired {r.fires}×
                      </span>
                    )}
                  </div>
                  <p className="mt-1 text-[12px] leading-relaxed text-[var(--ink-3)]">
                    {r.description}
                  </p>
                </button>
              ))}
            </div>
          )}
        </Card>

        <div className="space-y-3">
          {active ? (
            <Card className="p-3.5">
              <div className="mb-2 flex items-center gap-1.5">
                <ScrollText size={13} style={{ color: "var(--accent)" }} />
                <span className="text-[13px] font-semibold">{active.name}</span>
              </div>
              <div className="eyebrow mb-1.5">conditions</div>
              <ul className="space-y-1">
                {active.conditions.map((c, i) => (
                  <li key={i} className="flex gap-2 text-[11.5px] text-[var(--ink-2)]">
                    <span style={{ color: "var(--accent)" }}>·</span>{c}
                  </li>
                ))}
              </ul>
              <div className="eyebrow mb-1 mt-3">yaml</div>
              <pre className="mono max-h-64 overflow-auto rounded-lg border p-2 text-[10px] leading-relaxed"
                   style={{ borderColor: "var(--line)", background: "var(--surface-2)" }}>
                {active.yaml}
              </pre>
            </Card>
          ) : (
            <Card className="p-4">
              <div className="flex items-start gap-2.5">
                <Sparkles size={15} style={{ color: "var(--accent)" }} className="mt-0.5 shrink-0" />
                <div>
                  <div className="text-[13px] font-semibold">Why rules are temporal</div>
                  <p className="mt-1 text-[12px] leading-relaxed text-[var(--ink-3)]">
                    Loitering is dwell over time. Tailgating is one event following another
                    within a window. An unattended object is a thing that persists after its
                    owner leaves. None of those can be expressed by a per-frame check, which
                    is why the engine keeps per-entity state and the language has real
                    temporal operators.
                  </p>
                </div>
              </div>
            </Card>
          )}
        </div>
      </motion.div>
    </div>
  );
}
