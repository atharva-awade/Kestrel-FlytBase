import Link from "next/link";

/** 404. Names the decks that do exist rather than dead-ending. */
export default function NotFound() {
  return (
    <div className="mx-auto max-w-2xl px-5 py-24 text-center">
      <div className="eyebrow mb-2">404</div>
      <h1 className="display mb-3 text-[26px]">No deck at that address.</h1>
      <p className="mb-6 text-[13.5px] leading-relaxed text-[var(--ink-2)]">
        The console covers command, live operations, investigation, entities, rules,
        the assistant, evaluations and the architecture.
      </p>
      <div className="flex flex-wrap justify-center gap-2">
        {[
          ["Command", "/command"],
          ["Console", "/console"],
          ["Investigate", "/investigate"],
          ["Ask KESTREL", "/analyst"],
        ].map(([label, href]) => (
          <Link
            key={href}
            href={href}
            className="rounded-lg border px-4 py-2 text-[13px] font-medium"
            style={{ borderColor: "var(--line-2)" }}
          >
            {label}
          </Link>
        ))}
      </div>
    </div>
  );
}
