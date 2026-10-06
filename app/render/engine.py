"""The render engine (Stage E, sections 12, 32-50).

One path from project to file:

    RenderRequest -> validate -> audio -> subtitles -> scene frames ->
    assembly -> encode/mux -> QC -> output

Every step is a service the GUI, the CLI and the tests all call, so a render
started from a button and one started from a terminal are the same render.

Two properties matter most here and shape the whole file:

* **Streaming.**  Frames are produced one at a time and piped to FFmpeg, so a
  60-minute video costs the same memory as a 30-second one (section 33).
* **Resumable.**  Each scene segment is written to disk under a key derived from
  what it depends on.  After a crash or a cancel, a render picks up where it
  stopped instead of starting again (sections 35, 36).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from ..audio.service import AudioService
from ..core.errors import JobCancelled
from ..core.logging_setup import log_event, log_exception
from ..scene.canvas import Canvas
from ..scene.storyboard import build_context
from ..scene.timing import build_timeline
from ..scene.validate import validate_project_scenes
from ..subtitles.service import generate_cues, to_ass, to_srt, to_vtt, write_subtitle_file
from .capabilities import (
    EncoderCapabilities,
    detect_capabilities,
    estimate_file_size,
    estimate_render_time,
    validate_export_settings,
)
from .encode import burn_subtitles, concat_and_mux, stream_encode
from .frames import FrameError, FrameSource
from .output import HistoryEntry, OutputDecision, OutputService
from .qc import QCReport, QCService
from .segments import plan_segments

__all__ = [
    "RenderRequest",
    "RenderProgress",
    "RenderResult",
    "RenderEngine",
    "RENDER_STATES",
    "QUEUED",
    "PREPARING",
    "VALIDATING",
    "AUDIO",
    "SUBTITLES",
    "SCENES",
    "ENCODING",
    "QC",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
]

QUEUED = "QUEUED"
PREPARING = "PREPARING"
VALIDATING = "VALIDATING"
AUDIO = "AUDIO"
SUBTITLES = "SUBTITLES"
SCENES = "SCENES"
ENCODING = "ENCODING"
QC = "QC"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

RENDER_STATES: tuple[str, ...] = (
    QUEUED, PREPARING, VALIDATING, AUDIO, SUBTITLES, SCENES, ENCODING, QC,
    COMPLETED, FAILED, CANCELLED,
)

#: Rough bytes of scratch space a render needs per frame of output, used for the
#: pre-flight disk check.  Deliberately generous.
SCRATCH_BYTES_PER_FRAME = 90_000


@dataclass
class RenderRequest:
    """Everything needed to render, with nothing implied."""

    project: Any = None
    #: Overrides applied on top of the project's stored settings for this run.
    #: Stored back only if the caller asks - a one-off draft render must not
    #: quietly change the project.
    overrides: dict = field(default_factory=dict)
    include_audio: bool = True
    include_subtitles: bool = True
    burn_subtitles: Optional[bool] = None      # None = use the project setting
    two_pass: bool = False
    persist_settings: bool = False
    #: Reuse segments from an earlier interrupted run.
    resume: bool = True
    #: Skip the deep (whole-file) QC scans.
    quick_qc: bool = False

    def to_dict(self) -> dict:
        return {"include_audio": self.include_audio,
                "include_subtitles": self.include_subtitles,
                "burn_subtitles": self.burn_subtitles,
                "two_pass": self.two_pass, "resume": self.resume,
                "overrides": dict(self.overrides)}


@dataclass
class RenderProgress:
    """What the caller is told while a render runs."""

    state: str = QUEUED
    percent: float = 0.0
    message: str = ""
    #: Frames encoded so far, and how many there are in total.
    frames_done: int = 0
    frames_total: int = 0
    segments_done: int = 0
    segments_total: int = 0
    #: Frames per second actually achieved, once measured.
    encode_fps: float = 0.0
    eta_seconds: float = 0.0
    #: Set when the work is being reused rather than redone.
    resumed_frames: int = 0

    def to_dict(self) -> dict:
        return {key: getattr(self, key) for key in (
            "state", "percent", "message", "frames_done", "frames_total",
            "segments_done", "segments_total", "encode_fps", "eta_seconds",
            "resumed_frames")}


@dataclass
class RenderResult:
    """How a render ended.  Always one of completed / failed / cancelled."""

    status: str = FAILED
    path: Optional[Path] = None
    message: str = ""
    what_to_do: str = ""
    technical: str = ""
    #: Blocking problems found before or during the render.
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    qc: Optional[QCReport] = None
    progress: RenderProgress = field(default_factory=RenderProgress)
    decision: Optional[OutputDecision] = None
    seconds: float = 0.0
    log_path: str = ""
    #: Where the intermediate work lives, so a resume can find it.
    work_dir: Optional[Path] = None
    estimates: dict = field(default_factory=dict)
    details: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == COMPLETED

    @property
    def cancelled(self) -> bool:
        return self.status == CANCELLED

    @property
    def failed(self) -> bool:
        return self.status == FAILED

    def summary(self) -> str:
        if self.status == COMPLETED and self.path is not None:
            qc = self.qc.verdict if self.qc else "not run"
            return f"Render complete: {self.path.name} (QC {qc})"
        if self.status == CANCELLED:
            return "The render was cancelled."
        return self.message or "The render did not finish."

    def to_dict(self) -> dict:
        return {
            "status": self.status, "path": str(self.path) if self.path else "",
            "message": self.message, "what_to_do": self.what_to_do,
            "technical": self.technical,
            "errors": [_issue_dict(item) for item in self.errors],
            "warnings": [_issue_dict(item) for item in self.warnings],
            "qc": self.qc.to_dict() if self.qc else None,
            "seconds": round(self.seconds, 2),
            "estimates": self.estimates, "details": self.details,
        }


class RenderEngine:
    """Runs a render from a project to a verified MP4."""

    def __init__(self, tools: Any, *, project_dir: Path, paths: Any = None,
                 cancel_token: Any = None,
                 progress: Optional[Callable[[RenderProgress], None]] = None,
                 caps: Optional[EncoderCapabilities] = None) -> None:
        self.tools = tools
        self.project_dir = Path(project_dir)
        self.paths = paths
        self.cancel_token = cancel_token
        self._progress_cb = progress
        self.caps = caps
        self.progress = RenderProgress()
        self.audio_service = AudioService(tools, project_dir=self.project_dir)
        self.output_service = OutputService(self.project_dir)

    # -- progress --------------------------------------------------------

    def _set_state(self, state: str, message: str = "", **fields: Any) -> None:
        self.progress.state = state
        if message:
            self.progress.message = message
        for key, value in fields.items():
            if hasattr(self.progress, key):
                setattr(self.progress, key, value)
        log_event("RENDER_STATE", message or state, state=state,
                  percent=round(self.progress.percent, 1))
        if self._progress_cb is not None:
            try:
                self._progress_cb(self.progress)
            except Exception as exc:  # noqa: BLE001 - a bad UI callback must not kill a render
                log_exception("Progress callback failed", exc, event="RENDER_PROGRESS_ERROR")

    def _check_cancelled(self) -> None:
        if self.cancel_token is not None and self.cancel_token.is_cancelled():
            raise JobCancelled("Cancelled by the user.")

    # -- validation ------------------------------------------------------

    def capabilities(self) -> EncoderCapabilities:
        """Encoder capabilities, detected once per engine."""
        if self.caps is None:
            self.caps = detect_capabilities(self.tools)
        return self.caps

    def validate(self, project: Any, *, duration: float = 0.0) -> tuple[list, list]:
        """Everything that must be right before drawing a single frame.

        Returns ``(errors, warnings)``.  Errors block the render; warnings are
        reported and the user decides (directive section 19).
        """
        errors: list = []
        warnings: list = []
        settings = project.format
        caps = self.capabilities()

        for issue in validate_export_settings(settings, caps, duration=duration):
            (errors if issue.severity == "error" else warnings).append(issue)

        canvas = Canvas(settings.width, settings.height, settings.fps)
        scene_validation = validate_project_scenes(project, canvas=canvas,
                                                   project_dir=self.project_dir)
        for error in scene_validation.errors:
            errors.append(_SceneIssueAdapter(error, "error"))
        for warning in scene_validation.warnings:
            warnings.append(_SceneIssueAdapter(warning, "warning"))

        timeline = build_timeline(project.scenes)
        audio_validation = self.audio_service.validate(project, timeline)
        for error in audio_validation.errors:
            errors.append(error)
        for warning in audio_validation.warnings:
            warnings.append(warning)

        if project.subtitles.enabled and project.subtitles.burn_in:
            if not self._libass_available():
                errors.append(_SimpleIssue(
                    "SUBTITLE_BURN_UNAVAILABLE",
                    "Burnt-in captions need FFmpeg's libass filter, which this FFmpeg "
                    "build does not have.",
                    "Turn off 'burn in subtitles' and export a .srt file instead, or "
                    "install an FFmpeg build with libass.",
                    "error"))
        return errors, warnings

    def _libass_available(self) -> bool:
        """Whether this FFmpeg can draw captions onto the picture.

        ``ffmpeg -filters`` lists one filter per line as
        ``... subtitles  V->V  Render text subtitles ...``, so the filter name is
        the *second* field - the last one is the end of the description.
        """
        result = self.tools.run(["-hide_banner", "-filters"], timeout=30.0)
        if not result.ok:
            return False
        for line in (result.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] in ("subtitles", "ass"):
                return True
        return False

    def disk_check(self, settings: Any, frames: int) -> Optional[Any]:
        """Refuse to start when there is not enough room for the scratch files."""
        directory = self.output_service.output_directory(settings)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            usage = shutil.disk_usage(directory)
        except OSError as exc:
            return _SimpleIssue(
                "OUTPUT_FOLDER_UNAVAILABLE",
                f"The output folder could not be used: {exc}",
                "Choose an output folder you have permission to write to.",
                "error")
        needed = frames * SCRATCH_BYTES_PER_FRAME
        estimate = estimate_file_size(settings, frames / max(1, settings.fps))
        needed += int(estimate["bytes"] * 1.5)
        if usage.free < needed:
            return _SimpleIssue(
                "DISK_SPACE_LOW",
                f"This render needs about {needed / (1024**3):.1f} GiB of free space "
                f"for scratch files and the finished video, but only "
                f"{usage.free / (1024**3):.1f} GiB is free.",
                "Free up disk space, lower the resolution, or choose a different "
                "output folder.", "error")
        return None

    # -- the render ------------------------------------------------------

    def render(self, request: RenderRequest) -> RenderResult:
        """Run the whole pipeline.  Never raises for an expected failure."""
        started = time.monotonic()
        result = RenderResult(progress=self.progress)
        project = request.project
        if project is None:
            result.message = "There is no open project to render."
            result.what_to_do = "Open a project, then render again."
            return result

        self._project = project
        if request.overrides:
            _apply_overrides(project, request.overrides)

        try:
            return self._render_inner(request, result, started)
        except JobCancelled:
            result.status = CANCELLED
            result.message = "The render was cancelled. Nothing was overwritten."
            result.what_to_do = "Start the render again when you are ready - it will " \
                                "reuse the scenes it already finished."
            result.seconds = time.monotonic() - started
            log_event("RENDER_CANCELLED", result.message,
                      frames_done=self.progress.frames_done)
            return result
        except FrameError as exc:
            result.status = FAILED
            result.message = exc.message
            result.what_to_do = exc.what_to_do
            result.technical = exc.technical
            result.seconds = time.monotonic() - started
            log_event("RENDER_FAILED", exc.message, scene=exc.scene_id,
                      technical=exc.technical[:400])
            return result
        except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
            result.status = FAILED
            result.message = f"The render stopped because of an unexpected problem: {exc}"
            result.what_to_do = "The details are in the log. Render again, and if it " \
                                "happens again please send the log."
            result.technical = f"{type(exc).__name__}: {exc}"
            result.seconds = time.monotonic() - started
            log_exception("Render failed unexpectedly", exc, event="RENDER_FAILED")
            return result

    def _render_inner(self, request: RenderRequest, result: RenderResult,
                      started: float) -> RenderResult:
        project = request.project
        settings = project.format

        # -- PREPARING ---------------------------------------------------
        self._set_state(PREPARING, "Preparing the render")
        timeline = build_timeline(project.scenes)
        fps = max(1, int(settings.fps))
        plan = plan_segments(timeline, fps=fps)
        result.details["plan"] = plan.to_dict()
        result.details["timeline_duration"] = round(timeline.total_duration, 3)

        work_dir = self._work_dir()
        result.work_dir = work_dir
        work_dir.mkdir(parents=True, exist_ok=True)

        # -- VALIDATING --------------------------------------------------
        self._check_cancelled()
        self._set_state(VALIDATING, "Checking the project before rendering")
        errors, warnings = self.validate(project, duration=timeline.total_duration)
        for issue in plan.issues:
            (errors if issue.severity == "error" else warnings).append(issue)
        disk_issue = self.disk_check(settings, plan.total_frames)
        if disk_issue is not None:
            errors.append(disk_issue)

        result.errors = errors
        result.warnings = warnings
        if errors:
            result.status = FAILED
            result.message = f"The render was stopped because {len(errors)} problem(s) " \
                             f"must be fixed first."
            first = errors[0]
            result.what_to_do = str(getattr(first, "what_to_do", "") or
                                    "Fix the problems listed, then render again.")
            result.seconds = time.monotonic() - started
            log_event("RENDER_BLOCKED", result.message,
                      errors="; ".join(str(getattr(item, "code", "?")) for item in errors[:6]))
            return result

        result.estimates = estimate_file_size(settings, timeline.total_duration)
        result.estimates["time"] = estimate_render_time(
            frames=plan.total_frames, width=settings.width, height=settings.height,
            fps=fps, encoder_preset=settings.encoder_preset)

        # -- AUDIO -------------------------------------------------------
        master_audio: Optional[Path] = None
        if request.include_audio:
            self._check_cancelled()
            self._set_state(AUDIO, "Mixing the audio", percent=4.0)
            master_audio = work_dir / "master_audio.wav"
            audio = self.audio_service.render_master(
                project, timeline, master_audio, cancel_token=self.cancel_token,
                progress=lambda fraction, note: self._set_state(
                    AUDIO, note or "Mixing the audio", percent=4.0 + fraction * 6.0))
            if not audio.ok:
                if audio.plan is not None and not audio.plan.has_audio:
                    # A video with no audio is legitimate; say so and carry on.
                    log_event("RENDER_AUDIO_EMPTY", "No audio tracks to mix",
                              message=audio.message)
                    master_audio = None
                    warnings.extend(audio.issues)
                else:
                    result.status = FAILED
                    result.message = audio.message or "The audio could not be mixed."
                    result.what_to_do = "Check the narration and music files, then " \
                                        "render again."
                    result.seconds = time.monotonic() - started
                    log_event("RENDER_AUDIO_FAILED", result.message)
                    return result
            else:
                result.details["audio_duration"] = audio.duration
                warnings.extend(audio.issues)

        # -- SUBTITLES ---------------------------------------------------
        ass_file: Optional[Path] = None
        if request.include_subtitles and project.subtitles.enabled:
            self._check_cancelled()
            self._set_state(SUBTITLES, "Preparing the captions", percent=11.0)
            cues, cue_issues = generate_cues(project, timeline)
            warnings.extend(cue_issues)
            project.subtitles.cues = cues
            if cues:
                srt_path = work_dir / "subtitles.srt"
                vtt_path = work_dir / "subtitles.vtt"
                write_subtitle_file(srt_path, to_srt(cues))
                write_subtitle_file(vtt_path, to_vtt(cues))
                result.details["subtitle_files"] = [str(srt_path), str(vtt_path)]
                result.details["subtitle_count"] = len(cues)
                burn = project.subtitles.burn_in if request.burn_subtitles is None \
                    else bool(request.burn_subtitles)
                if burn:
                    ass_file = work_dir / "subtitles.ass"
                    write_subtitle_file(ass_file, to_ass(
                        cues, _subtitle_style(project),
                        width=settings.width, height=settings.height,
                        spec=project.subtitles))
                    result.details["subtitle_burn"] = str(ass_file)

        # -- SCENES ------------------------------------------------------
        self._check_cancelled()
        canvas = Canvas(settings.width, settings.height, fps)
        ctx = build_context(project, canvas=canvas, project_dir=self.project_dir)
        source = FrameSource(project, canvas=canvas, ctx=ctx, fps=fps)
        key_basis = self._cache_basis(project, settings, request)

        self.progress.frames_total = plan.total_frames
        self.progress.segments_total = len(plan.segments)
        segment_paths: list[Path] = []
        reused = 0

        for segment in plan.segments:
            self._check_cancelled()
            target = work_dir / f"segment_{segment.index:04d}.mp4"
            key_file = target.with_suffix(".key")
            cache_key = self._segment_key(segment, key_basis)

            if request.resume and target.is_file() and key_file.is_file() \
                    and key_file.read_text(encoding="utf-8").strip() == cache_key \
                    and self._segment_valid(target):
                segment_paths.append(target)
                reused += segment.frame_count
                self.progress.resumed_frames += segment.frame_count
                self.progress.frames_done += segment.frame_count
                self.progress.segments_done += 1
                self._set_state(SCENES, f"Reusing scene {segment.index + 1} of "
                                        f"{len(plan.segments)}",
                                percent=self._percent(segment))
                continue

            label = segment.name or f"scene {segment.index + 1}"
            self._set_state(SCENES,
                            f"Rendering {label} ({segment.index + 1} of "
                            f"{len(plan.segments)})",
                            percent=self._percent(segment))
            encode = stream_encode(
                tools=self.tools,
                frames=source.stream(segment, width=settings.width, height=settings.height),
                output=target, width=settings.width, height=settings.height, fps=fps,
                settings=settings, cancel_token=self.cancel_token,
                timeout=21600.0,
                progress=lambda written, seg=segment: self._frame_progress(seg, written),
            )
            if encode.cancelled:
                _remove_quietly(target)
                raise JobCancelled("Cancelled by the user.")
            if not encode.ok:
                result.status = FAILED
                result.message = f"The scene '{label}' could not be encoded."
                result.what_to_do = "See the FFmpeg output below. Lowering the quality " \
                                    "preset or the resolution usually helps."
                result.technical = encode.describe_failure()
                result.seconds = time.monotonic() - started
                log_event("RENDER_SCENES_FAILED", result.message,
                          segment=segment.index)
                return result
            key_file.write_text(cache_key, encoding="utf-8")
            segment_paths.append(target)
            self.progress.segments_done += 1
            if encode.details.get("encode_fps"):
                self.progress.encode_fps = float(encode.details["encode_fps"])
                remaining = max(0, plan.total_frames - self.progress.frames_done)
                self.progress.eta_seconds = round(
                    remaining / max(0.1, self.progress.encode_fps), 1)

        # -- ENCODING (assembly) ----------------------------------------
        self._check_cancelled()
        decision = self.output_service.decide(
            settings, project_name=_project_name(project),
            quality=str(settings.quality_preset), resolution=f"{settings.width}x{settings.height}")
        result.decision = decision
        staged = self.output_service.staging_path(decision)
        self._set_state(ENCODING, f"Assembling {decision.filename}", percent=92.0)

        assembled = work_dir / "assembled.mp4"
        join = concat_and_mux(tools=self.tools, segments=segment_paths, output=assembled,
                              audio=master_audio, settings=settings,
                              cancel_token=self.cancel_token)
        if join.cancelled:
            raise JobCancelled("Cancelled by the user.")
        if not join.ok:
            result.status = FAILED
            result.message = "The scenes could not be joined into one video."
            result.what_to_do = "Check that there is disk space, then render again - " \
                                "the finished scenes are kept and will be reused."
            result.technical = join.describe_failure()
            result.seconds = time.monotonic() - started
            return result

        final_source = assembled
        if ass_file is not None:
            self._check_cancelled()
            self._set_state(ENCODING, "Burning in the captions", percent=95.0)
            burned = work_dir / "burned.mp4"
            burn = burn_subtitles(tools=self.tools, source=assembled, ass_file=ass_file,
                                  output=burned, settings=settings,
                                  cancel_token=self.cancel_token)
            if burn.cancelled:
                raise JobCancelled("Cancelled by the user.")
            if not burn.ok:
                result.status = FAILED
                result.message = "The captions could not be burned into the video."
                result.what_to_do = burn.what_to_do
                result.technical = burn.describe_failure()
                result.seconds = time.monotonic() - started
                return result
            final_source = burned

        # -- QC ----------------------------------------------------------
        # The finished picture is moved next to its final home first, so the
        # quality check inspects the very file the user will be handed, and the
        # last step is a rename inside one folder.
        _remove_quietly(staged)
        shutil.move(str(final_source), str(staged))

        self._check_cancelled()
        self._set_state(QC, "Checking the finished video", percent=97.0)
        qc = QCService(self.tools, deep_checks=not request.quick_qc)
        report = qc.check(
            staged,
            expected_duration=timeline.total_duration,
            expected_width=settings.width, expected_height=settings.height,
            expected_fps=fps, expect_audio=master_audio is not None,
            expect_subtitles=project.subtitles.duration if project.subtitles.enabled else 0.0,
            inherited=[_SceneIssueAdapter(item, "warning")
                       for item in _scene_warnings(project, canvas, self.project_dir)],
        )
        result.qc = report
        if report.has_fail:
            # A failed video is never left behind looking like a finished take.
            _remove_quietly(staged)
            result.status = FAILED
            result.message = f"The quality check failed: {report.errors[0].message if report.errors else 'see the report'}"
            result.what_to_do = report.errors[0].what_to_do if report.errors else \
                "See the quality report, then render again."
            result.technical = report.describe()
            result.seconds = time.monotonic() - started
            log_event("RENDER_QC_FAILED", result.message)
            return result

        # -- OUTPUT ------------------------------------------------------
        saved = self.output_service.finalize(staged, decision)
        report.path = saved
        result.path = saved
        result.status = COMPLETED
        result.message = f"Rendered {saved.name}"
        result.seconds = time.monotonic() - started
        result.progress.percent = 100.0
        result.details["resumed_frames"] = reused
        result.details["frames"] = plan.total_frames
        result.details["segments"] = len(segment_paths)
        self._set_state(COMPLETED, result.message, percent=100.0)

        self.output_service.record(HistoryEntry(
            at=_now(), path=str(saved), status=COMPLETED,
            duration=report.measured.get("duration") or timeline.total_duration,
            width=int(report.measured.get("width") or settings.width),
            height=int(report.measured.get("height") or settings.height),
            fps=float(report.measured.get("fps") or fps),
            size_bytes=int(report.measured.get("size_bytes") or 0),
            quality=str(settings.quality_preset),
            resolution=f"{settings.width}x{settings.height}",
            qc=report.verdict,
            notes=result.message,
        ))
        if request.persist_settings and request.overrides:
            project.export.sync_from_format(settings)
            log_event("RENDER_SETTINGS_PERSISTED", "Export settings stored in the project",
                      keys=",".join(sorted(request.overrides)))
        self._cleanup_segments(work_dir, segment_paths)
        return result

    # -- helpers ---------------------------------------------------------

    def _work_dir(self) -> Path:
        base = None
        if self.paths is not None:
            base = getattr(self.paths, "temp_dir", None)
        if base is None:
            base = self.project_dir / "cache" / "renders"
        base = Path(base) / "render_work"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _percent(self, segment: Any) -> float:
        total = max(1, self.progress.frames_total)
        done = segment.frame_start
        return round(15.0 + 75.0 * (done / total), 1)

    def _frame_progress(self, segment: Any, written: int) -> None:
        self.progress.frames_done = segment.frame_start + written
        total = max(1, self.progress.frames_total)
        self.progress.percent = round(15.0 + 75.0 * (self.progress.frames_done / total), 1)
        # Reporting every frame would flood the log and the UI; report about
        # fifty times over a render, whatever its length.
        step = max(1, total // 50)
        if self.progress.frames_done % step == 0 or self.progress.frames_done == total:
            self._set_state(SCENES, self.progress.message,
                            percent=self.progress.percent)

    def _cache_basis(self, project: Any, settings: Any, request: RenderRequest) -> dict:
        """What a cached segment depends on.

        Anything that would change a pixel is in here; anything that would not is
        left out, so a re-render after a crash reuses the scenes it already did.
        """
        return {
            "size": [int(settings.width), int(settings.height)],
            "fps": int(settings.fps),
            "codec": str(settings.codec),
            "crf": int(settings.crf or 0),
            "bitrate": int(settings.bitrate_kbps or 0),
            "preset": str(settings.encoder_preset),
            "pixel_format": str(settings.pixel_format),
            "container": str(settings.container),
            "two_pass": bool(request.two_pass),
        }

    def _segment_key(self, segment: Any, basis: dict) -> str:
        """A key for one cached segment.

        Includes the scene content itself, so editing a scene invalidates exactly
        that segment and nothing else.
        """
        payload = dict(basis)
        payload.update({
            "index": segment.index, "kind": segment.kind,
            "scene_id": segment.scene_id, "next": segment.next_scene_id,
            "transition": segment.transition,
            "start": round(segment.start, 4), "end": round(segment.end, 4),
            "frame_start": segment.frame_start, "frame_count": segment.frame_count,
        })
        project = self._current_project
        for scene_id in (segment.scene_id, segment.next_scene_id):
            scene = project.scene_by_id(scene_id) if hasattr(project, "scene_by_id") else None
            if scene is None:
                for candidate in list(getattr(project, "scenes", []) or []):
                    if str(getattr(candidate, "id", "")) == scene_id:
                        scene = candidate
                        break
            if scene is not None and hasattr(scene, "to_dict"):
                payload[f"scene:{scene_id}"] = scene.to_dict()
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8"))
        return digest.hexdigest()

    @property
    def _current_project(self) -> Any:
        return getattr(self, "_project", None)

    def _segment_valid(self, path: Path) -> bool:
        """A cached segment is only reused if FFmpeg can still read it."""
        if path.stat().st_size <= 0:
            return False
        from ..media.probe import probe_media

        info = probe_media(path, self.tools)
        return bool(info.ok and info.has_video)

    def _cleanup_segments(self, work_dir: Path, segments: Sequence[Path]) -> None:
        """Remove the finished scratch files (never the project or the output)."""
        for segment in segments:
            _remove_quietly(segment)
            _remove_quietly(segment.with_suffix(".key"))

    # -- binding ---------------------------------------------------------

    def _set_project(self, project: Any) -> None:
        self._project = project


def _subtitle_style(project: Any) -> Any:
    """The caption styling, which lives in the theme section."""
    theme = getattr(project, "theme", None)
    return getattr(theme, "subtitle_style", None)


def _project_name(project: Any) -> str:
    """The project's display name, which lives in its metadata section."""
    meta = getattr(project, "project", None)
    name = str(getattr(meta, "name", "") or "").strip()
    return name or "Video"


def _apply_overrides(project: Any, overrides: dict) -> None:
    """Apply one-off export overrides to the project's format section."""
    settings = project.format
    for key, value in dict(overrides or {}).items():
        if hasattr(settings, key):
            setattr(settings, key, value)


def _scene_warnings(project: Any, canvas: Any, project_dir: Path) -> list:
    try:
        validation = validate_project_scenes(project, canvas=canvas, project_dir=project_dir)
    except Exception:  # noqa: BLE001 - a warning lookup must never fail a render
        return []
    return list(validation.warnings)


class _SceneIssueAdapter:
    """Normalises a scene validation finding into the render issue shape."""

    def __init__(self, issue: Any, severity: str) -> None:
        self.code = str(getattr(issue, "code", "") or "SCENE_ISSUE")
        element_id = getattr(issue, "element_id", "")
        prefix = f"[{element_id}] " if element_id else ""
        self.message = f"{prefix}{getattr(issue, 'message', '') or ''}"
        self.what_to_do = str(getattr(issue, "what_to_do", "") or "")
        self.severity = severity
        self.area = "scenes"

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "what_to_do": self.what_to_do, "severity": self.severity}


class _SimpleIssue:
    def __init__(self, code: str, message: str, what_to_do: str, severity: str) -> None:
        self.code = code
        self.message = message
        self.what_to_do = what_to_do
        self.severity = severity
        self.area = "project"

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "what_to_do": self.what_to_do, "severity": self.severity}


def _issue_dict(issue: Any) -> dict:
    if hasattr(issue, "to_dict"):
        try:
            return issue.to_dict()
        except Exception:  # noqa: BLE001
            pass
    return {"code": str(getattr(issue, "code", "")),
            "message": str(getattr(issue, "message", "")),
            "what_to_do": str(getattr(issue, "what_to_do", "")),
            "severity": str(getattr(issue, "severity", "warning"))}


def _remove_quietly(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")
