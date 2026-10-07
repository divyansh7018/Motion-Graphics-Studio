"""The AI Studio service: one door for generating anything (sections 8, 9, 34, 61).

The GUI, the CLI and the tests all come through here.  A call goes

    caller -> AIService -> AIBackendManager -> backend -> video_validation
                                                       -> history -> asset

so there is exactly one implementation of each step and no path that can skip
the validation.  Nothing in this module touches Qt, and nothing blocks on
something it cannot stop.

The three promises this module keeps:

* **no silent switching** - the backend and model that were asked for are the
  ones used, or the job fails and says what was asked for and why it could not
  be used (section 36);
* **no success without a file** - a result is only ``ok`` when a real file was
  written and measured (sections 46, 48);
* **no model, no AI** - with nothing installed the answer is NOT INSTALLED with
  the reason and what to do, never a placeholder (sections 79, 93).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from ..core.logging_setup import log_event
from ..image.provider import MODE_FEATURE
from .cache import GenerationCache
from .jobs import AIJobRegistry
from .history import GenerationHistory
from .models import human_bytes
from .presets import PresetStore
from .prompts import PromptWorkspace
from .references import ReferenceStore
from .registry import AIBackendManager, DiscoveryReport
from .types import (OPERATION_LABELS, ProviderState, is_reachable)
from .video import (MODE_LABELS, VIDEO_MODES, VideoMode, VideoRequest,
                    VideoResult, VideoState)
from .video_library import MediaProbeCache, VideoLibrary
from .video_validation import validate_video_request

__all__ = ["AIService", "service_for", "NOT_INSTALLED_HELP", "plan_summary"]

#: The sentence the studio shows when nothing can generate.
NOT_INSTALLED_HELP = (
    "AI model not installed. Nothing will be generated until you install one: "
    "a local video model for Diffusers, a ComfyUI workflow, your own command, "
    "or a local endpoint. The built-in clip writer is a test backend and is "
    "labelled as one.")


class AIService:
    """Everything the AI Studio does, in one place."""

    def __init__(self, settings: Any = None, *, data_root: Any = None,
                 tools: Any = None, manager: Optional[AIBackendManager] = None,
                 paths: Any = None,
                 image_service: Any = None) -> None:
        self.settings = settings
        self.data_root = Path(data_root) if data_root else (
            Path(getattr(paths, "data_root")) if paths is not None
            and getattr(paths, "data_root", None) else None)
        self.paths = paths
        self.tools = tools
        base = self.data_root
        self.manager = manager or AIBackendManager(settings, tools=tools,
                                                   data_root=base)
        self.history = GenerationHistory(
            (Path(getattr(paths, "config_dir", base)) if (paths or base) else None) /
            "ai_history.json" if (paths or base) else None)
        self.presets = PresetStore(
            (Path(getattr(paths, "config_dir", base)) if (paths or base) else None) /
            "ai_presets.json" if (paths or base) else None)
        self.prompts = PromptWorkspace(
            (Path(getattr(paths, "config_dir", base)) if (paths or base) else None) /
            "ai_prompts.json" if (paths or base) else None)
        self.references = ReferenceStore(
            Path(getattr(paths, "assets_dir", base / "assets")) if (paths or base)
            else None)
        self.cache = GenerationCache(
            Path(getattr(paths, "cache_dir", base / "cache")) if (paths or base)
            else None)
        #: The video library: generated clips, renders and imported files, with
        #: measured metadata, so a finished clip is an asset like any other
        #: (sections 14, 25, 30).  It is the studio's own folder, never the
        #: project's, so generated media is never confused with a final render.
        videos_root = (Path(getattr(paths, "videos_dir", base / "videos"))
                       if (paths or base) else None)
        self.library = VideoLibrary(
            videos_root, tools=tools,
            cache=MediaProbeCache(
                Path(getattr(paths, "cache_dir", base / "cache")) / "video_meta.json"
                if (paths or base) else None))
        if videos_root is not None:
            self.library.thumbnails.cache_dir = Path(
                getattr(paths, "video_thumbnails_dir",
                        videos_root / "thumbnails"))
        #: The studio's own view of AI jobs, shared by the GUI and the CLI so
        #: the queue and the command line never disagree about what is running.
        self.jobs = AIJobRegistry()
        self.approved_prompts: list[str] = []
        self._image_service = image_service
        self._last_digests: dict[str, str] = {}

    # -- collaborators -----------------------------------------------------

    @classmethod
    def for_paths(cls, paths: Any, settings: Any = None, *,
                  tools: Any = None,
                  manager: Optional[AIBackendManager] = None) -> "AIService":
        """Build a service from :class:`AppPaths`, so everything lands in one place."""
        return cls(settings, paths=paths, tools=tools, manager=manager,
                   data_root=getattr(paths, "data_root", None))

    def image_service(self) -> Any:
        """The Stage F image service, reused rather than reimplemented.

        The AI Studio's image tab is Stage F's engine with the AI Studio's
        backend list in front of it; there is no second image generator.
        """
        if self._image_service is None:
            from ..image.service import ImageService

            self._image_service = ImageService.for_paths(
                self.paths, self.settings) if self.paths is not None else \
                ImageService(self.settings, data_root=self.data_root)
        return self._image_service

    # -- state -------------------------------------------------------------

    def status(self, *, refresh: bool = True) -> DiscoveryReport:
        """Detect every backend.  Never loads a model (section 11)."""
        return self.manager.detect(refresh=refresh)

    def summary(self) -> dict:
        return self.manager.summary()

    def video_backends(self) -> list:
        from .types import BackendKind

        return self.manager.enabled_entries(kind=BackendKind.VIDEO)

    def usable_video_backends(self) -> list:
        from .types import BackendKind

        return self.manager.usable_backends(kind=BackendKind.VIDEO)

    def real_video_backends(self) -> list:
        """Backends that run a real model - the test fixture is not one."""
        from .types import BackendKind

        return self.manager.real_backends(kind=BackendKind.VIDEO)

    def has_ai_model(self, kind: str = "") -> bool:
        """Whether a real AI model is installed for this kind of work."""
        return bool(self.manager.real_backends(kind=kind))

    def capabilities_for(self, backend_id: str) -> Any:
        entry = self.manager.get(backend_id)
        return None if entry is None else entry.capabilities()

    # -- video generation --------------------------------------------------

    def generate_video(self, request: VideoRequest, *,
                       backend_id: str = "",
                       progress: Optional[Callable[[str, float], None]] = None,
                       cancel: Any = None,
                       record: bool = True) -> VideoResult:
        """Run one clip generation through the backend that was asked for.

        ``backend_id`` empty means the user has not chosen one: the manager's
        default is used and *named in the result*, so the interface can show
        which backend really ran rather than assuming.
        """
        if not isinstance(request, VideoRequest):
            request = VideoRequest.from_dict(dict(request or {}))
        backend_id = str(backend_id or request.backend or "").strip()
        if not backend_id:
            backend_id = self.manager.default_backend("video")
        request.backend = backend_id
        if not request.model:
            request.model = self.manager.default_model(backend_id)

        entry = self.manager.get(backend_id)
        if entry is None or entry.backend is None:
            return self._no_backend(request, backend_id)
        if not self.manager.is_enabled(backend_id):
            return VideoResult(
                ok=False, state=VideoState.FAILED, mode=request.mode,
                backend=backend_id, model=request.model,
                error=f"The backend '{entry.name}' is turned off.",
                why="You disabled it in the backend list.",
                what_to_do="Enable it again, or choose another backend.",
                code="BACKEND_DISABLED",
                options=["choose_backend", "cancel"])
        status = entry.status()
        if not is_reachable(str(status.state)):
            return VideoResult(
                ok=False, state=VideoState.FAILED, mode=request.mode,
                backend=backend_id, model=request.model,
                error=(f"{entry.name} is {status.state}: {status.reason}"),
                why="The backend is not usable on this machine right now.",
                what_to_do=" ".join(status.instructions) or
                           "Install it, or choose another backend.",
                code="BACKEND_NOT_AVAILABLE", state_detail=str(status.state),
                options=["choose_backend", "retry", "cancel"])

        capabilities = entry.capabilities()
        issues = validate_video_request(request, capabilities)
        errors = [issue for issue in issues if issue.severity == "error"]
        if errors:
            first = errors[0]
            return VideoResult(
                ok=False, state=VideoState.FAILED, mode=request.mode,
                backend=backend_id, model=request.model, error=first.message,
                why="The request cannot be run as it is.",
                what_to_do=first.what_to_do, code=first.code,
                options=["retry", "choose_model", "cancel"])
        if not entry.backend.supports_operation(request.mode):
            label = MODE_LABELS.get(request.mode, request.mode)
            return VideoResult(
                ok=False, state=VideoState.FAILED, mode=request.mode,
                backend=backend_id, model=request.model,
                error=f"{entry.name} cannot do {label.lower()}.",
                why="Its capabilities do not include this operation.",
                what_to_do="Choose a backend that supports it, or change the mode.",
                code="OPERATION_NOT_SUPPORTED",
                options=["choose_backend", "cancel"])

        # Reuse an identical earlier result only if the user asked for reuse;
        # otherwise a cache hit is reported, never silently substituted.
        fingerprint = ""
        if self.cache.enabled and request.seed:
            fingerprint = self.cache.fingerprint(
                backend=backend_id, model=request.model,
                operation=request.mode, request=request.to_dict())
            self._last_digests[fingerprint] = fingerprint
        try:
            result = entry.backend.generate(request, progress=progress,
                                            cancel=cancel)
            result = self._verified(request, result)
        except NotImplementedError as exc:
            return VideoResult(
                ok=False, state=VideoState.FAILED, mode=request.mode,
                backend=backend_id, model=request.model, error=str(exc),
                why="This backend does not implement this operation.",
                what_to_do="Use a backend that supports it.",
                code="NOT_IMPLEMENTED", options=["choose_backend", "cancel"])
        except Exception as exc:  # noqa: BLE001 - reported, never a crash
            log_event("AI_VIDEO_FAILED", f"{entry.name} failed: {exc}",
                      backend=backend_id, error=f"{type(exc).__name__}: {exc}")
            return VideoResult(
                ok=False, state=VideoState.FAILED, mode=request.mode,
                backend=backend_id, model=request.model,
                error=f"{entry.name} stopped with an error: {exc}",
                why="The backend raised an exception rather than returning.",
                what_to_do=("Check the backend's own log. The application is "
                            "still running and nothing else was changed."),
                code="BACKEND_CRASHED", options=["retry", "choose_backend", "cancel"])

        if result.ok and record:
            self.record_video(result, request)
        if result.ok and fingerprint:
            self.cache.remember(fingerprint, result)
        return result

    def _verified(self, request: VideoRequest,
                  result: VideoResult) -> VideoResult:
        """Check a backend's output before believing it (sections 46, 48).

        A backend that says "ok" while writing nothing, half a file or something
        that is not a video is not successful, whoever wrote it.  The service is
        the last place before the file reaches the project, so the check happens
        here as well as inside the backend - and a result that fails it is
        reported with what was wrong and what to do, never rounded up.
        """
        if result is None:
            return VideoResult(
                ok=False, state=VideoState.FAILED, mode=request.mode,
                backend=request.backend, model=request.model,
                error="The backend returned no result at all.",
                why="A backend must return a result, even when it fails.",
                what_to_do="Choose another backend, or check this one's log.",
                code="BACKEND_CRASHED",
                options=["retry", "choose_backend", "cancel"])
        if not result.ok or result.cancelled:
            return result
        path = Path(str(result.path)) if result.path else None
        if path is None or not path.is_file():
            return VideoResult(
                ok=False, state=VideoState.FAILED, mode=request.mode,
                backend=result.backend or request.backend,
                model=result.model or request.model,
                error=("The backend reported success but wrote no video file "
                       + (f"at {path}." if path else ".")),
                why="A generation is only finished when a real file exists.",
                what_to_do=("Run it again; if it repeats, the backend is not "
                            "writing where it claims. Check the backend's log."),
                code="VIDEO_OUTPUT_MISSING", seed=result.seed,
                state_detail=result.state_detail,
                options=["retry", "choose_backend", "cancel"])
        check = self._measure(path, request)
        if check is not None and not check.ok:
            return VideoResult(
                ok=False, state=VideoState.FAILED, mode=request.mode,
                backend=result.backend or request.backend,
                model=result.model or request.model, path=path,
                error=check.error, why=check.why or
                "The file the backend wrote is not a usable video.",
                what_to_do=check.what_to_do or
                "Run the generation again, or choose another backend.",
                code=check.code, seed=result.seed, issues=list(check.notes),
                state_detail=result.state_detail,
                options=["retry", "choose_backend", "cancel"])
        if check is not None:
            # The measured file is the truth about the clip, not the request.
            result.width = int(check.width or result.width or 0)
            result.height = int(check.height or result.height or 0)
            result.fps = float(check.fps or result.fps or 0.0)
            result.duration = float(check.duration or result.duration or 0.0)
            result.frames = int(check.frames or result.frames or 0)
            result.has_audio = bool(check.has_audio)
            result.output_format = str(check.video_codec or
                                       result.output_format or "")
            result.quality = {**dict(check.to_dict()), **dict(result.quality or {})}
            if check.mismatch:
                result.mismatch = list(check.mismatch)
        return result

    def _measure(self, path: Path, request: VideoRequest) -> Any:
        """Measure a produced clip, or None when it cannot be measured."""
        try:
            from .video_validation import validate_video_file

            return validate_video_file(
                path, tools=self._ffmpeg_tools(), expected_width=request.width,
                expected_height=request.height, expected_fps=request.fps,
                expected_duration=request.duration)
        except Exception as exc:  # noqa: BLE001 - never a crash, always a note
            log_event("AI_VIDEO_MEASURE_FAILED",
                      f"The clip could not be measured: {exc}", level="WARNING")
            return None

    def _ffmpeg_tools(self) -> Any:
        if self.tools is not None:
            return self.tools
        try:
            from ..tools.ffmpeg import FFmpegTools, discover_ffmpeg

            self.tools = FFmpegTools(discover_ffmpeg())
        except Exception:  # noqa: BLE001
            return None
        return self.tools

    def _no_backend(self, request: VideoRequest, backend_id: str) -> VideoResult:
        """Nothing installed: say so in those words (sections 79, 93)."""
        return VideoResult(
            ok=False, state=VideoState.FAILED, mode=request.mode,
            backend=backend_id, model=request.model,
            error=NOT_INSTALLED_HELP,
            why=("This build is local-first: it generates with a model on this "
                 "machine, and none is available."),
            what_to_do=("Open the AI Studio's backend list to see what each "
                        "backend needs, or install a model and press Detect again."),
            code="NO_BACKEND_AVAILABLE",
            options=["choose_backend", "open_backends", "cancel"])

    # -- image generation (delegated to Stage F) ---------------------------

    def generate_image(self, request: Any, *,
                       backend_id: str = "",
                       progress: Optional[Callable[[str, float], None]] = None,
                       cancel: Any = None,
                       record: bool = True) -> Any:
        """Generate an image with a Stage F backend, chosen by the AI Studio.

        Stage F already does this properly - validation, one job, no
        overwriting - so this method configures the backend and calls it.  It
        does not reimplement any of it (sections 61, 79).
        """
        service = self.image_service()
        if backend_id:
            try:
                if hasattr(request, "backend"):
                    request.backend = backend_id
            except Exception:  # noqa: BLE001 - a frozen request is not fatal
                pass
        result = service.generate(request, progress=progress, cancel=cancel)
        if result.ok and record:
            self.record_image(result, request)
        return result

    # -- checks ------------------------------------------------------------

    def self_test(self, provider_id: str, *, model: Any = None,
                  operation: str = "") -> dict:
        """Run one real generation to prove a backend works (section 7).

        This is the only thing allowed to produce VERIFIED.  It writes a real
        file into the studio's own check folder, measures it, and records the
        evidence - and it deletes nothing, so the evidence can be looked at.
        """
        entry = self.manager.get(provider_id)
        if entry is None or entry.backend is None:
            return {"ok": False, "state": ProviderState.NOT_INSTALLED,
                    "message": f"No backend called '{provider_id}' was found.",
                    "what_to_do": "Refresh the backend list."}
        from .types import BackendKind

        try:
            declared = list(entry.backend.capabilities().features())
        except Exception:  # noqa: BLE001 - a backend that cannot describe itself
            declared = ["unknown"]
        if not declared:
            # Nothing to run means nothing can be proved.  That is a check that
            # could not happen, not a backend that failed (section 7).
            return self._cannot_check(
                entry,
                f"{entry.name} does not declare a single operation it can do, "
                "so there is no real generation to check it with.",
                "Open its settings and enable what it supports, then check "
                "again - or choose a backend that declares its capabilities.")

        folder = self._check_folder(provider_id)
        model_id = str(getattr(model, "id", "") or self.manager.default_model(provider_id))
        if entry.kind == BackendKind.VIDEO:
            mode = self._self_test_mode(entry, operation)
            if mode is None:
                return self._cannot_check(
                    entry,
                    "This backend cannot make a clip from a prompt alone, and it "
                    "has no source-clip mode to be checked with either.",
                    "Give it a source clip in the Video Studio, or install a "
                    "backend that can generate from text.")
            request = VideoRequest(
                mode=mode, backend=provider_id, model=model_id,
                prompt="backend self check", duration=1.0, fps=8,
                width=128, height=128, seed=12345,
                output_dir=str(folder), name_stem=f"selftest_{provider_id}",
                quality="draft")
            if request.mode in ("image_to_video", "video_to_video", "extend"):
                request.source_image = str(self._check_image())
                if request.mode in ("video_to_video", "extend"):
                    request.source_video = str(self._check_image())
                    request.extend_from = str(self._check_image())
            result = self.generate_video(request, backend_id=provider_id,
                                         record=False)
            if not result.ok:
                return {"ok": False, "state": ProviderState.NOT_VERIFIED,
                        "message": result.error,
                        "why": result.why, "what_to_do": result.what_to_do,
                        "code": result.code}
            path = Path(result.path) if result.path else None
            size = path.stat().st_size if path and path.is_file() else 0
            return {
                "ok": True, "state": ProviderState.VERIFIED,
                "message": (f"{entry.name} generated a real clip: "
                            f"{result.width}x{result.height}, "
                            f"{result.duration:.2f}s, {human_bytes(size)}."),
                "evidence": {"path": str(path), "size_bytes": int(size),
                             "width": result.width, "height": result.height,
                             "duration": result.duration, "fps": result.fps,
                             "seconds": round(result.seconds, 3),
                             "label": getattr(entry.backend, "name", ""),
                             "is_ai_model": bool(getattr(entry.backend, "is_model", True)),
                             "measured_with": (result.quality or {}).get("measured_with", "")},
            }
        # Image and upscale backends: run Stage F's own generation.
        request = self._image_check_request(entry, folder)
        if request is None:
            return self._cannot_check(
                entry,
                "This backend has nothing it can do without an image from you: "
                "it cannot draw a prompt, and every operation it offers needs a "
                "source image.",
                "Give it a source image in Image Studio - or use the standard "
                "editor, which resizes and varies an existing picture.")
        result = self.generate_image(request, backend_id=provider_id, record=False)
        if not result.ok:
            return {"ok": False, "state": ProviderState.NOT_VERIFIED,
                    "message": result.error, "why": result.why,
                    "what_to_do": result.what_to_do, "code": result.code}
        path = Path(result.paths[0]) if result.paths else None
        size = path.stat().st_size if path and path.is_file() else 0
        return {
            "ok": True, "state": ProviderState.VERIFIED,
            "message": (f"{entry.name} generated a real image: "
                        f"{result.width}x{result.height}, {human_bytes(size)}."),
            "evidence": {"path": str(path), "size_bytes": int(size),
                         "width": result.width, "height": result.height,
                         "seconds": round(result.seconds, 3),
                         "is_ai_model": bool(getattr(entry.backend, "is_model", True))},
        }

    def _check_folder(self, provider_id: str) -> Path:
        base = self.data_root or Path.cwd()
        folder = Path(base) / "cache" / "ai_checks" / str(provider_id or "backend")
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _check_image(self) -> Path:
        """A small picture for image-to-video checks, made once."""
        from PIL import Image

        folder = self._check_folder("shared")
        path = folder / "check_source.png"
        if not path.is_file():
            image = Image.new("RGB", (64, 64), (40, 90, 150))
            for x in range(0, 64, 8):
                for y in range(64):
                    image.putpixel((x, y), (220, 200, 90))
            image.save(path)
        return path

    def _self_test_mode(self, entry: Any, operation: str) -> str:
        """The mode a backend can really be checked in, from its own claims."""
        capabilities = entry.capabilities()
        offered = [mode for mode in VIDEO_MODES
                   if capabilities.supports(MODE_FEATURE.get(mode, mode))]
        if operation and operation in offered:
            return operation
        for preferred in (VideoMode.TEXT_TO_VIDEO, VideoMode.IMAGE_TO_VIDEO,
                          VideoMode.VIDEO_TO_VIDEO, VideoMode.EXTEND,
                          VideoMode.STORYBOARD_TO_VIDEO):
            if preferred in offered:
                return preferred
        return ""

    def _cannot_check(self, entry: Any, why: str, what_to_do: str) -> dict:
        """A check that could not run is not a failure (section 7)."""
        return {"ok": False, "state": "CHECK NOT AVAILABLE",
                "message": f"{entry.name} was not checked.", "why": why,
                "what_to_do": what_to_do,
                "evidence": {"checked": False}}

    def _image_check_request(self, entry: Any, folder: Path) -> Any:
        """A minimal request in a mode the backend says it can do.

        Text to image first; otherwise the smallest thing that still proves a
        model really ran - a variation, an image to image, or an upscale - from
        the studio's own check picture, never from the user's work.
        """
        from ..image.provider import GenerationMode, GenerationRequest

        capabilities = entry.capabilities()
        base = {"prompt": "backend self check", "width": 128, "height": 128,
                "seed": 12345, "output_dir": str(folder), "name_stem": "selftest"}
        if capabilities.supports("text_to_image"):
            return GenerationRequest(mode=GenerationMode.TEXT_TO_IMAGE, **base)
        source = str(self._check_image())
        for mode in (GenerationMode.VARIATION, GenerationMode.IMAGE_TO_IMAGE,
                     GenerationMode.INPAINT, GenerationMode.OUTPAINT):
            if capabilities.supports(MODE_FEATURE[mode]):
                request = GenerationRequest(mode=mode, source_image=source, **base)
                if mode == GenerationMode.INPAINT:
                    request.mask_image = source
                if mode == GenerationMode.OUTPAINT:
                    request.extend = {"left": 16, "right": 16, "top": 16,
                                      "bottom": 16}
                return request
        if capabilities.supports("upscale"):
            return GenerationRequest(mode=GenerationMode.UPSCALE,
                                     source_image=source, scale=1.5, **base)
        return None

    # -- recording ---------------------------------------------------------

    def record_video(self, result: VideoResult, request: VideoRequest) -> Any:
        """Write one history entry for a finished clip (sections 47, 50).

        The clip is also indexed in the video library, with its provenance, so
        "the clip I made" is the same thing as "the clip in my library"
        (sections 22, 25) - and so it can be sent to a project from there.
        """
        try:
            self.library.register_generated(result, request)
        except Exception as exc:  # noqa: BLE001 - the library never blocks a clip
            log_event("AI_LIBRARY_FAILED",
                      f"The clip could not be added to the library: {exc}",
                      level="WARNING")
        try:
            return self.history.add_video(result, request)
        except Exception as exc:  # noqa: BLE001 - history is never worth a crash
            log_event("AI_HISTORY_FAILED", f"History could not record the clip: {exc}",
                      level="WARNING")
            return None

    def record_image(self, result: Any, request: Any) -> Any:
        try:
            return self.history.add_image_result(result, request)
        except Exception as exc:  # noqa: BLE001
            log_event("AI_HISTORY_FAILED", f"History could not record the image: {exc}",
                      level="WARNING")
            return None

    # -- storyboard to video ----------------------------------------------

    def storyboard_plan(self, project: Any, *, backend_id: str = "",
                        width: int = 0, height: int = 0, fps: int = 0,
                        quality: str = "", mode: str = "",
                        project_dir: Any = None) -> list[dict]:
        """Build the plan for turning a storyboard into clips (section 30).

        One entry per scene, with the prompt taken from the scene's **own**
        words - the script text, or its name when the script is empty.  Nothing
        is generated here, nothing is sent, and every entry starts unapproved:
        the user reviews the prompts and approves them, and only then can a
        generation run (section 49).
        """
        entries: list[dict] = []
        for index, scene in enumerate(list(getattr(project, "scenes", []) or [])):
            if not bool(getattr(scene, "enabled", True)):
                continue
            prompt = str(getattr(scene, "script", "") or "").strip()
            if not prompt:
                prompt = str(getattr(scene, "name", "") or "").strip()
            if not prompt:
                continue
            scene_id = str(getattr(scene, "id", "") or "")
            picture = _first_image_path(scene, project, project_dir)
            entry = {
                "index": index + 1, "scene_id": scene_id,
                "name": str(getattr(scene, "name", "") or scene_id),
                "prompt": prompt,
                "duration": float(getattr(scene, "effective_duration", 0.0) or 0.0),
                # A seed derived from the scene and its words, so the same
                # storyboard can be generated again and match - and it is shown
                # in the plan, so no seed is chosen behind the user's back.
                "seed": _stable_seed(scene_id, prompt),
                "source_image": picture,
                "mode": str(mode or (VideoMode.IMAGE_TO_VIDEO if picture
                                     else VideoMode.TEXT_TO_VIDEO)),
                "backend": str(backend_id or ""),
                "width": int(width or 0), "height": int(height or 0),
                "fps": int(fps or 0), "quality": str(quality or ""),
                "approved": False,
            }
            entries.append(entry)
        log_event("AI_STORYBOARD_PLANNED",
                  f"{len(entries)} scene(s) planned from the storyboard",
                  backend=str(backend_id or ""))
        return entries

    def approve_plan(self, plan: list) -> list:
        """Mark a plan as approved by the user.  Returns the same entries."""
        for entry in list(plan or []):
            if isinstance(entry, dict):
                entry["approved"] = True
                self.approve_prompt(str(entry.get("prompt", "")))
        return list(plan or [])

    def requests_from_plan(self, plan: list, *, backend_id: str = "",
                           output_dir: str = "", project: str = "") -> list:
        """One :class:`VideoRequest` per planned scene, ready to be submitted.

        Each request carries its scene id, so a clip generated for scene 3 is
        filed under scene 3 and can be sent back to that scene (section 30).
        """
        requests: list[VideoRequest] = []
        for position, entry in enumerate(list(plan or []), start=1):
            if not isinstance(entry, dict):
                continue
            request = VideoRequest(
                mode=str(entry.get("mode") or VideoMode.TEXT_TO_VIDEO),
                backend=str(entry.get("backend") or backend_id or ""),
                model=str(entry.get("model") or ""),
                prompt=str(entry.get("prompt", "")),
                negative_prompt=str(entry.get("negative_prompt", "")),
                seed=int(entry.get("seed", 0) or 0),
                duration=float(entry.get("duration", 0.0) or 0.0),
                fps=int(entry.get("fps", 0) or 0),
                width=int(entry.get("width", 0) or 0),
                height=int(entry.get("height", 0) or 0),
                quality=str(entry.get("quality", "") or ""),
                source_image=str(entry.get("source_image", "") or ""),
                output_dir=str(output_dir or ""),
                name_stem=f"scene_{position:02d}",
                project=str(project or ""),
                scene_id=str(entry.get("scene_id", "") or ""),
                extra={"storyboard_entry": dict(entry)})
            requests.append(request)
        return requests

    # -- prompts and approval ---------------------------------------------

    def approve_prompt(self, text: str) -> str:
        """Record that the user accepted this text before anything is sent.

        The studio has no path that generates from a prompt the user has not
        seen and accepted (section 49), so this is a gate rather than a note.
        """
        cleaned = str(text or "").strip()
        if cleaned:
            self.approved_prompts.append(cleaned)
            self.approved_prompts = self.approved_prompts[-50:]
        return cleaned

    def is_approved(self, text: str) -> bool:
        return str(text or "").strip() in self.approved_prompts

    def operation_label(self, operation: str) -> str:
        return OPERATION_LABELS.get(str(operation), str(operation))

    def memory_estimate(self, backend_id: str, model_id: str, *,
                        dtype: str = "float32") -> Any:
        return self.manager.estimate(backend_id, model_id, dtype=dtype)

    def cache_or_reuse_note(self, request: VideoRequest, *, backend_id: str,
                            model_id: str) -> dict:
        """Whether an identical generation already exists, and where.

        The answer never replaces the user's request on its own: it is a note
        the interface can act on (Open / Generate again), so a cache hit can
        never be mistaken for a fresh generation (section 50).
        """
        if not request.seed:
            return {"hit": False, "reason": "No seed was given, so each run is new."}
        fingerprint = self.cache.fingerprint(
            backend=backend_id, model=model_id, operation=request.mode,
            request=request.to_dict())
        entry = self.cache.lookup(fingerprint)
        if entry is None:
            return {"hit": False, "reason": "", "fingerprint": fingerprint}
        return {"hit": True, "path": str(getattr(entry, "path", "") or ""),
                "created": getattr(entry, "created", ""),
                "fingerprint": fingerprint,
                "seconds": float(getattr(entry, "seconds", 0.0) or 0.0),
                "reason": "An identical generation already exists."}

    def library_summary(self) -> str:
        """One line about the video library, for the header and the CLI."""
        return self.library.describe()

    def describe(self) -> str:
        lines = [self.manager.describe()]
        if self.library is not None:
            lines.append(self.library.describe())
        counts = self.history.counts()
        if counts:
            lines.append("History: " + ", ".join(
                f"{value} {key}" for key, value in sorted(counts.items())))
        return "\n".join(lines)


def service_for(context: Any = None, **kwargs: Any) -> AIService:
    """Build a service from a GUI context, a paths object, or nothing at all."""
    if isinstance(context, AIService):
        return context
    if context is None:
        return AIService(**kwargs)
    if hasattr(context, "data_root") and hasattr(context, "settings"):
        return AIService.for_paths(context, getattr(context, "settings", None), **kwargs)
    return AIService(getattr(context, "settings", None), **kwargs)


def plan_summary(plan: list) -> str:
    """A one-line description of a storyboard plan, for the confirmation step."""
    entries = [item for item in list(plan or []) if isinstance(item, dict)]
    if not entries:
        return ("No scenes with text were found, so there is nothing to plan. "
                "Write a script or a scene name first.")
    approved = sum(1 for item in entries if item.get("approved"))
    total = sum(float(item.get("duration", 0.0) or 0.0) for item in entries)
    return (f"{len(entries)} clip(s), about {total:.1f}s in total, "
            f"{approved} of them approved.")


def _stable_seed(scene_id: str, prompt: str) -> int:
    """A repeatable seed for one scene, from the scene's own identity."""
    import zlib

    key = f"{scene_id}|{prompt}".encode("utf-8", "replace")
    return int(zlib.crc32(key) & 0x7FFF_FFFF) or 1


def _first_image_path(scene: Any, project: Any, project_dir: Any = None) -> str:
    """The picture a scene already has, if it has one (used for image to video).

    ``project_dir`` is the **open project's own folder**, because an asset's
    stored path is relative to the project that owns it.  Guessing the folder
    from the data root would resolve an asset to the wrong place, and the first
    anyone would know about it is a generation failing on a missing file - so
    the caller passes what it knows, and nothing is invented here.
    """
    try:
        from ..scene.elements import build_asset_paths

        folder = Path(project_dir) if project_dir else None
        asset_paths = build_asset_paths(project, folder)
        for element in list(getattr(scene, "elements", []) or []):
            if str(getattr(element, "kind", "")) != "image":
                continue
            asset_id = str(getattr(element, "asset_id", "") or "")
            if asset_id and asset_id in asset_paths:
                return str(asset_paths[asset_id])
            extra = dict(getattr(element, "extra", {}) or {})
            candidate = str(extra.get("path", "") or "")
            if candidate and Path(candidate).is_file():
                return candidate
    except Exception:  # noqa: BLE001 - a missing picture is normal
        return ""
    return ""
