"""A local program that makes clips (sections 2, 36, 65).

The user configures a command line with placeholders; this adapter substitutes
the request into it, runs the program with a real timeout, and only reports
success after reading the file back.  It is the escape hatch that keeps the
studio useful for a model this application has never heard of - and it is a
*real* adapter, tested end to end with a stand-in program, not a stub.

Security (section 65): the command is never run through a shell.  The template
is split into arguments by :func:`shlex.split` and executed directly, so a
prompt containing ``&`` or ``;`` cannot become a second command.  Nothing is
downloaded, and nothing runs that the user did not type.
"""

from __future__ import annotations

import shlex
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

from ...core.logging_setup import log_event
from ...core.process import run_process
from ...image.saving import unique_path
from ..capabilities import AICapabilities
from ..provider import ProviderStatus, SettingField
from ..types import BackendKind, DeviceRequirement, ProviderState
from ..video import (VideoModel, VideoProvider, VideoRequest,
                     VideoResult, VideoState)
from ..video_validation import validate_video_file
from .standard import TEST_BACKEND_LABEL  # noqa: F401 - imported for symmetry

__all__ = ["CommandVideoBackend", "PLACEHOLDERS"]

#: Placeholders the template may use.  A template that names one claims the
#: matching capability - that is how "report False, never guess True" is kept
#: honest for a program the application cannot introspect.
PLACEHOLDERS: tuple[str, ...] = (
    "{prompt}", "{negative_prompt}", "{seed}", "{duration}", "{fps}",
    "{width}", "{height}", "{output}", "{source_image}", "{source_video}",
    "{reference_image}", "{model}", "{camera}", "{strength}", "{scene_id}",
)

#: Which capability each placeholder implies.
PLACEHOLDER_FEATURE: dict[str, str] = {
    "{source_image}": "image_to_video",
    "{source_video}": "video_to_video",
    "{reference_image}": "reference_image",
    "{camera}": "camera_control",
    "{negative_prompt}": "negative_prompt",
    "{duration}": "duration",
    "{fps}": "fps",
    "{strength}": "strength",
}


class CommandVideoBackend(VideoProvider):
    """One external program, invoked once per clip."""

    id = "command_video"
    name = "Local video command"
    kind = BackendKind.VIDEO
    transport = "command"
    version = "1"
    device_requirement = DeviceRequirement.UNKNOWN
    licence = "The program you configure"
    homepage = ""

    def __init__(self, command: str = "", *,
                 model: str = "command-video-model",
                 capabilities: Optional[AICapabilities] = None,
                 timeout: float = 3600.0, tools: Any = None) -> None:
        #: The FFmpeg tools used to measure what the program wrote.  Kept rather
        #: than discovered here, so a caller (and a test) can hand one in.
        self.tools = tools
        self.command = str(command or "")
        self.model_name = str(model or "command-video-model")
        self.timeout = float(timeout or 3600.0)
        self._capabilities = capabilities
        self._loaded = ""

    # -- description -------------------------------------------------------

    def describe(self) -> str:
        if self.command.strip():
            return (f"Runs: {self.command.strip()}\n"
                    f"The program must write the clip to the path given by "
                    f"{{output}} and exit 0.")
        return ("Runs a clip-making program you configure. Nothing runs until "
                "a command is set.")

    def location(self) -> str:
        return self.command.strip() or "(no command configured)"

    def install_hint(self) -> str:
        return ("Set a video command in Settings -> AI Studio, using "
                "{prompt}, {seed}, {duration}, {fps}, {width}, {height} and "
                "{output}.")

    def uses_network(self) -> bool:
        """Unknown: it is an arbitrary program, so the UI warns rather than guesses."""
        return True

    def settings_schema(self) -> list:
        return [
            SettingField(name="command", label="Command", kind="text",
                         default="", required=True,
                         help=("Placeholders: " + ", ".join(PLACEHOLDERS)),
                         advanced=False),
            SettingField(name="timeout", label="Timeout (seconds)", kind="float",
                         default=self.timeout,
                         help="The program is stopped after this long."),
            SettingField(name="model", label="Model name to record", kind="text",
                         default=self.model_name,
                         help="Stored with each clip so it can be reproduced."),
        ]

    # -- discovery ---------------------------------------------------------

    def status(self) -> ProviderStatus:
        template = self.command.strip()
        if not template:
            return ProviderStatus(
                state=ProviderState.NOT_INSTALLED,
                reason="No local video command has been configured.",
                instructions=list(self.settings_schema()[0].help.split(", "))
                + ["The program must write the clip to {output} and exit 0."],
                device="cpu")
        program = self._program(template)
        if program is None:
            return ProviderStatus(
                state=ProviderState.NOT_INSTALLED,
                reason=(f"The configured command could not be split into a "
                        f"program and its arguments: {template!r}"),
                instructions=["Use a normal command line, for example "
                              "'my-video-gen --prompt {prompt} --out {output}'."],
                device="cpu")
        resolved = shutil.which(program) or (program if Path(program).is_file() else None)
        if resolved is None:
            return ProviderStatus(
                state=ProviderState.NOT_INSTALLED,
                reason=f"'{program}' was not found on this machine.",
                instructions=["Install the program, or put it on PATH.",
                              "Or give its full path in the command."],
                device="cpu")
        return ProviderStatus(state=ProviderState.AVAILABLE,
                              reason=f"Found {resolved}.",
                              version=str(resolved), device="cpu")

    def capabilities(self) -> AICapabilities:
        if self._capabilities is not None:
            return self._capabilities
        template = self.command.strip()
        return AICapabilities(
            text_to_video=True, image_to_video="{source_image}" in template,
            video_to_video="{source_video}" in template,
            video_extend="{source_video}" in template,
            camera_control="{camera}" in template,
            reference_image="{reference_image}" in template,
            negative_prompt="{negative_prompt}" in template,
            seed_control="{seed}" in template,
            duration="{duration}" in template, fps="{fps}" in template,
            resolution_control=("{width}" in template and "{height}" in template),
            strength="{strength}" in template, quality=True,
            max_dimension=7680, min_dimension=16, dimension_multiple=2,
            min_duration=0.1, max_duration=600.0, max_fps=120,
            notes=("What the configured command claims, from the placeholders "
                   "its template uses."))

    def models(self) -> list[VideoModel]:
        if not self.status().available:
            return []
        return [VideoModel(id=self.model_name, name=self.model_name,
                           backend=self.id, kind="external",
                           capabilities=self.capabilities(),
                           requirement=DeviceRequirement.UNKNOWN,
                           status="available",
                           notes="Runs the configured local video command.")]

    # -- generation --------------------------------------------------------

    def load(self, model: Any = None) -> None:
        self._loaded = str(getattr(model, "id", "") or self.model_name)

    def unload(self) -> None:
        self._loaded = ""

    def loaded_model(self) -> str:
        return self._loaded

    def generate(self, request: VideoRequest, *,
                 progress: Optional[Callable[[str, float], None]] = None,
                 cancel: Any = None) -> VideoResult:
        started = self.started()
        report = progress or (lambda _state, _fraction: None)
        template = self.command.strip()
        if not template:
            return self._failed(request, started, "COMMAND_NOT_CONFIGURED",
                                "No local video command is configured.",
                                "This backend runs a program you choose, and "
                                "none is set.",
                                "Set a command in Settings -> AI Studio.")
        issues = [issue for issue in self.validate(request)
                  if issue.severity == "error"]
        if issues:
            first = issues[0]
            return self._failed(request, started, first.code, first.message,
                                "The request cannot be run as it is.",
                                first.what_to_do)
        if cancel is not None and cancel.is_cancelled():
            return self._cancelled(request, started)

        seed = int(request.seed or 0) or self._fresh_seed(request)
        output_dir = Path(request.output_dir or ".")
        output_dir.mkdir(parents=True, exist_ok=True)
        suffix = f".{request.output_format or 'mp4'}"
        target = unique_path(output_dir, request.name_stem or "clip", suffix)

        argv = self._argv(template, request, seed=seed, output=target)
        if not argv:
            return self._failed(request, started, "COMMAND_EMPTY",
                                "The configured command produced no program to run.",
                                "The template is only placeholders.",
                                "Add the program name to the command.")
        report("RUNNING", 0.1)
        outcome = run_process(argv, timeout=self.timeout, cancel=cancel,
                              cwd=str(output_dir))
        if outcome.cancelled:
            _remove_quietly(target)
            return self._cancelled(request, started)
        if outcome.timed_out:
            _remove_quietly(target)
            return self._failed(
                request, started, "COMMAND_TIMEOUT",
                f"The program did not finish within {self.timeout:.0f} seconds.",
                "It was stopped so the application would not hang.",
                "Raise the timeout in Settings -> AI Studio, or ask for a "
                "shorter clip.")
        if outcome.returncode != 0:
            _remove_quietly(target)
            return self._failed(
                request, started, "COMMAND_FAILED",
                f"The program exited with code {outcome.returncode}.",
                (outcome.output[-400:].strip() or
                 "The program reported no reason."),
                "Run the same command in a terminal to see the full output.")

        report("SAVING", 0.9)
        check = validate_video_file(
            target, tools=self._tools(), expected_width=request.width,
            expected_height=request.height, expected_fps=request.fps,
            expected_duration=request.duration)
        if check.code == "VIDEO_CHECK_NOT_AVAILABLE":
            # The file exists but cannot be measured.  That is a real outcome
            # and it is reported as one, not rounded up to success (section 93).
            return self._failed(
                request, started, "CLIP_UNMEASURED",
                f"{target.name} was written but FFmpeg is not available to "
                f"check it.",
                "The clip cannot be verified without FFmpeg.",
                "Install FFmpeg, then generate again.")
        if not check.ok:
            _remove_quietly(target)
            return self._failed(request, started, check.code, check.error,
                                check.why, check.what_to_do)

        log_event("AI_CLIP_WRITTEN", f"Local video command wrote {target.name}",
                  backend=self.id, mode=request.mode, seed=seed)
        result = VideoResult(
            ok=True, path=target, state=VideoState.COMPLETED, seed=seed,
            model=request.model or self.model_name, backend=self.id,
            mode=request.mode, width=check.width, height=check.height,
            fps=check.fps, duration=check.duration,
            frames=check.frames, output_format=request.output_format or "mp4",
            has_audio=check.has_audio, requested=request.to_dict(),
            seconds=self.started() - started, mismatch=list(check.mismatch))
        result.quality = {
            "model": result.model, "backend": self.id,
            "resolution": f"{result.width}x{result.height}",
            "fps": round(result.fps, 3), "duration": round(result.duration, 3),
            "seed": result.seed, "size_bytes": check.size_bytes,
            "measured_with": check.measured_with, "status": "measured"}
        result.metadata = {
            "prompt": request.prompt, "negative_prompt": request.negative_prompt,
            "seed": seed, "mode": request.mode, "camera": request.camera,
            "command": template, "command_output": outcome.output[-2000:],
            "generator": "local-command", "is_ai_model": None,
        }
        return result

    # -- internals ---------------------------------------------------------

    def _tools(self) -> Any:
        if self.tools is None:
            from ...tools.ffmpeg import FFmpegTools, discover_ffmpeg

            self.tools = FFmpegTools(discover_ffmpeg())
        return self.tools

    def _failed(self, request: VideoRequest, started: float, code: str,
                error: str, why: str, what_to_do: str) -> VideoResult:
        log_event("AI_CLIP_FAILED", error, code=code, backend=self.id)
        return VideoResult(ok=False, state=VideoState.FAILED, mode=request.mode,
                           backend=self.id, model=request.model, error=error,
                           why=why, what_to_do=what_to_do, code=code,
                           seconds=self.started() - started)

    def _cancelled(self, request: VideoRequest, started: float) -> VideoResult:
        return VideoResult(ok=False, state=VideoState.CANCELLED, cancelled=True,
                           mode=request.mode, backend=self.id,
                           model=request.model,
                           error="Clip generation was cancelled.",
                           seconds=self.started() - started)

    @staticmethod
    def _fresh_seed(request: VideoRequest) -> int:
        import hashlib
        import time as _time

        raw = f"{request.prompt}|{request.mode}|{_time.time()}".encode("utf-8")
        return int.from_bytes(hashlib.sha256(raw).digest()[:4], "big") or 1

    @staticmethod
    def _program(template: str) -> Optional[str]:
        try:
            parts = shlex.split(template, posix=True)
        except ValueError:
            return None
        if not parts:
            return None
        return parts[0]

    def _argv(self, template: str, request: VideoRequest, *, seed: int,
              output: Path) -> list[str]:
        """The command line, with every placeholder substituted.

        Values are substituted *after* splitting, so a prompt containing spaces
        or quotes stays one argument and cannot change the program being run.
        """
        try:
            parts = shlex.split(template, posix=True)
        except ValueError:
            return []
        values = {
            "prompt": str(request.prompt or ""),
            "negative_prompt": str(request.negative_prompt or ""),
            "seed": str(seed),
            "duration": f"{float(request.duration or 0.0):g}",
            "fps": str(int(request.fps or 0)),
            "width": str(int(request.width or 0)),
            "height": str(int(request.height or 0)),
            "output": str(output),
            "source_image": str(request.source_image or ""),
            "source_video": str(request.source_video or request.extend_from or ""),
            "reference_image": str(request.reference_image or ""),
            "model": str(request.model or self.model_name),
            "camera": str(request.camera or ""),
            "strength": f"{float(request.strength or 0.0):g}",
            "scene_id": str(request.scene_id or ""),
        }
        argv: list[str] = []
        for part in parts:
            text = part
            for key, value in values.items():
                text = text.replace("{" + key + "}", value)
            argv.append(text)
        return argv


def _remove_quietly(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:  # pragma: no cover
        pass
