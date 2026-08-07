"use client";

import { useEffect, useState } from "react";

import { resolveColor, sevColor } from "@/lib/format";

/**
 * The severity scale as literal colours, kept in step with the theme.
 *
 * Resolution happens in an effect rather than during render, because the theme
 * attribute is written to `<html>` by next-themes and reading
 * `getComputedStyle` mid-render can catch the previous theme's values for one
 * frame. On a WebGL layer that frame is not a subtle flicker: the colour is
 * baked into a material and stays wrong until something else forces an update.
 */
export function useSeverityPalette(themeKey: string | undefined) {
  const [palette, setPalette] = useState<Record<string, string>>(() => FALLBACK);

  useEffect(() => {
    const read = () => ({
      info: resolveColor(sevColor("info"), FALLBACK.info),
      low: resolveColor(sevColor("low"), FALLBACK.low),
      medium: resolveColor(sevColor("medium"), FALLBACK.medium),
      high: resolveColor(sevColor("high"), FALLBACK.high),
      critical: resolveColor(sevColor("critical"), FALLBACK.critical),
      accent: resolveColor("var(--accent)", FALLBACK.accent),
    });
    setPalette(read());
    // One more read on the next frame: on a cold load the stylesheet can still be
    // parsing when the effect first runs, which yields empty strings.
    const raf = requestAnimationFrame(() => setPalette(read()));
    return () => cancelAnimationFrame(raf);
  }, [themeKey]);

  return palette;
}

/** Used before the stylesheet resolves, and if a token is ever renamed. */
const FALLBACK: Record<string, string> = {
  info: "#64748b",
  low: "#0ea5e9",
  medium: "#f59e0b",
  high: "#f97316",
  critical: "#e11d48",
  accent: "#0ea5e9",
};

export function severityOf(
  palette: Record<string, string>,
  s: string | null | undefined,
): string {
  return palette[(s ?? "info").toLowerCase()] ?? palette.info;
}
