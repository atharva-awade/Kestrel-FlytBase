import { create } from "zustand";

import type { Alert } from "@/lib/api";

/**
 * Drone dispatch choreography.
 *
 * Mirrors the pattern in `lib/story.ts`: the animation loop reads `getState()`
 * inside `useFrame` so a 60 Hz flight never re-renders React, while the HUD
 * subscribes to `phase` alone, which changes five times in the whole sequence.
 *
 * The sequence ends on an approval card rather than a landing, because that is
 * the honest end of the story: the agent can plan and propose a flight, and only
 * a person can authorise one.
 */

export type Phase = "idle" | "spinup" | "climb" | "transit" | "arrive" | "decision";

/** Seconds each phase runs for. Total ~7.2s: long enough to read the HUD, short
 *  enough that nobody reaches for the escape key. */
export const PHASE_SECONDS: Record<Exclude<Phase, "idle" | "decision">, number> = {
  spinup: 1.1,
  climb: 1.6,
  transit: 3.2,
  arrive: 1.3,
};

interface DeployState {
  open: boolean;
  alert: Alert | null;
  phase: Phase;
  /** 0-1 across the whole flight, driven by the render loop. */
  progress: number;
  /** Set when the operator decides, so the overlay can show the outcome. */
  outcome: "approved" | "declined" | null;
  outcomeNote: string;

  launch: (alert: Alert) => void;
  setPhase: (p: Phase) => void;
  setProgress: (p: number) => void;
  resolve: (outcome: "approved" | "declined", note: string) => void;
  close: () => void;
}

export const useDeploy = create<DeployState>((set) => ({
  open: false,
  alert: null,
  phase: "idle",
  progress: 0,
  outcome: null,
  outcomeNote: "",

  launch: (alert) =>
    set({ open: true, alert, phase: "spinup", progress: 0, outcome: null, outcomeNote: "" }),
  setPhase: (phase) => set({ phase }),
  setProgress: (progress) => set({ progress }),
  resolve: (outcome, outcomeNote) => set({ outcome, outcomeNote, phase: "decision" }),
  close: () => set({ open: false, phase: "idle", progress: 0, alert: null }),
}));

/** Total flight time, so the HUD can show a countdown that means something. */
export const FLIGHT_SECONDS = Object.values(PHASE_SECONDS).reduce((a, b) => a + b, 0);

/**
 * Where the aircraft is at a given moment, in world units.
 *
 * The dock sits at the origin; the target is off to one side and further out.
 * Position is eased with a smoothstep per leg so the drone accelerates out of
 * the dock and settles into the hover rather than moving at a constant rate,
 * which is what makes it read as a vehicle rather than a sprite.
 */
export function flightAt(progress: number): {
  pos: [number, number, number];
  phase: Exclude<Phase, "idle" | "decision">;
  legT: number;
} {
  const total = FLIGHT_SECONDS;
  const t = Math.max(0, Math.min(1, progress)) * total;

  const spin = PHASE_SECONDS.spinup;
  const climb = spin + PHASE_SECONDS.climb;
  const transit = climb + PHASE_SECONDS.transit;

  const ease = (k: number) => k * k * (3 - 2 * k);

  if (t < spin) {
    return { pos: [0, 0, 0], phase: "spinup", legT: t / spin };
  }
  if (t < climb) {
    const k = ease((t - spin) / PHASE_SECONDS.climb);
    return { pos: [0, k * 2.6, 0], phase: "climb", legT: k };
  }
  if (t < transit) {
    const k = ease((t - climb) / PHASE_SECONDS.transit);
    // A shallow arc: gains a little more height mid-leg, the way a real transit does.
    return {
      pos: [k * 5.2, 2.6 + Math.sin(k * Math.PI) * 0.9, -k * 2.4],
      phase: "transit",
      legT: k,
    };
  }
  const k = ease((t - transit) / PHASE_SECONDS.arrive);
  return {
    pos: [5.2, 2.6 - k * 0.7, -2.4],
    phase: "arrive",
    legT: k,
  };
}

/** Phase for a progress value, without recomputing the position. */
export function phaseAt(progress: number): Exclude<Phase, "idle" | "decision"> {
  return flightAt(progress).phase;
}
