"use client";

import { motion } from "framer-motion";
import { ArrowLeft, MapPin, Navigation, Plane } from "lucide-react";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { SiteMap } from "@/components/viz/SiteMap";
import {
  Card, Empty, KeyValue, Pill, SectionTitle, SeverityChip, SimulatedBadge, fadeUp, stagger,
} from "@/components/ui/primitives";
import { api, type Alert, type Telemetry } from "@/lib/api";
import { useDeploy } from "@/lib/deploy";
import { bearing, cn, coords, metres, seconds, sevClass, time } from "@/lib/format";

/**
 * Site map: tier 2 of the drill-down.
 *
 * Every alert is placed at its dispatch coordinates, with its accuracy radius
 * drawn, so an operator can read a position and send an aircraft to it. That is
 * the difference between a notification and something actionable.
 */
export default function SitePage() {
  const { id } = useParams<{ id: string }>();
  const params = useSearchParams();
  const [site, setSite] = useState<any>(null);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [telemetry, setTelemetry] = useState<Telemetry[]>([]);
  const [focus, setFocus] = useState<string | null>(params.get("alert"));
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    (async () => {
      const [s, a, f] = await Promise.all([
        api.site(id), api.alerts(id, 60), api.frames(id, 120),
      ]);
      if (!alive) return;
      setSite(s);
      setAlerts(a?.alerts ?? []);
      setTelemetry(
        (f?.frames ?? []).map((x) => x.telemetry).filter(Boolean).reverse() as Telemetry[],
      );
      setLoading(false);
    })();
    return () => { alive = false; };
  }, [id]);

  const selected = alerts.find((a) => a.id === focus) ?? null;

  if (!loading && !site) {
    return (
      <div className="mx-auto max-w-3xl px-5 py-16">
        <Empty title={`No site "${id}"`} hint="Check the id, or start the API." />
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-[1500px] px-5 py-6">
      <motion.div {...fadeUp}>
        <Link href="/command"
              className="mb-2 inline-flex items-center gap-1.5 text-[12px] text-[var(--ink-3)] hover:text-[var(--ink)]">
          <ArrowLeft size={13} /> Global command
        </Link>
        <SectionTitle
          eyebrow="Tier 2 · Site"
          title={site?.name ?? id}
          subtitle={site?.notes}
          right={
            <div className="flex items-center gap-2">
              {site && (site.live_footage ? <Pill tone="ok">live footage</Pill> : <SimulatedBadge />)}
              {site && <Pill tone="muted">{site.zones?.length ?? 0} zones</Pill>}
            </div>
          }
        />
      </motion.div>

      <div className="grid gap-4 lg:grid-cols-[1fr_360px]">
        <motion.div {...stagger(1)}>
          <Card className="overflow-hidden">
            {loading ? (
              <div className="skeleton" style={{ height: 480 }} />
            ) : (
              <SiteMap
                site={site}
                alerts={alerts}
                telemetry={telemetry}
                height={480}
                focusAlert={focus}
                onSelectAlert={setFocus}
              />
            )}
          </Card>
        </motion.div>

        <div className="space-y-4">
          {selected && <DispatchCard alert={selected} />}

          <motion.div {...stagger(2)}>
            <Card className="p-3.5">
              <div className="mb-2 flex items-center gap-1.5">
                <MapPin size={13} style={{ color: "var(--accent)" }} />
                <span className="text-[13px] font-semibold">
                  Alerts {alerts.length > 0 && <span className="text-[var(--ink-4)]">({alerts.length})</span>}
                </span>
              </div>
              {alerts.length === 0 ? (
                <p className="text-[12px] leading-relaxed text-[var(--ink-4)]">
                  No alerts recorded. Run a session:{" "}
                  <code className="mono">uv run kestrel ingest</code>
                </p>
              ) : (
                <div className="max-h-[440px] space-y-1.5 overflow-y-auto">
                  {alerts.map((a) => (
                    <button
                      key={a.id}
                      onClick={() => setFocus(a.id)}
                      className={cn(
                        sevClass(a.severity),
                        "sev-bar w-full rounded-r-lg py-1.5 pl-2 pr-2 text-left transition-colors hover:bg-[var(--surface-2)]",
                        focus === a.id && "bg-[var(--surface-2)]",
                      )}
                    >
                      <div className="flex items-center gap-1.5">
                        <SeverityChip severity={a.severity} />
                        <span className="mono text-[10px] text-[var(--ink-4)]">{time(a.ts)}</span>
                        <span className="tnum ml-auto text-[10px] text-[var(--ink-4)]">
                          {(a.confidence * 100).toFixed(0)}%
                        </span>
                      </div>
                      <div className="mt-0.5 text-[12px] leading-snug">{a.title}</div>
                      {a.location?.lat != null && (
                        <div className="mono mt-0.5 text-[10px] text-[var(--accent-ink)]">
                          {metres(a.location.distance_from_dock_m)} · {bearing(a.location.bearing_from_dock_deg)}
                        </div>
                      )}
                    </button>
                  ))}
                </div>
              )}
            </Card>
          </motion.div>

          {site?.zones && (
            <motion.div {...stagger(3)}>
              <Card className="p-3.5">
                <div className="mb-2 text-[13px] font-semibold">Zones</div>
                <div className="space-y-1">
                  {site.zones.map((z: any) => (
                    <div key={z.id} className="flex items-center gap-2 text-[11.5px]">
                      <span className="h-2 w-2 rounded-sm"
                            style={{
                              background: z.priority >= 2 ? "var(--sev-critical)"
                                : z.priority >= 1.4 ? "var(--sev-medium)" : "var(--accent)",
                            }} />
                      <span className="flex-1 truncate">{z.name}</span>
                      <span className="tnum text-[10px] text-[var(--ink-4)]">
                        ×{z.priority}
                      </span>
                      {z.normal_hours && (
                        <span className="mono text-[9.5px] text-[var(--ink-4)]">
                          {String(z.normal_hours[0]).padStart(2, "0")}–
                          {String(z.normal_hours[1]).padStart(2, "0")}
                        </span>
                      )}
                    </div>
                  ))}
                </div>
              </Card>
            </motion.div>
          )}
        </div>
      </div>
    </div>
  );
}

function DispatchCard({ alert }: { alert: Alert }) {
  const launchDrone = useDeploy((s) => s.launch);
  const l = alert.location;
  /* No projected position means nothing to fly to. Saying so beats a card
   * that silently fails to appear when an alert is clicked. */
  if (!l?.lat) {
    return (
      <Card className="p-3.5">
        <div className="text-[12.5px] font-semibold">No dispatch position</div>
        <p className="mt-1 text-[12px] leading-relaxed text-[var(--ink-3)]">
          This alert has no geo-projection, so there are no coordinates to send an
          aircraft to. It can still be investigated from its frames.
        </p>
      </Card>
    );
  }
  return (
    <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}>
      <Card className="p-3.5"
            style={{ borderColor: "color-mix(in oklab, var(--accent) 34%, transparent)" }}>
        <div className="mb-2 flex items-center gap-1.5">
          <Navigation size={13} style={{ color: "var(--accent)" }} />
          <span className="text-[13px] font-semibold">Dispatch position</span>
          <Pill tone={l.source === "geo-projection" ? "ok" : "warn"} className="ml-auto">
            {l.source}
          </Pill>
        </div>

        <div className="mono rounded-lg border px-2.5 py-2 text-[12.5px] font-semibold"
             style={{ borderColor: "var(--line)", background: "var(--surface-2)",
                      color: "var(--accent-ink)" }}>
          {coords(l.lat, l.lon)}
        </div>

        <div className="mt-2.5">
          <KeyValue
            items={[
              ["accuracy", `±${l.accuracy_m?.toFixed(0) ?? "?"} m`],
              ["from dock", metres(l.distance_from_dock_m)],
              ["bearing", bearing(l.bearing_from_dock_deg)],
              ["ETA", seconds(l.eta_seconds)],
              ["fly at", `${l.recommended_altitude_m} m`],
              ["geofence", l.within_geofence
                ? <span style={{ color: "var(--ok)" }}>inside</span>
                : <span style={{ color: "var(--sev-critical)" }}>OUTSIDE</span>],
              ["zone", l.zone_name ?? l.zone_id ?? "n/a"],
            ]}
          />
        </div>

        {["high", "critical"].includes((alert.severity ?? "").toLowerCase()) && (
          <button
            onClick={() => launchDrone(alert)}
            className="mt-2.5 flex w-full items-center justify-center gap-1.5 rounded-lg px-3 py-2 text-[12.5px] font-semibold text-white"
            style={{ background: "var(--accent)" }}
          >
            <Plane size={12} />
            Deploy drone
          </button>
        )}

        {alert.mission_id && (
          <div className="mt-2.5 flex items-center gap-1.5 rounded-lg border px-2 py-1.5"
               style={{ borderColor: "var(--line)" }}>
            <Plane size={12} style={{ color: "var(--accent)" }} />
            <span className="mono text-[10.5px] text-[var(--ink-3)]">{alert.mission_id}</span>
            <span className="ml-auto text-[10.5px] text-[var(--ink-4)]">mission planned</span>
          </div>
        )}
      </Card>
    </motion.div>
  );
}
