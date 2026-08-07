"use client";

import { Maximize2, Minimize2, Navigation } from "lucide-react";
import { useTheme } from "next-themes";
import { useEffect, useRef, useState } from "react";
import type { Alert, Telemetry } from "@/lib/api";
import { cn, coords, metres, sevColor } from "@/lib/format";
import { severityOf, useSeverityPalette } from "@/lib/useSeverityPalette";

/**
 * The site map: tier 2 of the spatial drill-down (globe → map → frame).
 *
 * Real geography from MapTiler vector tiles, with the site's own zones, the
 * drone's position and track, and every alert placed at its **dispatch
 * coordinates** rather than a generic site pin. That distinction is the point: an
 * operator can read a position off this map and send an aircraft to it.
 *
 * The MapTiler key is necessarily public because the browser fetches tiles directly, so
 * it cannot be hidden. It carries the NEXT_PUBLIC_ prefix to make that explicit
 * and should be domain-restricted at the provider. Model-provider keys are a
 * different matter entirely and never leave the server.
 */

const KEY = process.env.NEXT_PUBLIC_MAPTILER_KEY ?? "";

// Light and dark styles, so the map changes with the theme rather than staying a
// bright rectangle in a dark console at 3am.
const STYLE = {
  light: `https://api.maptiler.com/maps/dataviz-light/style.json?key=${KEY}`,
  dark: `https://api.maptiler.com/maps/dataviz-dark/style.json?key=${KEY}`,
};

interface Props {
  site: any;
  alerts?: Alert[];
  telemetry?: Telemetry[];
  height?: number;
  focusAlert?: string | null;
  onSelectAlert?: (id: string) => void;
}

export function SiteMap({
  site, alerts = [], telemetry = [], height = 420, focusAlert, onSelectAlert,
}: Props) {
  const boxRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<any>(null);
  const { resolvedTheme } = useTheme();
  const dark = resolvedTheme === "dark";
  // MapLibre paint properties are parsed by the renderer, not the DOM, so a CSS
  // variable reaches it as an unparseable string. Same class of bug as the globe.
  const palette = useSeverityPalette(resolvedTheme);
  const [fullscreen, setFullscreen] = useState(false);
  const [ready, setReady] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  /** The theme the current style was built for, so a swap only happens on a
   *  real change rather than on every dependency tick. */
  const appliedTheme = useRef<boolean | null>(null);
  /** Bumped whenever a style swap wipes the custom layers, so the effects that
   *  own those layers know to rebuild them. */
  const [styleEpoch, setStyleEpoch] = useState(0);
  const handlersBound = useRef(false);

  // ── init ────────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!boxRef.current || mapRef.current || !site) return;
    let cancelled = false;

    (async () => {
      try {
        const maplibre = await import("maplibre-gl");
        if (cancelled || !boxRef.current) return;

        /* Point MapLibre at a worker it can actually load.
         *
         * v6 derives its worker URL from `import.meta.url`, and webpack inlines
         * that as a `file:` URL. MapLibre's own guard rejects anything that is
         * not http(s) and returns "", so the browser runs
         * `new Worker("", {type:"module"})`, which resolves against the document
         * and fetches the HTML page as a module script. The worker pool then
         * never answers.
         *
         * Everything else keeps working, which is what made this so hard to see:
         * style, tiles.json and sprite are fetched on the main thread and all
         * return 200, and the controls and attribution are plain DOM. Only tile
         * *decoding* is in the worker, so the canvas silently never paints and
         * no `error` event ever fires. `web/scripts/sync-maplibre-worker.mjs`
         * copies the worker into public/ on predev and prebuild. */
        maplibre.setWorkerUrl("/maplibre/maplibre-gl-worker.mjs");

        const map = new maplibre.Map({
          container: boxRef.current,
          style: KEY ? STYLE[dark ? "dark" : "light"] : blankStyle(dark),
          center: [site.origin.lon, site.origin.lat],
          zoom: 16.1,
          pitch: 42,
          bearing: -18,
          attributionControl: { compact: true },
        });
        map.addControl(new maplibre.NavigationControl({ visualizePitch: true }), "top-right");

        const draw = () => {
          if (cancelled) return;
          appliedTheme.current = dark;
          try {
            drawSite(map, site, dark);
          } catch (e) {
            // A throw in here is swallowed by the emitter, and the only visible
            // trace is a basemap with no zones on it.
            console.error("[KESTREL] drawSite failed", e);
            setFailed(e instanceof Error ? e.message : "could not draw the site");
          }
          // A container that was still being laid out when the map initialised
          // leaves the canvas at the wrong size, which renders as blank.
          map.resize();
          setReady(true);
        };

        /* `style.load`, not `load`. `load` waits for every tile source to report
         * loaded, so one stalled source blocks it forever and the site's own
         * GeoJSON never gets drawn either. `style.load` fires as soon as the
         * style is parsed, which is all `drawSite` actually needs. */
        if (map.isStyleLoaded()) draw();
        else map.once("style.load", draw);

        map.on("error", (e: any) => {
          const msg = e?.error?.message ?? e?.message ?? "unknown map error";
          console.error("[KESTREL] maplibre error:", msg, e);
          setFailed(String(msg).slice(0, 160));
        });
        mapRef.current = map;
      } catch (e) {
        setFailed(e instanceof Error ? e.message : "map failed to initialise");
      }
    })();

    return () => {
      cancelled = true;
      mapRef.current?.remove();
      mapRef.current = null;
    };
    // Intentionally init-only; theme and data changes are handled below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [site]);

  // ── theme swap ──────────────────────────────────────────────────────────
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;

    /* Only swap when the theme has actually changed.
     *
     * This effect also depends on `ready`, so without the guard it fired the
     * instant the initial load completed and called `setStyle` with the style
     * that had just finished loading. That tears the style down and reloads it,
     * dropping every layer `drawSite` had just added, and leaves a blank canvas
     * if anything in the second pass does not line up. The symptom is a white
     * map with working controls and no zones, which looks like a tile or key
     * problem and is neither. */
    if (appliedTheme.current === dark) return;
    appliedTheme.current = dark;

    map.setStyle(KEY ? STYLE[dark ? "dark" : "light"] : blankStyle(dark));

    /* `setStyle` diffs against the serialised current style, which includes our
     * own sources and layers, so it emits removeLayer/removeSource for all of
     * them. `drawSite` puts the zones and dock back; `styleEpoch` re-runs the
     * alerts/track effect, which owns the rest. Waiting on `style.load` rather
     * than `styledata` matters because `styledata` also fires for sprite and
     * diff events, and `once` would take whichever arrived first. */
    map.once("style.load", () => {
      if (!mapRef.current) return;
      try {
        drawSite(map, site, dark);
      } catch (e) {
        console.error("[KESTREL] drawSite failed after theme swap", e);
      }
      setStyleEpoch((n) => n + 1);
    });
  }, [dark, ready, site]);

  // ── alerts and track ────────────────────────────────────────────────────
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;

    setSource(map, "kestrel-alerts", {
      type: "FeatureCollection",
      features: alerts
        .filter((a) => a.location?.lat != null)
        .map((a) => ({
          type: "Feature",
          properties: {
            id: a.id, severity: a.severity, title: a.title,
            colour: severityOf(palette, a.severity),
            accuracy: a.location?.accuracy_m ?? 10,
            distance: a.location?.distance_from_dock_m ?? 0,
          },
          geometry: { type: "Point", coordinates: [a.location!.lon, a.location!.lat] },
        })),
    });

    if (!map.getLayer("alerts-accuracy")) {
      // Accuracy radius drawn first, so a low-confidence position visibly reads as
      // an area rather than a point.
      map.addLayer({
        id: "alerts-accuracy", type: "circle", source: "kestrel-alerts",
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 14, 6, 18, ["*", ["get", "accuracy"], 1.4]],
          "circle-color": ["get", "colour"], "circle-opacity": 0.13,
          "circle-stroke-width": 1, "circle-stroke-color": ["get", "colour"],
          "circle-stroke-opacity": 0.35,
        },
      });
      map.addLayer({
        id: "alerts-dot", type: "circle", source: "kestrel-alerts",
        paint: {
          "circle-radius": 6, "circle-color": ["get", "colour"],
          "circle-stroke-width": 2, "circle-stroke-color": "#fff", "circle-stroke-opacity": 0.9,
        },
      });
      if (!handlersBound.current) {
        // Bound once for the life of the map. These used to sit inside the
        // add-layer branch, so every style swap stacked another set.
        handlersBound.current = true;
        map.on("click", "alerts-dot", (e: any) => {
          const f = e.features?.[0];
          if (f) onSelectAlert?.(f.properties.id);
        });
        map.on("mouseenter", "alerts-dot", () => (map.getCanvas().style.cursor = "pointer"));
        map.on("mouseleave", "alerts-dot", () => (map.getCanvas().style.cursor = ""));
      }
    }

    if (telemetry.length > 1) {
      setSource(map, "kestrel-track", {
        type: "FeatureCollection",
        features: [{
          type: "Feature", properties: {},
          geometry: { type: "LineString", coordinates: telemetry.map((t) => [t.lon, t.lat]) },
        }],
      });
      if (!map.getLayer("track-line")) {
        map.addLayer({
          id: "track-line", type: "line", source: "kestrel-track",
          layout: { "line-cap": "round", "line-join": "round" },
          paint: {
            "line-color": dark ? "#38bdf8" : "#0ea5e9",
            "line-width": 2.2, "line-opacity": 0.65, "line-dasharray": [2, 1.5],
          },
        });
      }
      const last = telemetry[telemetry.length - 1];
      setSource(map, "kestrel-drone", {
        type: "FeatureCollection",
        features: [{
          type: "Feature",
          properties: { heading: last.heading_deg, alt: last.alt_m, state: last.state },
          geometry: { type: "Point", coordinates: [last.lon, last.lat] },
        }],
      });
      if (!map.getLayer("drone-dot")) {
        map.addLayer({
          id: "drone-halo", type: "circle", source: "kestrel-drone",
          paint: {
            "circle-radius": 16, "circle-color": dark ? "#38bdf8" : "#0ea5e9",
            "circle-opacity": 0.14,
          },
        });
        map.addLayer({
          id: "drone-dot", type: "circle", source: "kestrel-drone",
          paint: {
            "circle-radius": 7, "circle-color": dark ? "#7dd3fc" : "#0369a1",
            "circle-stroke-width": 2.5, "circle-stroke-color": "#fff",
          },
        });
      }
    }
  }, [alerts, telemetry, ready, dark, styleEpoch, palette, onSelectAlert]);

  // ── focus ───────────────────────────────────────────────────────────────
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready || !focusAlert) return;
    const a = alerts.find((x) => x.id === focusAlert);
    if (a?.location?.lat != null) {
      map.flyTo({
        center: [a.location.lon, a.location.lat],
        zoom: 18.4, pitch: 55, duration: 1400, essential: true,
      });
    }
  }, [focusAlert, alerts, ready]);

  useEffect(() => {
    if (!fullscreen) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setFullscreen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fullscreen]);

  useEffect(() => {
    const t = setTimeout(() => mapRef.current?.resize(), 220);
    return () => clearTimeout(t);
  }, [fullscreen]);

  const body = (
    <div className={cn("relative overflow-hidden", fullscreen ? "h-full w-full" : "rounded-xl")}
         style={fullscreen ? undefined : { height }}>
      <div ref={boxRef} className="h-full w-full" />

      {!KEY && (
        <div className="glass absolute left-3 top-3 z-10 max-w-xs rounded-lg px-2.5 py-2 text-[11px] leading-relaxed text-[var(--ink-3)]">
          No MapTiler key set, so zone geometry is shown without basemap tiles. Add
          <code className="mono mx-1">NEXT_PUBLIC_MAPTILER_KEY</code>to enable them.
        </div>
      )}
      {failed && KEY && (
        <div className="glass absolute top-3 left-3 z-10 max-w-xs rounded-lg px-2.5 py-2 text-[11px] leading-relaxed text-[var(--sev-medium)]">
          Map problem: {failed}
          <div className="mt-1 text-[10px] text-[var(--ink-4)]">
            Check the key and any domain restrictions on it.
          </div>
        </div>
      )}

      <button
        onClick={() => setFullscreen((v) => !v)}
        className="glass absolute bottom-3 right-3 z-10 flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[11.5px] font-medium"
      >
        {fullscreen ? <><Minimize2 size={12} /> Exit</> : <><Maximize2 size={12} /> Fullscreen</>}
      </button>

      {alerts.length > 0 && (
        <div className="glass absolute bottom-3 left-3 z-10 rounded-lg px-2.5 py-1.5">
          <div className="flex items-center gap-1.5 text-[11px]">
            <Navigation size={11} style={{ color: "var(--accent)" }} />
            <span className="text-[var(--ink-3)]">
              {alerts.filter((a) => a.location?.lat != null).length} dispatch position
              {alerts.length === 1 ? "" : "s"}
            </span>
          </div>
        </div>
      )}
    </div>
  );

  if (fullscreen) {
    return <div className="fixed inset-0 z-50" style={{ background: "var(--bg)" }}>{body}</div>;
  }
  return body;
}

// ── helpers ───────────────────────────────────────────────────────────────
function setSource(map: any, id: string, data: any) {
  const src = map.getSource(id);
  if (src) src.setData(data);
  else map.addSource(id, { type: "geojson", data });
}

function drawSite(map: any, site: any, dark: boolean) {
  if (!site?.geojson) return;
  setSource(map, "kestrel-zones", site.geojson);

  if (!map.getLayer("zones-fill")) {
    map.addLayer({
      id: "zones-fill", type: "fill", source: "kestrel-zones",
      paint: {
        // Higher-priority zones read hotter, so the map itself communicates where
        // the risk is before anything has happened.
        "fill-color": [
          "interpolate", ["linear"], ["get", "priority"],
          0.5, dark ? "#1e3a5f" : "#dbeafe",
          1.5, dark ? "#0e7490" : "#93c5fd",
          2.5, dark ? "#be123c" : "#fda4af",
        ],
        "fill-opacity": 0.2,
      },
    });
    map.addLayer({
      id: "zones-line", type: "line", source: "kestrel-zones",
      paint: {
        "line-color": dark ? "#38bdf8" : "#0369a1",
        "line-width": 1.4, "line-opacity": 0.55,
      },
    });
    map.addLayer({
      id: "zones-label", type: "symbol", source: "kestrel-zones",
      layout: {
        "text-field": ["get", "name"],
        "text-size": 10.5,
        "text-font": ["Open Sans Semibold", "Noto Sans Bold"],
        "text-allow-overlap": false,
      },
      paint: {
        "text-color": dark ? "#bae6fd" : "#0c4a6e",
        "text-halo-color": dark ? "#070b14" : "#ffffff",
        "text-halo-width": 1.4,
      },
    });
  }

  if (site.dock) {
    setSource(map, "kestrel-dock", {
      type: "FeatureCollection",
      features: [{
        type: "Feature", properties: { name: "Dock" },
        geometry: { type: "Point", coordinates: [site.dock.lon, site.dock.lat] },
      }],
    });
    if (!map.getLayer("dock-dot")) {
      map.addLayer({
        id: "dock-dot", type: "circle", source: "kestrel-dock",
        paint: {
          "circle-radius": 7, "circle-color": dark ? "#34d399" : "#10a37f",
          "circle-stroke-width": 2.5, "circle-stroke-color": "#fff",
        },
      });
    }
  }
}

/** A usable map with no key: zones on a flat background rather than nothing. */
function blankStyle(dark: boolean) {
  return {
    version: 8 as const,
    sources: {},
    layers: [{
      id: "bg", type: "background" as const,
      paint: { "background-color": dark ? "#0b1424" : "#eef4fb" },
    }],
  };
}
