"use client";

import { motion } from "framer-motion";
import { Layers, Search, Sparkles } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import {
  Card, Empty, Pill, SectionTitle, Skeleton, fadeUp, stagger,
} from "@/components/ui/primitives";
import { api, type SearchResult } from "@/lib/api";
import { dateTime, time } from "@/lib/format";

const EXAMPLES = [
  "show me all truck events",
  "anyone near the substation after midnight",
  "a person in a high-visibility vest",
  "vehicles at the loading dock last night",
];

function InvestigateInner() {
  const params = useSearchParams();
  const [q, setQ] = useState(params.get("q") ?? "");
  const [res, setRes] = useState<SearchResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [ran, setRan] = useState(false);

  const run = useCallback(async (query: string) => {
    if (!query.trim()) return;
    setBusy(true); setRan(true);
    setRes(await api.search(query, 30));
    setBusy(false);
  }, []);

  useEffect(() => {
    const initial = params.get("q");
    if (initial) void run(initial);
  }, [params, run]);

  return (
    <div className="mx-auto max-w-[1400px] px-5 py-6">
      <motion.div {...fadeUp}>
        <SectionTitle
          eyebrow="Cross-domain indexing"
          title="Investigate"
          subtitle="Structured filters, caption vectors and joint image/text vectors, fused by rank. The plan is shown because a retrieval system you cannot audit is one you cannot trust."
        />
      </motion.div>

      <motion.div {...stagger(1)}>
        <Card className="p-3">
          <div className="flex items-center gap-2 rounded-lg border px-3 py-2"
               style={{ borderColor: "var(--line)", background: "var(--surface-2)" }}>
            <Search size={15} className="text-[var(--ink-4)]" />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && run(q)}
              placeholder="Ask in plain English…"
              className="flex-1 bg-transparent text-[14px] outline-none placeholder:text-[var(--ink-4)]"
            />
            <button
              onClick={() => run(q)}
              disabled={busy || !q.trim()}
              className="rounded-lg px-3 py-1.5 text-[12.5px] font-semibold text-white disabled:opacity-40"
              style={{ background: "var(--accent)" }}
            >
              {busy ? "Searching…" : "Search"}
            </button>
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {EXAMPLES.map((e) => (
              <button key={e} onClick={() => { setQ(e); void run(e); }}
                      className="rounded-full border px-2.5 py-1 text-[11.5px] text-[var(--ink-3)] transition-colors hover:bg-[var(--surface-2)]"
                      style={{ borderColor: "var(--line)" }}>
                {e}
              </button>
            ))}
          </div>
        </Card>
      </motion.div>

      {busy && (
        <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {[0, 1, 2, 3, 4, 5, 6, 7].map((i) => <Skeleton key={i} className="h-44" />)}
        </div>
      )}

      {!busy && res && (
        <div className="mt-4 grid gap-4 lg:grid-cols-[300px_1fr]">
          <motion.div {...stagger(2)}>
            <Card className="p-3.5">
              <div className="mb-2 flex items-center gap-1.5">
                <Layers size={13} style={{ color: "var(--accent)" }} />
                <span className="text-[13px] font-semibold">Query plan</span>
              </div>
              <Pill tone="accent">{res.plan.intent}</Pill>
              {res.plan.reasoning && (
                <p className="mt-2 text-[11.5px] leading-relaxed text-[var(--ink-3)]">
                  {res.plan.reasoning}
                </p>
              )}
              <ol className="mt-2.5 space-y-1.5">
                {res.plan_steps.map((s, i) => (
                  <li key={i} className="flex gap-2 text-[11.5px] leading-snug text-[var(--ink-2)]">
                    <span className="grid h-4 w-4 shrink-0 place-items-center rounded-full text-[9px] font-bold"
                          style={{ background: "var(--accent-soft)", color: "var(--accent-ink)" }}>
                      {i + 1}
                    </span>
                    {s}
                  </li>
                ))}
              </ol>
              <div className="mt-3 border-t pt-2.5" style={{ borderColor: "var(--line)" }}>
                <div className="eyebrow mb-1.5">retrievers</div>
                {Object.entries(res.counts).map(([k, v]) => (
                  <div key={k} className="flex items-center justify-between text-[11.5px]">
                    <span className="text-[var(--ink-3)]">{k}</span>
                    <span className="tnum text-[var(--ink)]">{v}</span>
                  </div>
                ))}
                {Object.entries(res.degraded ?? {}).map(([k, why]) => (
                  <div key={k} className="flex items-center justify-between text-[11.5px]">
                    <span className="text-[var(--ink-3)]">{k}</span>
                    <span className="sev-high sev-chip rounded px-1.5 py-0.5 text-[10px]">
                      unavailable
                    </span>
                  </div>
                ))}
                <div className="mt-2 text-[10.5px] text-[var(--ink-4)]">
                  fused by reciprocal rank in {res.took_ms.toFixed(0)}ms
                </div>
              </div>
            </Card>
          </motion.div>

          <motion.div {...stagger(3)}>
            {res.hits.length === 0 ? (
              /* An empty result and an unavailable retriever are different claims,
                 and conflating them is how a system quietly loses trust. */
              Object.keys(res.degraded ?? {}).length > 0 ? (
                <Empty
                  title="Search ran incomplete"
                  hint={`Could not run: ${Object.entries(res.degraded ?? {})
                    .map(([k, why]) => `${k} (${why})`)
                    .join(", ")}. This is not the same as finding nothing, so no result is being reported as final.`}
                />
              ) : (
                <Empty
                  title="No frames matched"
                  hint="Nothing was invented to fill the gap. If no session has been ingested, run `uv run kestrel ingest` first."
                />
              )
            ) : (
              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                {res.hits.map((h, i) => (
                  <motion.div key={h.frame_id} {...stagger(i, 0.02)}>
                    <Card lift className="overflow-hidden">
                      <div className="relative aspect-video" style={{ background: "var(--surface-3)" }}>
                        {/* eslint-disable-next-line @next/next/no-img-element */}
                        <img src={api.frameImage(h.frame_id)} alt=""
                             className="h-full w-full object-cover"
                             onError={(e) => ((e.target as HTMLImageElement).style.opacity = "0")} />
                        <div className="absolute left-1.5 top-1.5 flex gap-1">
                          {h.sources.map((s) => (
                            <span key={s} className="glass rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase"
                                  style={{ color: "var(--accent-ink)" }}>
                              {s}
                            </span>
                          ))}
                        </div>
                      </div>
                      <div className="p-2.5">
                        <div className="flex items-center gap-1.5">
                          <span className="mono text-[10px] text-[var(--ink-4)]">{dateTime(h.ts)}</span>
                          {h.zone_id && <Pill tone="muted">{h.zone_id}</Pill>}
                        </div>
                        <p className="mt-1 line-clamp-3 text-[12px] leading-snug text-[var(--ink-2)]">
                          {h.caption || "no caption"}
                        </p>
                        {h.labels.length > 0 && (
                          <div className="mt-1.5 flex flex-wrap gap-1">
                            {h.labels.slice(0, 4).map((l) => (
                              <span key={l} className="rounded px-1.5 py-0.5 text-[9.5px]"
                                    style={{ background: "var(--accent-soft)", color: "var(--accent-ink)" }}>
                                {l}
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                    </Card>
                  </motion.div>
                ))}
              </div>
            )}
          </motion.div>
        </div>
      )}

      {!busy && !ran && (
        <div className="mt-6">
          <Card className="p-5">
            <div className="flex items-start gap-3">
              <Sparkles size={16} style={{ color: "var(--accent)" }} className="mt-0.5 shrink-0" />
              <div>
                <div className="text-[13.5px] font-semibold">Three indexes, one answer</div>
                <p className="mt-1 max-w-2xl text-[12.5px] leading-relaxed text-[var(--ink-3)]">
                  <strong>Structured</strong> SQL answers what is genuinely a filter.{" "}
                  <strong>Caption vectors</strong> answer semantic questions.{" "}
                  <strong>Joint image/text vectors</strong> answer questions about appearance:
                  “a white pickup” finds frames whose captions never used those words.
                  Results are fused by reciprocal rank, which combines rankings rather
                  than scores, so two retrievers with incomparable similarity scales
                  can be merged without inventing a normalisation.
                </p>
              </div>
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}

export default function InvestigatePage() {
  return (
    <Suspense fallback={<div className="p-8"><Skeleton className="h-40" /></div>}>
      <InvestigateInner />
    </Suspense>
  );
}
