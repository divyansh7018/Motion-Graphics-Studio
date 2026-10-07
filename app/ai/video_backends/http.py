"""A local HTTP video server (sections 37, 63, 64).

Talks to a video-generation server the user is already running on their own
machine.  It is local-only by construction: the default endpoint is
``127.0.0.1``, a non-loopback address is refused rather than quietly used, and
the endpoint is shown in the interface so it is never a mystery where a prompt
went (section 63).

The client handles every way a server can disappoint it, each with its own
sentence: unreachable, refused, timed out, an HTTP error, a malformed body, a
body that is not a video.  There is no silent fallback - if the server fails,
the job fails and says why (section 37).

No API key is stored and no paid service is contacted (section 3).
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from ...core.logging_setup import log_event
from ...image.saving import unique_path
from ..capabilities import AICapabilities
from ..provider import ProviderStatus, SettingField
from ..types import BackendKind, DeviceRequirement, ProviderState
from ..video import (VideoModel, VideoProvider, VideoRequest, VideoResult,
                     VideoState)
from ..video_validation import validate_video_file

__all__ = ["HttpVideoBackend", "DEFAULT_ENDPOINT", "is_loopback",
           "MAX_RESPONSE_BYTES", "hex_payload"]

DEFAULT_ENDPOINT = "http://127.0.0.1:8189"

#: A server that answers with more than this is not answering with a clip.
MAX_RESPONSE_BYTES = 2048 * 1024 * 1024

#: How long to wait for the health check before calling the server absent.
CONNECT_TIMEOUT = 2.5


def is_loopback(url: str) -> bool:
    """Whether an endpoint is on this machine."""
    text = str(url or "").lower()
    return "127.0.0.1" in text or "localhost" in text or "[::1]" in text


def hex_payload(value: Any) -> str:
    """Bytes as hex, for tests and logs - never for anything secret."""
    return str(value or "").encode("utf-8").hex()


class HttpVideoBackend(VideoProvider):
    """A local HTTP video endpoint."""

    id = "http_video"
    name = "Local HTTP video endpoint"
    kind = BackendKind.VIDEO
    transport = "http"
    version = "1"
    device_requirement = DeviceRequirement.UNKNOWN
    licence = "The server you run"
    homepage = ""

    def __init__(self, endpoint: str = "", *, path: str = "/generate",
                 health_path: str = "/health",
                 capabilities: Optional[AICapabilities] = None,
                 timeout: float = 3600.0,
                 connect_timeout: float = CONNECT_TIMEOUT,
                 tools: Any = None) -> None:
        #: FFmpeg tools, for measuring what the server sent back.
        self.tools = tools
        self.endpoint = str(endpoint or "").rstrip("/")
        self.path = str(path or "/generate")
        self.health_path = str(health_path or "/health")
        self.timeout = float(timeout or 3600.0)
        self.connect_timeout = float(connect_timeout or CONNECT_TIMEOUT)
        self._capabilities = capabilities or AICapabilities(
            text_to_video=True, image_to_video=True, video_to_video=True,
            video_extend=True, reference_image=True, negative_prompt=True,
            seed_control=True, duration=True, fps=True, resolution_control=True,
            quality=True, strength=True, camera_control=True,
            camera_moves=["static", "pan", "tilt", "zoom", "dolly", "orbit",
                          "handheld", "custom"],
            max_dimension=4096, min_dimension=64, dimension_multiple=2,
            min_duration=0.5, max_duration=120.0, max_fps=60,
            notes="A local HTTP video endpoint configured by the user.")
        self._loaded = ""

    # -- description -------------------------------------------------------

    def describe(self) -> str:
        if not self.endpoint:
            return ("Talks to a clip-making server running on this machine. "
                    "No address is configured yet.")
        return (f"Sends one JSON request to {self.endpoint}{self.path} and saves "
                f"the clip it answers with.")

    def location(self) -> str:
        return self.endpoint or "(no endpoint configured)"

    def install_hint(self) -> str:
        return ("Start your local video server, then set its address in "
                f"Settings -> AI Studio (the default is {DEFAULT_ENDPOINT}).")

    def uses_network(self) -> bool:
        return True

    def settings_schema(self) -> list:
        return [
            SettingField(name="endpoint", label="Endpoint", kind="url",
                         default=DEFAULT_ENDPOINT, required=True,
                         help="Must be on this machine: 127.0.0.1 or localhost."),
            SettingField(name="path", label="Request path", kind="text",
                         default=self.path),
            SettingField(name="timeout", label="Generation timeout (seconds)",
                         kind="float", default=self.timeout,
                         help="A clip can take minutes; the request is stopped "
                              "after this long."),
        ]

    # -- discovery ---------------------------------------------------------

    def status(self) -> ProviderStatus:
        if not self.endpoint:
            return ProviderStatus(
                state=ProviderState.NOT_INSTALLED,
                reason="No local video endpoint has been configured.",
                instructions=[f"Start your server, then set {DEFAULT_ENDPOINT}."],
                device="cpu")
        if not is_loopback(self.endpoint):
            return ProviderStatus(
                state=ProviderState.NOT_SUPPORTED,
                reason=(f"'{self.endpoint}' is not on this machine. This build "
                        f"only talks to local endpoints."),
                instructions=["Use an address on this PC such as 127.0.0.1."],
                device="cpu")
        reachable, detail = self._ping()
        if not reachable:
            return ProviderStatus(
                state=ProviderState.NOT_INSTALLED,
                reason=f"The local endpoint did not answer: {detail}",
                instructions=["Start the server, then refresh the backend list."],
                device="cpu")
        return ProviderStatus(state=ProviderState.AVAILABLE,
                              reason=f"Answering at {self.endpoint}.",
                              version=detail, device="cpu")

    def capabilities(self) -> AICapabilities:
        return self._capabilities

    def models(self) -> list[VideoModel]:
        if not self.status().available:
            return []
        return [VideoModel(id="http-video-model", name="HTTP video server",
                           backend=self.id, kind="external",
                           capabilities=self._capabilities,
                           requirement=DeviceRequirement.UNKNOWN,
                           status="available",
                           notes=f"At {self.endpoint}{self.path}")]

    # -- generation --------------------------------------------------------

    def load(self, model: Any = None) -> None:
        self._loaded = str(getattr(model, "id", "") or "http-video-model")

    def unload(self) -> None:
        self._loaded = ""

    def loaded_model(self) -> str:
        return self._loaded

    def generate(self, request: VideoRequest, *,
                 progress: Optional[Callable[[str, float], None]] = None,
                 cancel: Any = None) -> VideoResult:
        started = self.started()
        report = progress or (lambda _state, _fraction: None)
        if not self.endpoint:
            return self._failed(request, started, "ENDPOINT_NOT_CONFIGURED",
                                "No local video endpoint is configured.",
                                "This backend sends your request to a server "
                                "you run yourself.",
                                "Set the address in Settings -> AI Studio.")
        if not is_loopback(self.endpoint):
            return self._failed(
                request, started, "ENDPOINT_NOT_LOCAL",
                f"'{self.endpoint}' is not on this machine.",
                "This build only talks to local endpoints, so nothing was sent.",
                "Use an address such as 127.0.0.1.")
        issues = [issue for issue in self.validate(request)
                  if issue.severity == "error"]
        if issues:
            first = issues[0]
            return self._failed(request, started, first.code, first.message,
                                "The request cannot be sent as it is.",
                                first.what_to_do)
        if cancel is not None and cancel.is_cancelled():
            return self._cancelled(request, started)

        output_dir = Path(request.output_dir or ".")
        output_dir.mkdir(parents=True, exist_ok=True)
        suffix = f".{request.output_format or 'mp4'}"
        target = unique_path(output_dir, request.name_stem or "clip", suffix)

        report("RUNNING", 0.05)
        payload = self._payload(request)
        try:
            body, content_type = self._post(payload, cancel=cancel)
        except _HttpProblem as problem:
            return self._failed(request, started, problem.code, problem.message,
                                problem.why, problem.what_to_do)
        if body is None:  # cancelled mid-request
            return self._cancelled(request, started)

        report("PROCESSING", 0.6)
        try:
            target = self._store(body, content_type, target)
        except _HttpProblem as problem:
            return self._failed(request, started, problem.code, problem.message,
                                problem.why, problem.what_to_do)

        report("SAVING", 0.9)
        check = validate_video_file(target, tools=self.tools_for_validation(),
                                    expected_width=request.width,
                                    expected_height=request.height,
                                    expected_fps=request.fps,
                                    expected_duration=request.duration)
        if check.code == "VIDEO_CHECK_NOT_AVAILABLE":
            return self._failed(
                request, started, "CLIP_UNMEASURED",
                "The server's clip was saved but FFmpeg is not available to "
                "check it.",
                "A clip that cannot be measured is not reported as a success.",
                "Install FFmpeg, then generate again.")
        if not check.ok:
            _remove_quietly(target)
            return self._failed(request, started, check.code, check.error,
                                check.why, check.what_to_do)

        log_event("AI_CLIP_WRITTEN", f"Local HTTP endpoint returned {target.name}",
                  backend=self.id, mode=request.mode, endpoint=self.endpoint)
        seed = int(request.seed or 0)
        result = VideoResult(
            ok=True, path=target, state=VideoState.COMPLETED, seed=seed,
            model=request.model or "http-video-model", backend=self.id,
            mode=request.mode, width=check.width, height=check.height,
            fps=check.fps, duration=check.duration, frames=check.frames,
            output_format=request.output_format or "mp4",
            has_audio=check.has_audio, requested=request.to_dict(),
            seconds=self.started() - started, mismatch=list(check.mismatch))
        result.quality = {
            "model": result.model, "backend": f"{self.id} ({self.endpoint})",
            "resolution": f"{result.width}x{result.height}",
            "fps": round(result.fps, 3), "duration": round(result.duration, 3),
            "seed": seed, "size_bytes": check.size_bytes,
            "measured_with": check.measured_with, "status": "measured"}
        result.metadata = {
            "prompt": request.prompt, "negative_prompt": request.negative_prompt,
            "seed": seed, "mode": request.mode, "camera": request.camera,
            "endpoint": self.endpoint, "generator": "local-http",
            "is_ai_model": None,
        }
        return result

    # -- HTTP --------------------------------------------------------------

    def tools_for_validation(self) -> Any:
        """The FFmpeg tools used to measure a returned clip.

        Kept as a method so a test can hand in a tools object without this
        backend having to discover FFmpeg itself.
        """
        tools = getattr(self, "tools", None)
        if tools is not None:
            return tools
        from ...tools.ffmpeg import FFmpegTools, discover_ffmpeg

        self.tools = FFmpegTools(discover_ffmpeg())
        return self.tools

    def _ping(self) -> tuple[bool, str]:
        """A short health request, so a dead server is found before a job starts."""
        url = f"{self.endpoint}{self.health_path}"
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.connect_timeout) as response:
                body = response.read(4096).decode("utf-8", "replace")
                return True, body.strip()[:120] or f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            # A server that answers 404 for /health is still a server.
            if exc.code in (404, 405):
                return True, f"HTTP {exc.code} (no health endpoint)"
            return False, f"HTTP {exc.code}"
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            return False, str(getattr(exc, "reason", exc))[:160]

    def _payload(self, request: VideoRequest) -> dict:
        data = request.to_dict()
        return {
            "mode": request.mode, "prompt": request.prompt,
            "negative_prompt": request.negative_prompt, "model": request.model,
            "seed": int(request.seed or 0), "duration": float(request.duration or 0.0),
            "fps": int(request.fps or 0), "width": int(request.width or 0),
            "height": int(request.height or 0),
            "strength": float(request.strength or 0.0),
            "camera": request.camera, "camera_amount": float(request.camera_amount or 0.0),
            "source_image": _as_data_url(request.source_image),
            "source_video": str(request.source_video or ""),
            "reference_image": _as_data_url(request.reference_image),
            "extend_from": str(request.extend_from or ""),
            "scene_id": request.scene_id, "settings": data,
        }

    def _post(self, payload: dict, *, cancel: Any = None) -> tuple[bytes, str]:
        """Send one request and return its body, with every failure named."""
        url = f"{self.endpoint}{self.path}"
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "video/*, application/json"})
        # Cancellation: urllib has no cancel, so a short timeout plus a check
        # between attempts is what stops a cancelled job waiting forever.
        timeout = min(self.timeout, 30.0) if cancel is not None else self.timeout
        attempts = 0
        deadline = time.monotonic() + max(1.0, self.timeout)
        while True:
            if cancel is not None and cancel.is_cancelled():
                return None, ""  # type: ignore[return-value]
            attempts += 1
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    content_type = str(response.headers.get("Content-Type", ""))
                    body = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise _HttpProblem(
                            "SERVER_RESPONSE_TOO_LARGE",
                            "The server sent more data than a clip can be.",
                            "The response was cut off rather than filling memory.",
                            "Check the server's own logs; it may be sending an error page.")
                    return body, content_type
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read(600).decode("utf-8", "replace").strip()
                except Exception:  # noqa: BLE001 - the body is a bonus, not the error
                    pass
                raise _HttpProblem(
                    "SERVER_HTTP_ERROR",
                    f"The server answered HTTP {exc.code}.",
                    detail or "The server rejected the request.",
                    "Check the server's own log; the request itself was well formed.")
            except socket.timeout:
                if cancel is not None and time.monotonic() < deadline:
                    continue
                raise _HttpProblem(
                    "SERVER_TIMEOUT",
                    f"The server did not answer within {self.timeout:.0f} seconds.",
                    "The request was stopped so the application would not hang.",
                    "Raise the timeout, or ask the server for a shorter clip.")
            except urllib.error.URLError as exc:
                reason = str(getattr(exc, "reason", exc))
                if "timed out" in reason.lower() and cancel is not None \
                        and time.monotonic() < deadline:
                    continue
                raise _HttpProblem(
                    "SERVER_UNREACHABLE",
                    f"The server at {self.endpoint} could not be reached: {reason}",
                    "Nothing was generated, and nothing was sent anywhere else.",
                    "Start the server, or correct the address in Settings.")

    def _store(self, body: bytes, content_type: str, target: Path) -> Path:
        """Turn the server's answer into a file, or explain why it is not one."""
        lowered = content_type.lower()
        if "json" in lowered or (body[:1] in (b"{", b"[") and "video" not in lowered):
            try:
                payload = json.loads(body.decode("utf-8", "replace"))
            except ValueError as exc:
                raise _HttpProblem(
                    "SERVER_MALFORMED_RESPONSE",
                    "The server's answer was not valid JSON.",
                    str(exc),
                    "Check the server: it is not answering in the documented shape.")
            path = str((payload or {}).get("path") or
                       (payload or {}).get("video") or "").strip()
            if not path:
                message = str((payload or {}).get("error") or "").strip()
                raise _HttpProblem(
                    "SERVER_NO_OUTPUT",
                    ("The server answered with no clip"
                     + (f": {message}" if message else ".")),
                    "The response contained no path to a video file.",
                    "Check the server's log; the request reached it.")
            source = Path(path)
            if not source.is_file():
                # A path on the server is only usable when it is on this
                # machine, which for a local server it is.
                raise _HttpProblem(
                    "SERVER_OUTPUT_MISSING",
                    f"The server said the clip is at {source}, but that file is "
                    f"not there.",
                    "The server reported a path this application cannot read.",
                    "Check that the server runs on this machine and writes "
                    "where it says.")
            import shutil

            shutil.copyfile(source, target)
            return target
        if not body:
            raise _HttpProblem("SERVER_EMPTY_RESPONSE",
                               "The server answered with nothing at all.",
                               "An empty response is not a clip.",
                               "Check the server's log.")
        target.write_bytes(body)
        return target

    # -- internals ---------------------------------------------------------

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


class _HttpProblem(Exception):
    """One HTTP failure, with what happened / why / what to do."""

    def __init__(self, code: str, message: str, why: str, what_to_do: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.why = why
        self.what_to_do = what_to_do


def _as_data_url(path: Any) -> str:
    """A local image as a data URL, or an empty string when there is none.

    Small images only: a clip request must not carry a 40 MB PNG, and a server
    on this machine can read a path instead.
    """
    text = str(path or "").strip()
    if not text:
        return ""
    source = Path(text)
    if not source.is_file():
        return ""
    if source.stat().st_size > 4 * 1024 * 1024:
        return text  # a path is better than a huge payload
    suffix = source.suffix.lower().lstrip(".") or "png"
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp",
            "bmp": "bmp"}.get(suffix, "png")
    import base64

    encoded = base64.b64encode(source.read_bytes()).decode("ascii")
    return f"data:image/{mime};base64,{encoded}"


def _remove_quietly(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:  # pragma: no cover
        pass
