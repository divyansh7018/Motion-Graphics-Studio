"""Media inspection (Stage E).

Everything downstream - audio mixing, subtitle timing, QC - needs to know the
*real* facts about a media file: its duration, resolution, frame rate, codecs,
sample rate and channel count.  Nothing here guesses and nothing is derived from
the project's intentions; the file itself is asked.

Two sources, in order of preference:

1. **FFprobe**, when it is installed, as structured JSON.
2. **``ffmpeg -i``**, which every FFmpeg install has and which prints the same
   facts on stderr.  Parsing it is a genuine fallback, not a substitute for
   verification: the numbers come from the decoder, and the result records which
   source produced them so a report can say so.

If neither can read the file, that is reported as an error - never as a zero
duration that would silently shorten a render.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "MediaInfo",
    "StreamInfo",
    "probe_media",
    "parse_ffmpeg_info",
    "format_timecode",
    "FFPROBE_SOURCE",
    "FFMPEG_FALLBACK_SOURCE",
    "FFPROBE_FALLBACK_LABEL",
]

#: ``MediaInfo.source`` when the real FFprobe answered.
FFPROBE_SOURCE = "ffprobe"
#: ``MediaInfo.source`` when FFprobe was absent and ``ffmpeg -i`` was parsed.
FFMPEG_FALLBACK_SOURCE = "ffmpeg -i"
#: The only wording allowed for the fallback in a user-facing report.
FFPROBE_FALLBACK_LABEL = "FFprobe fallback / limited probe (ffmpeg -i)"


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #\d+:\d+.*?Video:\s*([A-Za-z0-9_]+)")
_AUDIO_RE = re.compile(r"Stream #\d+:\d+.*?Audio:\s*([A-Za-z0-9_]+)")
_SIZE_RE = re.compile(r"\b(\d{2,5})x(\d{2,5})\b")
_FPS_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s+fps\b")
_TBR_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s+tbr\b")
_PIXFMT_RE = re.compile(r"\b(yuv[0-9a-z]+|rgb[0-9a-z]+|bgr[0-9a-z]+|nv12|nv21|gray[0-9a-z]*)\b")
_RATE_RE = re.compile(r"\b(\d{4,6})\s+Hz\b")
_CHANNELS_RE = re.compile(r"\b(mono|stereo|quad|5\.1|7\.1|\d+\s+channels)\b")
_BITRATE_RE = re.compile(r"bitrate:\s*(\d+)\s+kb/s")


@dataclass
class StreamInfo:
    """One stream inside a media file."""

    index: int = 0
    kind: str = ""            # "video" | "audio" | "subtitle" | "data"
    codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    pixel_format: str = ""
    sample_rate: int = 0
    channels: int = 0
    bitrate_kbps: int = 0
    duration: float = 0.0
    raw: dict = field(default_factory=dict)

    def describe(self) -> str:
        if self.kind == "video":
            return f"{self.codec} {self.width}x{self.height}@{self.fps:g}fps {self.pixel_format}".strip()
        if self.kind == "audio":
            return f"{self.codec} {self.sample_rate}Hz {self.channels}ch".strip()
        return f"{self.kind} {self.codec}".strip()


@dataclass
class MediaInfo:
    """What a media file actually contains."""

    path: str = ""
    ok: bool = False
    #: Plain-language reason when the file could not be read.
    error: str = ""
    #: Which source produced these facts: "ffprobe" or "ffmpeg -i".
    source: str = ""
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    pixel_format: str = ""
    audio_codec: str = ""
    sample_rate: int = 0
    channels: int = 0
    bitrate_kbps: int = 0
    size_bytes: int = 0
    streams: list = field(default_factory=list)

    # -- conveniences ----------------------------------------------------

    @property
    def has_video(self) -> bool:
        return any(stream.kind == "video" for stream in self.streams)

    @property
    def has_audio(self) -> bool:
        return any(stream.kind == "audio" for stream in self.streams)

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}" if self.width and self.height else ""

    @property
    def aspect_ratio(self) -> float:
        return (self.width / self.height) if self.height else 0.0

    def stream_duration(self, kind: str) -> float:
        """The duration FFmpeg reported for one kind of stream.

        The container duration and a stream's own duration are different
        numbers: a video whose audio track is shorter than its picture track has
        one container duration but two stream durations.  Comparing the
        container duration with itself would never find that, so the real check
        asks for each stream separately (Stage E directive section 9).
        """
        longest = 0.0
        for stream in self.streams:
            if stream.kind == kind and float(stream.duration or 0.0) > longest:
                longest = float(stream.duration or 0.0)
        return longest

    def duration_label(self) -> str:
        return format_timecode(self.duration)

    @property
    def used_ffprobe(self) -> bool:
        """Whether the real FFprobe produced these facts.

        This is deliberately a separate question from "could the file be read".
        A file can be read perfectly well by the ``ffmpeg -i`` fallback while
        FFprobe is missing, and a report must never describe that as FFprobe
        verification (Stage E directive section 2).
        """
        return self.source == FFPROBE_SOURCE

    @property
    def source_label(self) -> str:
        """How the facts were obtained, in words a report can print verbatim."""
        if self.source == FFPROBE_SOURCE:
            return "FFprobe"
        if self.source == FFMPEG_FALLBACK_SOURCE:
            return "FFprobe fallback / limited probe (ffmpeg -i)"
        return self.source or "not probed"

    def summary(self) -> str:
        if not self.ok:
            return self.error or "could not be read"
        parts = [format_timecode(self.duration)]
        if self.has_video:
            parts.append(f"{self.resolution} {self.video_codec} {self.fps:g}fps")
        if self.has_audio:
            parts.append(f"{self.audio_codec} {self.sample_rate}Hz {self.channels}ch")
        return " ".join(part for part in parts if part)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "ok": self.ok,
            "error": self.error,
            "source": self.source,
            "duration": round(self.duration, 3),
            "width": self.width,
            "height": self.height,
            "fps": round(self.fps, 3),
            "video_codec": self.video_codec,
            "pixel_format": self.pixel_format,
            "audio_codec": self.audio_codec,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "bitrate_kbps": self.bitrate_kbps,
            "size_bytes": self.size_bytes,
            "streams": [stream.__dict__ for stream in self.streams],
        }


def format_timecode(seconds: float) -> str:
    """``1:02:03.50`` - readable however long the media is."""
    value = max(0.0, float(seconds or 0.0))
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    secs = value % 60
    return f"{hours:d}:{minutes:02d}:{secs:05.2f}"


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def parse_ffmpeg_info(text: str, *, path: str = "", size_bytes: int = 0) -> MediaInfo:
    """Read the facts FFmpeg prints for ``-i`` (its stderr banner).

    This is the fallback used when FFprobe is not installed.  It reads real
    decoder output, so the numbers are genuine; only the presentation differs.
    """
    info = MediaInfo(path=path, source=FFMPEG_FALLBACK_SOURCE, size_bytes=size_bytes)

    duration = _DURATION_RE.search(text)
    if duration:
        hours, minutes, secs = duration.groups()
        info.duration = int(hours) * 3600 + int(minutes) * 60 + float(secs)
    bitrate = _BITRATE_RE.search(text)
    if bitrate:
        info.bitrate_kbps = int(bitrate.group(1))

    index = 0
    for line in text.splitlines():
        if "Stream #" not in line:
            continue
        video = _VIDEO_RE.search(line)
        audio = _AUDIO_RE.search(line)
        if video:
            stream = StreamInfo(index=index, kind="video", codec=video.group(1))
            size = _SIZE_RE.search(line)
            if size:
                stream.width, stream.height = int(size.group(1)), int(size.group(2))
            fps = _FPS_RE.search(line) or _TBR_RE.search(line)
            if fps:
                stream.fps = float(fps.group(1))
            pixfmt = _PIXFMT_RE.search(line)
            if pixfmt:
                stream.pixel_format = pixfmt.group(1)
            info.streams.append(stream)
        elif audio:
            stream = StreamInfo(index=index, kind="audio", codec=audio.group(1))
            rate = _RATE_RE.search(line)
            if rate:
                stream.sample_rate = int(rate.group(1))
            channels = _CHANNELS_RE.search(line)
            if channels:
                stream.channels = _channel_count(channels.group(1))
            info.streams.append(stream)
        elif "Subtitle:" in line:
            info.streams.append(StreamInfo(index=index, kind="subtitle"))
        else:
            continue
        index += 1

    for stream in info.streams:
        if stream.kind == "video":
            info.width, info.height = stream.width, stream.height
            info.fps, info.video_codec = stream.fps, stream.codec
            info.pixel_format = stream.pixel_format
        elif stream.kind == "audio":
            info.sample_rate, info.channels = stream.sample_rate, stream.channels
            info.audio_codec = stream.codec

    info.ok = bool(info.streams)
    if not info.ok:
        info.error = "FFmpeg could not read this file (no streams reported)."
    return info


def _channel_count(text: str) -> int:
    mapping = {"mono": 1, "stereo": 2, "quad": 4, "5.1": 6, "7.1": 8}
    if text in mapping:
        return mapping[text]
    digits = re.search(r"(\d+)\s+channels", text)
    return int(digits.group(1)) if digits else 0


def _info_from_ffprobe(payload: dict, *, path: str = "", size_bytes: int = 0) -> MediaInfo:
    info = MediaInfo(path=path, source=FFPROBE_SOURCE, size_bytes=size_bytes, ok=True)
    fmt = payload.get("format") or {}
    try:
        info.duration = float(fmt.get("duration", 0.0) or 0.0)
    except (TypeError, ValueError):
        info.duration = 0.0
    try:
        info.bitrate_kbps = int(round(float(fmt.get("bit_rate", 0) or 0) / 1000))
    except (TypeError, ValueError):
        info.bitrate_kbps = 0

    for index, raw in enumerate(payload.get("streams") or []):
        codec_type = str(raw.get("codec_type", ""))
        stream = StreamInfo(
            index=index, kind=codec_type, codec=str(raw.get("codec_name", "") or ""),
            width=int(raw.get("width", 0) or 0), height=int(raw.get("height", 0) or 0),
            pixel_format=str(raw.get("pix_fmt", "") or ""),
            sample_rate=int(raw.get("sample_rate", 0) or 0),
            channels=int(raw.get("channels", 0) or 0),
            raw=dict(raw),
        )
        stream.fps = _ffprobe_fps(raw)
        try:
            stream.duration = float(raw.get("duration", 0.0) or 0.0)
        except (TypeError, ValueError):
            stream.duration = 0.0
        try:
            stream.bitrate_kbps = int(round(float(raw.get("bit_rate", 0) or 0) / 1000))
        except (TypeError, ValueError):
            stream.bitrate_kbps = 0
        info.streams.append(stream)
        if codec_type == "video":
            info.width, info.height = stream.width, stream.height
            info.fps, info.video_codec = stream.fps, stream.codec
            info.pixel_format = stream.pixel_format
        elif codec_type == "audio":
            info.sample_rate, info.channels = stream.sample_rate, stream.channels
            info.audio_codec = stream.codec

    info.ok = bool(info.streams)
    if not info.ok:
        info.error = "FFprobe reported no streams for this file."
    return info


def _ffprobe_fps(raw: dict) -> float:
    """FFprobe gives frame rate as a rational string such as ``30000/1001``."""
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = str(raw.get(key, "") or "")
        if not value or value in ("0/0", "N/A"):
            continue
        try:
            if "/" in value:
                numerator, denominator = value.split("/", 1)
                if float(denominator):
                    return round(float(numerator) / float(denominator), 3)
            return round(float(value), 3)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
    return 0.0


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------

def probe_media(path: Any, tools: Any = None, *, timeout: float = 60.0) -> MediaInfo:
    """Ask a media file what it really contains.

    ``tools`` is an :class:`app.tools.ffmpeg.FFmpegTools`; when it is omitted (or
    has no FFmpeg) the result reports why rather than raising, so a QC report can
    explain the gap instead of crashing.
    """
    target = Path(path)
    resolved = str(target)
    if not target.is_file():
        return MediaInfo(path=resolved, ok=False, error=f"The file was not found: {resolved}")
    try:
        size = target.stat().st_size
    except OSError:
        size = 0
    if size == 0:
        return MediaInfo(path=resolved, ok=False, error="The file is empty (0 bytes).", size_bytes=0)

    if tools is None:
        return MediaInfo(path=resolved, ok=False,
                         error="No FFmpeg tools were supplied, so the file could not be inspected.",
                         size_bytes=size)
    if not getattr(tools.discovery, "has_ffmpeg", False):
        return MediaInfo(path=resolved, ok=False,
                         error="FFmpeg is not available, so the file could not be inspected.",
                         size_bytes=size)

    # 1. FFprobe, when present: structured and unambiguous.
    if getattr(tools.discovery, "has_ffprobe", False):
        payload, error = tools.probe_json(["-show_format", "-show_streams", resolved], timeout=timeout)
        if payload is not None:
            return _info_from_ffprobe(payload, path=resolved, size_bytes=size)
        # Fall through to ffmpeg -i rather than giving up on a valid file.
        fallback_error = error
    else:
        fallback_error = "FFprobe is not installed; read with 'ffmpeg -i' instead."

    # 2. ``ffmpeg -i`` prints the same facts on stderr for any readable file.
    from app.tools.ffmpeg import run_capture

    result = run_capture([str(tools.ffmpeg), "-hide_banner", "-i", resolved], timeout=timeout)
    # ``ffmpeg -i`` with no output file exits non-zero *after* printing the
    # banner, so the banner - not the exit code - is what decides success.
    text = (result.stderr or "") + (result.stdout or "")
    info = parse_ffmpeg_info(text, path=resolved, size_bytes=size)
    if not info.ok and fallback_error:
        info.error = f"{info.error} ({fallback_error})"
    return info
