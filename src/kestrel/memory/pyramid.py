"""The temporal memory pyramid — context management for eight-hour shifts.

An eight-hour patrol at 2 fps is ~57,600 frames. Even at a terse 30 tokens each,
that is 1.7M tokens of raw observation: too much to reason over, too much to put in
a prompt, and mostly redundant. But an operator asking "what happened last night?"
expects an answer grounded in *all* of it, not in the last twenty frames.

The resolution is hierarchical compression. Each level summarises the one below
under a token budget, so detail is available when you drill in and absent when you
do not need it:

    L0  frame     one observation                    ~30 tokens each
    L1  clip      ~30 s of frames                    ~60 tokens
    L2  event     a bounded episode                  ~90 tokens
    L3  shift     a watch period                    ~250 tokens
    L4  day       the whole day                     ~400 tokens

Two properties matter more than the exact ratios:

*   **Salience-weighted.** Compression is not uniform. A shift where nothing
    happened compresses to a sentence; the four minutes around an intrusion keep
    their frame-level detail. Uniform summarisation would discard precisely the
    material an investigation needs.
*   **Navigable both ways.** Every node keeps its children, so an answer at L4 can
    be expanded down to the frames that support it. A summary you cannot audit is
    not evidence.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from kestrel.domain import Event, MemoryLevel, MemoryNode
from kestrel.obs.meter import Stage

# Target sizes per level. Not hard limits — a prompt asking for "about 60 tokens"
# lands close enough, and clipping mid-sentence would be worse than overshooting.
BUDGETS: dict[MemoryLevel, int] = {
    MemoryLevel.CLIP: 60,
    MemoryLevel.EVENT: 90,
    MemoryLevel.SHIFT: 250,
    MemoryLevel.DAY: 400,
}

CLIP_WINDOW = timedelta(seconds=30)
SHIFT_HOURS = 8


@dataclass
class FrameNote:
    """The L0 unit: one analysed frame, reduced to what is worth remembering."""

    frame_id: str
    ts: datetime
    caption: str
    labels: list[str]
    zones: list[str]
    entity_ids: list[str]
    salience: float = 0.5

    def as_line(self) -> str:
        who = ", ".join(self.labels[:4]) or "nothing"
        where = f" in {self.zones[0]}" if self.zones else ""
        return f"[{self.ts:%H:%M:%S}] {who}{where}: {self.caption[:130]}"


def _nid(site_id: str, level: MemoryLevel, start: datetime, end: datetime) -> str:
    h = hashlib.sha1(f"{site_id}{level}{start}{end}".encode()).hexdigest()[:8]
    return f"mem_{level.value}_{h}"


def salience_of(note: FrameNote, high_priority_zones: set[str]) -> float:
    """How much this frame deserves to survive compression.

    People outrank objects, high-priority zones outrank routine ones, and night
    outranks day — because those are the combinations an investigation cares about.
    """
    s = 0.3
    if any("person" in lab for lab in note.labels):
        s += 0.25
    if any(z in high_priority_zones for z in note.zones):
        s += 0.3
    if note.ts.hour >= 22 or note.ts.hour < 5:
        s += 0.15
    if len(note.labels) >= 3:
        s += 0.1
    return min(1.0, s)


class MemoryPyramid:
    """Builds and holds the hierarchy for one site."""

    def __init__(self, site_id: str, *, client=None, high_priority_zones: set[str] | None = None):
        self.site_id = site_id
        self.client = client
        self.high_priority_zones = high_priority_zones or set()
        self.notes: list[FrameNote] = []
        self.nodes: dict[MemoryLevel, list[MemoryNode]] = {lvl: [] for lvl in MemoryLevel}
        self.tokens_raw = 0
        self.tokens_compressed = 0

    # ── ingest ───────────────────────────────────────────────────────────
    def add_frame(self, note: FrameNote) -> None:
        note.salience = salience_of(note, self.high_priority_zones)
        self.notes.append(note)
        self.tokens_raw += max(1, len(note.as_line()) // 4)

    # ── build ────────────────────────────────────────────────────────────
    async def build(self) -> dict[MemoryLevel, list[MemoryNode]]:
        """Compress upward. Cheap to call repeatedly; rebuilds from the notes."""
        if not self.notes:
            return self.nodes
        self.nodes = {lvl: [] for lvl in MemoryLevel}
        self.tokens_compressed = 0

        clips = await self._build_clips()
        self.nodes[MemoryLevel.CLIP] = clips
        shifts = await self._build_shifts(clips)
        self.nodes[MemoryLevel.SHIFT] = shifts
        days = await self._build_days(shifts)
        self.nodes[MemoryLevel.DAY] = days
        return self.nodes

    async def _build_clips(self) -> list[MemoryNode]:
        buckets: list[list[FrameNote]] = []
        current: list[FrameNote] = []
        anchor: datetime | None = None
        for n in sorted(self.notes, key=lambda x: x.ts):
            if anchor is None or n.ts - anchor > CLIP_WINDOW:
                if current:
                    buckets.append(current)
                current, anchor = [n], n.ts
            else:
                current.append(n)
        if current:
            buckets.append(current)

        out: list[MemoryNode] = []
        for b in buckets:
            summary = await self._summarise_frames(b)
            node = MemoryNode(
                id=_nid(self.site_id, MemoryLevel.CLIP, b[0].ts, b[-1].ts),
                site_id=self.site_id,
                level=MemoryLevel.CLIP,
                start_ts=b[0].ts,
                end_ts=b[-1].ts,
                summary=summary,
                child_ids=[n.frame_id for n in b],
                entity_ids=sorted({e for n in b for e in n.entity_ids}),
                tokens=max(1, len(summary) // 4),
                salience=max(n.salience for n in b),
            )
            self.tokens_compressed += node.tokens
            out.append(node)
        return out

    async def _build_shifts(self, clips: list[MemoryNode]) -> list[MemoryNode]:
        if not clips:
            return []
        buckets: dict[tuple[str, int], list[MemoryNode]] = {}
        for c in clips:
            key = (c.start_ts.date().isoformat(), c.start_ts.hour // SHIFT_HOURS)
            buckets.setdefault(key, []).append(c)

        out: list[MemoryNode] = []
        for (_, _), group in sorted(buckets.items()):
            summary = await self._summarise_nodes(group, MemoryLevel.SHIFT)
            node = MemoryNode(
                id=_nid(self.site_id, MemoryLevel.SHIFT, group[0].start_ts, group[-1].end_ts),
                site_id=self.site_id,
                level=MemoryLevel.SHIFT,
                start_ts=group[0].start_ts,
                end_ts=group[-1].end_ts,
                summary=summary,
                child_ids=[c.id for c in group],
                entity_ids=sorted({e for c in group for e in c.entity_ids}),
                tokens=max(1, len(summary) // 4),
                salience=max(c.salience for c in group),
            )
            self.tokens_compressed += node.tokens
            out.append(node)
        return out

    async def _build_days(self, shifts: list[MemoryNode]) -> list[MemoryNode]:
        if not shifts:
            return []
        buckets: dict[str, list[MemoryNode]] = {}
        for s in shifts:
            buckets.setdefault(s.start_ts.date().isoformat(), []).append(s)

        out: list[MemoryNode] = []
        for _, group in sorted(buckets.items()):
            summary = await self._summarise_nodes(group, MemoryLevel.DAY)
            node = MemoryNode(
                id=_nid(self.site_id, MemoryLevel.DAY, group[0].start_ts, group[-1].end_ts),
                site_id=self.site_id,
                level=MemoryLevel.DAY,
                start_ts=group[0].start_ts,
                end_ts=group[-1].end_ts,
                summary=summary,
                child_ids=[s.id for s in group],
                entity_ids=sorted({e for s in group for e in s.entity_ids}),
                tokens=max(1, len(summary) // 4),
                salience=max(s.salience for s in group),
            )
            self.tokens_compressed += node.tokens
            out.append(node)
        return out

    # ── summarisation ────────────────────────────────────────────────────
    async def _summarise_frames(self, notes: list[FrameNote]) -> str:
        # A handful of frames does not need a model call — the frames themselves
        # are already about as short as a summary would be.
        if self.client is None or len(notes) <= 2:
            return self._extractive(notes)
        body = "\n".join(n.as_line() for n in notes[:40])
        try:
            return await self.client.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You compress security camera observations. Write ONE factual "
                            "sentence covering what happened across these frames. Name "
                            "objects, people and places. State only what the observations "
                            "state — never infer intent. No preamble."
                        ),
                    },
                    {"role": "user", "content": body},
                ],
                stage=Stage.REASON,
                max_tokens=BUDGETS[MemoryLevel.CLIP],
            )
        except Exception:
            return self._extractive(notes)

    async def _summarise_nodes(self, nodes: list[MemoryNode], level: MemoryLevel) -> str:
        if self.client is None or len(nodes) <= 1:
            return nodes[0].summary if nodes else ""
        # Salience ordering means that when the input is truncated, the material
        # that survives is the material that mattered.
        ordered = sorted(nodes, key=lambda n: -n.salience)[:30]
        body = "\n".join(
            f"[{n.start_ts:%H:%M}-{n.end_ts:%H:%M}] {n.summary}" for n in ordered
        )
        window = f"{nodes[0].start_ts:%Y-%m-%d %H:%M} to {nodes[-1].end_ts:%H:%M}"
        want = (
            "a shift report: what happened, who was involved, anything unusual"
            if level is MemoryLevel.SHIFT
            else "a daily site summary: patterns, recurring entities, and notable deviations"
        )
        try:
            return await self.client.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            f"You are a security analyst writing {want}. Be specific and "
                            "concrete: name entities, zones and times. Do not speculate "
                            "about motive. If the period was uneventful, say so in one "
                            "sentence rather than padding."
                        ),
                    },
                    {"role": "user", "content": f"Period: {window}\n\n{body}"},
                ],
                stage=Stage.REASON,
                max_tokens=BUDGETS[level],
            )
        except Exception:
            return "; ".join(n.summary[:80] for n in ordered[:4])

    @staticmethod
    def _extractive(notes: list[FrameNote]) -> str:
        """Model-free fallback. Never as good, always available."""
        if not notes:
            return ""
        labels: dict[str, int] = {}
        zones: set[str] = set()
        for n in notes:
            for lab in n.labels:
                labels[lab] = labels.get(lab, 0) + 1
            zones.update(n.zones)
        who = ", ".join(f"{v}x {k}" for k, v in sorted(labels.items(), key=lambda x: -x[1])[:3])
        where = f" in {', '.join(sorted(zones)[:2])}" if zones else ""
        span = f"{notes[0].ts:%H:%M:%S}-{notes[-1].ts:%H:%M:%S}"
        return f"{span}: {who or 'no detections'}{where}. {notes[0].caption[:110]}"

    # ── read-out ─────────────────────────────────────────────────────────
    def context_for(
        self, start: datetime | None = None, end: datetime | None = None,
        *, budget_tokens: int = 2000,
    ) -> str:
        """Assemble a prompt-sized view of a period.

        Coarse first, refining while budget remains. This is what the agent gets
        when it asks "what happened last night" — the whole period, at whatever
        resolution fits.
        """
        parts: list[str] = []
        used = 0
        for level in (MemoryLevel.DAY, MemoryLevel.SHIFT, MemoryLevel.CLIP):
            for n in self.nodes.get(level, []):
                if start and n.end_ts < start:
                    continue
                if end and n.start_ts > end:
                    continue
                line = f"[{level.value}] {n.start_ts:%d %b %H:%M}-{n.end_ts:%H:%M} {n.summary}"
                cost = max(1, len(line) // 4)
                if used + cost > budget_tokens:
                    return "\n".join(parts)
                parts.append(line)
                used += cost
        return "\n".join(parts)

    @property
    def stats(self) -> dict:
        ratio = (self.tokens_raw / self.tokens_compressed) if self.tokens_compressed else 0.0
        return {
            "frames": len(self.notes),
            "clips": len(self.nodes.get(MemoryLevel.CLIP, [])),
            "shifts": len(self.nodes.get(MemoryLevel.SHIFT, [])),
            "days": len(self.nodes.get(MemoryLevel.DAY, [])),
            "tokens_raw": self.tokens_raw,
            "tokens_compressed": self.tokens_compressed,
            "compression_ratio": round(ratio, 1),
        }


def events_from_notes(
    site_id: str, notes: list[FrameNote], *, gap: timedelta = timedelta(seconds=90)
) -> list[Event]:
    """Derive L2 events by grouping consecutive frames that share an entity.

    An "event" here is a bounded episode of one entity's presence — which is the
    unit dwell-time rules operate on, and the unit an operator means when they say
    "show me that incident".
    """

    by_entity: dict[str, list[FrameNote]] = {}
    for n in sorted(notes, key=lambda x: x.ts):
        for eid in n.entity_ids:
            by_entity.setdefault(eid, []).append(n)

    events: list[Event] = []
    for eid, group in by_entity.items():
        run: list[FrameNote] = []
        for n in group:
            if run and n.ts - run[-1].ts > gap:
                events.append(_event(site_id, eid, run))
                run = []
            run.append(n)
        if run:
            events.append(_event(site_id, eid, run))

    return sorted(events, key=lambda e: e.start_ts)


def _event(site_id: str, entity_id: str, run: list[FrameNote]) -> Event:
    from kestrel.domain import EventKind

    dur = (run[-1].ts - run[0].ts).total_seconds()
    kind = EventKind.DWELL if dur >= 60 else EventKind.APPEARANCE
    zones = [z for n in run for z in n.zones]
    zone = max(set(zones), key=zones.count) if zones else None
    return Event(
        id=_nid(site_id, MemoryLevel.EVENT, run[0].ts, run[-1].ts) + f"_{entity_id[-6:]}",
        site_id=site_id,
        kind=kind,
        entity_id=entity_id,
        zone_id=zone,
        start_ts=run[0].ts,
        end_ts=run[-1].ts,
        frame_ids=[n.frame_id for n in run],
        summary=(
            f"{entity_id} present in {zone or 'unresolved zone'} for {dur:.0f}s "
            f"across {len(run)} frames"
        ),
        salience=max(n.salience for n in run),
    )
