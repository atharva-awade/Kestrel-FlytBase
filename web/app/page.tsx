"use client";

import { motion, useReducedMotion } from "framer-motion";
import gsap from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import Lenis from "lenis";
import {
  ArrowRight, Boxes, Compass, Eye, Gauge, Globe2, Layers, Plane, Radar,
  ScrollText, Search, ShieldCheck, Sparkles, Zap,
} from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import StoryOverlay from "@/components/hero/StoryOverlay";
import { StaticFallback, WebGLBoundary } from "@/components/hero/WebGLBoundary";
import { ThemeToggle } from "@/components/ThemeToggle";
import { api } from "@/lib/api";
import { CHAPTERS, useStory } from "@/lib/story";

/* WebGL cannot be server-rendered, and the model is a 2 MB fetch. Both are
 * reasons to keep this out of the initial bundle entirely. */
const DroneCanvas = dynamic(() => import("@/components/hero/DroneCanvas"), {
  ssr: false,
});

/**
 * Landing page.
 *
 * A scroll-choreographed story: a Matrice 300 RTK, the class of aircraft KESTREL
 * is built for, holds screen centre while the camera orbits it through seven
 * chapters. The aircraft never moves; only the camera does, which is what keeps
 * the subject composed at every scroll position instead of drifting.
 *
 * Below the story, the argument in detail. Every number quoted here comes from
 * `data/eval/*.json` and was measured on this machine, including the one that
 * makes the system look worse than the design plan predicted.
 */
export default function Landing() {
  const stage = useRef<HTMLDivElement>(null);
  const reduced = useReducedMotion() ?? false;
  const active = useStory((s) => s.active);
  const [health, setHealth] = useState<any>(null);

  useEffect(() => {
    (async () => setHealth(await api.health()))();
  }, []);

  useEffect(() => {
    if (reduced) {
      // No inertial scroll, no scroll-driven camera. The hero holds its pose and
      // the page behaves like an ordinary document.
      useStory.getState().setProgress(0);
      const onScroll = () =>
        useStory.getState().setActive(window.scrollY < window.innerHeight * 0.9);
      onScroll();
      window.addEventListener("scroll", onScroll, { passive: true });
      return () => window.removeEventListener("scroll", onScroll);
    }

    const lenis = new Lenis({ lerp: 0.085 });
    let raf = 0;
    const loop = (t: number) => {
      lenis.raf(t);
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);

    gsap.registerPlugin(ScrollTrigger);
    lenis.on("scroll", ScrollTrigger.update);

    const trigger = ScrollTrigger.create({
      trigger: stage.current,
      start: "top top",
      end: "bottom bottom",
      scrub: 1,
      onUpdate: (self) => useStory.getState().setProgress(self.progress),
      // Rendering stops the moment the story leaves the viewport.
      onToggle: (self) => useStory.getState().setActive(self.isActive),
    });

    return () => {
      trigger.kill();
      cancelAnimationFrame(raf);
      lenis.destroy();
    };
  }, [reduced]);

  return (
    <div className="relative">
      {/* ── nav ──────────────────────────────────────────────────────────── */}
      <header className="glass fixed top-0 right-0 left-0 z-50 border-b">
        <div className="mx-auto flex h-14 max-w-6xl items-center gap-3 px-5">
          <Mark />
          <span className="text-[15px] font-bold tracking-[-0.04em]">KESTREL</span>
          <span className="mono hidden items-center gap-4 pl-3 text-[10px] tracking-[0.16em] text-[var(--ink-4)] uppercase lg:flex">
            <span>M300 RTK</span>
            <span>Dock · PLANT-01</span>
            <span className="flex items-center gap-1.5 text-[var(--ink-3)]">
              <span className="live-dot" />
              Armed
            </span>
          </span>
          <div className="ml-auto flex items-center gap-2">
            <ThemeToggle compact />
            <Link
              href="/command"
              className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[12.5px] font-semibold text-white"
              style={{ background: "var(--accent)" }}
            >
              Launch console <ArrowRight size={13} />
            </Link>
          </div>
        </div>
      </header>

      {/* ── the 3D stage ─────────────────────────────────────────────────── */}
      <div
        className="aurora grain pointer-events-none fixed inset-0 z-0 transition-opacity duration-500"
        style={{ opacity: active ? 1 : 0 }}
        aria-hidden={!active}
      >
        <div className="hairline-grid pointer-events-none absolute inset-0 opacity-40" />
        <div className="absolute inset-0">
          <WebGLBoundary fallback={<StaticFallback />}>
            <DroneCanvas reduced={reduced} />
          </WebGLBoundary>
        </div>
        <StoryOverlay />
      </div>

      {/* The story has no content of its own; it exists to generate the scroll
          distance the camera choreography is sampled against. */}
      <section
        ref={stage}
        className="pointer-events-none relative z-10"
        style={{ height: reduced ? "100vh" : "660vh" }}
        aria-hidden
      />

      {/* ── the argument, in detail ──────────────────────────────────────── */}
      <div className="relative z-10" style={{ background: "var(--bg)" }}>
        {health && (
          <div className="border-y" style={{ background: "var(--surface-2)" }}>
            <div className="mono mx-auto flex max-w-6xl flex-wrap items-center gap-x-6 gap-y-1 px-5 py-2.5 text-[10.5px] tracking-[0.08em] text-[var(--ink-4)]">
              <span>
                mode <strong className="text-[var(--ink-3)]">{health.mode}</strong>
              </span>
              {health.runs_without_api_key && (
                <span style={{ color: "var(--ok)" }}>runs with no API key</span>
              )}
              <span>{health.cassettes?.count_on_disk} recorded cassettes</span>
              <span>{String(health.storage?.vector_index)}</span>
              <span className="ml-auto hidden sm:inline">live from the running backend</span>
            </div>
          </div>
        )}

        {/* With motion reduced there is no camera choreography, so the six
            chapters after the hero would never be readable. They are restated
            here as plain text rather than silently dropped. */}
        {reduced && (
          <Section>
            <div className="eyebrow mb-4">the story</div>
            <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
              {CHAPTERS.slice(1).map((c) => (
                <div key={c.key} className="card h-full p-5">
                  <div className="eyebrow mb-2 text-[var(--accent-ink)]">{c.eyebrow}</div>
                  <div className="text-[14.5px] font-semibold">{c.title}</div>
                  <p className="mt-1.5 text-[12.5px] leading-relaxed text-[var(--ink-3)]">
                    {c.line}
                  </p>
                  {c.proof && (
                    <div className="mono mt-3 text-[10.5px] text-[var(--ink-4)]">{c.proof}</div>
                  )}
                </div>
              ))}
            </div>
          </Section>
        )}

        {/* ── problem ─────────────────────────────────────────────────── */}
        <Section>
          <div className="grid gap-10 lg:grid-cols-2 lg:items-center">
            <Reveal>
              <div className="eyebrow mb-3">the problem</div>
              <h2 className="display text-[clamp(1.7rem,3.4vw,2.5rem)]">
                Everything is recorded.
                <br />
                Almost nothing is watched.
              </h2>
              <p className="mt-4 text-[14.5px] leading-relaxed text-[var(--ink-2)]">
                Security footage is reviewed after something has already happened. A
                guard cannot watch every camera, and an operator who receives forty
                alerts a night learns to dismiss all of them, at which point the
                system protects nothing.
              </p>
              <p className="mt-3 text-[14.5px] leading-relaxed text-[var(--ink-2)]">
                The hard problem is not detection. It is deciding what deserves
                attention, remembering enough context to know, and being right often
                enough to stay trusted.
              </p>
            </Reveal>

            <Reveal delay={0.1}>
              <div className="card grain relative overflow-hidden p-6">
                <div className="flex items-end gap-[2px]" style={{ height: 130 }}>
                  {Array.from({ length: 96 }, (_, i) => {
                    const matters = [17, 18, 44, 45, 46, 72, 88].includes(i);
                    return (
                      <motion.div
                        key={i}
                        initial={{ height: "6%" }}
                        whileInView={{ height: matters ? "100%" : "6%" }}
                        viewport={{ once: true }}
                        transition={{
                          delay: i * 0.006,
                          type: "spring",
                          stiffness: 130,
                          damping: 18,
                        }}
                        className="flex-1 rounded-sm"
                        style={{ background: matters ? "var(--sev-critical)" : "var(--line-2)" }}
                      />
                    );
                  })}
                </div>
                <div className="mt-4 flex items-baseline gap-3">
                  <span className="tnum text-[30px] font-bold" style={{ color: "var(--accent)" }}>
                    7
                  </span>
                  <span className="text-[13px] text-[var(--ink-3)]">
                    moments that mattered, out of an eight-hour shift
                  </span>
                </div>
              </div>
            </Reveal>
          </div>
        </Section>

        {/* ── cascade ─────────────────────────────────────────────────── */}
        <Section tint>
          <Reveal>
            <div className="eyebrow mb-3">how it works</div>
            <h2 className="display max-w-3xl text-[clamp(1.7rem,3.4vw,2.5rem)]">
              Five tiers. Each one only runs when the tier before it earns the spend.
            </h2>
            <p className="mt-4 max-w-2xl text-[14.5px] leading-relaxed text-[var(--ink-2)]">
              The free inference tier allows about 40 requests a minute against roughly
              57,600 frames a shift. Captioning every frame is impossible and pointless.
              Every architectural decision in KESTREL follows from that one constraint.
            </p>
          </Reveal>

          <div className="mt-8 space-y-2.5">
            {CASCADE.map((row, i) => (
              <Reveal key={row.t} delay={i * 0.05}>
                <div className="card card-lift flex items-start gap-4 p-4">
                  <div
                    className="mono grid h-9 w-11 shrink-0 place-items-center rounded-lg text-[12px] font-bold"
                    style={{ background: "var(--accent-soft)", color: "var(--accent-ink)" }}
                  >
                    {row.t}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <row.i size={14} style={{ color: "var(--accent)" }} />
                      <span className="text-[14px] font-semibold">{row.n}</span>
                      <span
                        className="mono rounded-full border px-2 py-0.5 text-[10px] text-[var(--ink-4)]"
                        style={{ borderColor: "var(--line)" }}
                      >
                        {row.c}
                      </span>
                    </div>
                    <p className="mt-1 text-[13.5px] leading-relaxed text-[var(--ink-2)]">
                      {row.d}
                    </p>
                  </div>
                </div>
              </Reveal>
            ))}
          </div>

          {/* the number that made us look worse */}
          <Reveal delay={0.1}>
            <div className="card mt-6 p-6">
              <div className="grid gap-6 sm:grid-cols-[auto_1fr] sm:items-center">
                <div className="flex gap-8">
                  <div>
                    <div className="tnum text-[34px] font-bold" style={{ color: "var(--sev-medium)" }}>
                      38.9%
                    </div>
                    <div className="mt-0.5 text-[11.5px] text-[var(--ink-4)]">
                      gate efficiency, real footage
                    </div>
                  </div>
                  <div>
                    <div className="tnum text-[34px] font-bold" style={{ color: "var(--ok)" }}>
                      96.7%
                    </div>
                    <div className="mt-0.5 text-[11.5px] text-[var(--ink-4)]">
                      constructed idle context
                    </div>
                  </div>
                </div>
                <p className="text-[13.5px] leading-relaxed text-[var(--ink-2)]">
                  The design plan predicted about 94%. Measurement said 38.9%, because
                  every licence-clean clip available is a CV demo reel authored for
                  continuous motion, which is close to worst case for a gate that skips
                  static frames. Both figures are reported, with their conditions, rather
                  than only the flattering one.
                </p>
              </div>
            </div>
          </Reveal>
        </Section>

        {/* ── beyond detection ────────────────────────────────────────── */}
        <Section>
          <Reveal>
            <div className="eyebrow mb-3">beyond detection</div>
            <h2 className="display max-w-3xl text-[clamp(1.7rem,3.4vw,2.5rem)]">
              The interesting problems start after the bounding box.
            </h2>
            <p className="mt-4 max-w-2xl text-[14.5px] leading-relaxed text-[var(--ink-2)]">
              Nine capabilities the brief did not ask for, each built because the
              assignment is only interesting past the point where detection stops.
            </p>
          </Reveal>

          <div className="mt-8 grid gap-3 md:grid-cols-2 lg:grid-cols-3">
            {FEATURES.map((f, i) => (
              <Reveal key={f.t} delay={i * 0.04}>
                <Link href={f.href} className="card card-lift group block h-full p-5">
                  <f.i size={17} style={{ color: "var(--accent)" }} />
                  <div className="mt-3 text-[14.5px] font-semibold">{f.t}</div>
                  <p className="mt-1.5 text-[13px] leading-relaxed text-[var(--ink-3)]">{f.d}</p>
                  <div
                    className="mt-3 flex items-center gap-1 text-[12px] font-medium"
                    style={{ color: "var(--accent)" }}
                  >
                    Open
                    <ArrowRight
                      size={12}
                      className="transition-transform group-hover:translate-x-0.5"
                    />
                  </div>
                </Link>
              </Reveal>
            ))}
          </div>
        </Section>

        {/* ── an alert you can fly to ─────────────────────────────────── */}
        <Section tint>
          <div className="grid gap-10 lg:grid-cols-2 lg:items-center">
            <Reveal>
              <div className="eyebrow mb-3">dispatch</div>
              <h2 className="display text-[clamp(1.7rem,3.4vw,2.5rem)]">
                An alert you can fly to.
              </h2>
              <p className="mt-4 text-[14.5px] leading-relaxed text-[var(--ink-2)]">
                A description is not actionable at 02:00 with one guard on shift. Every
                alert carries the pixel footprint projected to ground truth through the
                telemetry (altitude, gimbal pitch and yaw) with an honest accuracy
                radius derived from the projection geometry rather than asserted.
              </p>
              <p className="mt-3 text-[14.5px] leading-relaxed text-[var(--ink-2)]">
                Bearing and ETA from the dock, a recommended stand-off altitude, and a
                geofence verdict, so the decision in front of the operator is “launch or
                do not” rather than “interpret this”.
              </p>
            </Reveal>

            <Reveal delay={0.1}>
              <div className="card overflow-hidden">
                <div
                  className="flex items-center gap-2 border-b px-4 py-2.5"
                  style={{ borderColor: "var(--line)" }}
                >
                  <span className="sev-critical sev-dot" />
                  <span className="text-[13px] font-semibold">
                    Person in restricted core · 02:14
                  </span>
                  <span className="mono ml-auto text-[10px] text-[var(--ink-4)]">
                    ALT-0a15-0007
                  </span>
                </div>
                <dl className="mono grid grid-cols-2 gap-px" style={{ background: "var(--line)" }}>
                  {DISPATCH.map((d) => (
                    <div key={d[0]} className="px-4 py-3" style={{ background: "var(--surface)" }}>
                      <dt className="text-[9.5px] tracking-[0.14em] text-[var(--ink-4)] uppercase">
                        {d[0]}
                      </dt>
                      <dd className="mt-1 text-[13px] font-semibold text-[var(--ink)]">{d[1]}</dd>
                    </div>
                  ))}
                </dl>
                <div className="px-4 py-3 text-[12px] leading-relaxed text-[var(--ink-3)]">
                  Flat-ground homography. The accuracy radius grows with slant range, and
                  the projection is stated as an estimate, because a coordinate with false
                  precision is worse than no coordinate at all.
                </div>
              </div>
            </Reveal>
          </div>
        </Section>

        {/* ── the closed loop ─────────────────────────────────────────── */}
        <Section>
          <div className="grid gap-10 lg:grid-cols-2 lg:items-center">
            <Reveal>
              <div className="eyebrow mb-3">the closed loop</div>
              <h2 className="display text-[clamp(1.7rem,3.4vw,2.5rem)]">
                An alert is not the end of the job.
              </h2>
              <p className="mt-4 text-[14.5px] leading-relaxed text-[var(--ink-2)]">
                KESTREL proposes a concrete flight, with waypoints, altitudes and coordinates,
                and checks it against battery reserve, geofence, wind and daylight before
                offering it.
              </p>
              <p className="mt-3 text-[14.5px] leading-relaxed text-[var(--ink-2)]">
                It cannot fly that mission on its own. Proposal and approval are separate
                code paths, enforced in the tool registry rather than in prompt wording,
                and a test asserts the agent's own loop can never set the approval flag.
              </p>
            </Reveal>

            <Reveal delay={0.1}>
              <div className="card p-6">
                {LOOP.map((step, i, arr) => (
                  <div key={step} className="flex gap-3">
                    <div className="flex flex-col items-center">
                      <div
                        className="grid h-7 w-7 shrink-0 place-items-center rounded-full text-[11px] font-bold"
                        style={{ background: "var(--accent-soft)", color: "var(--accent-ink)" }}
                      >
                        {i + 1}
                      </div>
                      {i < arr.length - 1 && (
                        <div className="my-1 w-px flex-1" style={{ background: "var(--line-2)" }} />
                      )}
                    </div>
                    <div className="pb-4 text-[13.5px] leading-relaxed text-[var(--ink-2)]">
                      {step}
                    </div>
                  </div>
                ))}
                <div
                  className="mt-1 rounded-lg border px-3 py-2 text-[12px] leading-relaxed text-[var(--ink-3)]"
                  style={{ borderColor: "var(--line)", background: "var(--surface-2)" }}
                >
                  The loop closes because the new vantage point re-enters perception:
                  the improvement is measured, not asserted.
                </div>
              </div>
            </Reveal>
          </div>
        </Section>

        {/* ── ask kestrel ─────────────────────────────────────────────── */}
        <Section tint>
          <Reveal>
            <div className="eyebrow mb-3">the control plane</div>
            <h2 className="display max-w-3xl text-[clamp(1.7rem,3.4vw,2.5rem)]">
              Ask it anything. It will tell you when it does not know.
            </h2>
            <p className="mt-4 max-w-2xl text-[14.5px] leading-relaxed text-[var(--ink-2)]">
              Conversation is not a feature bolted to the side; it is the way every
              capability is reachable. Answers arrive as live interface, not paragraphs,
              and each cited identifier is checked against the tool results that produced
              it. A citation that resolves to nothing is marked unverified rather than
              rendered as fact.
            </p>
          </Reveal>

          <div className="mt-8 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {[
              ["27", "tools", "every capability, callable"],
              ["8", "classes", "retrieve, analyse, author, act, operate, navigate, fleet, explain"],
              ["4", "gated", "no aircraft moves without a human decision"],
              ["0", "prompt-only guards", "the boundary is a code path, and a test proves it"],
            ].map(([n, label, sub], i) => (
              <Reveal key={label} delay={i * 0.05}>
                <div className="card h-full p-5">
                  <div className="tnum text-[32px] font-bold" style={{ color: "var(--accent)" }}>
                    {n}
                  </div>
                  <div className="mt-1 text-[13.5px] font-semibold">{label}</div>
                  <p className="mt-1.5 text-[12.5px] leading-relaxed text-[var(--ink-3)]">{sub}</p>
                </div>
              </Reveal>
            ))}
          </div>
        </Section>

        {/* ── measured ────────────────────────────────────────────────── */}
        <Section>
          <Reveal>
            <div className="eyebrow mb-3">measured, not claimed</div>
            <h2 className="display max-w-3xl text-[clamp(1.7rem,3.4vw,2.5rem)]">
              Every number on this page was produced by a benchmark in the repository.
            </h2>
          </Reveal>

          <div className="mt-8 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {RESULTS.map((r, i) => (
              <Reveal key={r.label} delay={i * 0.04}>
                <div className="card h-full p-5">
                  <div className="tnum text-[28px] font-bold" style={{ color: "var(--accent)" }}>
                    {r.value}
                  </div>
                  <div className="mt-1 text-[13px] font-semibold">{r.label}</div>
                  <p className="mt-1.5 text-[12px] leading-relaxed text-[var(--ink-3)]">{r.sub}</p>
                </div>
              </Reveal>
            ))}
          </div>

          <Reveal delay={0.1}>
            <p className="mt-6 text-[13px] leading-relaxed text-[var(--ink-3)]">
              The scenario suite weights true negatives (routine delivery, wildlife at
              the fence, shift change) equally with true positives. A security system
              that cries wolf gets switched off, and then it protects nothing.
            </p>
          </Reveal>
        </Section>

        {/* ── honesty ─────────────────────────────────────────────────── */}
        <Section tint>
          <Reveal>
            <div className="card p-6 sm:p-8">
              <div className="eyebrow mb-3">what is real, and what is not</div>
              <div className="grid gap-6 sm:grid-cols-2">
                <div>
                  <div className="mb-2 text-[13px] font-semibold" style={{ color: "var(--ok)" }}>
                    Real
                  </div>
                  <ul className="space-y-1.5 text-[13px] leading-relaxed text-[var(--ink-2)]">
                    {[
                      "The video: CC BY 4.0 footage through the real pipeline",
                      "The detections, tracks and embeddings",
                      "The captions, from a live vision-language model",
                      "The rules, memory, retrieval and audit chain",
                      "Every measured number on the evals page",
                    ].map((x) => (
                      <li key={x} className="flex gap-2">
                        <span style={{ color: "var(--ok)" }}>✓</span>
                        {x}
                      </li>
                    ))}
                  </ul>
                </div>
                <div>
                  <div
                    className="mb-2 text-[13px] font-semibold"
                    style={{ color: "var(--sev-medium)" }}
                  >
                    Simulated
                  </div>
                  <ul className="space-y-1.5 text-[13px] leading-relaxed text-[var(--ink-2)]">
                    {[
                      "The telemetry: there is no aircraft",
                      "The site geometry and zone definitions",
                      "Every fleet site except the flagship plant",
                      "Mission execution, integrated rather than flown",
                    ].map((x) => (
                      <li key={x} className="flex gap-2">
                        <span style={{ color: "var(--sev-medium)" }}>~</span>
                        {x}
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
              <p className="mt-5 text-[13px] leading-relaxed text-[var(--ink-3)]">
                Simulated data is labelled wherever it appears, including in the API
                payloads. A portfolio view implying more live aircraft than exist would be
                the one thing capable of discrediting everything else here.
              </p>
            </div>
          </Reveal>
        </Section>

        {/* ── cta ─────────────────────────────────────────────────────── */}
        <section className="aurora grain relative overflow-hidden border-t py-20">
          <div className="mx-auto max-w-3xl px-5 text-center">
            <Reveal>
              <h2 className="display text-[clamp(1.8rem,4vw,3rem)]">
                Ask it what happened last night.
              </h2>
              <p className="mx-auto mt-4 max-w-xl text-[15px] leading-relaxed text-[var(--ink-2)]">
                Everything KESTREL can do is reachable in conversation, and it will tell
                you honestly when it has no evidence to answer with.
              </p>
              <div className="mt-8 flex flex-wrap justify-center gap-3">
                <Link
                  href="/command"
                  className="flex items-center gap-2 rounded-xl px-5 py-3 text-[14px] font-semibold text-white"
                  style={{ background: "var(--accent)", boxShadow: "var(--shadow-accent)" }}
                >
                  <Globe2 size={16} /> Launch console
                </Link>
                <Link
                  href="/architecture"
                  className="flex items-center gap-2 rounded-xl border px-5 py-3 text-[14px] font-medium"
                  style={{ borderColor: "var(--line-2)" }}
                >
                  <Layers size={15} style={{ color: "var(--accent)" }} /> How it works
                </Link>
              </div>
            </Reveal>
          </div>
        </section>

        <footer className="border-t py-8">
          <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-3 px-5 text-[11.5px] text-[var(--ink-4)]">
            <Mark />
            <span className="font-semibold text-[var(--ink-3)]">KESTREL</span>
            <span>Autonomous drone security analyst</span>
            <span className="ml-auto">Made with ❤️ by Atharva Awade.</span>
          </div>
        </footer>
      </div>
    </div>
  );
}

/* ── content ────────────────────────────────────────────────────────────── */

const CASCADE = [
  { t: "0", n: "Gate", i: Zap, c: "free",
    d: "Perceptual hash, pixel delta and embedding novelty, all on CPU. Decides whether a frame is worth spending anything on at all." },
  { t: "1", n: "Detect", i: Eye, c: "~12 ms",
    d: "YOLO11 on the local GPU, or Grounding DINO when the query is open-vocabulary. On-device, so no API budget and no rate limit." },
  { t: "1.5", n: "Track", i: Layers, c: "~free",
    d: "ByteTrack. Identity that persists across frames. Without it, every claim about duration is a guess." },
  { t: "2", n: "Embed", i: Boxes, c: "cheap",
    d: "Joint image/text vectors in one 2048-dimension space, for re-identification and for search that works on appearance." },
  { t: "3", n: "Perceive", i: Sparkles, c: "~1.3 s",
    d: "A vision-language model returns a structured scene graph: objects, colours, activities, anomalies. Never prose." },
  { t: "4", n: "Escalate", i: Compass, c: "async",
    d: "The deep model measured 57–84 s, so it runs out of band and upgrades the record afterwards, split by the kind of doubt." },
];

const FEATURES = [
  { i: ScrollText, t: "Rules you write in English", href: "/rules",
    d: "Type a requirement, get a validated temporal rule, then see what it would have done against indexed history before it is allowed to fire." },
  { i: Search, t: "Open-vocabulary detection", href: "/investigate",
    d: "Ask for “a traffic cone” and the detector grounds the phrase directly, with no retraining and no fixed class list." },
  { i: Boxes, t: "Memory that spans days", href: "/entities",
    d: "The same vehicle, seventh visit, first time ever after midnight. No single frame is alarming; only the pattern is." },
  { i: Layers, t: "A temporal memory pyramid", href: "/architecture",
    d: "Frame to clip to event to shift to day, weighted by salience, so eight hours collapses to roughly twelve thousand queryable tokens." },
  { i: Radar, t: "A normalcy baseline that abstains", href: "/evals",
    d: "Counts per zone, hour and class, z-scored, and it declines to judge anything until it has at least three days of history." },
  { i: Plane, t: "Alerts you can fly to", href: "/console",
    d: "Geo-projected coordinates, accuracy radius, bearing, ETA and a geofence check, not just a description of what happened." },
  { i: Compass, t: "Findings only a fleet reveals", href: "/command",
    d: "A subject seen at three sites in five days is a reconnaissance pattern that no single site could possibly detect." },
  { i: Gauge, t: "Search that understands appearance", href: "/investigate",
    d: "Structured SQL, caption vectors and image vectors fused by reciprocal rank, so “a white pickup” finds frames whose captions never said it." },
  { i: ShieldCheck, t: "Decisions you can audit", href: "/evals",
    d: "Every consequential action is hash-chained. Altering history invalidates every hash after it, and verification is one click." },
];

const DISPATCH: [string, string][] = [
  ["coordinates", "18.760018, 73.862886"],
  ["accuracy", "± 4.9 m"],
  ["bearing from dock", "041° · 212 m"],
  ["eta", "1 min 04 s"],
  ["recommended alt", "28 m AGL"],
  ["geofence", "inside · cleared"],
];

const LOOP = [
  "Alert raised with dispatch coordinates",
  "Mission planned and feasibility checked",
  "Human approves; the agent cannot",
  "Drone flies; the closer vantage improves the view",
  "Confidence revised on new evidence",
];

const RESULTS = [
  { value: "8 / 8", label: "scenarios pass", sub: "9 true positives, 0 false positives, 0 false negatives" },
  { value: "1.00", label: "precision and recall", sub: "on the labelled scenario suite, F1 = 1.00" },
  { value: "0.975", label: "mean P@k", sub: "hybrid retrieval over one ingested session" },
  { value: "6 / 6", label: "chaos faults survived", sub: "no key, cassette miss, corrupt frame, timeout and more" },
];

/* ── helpers ────────────────────────────────────────────────────────────── */

function Section({ children, tint }: { children: React.ReactNode; tint?: boolean }) {
  return (
    <section
      className="border-t py-16 sm:py-20"
      style={{ background: tint ? "var(--surface-2)" : undefined }}
    >
      <div className="mx-auto max-w-6xl px-5">{children}</div>
    </section>
  );
}

function Reveal({ children, delay = 0 }: { children: React.ReactNode; delay?: number }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 18 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: "-80px" }}
      transition={{ duration: 0.55, delay, ease: [0.22, 1, 0.36, 1] }}
    >
      {children}
    </motion.div>
  );
}

function Mark() {
  return (
    <svg width="24" height="24" viewBox="0 0 32 32" fill="none">
      <circle cx="16" cy="16" r="14.5" stroke="var(--line-2)" strokeWidth="1" />
      <path
        d="M6.5 18.5c3.6-.4 6.2-2.1 8-5 .5-.8 1.5-.8 2 0 1.8 2.9 4.4 4.6 8 5"
        stroke="var(--accent)"
        strokeWidth="2"
        strokeLinecap="round"
        fill="none"
      />
      <circle cx="16" cy="15" r="2.1" fill="var(--accent)" />
    </svg>
  );
}
