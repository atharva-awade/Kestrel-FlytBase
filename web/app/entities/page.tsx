"use client";

import { motion } from "framer-motion";
import { Boxes, Clock, MapPin } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import {
  Card, Empty, KeyValue, Pill, SectionTitle, Skeleton, Stat, fadeUp, stagger,
} from "@/components/ui/primitives";
import { api, type Detection, type Entity } from "@/lib/api";
import { ago, dateTime, time } from "@/lib/format";

/**
 * Entities: the difference between a caption bot and an analyst.
 *
 * "A person was detected" is an observation. "The same vehicle, seventh visit,
 * first time ever after midnight" is a finding, and it requires identity that
 * survives across frames, sessions and days.
 */
function EntitiesInner() {
  const params = useSearchParams();
  const [entities, setEntities] = useState<Entity[]>([]);
  const [selected, setSelected] = useState<string | null>(params.get("entity"));
  const [detail, setDetail] = useState<{ entity: Entity; sightings: Detection[] } | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      const r = await api.entities("plant-01", 200);
      setEntities(r?.entities ?? []);
      setLoading(false);
      if (!selected && r?.entities?.length) setSelected(r.entities[0].id);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!selected) return;
    (async () => setDetail(await api.entity(selected)))();
  }, [selected]);

  const zoneCounts = (detail?.sightings ?? []).reduce<Record<string, number>>((acc, s) => {
    if (s.zone_id) acc[s.zone_id] = (acc[s.zone_id] ?? 0) + 1;
    return acc;
  }, {});

  const hourHistogram = (detail?.sightings ?? []).reduce<Record<number, number>>((acc, s) => {
    const h = new Date(s.ts).getHours();
    acc[h] = (acc[h] ?? 0) + 1;
    return acc;
  }, {});
  const peakHour = Math.max(1, ...Object.values(hourHistogram));

  return (
    <div className="mx-auto max-w-[1400px] px-5 py-6">
      <motion.div {...fadeUp}>
        <SectionTitle
          eyebrow="Persistent memory"
          title="Entities"
          subtitle="Subjects that persist across frames, sessions and days, matched by appearance, attributes and spatio-temporal plausibility."
          right={<Pill tone="accent">{entities.length} tracked</Pill>}
        />
      </motion.div>

      {loading ? (
        <div className="grid gap-3 lg:grid-cols-[320px_1fr]">
          <Skeleton className="h-96" /><Skeleton className="h-96" />
        </div>
      ) : entities.length === 0 ? (
        <Empty
          title="No entities yet"
          hint="Entities appear once a session has been ingested. Run `uv run kestrel ingest --clip worker-zone`."
        />
      ) : (
        <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
          <motion.div {...stagger(1)}>
            <Card className="p-2.5">
              <div className="max-h-[600px] space-y-1 overflow-y-auto">
                {entities.map((e) => (
                  <button
                    key={e.id}
                    onClick={() => setSelected(e.id)}
                    className="flex w-full items-center gap-2 rounded-lg border px-2 py-1.5 text-left transition-colors hover:bg-[var(--surface-2)]"
                    style={{
                      borderColor: selected === e.id ? "var(--accent)" : "var(--line)",
                      background: selected === e.id ? "var(--accent-soft)" : undefined,
                    }}
                  >
                    <span className="grid h-7 w-7 shrink-0 place-items-center rounded-lg text-[10px] font-bold"
                          style={{ background: "var(--surface-3)", color: "var(--ink-3)" }}>
                      {e.kind.slice(0, 2).toUpperCase()}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[12.5px] font-medium">
                        {e.descriptor || e.label}
                      </span>
                      <span className="mono block truncate text-[9.5px] text-[var(--ink-4)]">
                        {e.id}
                      </span>
                    </span>
                    <span className="tnum shrink-0 text-[11px] font-semibold"
                          style={{ color: "var(--accent)" }}>
                      {e.visit_count}
                    </span>
                  </button>
                ))}
              </div>
            </Card>
          </motion.div>

          <motion.div {...stagger(2)} className="space-y-4">
            {detail && (
              <>
                <Card className="p-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <div className="text-[19px] font-semibold tracking-[-0.02em]">
                        {detail.entity.descriptor || detail.entity.label}
                      </div>
                      <div className="mono text-[11px] text-[var(--ink-4)]">{detail.entity.id}</div>
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      <Pill tone="accent">{detail.entity.kind}</Pill>
                      {Object.entries(detail.entity.attributes ?? {}).slice(0, 3).map(([k, v]) => (
                        <Pill key={k} tone="muted">{k}: {v}</Pill>
                      ))}
                    </div>
                  </div>

                  <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
                    <Stat label="visits" value={detail.entity.visit_count} tone="accent" />
                    <Stat label="frames" value={detail.entity.frame_count} />
                    <Stat label="zones" value={detail.entity.zones?.length ?? 0} />
                    <Stat label="sites" value={detail.entity.sites?.length ?? 0}
                          hint={detail.entity.sites?.length > 1 ? "cross-site" : undefined}
                          tone={detail.entity.sites?.length > 1 ? "warn" : "default"} />
                  </div>

                  <div className="mt-4 grid gap-4 sm:grid-cols-2">
                    <KeyValue items={[
                      ["first seen", dateTime(detail.entity.first_seen)],
                      ["last seen", `${dateTime(detail.entity.last_seen)} (${ago(detail.entity.last_seen)})`],
                      ["sightings", detail.sightings.length],
                    ]} />
                    <div>
                      <div className="eyebrow mb-1.5">zones visited</div>
                      <div className="flex flex-wrap gap-1">
                        {Object.entries(zoneCounts).map(([z, n]) => (
                          <span key={z} className="rounded-full border px-2 py-0.5 text-[11px]"
                                style={{ borderColor: "var(--line)" }}>
                            {z} <span className="tnum text-[var(--ink-4)]">×{n}</span>
                          </span>
                        ))}
                        {Object.keys(zoneCounts).length === 0 && (
                          <span className="text-[11.5px] text-[var(--ink-4)]">none resolved</span>
                        )}
                      </div>
                    </div>
                  </div>
                </Card>

                <Card className="p-4">
                  <div className="mb-3 flex items-center gap-1.5">
                    <Clock size={13} style={{ color: "var(--accent)" }} />
                    <span className="text-[13px] font-semibold">When this subject appears</span>
                  </div>
                  {/* Each column is a full-height flex track so the bar's percentage
                      height has something definite to resolve against. Previously the
                      column was auto-height (its content was the bar plus a label), so
                      every percentage collapsed and the chart rendered as an empty
                      strip no matter how many sightings there were. The hour labels
                      now sit outside the measured area for the same reason. */}
                  <div className="flex h-20 items-stretch gap-[3px]">
                    {Array.from({ length: 24 }, (_, h) => {
                      const n = hourHistogram[h] ?? 0;
                      const night = h >= 22 || h < 5;
                      return (
                        <div key={h} className="flex h-full flex-1 flex-col justify-end">
                          <div
                            className="w-full rounded-t transition-all"
                            style={{
                              height: `${n === 0 ? 3 : Math.max(8, (n / peakHour) * 100)}%`,
                              background: n === 0 ? "var(--surface-3)"
                                : night ? "var(--sev-high)" : "var(--accent)",
                              opacity: n === 0 ? 0.6 : 1,
                            }}
                            title={`${String(h).padStart(2, "0")}:00 · ${n} sighting(s)`}
                          />
                        </div>
                      );
                    })}
                  </div>
                  <div className="mt-1 flex gap-[3px]">
                    {Array.from({ length: 24 }, (_, h) => (
                      <span
                        key={h}
                        className="mono flex-1 text-center text-[8.5px] text-[var(--ink-4)]"
                      >
                        {h % 6 === 0 ? h : ""}
                      </span>
                    ))}
                  </div>
                  <p className="mt-2 text-[11px] leading-relaxed text-[var(--ink-4)]">
                    Bars between 22:00 and 05:00 are shown in amber. An otherwise routine
                    subject appearing in that window is what the baseline model flags. No
                    single frame is alarming, only the pattern.
                  </p>
                </Card>

                <Card className="p-4">
                  <div className="mb-2 flex items-center gap-1.5">
                    <MapPin size={13} style={{ color: "var(--accent)" }} />
                    <span className="text-[13px] font-semibold">Recent sightings</span>
                  </div>
                  <div className="max-h-72 overflow-y-auto">
                    <table className="w-full text-[11.5px]">
                      <thead>
                        <tr className="text-left text-[var(--ink-4)]">
                          <th className="pb-1.5 font-medium">time</th>
                          <th className="pb-1.5 font-medium">zone</th>
                          <th className="pb-1.5 font-medium">label</th>
                          <th className="pb-1.5 text-right font-medium">conf</th>
                          <th className="pb-1.5 text-right font-medium">position</th>
                        </tr>
                      </thead>
                      <tbody>
                        {detail.sightings.slice(0, 40).map((s) => (
                          <tr key={s.id} className="border-t" style={{ borderColor: "var(--line)" }}>
                            <td className="mono py-1 text-[10.5px]">{time(s.ts)}</td>
                            <td className="py-1">{s.zone_id ?? "n/a"}</td>
                            <td className="py-1">{s.label}</td>
                            <td className="tnum py-1 text-right">{(s.confidence * 100).toFixed(0)}%</td>
                            <td className="mono py-1 text-right text-[9.5px] text-[var(--ink-4)]">
                              {s.lat != null ? `${s.lat.toFixed(5)}, ${s.lon!.toFixed(5)}` : "n/a"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </Card>
              </>
            )}
          </motion.div>
        </div>
      )}
    </div>
  );
}

export default function EntitiesPage() {
  return (
    <Suspense fallback={<div className="p-8"><Skeleton className="h-40" /></div>}>
      <EntitiesInner />
    </Suspense>
  );
}
