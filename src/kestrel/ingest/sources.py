"""Frame sources.

One protocol, three implementations, so nothing downstream knows or cares where a
frame came from. That indifference is the point: the demo switches source
mid-session and the rules, memory and agent layers behave identically.

    VideoFileSource   real MP4 → the hero path, real pixels through the real cascade
    ScriptedSource    text-described frames → the assignment's literal specification,
                      zero API cost, fully deterministic, and the way the reasoning
                      layers are tested without a VLM in the loop
    SyntheticSource   rendered frames → used only where a deterministic *image* is
                      needed (gate unit tests, CI) and no footage may be assumed

Frames carry a synthetic site clock rather than wall time, so a 40-second clip can
be presented as an overnight patrol and the time-of-day rules become exercisable.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

import cv2
import numpy as np
from PIL import Image, ImageDraw

from kestrel.domain import Frame, FrameSourceKind, Site
from kestrel.sim.telemetry import PatrolSimulator


# ═══════════════════════════════════════════════════════════════════════════════
def phash(image: np.ndarray, size: int = 8) -> str:
    """64-bit perceptual hash via DCT.

    Used by the tier-0 gate. Chosen over a raw histogram because it is stable
    under compression and lighting drift but still moves when the scene's
    structure changes — which is exactly the signal the gate wants.
    """
    if image.ndim == 3:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(image, (size * 4, size * 4), interpolation=cv2.INTER_AREA)
    dct = cv2.dct(np.float32(small))
    block = dct[:size, :size]
    med = np.median(block[1:, 1:])            # skip DC — it only tracks brightness
    bits = (block > med).flatten()
    return "".join("1" if b else "0" for b in bits)


def hamming(a: str | None, b: str | None) -> int:
    if not a or not b or len(a) != len(b):
        return 64
    return sum(c1 != c2 for c1, c2 in zip(a, b, strict=True))


# ═══════════════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class RawFrame:
    """A frame plus its pixels, before it becomes a persisted ``Frame``."""

    frame: Frame
    image: np.ndarray | None = None   # BGR, as OpenCV produces

    def jpeg(self, quality: int = 88) -> bytes | None:
        if self.image is None:
            return None
        ok, buf = cv2.imencode(".jpg", self.image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        return buf.tobytes() if ok else None


@runtime_checkable
class FrameSource(Protocol):
    kind: FrameSourceKind

    def __iter__(self) -> Iterator[RawFrame]: ...
    @property
    def total(self) -> int: ...


# ═══════════════════════════════════════════════════════════════════════════════
class VideoFileSource:
    """Real footage. The path the demo is recorded on.

    ``sample_fps`` decouples analysis rate from the file's native rate: security
    footage at 30 fps carries almost no new information per frame, and the gate
    would discard most of it anyway. Sampling at the source is cheaper than
    decoding frames only to throw them away.
    """

    kind = FrameSourceKind.VIDEO

    def __init__(
        self,
        path: Path,
        site: Site,
        *,
        start_clock: datetime,
        sample_fps: float = 2.0,
        clock_scale: float = 1.0,
        max_frames: int | None = None,
        telemetry: PatrolSimulator | None = None,
        resize_width: int | None = 960,
    ) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"footage not found: {self.path}")
        self.site = site
        self.start_clock = start_clock
        self.sample_fps = sample_fps
        # >1 makes the site clock advance faster than the video, so a short clip
        # can span a night. Reported in the UI so the compression is never hidden.
        self.clock_scale = clock_scale
        self.max_frames = max_frames
        self.resize_width = resize_width
        self.telemetry = telemetry or PatrolSimulator(site, start_clock)

        cap = cv2.VideoCapture(str(self.path))
        self.native_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        self.frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.duration_s = self.frame_count / self.native_fps if self.native_fps else 0.0
        cap.release()

    @property
    def total(self) -> int:
        n = int(self.duration_s * self.sample_fps) if self.duration_s else 0
        return min(n, self.max_frames) if self.max_frames else n

    def __iter__(self) -> Iterator[RawFrame]:
        cap = cv2.VideoCapture(str(self.path))
        stride = max(1, round(self.native_fps / self.sample_fps))
        idx = seq = 0
        try:
            while True:
                ok, img = cap.read()
                if not ok:
                    break
                if idx % stride:
                    idx += 1
                    continue
                if self.max_frames and seq >= self.max_frames:
                    break

                if self.resize_width and img.shape[1] > self.resize_width:
                    scale = self.resize_width / img.shape[1]
                    img = cv2.resize(
                        img,
                        (self.resize_width, int(img.shape[0] * scale)),
                        interpolation=cv2.INTER_AREA,
                    )

                video_t = idx / self.native_fps
                ts = self.start_clock + timedelta(seconds=video_t * self.clock_scale)
                h, w = img.shape[:2]
                yield RawFrame(
                    frame=Frame(
                        id=_frame_id(self.site.id, seq, ts),
                        site_id=self.site.id,
                        seq=seq,
                        ts=ts,
                        source=self.kind,
                        width=w,
                        height=h,
                        phash=phash(img),
                        telemetry=self.telemetry.at(ts),
                    ),
                    image=img,
                )
                seq += 1
                idx += 1
        finally:
            cap.release()


# ═══════════════════════════════════════════════════════════════════════════════
class ScriptedSource:
    """Text-described frames — the assignment's literal specification.

    "Simulate video frames with text descriptions (e.g. 'Frame 1: Blue truck at
    gate')". Kept as a first-class source for three reasons: it satisfies the brief
    exactly, it lets the rules/memory/agent layers be exercised at zero API cost and
    with perfect determinism, and switching to it mid-demo proves the pipeline is
    genuinely source-agnostic rather than merely claimed to be.
    """

    kind = FrameSourceKind.SCRIPTED

    def __init__(
        self,
        script: list[dict],
        site: Site,
        *,
        start_clock: datetime,
        seconds_per_frame: float = 30.0,
        telemetry: PatrolSimulator | None = None,
    ) -> None:
        self.script = script
        self.site = site
        self.start_clock = start_clock
        self.spf = seconds_per_frame
        self.telemetry = telemetry or PatrolSimulator(site, start_clock)

    @classmethod
    def from_file(cls, path: Path, site: Site, **kw) -> ScriptedSource:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data["frames"], site, **kw)

    @property
    def total(self) -> int:
        return len(self.script)

    def __iter__(self) -> Iterator[RawFrame]:
        for seq, entry in enumerate(self.script):
            # A scripted frame may pin its own clock time, which is how the
            # midnight-loitering scenario is expressed without waiting for midnight.
            if "at" in entry:
                ts = datetime.fromisoformat(entry["at"])
            else:
                ts = self.start_clock + timedelta(seconds=seq * self.spf)
            yield RawFrame(
                frame=Frame(
                    id=_frame_id(self.site.id, seq, ts),
                    site_id=self.site.id,
                    seq=seq,
                    ts=ts,
                    source=self.kind,
                    text=entry["text"],
                    phash=None,
                    telemetry=self.telemetry.at(ts),
                ),
                image=None,
            )


# ═══════════════════════════════════════════════════════════════════════════════
class SyntheticSource:
    """Deterministically rendered frames.

    Not a substitute for real footage — a VLM asked to caption a diagram will
    correctly tell you it is looking at a diagram. This exists narrowly so that
    tests which need *pixels* (the gate's phash and delta thresholds, the tracker's
    identity continuity) can run in CI without shipping a video file.
    """

    kind = FrameSourceKind.VIDEO

    def __init__(
        self,
        site: Site,
        *,
        start_clock: datetime,
        n: int = 60,
        seconds_per_frame: float = 1.0,
        size: tuple[int, int] = (640, 384),
        moving: bool = True,
        telemetry: PatrolSimulator | None = None,
    ) -> None:
        self.site = site
        self.start_clock = start_clock
        self.n = n
        self.spf = seconds_per_frame
        self.size = size
        self.moving = moving
        self.telemetry = telemetry or PatrolSimulator(site, start_clock)

    @property
    def total(self) -> int:
        return self.n

    def __iter__(self) -> Iterator[RawFrame]:
        w, h = self.size
        for seq in range(self.n):
            ts = self.start_clock + timedelta(seconds=seq * self.spf)
            im = Image.new("RGB", (w, h), (176, 186, 200))
            d = ImageDraw.Draw(im)
            d.rectangle([0, int(h * 0.62), w, h], fill=(112, 116, 124))
            if self.moving:
                # A vehicle crossing left to right, and a slower pedestrian.
                x = int((seq / max(1, self.n - 1)) * (w - 180))
                d.rectangle([x, int(h * 0.36), x + 150, int(h * 0.66)], fill=(28, 78, 168))
                d.ellipse([x + 18, int(h * 0.62), x + 52, int(h * 0.74)], fill=(20, 20, 24))
                d.ellipse([x + 104, int(h * 0.62), x + 138, int(h * 0.74)], fill=(20, 20, 24))
                px = int(w * 0.82 - (seq / max(1, self.n - 1)) * 90)
                d.ellipse([px, int(h * 0.52), px + 18, int(h * 0.58)], fill=(226, 200, 172))
                d.rectangle([px + 3, int(h * 0.58), px + 15, int(h * 0.72)], fill=(56, 64, 88))
            arr = cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR)
            yield RawFrame(
                frame=Frame(
                    id=_frame_id(self.site.id, seq, ts),
                    site_id=self.site.id,
                    seq=seq,
                    ts=ts,
                    source=self.kind,
                    width=w,
                    height=h,
                    phash=phash(arr),
                    telemetry=self.telemetry.at(ts),
                ),
                image=arr,
            )


# ═══════════════════════════════════════════════════════════════════════════════
def _frame_id(site_id: str, seq: int, ts: datetime) -> str:
    """Stable, content-independent frame id.

    Derived from site + sequence + timestamp so the same run always produces the
    same ids — which is what lets cassettes, golden files and evidence references
    survive a re-ingest.
    """
    h = hashlib.sha1(f"{site_id}:{seq}:{ts.isoformat()}".encode()).hexdigest()[:10]
    return f"frm_{site_id}_{seq:06d}_{h}"


def save_jpeg(image: np.ndarray, dest: Path, quality: int = 88) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(dest), image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return dest


def crop(image: np.ndarray, box: tuple[float, float, float, float], pad: float = 0.06) -> np.ndarray:
    """Crop a detection with a small margin.

    The margin matters for re-identification: a box cropped exactly to its edges
    loses the contextual pixels that make two sightings of the same vehicle look
    alike to an embedding model.
    """
    h, w = image.shape[:2]
    x1, y1, x2, y2 = box
    pw, ph = (x2 - x1) * pad, (y2 - y1) * pad
    x1 = int(max(0, x1 - pw))
    y1 = int(max(0, y1 - ph))
    x2 = int(min(w, x2 + pw))
    y2 = int(min(h, y2 + ph))
    if x2 <= x1 or y2 <= y1:
        return image
    return image[y1:y2, x1:x2]


def to_jpeg_bytes(image: np.ndarray, quality: int = 88) -> bytes:
    ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        pil = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
        b = io.BytesIO()
        pil.save(b, format="JPEG", quality=quality)
        return b.getvalue()
    return buf.tobytes()
