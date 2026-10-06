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
from ..media.probe import FFPROBE_FALLBACK_LABEL, MediaInfo, probe_media
from .encode import probe_detect

__all__ = [
    "QCIssue",
    "QCReport",
    "QCService",
    "PASS",
    "WARNING",
    "FAIL",
    "NOT_AVAILABLE",
]

PASS = "PASS"
WARNING = "WARNING"
FAIL = "FAIL"
#: A check that could not be run at all, because something it needs is missing.
#: This is deliberately *not* PASS: an unrun check never counts as a clean one,
#: and a report containing one can never come out as a bare PASS.
NOT_AVAILABLE = "CHECK NOT AVAILABLE"

#: The three states a single check can be in.  Nothing else is allowed, so a
#: report can never say something ambiguous about one check.
CHECK_STATES = (PASS, FAIL, NOT_AVAILABLE)

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
    #: One entry per check that was attempted: PASS, FAIL or CHECK NOT
    #: AVAILABLE.  A check that never ran is absent, never silently PASS.
    checks: dict = field(default_factory=dict)
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

    @property
    def unavailable(self) -> list:
        """The checks that could not be run, with the issues explaining why."""
        return [issue for issue in self.issues if issue.severity == NOT_AVAILABLE]

    @property
    def unavailable_checks(self) -> list:
        return sorted(name for name, state in self.checks.items() if state == NOT_AVAILABLE)

    @property
    def complete(self) -> bool:
        """Whether every attempted check actually ran."""
        return not self.unavailable_checks

    def set_check(self, name: str, state: str) -> None:
        """Record the outcome of one check, and refuse an invented state."""
        if state not in CHECK_STATES:
            state = NOT_AVAILABLE
        self.checks[name] = state

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for issue in self.issues:
            counts[issue.severity] = counts.get(issue.severity, 0) + 1
        parts = [f"QC {self.verdict}"]
        if counts:
            parts.append(", ".join(f"{count} {severity}" for severity, count in sorted(counts.items())))
        if self.unavailable_checks:
            parts.append(f"{len(self.unavailable_checks)} check(s) not available")
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
            if measured.get("probe_source_label"):
                lines.append(f"Inspected with: {measured['probe_source_label']}")
        for name in sorted(self.checks):
            lines.append(f"  {name}: {self.checks[name]}")
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
            "checks": dict(self.checks),
            "unavailable_checks": self.unavailable_checks,
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
              expect_subtitles: float = 0.0, inherited: Sequence[Any] = (),
              expected_video_codec: str = "", expected_audio_codec: str = "") -> QCReport:
        """Inspect one finished file and produce a verdict.

        Every check that runs records PASS, FAIL or CHECK NOT AVAILABLE, and the
        verdict follows from those states rather than from an absence of
        complaints.  A check that could not run is never a pass.
        """
        import time

        started = time.monotonic()
        path = Path(path)
        report = QCReport(path=path)
        report.expected = {
            "duration": round(float(expected_duration or 0.0), 3),
            "width": int(expected_width or 0), "height": int(expected_height or 0),
            "fps": round(float(expected_fps or 0.0), 3),
            "audio": bool(expect_audio),
            "video_codec": str(expected_video_codec or ""),
            "audio_codec": str(expected_audio_codec or ""),
        }

        # 1. The file itself.
        if not path.exists():
            report.issues.append(QCIssue(
                "FILE_MISSING", f"The output file was not found at {path}.",
                "The render did not finish. Check the log for the reason, then render "
                "again.", severity="error", area="file"))
            report.set_check("file_exists", FAIL)
            report.verdict = FAIL
            report.seconds = time.monotonic() - started
            return report
        report.set_check("file_exists", PASS)

        size = path.stat().st_size
        report.measured["size_bytes"] = size
        if size <= 0:
            report.issues.append(QCIssue(
                "FILE_EMPTY", f"{path.name} is 0 bytes long.",
                "The render failed while writing. Free some disk space and try again.",
                severity="error", area="file"))
            report.set_check("file_not_empty", FAIL)
            report.verdict = FAIL
            report.seconds = time.monotonic() - started
            return report
        report.set_check("file_not_empty", PASS)

        if not self._readable(path):
            report.issues.append(QCIssue(
                "FILE_UNREADABLE", f"{path.name} could not be opened for reading.",
                "Another program may have the file open, or permissions may have "
                "changed. Close it and try again.", severity="error", area="file"))
            report.set_check("file_readable", FAIL)
            report.verdict = FAIL
            report.seconds = time.monotonic() - started
            return report
        report.set_check("file_readable", PASS)

        # 2. What FFmpeg says is inside it.
        info = probe_media(path, self.tools)
        if not info.ok:
            report.issues.append(QCIssue(
                "FILE_NOT_READABLE_BY_FFMPEG",
                f"FFmpeg could not read {path.name}: {info.error}",
                "The file is probably incomplete. Render again, and if it happens "
                "repeatedly check the disk.", severity="error", area="file"))
            report.set_check("container_readable", FAIL)
            report.verdict = FAIL
            report.seconds = time.monotonic() - started
            return report
        report.set_check("container_readable", PASS)

        report.measured.update({
            "width": info.width, "height": info.height, "fps": info.fps,
            "duration": round(info.duration or 0.0, 3),
            "video_codec": info.video_codec, "pixel_format": info.pixel_format,
            "audio_codec": info.audio_codec, "sample_rate": info.sample_rate,
            "channels": info.channels, "bitrate_kbps": info.bitrate_kbps,
            "has_video": info.has_video, "has_audio": info.has_audio,
            "probe_source": info.source,
            "probe_source_label": info.source_label,
        })

        # 2b. Which tool produced those numbers.  The facts are real either way,
        # but "verified with FFprobe" may only be said when FFprobe ran.
        if not info.used_ffprobe:
            report.set_check("ffprobe_inspection", NOT_AVAILABLE)
            report.issues.append(QCIssue(
                "FFPROBE_NOT_USED",
                f"The file was measured with the {FFPROBE_FALLBACK_LABEL}.",
                "Install FFprobe alongside FFmpeg for full verification. The "
                "measurements shown are real, but per-stream detail is limited "
                "without it.", severity=NOT_AVAILABLE, area="file"))
        else:
            report.set_check("ffprobe_inspection", PASS)

        # 3. Truncation: a real container read reaches the end.
        if self._looks_truncated(info):
            report.issues.append(QCIssue(
                "FILE_TRUNCATED",
                "The file looks incomplete - the container does not describe a "
                "complete video.",
                "The render was interrupted. Render again; the previous take is "
                "untouched.", severity="error", area="file"))
            report.set_check("container_complete", FAIL)
        else:
            report.set_check("container_complete", PASS)

        # 4. The picture.
        if not info.has_video:
            report.issues.append(QCIssue(
                "NO_VIDEO_STREAM", "The file contains no video stream.",
                "Render again with a supported codec.", severity="error", area="video"))
            report.set_check("video_stream", FAIL)
        else:
            report.set_check("video_stream", PASS)
        if expected_width and info.width and int(info.width) != int(expected_width):
            report.issues.append(QCIssue(
                "WIDTH_MISMATCH",
                f"The video is {info.width} pixels wide but the project asked for "
                f"{expected_width}.",
                "Check the resolution in the export settings.", severity="error",
                area="video"))
            report.set_check("resolution", FAIL)
        elif expected_height and info.height and int(info.height) != int(expected_height):
            report.set_check("resolution", FAIL)
        elif expected_width or expected_height:
            report.set_check("resolution", PASS)
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
                report.set_check("frame_rate", FAIL)
            else:
                report.set_check("frame_rate", PASS)

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
                report.set_check("duration", FAIL)
            elif difference > max(DURATION_TOLERANCE, 1.0):
                report.issues.append(QCIssue(
                    "VIDEO_TOO_LONG",
                    f"The video is {duration:.2f}s but the timeline is "
                    f"{expected_duration:.2f}s long.",
                    "Check the scene durations and the export settings.",
                    severity=WARNING, area="timeline"))
                report.set_check("duration", FAIL)
            else:
                report.set_check("duration", PASS)

        # 6. Codecs: the file must contain what was asked for, not a substitute.
        if expected_video_codec:
            wanted = str(expected_video_codec).lower()
            got = str(info.video_codec or "").lower()
            if got and got != wanted:
                report.issues.append(QCIssue(
                    "VIDEO_CODEC_MISMATCH",
                    f"The video stream is {got or 'unknown'} but the export asked "
                    f"for {wanted}.",
                    "Check the codec in the export settings, then render again.",
                    severity="error", area="video"))
                report.set_check("video_codec", FAIL)
            elif got:
                report.set_check("video_codec", PASS)
            else:
                report.set_check("video_codec", NOT_AVAILABLE)
        if expected_audio_codec and expect_audio and info.has_audio:
            wanted = str(expected_audio_codec).lower()
            got = str(info.audio_codec or "").lower()
            if got and got != wanted:
                report.issues.append(QCIssue(
                    "AUDIO_CODEC_MISMATCH",
                    f"The audio stream is {got or 'unknown'} but the export asked "
                    f"for {wanted}.",
                    "Check the audio codec in the export settings, then render "
                    "again.", severity="error", area="audio"))
                report.set_check("audio_codec", FAIL)
            elif got:
                report.set_check("audio_codec", PASS)
            else:
                report.set_check("audio_codec", NOT_AVAILABLE)

        # 7. Audio.
        if expect_audio:
            if not info.has_audio:
                report.issues.append(QCIssue(
                    "NO_AUDIO_STREAM",
                    "The video has no audio track, but the project has narration or "
                    "music.",
                    "Check that narration has been generated, then render again.",
                    severity="error", area="audio"))
                report.set_check("audio_stream", FAIL)
            else:
                report.set_check("audio_stream", PASS)
                self._check_av_lengths(info, report)

        # 8. Deep scans (they read the whole file).
        if self.deep_checks:
            # A check that does not apply is simply absent.  Only a check that
            # *should* have run but could not is recorded as not available -
            # otherwise a silent video would be reported as under-verified.
            if info.has_audio:
                report.issues.extend(self._audio_stats(path, duration, report))
            elif expect_audio:
                report.set_check("audio_levels", NOT_AVAILABLE)
            if info.has_video:
                report.issues.extend(self._video_scan(path, duration, report))
        else:
            for name in ("audio_levels", "silence", "black_frames"):
                report.set_check(name, NOT_AVAILABLE)
            report.issues.append(QCIssue(
                "DEEP_CHECKS_SKIPPED",
                "The silence, clipping and black-frame scans were skipped for this "
                "check.",
                "Run a full quality check to confirm the picture and audio.",
                severity=NOT_AVAILABLE, area="file"))

        # 9. Subtitles, when the project has them.
        if expect_subtitles and expect_subtitles > 0:
            if duration and expect_subtitles - duration > 1.0:
                report.issues.append(QCIssue(
                    "SUBTITLES_LONGER_THAN_VIDEO",
                    f"The captions run to {expect_subtitles:.2f}s but the video is "
                    f"{duration:.2f}s long.",
                    "Regenerate the captions from the narration before rendering.",
                    severity=WARNING, area="subtitles"))
                report.set_check("subtitle_span", FAIL)
            else:
                report.set_check("subtitle_span", PASS)

        # 10. Anything the scene validation already knew about.
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
            report.set_check("scene_validation", FAIL)

        report.verdict = self._verdict(report.issues, report.checks)
        report.seconds = time.monotonic() - started
        log_event("RENDER_QC_DONE", report.summary(), path=str(path),
                  duration=report.measured.get("duration"),
                  verdict=report.verdict,
                  probe=report.measured.get("probe_source"),
                  unavailable=",".join(report.unavailable_checks))
        return report

    # -- individual checks ----------------------------------------------

    def _check_av_lengths(self, info: MediaInfo, report: "QCReport") -> None:
        """Compare the audio track's own length with the picture's.

        The container reports one duration for the whole file, so comparing it
        with itself can never detect a short audio track.  Each stream reports
        its own duration, and that is the comparison that matters.
        """
        video_seconds = info.stream_duration("video")
        audio_seconds = info.stream_duration("audio")
        if video_seconds <= 0 or audio_seconds <= 0:
            report.set_check("av_sync", NOT_AVAILABLE)
            report.issues.append(QCIssue(
                "AV_LENGTH_NOT_MEASURED",
                "FFmpeg did not report separate lengths for the audio and video "
                "tracks, so their alignment could not be verified.",
                "Install FFprobe for per-stream measurements, or check the audio by "
                "playing the file.", severity=NOT_AVAILABLE, area="audio"))
            return
        report.measured["video_stream_duration"] = round(video_seconds, 3)
        report.measured["audio_stream_duration"] = round(audio_seconds, 3)
        drift = abs(video_seconds - audio_seconds)
        if drift > AV_SYNC_TOLERANCE:
            report.issues.append(QCIssue(
                "AV_LENGTH_MISMATCH",
                f"The audio track is {audio_seconds:.2f}s but the picture is "
                f"{video_seconds:.2f}s long, so they drift {drift:.2f}s apart.",
                "Render again; if it repeats, shorten the longest audio track.",
                severity=WARNING, area="audio"))
            report.set_check("av_sync", FAIL)
        else:
            report.set_check("av_sync", PASS)

    def _readable(self, path: Path) -> bool:
        try:
            with path.open("rb") as handle:
                return len(handle.read(16)) > 0
        except OSError:
            return False

    def _looks_truncated(self, info: MediaInfo) -> bool:
        if not info.duration or info.duration <= 0:
            return True
        # An MP4/MOV without a moov atom reports no duration at all - covered
        # above.  When FFprobe is available it also gives every stream its own
        # duration, so a container that claims a length while its video stream
        # reports none is missing its tail.  Without FFprobe that signal does
        # not exist, and guessing would produce false alarms.
        if info.used_ffprobe and info.has_video and info.stream_duration("video") <= 0:
            return True
        return False

    def _audio_stats(self, path: Path, duration: float, report: "QCReport") -> list:
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
            report.set_check("audio_levels", NOT_AVAILABLE)
            issues.append(QCIssue(
                "AUDIO_STATS_UNAVAILABLE",
                "The audio level could not be measured.",
                "Open the video and listen to it, or check the log.",
                severity=NOT_AVAILABLE, area="audio"))
            return issues

        report.measured["audio_peak_db"] = peak
        report.measured["audio_rms_db"] = rms
        report.set_check("audio_levels", PASS)
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
        report.set_check("silence", PASS)
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

    def _video_scan(self, path: Path, duration: float, report: "QCReport") -> list:
        """Real black-frame detection, using FFmpeg's own analysis."""
        issues: list[QCIssue] = []
        detected = probe_detect(tools=self.tools, source=path, duration=duration)
        if not detected.get("ok", False):
            # The scan itself did not run, which is not the same as finding
            # nothing: saying PASS here would be a false clean bill of health.
            report.set_check("black_frames", NOT_AVAILABLE)
            issues.append(QCIssue(
                "BLACK_FRAME_SCAN_UNAVAILABLE",
                "The black-frame scan could not be run on this file.",
                "Watch the finished video for black sections, or check the log for "
                "the FFmpeg error.", severity=NOT_AVAILABLE, area="video"))
            return issues
        ranges = detected.get("ranges") or []
        report.measured["black_seconds"] = float(detected.get("black_seconds") or 0.0)
        if not ranges:
            report.set_check("black_frames", PASS)
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
            report.set_check("black_frames", FAIL)
        else:
            report.set_check("black_frames", PASS)
        return issues

    @staticmethod
    def _verdict(issues: Sequence[QCIssue],
                 checks: Optional[dict] = None) -> str:
        """PASS, WARNING or FAIL - and never an ambiguous fourth thing.

        A check that could not be run counts as a warning, not as a pass: the
        verdict says "we could not confirm this file completely", which is the
        truth, rather than claiming a clean result that was never measured.
        """
        if any(issue.severity == "error" for issue in issues):
            return FAIL
        if any(issue.severity in (WARNING, NOT_AVAILABLE) for issue in issues):
            return WARNING
        if checks and any(state == NOT_AVAILABLE for state in checks.values()):
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
