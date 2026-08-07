"use client";

import { AnimatePresence, motion } from "framer-motion";
import { Globe2, Maximize2, Minimize2, X } from "lucide-react";
import Link from "next/link";
import {
  forwardRef, useCallback, useEffect, useMemo, useRef, useState,
} from "react";
import { useTheme } from "next-themes";
import type { CountryBucket, FleetResponse, SiteStatus } from "@/lib/api";
import { cn, sevClass, sevColor } from "@/lib/format";
import { isNight, localSolarTime } from "@/lib/solar";
import { severityOf, useSeverityPalette } from "@/lib/useSeverityPalette";
import { Pill, SeverityChip, SimulatedBadge } from "@/components/ui/primitives";

/**
 * The portfolio globe.
 *
 * The assignment describes one drone on one property. This exists because the
 * scalability question, "does the architecture survive a fleet?", is far better
 * answered by showing the fleet than by asserting it, and because a portfolio
 * produces findings a single site cannot (a subject probing three sites).
 *
 * Two non-obvious mechanics, both learned the hard way:
 *
 * 1. `next/dynamic` strips refs through its Loadable wrapper, so react-globe.gl's
 *    imperative handle (controls, pointOfView) is unreachable. The module is
 *    imported manually inside an effect and rendered directly instead.
 * 2. Fullscreen layout takes one or two frames to settle, so dimensions are
 *    re-measured across several rAF ticks or the canvas renders at the old size.
 */

/* Layer heights, defined together because they only make sense relative to each
 * other.
 *
 * react-globe.gl extrudes country polygons upward from the sphere, and a point
 * is a column of the given height. If a polygon is taller than the markers on
 * top of it, the markers are swallowed by the extrusion: an alerting country
 * rose to 0.045 while its own site dots stood at 0.026, and the alert rings sat
 * flat on the sphere at 0 underneath everything. The signals disappeared exactly
 * where there was something to signal.
 *
 * So every marker altitude is derived from the tallest polygon rather than
 * chosen independently. */
const POLY_ALT = { base: 0.008, alerting: 0.045, hovered: 0.06, pinned: 0.1 };
const POLY_MAX = Math.max(...Object.values(POLY_ALT));
/** Clearance above the tallest extrusion, so nothing z-fights at the boundary. */
const MARKER_FLOOR = POLY_MAX + 0.015;

const COUNTRIES_GEOJSON =
  "https://raw.githubusercontent.com/vasturiano/react-globe.gl/master/example/datasets/ne_110m_admin_0_countries.geojson";

function GlobeBase(props: any, ref: React.ForwardedRef<any>) {
  const [Loaded, setLoaded] = useState<any>(null);
  useEffect(() => {
    let cancelled = false;
    import("react-globe.gl").then((m) => {
      if (!cancelled) setLoaded(() => m.default);
    });
    return () => { cancelled = true; };
  }, []);
  if (!Loaded) {
    return (
      <div className="grid h-full w-full place-items-center text-[12px] text-[var(--ink-4)]">
        Loading globe…
      </div>
    );
  }
  return <Loaded {...props} ref={ref} />;
}
const Globe = forwardRef<any, any>(GlobeBase);
Globe.displayName = "Globe";

interface Props {
  fleet: FleetResponse | null;
  height?: number;
  onSelectSite?: (siteId: string) => void;
  arcs?: { from: [number, number]; to: [number, number]; label: string }[];
}

export function CommandGlobe({ fleet, height = 460, onSelectSite, arcs = [] }: Props) {
  const globeRef = useRef<any>(null);
  const boxRef = useRef<HTMLDivElement | null>(null);
  const { resolvedTheme } = useTheme();
  const dark = resolvedTheme === "dark";
  /* Every colour below is handed to three.js, which cannot read a CSS variable.
     See `resolveColor` in lib/format for what that failure looks like. */
  const palette = useSeverityPalette(resolvedTheme);
  const sev = (v: string | null | undefined) => severityOf(palette, v);

  const [countries, setCountries] = useState<{ features: any[] }>({ features: [] });
  const [dims, setDims] = useState({ width: 640, height });
  const [fullscreen, setFullscreen] = useState(false);
  const [hovered, setHovered] = useState<string | null>(null);
  const [pinned, setPinned] = useState<CountryBucket | null>(null);
  const [material, setMaterial] = useState<any>(null);
  /** Site the idle tour is currently resting on, shown in the HUD. */
  const [touring, setTouring] = useState<string | null>(null);

  const byCountry = useMemo(() => {
    const m: Record<string, CountryBucket> = {};
    for (const b of fleet?.by_country ?? []) m[b.country] = b;
    return m;
  }, [fleet]);

  const sitesFor = useCallback(
    (code: string): SiteStatus[] =>
      (fleet?.sites ?? []).filter((s) => s.country === code),
    [fleet],
  );

  useEffect(() => {
    let cancelled = false;
    fetch(COUNTRIES_GEOJSON)
      .then((r) => r.json())
      .then((g) => { if (!cancelled) setCountries(g); })
      .catch(() => { /* the globe still renders points without borders */ });
    return () => { cancelled = true; };
  }, []);

  // The ocean is a plain material rather than a texture: it keeps the light theme
  // genuinely light, and swaps cleanly when the theme does.
  useEffect(() => {
    let cancelled = false;
    import("three").then((THREE: any) => {
      if (cancelled) return;
      setMaterial(new THREE.MeshBasicMaterial({ color: dark ? "#0b1424" : "#dce8f5" }));
    });
    return () => { cancelled = true; };
  }, [dark]);

  useEffect(() => {
    const measure = () => {
      const el = boxRef.current;
      if (!el) return;
      setDims({
        width: el.clientWidth || (fullscreen ? window.innerWidth : 640),
        height: el.clientHeight || (fullscreen ? window.innerHeight : height),
      });
    };
    measure();
    const r1 = requestAnimationFrame(measure);
    const r2 = requestAnimationFrame(() => requestAnimationFrame(measure));
    const t = setTimeout(measure, 120);
    window.addEventListener("resize", measure);
    return () => {
      window.removeEventListener("resize", measure);
      cancelAnimationFrame(r1); cancelAnimationFrame(r2); clearTimeout(t);
    };
  }, [fullscreen, height]);

  useEffect(() => {
    if (!fullscreen) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setFullscreen(false); };
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [fullscreen]);

  // Auto-rotate, with an own rAF loop so it cannot stall on a tab switch or
  // remount. Disabled entirely under prefers-reduced-motion.
  useEffect(() => {
    let cancelled = false;
    let raf: number | null = null;
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

    (async () => {
      const start = Date.now();
      while (!globeRef.current?.controls?.() && Date.now() - start < 5000) {
        await new Promise((r) => requestAnimationFrame(r));
        if (cancelled) return;
      }
      const controls = globeRef.current?.controls?.();
      if (!controls || cancelled) return;
      controls.autoRotate = !reduce;
      controls.autoRotateSpeed = 0.42;
      controls.enableZoom = true;
      globeRef.current.pointOfView({ lat: 18, lng: 60, altitude: 2.35 }, 0);

      if (reduce) return;
      const tick = () => {
        if (cancelled) return;
        const c = globeRef.current?.controls?.();
        if (c) { if (!c.autoRotate) c.autoRotate = true; c.update(); }
        raf = requestAnimationFrame(tick);
      };
      raf = requestAnimationFrame(tick);
    })();

    return () => { cancelled = true; if (raf !== null) cancelAnimationFrame(raf); };
  }, [countries]);

  /* When nobody is touching it, tour the sites that matter.
   *
   * A command deck that sits still reads as a screenshot. This eases the camera
   * to the highest-threat site, holds long enough to read the label, then moves
   * on - and stands down the moment anyone interacts, so it never fights the
   * operator for control. Disabled entirely when a region is pinned or under
   * prefers-reduced-motion. */
  useEffect(() => {
    if (pinned || !fleet?.sites?.length) return;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;

    const ranked = [...fleet.sites]
      .sort((a, b) => (b.threat_score ?? 0) - (a.threat_score ?? 0))
      .slice(0, 5);
    if (!ranked.length) return;

    let idx = 0;
    let idle = true;
    const wake = () => { idle = false; };
    const el = boxRef.current;
    el?.addEventListener("pointerdown", wake);
    el?.addEventListener("wheel", wake, { passive: true });

    const timer = setInterval(() => {
      if (!idle || !globeRef.current) return;
      const site = ranked[idx % ranked.length];
      idx += 1;
      setTouring(site.site_id);
      globeRef.current.pointOfView(
        { lat: site.lat, lng: site.lon, altitude: 1.9 }, 1600,
      );
    }, 7000);

    return () => {
      clearInterval(timer);
      el?.removeEventListener("pointerdown", wake);
      el?.removeEventListener("wheel", wake);
    };
  }, [fleet, pinned]);

  const iso = (f: any): string => {
    const raw = f?.properties?.ISO_A2;
    if (typeof raw !== "string" || raw === "-99") return "";
    return raw.toUpperCase();
  };

  const colourFor = (code: string): string => {
    const b = byCountry[code];
    if (!b || b.alerts === 0) return dark ? "#16233a" : "#ffffff";
    // Severity-weighted, so one critical outranks a handful of informational.
    const s = b.by_severity;
    const total = Object.values(s).reduce((a, n) => a + n, 0) || 1;
    const weight =
      (s.info * 1 + s.low * 2 + s.medium * 3 + s.high * 4 + s.critical * 5) / (total * 5);
    if (weight >= 0.8) return sev("critical");
    if (weight >= 0.6) return sev("high");
    if (weight >= 0.4) return sev("medium");
    if (weight > 0) return sev("low");
    return sev("info");
  };

  /* Sites are marked by whether the sun is actually down on them right now.
     Every after-hours rule in the system turns on that fact, and across ten
     countries it is a per-site question rather than a property of the viewer. */
  const points = (fleet?.sites ?? []).map((s) => {
    const dark = isNight(s.lat, s.lon);
    return {
      lat: s.lat, lng: s.lon, site: s, dark,
      solar: localSolarTime(s.lon),
      size: (0.28 + Math.min(0.7, s.threat_score * 0.9)) * (dark ? 1.25 : 1),
      color: s.active_alerts > 0 ? sev(s.peak_severity) : palette.accent,
    };
  });

  const nightSites = points.filter((p) => p.dark).length;

  /* A shockwave at every site with something open, sized by severity. The
     deck should read as a live operations picture rather than a static chart. */
  const rings = (fleet?.sites ?? [])
    .filter((s) => s.active_alerts > 0)
    .map((s) => ({
      lat: s.lat, lng: s.lon, color: sev(s.peak_severity),
      maxR: s.peak_severity === "critical" ? 5.2 : s.peak_severity === "high" ? 4.2 : 3,
      speed: s.peak_severity === "critical" ? 2.2 : 1.4,
    }));

  const arcData = arcs.map((a) => ({
    startLat: a.from[0], startLng: a.from[1],
    endLat: a.to[0], endLng: a.to[1], label: a.label,
  }));

  const body = (
    <div ref={boxRef} className={cn("relative overflow-hidden", fullscreen ? "h-full w-full" : "")}
         style={fullscreen ? undefined : { height }}>
      {countries.features.length > 0 || points.length > 0 ? (
        <Globe
          ref={globeRef}
          width={dims.width}
          height={dims.height}
          backgroundColor="rgba(0,0,0,0)"
          {...(material ? { globeMaterial: material } : {})}
          showAtmosphere
          atmosphereColor={dark ? "#38bdf8" : "#bae6fd"}
          atmosphereAltitude={0.19}
          polygonsData={countries.features}
          polygonAltitude={(d: any) => {
            const code = iso(d);
            if (pinned?.country === code) return POLY_ALT.pinned;
            if (hovered === code) return POLY_ALT.hovered;
            return byCountry[code]?.alerts ? POLY_ALT.alerting : POLY_ALT.base;
          }}
          polygonCapColor={(d: any) => colourFor(iso(d))}
          polygonSideColor={() => (dark ? "rgba(56,189,248,0.14)" : "rgba(14,165,233,0.12)")}
          polygonStrokeColor={(d: any) => {
            const code = iso(d);
            if (pinned?.country === code) return dark ? "#7dd3fc" : "#0369a1";
            if (hovered === code) return dark ? "#bae6fd" : "#0ea5e9";
            return dark ? "rgba(90,120,160,0.3)" : "rgba(120,145,175,0.35)";
          }}
          polygonLabel={(d: any) => {
            const code = iso(d);
            const b = byCountry[code];
            const name = d?.properties?.ADMIN ?? d?.properties?.NAME ?? "";
            if (!b) return `<div style="font:500 11px Inter,sans-serif;background:var(--surface);border:1px solid var(--line);color:var(--ink-3);padding:5px 8px;border-radius:8px">${name}<br/><span style="opacity:.6">no sites</span></div>`;
            return `<div style="font:500 11px Inter,sans-serif;background:var(--surface);border:1px solid var(--line);color:var(--ink);padding:6px 9px;border-radius:8px;box-shadow:var(--shadow)">
              <div style="font-weight:700">${name}</div>
              <div style="color:var(--accent);font-weight:600">${b.sites} site${b.sites === 1 ? "" : "s"} · ${b.alerts} alert${b.alerts === 1 ? "" : "s"}</div>
              <div style="opacity:.6;font-size:10px">click to open</div>
            </div>`;
          }}
          onPolygonHover={(d: any) => setHovered(d ? iso(d) : null)}
          onPolygonClick={(d: any) => {
            const b = byCountry[iso(d)];
            if (b) setPinned(b);
          }}
          polygonsTransitionDuration={280}
          pointsData={points}
          pointLat="lat"
          pointLng="lng"
          pointColor="color"
          pointAltitude={(d: any) => MARKER_FLOOR + d.size * 0.05}
          pointRadius={(d: any) => d.size * 0.4}
          onPointClick={(d: any) => onSelectSite?.(d.site.site_id)}
          pointLabel={(d: any) =>
            `<div style="font:500 11px Inter,sans-serif;background:var(--surface);border:1px solid var(--line);padding:6px 9px;border-radius:8px;color:var(--ink)">
              <b>${d.site.name}</b><br/>
              <span style="opacity:.7">${d.site.drone_state} · ${d.site.active_alerts} alerts</span><br/>
              <span style="opacity:.55;font-size:10px">${d.dark ? "night" : "day"} · ~${d.solar} local solar</span>
              ${d.site.simulated ? '<br/><span style="color:var(--sev-medium);font-size:9px">SIMULATED</span>' : ""}
            </div>`}
          ringsData={rings}
          ringLat="lat" ringLng="lng" ringColor={(d: any) => () => d.color}
          ringAltitude={MARKER_FLOOR}
          ringMaxRadius={(d: any) => d.maxR}
          ringPropagationSpeed={(d: any) => d.speed}
          ringRepeatPeriod={1100}
          arcsData={arcData}
          arcStartLat="startLat" arcStartLng="startLng"
          arcEndLat="endLat" arcEndLng="endLng"
          arcColor={() => [sev("high"), sev("critical")]}
          arcDashLength={0.42} arcDashGap={0.18} arcDashAnimateTime={2200}
          arcStroke={0.5} arcAltitudeAutoScale={0.42}
          arcLabel={(d: any) => d.label}
        />
      ) : (
        <div className="grid h-full place-items-center text-[12px] text-[var(--ink-4)]">
          Waiting for fleet data…
        </div>
      )}

      <div className="glass pointer-events-none absolute top-3 left-3 z-10 rounded-lg px-2.5 py-1.5">
        <div className="mono flex items-center gap-3 text-[10px] tracking-[0.1em] text-[var(--ink-3)] uppercase">
          <span>{nightSites} of {points.length} in darkness</span>
          {touring && (
            <span className="text-[var(--accent-ink)]">
              {fleet?.sites?.find((x) => x.site_id === touring)?.name ?? touring}
            </span>
          )}
        </div>
      </div>

      <button
        onClick={() => setFullscreen((v) => !v)}
        className="glass absolute bottom-3 right-3 z-10 flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[11.5px] font-medium"
        title={fullscreen ? "Exit fullscreen (Esc)" : "Fullscreen"}
      >
        {fullscreen ? <><Minimize2 size={12} /> Exit</> : <><Maximize2 size={12} /> Fullscreen</>}
      </button>

      <Legend />

      <AnimatePresence>
        {pinned && (
          <motion.div
            initial={{ opacity: 0, x: 16 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: 16 }}
            transition={{ type: "spring", stiffness: 320, damping: 30 }}
            className="glass absolute right-3 top-3 z-20 max-h-[calc(100%-5rem)] w-[300px] overflow-y-auto rounded-xl p-3"
          >
            <div className="flex items-start gap-2">
              <div className="min-w-0 flex-1">
                <div className="text-[13.5px] font-semibold">{pinned.country_name}</div>
                <div className="text-[11px] text-[var(--ink-4)]">
                  {pinned.sites} site{pinned.sites === 1 ? "" : "s"} · {pinned.alerts} active alert
                  {pinned.alerts === 1 ? "" : "s"}
                </div>
              </div>
              <button onClick={() => setPinned(null)}
                      className="grid h-6 w-6 place-items-center rounded-md hover:bg-[var(--surface-3)]">
                <X size={12} />
              </button>
            </div>

            <div className="mt-2.5 space-y-1.5">
              {sitesFor(pinned.country).map((s) => (
                <Link
                  key={s.site_id}
                  href={`/site/${s.site_id}`}
                  onClick={() => onSelectSite?.(s.site_id)}
                  className="block rounded-lg border p-2 transition-colors hover:bg-[var(--surface-2)]"
                  style={{ borderColor: "var(--line)" }}
                >
                  <div className="flex items-center gap-1.5">
                    <span className={cn(sevClass(s.peak_severity), "sev-dot")} />
                    <span className="flex-1 truncate text-[12px] font-medium">{s.name}</span>
                    {s.simulated ? <SimulatedBadge compact /> : <Pill tone="ok">live</Pill>}
                  </div>
                  <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[10.5px] text-[var(--ink-4)]">
                    <span>{s.drone_state}</span>
                    <span className="tnum">· {s.battery_pct.toFixed(0)}% batt</span>
                    {s.active_alerts > 0 && (
                      <>
                        <span>·</span>
                        <SeverityChip severity={s.peak_severity ?? "info"}>
                          {s.active_alerts} alert{s.active_alerts === 1 ? "" : "s"}
                        </SeverityChip>
                      </>
                    )}
                  </div>
                </Link>
              ))}
            </div>
            <p className="mt-2 text-[10px] leading-relaxed text-[var(--ink-4)]">
              Sites marked SIMULATED have no live feed; their activity comes from a
              seeded generator.
            </p>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );

  if (fullscreen) {
    return (
      <div className="fixed inset-0 z-50" style={{ background: "var(--bg)" }}>
        <div className="absolute left-4 top-4 z-20 flex items-center gap-2">
          <Globe2 size={15} style={{ color: "var(--accent)" }} />
          <span className="text-[13px] font-semibold">Global Command</span>
          <span className="text-[11px] text-[var(--ink-4)]">press Esc to close</span>
        </div>
        {body}
      </div>
    );
  }
  return body;
}

function Legend() {
  return (
    <div className="glass absolute bottom-3 left-3 z-10 rounded-lg px-2.5 py-1.5">
      <div className="flex items-center gap-2">
        <span className="eyebrow">threat</span>
        {(["info", "low", "medium", "high", "critical"] as const).map((s) => (
          <span key={s} title={s} className="h-2 w-4 rounded-sm"
                style={{ background: sevColor(s) }} />
        ))}
      </div>
    </div>
  );
}
