"use client";

import { animate, motion, useMotionValue, useReducedMotion, useTransform } from "framer-motion";
import { useEffect } from "react";
import type { ReactNode } from "react";
import { cn, sevClass } from "@/lib/format";

/* Small, shared building blocks. Kept together so spacing, radii and weights stay
   consistent across eight pages without a component library's ceremony. */

export function Card({
  children, className, lift = false, ...rest
}: { children: ReactNode; className?: string; lift?: boolean } & React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={cn("card", lift && "card-lift", className)} {...rest}>
      {children}
    </div>
  );
}

export function SectionTitle({
  eyebrow, title, subtitle, right,
}: { eyebrow?: string; title: string; subtitle?: string; right?: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 mb-4">
      <div className="min-w-0">
        {eyebrow && <div className="eyebrow mb-1.5">{eyebrow}</div>}
        <h2 className="text-[17px] font-semibold tracking-[-0.02em]">{title}</h2>
        {subtitle && (
          <p className="mt-1 text-[13px] leading-relaxed text-[var(--ink-3)] max-w-2xl">{subtitle}</p>
        )}
      </div>
      {right}
    </div>
  );
}

/**
 * A number that arrives rather than appears.
 *
 * Counting a statistic up to its value reads as the system reporting a
 * measurement, where a number that simply pops into place reads as static
 * markup. It is a small thing that changes how alive a dashboard feels.
 *
 * Only numeric values animate; anything else renders immediately, and under
 * `prefers-reduced-motion` nothing counts at all.
 */
function CountUp({ to, color }: { to: number; color: string }) {
  const reduced = useReducedMotion();
  const mv = useMotionValue(0);
  // Integers stay integers; a rate like 82.5 keeps its decimal.
  const decimals = Number.isInteger(to) ? 0 : 1;
  const text = useTransform(mv, (v) => v.toFixed(decimals));

  useEffect(() => {
    if (reduced) {
      mv.set(to);
      return;
    }
    const controls = animate(mv, to, {
      duration: Math.min(1.1, 0.35 + Math.abs(to) / 900),
      ease: [0.22, 1, 0.36, 1],
    });
    return () => controls.stop();
  }, [to, mv, reduced]);

  return (
    <motion.span
      className="tnum text-[26px] font-semibold tracking-[-0.03em]"
      style={{ color }}
    >
      {text}
    </motion.span>
  );
}

export function Stat({
  label, value, unit, hint, tone = "default",
}: {
  label: string; value: ReactNode; unit?: string; hint?: string;
  tone?: "default" | "accent" | "ok" | "warn";
}) {
  const color =
    tone === "accent" ? "var(--accent)"
    : tone === "ok" ? "var(--ok)"
    : tone === "warn" ? "var(--sev-medium)"
    : "var(--ink)";
  return (
    <div className="min-w-0">
      <div className="eyebrow">{label}</div>
      <div className="mt-1 flex items-baseline gap-1">
        {typeof value === "number" && Number.isFinite(value) ? (
          <CountUp to={value} color={color} />
        ) : (
          <span className="tnum text-[26px] font-semibold tracking-[-0.03em]" style={{ color }}>
            {value}
          </span>
        )}
        {unit && <span className="text-[12px] text-[var(--ink-3)]">{unit}</span>}
      </div>
      {hint && <div className="mt-0.5 text-[11.5px] text-[var(--ink-4)]">{hint}</div>}
    </div>
  );
}

export function SeverityChip({ severity, children }: { severity: string; children?: ReactNode }) {
  return (
    <span
      className={cn(
        sevClass(severity),
        "sev-chip inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[10.5px] font-semibold uppercase tracking-wider",
      )}
    >
      <span className="sev-dot" style={{ width: 5, height: 5, boxShadow: "none" }} />
      {children ?? severity}
    </span>
  );
}

export function Pill({
  children, tone = "muted", className,
}: { children: ReactNode; tone?: "muted" | "accent" | "ok" | "warn" | "danger"; className?: string }) {
  const tones = {
    muted: "bg-[var(--surface-2)] text-[var(--ink-3)] border-[var(--line)]",
    accent: "bg-[var(--accent-soft)] text-[var(--accent-ink)] border-[color-mix(in_oklab,var(--accent)_28%,transparent)]",
    ok: "bg-[color-mix(in_oklab,var(--ok)_12%,transparent)] text-[var(--ok)] border-[color-mix(in_oklab,var(--ok)_28%,transparent)]",
    warn: "bg-[color-mix(in_oklab,var(--sev-medium)_12%,transparent)] text-[var(--sev-medium)] border-[color-mix(in_oklab,var(--sev-medium)_28%,transparent)]",
    danger: "bg-[color-mix(in_oklab,var(--sev-critical)_12%,transparent)] text-[var(--sev-critical)] border-[color-mix(in_oklab,var(--sev-critical)_28%,transparent)]",
  }[tone];
  return (
    <span className={cn("inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium", tones, className)}>
      {children}
    </span>
  );
}

/** Marks anything not backed by a live feed. Used everywhere such data appears:
    the portfolio view must never imply more live aircraft than exist. */
export function SimulatedBadge({ compact = false }: { compact?: boolean }) {
  return (
    <Pill tone="warn" className={compact ? "px-1.5 py-0 text-[9.5px]" : undefined}>
      SIMULATED
    </Pill>
  );
}

export function LiveBadge({ label = "LIVE" }: { label?: string }) {
  return (
    <Pill tone="ok">
      <span className="live-dot" />
      {label}
    </Pill>
  );
}

export function Empty({
  title, hint, action,
}: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed px-6 py-12 text-center"
         style={{ borderColor: "var(--line-2)" }}>
      <div className="text-[14px] font-medium text-[var(--ink-2)]">{title}</div>
      {hint && <div className="max-w-md text-[12.5px] leading-relaxed text-[var(--ink-4)]">{hint}</div>}
      {action}
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("skeleton rounded-lg", className)} />;
}

export function Bar({ value, max = 1, tone }: { value: number; max?: number; tone?: string }) {
  const w = Math.max(0, Math.min(1, value / (max || 1)));
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-[var(--surface-3)]">
      <motion.div
        className="h-full rounded-full"
        style={{ background: tone ?? "var(--accent)" }}
        initial={{ width: 0 }}
        animate={{ width: `${w * 100}%` }}
        transition={{ type: "spring", stiffness: 120, damping: 22 }}
      />
    </div>
  );
}

export function KeyValue({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-[12.5px]">
      {items.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-[var(--ink-4)] whitespace-nowrap">{k}</dt>
          <dd className="tnum text-right text-[var(--ink)]">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

/** A citation the operator can click back to the evidence. */
export function Cite({ id, onClick }: { id: string; onClick?: (id: string) => void }) {
  return (
    <button
      onClick={() => onClick?.(id)}
      className="mono rounded border px-1 py-[1px] text-[10.5px] text-[var(--accent-ink)] transition-colors hover:bg-[var(--accent-soft)]"
      style={{ borderColor: "color-mix(in oklab, var(--accent) 25%, transparent)" }}
    >
      {id}
    </button>
  );
}

export const fadeUp = {
  initial: { opacity: 0, y: 8 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: 0.34, ease: [0.22, 1, 0.36, 1] as const },
};

export function stagger(i: number, base = 0.03) {
  return { ...fadeUp, transition: { ...fadeUp.transition, delay: i * base } };
}
