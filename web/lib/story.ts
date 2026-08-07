import { create } from "zustand";

/**
 * Scroll choreography for the landing page.
 *
 * GSAP ScrollTrigger writes `progress` (0–1); the R3F camera rig reads it every
 * frame to place the camera. It lives in a store rather than React state for one
 * specific reason: scroll fires ~60 times a second, and `useStory.getState()`
 * reads the value *without subscribing*, so the React tree never re-renders from
 * the animation loop. Components that genuinely need to re-render (the chapter
 * card) subscribe to a derived, low-cardinality slice instead.
 */

type StoryState = {
  progress: number;
  setProgress: (p: number) => void;
  /** True while the pinned 3D stage is the thing on screen. */
  active: boolean;
  setActive: (a: boolean) => void;
  /** The model has finished loading and the first frame has been drawn. */
  ready: boolean;
  setReady: (r: boolean) => void;
};

export const useStory = create<StoryState>((set) => ({
  progress: 0,
  setProgress: (progress) => set({ progress }),
  active: true,
  setActive: (active) => set({ active }),
  ready: false,
  setReady: (ready) => set({ ready }),
}));

/* ── the narrative ──────────────────────────────────────────────────────────
 *
 * Seven chapters. Each one owns a camera position, and each camera position was
 * chosen to *mean* something rather than merely to differ from the last. The
 * clearest example is chapter 4, which is a true top-down nadir shot: the
 * drone's own viewpoint, held while the copy is about dispatch coordinates.
 */

export interface Chapter {
  key: string;
  eyebrow: string;
  title: string;
  line: string;
  /** Short factual proof, shown as a chip. Every one is a measured number. */
  proof?: string;
  /** Camera position in world units. The look-at target never moves. */
  camera: [number, number, number];
}

export const CHAPTERS: Chapter[] = [
  {
    key: "reveal",
    eyebrow: "Autonomous drone security analyst",
    title: "The drone security analyst that never blinks.",
    line:
      "A docked patrol drone produces about 57,600 frames per shift. Almost all of them show the same empty yard.",
    camera: [4.2, 1.9, 6.4],
  },
  {
    key: "perceive",
    eyebrow: "01 · Perceive",
    title: "Five tiers. Each earns the next.",
    line:
      "A CPU gate decides whether a frame is worth spending anything on. Only what survives reaches a detector, a tracker, and finally a vision model that returns a structured scene graph, never prose.",
    proof: "YOLO11 on-device · ~12 ms",
    camera: [1.9, 1.5, 3.7],
  },
  {
    key: "remember",
    eyebrow: "02 · Remember",
    title: "The same vehicle. Seventh visit. First time at 02:00.",
    line:
      "Persistent entities, a salience-weighted memory pyramid, and a normalcy baseline that abstains until it has enough history to judge. No single frame is alarming; only the pattern is.",
    proof: "8 hours → ~12k queryable tokens",
    camera: [6.1, 2.0, -2.2],
  },
  {
    key: "reason",
    eyebrow: "03 · Reason",
    title: "Write a rule in English. Watch it prove itself.",
    line:
      "Rules are declarative data with real temporal operators, so a model can author them and the engine can replay them over indexed history, before they are ever allowed to fire.",
    proof: "12 condition types · backtested",
    camera: [-4.9, 2.6, -3.9],
  },
  {
    key: "act",
    eyebrow: "04 · Act",
    title: "An alert you can fly to.",
    line:
      "Geo-projected coordinates with an accuracy radius, bearing and ETA from the dock, a recommended altitude and a geofence verdict, then a flight plan checked against battery reserve, wind and daylight.",
    proof: "18.760018, 73.862886 · ±4.9 m",
    camera: [0.4, 7.4, 0.8],
  },
  {
    key: "converse",
    eyebrow: "05 · Converse",
    title: "Ask it anything. It will tell you when it does not know.",
    line:
      "A conversational control plane over every capability, answering with live UI and citations. Tools that move an aircraft cannot be executed by the agent at all, only proposed.",
    proof: "27 tools · 8 classes · 4 gated",
    camera: [2.6, 0.85, 3.9],
  },
  {
    key: "prove",
    eyebrow: "06 · Prove",
    title: "Every number here was measured.",
    line:
      "Including the one that made us look worse. The design plan predicted 94% gate efficiency; the real footage measured 38.9%. Both figures are reported, with their conditions attached.",
    proof: "8/8 scenarios · 90 tests · 6/6 chaos",
    camera: [-2.0, 3.4, 9.2],
  },
];

/** Where the camera always points. Keeping this fixed is what holds the aircraft
 *  composed at every scroll position instead of drifting.
 *
 *  The value is the airframe's centre after auto-fit: it sits from y = 0.55 (the
 *  hover gap above the shadow plane) to y = 2.58, so 1.5 is very slightly below
 *  centre, which reads better than dead-centre, because it leaves headroom for
 *  the rotor arms. */
export const LOOK_AT: [number, number, number] = [0, 1.5, 0];

/** Chapter index for a given progress value. */
export function chapterAt(progress: number): number {
  const n = CHAPTERS.length - 1;
  return Math.max(0, Math.min(n, Math.round(progress * n)));
}

/** Camera position for a progress value, eased between keyframes.
 *
 *  `smoothstep` on the segment fraction removes the velocity discontinuity you
 *  get from raw linear interpolation. Without it the camera visibly "kinks" as
 *  it passes each keyframe. */
export function cameraAt(progress: number): [number, number, number] {
  const n = CHAPTERS.length - 1;
  const x = Math.max(0, Math.min(1, progress)) * n;
  const i = Math.min(n - 1, Math.floor(x));
  const raw = x - i;
  const t = raw * raw * (3 - 2 * raw);
  const a = CHAPTERS[i].camera;
  const b = CHAPTERS[i + 1].camera;
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
}

/** 0→1 ramp peaking on the "perceive" chapter, used to ignite the drone's own
 *  camera lenses exactly as the page starts talking about perception. */
export function lensIgnition(progress: number): number {
  const n = CHAPTERS.length - 1;
  const target = 1 / n; // chapter 1
  const d = Math.abs(progress - target);
  const peak = Math.max(0, 1 - d * 5);
  // Never fully dark after the reveal; the sensor should read as "awake".
  return Math.max(progress > 0.02 ? 0.22 : 0, peak);
}
