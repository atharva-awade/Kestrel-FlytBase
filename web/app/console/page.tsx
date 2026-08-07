"use client";

import { AnimatePresence, motion } from "framer-motion";
import {
  AlertTriangle, Maximize2, Minimize2, Pause, Play, Radar, RotateCcw, Rocket, Send,
} from "lucide-react";
import { useTheme } from "next-themes";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { DetectionOverlay, type OverlayStats } from "@/components/console/DetectionOverlay";
import { UploadFootage } from "@/components/console/UploadFootage";
import {
  Card, Empty, Pill, SectionTitle, SeverityChip, Skeleton, Stat, fadeUp, stagger,
} from "@/components/ui/primitives";
import { api, type Clip, type PlaybackIndex } from "@/lib/api";
import { useDeploy } from "@/lib/deploy";
import { cn, sevClass } from "@/lib/format";
import { useSeverityPalette } from "@/lib/useSeverityPalette";

/**
 * Live operations: real footage played at its own frame rate, with every
 * detection drawn as it happens and alerts firing on the timeline they belong to.
 *
 * The detections are not computed in the browser and not drawn for effect. They
 * come from a dense index built by `scripts/build_playback_index.py`, which runs
 * the same detector, tracker, geo-projection and rule engine as the live
 * pipeline. This is a replay of a real analysis, frame-accurate to the video.
 *
 * The gate ribbon under the video is the part worth saying out loud: it shows
 * which frames the tier-0 gate judged worth a model call. Local detection is
 * free, so it runs on everything; the gate governs the hosted tiers. You can
 * watch it skip frames while the boxes keep tracking.
 */

const LABEL_TONE: Record<string, "accent" | "medium" | "low" | "info"> = {
  person: "accent",
  car: "medium", truck: "medium", bus: "medium", train: "medium",
  bicycle: "low", motorcycle: "low",
  dog: "info", cat: "info", bird: "info", boat: "info",
  backpack: "info", handbag: "info", suitcase: "info",
};

export default function ConsolePage() {
  const { resolvedTheme } = useTheme();
  const palette = useSeverityPalette(resolvedTheme);
  const launchDrone = useDeploy((s) => s.launch);

  const [clips, setClips] = useState<Clip[] | null>(null);
  const [slug, setSlug] = useState<string | null>(null);
  const [index, setIndex] = useState<PlaybackIndex | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [videoEl, setVideoEl] = useState<HTMLVideoElement | null>(null);

  const [playing, setPlaying] = useState(false);
  const [t, setT] = useState(0);
  const [rate, setRate] = useState(1);
  const [fullscreen, setFullscreen] = useState(false);
  const [stats, setStats] = useState<OverlayStats | null>(null);
  const [showBoxes, setShowBoxes] = useState(true);
  const [showLabels, setShowLabels] = useState(true);

  const reloadClips = useCallback(async () => {
    const res = await api.clips();
    if (res) setClips(res.clips);
    return res?.clips ?? [];
  }, []);

  // ── data ────────────────────────────────────────────────────────────────
  useEffect(() => {
    (async () => {
      const list = await reloadClips();
      if (!list.length) {
        setError("Could not reach the API. Start it with: uv run kestrel serve");
        setLoading(false);
        return;
      }
      const first =
        list.find((c) => c.indexed && c.primary) ?? list.find((c) => c.indexed) ?? list[0];
      setSlug(first?.slug ?? null);
      setLoading(false);
    })();
  }, [reloadClips]);

  useEffect(() => {
    if (!slug) return;
    let cancelled = false;
    setIndex(null);
    setError(null);
    setStats(null);
    setT(0);
    (async () => {
      const res = await api.playback(slug);
      if (cancelled) return;
      if (!res) {
        setError(
          `No playback index for "${slug}". Build one with: ` +
            `uv run python scripts/build_playback_index.py --clip ${slug}`,
        );
        return;
      }
      setIndex(res);
    })();
    return () => {
      cancelled = true;
    };
  }, [slug]);

  // ── transport ───────────────────────────────────────────────────────────
  const seek = useCallback((to: number) => {
    const v = videoRef.current;
    if (!v) return;
    v.currentTime = Math.max(0, Math.min(v.duration || 0, to));
    setT(v.currentTime);
  }, []);

  const toggle = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) void v.play();
    else v.pause();
  }, []);

  useEffect(() => {
    if (videoRef.current) videoRef.current.playbackRate = rate;
  }, [rate, index]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      if (e.code === "Space") {
        e.preventDefault();
        toggle();
      }
      if (e.code === "ArrowRight") seek((videoRef.current?.currentTime ?? 0) + 1);
      if (e.code === "ArrowLeft") seek((videoRef.current?.currentTime ?? 0) - 1);
      if (e.code === "Escape") setFullscreen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggle, seek]);

  useEffect(() => {
    if (!fullscreen) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [fullscreen]);

  // ── derived ─────────────────────────────────────────────────────────────
  const duration = index?.duration_s ?? 0;

  /** Alerts whose moment has passed, newest first: the operator's live feed. */
  const fired = useMemo(
    () => (index?.alerts ?? []).filter((a) => a.t <= t + 0.15).reverse(),
    [index, t],
  );

  const labelColours = useMemo(() => {
    const tone = {
      accent: palette.accent,
      medium: palette.medium,
      low: palette.low,
      info: palette.info,
    };
    const out: Record<string, string> = {};
    for (const [label, k] of Object.entries(LABEL_TONE)) out[label] = tone[k];
    return out;
  }, [palette]);

  const current = clips?.find((c) => c.slug === slug) ?? null;
  const totalDets = useMemo(
    () => (index ? index.frames.reduce((n, f) => n + f.dets.length, 0) : 0),
    [index],
  );

  // ── the video stage ─────────────────────────────────────────────────────
  const stage = (
    <div
      className={cn(
        "relative overflow-hidden bg-black",
        fullscreen ? "h-full w-full" : "aspect-video w-full",
      )}
    >
      {index && (
        <video
          key={index.clip}
          ref={(el) => {
            videoRef.current = el;
            setVideoEl(el);
          }}
          src={api.footageUrl(index.clip)}
          className="h-full w-full object-contain"
          playsInline
          muted
          loop
          onTimeUpdate={(e) => setT(e.currentTarget.currentTime)}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onClick={toggle}
        />
      )}

      <DetectionOverlay
        video={videoEl}
        index={index}
        palette={labelColours}
        labelColour={palette.accent}
        showBoxes={showBoxes}
        showLabels={showLabels}
        onStats={setStats}
      />

      {stats && (
        <div className="glass pointer-events-none absolute top-3 left-3 rounded-lg px-2.5 py-1.5">
          <div className="mono flex items-center gap-2 text-[10px] tracking-[0.1em] uppercase">
            <span
              className="h-1.5 w-1.5 rounded-full"
              style={{ background: stats.analysed ? "var(--ok)" : "var(--ink-4)" }}
            />
            <span className="text-[var(--ink-2)]">{stats.analysed ? "analysed" : "gated"}</span>
            <span className="text-[var(--ink-4)]">{stats.gateReason}</span>
          </div>
        </div>
      )}

      {stats && (
        <div className="glass pointer-events-none absolute top-3 right-3 rounded-lg px-2.5 py-1.5">
          <div className="mono flex items-center gap-3 text-[10px] text-[var(--ink-2)]">
            <span>{stats.visible} objects</span>
            <span>{stats.tracks} tracks</span>
            {stats.interpolated && (
              <span
                className="text-[var(--ink-4)]"
                title="Between sampled frames, boxes follow their track id"
              >
                interp
              </span>
            )}
          </div>
        </div>
      )}

      <button
        onClick={() => setFullscreen((f) => !f)}
        className="glass absolute right-3 bottom-3 flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[11px] font-medium"
      >
        {fullscreen ? <Minimize2 size={12} /> : <Maximize2 size={12} />}
        {fullscreen ? "Exit" : "Fullscreen"}
      </button>

      {!index && !error && (
        <div className="absolute inset-0 grid place-items-center">
          <div className="text-center">
            <div className="pulse-ring relative mx-auto mb-3 h-8 w-8 rounded-full border border-[var(--accent)]/50" />
            <p className="mono text-[10.5px] tracking-[0.2em] text-[var(--ink-4)] uppercase">
              Loading index
            </p>
          </div>
        </div>
      )}
    </div>
  );

  const transport = (
    <Transport
      t={t}
      duration={duration}
      playing={playing}
      rate={rate}
      index={index}
      onToggle={toggle}
      onSeek={seek}
      onRate={setRate}
    />
  );

  return (
    <div className="mx-auto max-w-[1500px] px-5 py-6">
      {fullscreen && (
        <div className="fixed inset-0 z-50 flex flex-col" style={{ background: "var(--bg)" }}>
          <div className="min-h-0 flex-1">{stage}</div>
          {transport}
        </div>
      )}

      <motion.div {...fadeUp}>
        <SectionTitle
          eyebrow="Live operations"
          title="Console"
          subtitle="Real footage through the full cascade, played at its own frame rate. Every box, track and alert below was produced by the pipeline."
          right={
            index ? (
              <div className="flex items-center gap-2">
                <Pill tone="accent">
                  {index.detector} · {index.detector_device}
                </Pill>
                <Pill tone="muted">{index.mean_detect_ms} ms/frame</Pill>
              </div>
            ) : undefined
          }
        />
      </motion.div>

      {/* ── scenario picker ──────────────────────────────────────────────── */}
      <motion.div {...stagger(1)} className="mb-4">
        {loading ? (
          <div className="flex flex-wrap gap-2">
            {[0, 1, 2, 3, 4].map((i) => (
              <Skeleton key={i} className="h-[58px] w-[190px] flex-1" />
            ))}
          </div>
        ) : (
          <div className="flex flex-wrap gap-2">
            {(clips ?? []).map((c) => (
              <button
                key={c.slug}
                onClick={() => setSlug(c.slug)}
                disabled={!c.indexed}
                className={cn(
                  "card card-lift min-w-[186px] flex-1 px-3.5 py-2.5 text-left",
                  !c.indexed && "cursor-not-allowed opacity-45",
                )}
                style={
                  c.slug === slug
                    ? { borderColor: "var(--accent)", boxShadow: "var(--shadow-accent)" }
                    : undefined
                }
              >
                <div className="flex items-center gap-1.5">
                  <span className="truncate text-[13px] font-semibold">{titleFor(c.slug, c)}</span>
                  {c.primary && <Pill tone="accent">primary</Pill>}
                  {c.uploaded && <Pill tone="warn">uploaded</Pill>}
                </div>
                <div className="mono mt-1 truncate text-[10px] text-[var(--ink-4)]">
                  {c.width}×{c.height} · {c.fps?.toFixed(0)} fps · {c.duration_s?.toFixed(0)}s
                  {!c.indexed && " · not indexed"}
                </div>
              </button>
            ))}
            <UploadFootage
              onIndexed={async (s) => {
                await reloadClips();
                setSlug(s);
              }}
            />
          </div>
        )}
        {current?.title && (
          <p className="mt-2 text-[12.5px] leading-relaxed text-[var(--ink-3)]">{current.title}</p>
        )}
      </motion.div>

      {error && (
        <motion.div {...stagger(2)} className="mb-4">
          <Empty title="Nothing to play" hint={error} />
        </motion.div>
      )}

      <div className="grid gap-4 xl:grid-cols-[1fr_390px]">
        {/* ── player ─────────────────────────────────────────────────────── */}
        <motion.div {...stagger(2)} className="space-y-3">
          <Card className="overflow-hidden p-0">
            {!fullscreen && stage}
            {!fullscreen && transport}
          </Card>

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Objects in frame" value={stats?.visible ?? 0} tone="accent" />
            <Stat label="Tracks alive" value={stats?.tracks ?? 0} />
            <Stat
              label="Gate skipped"
              value={index ? Math.round(index.gate.efficiency * 100) : 0}
              unit="%"
              hint={index ? `${index.gate.skipped} of ${index.sampled_frames}` : undefined}
            />
            <Stat
              label="Detections indexed"
              value={totalDets}
              hint={index ? `at ${index.sampled_fps} fps` : undefined}
            />
          </div>

          <Card className="p-3.5">
            <div className="flex flex-wrap items-center gap-3">
              <Toggle on={showBoxes} onClick={() => setShowBoxes((v) => !v)} label="Boxes" />
              <Toggle on={showLabels} onClick={() => setShowLabels((v) => !v)} label="Labels" />
              <div className="ml-auto flex flex-wrap items-center gap-2.5">
                {["person", "car", "bicycle", "dog"].map((label) => (
                  <span
                    key={label}
                    className="mono flex items-center gap-1 text-[10px] text-[var(--ink-4)]"
                  >
                    <span
                      className="h-2 w-2 rounded-sm"
                      style={{ background: labelColours[label] }}
                    />
                    {label}
                  </span>
                ))}
              </div>
            </div>
            {index && (
              <p className="mt-2.5 text-[11.5px] leading-relaxed text-[var(--ink-4)]">
                Indexed at {index.sampled_fps} fps with {index.detector} on{" "}
                {index.detector_device}. Between sampled frames boxes follow their track id
                rather than a detection running on every video frame. Telemetry is{" "}
                {index.telemetry}.
              </p>
            )}
          </Card>
        </motion.div>

        {/* ── live feed ──────────────────────────────────────────────────── */}
        <motion.div {...stagger(3)} className="space-y-3">
          <Card className="p-3.5">
            <div className="mb-2.5 flex items-center gap-1.5">
              <AlertTriangle size={13} style={{ color: "var(--accent)" }} />
              <span className="text-[13px] font-semibold">Alerts</span>
              <span className="mono ml-auto text-[10.5px] text-[var(--ink-4)]">
                {fired.length} of {index?.alerts.length ?? 0}
              </span>
            </div>

            {!index ? (
              <Skeleton className="h-40" />
            ) : !index.alerts.length ? (
              <p className="py-6 text-center text-[12px] leading-relaxed text-[var(--ink-4)]">
                No rule fired on this clip. That is a result, not a gap: the suite weights
                true negatives as heavily as true positives.
              </p>
            ) : fired.length === 0 ? (
              <p className="py-6 text-center text-[12px] text-[var(--ink-4)]">
                Nothing yet. Alerts appear as the playhead reaches them.
              </p>
            ) : (
              <div className="max-h-[26rem] space-y-1.5 overflow-y-auto pr-1">
                <AnimatePresence initial={false}>
                  {fired.map((a) => (
                    <motion.button
                      key={a.id}
                      layout
                      initial={{ opacity: 0, x: 16 }}
                      animate={{ opacity: 1, x: 0 }}
                      onClick={() => seek(a.t)}
                      className={cn(
                        "sev-bar w-full rounded-r-lg px-2.5 py-2 text-left transition hover:bg-[var(--surface-2)]",
                        sevClass(a.severity),
                      )}
                    >
                      <div className="flex items-center gap-2">
                        <SeverityChip severity={a.severity} />
                        <span className="mono text-[10px] text-[var(--ink-4)]">{fmt(a.t)}</span>
                        <span className="mono ml-auto text-[10px] text-[var(--ink-4)]">
                          {Math.round(a.confidence * 100)}%
                        </span>
                      </div>
                      <div className="mt-1 text-[12.5px] leading-snug text-[var(--ink)]">
                        {a.title}
                      </div>
                      {a.location?.lat != null && (
                        <div className="mono mt-1 flex items-center gap-1.5 text-[10px] text-[var(--ink-4)]">
                          <Send size={9} />
                          {a.location.lat.toFixed(6)}, {a.location.lon.toFixed(6)}
                          {a.location.accuracy_m != null && ` ±${a.location.accuracy_m}m`}
                        </div>
                      )}
                      {["high", "critical"].includes(a.severity?.toLowerCase()) &&
                        a.location?.lat != null && (
                          <span
                            role="button"
                            tabIndex={0}
                            onClick={(e) => {
                              e.stopPropagation();
                              launchDrone(toAlert(a, index?.site_id));
                            }}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") {
                                e.stopPropagation();
                                launchDrone(toAlert(a, index?.site_id));
                              }
                            }}
                            className="mt-2 inline-flex cursor-pointer items-center gap-1.5 rounded-lg px-2.5 py-1 text-[11px] font-semibold text-white"
                            style={{ background: "var(--accent)" }}
                          >
                            <Rocket size={10} />
                            Deploy drone
                          </span>
                        )}
                    </motion.button>
                  ))}
                </AnimatePresence>
              </div>
            )}
          </Card>

          {index && Object.keys(index.tracks).length > 0 && (
            <Card className="p-3.5">
              <div className="mb-2 flex items-center gap-1.5">
                <Radar size={13} style={{ color: "var(--accent)" }} />
                <span className="text-[13px] font-semibold">Tracked subjects</span>
                <span className="mono ml-auto text-[10.5px] text-[var(--ink-4)]">
                  {Object.keys(index.tracks).length}
                </span>
              </div>
              <div className="max-h-52 space-y-1 overflow-y-auto pr-1">
                {Object.entries(index.tracks)
                  .sort((a, b) => b[1].frames - a[1].frames)
                  .slice(0, 30)
                  .map(([id, tr]) => {
                    const live = t >= tr.first_t && t <= tr.last_t;
                    return (
                      <button
                        key={id}
                        onClick={() => seek(tr.first_t)}
                        className="mono flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-[10.5px] transition hover:bg-[var(--surface-2)]"
                      >
                        <span
                          className="h-1.5 w-1.5 rounded-full"
                          style={{ background: live ? "var(--ok)" : "var(--line-2)" }}
                        />
                        <span className="text-[var(--ink-2)]">#{id}</span>
                        <span className="text-[var(--ink-3)]">{tr.label}</span>
                        <span className="ml-auto text-[var(--ink-4)]">
                          {fmt(tr.first_t)}–{fmt(tr.last_t)}
                        </span>
                      </button>
                    );
                  })}
              </div>
            </Card>
          )}
        </motion.div>
      </div>
    </div>
  );
}

/* ── transport + timeline ──────────────────────────────────────────────── */
function Transport({
  t, duration, playing, rate, index, onToggle, onSeek, onRate,
}: {
  t: number;
  duration: number;
  playing: boolean;
  rate: number;
  index: PlaybackIndex | null;
  onToggle: () => void;
  onSeek: (t: number) => void;
  onRate: (r: number) => void;
}) {
  const barRef = useRef<HTMLDivElement | null>(null);
  const pct = duration ? (t / duration) * 100 : 0;
  const ribbon = useMemo(() => (index ? sampleRibbon(index) : []), [index]);

  const click = (e: React.MouseEvent) => {
    const el = barRef.current;
    if (!el || !duration) return;
    const r = el.getBoundingClientRect();
    onSeek(((e.clientX - r.left) / r.width) * duration);
  };

  return (
    <div className="border-t px-3.5 py-3" style={{ borderColor: "var(--line)" }}>
      {/* One tick per sampled frame: which frames the gate let through. */}
      {ribbon.length > 0 && (
        <div className="mb-1.5 flex h-1.5 gap-px overflow-hidden rounded-full" title="Tier-0 gate: analysed vs skipped">
          {ribbon.map((on, i) => (
            <span
              key={i}
              className="flex-1"
              style={{ background: on ? "var(--accent)" : "var(--line-2)" }}
            />
          ))}
        </div>
      )}

      <div
        ref={barRef}
        onClick={click}
        className="relative h-7 cursor-pointer rounded-md"
        style={{ background: "var(--surface-2)" }}
      >
        <div
          className="absolute inset-y-0 left-0 rounded-md"
          style={{ width: `${pct}%`, background: "var(--accent-soft)" }}
        />
        {(index?.alerts ?? []).map((a) => (
          <span
            key={a.id}
            title={`${a.title} @ ${fmt(a.t)}`}
            className={cn("sev-dot absolute top-1/2 -translate-y-1/2", sevClass(a.severity))}
            style={{ left: `calc(${duration ? (a.t / duration) * 100 : 0}% - 3px)` }}
          />
        ))}
        <div
          className="absolute inset-y-0 w-0.5"
          style={{ left: `${pct}%`, background: "var(--accent)" }}
        />
      </div>

      <div className="mt-2 flex items-center gap-2">
        <button
          onClick={onToggle}
          className="grid h-8 w-8 place-items-center rounded-lg text-white"
          style={{ background: "var(--accent)" }}
          aria-label={playing ? "Pause" : "Play"}
        >
          {playing ? <Pause size={14} /> : <Play size={14} />}
        </button>
        <button
          onClick={() => onSeek(0)}
          className="grid h-8 w-8 place-items-center rounded-lg border"
          style={{ borderColor: "var(--line-2)" }}
          aria-label="Restart"
        >
          <RotateCcw size={13} />
        </button>
        <span className="mono text-[11px] text-[var(--ink-3)]">
          {fmt(t)} / {fmt(duration)}
        </span>
        <div className="ml-auto flex items-center gap-1">
          {[0.5, 1, 2].map((r) => (
            <button
              key={r}
              onClick={() => onRate(r)}
              className={cn(
                "mono rounded-md px-2 py-1 text-[10.5px] transition",
                rate === r ? "text-white" : "text-[var(--ink-3)] hover:bg-[var(--surface-2)]",
              )}
              style={rate === r ? { background: "var(--accent)" } : undefined}
            >
              {r}×
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function Toggle({ on, onClick, label }: { on: boolean; onClick: () => void; label: string }) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[11.5px] font-medium transition",
        on ? "text-[var(--ink)]" : "text-[var(--ink-4)]",
      )}
      style={{ borderColor: on ? "var(--accent)" : "var(--line-2)" }}
    >
      <span
        className="h-1.5 w-1.5 rounded-full"
        style={{ background: on ? "var(--accent)" : "var(--line-2)" }}
      />
      {label}
    </button>
  );
}

/* ── helpers ───────────────────────────────────────────────────────────── */

/** Adapt a playback-index alert to the shape the dispatch overlay reads.
 *  The index stores exactly what the rule engine produced, so this is a rename
 *  rather than an invention. */
function toAlert(a: PlaybackIndex["alerts"][number], siteId?: string): any {
  return {
    id: a.id,
    site_id: siteId ?? "plant-01",
    rule_id: a.rule_id,
    severity: a.severity,
    title: a.title,
    zone_id: a.zone_id,
    confidence: a.confidence,
    location: a.location,
    status: "open",
  };
}
const TITLES: Record<string, string> = {
  "worker-zone": "Worker Zone",
  "person-bicycle-car": "Mixed Traffic",
  "car-detection": "Vehicle Gate",
  "people-detection": "Pedestrians",
  "one-by-one-person": "Sequential Entry",
  "store-aisle": "Overhead Aisle",
};

function titleFor(slug: string, clip?: Clip) {
  if (TITLES[slug]) return TITLES[slug];
  // An uploaded clip is named by where it was filmed, which is the only label
  // that means anything to the person who uploaded it. A slug of random hex is not.
  if (clip?.uploaded) return (clip.title || "Uploaded footage").split(",")[0];
  return slug.replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function fmt(s: number) {
  if (!Number.isFinite(s)) return "0:00";
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}

/** Downsample gate verdicts to ~140 ticks so the ribbon stays readable. */
function sampleRibbon(index: PlaybackIndex): boolean[] {
  const n = Math.min(140, index.frames.length);
  if (!n) return [];
  const step = index.frames.length / n;
  return Array.from(
    { length: n },
    (_, i) => index.frames[Math.floor(i * step)]?.analysed ?? false,
  );
}
