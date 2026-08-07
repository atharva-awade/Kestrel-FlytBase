"use client";

import { AnimatePresence, motion } from "framer-motion";
import Link from "next/link";
import { useEffect, useState } from "react";

import { CHAPTERS, chapterAt, useStory } from "@/lib/story";

/**
 * Everything drawn *over* the 3D stage: the chapter card, the progress rail, the
 * telemetry strip and the loading state.
 *
 * It reads `chapter`, a small integer, rather than `progress`, so the React tree
 * re-renders seven times across the whole story instead of sixty times a second.
 */
export default function StoryOverlay() {
  const ready = useStory((s) => s.ready);
  const [chapter, setChapter] = useState(0);

  useEffect(
    () =>
      useStory.subscribe((s) => {
        const next = chapterAt(s.progress);
        setChapter((prev) => (prev === next ? prev : next));
      }),
    [],
  );

  const c = CHAPTERS[chapter];

  return (
    <>
      {/* ── loading ─────────────────────────────────────────────────────── */}
      <AnimatePresence>
        {!ready && (
          <motion.div
            className="pointer-events-none absolute inset-0 z-30 grid place-items-center"
            exit={{ opacity: 0 }}
            transition={{ duration: 0.6, ease: "easeOut" }}
          >
            <div className="text-center">
              <div className="pulse-ring relative mx-auto mb-4 h-10 w-10 rounded-full border border-[var(--accent)]/50" />
              <p className="mono text-[10.5px] uppercase tracking-[0.22em] text-[var(--ink-4)]">
                Establishing link
              </p>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* A scrim under the copy. The airframe is pushed right of frame centre, but
          a dark rotor arm swinging through the left third during the orbit would
          still put white-on-grey text on screen, so this guarantees the contrast
          rather than hoping the choreography avoids it. */}
      <div
        className="pointer-events-none absolute inset-y-0 left-0 z-10 hidden w-[58%] lg:block"
        style={{
          background:
            "linear-gradient(90deg, var(--bg) 0%, color-mix(in oklab, var(--bg) 82%, transparent) 42%, transparent 100%)",
        }}
      />

      {/* ── chapter card ────────────────────────────────────────────────── */}
      <div className="pointer-events-none absolute inset-x-0 bottom-0 z-20 lg:inset-y-0 lg:right-auto lg:left-0 lg:flex lg:items-center">
        <div className="mx-auto w-full max-w-6xl px-5 pb-16 sm:px-8 lg:pb-0">
          <div className="max-w-[34rem]">
            <AnimatePresence mode="wait">
              <motion.div
                key={c.key}
                initial={{ opacity: 0, y: 22, filter: "blur(6px)" }}
                animate={{ opacity: 1, y: 0, filter: "blur(0px)" }}
                exit={{ opacity: 0, y: -14, filter: "blur(6px)" }}
                transition={{ duration: 0.55, ease: [0.22, 1, 0.36, 1] }}
              >
                <p className="eyebrow mb-3 text-[var(--accent-ink)]">{c.eyebrow}</p>
                <h1 className="display text-[clamp(1.9rem,4.6vw,3.35rem)] leading-[1.05] text-[var(--ink)]">
                  {c.title}
                </h1>
                <p className="mt-4 max-w-[30rem] text-[14px] leading-relaxed text-[var(--ink-2)] sm:text-[15px]">
                  {c.line}
                </p>

                {c.proof && (
                  <div className="mono mt-5 inline-flex items-center gap-2 rounded-full border border-[var(--line)] bg-[var(--surface)]/70 px-3 py-1.5 text-[10.5px] tracking-[0.08em] text-[var(--ink-3)] backdrop-blur">
                    <span className="live-dot" />
                    {c.proof}
                  </div>
                )}

                {chapter === 0 && (
                  <div className="pointer-events-auto mt-8 flex flex-wrap items-center gap-3">
                    <Link
                      href="/command"
                      className="rounded-full bg-[var(--accent)] px-5 py-2.5 text-[13px] font-medium text-white shadow-[0_8px_24px_-8px_var(--accent)] transition hover:brightness-110"
                    >
                      Open the console
                    </Link>
                    <Link
                      href="/analyst"
                      className="rounded-full border border-[var(--line-2)] bg-[var(--surface)]/70 px-5 py-2.5 text-[13px] font-medium text-[var(--ink)] backdrop-blur transition hover:border-[var(--accent)]"
                    >
                      Ask KESTREL
                    </Link>
                  </div>
                )}
              </motion.div>
            </AnimatePresence>
          </div>
        </div>
      </div>

      {/* ── progress rail ───────────────────────────────────────────────── */}
      <div className="pointer-events-none absolute top-1/2 right-5 z-20 hidden -translate-y-1/2 lg:block">
        <ul className="flex flex-col gap-3">
          {CHAPTERS.map((ch, i) => (
            <li key={ch.key} className="flex items-center justify-end gap-2.5">
              <span
                className={`mono text-[9.5px] tracking-[0.16em] uppercase transition-opacity duration-300 ${
                  i === chapter ? "text-[var(--ink-2)] opacity-100" : "opacity-0"
                }`}
              >
                {ch.eyebrow.split("·").pop()?.trim()}
              </span>
              <span
                className={`block rounded-full transition-all duration-500 ${
                  i === chapter
                    ? "h-6 w-[3px] bg-[var(--accent)]"
                    : "h-[3px] w-[3px] bg-[var(--line-2)]"
                }`}
              />
            </li>
          ))}
        </ul>
      </div>

      {/* ── scroll hint ─────────────────────────────────────────────────── */}
      <AnimatePresence>
        {chapter === 0 && ready && (
          <motion.div
            className="pointer-events-none absolute bottom-5 left-1/2 z-20 hidden -translate-x-1/2 lg:block"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
          >
            <motion.div
              className="mono flex flex-col items-center gap-2 text-[9.5px] tracking-[0.24em] text-[var(--ink-4)] uppercase"
              animate={{ y: [0, 6, 0] }}
              transition={{ duration: 2.2, repeat: Infinity, ease: "easeInOut" }}
            >
              Scroll
              <span className="h-8 w-px bg-gradient-to-b from-[var(--line-2)] to-transparent" />
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}
