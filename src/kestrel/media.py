"""Make operator-supplied footage playable in a browser.

A video the *detector* can read and a video a *browser* can play are not the
same thing, and conflating them produced a bug that looked like a server fault
but was not one: uploads encoded as MPEG-4 Part 2 indexed perfectly (OpenCV
decodes them happily) and then failed in the `<video>` element with

    NotSupportedError: The element has no supported sources.

The HTTP layer was innocent -- the file was served with the right status, the
right length and the right content type. No browser ships an MPEG-4 Part 2
decoder, so there was nothing to decode it with.

Two further mismatches are handled here for the same reason:

  * **container vs extension.** Uploads are stored as `.mp4` whatever arrives,
    so a Matroska or AVI file would be served as MP4 and rejected.
  * **`moov` after `mdat`.** Muxers that cannot seek backwards write the index
    at the end. The file is valid, but a player cannot start until it has the
    index, which stalls playback and defeats seeking.

Everything runs through the ffmpeg binary that `imageio-ffmpeg` already ships
inside the virtualenv, so this adds no system dependency.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Codecs every current browser can decode from an MP4 container.
#
# HEVC is deliberately absent. Some Chrome and Safari builds play it with
# hardware support and others do not, and "works on the developer's laptop"
# is precisely the failure being fixed here, so it is always re-encoded.
BROWSER_SAFE_VIDEO = frozenset({"h264", "av1", "vp9", "vp8"})
BROWSER_SAFE_AUDIO = frozenset({"aac", "mp3", "opus", "vorbis", "flac"})

_STREAM = re.compile(
    r"Stream #\d+:\d+.*?: (?P<kind>Video|Audio): (?P<codec>[A-Za-z0-9_]+)", re.I
)
_DURATION = re.compile(r"Duration: (\d+):(\d\d):(\d\d\.\d+)")
_OUT_TIME = re.compile(r"out_time_us=(\d+)")


class MediaError(RuntimeError):
    """Raised when footage cannot be made playable, with a reason to show."""


@dataclass
class Probe:
    video: str = ""
    audio: str = ""
    duration_s: float = 0.0
    faststart: bool = False

    @property
    def playable(self) -> bool:
        """Playable *as stored*: decodable codec and an index a player can reach."""
        return bool(self.video) and self.video in BROWSER_SAFE_VIDEO and self.faststart


def ffmpeg_exe() -> str:
    """The ffmpeg binary, preferring the one vendored in the virtualenv."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - fall back to a system install
        found = shutil.which("ffmpeg")
        if not found:
            raise MediaError(
                "no ffmpeg available to convert this video; install ffmpeg or the "
                "imageio-ffmpeg package"
            ) from None
        return found


def _faststart(path: Path) -> bool:
    """True when `moov` precedes `mdat`, so a player can start before the end.

    Read from the raw boxes rather than asked of ffmpeg, because ffmpeg reports
    what a file *contains*, not the order it is laid out in.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(4 << 20)
    except OSError:
        return False
    moov, mdat = head.find(b"moov"), head.find(b"mdat")
    if moov == -1:
        return False          # index not in the first chunk at all
    return mdat == -1 or moov < mdat


async def probe(path: Path) -> Probe:
    """Identify the codecs in a file. Never raises for undecodable input."""
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_exe(), "-hide_banner", "-i", str(path),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    text = err.decode("utf-8", "replace")

    out = Probe(faststart=_faststart(path))
    for m in _STREAM.finditer(text):
        codec = m.group("codec").lower()
        if m.group("kind").lower() == "video" and not out.video:
            out.video = codec
        elif m.group("kind").lower() == "audio" and not out.audio:
            out.audio = codec
    if d := _DURATION.search(text):
        out.duration_s = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3))
    return out


async def _run(args: list[str], total_s: float,
               on_progress: Callable[[float], None] | None) -> None:
    """Run ffmpeg, translating its progress stream into a 0..1 fraction."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None
    while line := await proc.stdout.readline():
        if on_progress and total_s > 0:
            if m := _OUT_TIME.search(line.decode("utf-8", "replace")):
                on_progress(min(1.0, int(m.group(1)) / 1e6 / total_s))
    err = (await proc.stderr.read()).decode("utf-8", "replace") if proc.stderr else ""
    if await proc.wait() != 0:
        tail = " / ".join(ln.strip() for ln in err.strip().splitlines()[-3:])
        raise MediaError(f"conversion failed: {tail[:300]}")


async def ensure_browser_playable(
    path: Path, on_progress: Callable[[float], None] | None = None
) -> dict[str, object]:
    """Rewrite `path` in place so a browser can play it, if it cannot already.

    Returns a record of what was done, which is surfaced to the operator: a
    conversion changes the file they uploaded and should not happen invisibly.
    """
    before = await probe(path)
    if not before.video:
        raise MediaError("no video stream found in that file")

    if before.playable:
        return {"action": "kept", "video": before.video, "audio": before.audio,
                "reason": f"{before.video} already plays in a browser"}

    # A safe codec in the wrong layout only needs the boxes moved, which is a
    # stream copy: no re-encode, no generation loss, near-instant.
    remux_only = before.video in BROWSER_SAFE_VIDEO
    args = [ffmpeg_exe(), "-y", "-v", "error", "-nostats",
            "-progress", "pipe:1", "-i", str(path)]
    if remux_only:
        args += ["-c", "copy"]
        reason = f"{before.video} stream re-laid out so playback can start immediately"
    else:
        args += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            # libx264 rejects odd dimensions; phone footage is often 1 px off.
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        ]
        args += (["-c:a", "aac", "-b:a", "128k"] if before.audio else ["-an"])
        reason = f"{before.video} is not decodable in a browser, re-encoded to h264"

    tmp = path.with_suffix(".converting.mp4")
    try:
        await _run(args + ["-movflags", "+faststart", "-f", "mp4", str(tmp)],
                   before.duration_s, on_progress)
        after = await probe(tmp)
        if not after.playable:
            raise MediaError(
                f"converted file is still not playable (video={after.video or 'none'}, "
                f"faststart={after.faststart})"
            )
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)

    return {"action": "remuxed" if remux_only else "transcoded",
            "from": before.video, "video": "h264" if not remux_only else before.video,
            "audio": before.audio, "reason": reason}
