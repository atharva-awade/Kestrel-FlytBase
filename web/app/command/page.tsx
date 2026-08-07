"use client";

import { motion } from "framer-motion";
import { Compass, Globe2, Radio, TriangleAlert } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { CommandGlobe } from "@/components/viz/CommandGlobe";
import {
  Card, Empty, LiveBadge, Pill, SectionTitle, SeverityChip, SimulatedBadge, Stat, fadeUp, stagger,
} from "@/components/ui/primitives";
import { api, type FleetResponse } from "@/lib/api";
import { cn, sevClass } from "@/lib/format";

/**
 * Global command: tier 1 of the drill-down.
 *
 * Opens on the portfolio because scale is the question the assignment cannot
 * answer on its own: one drone on one property proves the pipeline works; a fleet
 * proves the architecture does. From here it is three clicks to a single frame.
 */
export default function CommandPage() {
  const router = useRouter();
  const [fleet, setFleet] = useState<FleetResponse | null>(null);
  const [corr, setCorr] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      const [f, c] = await Promise.all([api.fleet(), api.correlations()]);
      if (!alive) return;
      setFleet(f);
      setCorr(c);
      setLoading(false);
    };
    void load();
    // The seeded generator is stable within an hour; polling faster would only
    // make the globe look busier without saying anything new.
    const t = setInterval(load, 60_000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  const s = fleet?.summary;
  const arcs =
    (corr?.matches ?? []).flatMap((m: any) =>
      m.sightings.slice(0, -1).map((a: any, i: number) => ({
        from: [a.lat, a.lon] as [number, number],
        to: [m.sightings[i + 1].lat, m.sightings[i + 1].lon] as [number, number],
        label: `${m.descriptor}: ${a.site_name} → ${m.sightings[i + 1].site_name}`,
      })),
    ) ?? [];

  return (
    <div className="mx-auto max-w-[1500px] px-5 py-6">
      <motion.div {...fadeUp}>
        <SectionTitle
          eyebrow="Tier 1 · Portfolio"
          title="Global Command"
          subtitle="Every monitored site, shaded by aggregate threat. Click a region for its sites, a site for its map, a detection for its frame."
          right={
            <div className="flex items-center gap-2">
              {s && <Pill tone="accent">{s.countries} countries</Pill>}
              <LiveBadge />
            </div>
          }
        />
      </motion.div>

      {s && (
        <motion.div {...stagger(1)} className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          <Card className="p-3"><Stat label="sites" value={s.sites} hint={`${s.online} online`} /></Card>
          <Card className="p-3"><Stat label="live feeds" value={s.live_sites} tone="ok" hint={`${s.simulated_sites} simulated`} /></Card>
          <Card className="p-3"><Stat label="active alerts" value={s.active_alerts} tone={s.active_alerts > 0 ? "warn" : "default"} /></Card>
          <Card className="p-3"><Stat label="airborne" value={s.airborne} tone="accent" hint={`${s.charging} charging`} /></Card>
          <Card className="p-3"><Stat label="mean battery" value={s.mean_battery} unit="%" /></Card>
          <Card className="p-3"><Stat label="peak threat" value={s.peak_threat.toFixed(2)} tone={s.peak_threat > 0.6 ? "warn" : "default"} /></Card>
        </motion.div>
      )}

      <div className="grid gap-4 lg:grid-cols-[1fr_340px]">
        <motion.div {...stagger(2)}>
          <Card className="overflow-hidden">
            <CommandGlobe
              fleet={fleet}
              height={520}
              arcs={arcs}
              onSelectSite={(id) => router.push(`/site/${id}`)}
            />
          </Card>
          {s && (
            <p className="mt-2 text-[11.5px] leading-relaxed text-[var(--ink-4)]">
              {s.note}
            </p>
          )}
        </motion.div>

        <div className="space-y-4">
          <motion.div {...stagger(3)}>
            <Card className="p-3.5">
              <div className="mb-2 flex items-center gap-1.5">
                <Compass size={13} style={{ color: "var(--sev-high)" }} />
                <span className="text-[13px] font-semibold">Cross-site correlation</span>
              </div>
              {loading ? (
                <div className="skeleton h-20 rounded-lg" />
              ) : (corr?.matches ?? []).length === 0 ? (
                <p className="text-[12px] leading-relaxed text-[var(--ink-4)]">
                  No subject has appeared at more than one site.
                </p>
              ) : (
                <div className="space-y-2">
                  {corr.matches.slice(0, 3).map((m: any, i: number) => (
                    <div key={i} className="rounded-lg border p-2"
                         style={{
                           borderColor: "color-mix(in oklab, var(--sev-high) 30%, transparent)",
                           background: "color-mix(in oklab, var(--sev-high) 5%, transparent)",
                         }}>
                      <div className="flex items-center gap-1.5">
                        <span className="text-[12.5px] font-semibold">{m.descriptor}</span>
                        <Pill tone="warn">{m.sites.length} sites</Pill>
                      </div>
                      <p className="mt-1 text-[11.5px] leading-relaxed text-[var(--ink-2)]">
                        {m.assessment}
                      </p>
                    </div>
                  ))}
                  <p className="text-[10.5px] leading-relaxed text-[var(--ink-4)]">
                    A single site cannot produce this finding; the evidence is
                    distributed across the portfolio.
                  </p>
                </div>
              )}
            </Card>
          </motion.div>

          <motion.div {...stagger(4)}>
            <Card className="p-3.5">
              <div className="mb-2 flex items-center gap-1.5">
                <Radio size={13} style={{ color: "var(--accent)" }} />
                <span className="text-[13px] font-semibold">Sites by threat</span>
              </div>
              {loading ? (
                <div className="space-y-1.5">
                  {[0, 1, 2, 3].map((i) => <div key={i} className="skeleton h-9 rounded-lg" />)}
                </div>
              ) : (
                <div className="max-h-[420px] space-y-1 overflow-y-auto">
                  {(fleet?.sites ?? []).map((x) => (
                    <button
                      key={x.site_id}
                      onClick={() => router.push(`/site/${x.site_id}`)}
                      className="flex w-full items-center gap-2 rounded-lg border px-2 py-1.5 text-left transition-colors hover:bg-[var(--surface-2)]"
                      style={{ borderColor: "var(--line)" }}
                    >
                      <span className={cn(sevClass(x.peak_severity), "sev-dot")} />
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-[12px] font-medium">{x.name}</div>
                        <div className="text-[10.5px] text-[var(--ink-4)]">
                          {x.country_name} · {x.drone_state}
                        </div>
                      </div>
                      {x.simulated ? <SimulatedBadge compact /> : <Pill tone="ok">live</Pill>}
                      {x.active_alerts > 0 && (
                        <SeverityChip severity={x.peak_severity ?? "info"}>
                          {x.active_alerts}
                        </SeverityChip>
                      )}
                    </button>
                  ))}
                </div>
              )}
            </Card>
          </motion.div>

          {(corr?.patterns ?? []).length > 0 && (
            <motion.div {...stagger(5)}>
              <Card className="p-3.5">
                <div className="mb-2 flex items-center gap-1.5">
                  <TriangleAlert size={13} style={{ color: "var(--sev-medium)" }} />
                  <span className="text-[13px] font-semibold">Regional patterns</span>
                </div>
                {corr.patterns.map((p: any, i: number) => (
                  <p key={i} className="text-[11.5px] leading-relaxed text-[var(--ink-2)]">
                    {p.assessment}
                  </p>
                ))}
              </Card>
            </motion.div>
          )}
        </div>
      </div>

      {!loading && !fleet && (
        <div className="mt-6">
          <Empty
            title="The API is not reachable"
            hint="Start it with `uv run kestrel serve`, then reload this page."
          />
        </div>
      )}
    </div>
  );
}
