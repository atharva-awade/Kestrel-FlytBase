"use client";

import { motion } from "framer-motion";
import { AlertCircle, ExternalLink, Network } from "lucide-react";
import { useEffect, useState } from "react";
import { Card, Pill, SectionTitle, Skeleton, fadeUp, stagger } from "@/components/ui/primitives";
import { Prose } from "@/components/Prose";
import { api } from "@/lib/api";
import { cn, titleCase } from "@/lib/format";

const TOPICS = [
  "overview", "cascade", "gate", "memory", "rules",
  "retrieval", "actions", "fleet", "models", "security",
];

/** The system explaining itself, using the same content Ask KESTREL serves, so a
    reviewer can either read it or interrogate it. */
export default function ArchitecturePage() {
  const [topic, setTopic] = useState("overview");
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    (async () => {
      setData(await api.architecture(topic));
      setLoading(false);
    })();
  }, [topic]);

  return (
    <div className="mx-auto max-w-[1200px] px-5 py-6">
      <motion.div {...fadeUp}>
        <SectionTitle
          eyebrow="Self-knowledge"
          title="Architecture"
          subtitle="KESTREL can explain its own design, including the parts that did not go to plan. Ask the same questions in the assistant and you get the same answers, grounded."
          right={
            <div className="flex gap-2">
              <a href="/flowchart.html" target="_blank" rel="noreferrer"
                 className="flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[12px] font-medium"
                 style={{ borderColor: "var(--line)" }}>
                Flowchart <ExternalLink size={11} />
              </a>
              <a href="/diagrams/system-architecture.svg" target="_blank" rel="noreferrer"
                 className="flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[12px] font-medium"
                 style={{ borderColor: "var(--line)" }}>
                Diagram <ExternalLink size={11} />
              </a>
            </div>
          }
        />
      </motion.div>

      <motion.div {...stagger(1)} className="mb-4 flex flex-wrap gap-1.5">
        {TOPICS.map((t) => (
          <button
            key={t}
            onClick={() => setTopic(t)}
            className={cn(
              "rounded-full border px-3 py-1.5 text-[12.5px] font-medium transition-colors",
              topic === t ? "text-[var(--accent-ink)]" : "text-[var(--ink-3)] hover:text-[var(--ink)]",
            )}
            style={{
              borderColor: topic === t ? "var(--accent)" : "var(--line)",
              background: topic === t ? "var(--accent-soft)" : undefined,
            }}
          >
            {titleCase(t)}
          </button>
        ))}
      </motion.div>

      <motion.div {...stagger(2)}>
        <Card className="p-5">
          {loading ? (
            <div className="space-y-2">{[0, 1, 2, 3, 4].map((i) => <Skeleton key={i} className="h-4" />)}</div>
          ) : (
            <Prose text={data?.explanation ?? ""} />
          )}
        </Card>
      </motion.div>

      {data?.limitations && (
        <motion.div {...stagger(3)} className="mt-4">
          <Card className="p-5"
                style={{ borderColor: "color-mix(in oklab, var(--sev-medium) 30%, transparent)" }}>
            <div className="mb-2 flex items-center gap-1.5">
              <AlertCircle size={14} style={{ color: "var(--sev-medium)" }} />
              <span className="text-[14px] font-semibold">Limitations</span>
              <Pill tone="warn" className="ml-auto">stated deliberately</Pill>
            </div>
            <Prose text={data.limitations} />
            <p className="mt-2 text-[11.5px] leading-relaxed text-[var(--ink-4)]">
              A system that overstates itself is harder to trust than one that says
              plainly where it is weak.
            </p>
          </Card>
        </motion.div>
      )}
    </div>
  );
}
