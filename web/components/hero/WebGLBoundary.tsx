"use client";

import { Component, type ReactNode } from "react";

/**
 * Catches a dead WebGL context and renders something deliberate instead.
 *
 * A 3D hero has three ways to fail that a normal component does not: the browser
 * may have WebGL disabled, the GPU may be blocklisted, or the context may be lost
 * mid-session when another tab claims the GPU. All three surface as a throw during
 * render, and the default outcome is a blank rectangle where the product's first
 * impression should be.
 *
 * The fallback is styled rather than empty, and the page below it remains fully
 * usable: the 3D layer is an enhancement to the story, not a prerequisite for
 * reading it.
 */
export class WebGLBoundary extends Component<
  { children: ReactNode; fallback?: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: unknown) {
    // Not silent: a reviewer opening the console should see why the stage is a
    // static panel rather than being left to guess.
    console.warn("[KESTREL] 3D stage unavailable, falling back to static hero.", error);
  }

  render() {
    if (this.state.failed) return this.props.fallback ?? <StaticFallback />;
    return this.props.children;
  }
}

export function StaticFallback() {
  return (
    <div className="grid h-full w-full place-items-center px-6">
      <div className="max-w-sm text-center">
        <svg
          width="72"
          height="72"
          viewBox="0 0 32 32"
          fill="none"
          className="mx-auto mb-4 opacity-70"
          aria-hidden
        >
          <circle cx="16" cy="16" r="14.5" stroke="var(--line-2)" strokeWidth="1" />
          <circle cx="16" cy="16" r="9.5" stroke="var(--line-2)" strokeWidth="0.75" />
          <path
            d="M6.5 18.5c3.6-.4 6.2-2.1 8-5 .5-.8 1.5-.8 2 0 1.8 2.9 4.4 4.6 8 5"
            stroke="var(--accent)"
            strokeWidth="2"
            strokeLinecap="round"
          />
          <circle cx="16" cy="15" r="2.1" fill="var(--accent)" />
        </svg>
        <p className="text-[12.5px] leading-relaxed text-[var(--ink-3)]">
          The 3D stage needs WebGL, which is unavailable in this browser. Everything
          below still works, so scroll on.
        </p>
      </div>
    </div>
  );
}
