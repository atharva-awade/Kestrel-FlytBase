"use client";

import { AnimatePresence, motion } from "framer-motion";
import { MapPin, Upload, X } from "lucide-react";
import { useRef, useState } from "react";

import { apiOrigin } from "@/lib/api";
import { cn } from "@/lib/format";

/**
 * Analyse footage the system has never seen, anchored to a real place.
 *
 * The location box is the interesting half. An uploaded clip has no site behind
 * it, and every downstream stage (zone membership, geo-projection, rule
 * severity, dispatch coordinates) is written against one. Asking where the
 * video was filmed lets the server synthesise an ad-hoc site there, so the
 * alerts come back with coordinates a drone could actually be sent to rather
 * than coordinates borrowed from the demo site.
 *
 * Geocoding happens here rather than on the server because the browser already
 * holds the MapTiler key (it must, to fetch tiles). The server only ever
 * receives a resolved latitude and longitude.
 */

const KEY = process.env.NEXT_PUBLIC_MAPTILER_KEY ?? "";
const COORD = /^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$/;

type Resolved = { lat: number; lon: number; label: string };

async function resolveLocation(input: string): Promise<Resolved> {
  const direct = COORD.exec(input);
  if (direct) {
    const lat = Number(direct[1]);
    const lon = Number(direct[2]);
    if (Math.abs(lat) > 90 || Math.abs(lon) > 180) {
      throw new Error("those coordinates are out of range");
    }
    return { lat, lon, label: `${lat.toFixed(5)}, ${lon.toFixed(5)}` };
  }
  if (!KEY) {
    throw new Error("no map key configured, so enter coordinates as `lat, lon`");
  }
  const url =
    `https://api.maptiler.com/geocoding/${encodeURIComponent(input)}.json` +
    `?key=${KEY}&limit=1`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`geocoding failed (${res.status})`);
  const data = await res.json();
  const hit = data?.features?.[0];
  if (!hit?.center) throw new Error(`could not find "${input}"`);
  return { lat: hit.center[1], lon: hit.center[0], label: hit.place_name ?? input };
}

export function UploadFootage({ onIndexed }: { onIndexed: (slug: string) => void }) {
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [where, setWhere] = useState("");
  const [resolved, setResolved] = useState<Resolved | null>(null);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(0);
  const [status, setStatus] = useState("");
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const reset = () => {
    setFile(null); setWhere(""); setResolved(null);
    setBusy(false); setProgress(0); setStatus(""); setError(null);
  };

  const submit = async () => {
    if (!file || !where.trim()) return;
    setBusy(true);
    setError(null);
    setStatus("resolving location");

    let point: Resolved;
    try {
      point = await resolveLocation(where.trim());
      setResolved(point);
    } catch (e) {
      setError(e instanceof Error ? e.message : "could not resolve that location");
      setBusy(false);
      return;
    }

    setStatus("uploading");
    const body = new FormData();
    body.append("file", file);
    body.append("lat", String(point.lat));
    body.append("lon", String(point.lon));
    body.append("label", point.label);

    let job: any;
    try {
      // Direct to the API: the dev proxy truncates bodies over 10 MB.
      const res = await fetch(`${apiOrigin}/api/upload/video`, {
        method: "POST",
        body,
      });
      job = await res.json().catch(() => null);
      if (!res.ok) throw new Error(job?.detail ?? `upload failed (${res.status})`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "upload failed");
      setBusy(false);
      return;
    }

    // Indexing runs server-side; poll rather than hold a socket open for it.
    setStatus("analysing");
    const started = Date.now();
    let misses = 0;
    while (Date.now() - started < 15 * 60 * 1000) {
      await new Promise((r) => setTimeout(r, 900));
      const p = await fetch(`${apiOrigin}/api/upload/${job.job_id}/progress`)
        .then((r) => (r.ok ? r.json() : null))
        .catch(() => null);
      if (!p) {
        misses++;
        if (misses >= 5) {
          setError("Upload job was interrupted (server restarted or connection lost). Please try again.");
          setBusy(false);
          return;
        }
        continue;
      }
      misses = 0;
      setProgress(p.progress ?? 0);
      setStatus(p.message ?? p.state);
      if (p.state === "ready") {
        onIndexed(p.slug);
        setOpen(false);
        reset();
        return;
      }
      if (p.state === "failed") {
        setError(p.message ?? "indexing failed");
        setBusy(false);
        return;
      }
    }
    setError("indexing timed out");
    setBusy(false);
  };

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="card card-lift flex min-w-[186px] flex-1 flex-col justify-center px-3.5 py-2.5 text-left"
        style={{ borderStyle: "dashed" }}
      >
        <div className="flex items-center gap-1.5">
          <Upload size={13} style={{ color: "var(--accent)" }} />
          <span className="text-[13px] font-semibold">Your own footage</span>
        </div>
        <div className="mono mt-1 text-[10px] text-[var(--ink-4)]">
          upload and locate a clip
        </div>
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            className="fixed inset-0 z-50 grid place-items-center p-5"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            style={{ background: "color-mix(in oklab, var(--bg) 72%, transparent)" }}
            onClick={() => !busy && (setOpen(false), reset())}
          >
            <motion.div
              className="card w-full max-w-lg p-5"
              initial={{ y: 14, scale: 0.98 }}
              animate={{ y: 0, scale: 1 }}
              exit={{ y: 8, opacity: 0 }}
              onClick={(e) => e.stopPropagation()}
            >
              <div className="mb-3 flex items-center gap-2">
                <Upload size={15} style={{ color: "var(--accent)" }} />
                <h2 className="text-[15px] font-semibold">Analyse your own footage</h2>
                {!busy && (
                  <button onClick={() => { setOpen(false); reset(); }} className="ml-auto">
                    <X size={15} className="text-[var(--ink-4)]" />
                  </button>
                )}
              </div>

              <p className="mb-4 text-[12.5px] leading-relaxed text-[var(--ink-3)]">
                The clip runs the same detector, tracker, geo-projection and rule engine
                as the bundled footage. Telling KESTREL where it was filmed is what lets
                its alerts carry coordinates a drone could be dispatched to.
              </p>

              {/* file */}
              <button
                onClick={() => inputRef.current?.click()}
                disabled={busy}
                className="mb-3 w-full rounded-xl border px-3 py-4 text-center transition"
                style={{ borderColor: "var(--line-2)", borderStyle: "dashed" }}
              >
                <div className="text-[13px] font-medium">
                  {file ? file.name : "Choose a video file"}
                </div>
                <div className="mono mt-1 text-[10.5px] text-[var(--ink-4)]">
                  {file
                    ? `${(file.size / 1e6).toFixed(1)} MB`
                    : "mp4 or mov, up to 200 MB and 10 minutes"}
                </div>
              </button>
              <input
                ref={inputRef}
                type="file"
                accept="video/*"
                className="hidden"
                onChange={(e) => { setFile(e.target.files?.[0] ?? null); setError(null); }}
              />

              {/* location */}
              <label className="eyebrow mb-1.5 block">Where was this filmed?</label>
              <div
                className="mb-1 flex items-center gap-2 rounded-xl border px-3 py-2.5"
                style={{ borderColor: "var(--line-2)" }}
              >
                <MapPin size={13} className="shrink-0 text-[var(--ink-4)]" />
                <input
                  value={where}
                  onChange={(e) => { setWhere(e.target.value); setError(null); }}
                  disabled={busy}
                  placeholder="Chakan, Pune   or   18.7582, 73.8594"
                  className="w-full bg-transparent text-[13px] outline-none placeholder:text-[var(--ink-4)]"
                  onKeyDown={(e) => e.key === "Enter" && void submit()}
                />
              </div>
              <p className="mb-4 text-[11px] text-[var(--ink-4)]">
                A place name or a raw <span className="mono">lat, lon</span>. This becomes
                the origin of an ad-hoc site, so every alert is projected against it.
              </p>

              {resolved && (
                <div className="mono mb-3 rounded-lg px-2.5 py-2 text-[11px] text-[var(--ink-3)]"
                     style={{ background: "var(--surface-2)" }}>
                  {resolved.label} · {resolved.lat.toFixed(5)}, {resolved.lon.toFixed(5)}
                </div>
              )}

              {busy && (
                <div className="mb-3">
                  <div className="h-1.5 overflow-hidden rounded-full"
                       style={{ background: "var(--surface-3)" }}>
                    <motion.div
                      className="h-full rounded-full"
                      style={{ background: "var(--accent)" }}
                      animate={{ width: `${Math.max(4, progress * 100)}%` }}
                    />
                  </div>
                  <p className="mono mt-1.5 text-[10.5px] text-[var(--ink-4)]">{status}</p>
                </div>
              )}

              {error && (
                <p className="mb-3 text-[12px] leading-relaxed" style={{ color: "var(--sev-high)" }}>
                  {error}
                </p>
              )}

              <div className="flex items-center gap-2">
                <button
                  onClick={() => void submit()}
                  disabled={busy || !file || !where.trim()}
                  className={cn(
                    "flex-1 rounded-xl px-4 py-2.5 text-[13px] font-semibold text-white transition",
                    (busy || !file || !where.trim()) && "opacity-45",
                  )}
                  style={{ background: "var(--accent)" }}
                >
                  {busy ? "Analysing…" : "Analyse footage"}
                </button>
              </div>

              <p className="mt-3 text-[11px] leading-relaxed text-[var(--ink-4)]">
                Telemetry for an uploaded clip is simulated: there is no aircraft, the
                projection assumes flat ground and a nadir camera, and the accuracy radius
                widens accordingly. The result is labelled so it cannot be mistaken for
                the live site.
              </p>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}
