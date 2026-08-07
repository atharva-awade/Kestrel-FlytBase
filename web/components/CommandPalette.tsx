"use client";

import { Command } from "cmdk";
import { AnimatePresence, motion } from "framer-motion";
import { Search } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

/** ⌘K palette. Navigation plus a direct line into search, so an operator who
    knows what they want never has to hunt for it. */
export function CommandPalette({
  nav,
}: { nav: { href: string; label: string; icon: any; hint: string }[] }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const router = useRouter();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const go = (href: string) => {
    router.push(href);
    setOpen(false);
    setQuery("");
  };

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="hidden items-center gap-2 rounded-full border px-3 py-1.5 text-[12px] text-[var(--ink-4)] transition-colors hover:bg-[var(--surface-2)] sm:flex"
        style={{ borderColor: "var(--line)" }}
      >
        <Search size={12} />
        <span>Search</span>
        <kbd className="mono rounded border px-1 text-[10px]" style={{ borderColor: "var(--line)" }}>
          ⌘K
        </kbd>
      </button>

      <AnimatePresence>
        {open && (
          <>
            <motion.div
              initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
              onClick={() => setOpen(false)}
              className="fixed inset-0 z-50 backdrop-blur-sm"
              style={{ background: "rgba(8,12,22,0.4)" }}
            />
            <motion.div
              initial={{ opacity: 0, scale: 0.97, y: -8 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.97, y: -8 }}
              transition={{ type: "spring", stiffness: 340, damping: 28 }}
              className="fixed left-1/2 top-[18vh] z-50 w-[min(560px,92vw)] -translate-x-1/2 overflow-hidden rounded-2xl border shadow-[var(--shadow-lg)]"
              style={{ background: "var(--surface)", borderColor: "var(--line)" }}
            >
              <Command shouldFilter>
                <div className="flex items-center gap-2 border-b px-4">
                  <Search size={15} className="text-[var(--ink-4)]" />
                  <Command.Input
                    value={query}
                    onValueChange={setQuery}
                    autoFocus
                    placeholder="Go to a view, or search the footage…"
                    className="h-12 flex-1 bg-transparent text-[14px] outline-none placeholder:text-[var(--ink-4)]"
                  />
                </div>
                <Command.List className="max-h-80 overflow-y-auto p-2">
                  <Command.Empty className="px-3 py-6 text-center text-[12.5px] text-[var(--ink-4)]">
                    Nothing matched.
                  </Command.Empty>

                  <Command.Group heading="Views" className="[&_[cmdk-group-heading]]:eyebrow [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1">
                    {nav.map((n) => (
                      <Command.Item
                        key={n.href}
                        value={`${n.label} ${n.hint}`}
                        onSelect={() => go(n.href)}
                        className="flex cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-2 text-[13px] data-[selected=true]:bg-[var(--accent-soft)]"
                      >
                        <n.icon size={14} className="text-[var(--ink-4)]" />
                        <span>{n.label}</span>
                        <span className="ml-auto text-[11px] text-[var(--ink-4)]">{n.hint}</span>
                      </Command.Item>
                    ))}
                  </Command.Group>

                  {query.trim().length > 2 && (
                    <Command.Group heading="Search" className="[&_[cmdk-group-heading]]:eyebrow [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1">
                      <Command.Item
                        value={`search ${query}`}
                        onSelect={() => go(`/investigate?q=${encodeURIComponent(query)}`)}
                        className="flex cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-2 text-[13px] data-[selected=true]:bg-[var(--accent-soft)]"
                      >
                        <Search size={14} style={{ color: "var(--accent)" }} />
                        Search footage for “{query}”
                      </Command.Item>
                    </Command.Group>
                  )}
                </Command.List>
              </Command>
            </motion.div>
          </>
        )}
      </AnimatePresence>
    </>
  );
}
