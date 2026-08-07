"use client";

import { useEffect, useRef } from "react";

import type { PlaybackDet, PlaybackFrame, PlaybackIndex } from "@/lib/api";

/**
 * Detection boxes drawn over a playing `<video>`.
 *
 * A canvas rather than SVG: at 15 sampled detections a second over a 1080p
 * element, React reconciliation of a few hundred `<rect>` nodes per frame is the
 * expensive part, and none of it is needed. One `requestAnimationFrame` loop
 * reads `video.currentTime`, finds the bracketing samples and paints.
 *
 * **Interpolation.** Four of the six clips are indexed at their native rate, so
 * every video frame has a real detection and nothing is invented. The two 59.94
 * fps clips are sampled at 15, so boxes are interpolated between consecutive
 * samples *of the same track id*, which is exactly what a track id is for. The
 * `interpolated` flag is surfaced so the UI can say so rather than implying a
 * detection ran on every frame.
 */

export interface OverlayStats {
  visible: number;
  tracks: number;
  interpolated: boolean;
  frameIndex: number;
  gateReason: string;
  analysed: boolean;
}

interface Props {
  video: HTMLVideoElement | null;
  index: PlaybackIndex | null;
  palette: Record<string, string>;
  labelColour: string;
  showBoxes?: boolean;
  showLabels?: boolean;
  showTracks?: boolean;
  onStats?: (s: OverlayStats) => void;
}

/** Last sample at or before `t`. The index is sorted, so this is a binary search. */
function sampleAt(frames: PlaybackFrame[], t: number): number {
  let lo = 0;
  let hi = frames.length - 1;
  let best = 0;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (frames[mid].t <= t) {
      best = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return best;
}

function lerp(a: number, b: number, k: number) {
  return a + (b - a) * k;
}

export function DetectionOverlay({
  video, index, palette, labelColour,
  showBoxes = true, showLabels = true, showTracks = true, onStats,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const rafRef = useRef<number>(0);
  const statsRef = useRef<OverlayStats | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !video || !index) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let stopped = false;

    const draw = () => {
      if (stopped) return;
      rafRef.current = requestAnimationFrame(draw);

      const rect = video.getBoundingClientRect();
      if (!rect.width || !rect.height) return;

      /* Size the canvas in CSS pixels scaled by DPR, not by the video's native
       * resolution. A 1080p backing store costs four times the fill rate for a
       * box outline nobody can see at that density. */
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const w = Math.round(rect.width);
      const h = Math.round(rect.height);
      if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
        canvas.width = w * dpr;
        canvas.height = h * dpr;
        canvas.style.width = `${w}px`;
        canvas.style.height = `${h}px`;
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      const t = video.currentTime;
      const frames = index.frames;
      if (!frames.length) return;

      const i = sampleAt(frames, t);
      const cur = frames[i];
      const next = frames[i + 1];

      /* The video is letterboxed inside the element by `object-contain`, so the
       * painted area is not the element's box. Recover it from the aspect ratio,
       * or every box sits a few percent off. */
      const vw = video.videoWidth || index.width;
      const vh = video.videoHeight || index.height;
      const scale = Math.min(w / vw, h / vh);
      const dw = vw * scale;
      const dh = vh * scale;
      const ox = (w - dw) / 2;
      const oy = (h - dh) / 2;

      let dets: (PlaybackDet & { interp?: boolean })[] = cur.dets;
      let interpolated = false;

      // Between samples, ease each track toward where it is next seen.
      if (next && next.t > cur.t) {
        const k = Math.max(0, Math.min(1, (t - cur.t) / (next.t - cur.t)));
        if (k > 0.02) {
          const byTrack = new Map<number, PlaybackDet>();
          for (const d of next.dets) if (d.track != null) byTrack.set(d.track, d);
          dets = cur.dets.map((d) => {
            const n = d.track != null ? byTrack.get(d.track) : undefined;
            if (!n) return d;
            interpolated = true;
            return {
              ...d,
              x1: lerp(d.x1, n.x1, k), y1: lerp(d.y1, n.y1, k),
              x2: lerp(d.x2, n.x2, k), y2: lerp(d.y2, n.y2, k),
              interp: true,
            };
          });
        }
      }

      ctx.lineJoin = "round";
      ctx.font = "600 11px ui-sans-serif, system-ui, sans-serif";
      ctx.textBaseline = "middle";

      const seen = new Set<number>();
      for (const d of dets) {
        if (d.track != null) seen.add(d.track);
        if (!showBoxes) continue;

        const x = ox + d.x1 * dw;
        const y = oy + d.y1 * dh;
        const bw = (d.x2 - d.x1) * dw;
        const bh = (d.y2 - d.y1) * dh;
        const colour = palette[d.label] ?? labelColour;

        // A soft halo first, so the outline survives a light or busy background.
        ctx.lineWidth = 3.5;
        ctx.strokeStyle = "rgba(0,0,0,0.32)";
        ctx.strokeRect(x, y, bw, bh);

        ctx.lineWidth = 1.75;
        ctx.strokeStyle = colour;
        ctx.strokeRect(x, y, bw, bh);

        // Corner ticks: reads as an instrument rather than a plain rectangle.
        const c = Math.min(12, bw * 0.28, bh * 0.28);
        ctx.lineWidth = 2.75;
        ctx.beginPath();
        ctx.moveTo(x, y + c); ctx.lineTo(x, y); ctx.lineTo(x + c, y);
        ctx.moveTo(x + bw - c, y); ctx.lineTo(x + bw, y); ctx.lineTo(x + bw, y + c);
        ctx.moveTo(x + bw, y + bh - c); ctx.lineTo(x + bw, y + bh); ctx.lineTo(x + bw - c, y + bh);
        ctx.moveTo(x + c, y + bh); ctx.lineTo(x, y + bh); ctx.lineTo(x, y + bh - c);
        ctx.stroke();

        if (!showLabels) continue;
        const trackTag = showTracks && d.track != null ? ` #${d.track}` : "";
        const text = `${d.label} ${Math.round(d.conf * 100)}%${trackTag}`;
        const tw = ctx.measureText(text).width + 12;
        const th = 17;
        const ty = y - th - 3 < 0 ? y + 3 : y - th - 3;

        ctx.fillStyle = colour;
        ctx.beginPath();
        ctx.roundRect(x - 0.5, ty, tw, th, 4);
        ctx.fill();

        ctx.fillStyle = "#fff";
        ctx.fillText(text, x + 5.5, ty + th / 2 + 0.5);
      }

      const stats: OverlayStats = {
        visible: dets.length,
        tracks: seen.size,
        interpolated,
        frameIndex: i,
        gateReason: cur.gate_reason,
        analysed: cur.analysed,
      };
      const prev = statsRef.current;
      if (
        !prev || prev.visible !== stats.visible || prev.tracks !== stats.tracks ||
        prev.frameIndex !== stats.frameIndex || prev.interpolated !== stats.interpolated
      ) {
        statsRef.current = stats;
        onStats?.(stats);
      }
    };

    rafRef.current = requestAnimationFrame(draw);
    return () => {
      stopped = true;
      cancelAnimationFrame(rafRef.current);
    };
  }, [video, index, palette, labelColour, showBoxes, showLabels, showTracks, onStats]);

  return (
    <canvas
      ref={canvasRef}
      className="pointer-events-none absolute inset-0 h-full w-full"
      aria-hidden
    />
  );
}
