"use client";

import { motion } from "framer-motion";
import { Send, Sparkles, Sunrise } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { ToolResult } from "@/components/ask/ToolResult";
import { Card, Pill, SectionTitle, Skeleton, fadeUp, stagger } from "@/components/ui/primitives";
import { ConfirmCard } from "@/components/ask/AskRail";
import { api, postStream, type AskTurn } from "@/lib/api";
import { cn } from "@/lib/format";

const PROMPTS = [
  { q: "What happened last night?", why: "Multi-hop retrieval over the memory pyramid" },
  { q: "Show me every truck near the loading dock", why: "Hybrid structured + visual search" },
  { q: "What rules are active and why?", why: "Reads the live rule pack" },
  { q: "Has any vehicle been seen at more than one site?", why: "Cross-site correlation" },
  { q: "Verify the audit ledger", why: "Recomputes the hash chain" },
  { q: "Explain your architecture", why: "The system accounts for itself" },
  { q: "Why did you skip so many frames?", why: "Explains its own cost decisions" },
  { q: "Was anyone here at 4am on the 3rd?", why: "Should refuse; no evidence exists" },
];

export default function AnalystPage() {
  const [messages, setMessages] = useState<
    { role: "user" | "agent"; text: string; turn?: AskTurn; tools?: any[] }[]
  >([]);
  const [tools, setTools] = useState<any[]>([]);
  /** What the agent is doing right now, rather than a bare spinner. */
  const [stage, setStage] = useState<string | null>(null);
  /** A gated tool the agent has proposed and cannot execute. */
  const [pending, setPending] = useState<any>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [brief, setBrief] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, tools]);

  const send = async (q: string) => {
    if (!q.trim() || busy) return;
    setBusy(true); setInput(""); setTools([]); setStage("routing"); setPending(null);
    setMessages((m) => [...m, { role: "user", text: q }, { role: "agent", text: "" }]);
    try {
      await postStream(api.askStreamUrl, { question: q }, (ev) => {
        if (ev.type === "intent") setStage(String(ev.intent).toLowerCase());
        else if (ev.type === "planning") setStage(`choosing tools (round ${ev.round})`);
        else if (ev.type === "tool_start") setStage(`running ${ev.tool}`);
        else if (ev.type === "composing") setStage("composing the answer");
        else if (ev.type === "heartbeat") {
          // Without this, a slow model and a dead connection look identical.
          setStage((p) => (p && !p.includes("·") ? `${p} · ${ev.waited_s}s` : p));
        } else if (ev.type === "error") {
          setMessages((m) => {
            const n = [...m];
            n[n.length - 1] = { ...n[n.length - 1], text: `That did not complete: ${ev.error}` };
            return n;
          });
        } else if (ev.type === "confirmation") {
          // This page used to drop the event entirely, so any gated action was a
          // dead end: the text said "needs your approval" with nothing to press.
          setPending(ev);
        } else if (ev.type === "tool") {
          setStage(null);
          setTools((t) => [...t, ev]);
          // Attach the evidence to the message it belongs to, so history keeps it.
          setMessages((m) => {
            const n = [...m];
            const last = { ...n[n.length - 1] };
            last.tools = [...(last.tools ?? []), ev];
            n[n.length - 1] = last;
            return n;
          });
        }
        else if (ev.type === "answer") {
          setMessages((m) => {
            const n = [...m];
            n[n.length - 1] = { ...n[n.length - 1], text: ev.text };
            return n;
          });
        } else if (ev.type === "done") {
          setStage(null);
          setMessages((m) => {
            const n = [...m];
            n[n.length - 1] = { ...n[n.length - 1], turn: ev.turn };
            return n;
          });
        }
      });
    } catch {
      setMessages((m) => {
        const n = [...m];
        n[n.length - 1] = { role: "agent", text: "Could not reach the API. Run `uv run kestrel serve`." };
        return n;
      });
    } finally {
      setBusy(false);
    }
  };

  const loadBrief = async () => {
    setBrief("…");
    const r = await api.brief();
    setBrief(r?.brief ?? "Could not generate a brief.");
  };

  return (
    <div className="mx-auto max-w-[1200px] px-5 py-6">
      <motion.div {...fadeUp}>
        <SectionTitle
          eyebrow="Conversational control plane"
          title="Ask KESTREL"
          subtitle="Every capability in the system, reachable in conversation. Answers cite the evidence they came from; anything that changes state stops for your approval."
          right={
            <button onClick={loadBrief}
                    className="flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[12px] font-medium"
                    style={{ borderColor: "var(--line)" }}>
              <Sunrise size={13} style={{ color: "var(--sev-medium)" }} />
              Morning brief
            </button>
          }
        />
      </motion.div>

      {brief && (
        <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className="mb-4">
          <Card className="p-4" style={{ borderColor: "color-mix(in oklab, var(--sev-medium) 30%, transparent)" }}>
            <div className="eyebrow mb-1.5" style={{ color: "var(--sev-medium)" }}>
              shift-change brief
            </div>
            {brief === "…" ? <Skeleton className="h-16" />
              : <p className="whitespace-pre-wrap text-[13px] leading-relaxed">{brief}</p>}
          </Card>
        </motion.div>
      )}

      {messages.length === 0 && (
        <motion.div {...stagger(1)} className="mb-4 grid gap-2 sm:grid-cols-2">
          {PROMPTS.map((p) => (
            <button key={p.q} onClick={() => send(p.q)}
                    className="card card-lift p-3 text-left">
              <div className="text-[13px] font-medium">{p.q}</div>
              <div className="mt-0.5 text-[11.5px] text-[var(--ink-4)]">{p.why}</div>
            </button>
          ))}
        </motion.div>
      )}

      <div className="space-y-4">
        {messages.map((m, i) => (
          <div key={i}>
            {m.role === "user" ? (
              <div className="ml-auto max-w-2xl rounded-2xl rounded-tr-md px-4 py-2.5 text-[14px]"
                   style={{ background: "var(--accent-soft)", color: "var(--accent-ink)" }}>
                {m.text}
              </div>
            ) : (
              <div className="max-w-3xl space-y-2.5">
                {(m.tools?.length ?? 0) > 0 && (
                  <div className="space-y-2">
                    {tools.map((t, j) => <ToolResult key={j} event={t} />)}
                  </div>
                )}
                {m.text ? (
                  <div className="whitespace-pre-wrap text-[14px] leading-relaxed">{m.text}</div>
                ) : (
                  <div className="flex items-center gap-1.5 py-2">
                    {[0, 1, 2].map((k) => (
                      <motion.span key={k} className="h-1.5 w-1.5 rounded-full"
                                   style={{ background: "var(--accent)" }}
                                   animate={{ opacity: [0.25, 1, 0.25] }}
                                   transition={{ duration: 1.1, repeat: Infinity, delay: k * 0.16 }} />
                    ))}
                    <span className="ml-1 text-[12px] text-[var(--ink-4)]">
                      {stage ? `${stage}…` : "working…"}
                    </span>
                  </div>
                )}
                {m.turn && (
                  <div className="flex flex-wrap items-center gap-2">
                    <Pill tone="muted">{m.turn.intent}</Pill>
                    <span className="text-[11px] text-[var(--ink-4)]">
                      {m.turn.tool_calls.length} tool call{m.turn.tool_calls.length === 1 ? "" : "s"} · {Math.round(m.turn.ms)}ms
                    </span>
                    <Pill tone={m.turn.verified ? "ok" : "danger"}>
                      {m.turn.verified ? "grounded" : "unverified"}
                    </Pill>
                    {m.turn.citations.length > 0 && (
                      <span className="mono text-[10.5px] text-[var(--ink-4)]">
                        {m.turn.citations.length} citation{m.turn.citations.length === 1 ? "" : "s"}
                      </span>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>
        ))}
        {pending && (
        <div className="mx-auto mb-4 max-w-3xl">
          <ConfirmCard
            pending={pending}
            onDecide={async (approve) => {
              const res = await api.confirm(pending.tool, pending.arguments, approve);
              setPending(null);
              setMessages((m) => [
                ...m,
                {
                  role: "agent",
                  text: approve
                    ? `Approved. ${(res as any)?.message ?? "Recorded in the audit ledger."}`
                    : "Declined. The decision is recorded in the audit ledger.",
                },
              ]);
            }}
          />
        </div>
      )}

      <div ref={endRef} />
      </div>

      <div className="sticky bottom-4 mt-5">
        <Card className="flex items-end gap-2 p-2">
          <textarea
            rows={1}
            value={input}
            disabled={busy}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void send(input); }
            }}
            placeholder="Ask about the site, the rules, the fleet, or how KESTREL works…"
            className="max-h-40 min-h-[32px] flex-1 resize-none bg-transparent px-2 py-1.5 text-[14px] outline-none placeholder:text-[var(--ink-4)]"
          />
          <button onClick={() => send(input)} disabled={busy || !input.trim()}
                  className="grid h-9 w-9 place-items-center rounded-lg text-white disabled:opacity-30"
                  style={{ background: "var(--accent)" }}>
            <Send size={15} />
          </button>
        </Card>
      </div>
    </div>
  );
}
