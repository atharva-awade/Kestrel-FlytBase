import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Times are shown to the second: operators correlate against other logs. */
export function time(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString("en-GB", { hour12: false });
  } catch {
    return iso;
  }
}

export function dateTime(iso: string): string {
  try {
    const d = new Date(iso);
    return `${d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" })} ${d.toLocaleTimeString("en-GB", { hour12: false })}`;
  } catch {
    return iso;
  }
}

export function ago(iso: string, now = Date.now()): string {
  const s = Math.max(0, (now - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function metres(m: number | null | undefined): string {
  if (m == null) return "n/a";
  return m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m)} m`;
}

export function seconds(s: number | null | undefined): string {
  if (s == null) return "n/a";
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s % 60)}s`;
}

/** Six decimal places ≈ 0.1 m, the precision a dispatch actually needs. */
export function coords(lat: number | null, lon: number | null): string {
  if (lat == null || lon == null) return "n/a";
  return `${lat.toFixed(6)}, ${lon.toFixed(6)}`;
}

export function bearing(deg: number | null | undefined): string {
  if (deg == null) return "n/a";
  const points = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
  return `${Math.round(deg)}° ${points[Math.round(deg / 22.5) % 16]}`;
}

export function pct(v: number | null | undefined, digits = 0): string {
  if (v == null) return "n/a";
  return `${(v * 100).toFixed(digits)}%`;
}

export function usd(v: number | null | undefined): string {
  if (v == null) return "n/a";
  if (v === 0) return "$0";
  if (v < 0.01) return `$${v.toFixed(5)}`;
  return `$${v.toFixed(2)}`;
}

export const SEVERITIES = ["info", "low", "medium", "high", "critical"] as const;
export type Severity = (typeof SEVERITIES)[number];

export function sevClass(s: string | null | undefined): string {
  return `sev-${(s ?? "info").toLowerCase()}`;
}

export function sevRank(s: string | null | undefined): number {
  return Math.max(0, SEVERITIES.indexOf((s ?? "info").toLowerCase() as Severity));
}

/** One severity scale, resolved from CSS so light and dark agree automatically. */
export function sevColor(s: string | null | undefined): string {
  const map: Record<string, string> = {
    info: "var(--sev-info)",
    low: "var(--sev-low)",
    medium: "var(--sev-medium)",
    high: "var(--sev-high)",
    critical: "var(--sev-critical)",
  };
  return map[(s ?? "info").toLowerCase()] ?? "var(--ink-4)";
}

/**
 * Resolve a design token to a literal colour.
 *
 * `sevColor` returns `var(--sev-critical)`, which is correct for the DOM and
 * silently fatal anywhere else. WebGL and canvas renderers parse colours
 * themselves and know nothing about CSS custom properties: three.js fails to
 * build the material, three-globe is left holding null, and the first thing it
 * reads off it is `.opacity`. The symptom is a TypeError plus a globe with no
 * shading at all, which does not obviously point back at a colour string.
 *
 * Anything drawn outside the DOM has to come through here.
 */
export function resolveColor(value: string, fallback = "#64748b"): string {
  if (typeof window === "undefined") return fallback;
  const token = /^var\((--[\w-]+)\)$/.exec(value.trim());
  if (!token) return value;
  const resolved = getComputedStyle(document.documentElement)
    .getPropertyValue(token[1])
    .trim();
  return resolved || fallback;
}

/** Severity colour as a literal, for canvas and WebGL consumers. */
export function sevColorLiteral(s: string | null | undefined, fallback?: string): string {
  return resolveColor(sevColor(s), fallback);
}

export function truncate(s: string, n = 90): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

export function titleCase(s: string): string {
  return s.replace(/[-_]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
