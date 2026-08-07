"use client";

import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { ContactShadows, Environment, Lightformer, useGLTF } from "@react-three/drei";
import { useTheme } from "next-themes";
import { Suspense, useLayoutEffect, useMemo, useRef } from "react";
import * as THREE from "three";

import { LOOK_AT, cameraAt, lensIgnition, useStory } from "@/lib/story";

const MODEL = "/models/kestrel-drone.glb";
/** Local Draco decoder, copied out of `three/examples`. Deliberately not a CDN:
 *  the rest of KESTREL runs with no network, and the landing page should too. */
const DRACO = "/draco/gltf/";

/** The aircraft's own optics: five camera lenses plus the sensor window. */
const LENS = /(glass[_ ]?(camera|sensor)|camera_\d|glass_\d)/i;
/** The navigation lamp. */
const LAMP = /lamp/i;

/** Longest dimension of the aircraft in world units after auto-fit. The camera
 *  keyframes are written against this, so it is the one number that controls how
 *  large the drone reads on screen.
 *
 *  The source measures 11.04 × 8.98 × 11.04, so at FIT = 2.5 the airframe stands
 *  2.03 units tall, a little over a third of the frame at the hero keyframe, and
 *  about three quarters of it at the gimbal close-up. */
const FIT = 2.5;

/** How far right of frame centre the aircraft sits, as a fraction of viewport
 *  width, so the chapter copy on the left has clean space to live in.
 *
 *  Applied as a projection-matrix offset rather than by moving the model: a world
 *  offset would read differently at every keyframe, drifting off the edge during
 *  the close-ups and barely showing on the wide pull-back. Shifting the frustum
 *  holds the same screen-space composition at every camera distance. */
function subjectShift(width: number): number {
  if (width >= 1536) return 0.17;
  if (width >= 1280) return 0.15;
  if (width >= 1024) return 0.11;
  return 0; // narrow screens put the copy underneath, so the drone stays centred
}

/* ────────────────────────────────────────────────────────────────────────────
 * The aircraft
 * ──────────────────────────────────────────────────────────────────────────── */

function Drone({ reduced }: { reduced: boolean }) {
  const { scene } = useGLTF(MODEL, DRACO);
  const bob = useRef<THREE.Group>(null);
  const setReady = useStory((s) => s.setReady);

  /* The export is a Sketchfab/Maya dump with arbitrary units and an arbitrary
   * origin. Rather than hard-code numbers that only work for this one file,
   * measure the bounding box and normalise: constant on-screen size, feet on
   * y=0, centred in x/z. */
  const { lenses, lamps } = usePreparedModel(scene, setReady);

  useFrame((state) => {
    const t = state.clock.elapsedTime;
    const glow = reduced ? 0.55 : lensIgnition(useStory.getState().progress);

    for (const m of lenses) {
      // Ease rather than assign: a hard cut on scroll reads as a flicker.
      m.emissiveIntensity += (glow * 2.6 - m.emissiveIntensity) * 0.08;
    }
    for (const m of lamps) {
      // A slow beacon, the way a real airframe idles on the pad.
      m.emissiveIntensity = reduced ? 1.2 : 0.9 + Math.sin(t * 2.1) * 0.75;
    }

    if (bob.current && !reduced) {
      // Hovering, not floating: a few centimetres of lift and well under a degree
      // of attitude change. Anything larger looks like a toy on a string.
      bob.current.position.y = Math.sin(t * 0.85) * 0.055;
      bob.current.rotation.z = Math.sin(t * 0.62) * 0.014;
      bob.current.rotation.x = Math.cos(t * 0.5) * 0.009;
    }
  });

  return (
    <group ref={bob}>
      <primitive object={scene} />
    </group>
  );
}

/** Fit + material preparation. Split out only to keep `Drone` readable. */
function usePreparedModel(scene: THREE.Group, setReady: (r: boolean) => void) {
  const result = useMemo(
    () => ({ lenses: [] as THREE.MeshStandardMaterial[], lamps: [] as THREE.MeshStandardMaterial[] }),
    [],
  );

  useLayoutEffect(() => {
    /* Reset before measuring. `useGLTF` caches and reuses the same scene object,
     * and React StrictMode invokes this effect twice, so on the second pass the
     * bounding box would be measured on an *already normalised* model, `scale`
     * would come out as 1, and the transform would be wiped, leaving the aircraft
     * at its raw export size filling the whole viewport. Measuring from identity
     * every time makes the fit idempotent. */
    scene.scale.setScalar(1);
    scene.position.set(0, 0, 0);
    scene.updateMatrixWorld(true);

    const box = new THREE.Box3().setFromObject(scene);
    const size = box.getSize(new THREE.Vector3());
    const centre = box.getCenter(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z) || 1;
    const scale = FIT / maxDim;

    scene.scale.setScalar(scale);
    scene.position.set(-centre.x * scale, 0.55 - box.min.y * scale, -centre.z * scale);

    result.lenses.length = 0;
    result.lamps.length = 0;

    scene.traverse((o) => {
      const mesh = o as THREE.Mesh;
      if (!mesh.isMesh) return;
      mesh.castShadow = true;
      mesh.receiveShadow = true;

      const name = `${mesh.name} ${(mesh.material as THREE.Material)?.name ?? ""}`;
      const isLens = LENS.test(name);
      const isLamp = LAMP.test(name);
      if (!isLens && !isLamp) return;

      /* Six materials are shared across forty-six meshes. Mutating one in place
       * would set the entire airframe glowing, so each emissive part gets its own
       * clone before anything is touched. */
      const mat = (mesh.material as THREE.MeshStandardMaterial).clone();
      mat.emissive = new THREE.Color(isLamp ? "#f5b942" : "#38bdf8");
      mat.emissiveIntensity = 0;
      mat.toneMapped = false;
      mesh.material = mat;
      (isLamp ? result.lamps : result.lenses).push(mat);
    });

    setReady(true);
  }, [scene, result, setReady]);

  return result;
}

/* ────────────────────────────────────────────────────────────────────────────
 * The camera
 * ──────────────────────────────────────────────────────────────────────────── */

function Rig({ reduced }: { reduced: boolean }) {
  const camera = useThree((s) => s.camera) as THREE.PerspectiveCamera;
  const width = useThree((s) => s.size.width);
  const height = useThree((s) => s.size.height);
  const pointer = useThree((s) => s.pointer);

  const desired = useMemo(() => new THREE.Vector3(), []);
  const look = useMemo(() => new THREE.Vector3(...LOOK_AT), []);

  /* Offsetting the frustum moves the subject right of centre by a fixed fraction
   * of the frame, independent of how close the camera is, which is the whole
   * point, since the story ranges from a gimbal close-up to a wide pull-back. */
  useLayoutEffect(() => {
    const shift = subjectShift(width);
    if (shift === 0) camera.clearViewOffset();
    else camera.setViewOffset(width, height, -shift * width, 0, width, height);
    camera.updateProjectionMatrix();
    return () => {
      camera.clearViewOffset();
      camera.updateProjectionMatrix();
    };
  }, [camera, width, height]);

  /* One multiplier on the whole orbit radius. The choreography is identical at
   * every size; only how far back the rig sits changes. Narrow screens need the
   * most room, because the vertical field of view is what constrains them. */
  const pull = width < 640 ? 1.55 : width < 1024 ? 1.3 : 1.12;

  useFrame(() => {
    const p = reduced ? 0 : useStory.getState().progress;
    const [x, y, z] = cameraAt(p);

    // A whisper of parallax on pointer move. Small enough that nobody consciously
    // notices it, large enough that the scene stops feeling like a video.
    const px = reduced ? 0 : pointer.x * 0.22;
    const py = reduced ? 0 : pointer.y * 0.14;

    desired.set(x * pull + px, y * pull + py, z * pull);

    /* The line that makes it feel expensive: scroll sets a *target*, the camera
     * eases toward it. Flicking the wheel glides instead of snapping. */
    camera.position.lerp(desired, reduced ? 1 : 0.075);
    camera.lookAt(look);
  });

  return null;
}

/** A slow sensor sweep on the ground plane: the system's own metaphor, and the
 *  only element on the stage that is decorative rather than the aircraft itself. */
function Sweep({ reduced, dark }: { reduced: boolean; dark: boolean }) {
  const ring = useRef<THREE.Mesh>(null);
  useFrame((state) => {
    if (!ring.current || reduced) return;
    const t = (state.clock.elapsedTime % 6) / 6;
    ring.current.scale.setScalar(0.6 + t * 4.4);
    (ring.current.material as THREE.MeshBasicMaterial).opacity = (1 - t) * (dark ? 0.3 : 0.18);
  });

  return (
    <mesh ref={ring} rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.012, 0]}>
      <ringGeometry args={[0.94, 1, 96]} />
      <meshBasicMaterial
        color={dark ? "#7dd3fc" : "#0ea5e9"}
        transparent
        opacity={0}
        side={THREE.DoubleSide}
        depthWrite={false}
      />
    </mesh>
  );
}

/* ────────────────────────────────────────────────────────────────────────────
 * Stage
 * ──────────────────────────────────────────────────────────────────────────── */

export default function DroneCanvas({ reduced = false }: { reduced?: boolean }) {
  const { resolvedTheme } = useTheme();
  const dark = resolvedTheme === "dark";
  const active = useStory((s) => s.active);

  return (
    <Canvas
      shadows
      dpr={[1, 1.6]}
      // Rendering stops entirely once the story scrolls out of view. A hero that
      // keeps burning a GPU core while the operator reads the page below is a
      // battery bug, not a feature.
      frameloop={active ? "always" : "demand"}
      camera={{ position: [4.2, 1.9, 6.4], fov: 38, near: 0.1, far: 100 }}
      gl={{
        alpha: true,
        antialias: true,
        powerPreference: "high-performance",
        failIfMajorPerformanceCaveat: false,
      }}
      style={{ background: "transparent" }}
    >
      <ambientLight intensity={dark ? 0.32 : 0.75} />
      <hemisphereLight
        intensity={dark ? 0.25 : 0.6}
        color={dark ? "#1c283f" : "#ffffff"}
        groundColor={dark ? "#050810" : "#dce7f4"}
      />
      {/* Key */}
      <directionalLight
        position={[5.5, 8.5, 4.5]}
        intensity={dark ? 1.5 : 2.1}
        castShadow
        shadow-mapSize={[1024, 1024]}
        shadow-bias={-0.0004}
      />
      {/* Rim, tinted with the accent so the silhouette separates from the page */}
      <directionalLight
        position={[-6, 3.5, -5]}
        intensity={dark ? 1.6 : 0.85}
        color={dark ? "#38bdf8" : "#7dd3fc"}
      />
      <spotLight position={[0, 9, 0]} angle={0.6} penumbra={1} intensity={dark ? 0.9 : 0.5} />

      <Suspense fallback={null}>
        <Drone reduced={reduced} />
        {/* A procedural environment: reflections on the airframe with no HDR fetch,
            which keeps the offline guarantee intact. */}
        <Environment resolution={256}>
          <Lightformer intensity={dark ? 1.1 : 2.2} position={[0, 5, -6]} scale={[12, 6, 1]} />
          <Lightformer
            intensity={dark ? 2.4 : 1.1}
            color="#38bdf8"
            position={[-6, 2, 3]}
            scale={[8, 4, 1]}
          />
          <Lightformer intensity={dark ? 0.8 : 1.6} position={[6, 3, 3]} scale={[8, 4, 1]} />
        </Environment>
      </Suspense>

      <Sweep reduced={reduced} dark={dark} />
      <ContactShadows
        position={[0, 0, 0]}
        opacity={dark ? 0.45 : 0.3}
        scale={11}
        blur={2.9}
        far={5}
        resolution={512}
        color={dark ? "#000000" : "#1e3a5f"}
      />

      <Rig reduced={reduced} />
    </Canvas>
  );
}

useGLTF.preload(MODEL, DRACO);
