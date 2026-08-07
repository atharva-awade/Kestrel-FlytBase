"use client";

import { AnimatePresence, motion } from "framer-motion";
import {
  Boxes, Radio, Search, ScrollText, Sparkles, Gauge, Network, Globe2, Menu, X,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { AskRail } from "@/components/ask/AskRail";
import { DeployOverlay } from "@/components/deploy/DeployOverlay";
import { PageTransition } from "@/components/ui/PageTransition";
import { CommandPalette } from "@/components/CommandPalette";
import { ThemeToggle } from "@/components/ThemeToggle";
import { cn } from "@/lib/format";

const NAV = [
  { href: "/command", label: "Command", icon: Globe2, hint: "Portfolio globe" },
  { href: "/console", label: "Console", icon: Radio, hint: "Live operations" },
  { href: "/investigate", label: "Investigate", icon: Search, hint: "Hybrid search" },
  { href: "/entities", label: "Entities", icon: Boxes, hint: "Persistent subjects" },
  { href: "/rules", label: "Rules", icon: ScrollText, hint: "Rules studio" },
  { href: "/analyst", label: "Analyst", icon: Sparkles, hint: "Ask KESTREL" },
  { href: "/evals", label: "Evals", icon: Gauge, hint: "Measured results" },
  { href: "/architecture", label: "Architecture", icon: Network, hint: "How it works" },
];

export function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [mobileNav, setMobileNav] = useState(false);
  const isLanding = pathname === "/";

  useEffect(() => setMobileNav(false), [pathname]);

  // The landing page is its own world: no chrome, full-bleed.
  if (isLanding) return <>{children}</>;

  return (
    <div className="flex min-h-screen flex-col">
      <header className="glass sticky top-0 z-40 border-b">
        <div className="flex h-14 items-center gap-3 px-4">
          <Link href="/" className="group flex items-center gap-2.5 shrink-0">
            <Mark />
            <span className="hidden text-[15px] font-bold tracking-[-0.04em] sm:block">
              KESTREL
            </span>
          </Link>

          <nav className="ml-3 hidden items-center gap-0.5 lg:flex">
            {NAV.map((item) => {
              const active = pathname.startsWith(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  title={item.hint}
                  className={cn(
                    "relative rounded-lg px-3 py-1.5 text-[13px] font-medium transition-colors",
                    active
                      ? "text-[var(--accent-ink)]"
                      : "text-[var(--ink-3)] hover:text-[var(--ink)]",
                  )}
                >
                  {active && (
                    <motion.span
                      layoutId="nav-active"
                      className="absolute inset-0 rounded-lg bg-[var(--accent-soft)]"
                      transition={{ type: "spring", stiffness: 380, damping: 32 }}
                    />
                  )}
                  <span className="relative">{item.label}</span>
                </Link>
              );
            })}
          </nav>

          <div className="ml-auto flex items-center gap-2">
            <CommandPalette nav={NAV} />
            <ThemeToggle compact />
            <button
              onClick={() => setMobileNav((v) => !v)}
              className="grid h-[30px] w-[30px] place-items-center rounded-full border lg:hidden"
              style={{ borderColor: "var(--line)" }}
              aria-label="Menu"
            >
              {mobileNav ? <X size={15} /> : <Menu size={15} />}
            </button>
          </div>
        </div>

        <AnimatePresence>
          {mobileNav && (
            <motion.nav
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: "auto", opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              className="overflow-hidden border-t lg:hidden"
            >
              <div className="grid grid-cols-2 gap-1 p-3">
                {NAV.map((item) => (
                  <Link
                    key={item.href}
                    href={item.href}
                    className="flex items-center gap-2 rounded-lg px-3 py-2 text-[13px] hover:bg-[var(--surface-2)]"
                  >
                    <item.icon size={14} className="text-[var(--ink-4)]" />
                    {item.label}
                  </Link>
                ))}
              </div>
            </motion.nav>
          )}
        </AnimatePresence>
      </header>

      <div className="flex flex-1 overflow-hidden">
        <main className="min-w-0 flex-1 overflow-y-auto">
          <PageTransition>{children}</PageTransition>
        </main>
        {/* Ask KESTREL is omnipresent by design: it is a control plane, not a
            page. Every route can be driven from it. */}
        <AskRail />
        {/* Mounted once at the shell so any deck can dispatch a drone. */}
        <DeployOverlay />
      </div>
    </div>
  );
}

function Mark() {
  return (
    <span className="relative grid h-7 w-7 place-items-center">
      <svg width="26" height="26" viewBox="0 0 32 32" fill="none">
        <circle cx="16" cy="16" r="14.5" stroke="var(--line-2)" strokeWidth="1" />
        <circle cx="16" cy="16" r="9.5" stroke="var(--line-2)" strokeWidth="0.75" />
        {/* A hovering raptor abstracted to a pair of swept wings over an aperture. */}
        <path
          d="M6.5 18.5c3.6-.4 6.2-2.1 8-5 .5-.8 1.5-.8 2 0 1.8 2.9 4.4 4.6 8 5"
          stroke="var(--accent)"
          strokeWidth="2"
          strokeLinecap="round"
          fill="none"
        />
        <circle cx="16" cy="15" r="2.1" fill="var(--accent)" />
      </svg>
      <span className="radar-sweep pointer-events-none absolute inset-0">
        <svg width="26" height="26" viewBox="0 0 32 32">
          <path d="M16 16 L16 1.5 A14.5 14.5 0 0 1 26 5.5 Z" fill="var(--accent)" opacity="0.1" />
        </svg>
      </span>
    </span>
  );
}
