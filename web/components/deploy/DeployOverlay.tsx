"use client";

import { ContactShadows, Environment, Lightformer, useGLTF } from "@react-three/drei";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { AnimatePresence, motion } from "framer-motion";
import { Check, Navigation, ShieldAlert, X } from "lucide-react";
import { useTheme } from "next-themes";
import { Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";

import { WebGLBoundary } from "@/components/hero/WebGLBoundary";
import { SeverityChip } from "@/components/ui/primitives";
import { api } from "@/lib/api";
import { FLIGHT_SECONDS, flightAt, useDeploy, type Phase } from "@/lib/deploy";
import { cn } from "@/lib/format";
import { useSeverityPalette } from "@/lib/useSeverityPalette";

/**
 * Dispatch, shown rather than described.
 *
 * Pressing "Deploy drone" on a high or critical alert dims the console, spins the
 * M300 up off its dock, flies it to the alert's own coordinates and holds a
 * hover, with a HUD reading the real bearing, distance, ETA, altitude and
 * geofence verdict carried on that alert.
 *
 * It ends on an approval card, and that is the point rather than a flourish: the
 * agent plans the flight and cannot authorise it. Declining is recorded in the
 * hash-chained ledger exactly as approving is.
 */

const MODEL = "/models/kestrel-drone.glb";
const DRACO = "/draco/gltf/";
const LENS = /(glass[_ ]?(camera|sensor)|camera_\d|glass_\d)/i;
const LAMP = /lamp/i;
const FIT = 1.9;

/* ── the aircraft ───────────────────────────────────────────────────────── */
function Drone() {
  const { scene } = useGLTF(MODEL, DRACO);
  const group = useRef<THREE.Group>(null);

  /* `useGLTF` caches by URL, so the landing page's hero and this overlay would
   * otherwise share one object graph - and a `<primitive>` can only be parented
   * in one place. Cloning keeps the two independent while still reusing the
   * decoded geometry. */
  const model = useMemo(() => scene.clone(true), [scene]);
  const parts = useMemo(
    () => ({ lenses: [] as THREE.MeshStandardMaterial[], lamps: [] as THREE.MeshStandardMaterial[] }),
    [],
  );

  useLayoutEffect(() => {
    model.scale.setScalar(1);
    model.position.set(0, 0, 0);
    model.updateMatrixWorld(true);

    const box = new THREE.Box3().setFromObject(model);
    const size = box.getSize(new THREE.Vector3());
    const centre = box.getCenter(new THREE.Vector3());
    const scale = FIT / (Math.max(size.x, size.y, size.z) || 1);
    model.scale.setScalar(scale);
    model.position.set(-centre.x * scale, -box.min.y * scale, -centre.z * scale);

    parts.lenses.length = 0;
    parts.lamps.length = 0;
    model.traverse((o) => {
      const mesh = o as THREE.Mesh;
      if (!mesh.isMesh) return;
      mesh.castShadow = true;
      const name = `${mesh.name} ${(mesh.material as THREE.Material)?.name ?? ""}`;
      const isLens = LENS.test(name);
      const isLamp = LAMP.test(name);
      if (!isLens && !isLamp) return;
      const mat = (mesh.material as THREE.MeshStandardMaterial).clone();
      mat.emissive = new THREE.Color(isLamp ? "#f5b942" : "#38bdf8");
      mat.emissiveIntensity = 0;
      mat.toneMapped = false;
      mesh.material = mat;
      (isLamp ? parts.lamps : parts.lenses).push(mat);
    });
  }, [model, parts]);

  useFrame((state, delta) => {
    const s = useDeploy.getState();
    const g = group.current;
    if (!g) return;

    // Advance the flight clock here rather than in React: this is the only place
    // that needs frame resolution.
    if (s.phase !== "decision" && s.progress < 1) {
      const next = Math.min(1, s.progress + delta / FLIGHT_SECONDS);
      s.setProgress(next);
      const { phase } = flightAt(next);
      if (next >= 1) s.setPhase("decision");
      else if (phase !== s.phase) s.setPhase(phase as Phase);
    }

    const { pos, phase, legT } = flightAt(s.progress);
    const t = state.clock.elapsedTime;

    g.position.set(pos[0], pos[1] + Math.sin(t * 1.6) * 0.045, pos[2]);
    // Bank into the transit and level out on arrival, like something with mass.
    g.rotation.z = phase === "transit" ? -0.16 * Math.sin(legT * Math.PI) : 0;
    g.rotation.y = phase === "transit" ? -0.35 * legT : phase === "arrive" ? -0.35 : 0;

    const spun = phase !== "spinup" ? 1 : legT;
    for (const m of parts.lenses) m.emissiveIntensity = spun * 2.4;
    for (const m of parts.lamps) m.emissiveIntensity = 0.9 + Math.sin(t * 3.2) * 0.8 * spun;
  });

  return (
    <group ref={group}>
      <primitive object={model} />
    </group>
  );
}

/** Ground markers: the dock it left and the target it is going to. */
function Pads({ colour }: { colour: string }) {
  const ring = useRef<THREE.Mesh>(null);
  useFrame((state) => {
    if (!ring.current) return;
    const k = (state.clock.elapsedTime % 2.2) / 2.2;
    ring.current.scale.setScalar(0.5 + k * 1.6);
    (ring.current.material as THREE.MeshBasicMaterial).opacity = (1 - k) * 0.55;
  });
  return (
    <>
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.01, 0]}>
        <ringGeometry args={[0.5, 0.56, 64]} />
        <meshBasicMaterial color="#64748b" transparent opacity={0.5} side={THREE.DoubleSide} />
      </mesh>
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[5.2, 0.01, -2.4]}>
        <ringGeometry args={[0.5, 0.58, 64]} />
        <meshBasicMaterial color={colour} transparent opacity={0.85} side={THREE.DoubleSide} />
      </mesh>
      <mesh ref={ring} rotation={[-Math.PI / 2, 0, 0]} position={[5.2, 0.012, -2.4]}>
        <ringGeometry args={[0.62, 0.68, 64]} />
        <meshBasicMaterial color={colour} transparent opacity={0} side={THREE.DoubleSide} />
      </mesh>
    </>
  );
}

/** Camera trails the aircraft with a lag, so the flight has a sense of pursuit. */
function Rig() {
  const camera = useThree((s) => s.camera);
  const width = useThree((s) => s.size.width);
  const desired = useMemo(() => new THREE.Vector3(), []);
  const look = useMemo(() => new THREE.Vector3(), []);
  const pull = width < 700 ? 1.5 : width < 1100 ? 1.2 : 1;

  useFrame(() => {
    const { pos } = flightAt(useDeploy.getState().progress);
    desired.set(pos[0] - 4.4 * pull, pos[1] + 2.5 * pull, pos[2] + 6.2 * pull);
    camera.position.lerp(desired, 0.045);
    look.lerp(new THREE.Vector3(pos[0], pos[1], pos[2]), 0.09);
    camera.lookAt(look);
  });
  return null;
}

/* ── the overlay ────────────────────────────────────────────────────────── */
export function DeployOverlay() {
  const { resolvedTheme } = useTheme();
  const dark = resolvedTheme === "dark";
  const palette = useSeverityPalette(resolvedTheme);

  const open = useDeploy((s) => s.open);
  const alert = useDeploy((s) => s.alert);
  const phase = useDeploy((s) => s.phase);
  const outcome = useDeploy((s) => s.outcome);
  const outcomeNote = useDeploy((s) => s.outcomeNote);
  const close = useDeploy((s) => s.close);
  const resolve = useDeploy((s) => s.resolve);

  const [deciding, setDeciding] = useState(false);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close();
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [open, close]);

  const decide = useCallback(
    async (approve: boolean) => {
      if (!alert) return;
      setDeciding(true);

      /* `approve_mission` authorises a *mission*, not an alert, so the id has to
       * be resolved first. `propose_mission` returns the plan already made for
       * that alert during the session; it does not invent one on demand.
       *
       * An alert replayed from a playback index has no row in the missions
       * table, and saying so is better than fabricating an authorisation for a
       * flight that was never planned. */
      let missionId = alert.mission_id ?? null;
      if (!missionId) {
        const proposed: any = await api.confirm(
          "propose_mission",
          { alert_id: alert.id },
          true,
        );
        missionId = proposed?.result?.mission_id ?? null;
      }

      if (!missionId) {
        setDeciding(false);
        resolve(
          approve ? "approved" : "declined",
          "No mission was planned for this alert, so there is no flight to authorise. " +
            "Alerts replayed from a playback index carry their coordinates but no " +
            "mission record; run a live session to plan one.",
        );
        return;
      }

      // The only route that can execute a gated tool. The agent cannot reach it.
      const res: any = await api.confirm(
        "approve_mission",
        { mission_id: missionId, note: "authorised from the dispatch overlay" },
        approve,
      );
      setDeciding(false);
      resolve(
        approve ? "approved" : "declined",
        res?.result?.message ??
          (approve
            ? `Mission ${missionId} authorised. The decision is recorded in the audit ledger.`
            : "Declined. The decision is recorded in the audit ledger."),
      );
    },
    [alert, resolve],
  );

  const loc = alert?.location;
  const sevColour = palette[(alert?.severity ?? "high").toLowerCase()] ?? palette.high;

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-[60]"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          style={{ background: dark ? "#04070d" : "#0a1120" }}
        >
          <WebGLBoundary>
            <Canvas
              shadows
              dpr={[1, 1.6]}
              camera={{ position: [-4.4, 3.2, 7], fov: 42, near: 0.1, far: 120 }}
              gl={{ antialias: true, powerPreference: "high-performance",
                    failIfMajorPerformanceCaveat: false }}
            >
              <color attach="background" args={[dark ? "#04070d" : "#0a1120"]} />
              <fog attach="fog" args={[dark ? "#04070d" : "#0a1120", 14, 34]} />

              <ambientLight intensity={0.5} />
              <directionalLight position={[6, 9, 5]} intensity={1.9} castShadow
                               shadow-mapSize={[1024, 1024]} />
              <directionalLight position={[-6, 4, -5]} intensity={1.5} color="#38bdf8" />
              <spotLight position={[5.2, 8, -2.4]} angle={0.5} penumbra={1}
                         intensity={2.2} color={sevColour} />

              <Suspense fallback={null}>
                <Drone />
                <Environment resolution={256}>
                  <Lightformer intensity={1.4} position={[0, 6, -6]} scale={[14, 6, 1]} />
                  <Lightformer intensity={2.2} color="#38bdf8" position={[-7, 3, 4]} scale={[9, 4, 1]} />
                </Environment>
              </Suspense>

              <Pads colour={sevColour} />
              <gridHelper args={[60, 60, "#1e3a5f", "#132437"]} position={[0, 0, 0]} />
              <ContactShadows position={[0, 0, 0]} opacity={0.5} scale={26} blur={2.6} far={8} />
              <Rig />
            </Canvas>
          </WebGLBoundary>

          {/* ── HUD ─────────────────────────────────────────────────────── */}
          <div className="pointer-events-none absolute inset-0 flex flex-col justify-between p-5 sm:p-8">
            <div className="flex items-start justify-between">
              <div>
                <div className="mono mb-2 flex items-center gap-2 text-[10px] tracking-[0.24em] text-white/55 uppercase">
                  <Navigation size={11} />
                  Drone dispatch
                </div>
                <div className="flex items-center gap-2">
                  {alert && <SeverityChip severity={alert.severity} />}
                  <h2 className="text-[17px] font-semibold text-white sm:text-[20px]">
                    {alert?.title ?? "Alert"}
                  </h2>
                </div>
                <p className="mono mt-1 text-[11px] text-white/45">
                  {alert?.zone_id ?? "unassigned zone"} · {alert?.id}
                </p>
              </div>
              <button
                onClick={close}
                className="pointer-events-auto rounded-lg p-2 text-white/60 transition hover:bg-white/10"
                aria-label="Close"
              >
                <X size={17} />
              </button>
            </div>

            <div className="flex flex-col gap-3">
              <PhaseRail phase={phase} />

              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
                <Readout label="Coordinates"
                         value={loc?.lat != null && loc?.lon != null
                           ? `${loc.lat.toFixed(5)}, ${loc.lon.toFixed(5)}` : "n/a"} />
                <Readout label="Accuracy" value={loc?.accuracy_m != null ? `± ${loc.accuracy_m} m` : "n/a"} />
                <Readout label="Bearing"
                         value={loc?.bearing_from_dock_deg != null
                           ? `${Math.round(loc.bearing_from_dock_deg)}°` : "n/a"} />
                <Readout label="Distance"
                         value={loc?.distance_from_dock_m != null
                           ? `${Math.round(loc.distance_from_dock_m)} m` : "n/a"} />
                <Readout label="ETA"
                         value={loc?.eta_seconds != null ? `${Math.round(loc.eta_seconds)} s` : "n/a"} />
                <Readout label="Altitude"
                         value={loc?.recommended_altitude_m != null
                           ? `${loc.recommended_altitude_m} m AGL` : "n/a"} />
              </div>

              {loc?.within_geofence != null && (
                <div className="mono text-[11px] tracking-[0.08em]"
                     style={{ color: loc.within_geofence ? "#34d399" : "#fb7185" }}>
                  GEOFENCE: {loc.within_geofence ? "INSIDE · CLEARED" : "OUTSIDE · BLOCKED"}
                </div>
              )}

              {/* ── the decision ───────────────────────────────────────── */}
              <AnimatePresence mode="wait">
                {phase === "decision" && !outcome && (
                  <motion.div
                    key="ask"
                    initial={{ opacity: 0, y: 18 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0 }}
                    className="pointer-events-auto max-w-xl rounded-xl border p-4"
                    style={{ background: "rgba(10,17,32,0.86)", borderColor: "rgba(255,255,255,0.14)" }}
                  >
                    <div className="mb-1 flex items-center gap-2">
                      <ShieldAlert size={14} className="text-amber-300" />
                      <span className="text-[13.5px] font-semibold text-white">
                        This flight needs your authorisation
                      </span>
                    </div>
                    <p className="mb-3 text-[12px] leading-relaxed text-white/65">
                      KESTREL planned this response and checked it against battery reserve,
                      geofence, wind and daylight. It cannot launch it. Approval and refusal
                      are both written to the tamper-evident ledger.
                    </p>
                    <div className="flex gap-2">
                      <button
                        onClick={() => void decide(true)}
                        disabled={deciding}
                        className="flex items-center gap-1.5 rounded-lg px-4 py-2 text-[13px] font-semibold text-white disabled:opacity-50"
                        style={{ background: "var(--accent)" }}
                      >
                        <Check size={13} /> Approve launch
                      </button>
                      <button
                        onClick={() => void decide(false)}
                        disabled={deciding}
                        className="rounded-lg border px-4 py-2 text-[13px] font-medium text-white/85 disabled:opacity-50"
                        style={{ borderColor: "rgba(255,255,255,0.2)" }}
                      >
                        Decline
                      </button>
                    </div>
                  </motion.div>
                )}

                {outcome && (
                  <motion.div
                    key="done"
                    initial={{ opacity: 0, y: 18 }}
                    animate={{ opacity: 1, y: 0 }}
                    className="pointer-events-auto max-w-xl rounded-xl border p-4"
                    style={{ background: "rgba(10,17,32,0.86)", borderColor: "rgba(255,255,255,0.14)" }}
                  >
                    <div className="text-[13.5px] font-semibold"
                         style={{ color: outcome === "approved" ? "#34d399" : "#fbbf24" }}>
                      {outcome === "approved" ? "Launch approved" : "Launch declined"}
                    </div>
                    <p className="mt-1 text-[12px] leading-relaxed text-white/65">{outcomeNote}</p>
                    <button
                      onClick={close}
                      className="mt-3 rounded-lg border px-3.5 py-1.5 text-[12.5px] text-white/85"
                      style={{ borderColor: "rgba(255,255,255,0.2)" }}
                    >
                      Back to the console
                    </button>
                  </motion.div>
                )}
              </AnimatePresence>

              <p className="mono text-[10px] text-white/35">
                Telemetry is simulated: there is no aircraft. The coordinates, bearing and ETA
                are computed from the alert's geo-projection.
              </p>
            </div>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

function Readout({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border px-2.5 py-1.5"
         style={{ background: "rgba(255,255,255,0.05)", borderColor: "rgba(255,255,255,0.1)" }}>
      <div className="mono text-[9px] tracking-[0.14em] text-white/40 uppercase">{label}</div>
      <div className="mono mt-0.5 text-[12px] text-white">{value}</div>
    </div>
  );
}

const RAIL: { key: Phase; label: string }[] = [
  { key: "spinup", label: "Spin-up" },
  { key: "climb", label: "Climb" },
  { key: "transit", label: "Transit" },
  { key: "arrive", label: "On station" },
  { key: "decision", label: "Awaiting approval" },
];

function PhaseRail({ phase }: { phase: Phase }) {
  const current = RAIL.findIndex((p) => p.key === phase);
  return (
    <div className="flex flex-wrap items-center gap-2">
      {RAIL.map((p, i) => (
        <div key={p.key} className="flex items-center gap-2">
          <span
            className={cn(
              "mono rounded-full px-2.5 py-1 text-[10px] tracking-[0.1em] uppercase transition",
              i <= current ? "text-white" : "text-white/35",
            )}
            style={{
              background: i <= current ? "rgba(56,189,248,0.22)" : "rgba(255,255,255,0.06)",
              border: `1px solid ${i === current ? "rgba(56,189,248,0.7)" : "rgba(255,255,255,0.1)"}`,
            }}
          >
            {p.label}
          </span>
          {i < RAIL.length - 1 && <span className="h-px w-3 bg-white/15" />}
        </div>
      ))}
    </div>
  );
}

useGLTF.preload(MODEL, DRACO);
