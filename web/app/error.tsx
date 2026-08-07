"use client";

import { RefreshCw } from "lucide-react";
import { useEffect } from "react";

/**
 * Route-level error boundary.
 *
 * Without one, a single thrown render blanks the whole route and the operator
 * gets a white page with no way back. That is the worst possible failure for a
 * monitoring tool: indistinguishable from "nothing is happening".
 *
 * This states what broke, offers a retry that re-renders rather than reloads,
 * and leaves the navigation intact so the rest of the console stays reachable.
 */
export default function RouteError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // A reviewer opening the console should find the real stack, not a summary.
    console.error("[KESTREL] route error", error);
  }, [error]);

  return (
    <div className="mx-auto max-w-2xl px-5 py-20">
      <div className="card p-6">
        <div className="eyebrow mb-2" style={{ color: "var(--sev-high)" }}>
          this deck failed to render
        </div>
        <h1 className="display mb-3 text-[22px]">Something in this view threw.</h1>
        <p className="mb-4 text-[13.5px] leading-relaxed text-[var(--ink-2)]">
          The rest of the console is unaffected, and no data was lost. If this keeps
          happening, the browser console carries the stack trace.
        </p>

        <pre
          className="mono mb-4 max-h-40 overflow-auto rounded-lg border p-3 text-[11px] leading-relaxed text-[var(--ink-3)]"
          style={{ borderColor: "var(--line)", background: "var(--surface-2)" }}
        >
          {error.message}
          {error.digest ? `\n\ndigest: ${error.digest}` : ""}
        </pre>

        <div className="flex flex-wrap gap-2">
          <button
            onClick={reset}
            className="flex items-center gap-1.5 rounded-lg px-4 py-2 text-[13px] font-semibold text-white"
            style={{ background: "var(--accent)" }}
          >
            <RefreshCw size={13} />
            Try again
          </button>
          <a
            href="/command"
            className="rounded-lg border px-4 py-2 text-[13px] font-medium"
            style={{ borderColor: "var(--line-2)" }}
          >
            Back to command
          </a>
        </div>
      </div>
    </div>
  );
}
