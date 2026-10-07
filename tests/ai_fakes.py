"""Deterministic fakes for the Stage G tests (sections 78-86, 97-98, 113-115).

Nothing here is an AI model, and nothing here pretends to be one.  The fakes
exist so the tests can exercise the *contracts* - the queue, the validator, the
project integration, cancellation, failure handling - on a machine with no model
installed, which is exactly the situation the studio has to behave well in.

Three fakes matter:

* :class:`FakeVideoBackend` - a video backend that writes a real (tiny) file and
  can be told to fail in a specific way: raise, return nothing, return a corrupt
  file, hang, or ignore cancellation.  Every failure mode is a real one a backend
  has, so the tests prove the studio survives them rather than guessing;
* :class:`FakeImageProvider` - the same idea for Stage F's image contract;
* :class:`FakeModel` / :func:`write_weights` - model discovery without weights.

The fakes never import a model library and never touch the network.
"""

from __future__ import annotations

import json
import struct
import subprocess
from pathlib import Path
from typing import Any, Optional

from app.ai.capabilities import AICapabilities
from app.ai.provider import ProviderStatus
from app.ai.types import BackendKind, DeviceRequirement, ProviderState
from app.ai.video import VideoMode, VideoProvider, VideoRequest, VideoResult

__all__ = [
    "FakeVideoBackend", "FakeImageProvider", "FakeModel", "write_weights",
    "write_png", "probe_video", "FakeCancel", "ffmpeg_tools",
    "make_video_request",
]


class FakeCancel:
    """A cancel token that can be told to cancel, like the real one."""

    def __init__(self, *, cancelled: bool = False, after: int = 0) -> None:
        self._cancelled = bool(cancelled)
        self._after = int(after)
        self._checks = 0

    def is_cancelled(self) -> bool:
        self._checks += 1
        if self._after and self._checks >= self._after:
            self._cancelled = True
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True


class FakeModel:
    """A model record, describing something that may or may not be on disk."""

    def __init__(self, model_id: str = "fake-model", *, name: str = "",
                 path: str = "", size_bytes: int = 0,
                 requirement: str = DeviceRequirement.CPU_SUPPORTED,
                 status: str = "available", notes: str = "") -> None:
        self.id = model_id
        self.name = name or model_id
        self.path = path
        self.size_bytes = int(size_bytes)
        self.requirement = requirement
        self.status = status
        self.notes = notes
        self.kind = "video"

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "path": self.path,
                "size_bytes": self.size_bytes, "requirement": self.requirement,
                "status": self.status, "notes": self.notes}


def write_weights(folder: Path, *, name: str = "model.safetensors",
                  size_bytes: int = 1024, descriptor: Optional[dict] = None) -> Path:
    """Put a fake weight file (and optional descriptor) in a folder."""
    folder.mkdir(parents=True, exist_ok=True)
    weight = folder / name
    weight.write_bytes(b"\0" * int(size_bytes))
    if descriptor is not None:
        (folder / "model_index.json").write_text(json.dumps(descriptor),
                                                 encoding="utf-8")
    return weight


def write_png(path: Path, *, size: tuple[int, int] = (64, 36),
              colour: tuple[int, int, int] = (40, 120, 90)) -> Path:
    """A real PNG, so image-to-video tests have a real source."""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path)
    return path


def ffmpeg_tools() -> Any:
    """The discovered FFmpeg tools, once per process."""
    global _TOOLS
    if _TOOLS is None:
        from app.tools.ffmpeg import FFmpegTools, discover_ffmpeg

        _TOOLS = FFmpegTools(discover_ffmpeg())
    return _TOOLS


_TOOLS: Any = None


def probe_video(path: Path) -> dict:
    """Read a clip's real properties with FFprobe (tests only)."""
    tools = ffmpeg_tools()
    if tools.ffprobe is None:
        return {}
    completed = subprocess.run(
        [str(tools.ffprobe), "-v", "error", "-show_entries",
         "stream=codec_name,width,height,nb_frames",
         "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60)
    if completed.returncode != 0:
        return {}
    data = json.loads(completed.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    return {"codec": stream.get("codec_name", ""),
            "width": int(stream.get("width", 0) or 0),
            "height": int(stream.get("height", 0) or 0),
            "frames": int(stream.get("nb_frames", 0) or 0),
            "duration": float((data.get("format") or {}).get("duration", 0) or 0)}


def _tiny_mp4(path: Path, *, width: int = 128, height: int = 96,
              fps: int = 8, seconds: float = 1.0) -> Path:
    """Write a real, tiny, valid MP4 without any AI (FFmpeg only)."""
    tools = ffmpeg_tools()
    if tools.ffmpeg is None:
        raise RuntimeError("FFmpeg is not available, so no clip can be written.")
    path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [str(tools.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i",
         f"color=c=0x204060:s={int(width)}x{int(height)}:d={float(seconds)}:"
         f"r={int(fps)}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        capture_output=True, text=True, timeout=120)
    if completed.returncode != 0:
        raise RuntimeError(f"FFmpeg could not write the fixture: "
                           f"{completed.stderr.strip()[:200]}")
    return path


class FakeVideoBackend(VideoProvider):
    """A video backend with every failure mode a real one can have.

    ``behaviour`` is one of:

    ``ok``          write a real clip (the default);
    ``raise``       raise from ``generate`` - the studio must report it, not die;
    ``empty``       return ``ok`` without writing a file (never a false success);
    ``corrupt``     write a file that is not a video;
    ``tiny``        write a one-pixel clip (the validator's too-small check);
    ``nothing``     return ``ok=False`` with a friendly explanation;
    ``hang``        sleep until cancelled, to prove cancellation really stops it;
    ``ignore``      ignore the cancel token and finish, to prove the studio still
                    refuses to call a cancelled job a success;
    ``missing_model`` report NOT INSTALLED, like a backend with no weights.
    """

    kind = BackendKind.VIDEO
    transport = "python"
    device_requirement = DeviceRequirement.CPU_SUPPORTED

    def __init__(self, backend_id: str = "fake_video", *,
                 behaviour: str = "ok", capabilities: Optional[AICapabilities] = None,
                 models: Optional[list] = None, state: str = ProviderState.AVAILABLE,
                 reason: str = "", is_model: bool = True,
                 settings: Optional[list] = None,
                 tools: Any = None, name: str = "") -> None:
        super().__init__()
        self.id = backend_id
        self.name = name or f"Fake video backend ({backend_id})"
        self.behaviour = str(behaviour)
        self.is_model = bool(is_model)
        self._capabilities = capabilities or AICapabilities(
            text_to_video=True, image_to_video=True, video_to_video=True,
            video_extend=True, camera_control=True, seed_control=True,
            duration=True, fps=True, resolution_control=True, quality=True,
            negative_prompt=True, max_dimension=1920, min_dimension=64,
            dimension_multiple=2, max_duration=30.0, min_duration=0.5, max_fps=60,
            notes="A test fake; it writes a real file with FFmpeg and does not "
                  "understand the prompt.")
        self._models = list(models or [])
        self._state = str(state)
        self._reason = str(reason)
        self._settings = list(settings or [])
        self.tools = tools
        self.calls: list[dict] = []
        self.loaded_with: Any = None
        self.unloaded = 0

    # -- contract ---------------------------------------------------------

    def status(self) -> ProviderStatus:
        if self._state == ProviderState.NOT_INSTALLED:
            return ProviderStatus(
                state=self._state,
                reason=self._reason or "No model weights were found.",
                instructions=["Install a model, then detect again."])
        if self._state == ProviderState.NOT_SUPPORTED:
            return ProviderStatus(state=self._state,
                                  reason=self._reason or "Not supported here.",
                                  instructions=["Choose another backend."])
        return ProviderStatus(state=self._state,
                              reason=self._reason or "Ready for a test run.",
                              instructions=[], device="cpu")

    def capabilities(self) -> AICapabilities:
        return self._capabilities

    def models(self) -> list:
        return list(self._models)

    def settings_schema(self) -> list:
        return list(self._settings)

    def describe(self) -> str:
        return (f"{self.name}: a deterministic test fake. It writes a real file "
                f"and is not an AI model.")

    def install_hint(self) -> str:
        return "Nothing to install: this fake ships with the tests."

    def location(self) -> str:
        return "Built into the test suite"

    def load(self, model: Any = None) -> None:
        self.loaded_with = model

    def unload(self) -> None:
        self.unloaded += 1
        self.loaded_with = None

    def loaded_model(self) -> str:
        return str(getattr(self.loaded_with, "id", "") or "")

    # -- generation -------------------------------------------------------

    def generate(self, request: VideoRequest, *, progress: Any = None,
                 cancel: Any = None) -> VideoResult:
        self.calls.append({"mode": request.mode, "prompt": request.prompt,
                           "model": request.model, "seed": request.seed,
                           "batch": request.batch})
        if progress is not None:
            progress("RUNNING", 0.25)

        if self.behaviour == "missing_model":
            return self._failed(request, "No model is installed for this backend.",
                                "NOT_INSTALLED", code="MODEL_NOT_FOUND")
        if self.behaviour == "nothing":
            return self._failed(
                request, "This fake was told to fail.",
                "It is a test fake with behaviour='nothing'.",
                code="FAKE_REFUSED")
        if self.behaviour == "raise":
            raise RuntimeError("the fake backend raised on purpose")
        if self.behaviour == "hang":
            import time

            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                if cancel is not None and cancel.is_cancelled():
                    return VideoResult(ok=False, path=None, state="CANCELLED",
                                       mode=request.mode, backend=self.id,
                                       model=request.model, cancelled=True,
                                       error="Cancelled while working.")
                time.sleep(0.02)
            return self._failed(request, "The fake never stopped.", "timeout")
        if self.behaviour == "ignore" and cancel is not None and cancel.is_cancelled():
            pass  # deliberately falls through and writes a file anyway

        if progress is not None:
            progress("SAVING", 0.8)
        width = int(request.width or 128)
        height = int(request.height or 96)
        fps = int(request.fps or 8)
        seconds = float(request.duration or 1.0)
        target = self._target(request)
        if self.behaviour == "empty":
            return VideoResult(ok=True, path=None, state="COMPLETED",
                               mode=request.mode, backend=self.id,
                               model=request.model, seed=int(request.seed or 0),
                               width=width, height=height, fps=float(fps),
                               duration=seconds, metadata={})
        if self.behaviour == "corrupt":
            target.write_bytes(b"this is not a video at all")
            return VideoResult(ok=True, path=target, state="COMPLETED",
                               mode=request.mode, backend=self.id,
                               model=request.model, seed=int(request.seed or 0))
        if self.behaviour == "tiny":
            _tiny_mp4(target, width=2, height=2, fps=2, seconds=0.5)
        elif self.behaviour == "bad_stream":
            target.write_bytes(struct.pack("<4s", b"RIFF") + b"\0" * 64)
        else:
            _tiny_mp4(target, width=width, height=height, fps=fps,
                      seconds=seconds)

        measured = probe_video(target)
        if progress is not None:
            progress("SAVING", 1.0)
        return VideoResult(
            ok=True, path=target, state="COMPLETED", mode=request.mode,
            backend=self.id, model=request.model, seed=int(request.seed or 0),
            width=int(measured.get("width", width) or width),
            height=int(measured.get("height", height) or height),
            fps=float(fps), duration=float(measured.get("duration", seconds)),
            frames=int(measured.get("frames", 0) or 0),
            output_format="mp4",
            requested={"width": width, "height": height, "fps": fps,
                       "duration": seconds},
            quality={"backend": self.name, "is_ai_model": bool(self.is_model)},
            metadata={"fake": True, "is_ai_model": bool(self.is_model)},
            seconds=0.01,
            mismatch=(["the fake wrote %dx%d instead of the requested size"
                       % (2, 2)] if self.behaviour == "tiny" else []))

    def _target(self, request: VideoRequest) -> Path:
        folder = Path(request.output_dir or Path.cwd())
        folder.mkdir(parents=True, exist_ok=True)
        stem = request.name_stem or "clip"
        target = folder / f"{stem}.mp4"
        index = 1
        while target.exists():
            target = folder / f"{stem}_{index}.mp4"
            index += 1
        return target

    def _failed(self, request: VideoRequest, error: str, why: str,
                *, code: str = "FAKE_FAILED") -> VideoResult:
        return VideoResult(
            ok=False, path=None, state="FAILED", mode=request.mode,
            backend=self.id, model=request.model, error=error, why=why,
            what_to_do="Try another backend, or fix the fake's behaviour.",
            code=code, state_detail=self._state)


class FakeImageProvider:
    """Stage F's image contract, faked the same way, for adapter tests."""

    id = "fake_image"
    label = "Fake image backend"
    kind = "python"
    is_model = True

    def __init__(self, *, state: str = ProviderState.AVAILABLE,
                 reason: str = "", written: bool = True,
                 models: Optional[list] = None) -> None:
        self._state = state
        self._reason = reason
        self.written = bool(written)
        self._models = list(models or [])
        self.calls: list[dict] = []

    def status(self) -> ProviderStatus:
        return ProviderStatus(state=self._state,
                              reason=self._reason or "Fake image backend.")

    def describe(self) -> str:
        return "Fake image backend (test only)."

    def install_hint(self) -> str:
        return "Ships with the test suite."

    def settings_schema(self) -> list:
        return []

    def uses_network(self) -> bool:
        return False

    def models(self) -> list:
        return list(self._models)

    def generate(self, request: Any, *, progress: Any = None,
                 cancel: Any = None) -> Any:
        from app.image.provider import GenerationResult, ImageCapabilities

        self.calls.append(request.to_dict() if hasattr(request, "to_dict")
                          else dict(request))
        if not self.written:
            return GenerationResult(ok=False, error="The fake wrote nothing.",
                                    why="behaviour=written False",
                                    what_to_do="Turn writing on.")
        folder = Path(getattr(request, "output_dir", "") or Path.cwd())
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{getattr(request, 'name_stem', 'image')}.png"
        index = 1
        while path.exists():
            path = folder / f"{getattr(request, 'name_stem', 'image')}_{index}.png"
            index += 1
        write_png(path, size=(64, 36))
        capabilities = ImageCapabilities(text_to_image=True, image_to_image=True,
                                         upscale=True)
        return GenerationResult(ok=True, paths=[str(path)], seeds=[7],
                                backend=self.id, model="fake-image-model",
                                width=64, height=36, seconds=0.01,
                                quality={"measured_with": "PIL"},
                                capabilities=capabilities)

    def validate(self, request: Any, model: Any = None) -> list:
        return []


def make_video_request(tmp_path: Any, *, mode: str = VideoMode.TEXT_TO_VIDEO,
                       **overrides: Any) -> VideoRequest:
    """A valid request, so a test only states what it is actually testing."""
    values = dict(
        mode=mode, prompt="a test clip", duration=1.0, fps=8, width=128,
        height=96, seed=1234, output_dir=str(Path(tmp_path) / "clips"),
        name_stem="clip", quality="draft")
    values.update(overrides)
    return VideoRequest(**values)
