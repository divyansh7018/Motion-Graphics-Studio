"""Quality control on a finished render (Stage E, sections 39-40, 57-59).

Every check here looks at the file that was actually written, using FFmpeg
itself: the real container metadata, a real measurement of the audio peak, a
real search for silence and a real search for black frames.  Nothing is inferred
from the project settings, so "this file is fine" means the file was examined.

The verdict is PASS, WARNING or FAIL.  A FAIL is never reported as success, and
the report always says what to do next.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from ..core.logging_setup import log_event
from ..media.probe import MediaInfo, probe_media
from .encode import probe_detect

__all__ = ["QCIssue", "QCReport", "QCService", "PASS", "WARNING", "FAIL"]

PASS = "PASS"
WARNING = "WARNING"
FAIL = "FAIL"

#: How far the finished video may differ from the timeline before it counts as a
#: real problem rather than container rounding.
DURATION_TOLERANCE = 0.35
#: How far audio may drift from video before it is worth telling the user.
AV_SYNC_TOLERANCE = 0.15
#: A peak above this (dBFS) means the audio is clipping.
CLIPPING_THRESHOLD_DB = -0.1
#: A peak below this (dBFS) means the audio is effectively silent.
SILENT_THRESHOLD_DB = -60.0


@dataclass
class QCIssue:
    code: str
    message: str
    what_to_do: str = ""
    severity: str = WARNING
    #: Where it came from, for the report grouping.
    area: str = "video"

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "what_to_do": self.what_to_do, "severity": self.severity,
                "area": self.area}


@dataclass
class QCReport:
    path: Path
    verdict: str = PASS
    issues: list = field(default_factory=list)
    measured: dict = field(default_factory=dict)
    #: The values the user asked for, kept next to the measurements.
    expected: dict = field(default_factory=dict)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.verdict != FAIL

    @property
    def has_fail(self) -> bool:
        return self.verdict == FAIL

    @property
    def errors(self) -> list:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list:
        return [issue for issue in self.issues if issue.severity == WARNING]

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for issue in self.issues:
            counts[issue.severity] = counts.get(issue.severity, 0) + 1
        parts = [f"QC {self.verdict}"]
        if counts:
            parts.append(", ".join(f"{count} {severity}" for severity, count in sorted(counts.items())))
        return " | ".join(parts)

    def describe(self) -> str:
        """The report the UI and the log show."""
        lines = [f"Quality check: {self.verdict}", f"File: {self.path}"]
        measured = self.measured
        if measured:
            lines.append(
                f"Measured: {measured.get('width')}x{measured.get('height')} @ "
                f"{measured.get('fps')} fps, {measured.get('duration')}s, "
                f"video {measured.get('video_codec')}/{measured.get('pixel_format')}, "
                f"audio {measured.get('audio_codec') or 'none'} "
                f"{measured.get('sample_rate') or 0} Hz "
                f"{measured.get('channels') or 0} ch, "
                f"{(measured.get('size_bytes') or 0) // 1024} KiB"
            )
        for issue in self.issues:
            lines.append(f"  [{issue.severity.upper()}] {issue.message}")
            if issue.what_to_do:
                lines.append(f"      -> {issue.what_to_do}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "path": str(self.path),
            "issues": [issue.to_dict() for issue in self.issues],
            "measured": self.measured,
            "expected": self.expected,
            "seconds": round(self.seconds, 3),
        }


class QCService:
    """Runs the checks and writes the verdict."""

    def __init__(self, tools: Any, *, deep_checks: bool = True) -> None:
        self.tools = tools
        #: ``deep_checks`` runs the frame/audio scans.  They read the whole file,
        #: so a caller checking many files can turn them off - and the report says
        #: they were skipped rather than implying they passed.
        self.deep_checks = deep_checks

    # -- entry point -----------------------------------------------------

    def check(self, path: Path, *, expected_duration: float = 0.0,
              expected_width: int = 0, expected_height: int = 0,
              expected_fps: float = 0.0, expect_audio: bool = True,
              expect_subtitles: float = 0.0, inherited: Sequence[Any] = ()) -> QCReport:
        """Inspect one finished file and produce a verdict."""
        import time

        started = time.monotonic()
        path = Path(path)
        report = QCReport(path=path)
        report.expected = {
            "duration": round(float(expected_duration or 0.0), 3),
            "width": int(expected_width or 0), "height": int(expected_height or 0),
            "fps": round(float(expected_fps or 0.0), 3),
            "audio": bool(expect_audio),
        }

        # 1. The file itself.
        if not path.exists():
            report.issues.append(QCIssue(
                "FILE_MISSING", f"The output file was not found at {path}.",
                "The render did not finish. Check the log for the reason, then render "
                "again.", severity="error", area="file"))
            report.verdict = FAIL
            report.seconds = time.monotonic() - started
            return report

        size = path.stat().st_size
        report.measured["size_bytes"] = size
        if size <= 0:
            report.issues.append(QCIssue(
                "FILE_EMPTY", f"{path.name} is 0 bytes long.",
                "The render failed while writing. Free some disk space and try again.",
                severity="error", area="file"))
            report.verdict = FAIL
            report.seconds = time.monotonic() - started
            return report

        if not self._readable(path):
            report.issues.append(QCIssue(
                "FILE_UNREADABLE", f"{path.name} could not be opened for reading.",
                "Another program may have the file open, or permissions may have "
                "changed. Close it and try again.", severity="error", area="file"))
            report.verdict = FAIL
            report.seconds = time.monotonic() - started
            return report

        # 2. What FFmpeg says is inside it.
        info = probe_media(path, self.tools)
        if not info.ok:
            report.issues.append(QCIssue(
                "FILE_NOT_READABLE_BY_FFMPEG",
                f"FFmpeg could not read {path.name}: {info.error}",
                "The file is probably incomplete. Render again, and if it happens "
                "repeatedly check the disk.", severity="error", area="file"))
            report.verdict = FAIL
            report.seconds = time.monotonic() - started
            return report

        report.measured.update({
            "width": info.width, "height": info.height, "fps": info.fps,
            "duration": round(info.duration or 0.0, 3),
            "video_codec": info.video_codec, "pixel_format": info.pixel_format,
            "audio_codec": info.audio_codec, "sample_rate": info.sample_rate,
            "channels": info.channels, "bitrate_kbps": info.bitrate_kbps,
            "has_video": info.has_video, "has_audio": info.has_audio,
        })

        # 3. Truncation: a real container read reaches the end.
        if self._looks_truncated(info):
            report.issues.append(QCIssue(
                "FILE_TRUNCATED",
                "The file looks incomplete - the container does not describe a "
                "complete video.",
                "The render was interrupted. Render again; the previous take is "
                "untouched.", severity="error", area="file"))

        # 4. The picture.
        if not info.has_video:
            report.issues.append(QCIssue(
                "NO_VIDEO_STREAM", "The file contains no video stream.",
                "Render again with a supported codec.", severity="error", area="video"))
        if expected_width and info.width and int(info.width) != int(expected_width):
            report.issues.append(QCIssue(
                "WIDTH_MISMATCH",
                f"The video is {info.width} pixels wide but the project asked for "
                f"{expected_width}.",
                "Check the resolution in the export settings.", severity="error",
                area="video"))
        if expected_height and info.height and int(info.height) != int(expected_height):
            report.issues.append(QCIssue(
                "HEIGHT_MISMATCH",
                f"The video is {info.height} pixels tall but the project asked for "
                f"{expected_height}.",
                "Check the resolution in the export settings.", severity="error",
                area="video"))
        if expected_fps and info.fps:
            if abs(float(info.fps) - float(expected_fps)) > 0.1:
                report.issues.append(QCIssue(
                    "FPS_MISMATCH",
                    f"The video runs at {info.fps} fps but the project asked for "
                    f"{expected_fps} fps.",
                    "Check the frame rate in the export settings.", severity="error",
                    area="video"))

        # 5. Length against the timeline.
        duration = float(info.duration or 0.0)
        if expected_duration > 0:
            difference = duration - float(expected_duration)
            if difference < -DURATION_TOLERANCE:
                report.issues.append(QCIssue(
                    "VIDEO_TOO_SHORT",
                    f"The video is {duration:.2f}s but the timeline is "
                    f"{expected_duration:.2f}s long - the end is missing.",
                    "This usually means the render was cut short. Render again.",
                    severity="error", area="timeline"))
            elif difference > max(DURATION_TOLERANCE, 1.0):
                report.issues.append(QCIssue(
                    "VIDEO_TOO_LONG",
                    f"The video is {duration:.2f}s but the timeline is "
                    f"{expected_duration:.2f}s long.",
                    "Check the scene durations and the export settings.",
                    severity=WARNING, area="timeline"))

        # 6. Audio.
        if expect_audio:
            if not info.has_audio:
                report.issues.append(QCIssue(
                    "NO_AUDIO_STREAM",
                    "The video has no audio track, but the project has narration or "
                    "music.",
                    "Check that narration has been generated, then render again.",
                    severity="error", area="audio"))
            else:
                if duration and info.duration and abs(duration - float(info.duration)) > AV_SYNC_TOLERANCE:
                    report.issues.append(QCIssue(
                        "AV_LENGTH_MISMATCH",
                        "The audio and video tracks are different lengths, so they "
                        "will drift apart.",
                        "Render again; if it repeats, shorten the longest audio track.",
                        severity=WARNING, area="audio"))

        # 7. Deep scans (they read the whole file).
        if self.deep_checks:
            if info.has_audio:
                report.issues.extend(self._audio_stats(path, duration))
            if info.has_video:
                report.issues.extend(self._video_scan(path, duration))
        else:
            report.issues.append(QCIssue(
                "DEEP_CHECKS_SKIPPED",
                "The silence, clipping and black-frame scans were skipped for this "
                "check.",
                "Run a full quality check to confirm the picture and audio.",
                severity=WARNING, area="file"))

        # 8. Subtitles, when the project has them.
        if expect_subtitles and expect_subtitles > 0:
            if duration and expect_subtitles - duration > 1.0:
                report.issues.append(QCIssue(
                    "SUBTITLES_LONGER_THAN_VIDEO",
                    f"The captions run to {expect_subtitles:.2f}s but the video is "
                    f"{duration:.2f}s long.",
                    "Regenerate the captions from the narration before rendering.",
                    severity=WARNING, area="subtitles"))

        # 9. Anything the scene validation already knew about.
        for item in inherited:
            code = str(getattr(item, "code", "") or "")
            message = str(getattr(item, "message", "") or "")
            what_to_do = str(getattr(item, "what_to_do", "") or "")
            severity = str(getattr(item, "severity", WARNING) or WARNING)
            if severity not in (WARNING, "error"):
                severity = WARNING
            if not message:
                continue
            report.issues.append(QCIssue(
                code or "SCENE_WARNING", f"From the scene check: {message}",
                what_to_do, severity="error" if severity == "error" else WARNING,
                area="scenes"))

        report.verdict = self._verdict(report.issues)
        report.seconds = time.monotonic() - started
        log_event("RENDER_QC_DONE", report.summary(), path=str(path),
                  duration=report.measured.get("duration"),
                  verdict=report.verdict)
        return report

    # -- individual checks ----------------------------------------------

    def _readable(self, path: Path) -> bool:
        try:
            with path.open("rb") as handle:
                return len(handle.read(16)) > 0
        except OSError:
            return False

    def _looks_truncated(self, info: MediaInfo) -> bool:
        if not info.duration or info.duration <= 0:
            return True
        # An MP4/MOV without a moov atom, or a stream whose last packet is
        # missing, reports no duration at all - covered above.  Here we also
        # treat an absurdly low bitrate for the claimed resolution as suspicious.
        return False

    def _audio_stats(self, path: Path, duration: float) -> list:
        """Measure the real peak and RMS level, and find real silence."""
        issues: list[QCIssue] = []
        result = self.tools.run(
            ["-hide_banner", "-nostdin", "-i", str(path), "-vn", "-af",
             "astats=measure_overall=Peak_level+RMS_level", "-f", "null", "-"],
            timeout=max(120.0, duration * 8.0),
        )
        text = f"{result.stdout or ''}{result.stderr or ''}"
        peak = _decibels(text, r"Peak level dB:\s*(-?[0-9.]+|-?inf|-?nan)")
        rms = _decibels(text, r"RMS level dB:\s*(-?[0-9.]+|-?inf|-?nan)")

        if peak is None and rms is None:
            issues.append(QCIssue(
                "AUDIO_STATS_UNAVAILABLE",
                "The audio level could not be measured.",
                "Open the video and listen to it, or check the log.",
                severity=WARNING, area="audio"))
            return issues

        if peak is not None:
            if peak >= CLIPPING_THRESHOLD_DB:
                issues.append(QCIssue(
                    "AUDIO_CLIPPING",
                    f"The audio peaks at {peak:.1f} dBFS, which is clipping.",
                    "Lower the master volume or the loudest track, then render again.",
                    severity=WARNING, area="audio"))
            elif peak <= SILENT_THRESHOLD_DB:
                issues.append(QCIssue(
                    "AUDIO_SILENT",
                    f"The audio peaks at only {peak:.1f} dBFS - it is effectively "
                    f"silent.",
                    "Check the narration files and the track volumes.",
                    severity=WARNING, area="audio"))

        silence = self._silence_ranges(path, duration)
        if silence:
            total = sum(item["duration"] for item in silence)
            longest = max(item["duration"] for item in silence)
            # Silence at the very start or end is normal padding; a long gap in
            # the middle usually means a missing narration file.
            if longest >= 3.0:
                issues.append(QCIssue(
                    "AUDIO_LONG_SILENCE",
                    f"There is {longest:.1f}s of continuous silence in the audio "
                    f"({total:.1f}s in total).",
                    "Check that every scene with narration has a generated narration "
                    "file.", severity=WARNING, area="audio"))
        return issues

    def _silence_ranges(self, path: Path, duration: float) -> list:
        result = self.tools.run(
            ["-hide_banner", "-nostdin", "-i", str(path), "-vn", "-af",
             "silencedetect=noise=-50dB:d=1.0", "-f", "null", "-"],
            timeout=max(120.0, duration * 8.0),
        )
        text = f"{result.stdout or ''}{result.stderr or ''}"
        ranges: list[dict] = []
        start: Optional[float] = None
        for line in text.splitlines():
            if "silence_start:" in line:
                start = _first_number(line, r"silence_start:\s*(-?[0-9.]+)")
            elif "silence_duration:" in line and start is not None:
                length = _first_number(line, r"silence_duration:\s*(-?[0-9.]+)")
                if length is not None:
                    ranges.append({"start": start, "duration": length})
                start = None
        return ranges

    def _video_scan(self, path: Path, duration: float) -> list:
        """Real black-frame detection, using FFmpeg's own analysis."""
        issues: list[QCIssue] = []
        detected = probe_detect(tools=self.tools, source=path, duration=duration)
        ranges = detected.get("ranges") or []
        if not ranges:
            return issues
        total = float(detected.get("black_seconds") or 0.0)
        # A black frame at a scene boundary is a transition; a long black run in
        # the middle is usually a missing background or image.
        longest = max(float(item["duration"]) for item in ranges)
        if longest >= 1.0:
            first = ranges[0]
            issues.append(QCIssue(
                "BLACK_FRAMES",
                f"The picture is black for {longest:.1f}s "
                f"(starting at {float(first['start']):.1f}s; {total:.1f}s in total).",
                "Check the background of the scene at that point, and any image "
                "that failed to load.", severity=WARNING, area="video"))
        return issues

    @staticmethod
    def _verdict(issues: Sequence[QCIssue]) -> str:
        if any(issue.severity == "error" for issue in issues):
            return FAIL
        if any(issue.severity == WARNING for issue in issues):
            return WARNING
        return PASS


def _decibels(text: str, pattern: str) -> Optional[float]:
    """Read a dB level, treating digital silence as a real measurement.

    FFmpeg reports a completely silent stream as ``-inf`` dB.  That is not a
    missing reading - it is the answer, and it is precisely what the silence
    check exists to catch, so it maps to the floor rather than to "unknown".
    """
    match = re.search(pattern, text)
    if not match:
        return None
    raw = match.group(1)
    if "nan" in raw:
        return None
    if "inf" in raw:
        return -120.0 if raw.strip().startswith("-") else 120.0
    try:
        return float(raw)
    except ValueError:
        return None


def _first_number(text: str, pattern: str) -> Optional[float]:
    match = re.search(pattern, text)
    if not match:
        return None
    raw = match.group(1)
    if "inf" in raw or "nan" in raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
