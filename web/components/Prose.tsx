"use client";

/**
 * Render the self-knowledge prose as structure rather than a wall of monospace.
 *
 * `selfknowledge.py` writes genuinely structured text: ALL-CAPS section heads,
 * dash bullets, indented sub-bullets, and aligned tier tables such as
 * `tier 1  YOLO11  12 ms  detect`. The architecture deck was rendering all of it
 * inside one `<pre className="mono">`, which threw that structure away and made
 * the most explanatory page in the console the least readable one.
 *
 * This is a small parser rather than a markdown dependency, because the source is
 * not markdown. It keeps the text verbatim and only decides how to present each
 * line, so nothing can be lost in translation.
 */

type Block =
  | { kind: "head"; text: string }
  | { kind: "para"; text: string; lead?: string }
  | { kind: "bullet"; text: string; depth: number }
  | { kind: "table"; rows: string[] };

/** Section labels are written inline: "PERMISSIONS. Every tool carries a class."
 *  Splitting the lead-in out keeps the sentence intact while letting the label
 *  carry the visual weight it was written to carry. */
const LEAD_IN = /^([A-Z][A-Z0-9 '’-]{2,40})[.:]\s+(.+)$/;

/** A heading: short, no sentence punctuation, and mostly capitals. */
function isHead(line: string): boolean {
  const t = line.trim();
  if (t.length < 3 || t.length > 64) return false;
  if (/[.?!,;]$/.test(t)) return false;
  const letters = t.replace(/[^A-Za-z]/g, "");
  if (letters.length < 3) return false;
  const caps = t.replace(/[^A-Z]/g, "").length;
  return caps / letters.length > 0.75;
}

/** A row from one of the aligned tier tables: leading token then wide gaps. */
function isTableRow(line: string): boolean {
  return /^\s{2,}\S/.test(line) && /\S\s{2,}\S/.test(line.trim());
}

export function parseProse(src: string): Block[] {
  const out: Block[] = [];
  let para: string[] = [];
  let table: string[] = [];

  const flushPara = () => {
    if (para.length) {
      const joined = para.join(" ");
      const lead = LEAD_IN.exec(joined);
      out.push(
        lead ? { kind: "para", lead: lead[1], text: lead[2] } : { kind: "para", text: joined },
      );
    }
    para = [];
  };
  const flushTable = () => {
    if (table.length) out.push({ kind: "table", rows: table });
    table = [];
  };

  for (const raw of src.split("\n")) {
    const line = raw.replace(/\s+$/, "");
    const t = line.trim();

    if (!t) {
      flushPara();
      flushTable();
      continue;
    }
    if (isTableRow(line) && !/^\s*[-*·]/.test(line)) {
      flushPara();
      table.push(line);
      continue;
    }
    flushTable();

    if (isHead(t)) {
      flushPara();
      out.push({ kind: "head", text: t.replace(/[.:]$/, "") });
      continue;
    }
    const bullet = /^([-*·])\s+(.*)$/.exec(t);
    if (bullet) {
      flushPara();
      const depth = /^\s{4,}/.test(line) ? 1 : 0;
      out.push({ kind: "bullet", text: bullet[2], depth });
      continue;
    }
    para.push(t);
  }
  flushPara();
  flushTable();
  return out;
}

export function Prose({ text }: { text: string }) {
  const blocks = parseProse(text);
  return (
    <div className="space-y-3">
      {blocks.map((b, i) => {
        if (b.kind === "head") {
          return (
            <h3
              key={i}
              className="pt-2 text-[11px] font-bold tracking-[0.14em] uppercase"
              style={{ color: "var(--accent-ink)" }}
            >
              {b.text}
            </h3>
          );
        }
        if (b.kind === "bullet") {
          return (
            <div
              key={i}
              className="flex gap-2.5 text-[13px] leading-relaxed text-[var(--ink-2)]"
              style={{ marginLeft: b.depth * 18 }}
            >
              <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full"
                    style={{ background: "var(--accent)" }} />
              <span>{b.text}</span>
            </div>
          );
        }
        if (b.kind === "table") {
          return (
            <pre
              key={i}
              className="mono overflow-x-auto rounded-lg border px-3 py-2.5 text-[11.5px] leading-[1.65] text-[var(--ink-2)]"
              style={{ borderColor: "var(--line)", background: "var(--surface-2)" }}
            >
              {b.rows.join("\n")}
            </pre>
          );
        }
        return (
          <p key={i} className="text-[13.5px] leading-relaxed text-[var(--ink-2)]">
            {b.lead && (
              <span
                className="mr-1.5 text-[11px] font-bold tracking-[0.12em] uppercase"
                style={{ color: "var(--accent-ink)" }}
              >
                {b.lead}
              </span>
            )}
            {b.text}
          </p>
        );
      })}
    </div>
  );
}
