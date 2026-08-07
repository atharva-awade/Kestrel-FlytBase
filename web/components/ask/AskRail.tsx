"use client";

import { AnimatePresence, motion } from "framer-motion";
import { ChevronRight, Mic, Send, Sparkles, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { ToolResult } from "@/components/ask/ToolResult";
import { api, postStream, type AskTurn } from "@/lib/api";
import { cn } from "@/lib/format";

/**
 * Ask KESTREL: the conversational control plane.
 *
 * Present on every route, not a page of its own. Three properties make it a
 * control plane rather than a chat box:
 *
 * 1. Results render as live components, not text. A search returns a frame strip;
 *    an entity returns a dossier card; a mission returns an approve/deny card.
 * 2. It can drive the app. A `navigate_to` tool result moves the operator's view.
 * 3. Actions are gated. When the agent proposes something consequential it stops
 *    and surfaces a confirmation card; approving is a separate request that the
 *    agent itself cannot make.
 */

const SUGGESTIONS = [
  "What happened last night?",
  "Show me every truck near the dock",
  "What rules are active?",
  "Has this vehicle been seen at another site?",
  "Explain your architecture",
  "Verify the audit ledger",
];

interface Message {
  role: "user" | "agent";
  text: string;
  turn?: AskTurn;
  streaming?: boolean;
}

export function AskRail() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [width, setWidth] = useState(400);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [toolEvents, setToolEvents] = useState<any[]>([]);
  /** What the agent is doing right now. Shown instead of a bare "thinking",
   *  which cannot distinguish a slow model from a dead connection. */
  const [stage, setStage] = useState<string | null>(null);
  const [pending, setPending] = useState<AskTurn["pending_confirmation"]>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  // ⌘K / Ctrl-K opens it from anywhere; ⌘/ focuses the input.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const meta = e.metaKey || e.ctrlKey;
      if (meta && e.key.toLowerCase() === "j") {
        e.preventDefault();
        setOpen((v) => !v);
      }
      if (meta && e.key === "/") {
        e.preventDefault();
        setOpen(true);
        setTimeout(() => inputRef.current?.focus(), 60);
      }
      if (e.key === "Escape" && open) setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, toolEvents]);

  const send = useCallback(
    async (question: string) => {
      if (!question.trim() || busy) return;
      setBusy(true);
      setInput("");
      setPending(null);
      setToolEvents([]);
      setStage("routing");
      setMessages((m) => [...m, { role: "user", text: question }, { role: "agent", text: "", streaming: true }]);

      abortRef.current?.abort();
      const ac = new AbortController();
      abortRef.current = ac;

      try {
        await postStream(
          api.askStreamUrl,
          { question, selection: currentSelection() },
          (ev) => {
            if (ev.type === "intent") {
              setStage(`${String(ev.intent).toLowerCase()}`);
            } else if (ev.type === "planning") {
              setStage(`choosing tools (round ${ev.round})`);
            } else if (ev.type === "tool_start") {
              setStage(`running ${ev.tool}`);
            } else if (ev.type === "composing") {
              setStage("composing the answer");
            } else if (ev.type === "heartbeat") {
              // Slow and broken look identical without this.
              setStage((prev) =>
                prev && !prev.includes("·") ? `${prev} · ${ev.waited_s}s` : prev);
            } else if (ev.type === "error") {
              setMessages((m) => {
                const next = [...m];
                const last = next[next.length - 1];
                if (last?.role === "agent") {
                  last.text = `That did not complete: ${ev.error}`;
                  last.streaming = false;
                }
                return next;
              });
            } else if (ev.type === "tool") {
              setToolEvents((t) => [...t, ev]);
              // Tool results can move the operator's view; the agent drives the app.
              const nav = ev.result?.navigate;
              if (nav?.view) {
                const path =
                  nav.view === "site" && nav.site_id ? `/site/${nav.site_id}` : `/${nav.view}`;
                router.push(path);
                toast.success(`Navigated to ${nav.view}`);
              }
            } else if (ev.type === "confirmation") {
              setPending(ev);
            } else if (ev.type === "answer") {
              setMessages((m) => {
                const next = [...m];
                const last = next[next.length - 1];
                if (last?.role === "agent") {
                  last.text = ev.text;
                  last.streaming = false;
                }
                return next;
              });
            } else if (ev.type === "done") {
              setStage(null);
              setMessages((m) => {
                const next = [...m];
                const last = next[next.length - 1];
                if (last?.role === "agent") {
                  last.turn = ev.turn;
                  last.streaming = false;
                }
                return next;
              });
            }
          },
          ac.signal,
        );
      } catch (e: any) {
        if (e?.name !== "AbortError") {
          setMessages((m) => {
            const next = [...m];
            const last = next[next.length - 1];
            if (last?.role === "agent") {
              last.text =
                "I could not reach the KESTREL API. Start it with `uv run kestrel serve`.";
              last.streaming = false;
            }
            return next;
          });
        }
      } finally {
        setBusy(false);
      }
    },
    [busy, router],
  );

  const decide = async (approve: boolean) => {
    if (!pending) return;
    try {
      const res = await api.confirm(pending.tool, pending.arguments, approve);
      toast[approve ? "success" : "message"](
        approve ? `Executed ${pending.tool}` : `Declined ${pending.tool}`,
      );
      setMessages((m) => [
        ...m,
        {
          role: "agent",
          text: approve
            ? `Done. \`${pending.tool}\` executed and written to the audit ledger.`
            : `Declined. Nothing was changed; the decision is recorded in the ledger.`,
          turn: undefined,
        },
      ]);
      void res;
    } catch {
      toast.error("Could not reach the API");
    } finally {
      setPending(null);
    }
  };

  const listen = () => {
    const SR =
      (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (!SR) return toast.error("Voice input is not supported in this browser");
    const rec = new SR();
    rec.lang = "en-GB";
    rec.interimResults = false;
    rec.onresult = (e: any) => {
      const said = e.results[0][0].transcript;
      setInput(said);
      void send(said);
    };
    rec.onerror = () => toast.error("Could not hear that");
    rec.start();
    toast.message("Listening…");
  };

  return (
    <>
      {!open && (
        <motion.button
          initial={{ opacity: 0, x: 20 }}
          animate={{ opacity: 1, x: 0 }}
          onClick={() => setOpen(true)}
          className="fixed bottom-6 right-6 z-40 flex items-center gap-2 rounded-full border px-4 py-2.5 text-[13px] font-medium shadow-[var(--shadow-lg)] transition-transform hover:-translate-y-0.5"
          style={{
            background: "var(--surface)",
            borderColor: "var(--line)",
            boxShadow: "var(--shadow-accent)",
          }}
        >
          <Sparkles size={15} style={{ color: "var(--accent)" }} />
          Ask KESTREL
          <kbd className="mono ml-1 rounded border px-1 text-[10px] text-[var(--ink-4)]"
               style={{ borderColor: "var(--line)" }}>
            ⌘J
          </kbd>
        </motion.button>
      )}

      <AnimatePresence>
        {open && (
          <motion.aside
            initial={{ width: 0, opacity: 0 }}
            animate={{ width, opacity: 1 }}
            exit={{ width: 0, opacity: 0 }}
            transition={{ type: "spring", stiffness: 240, damping: 30 }}
            className="relative z-30 flex shrink-0 flex-col border-l"
            style={{ background: "var(--surface)", borderColor: "var(--line)" }}
          >
            {/* Drag to resize; long research sessions want more room. */}
            <div
              onMouseDown={(e) => {
                e.preventDefault();
                const startX = e.clientX;
                const startW = width;
                const move = (ev: MouseEvent) =>
                  setWidth(Math.min(760, Math.max(320, startW + (startX - ev.clientX))));
                const up = () => {
                  window.removeEventListener("mousemove", move);
                  window.removeEventListener("mouseup", up);
                };
                window.addEventListener("mousemove", move);
                window.addEventListener("mouseup", up);
              }}
              className="absolute left-0 top-0 h-full w-1 cursor-col-resize hover:bg-[var(--accent)]"
            />

            <div className="flex h-12 items-center gap-2 border-b px-3">
              <Sparkles size={14} style={{ color: "var(--accent)" }} />
              <span className="text-[13px] font-semibold">Ask KESTREL</span>
              <span className="eyebrow ml-1">control plane</span>
              <button
                onClick={() => setOpen(false)}
                className="ml-auto grid h-7 w-7 place-items-center rounded-md hover:bg-[var(--surface-2)]"
                aria-label="Close"
              >
                <X size={14} />
              </button>
            </div>

            <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto p-3">
              {messages.length === 0 && (
                <div className="space-y-3 pt-2">
                  <p className="text-[12.5px] leading-relaxed text-[var(--ink-3)]">
                    I can search the footage, explain an alert, write a rule, dispatch a
                    drone for your approval, or explain how I work. Everything I say
                    cites the evidence it came from.
                  </p>
                  <div className="space-y-1.5">
                    {SUGGESTIONS.map((s) => (
                      <button
                        key={s}
                        onClick={() => void send(s)}
                        className="flex w-full items-center gap-2 rounded-lg border px-2.5 py-2 text-left text-[12.5px] transition-colors hover:bg-[var(--surface-2)]"
                        style={{ borderColor: "var(--line)" }}
                      >
                        <ChevronRight size={12} className="shrink-0 text-[var(--ink-4)]" />
                        {s}
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {messages.map((m, i) => (
                <div key={i}>
                  {m.role === "user" ? (
                    <div className="ml-6 rounded-xl rounded-tr-sm px-3 py-2 text-[13px]"
                         style={{ background: "var(--accent-soft)", color: "var(--accent-ink)" }}>
                      {m.text}
                    </div>
                  ) : (
                    <AgentMessage
                      message={m}
                      toolEvents={i === messages.length - 1 ? toolEvents : []}
                      stage={i === messages.length - 1 ? stage : null}
                    />
                  )}
                </div>
              ))}

              {pending && <ConfirmCard pending={pending} onDecide={decide} />}
            </div>

            <div className="border-t p-2.5">
              <div className="flex items-end gap-1.5 rounded-xl border px-2 py-1.5"
                   style={{ borderColor: "var(--line)", background: "var(--surface-2)" }}>
                <textarea
                  ref={inputRef}
                  rows={1}
                  value={input}
                  disabled={busy}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      void send(input);
                    }
                  }}
                  placeholder="Ask anything about the site…"
                  className="max-h-32 min-h-[26px] flex-1 resize-none bg-transparent text-[13px] outline-none placeholder:text-[var(--ink-4)]"
                />
                <button onClick={listen} title="Voice input"
                        className="grid h-7 w-7 place-items-center rounded-md hover:bg-[var(--surface-3)]">
                  <Mic size={13} className="text-[var(--ink-3)]" />
                </button>
                <button
                  onClick={() => void send(input)}
                  disabled={busy || !input.trim()}
                  className="grid h-7 w-7 place-items-center rounded-md disabled:opacity-30"
                  style={{ background: "var(--accent)", color: "#fff" }}
                >
                  <Send size={13} />
                </button>
              </div>
            </div>
          </motion.aside>
        )}
      </AnimatePresence>
    </>
  );
}

function AgentMessage(
  { message, toolEvents, stage }:
  { message: Message; toolEvents: any[]; stage?: string | null },
) {
  const turn = message.turn;
  return (
    <div className="space-y-2">
      {toolEvents.length > 0 && (
        <div className="space-y-1.5">
          {toolEvents.map((ev, i) => (
            <ToolResult key={i} event={ev} />
          ))}
        </div>
      )}

      {message.streaming && !message.text ? (
        <div className="flex items-center gap-1.5 px-1 py-2">
          {[0, 1, 2].map((i) => (
            <motion.span
              key={i}
              className="h-1.5 w-1.5 rounded-full"
              style={{ background: "var(--accent)" }}
              animate={{ opacity: [0.25, 1, 0.25] }}
              transition={{ duration: 1.1, repeat: Infinity, delay: i * 0.16 }}
            />
          ))}
          <span className="ml-1 text-[11.5px] text-[var(--ink-4)]">
            {stage ? `${stage}…` : "thinking…"}
          </span>
        </div>
      ) : (
        <div className="whitespace-pre-wrap text-[13px] leading-relaxed text-[var(--ink)]">
          {message.text}
        </div>
      )}

      {turn && (
        <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
          <span className="eyebrow">{turn.intent}</span>
          <span className="text-[10.5px] text-[var(--ink-4)]">{Math.round(turn.ms)}ms</span>
          <span
            className={cn(
              "rounded-full border px-1.5 py-[1px] text-[10px] font-medium",
              turn.verified
                ? "border-[color-mix(in_oklab,var(--ok)_30%,transparent)] text-[var(--ok)]"
                : "border-[color-mix(in_oklab,var(--sev-critical)_30%,transparent)] text-[var(--sev-critical)]",
            )}
            title={turn.verification_note}
          >
            {turn.verified ? "grounded" : "unverified"}
          </span>
        </div>
      )}
    </div>
  );
}

export function ConfirmCard({
  pending, onDecide,
}: { pending: NonNullable<AskTurn["pending_confirmation"]>; onDecide: (a: boolean) => void }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-xl border p-3"
      style={{
        borderColor: "color-mix(in oklab, var(--sev-medium) 40%, transparent)",
        background: "color-mix(in oklab, var(--sev-medium) 7%, transparent)",
      }}
    >
      <div className="eyebrow mb-1" style={{ color: "var(--sev-medium)" }}>
        Approval required
      </div>
      <div className="mono text-[12px] font-semibold">{pending.tool}</div>
      <p className="mt-1.5 text-[12px] leading-relaxed text-[var(--ink-2)]">
        {pending.consequence}
      </p>
      <pre className="mono mt-2 overflow-x-auto rounded-lg border p-2 text-[10.5px] text-[var(--ink-3)]"
           style={{ borderColor: "var(--line)", background: "var(--surface)" }}>
        {JSON.stringify(pending.arguments, null, 2)}
      </pre>
      <div className="mt-2.5 flex gap-2">
        <button
          onClick={() => onDecide(true)}
          className="flex-1 rounded-lg px-3 py-1.5 text-[12.5px] font-semibold text-white"
          style={{ background: "var(--accent)" }}
        >
          Approve
        </button>
        <button
          onClick={() => onDecide(false)}
          className="flex-1 rounded-lg border px-3 py-1.5 text-[12.5px] font-medium"
          style={{ borderColor: "var(--line)" }}
        >
          Decline
        </button>
      </div>
      <p className="mt-2 text-[10.5px] leading-relaxed text-[var(--ink-4)]">
        KESTREL cannot execute this itself. Your decision is recorded in the
        tamper-evident audit ledger either way.
      </p>
    </motion.div>
  );
}

/** What the operator is currently looking at, so "this" and "that" resolve. */
function currentSelection(): Record<string, unknown> {
  if (typeof window === "undefined") return {};
  const path = window.location.pathname;
  const params = new URLSearchParams(window.location.search);
  return {
    view: path.replace(/^\//, "") || "landing",
    entity_id: params.get("entity") ?? undefined,
    alert_id: params.get("alert") ?? undefined,
    site_id: path.startsWith("/site/") ? path.split("/")[2] : undefined,
  };
}
