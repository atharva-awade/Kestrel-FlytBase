"use client";

import { motion } from "framer-motion";
import { useTheme } from "next-themes";
import { useEffect, useState } from "react";

/**
 * The theme switch.
 *
 * A sun that morphs into a moon: the mask circle slides across the disc, rays
 * retract, stars fade in. Spring-driven rather than eased, so it feels physical
 * rather than timed.
 *
 * Mounted-state guard is required because the server does not know the stored theme, so
 * rendering the real icon before hydration guarantees a mismatch.
 */
export function ThemeToggle({ compact = false }: { compact?: boolean }) {
  const { resolvedTheme, setTheme } = useTheme();
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  const dark = resolvedTheme === "dark";
  const size = compact ? 30 : 36;

  if (!mounted) {
    return <div style={{ width: size, height: size }} className="rounded-full skeleton" />;
  }

  return (
    <button
      onClick={() => setTheme(dark ? "light" : "dark")}
      aria-label={dark ? "Switch to light theme" : "Switch to dark theme"}
      title={dark ? "Light theme" : "Dark theme"}
      className="relative grid place-items-center rounded-full border transition-colors hover:bg-[var(--surface-2)]"
      style={{ width: size, height: size, borderColor: "var(--line)" }}
    >
      <svg width={compact ? 15 : 18} height={compact ? 15 : 18} viewBox="0 0 24 24" fill="none">
        <defs>
          <mask id="kestrel-moon-mask">
            <rect width="24" height="24" fill="white" />
            {/* Sliding cut-out turns the disc into a crescent. */}
            <motion.circle
              r="9"
              fill="black"
              initial={false}
              animate={{ cx: dark ? 17 : 30, cy: dark ? 6 : 0 }}
              transition={{ type: "spring", stiffness: 260, damping: 24 }}
            />
          </mask>
        </defs>

        <motion.circle
          cx="12"
          cy="12"
          fill="var(--accent)"
          mask="url(#kestrel-moon-mask)"
          initial={false}
          animate={{ r: dark ? 8.5 : 5 }}
          transition={{ type: "spring", stiffness: 240, damping: 22 }}
        />

        {/* Rays retract into the disc rather than simply disappearing. */}
        <motion.g
          stroke="var(--accent)"
          strokeWidth="1.9"
          strokeLinecap="round"
          initial={false}
          animate={{ opacity: dark ? 0 : 1, rotate: dark ? 45 : 0, scale: dark ? 0.5 : 1 }}
          transition={{ type: "spring", stiffness: 220, damping: 20 }}
          style={{ originX: "12px", originY: "12px" }}
        >
          {[0, 45, 90, 135, 180, 225, 270, 315].map((deg) => (
            <line
              key={deg}
              x1="12"
              y1="2.6"
              x2="12"
              y2="4.6"
              transform={`rotate(${deg} 12 12)`}
            />
          ))}
        </motion.g>

        <motion.g
          fill="var(--accent-3)"
          initial={false}
          animate={{ opacity: dark ? 1 : 0 }}
          transition={{ duration: 0.4, delay: dark ? 0.16 : 0 }}
        >
          <circle cx="5" cy="5.5" r="0.85" />
          <circle cx="19.5" cy="16" r="0.7" />
          <circle cx="7" cy="19" r="0.6" />
        </motion.g>
      </svg>
    </button>
  );
}
