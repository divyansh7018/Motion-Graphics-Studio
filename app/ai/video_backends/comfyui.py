"""ComfyUI as a video backend (sections 38, 64).

ComfyUI is the most common way people run local video models, so it gets a
real adapter rather than a promise: a workflow file is loaded, the request is
mapped onto the workflow's own named inputs, the run is submitted and polled,
and the produced clip is copied back and measured.

Everything the directive asks to see is here and visible in the interface:
the endpoint, the workflow being used, the parameter mapping, where the output
was discovered, the timeout, and a cancel that stops the polling and asks
ComfyUI to interrupt the run.

Nothing runs until a workflow is chosen, and nothing is downloaded.  When no
workflow is configured the backend says so instead of guessing (section 64).
"""

from __future__ import annotations

import json
import shutil
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
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
from .http import _HttpProblem, is_loopback

__all__ = ["ComfyUIVideoBackend", "DEFAULT_ENDPOINT", "PARAMETER_MAP",
           "OUTPUT_KEYS", "DEFAULT_POLL_INTERVAL"]

DEFAULT_ENDPOINT = "http://127.0.0.1:8188"

#: How long to wait between polls of the run's status.
DEFAULT_POLL_INTERVAL = 1.0

#: Request field -> the names a workflow node may use for it.  First match wins,
#: and the match is reported so the mapping is never a secret (section 38).
PARAMETER_MAP: dict[str, tuple[str, ...]] = {
    "prompt": ("text", "prompt", "positive", "positive_prompt", "caption"),
    "negative_prompt": ("negative", "negative_prompt", "neg"),
    "seed": ("seed", "noise_seed", "rand_seed"),
    "steps": ("steps", "num_steps", "sampler_steps"),
    "guidance": ("cfg", "cfg_scale", "guidance", "guidance_scale"),
    "sampler": ("sampler_name", "sampler", "scheduler"),
    "width": ("width", "W", "video_width"),
    "height": ("height", "H", "video_height"),
    "duration": ("duration", "length", "frames", "video_length", "num_frames"),
    "fps": ("fps", "frame_rate", "video_fps"),
    "strength": ("strength", "denoise", "denoising_strength"),
    "model": ("model", "ckpt_name", "unet_name", "model_name"),
    "camera": ("camera", "camera_move", "motion"),
    "batch": ("batch_size", "batch", "num_videos"),
}

#: Where a finished run's files are described, in the order they are looked for.
OUTPUT_KEYS: tuple[str, ...] = (
    "outputs", "files", "videos", "images", "gifs", "results",
)


class ComfyUIVideoBackend(VideoProvider):
    """A local ComfyUI server, driven by the user's own workflow."""

    id = "comfyui_video"
    name = "ComfyUI (local video workflow)"
    kind = BackendKind.VIDEO
    transport = "comfyui"
    version = "1"
    device_requirement = DeviceRequirement.GPU_RECOMMENDED
    licence = "ComfyUI (open source) and the models you install"
    homepage = "https://github.com/comfyanonymous/ComfyUI"

    def __init__(self, endpoint: str = "", workflow: str = "", *,
                 timeout: float = 3600.0,
                 poll_interval: float = DEFAULT_POLL_INTERVAL,
                 capabilities: Optional[AICapabilities] = None,
                 tools: Any = None) -> None:
        self.endpoint = str(endpoint or "").rstrip("/")
        self.workflow = str(workflow or "")
        self.tools = tools
        self.timeout = float(timeout or 3600.0)
        self.poll_interval = max(0.05, float(poll_interval or DEFAULT_POLL_INTERVAL))
        self.tools = tools
        self._capabilities = capabilities or AICapabilities(
            text_to_video=True, image_to_video=True, video_to_video=True,
            video_extend=True, reference_image=True, negative_prompt=True,
            seed_control=True, steps=True, guidance=True, sampler=True,
            duration=True, fps=True, resolution_control=True, quality=True,
            strength=True, camera_control=True, batch=True, max_batch=4,
            camera_moves=["static", "pan", "tilt", "zoom", "dolly", "orbit",
                          "handheld", "custom"],
            max_dimension=4096, min_dimension=64, dimension_multiple=8,
            min_duration=0.5, max_duration=300.0, max_fps=60,
            notes="Whatever the chosen ComfyUI workflow can do.")
        self._loaded = ""
        self._client_id = uuid.uuid4().hex
        self.last_mapping: dict = {}

    # -- description -------------------------------------------------------

    def describe(self) -> str:
        if not self.endpoint:
            return ("Runs one of your ComfyUI workflows on this machine. Set the "
                    "address and choose a workflow first.")
        workflow = Path(self.workflow).name if self.workflow else "(none chosen)"
        return (f"Submits the workflow '{workflow}' to {self.endpoint} and copies "
                f"the clip it produces. The workflow's own node names decide "
                f"which settings are used.")

    def location(self) -> str:
        if not self.endpoint:
            return "(no endpoint configured)"
        if self.workflow:
            return f"{self.endpoint}\nWorkflow: {self.workflow}"
        return f"{self.endpoint}\nWorkflow: none chosen"

    def install_hint(self) -> str:
        return ("Start ComfyUI on this machine, export a workflow that makes a "
                f"video (API format), and set both in Settings -> AI Studio. "
                f"The default address is {DEFAULT_ENDPOINT}.")

    def uses_network(self) -> bool:
        return True

    def settings_schema(self) -> list:
        return [
            SettingField(name="endpoint", label="ComfyUI address", kind="url",
                         default=DEFAULT_ENDPOINT, required=True,
                         help="Must be on this machine: 127.0.0.1 or localhost."),
            SettingField(name="workflow", label="Workflow file", kind="path",
                         default="", required=True,
                         help="The API-format workflow JSON exported from ComfyUI."),
            SettingField(name="timeout", label="Timeout (seconds)", kind="float",
                         default=self.timeout,
                         help="The run is stopped after this long."),
        ]

    # -- discovery ---------------------------------------------------------

    def status(self) -> ProviderStatus:
        if not self.endpoint:
            return ProviderStatus(
                state=ProviderState.NOT_INSTALLED,
                reason="No ComfyUI address has been configured.",
                instructions=[f"Start ComfyUI, then set {DEFAULT_ENDPOINT}."],
                device="gpu")
        if not is_loopback(self.endpoint):
            return ProviderStatus(
                state=ProviderState.NOT_SUPPORTED,
                reason=(f"'{self.endpoint}' is not on this machine. This build "
                        f"only talks to local endpoints."),
                instructions=["ComfyUI must be running on this PC."],
                device="gpu")
        reachable, detail = self._ping()
        if not reachable:
            return ProviderStatus(
                state=ProviderState.NOT_INSTALLED,
                reason=f"ComfyUI did not answer at {self.endpoint}: {detail}",
                instructions=["Start ComfyUI, then refresh the backend list."],
                device="gpu")
        if not self.workflow:
            return ProviderStatus(
                state=ProviderState.LIMITED,
                reason=(f"ComfyUI is answering at {self.endpoint}, but no "
                        f"workflow has been chosen, so there is nothing to run."),
                instructions=["Choose a workflow file in Settings -> AI Studio."],
                device="gpu")
        if not Path(self.workflow).is_file():
            return ProviderStatus(
                state=ProviderState.LIMITED,
                reason=(f"The workflow file was not found: {self.workflow}"),
                instructions=["Choose the workflow file again - it may have moved."],
                device="gpu")
        try:
            count = len(self._load_workflow())
        except _HttpProblem as problem:
            return ProviderStatus(state=ProviderState.NOT_VERIFIED,
                                  reason=problem.message,
                                  instructions=[problem.what_to_do],
                                  device="gpu")
        return ProviderStatus(
            state=ProviderState.AVAILABLE,
            reason=(f"ComfyUI answering at {self.endpoint}; workflow has {count} "
                    f"node(s)."),
            version=detail, device="gpu")

    def capabilities(self) -> AICapabilities:
        return self._capabilities

    def models(self) -> list[VideoModel]:
        if not self.endpoint:
            return []
        name = Path(self.workflow).name if self.workflow else "ComfyUI workflow"
        return [VideoModel(
            id=Path(self.workflow).stem if self.workflow else "comfyui-workflow",
            name=name, backend=self.id, kind="workflow",
            path=self.workflow, capabilities=self._capabilities,
            requirement=DeviceRequirement.GPU_RECOMMENDED,
            status="available" if self.workflow else "not_configured",
            notes=("Your own workflow. The settings it exposes are the ones "
                   "mapped from the request."))]

    # -- generation --------------------------------------------------------

    def load(self, model: Any = None) -> None:
        self._loaded = str(getattr(model, "id", "") or
                           (Path(self.workflow).stem if self.workflow else ""))

    def unload(self) -> None:
        self._loaded = ""

    def loaded_model(self) -> str:
        return self._loaded

    def generate(self, request: VideoRequest, *,
                 progress: Optional[Callable[[str, float], None]] = None,
                 cancel: Any = None) -> VideoResult:
        started = self.started()
        report = progress or (lambda _state, _fraction: None)
        issues = [issue for issue in self.validate(request)
                  if issue.severity == "error"]
        if not self.workflow:
            return self._failed(request, started, "WORKFLOW_NOT_CHOSEN",
                                "No ComfyUI workflow has been chosen.",
                                "This backend runs a workflow you pick, and none "
                                "is set.",
                                "Choose an API-format workflow file in "
                                "Settings -> AI Studio.")
        if not self.endpoint:
            return self._failed(request, started, "ENDPOINT_NOT_CONFIGURED",
                                "No ComfyUI address has been configured.",
                                "There is no server to send the workflow to.",
                                "Set the address in Settings -> AI Studio.")
        if issues:
            first = issues[0]
            return self._failed(request, started, first.code, first.message,
                                "The request cannot be run as it is.",
                                first.what_to_do)
        if cancel is not None and cancel.is_cancelled():
            return self._cancelled(request, started)

        output_dir = Path(request.output_dir or ".")
        output_dir.mkdir(parents=True, exist_ok=True)
        suffix = f".{request.output_format or 'mp4'}"
        target = unique_path(output_dir, request.name_stem or "clip", suffix)
        seed = int(request.seed or 0) or self._fresh_seed(request)

        try:
            workflow = self._load_workflow()
        except _HttpProblem as problem:
            return self._failed(request, started, problem.code, problem.message,
                                problem.why, problem.what_to_do)
        mapped, mapping = self._map_parameters(workflow, request, seed=seed)
        self.last_mapping = mapping
        if not mapping:
            return self._failed(
                request, started, "WORKFLOW_NO_MATCHING_INPUT",
                "None of this workflow's inputs matched the request.",
                "The workflow has no writable field the application can map "
                "onto. Nothing was submitted.",
                "Check that the workflow is in API format and has a text node "
                "with a normal name such as 'text' or 'prompt'.")

        report("RUNNING", 0.05)
        try:
            prompt_id = self._submit(mapped)
        except _HttpProblem as problem:
            return self._failed(request, started, problem.code, problem.message,
                                problem.why, problem.what_to_do)

        try:
            outputs = self._wait(prompt_id, cancel=cancel, report=report)
        except _HttpProblem as problem:
            return self._failed(request, started, problem.code, problem.message,
                                problem.why, problem.what_to_do)
        if outputs is None:
            self._interrupt()
            return self._cancelled(request, started)

        item = self._first_video(outputs)
        if item is None:
            return self._failed(
                request, started, "WORKFLOW_NO_VIDEO_OUTPUT",
                "The workflow finished but produced no video file.",
                "ComfyUI saved no clip this application could find.",
                "Check the workflow's save node - it must write an mp4, webm or "
                "mov file.")
        report("PROCESSING", 0.85)
        try:
            self._download(item, target)
        except _HttpProblem as problem:
            return self._failed(request, started, problem.code, problem.message,
                                problem.why, problem.what_to_do)

        report("SAVING", 0.95)
        check = validate_video_file(target, tools=self.tools,
                                    expected_width=request.width,
                                    expected_height=request.height,
                                    expected_fps=request.fps,
                                    expected_duration=request.duration)
        if check.code == "VIDEO_CHECK_NOT_AVAILABLE":
            return self._failed(
                request, started, "CLIP_UNMEASURED",
                "The workflow's clip was saved but FFmpeg is not available to "
                "check it.",
                "A clip that cannot be measured is not reported as a success.",
                "Install FFmpeg, then run the workflow again.")
        if not check.ok:
            _remove_quietly(target)
            return self._failed(request, started, check.code, check.error,
                                check.why, check.what_to_do)

        log_event("AI_CLIP_WRITTEN", f"ComfyUI workflow produced {target.name}",
                  backend=self.id, prompt_id=prompt_id,
                  workflow=Path(self.workflow).name)
        result = VideoResult(
            ok=True, path=target, state=VideoState.COMPLETED, seed=seed,
            model=request.model or Path(self.workflow).stem, backend=self.id,
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
            "endpoint": self.endpoint, "workflow": self.workflow,
            "prompt_id": prompt_id, "parameter_mapping": mapping,
            "generator": "comfyui", "is_ai_model": None,
        }
        return result

    # -- ComfyUI protocol --------------------------------------------------

    def _ping(self) -> tuple[bool, str]:
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(f"{self.endpoint}/system_stats"),
                    timeout=2.5) as response:
                body = response.read(2048).decode("utf-8", "replace")
                try:
                    payload = json.loads(body)
                    version = str((payload.get("system") or {}).get("comfyui_version")
                                  or "")
                except ValueError:
                    version = ""
                return True, version or f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 405):
                return True, f"HTTP {exc.code} (no /system_stats)"
            return False, f"HTTP {exc.code}"
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            return False, str(getattr(exc, "reason", exc))[:160]

    def _load_workflow(self) -> dict:
        path = Path(self.workflow)
        if not path.is_file():
            raise _HttpProblem(
                "WORKFLOW_MISSING",
                f"The workflow file was not found: {path}",
                "There is nothing to submit to ComfyUI.",
                "Choose the workflow file again in Settings -> AI Studio.")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise _HttpProblem(
                "WORKFLOW_MALFORMED",
                f"The workflow file could not be read as JSON: {exc}",
                "ComfyUI needs the API-format workflow, which is JSON.",
                "Export the workflow again with 'Save (API Format)'.")
        except OSError as exc:
            raise _HttpProblem("WORKFLOW_UNREADABLE",
                               f"The workflow file could not be read: {exc}",
                               "The file or its folder is not readable.",
                               "Check the file's permissions.")
        if not isinstance(payload, dict):
            raise _HttpProblem(
                "WORKFLOW_WRONG_SHAPE",
                "The workflow file does not contain a workflow object.",
                "It is JSON, but not the shape ComfyUI expects.",
                "Export the workflow again with 'Save (API Format)'.")
        # Some exports wrap the nodes under a "prompt" key.
        if "prompt" in payload and isinstance(payload["prompt"], dict):
            return dict(payload["prompt"])
        return dict(payload)

    @staticmethod
    def _node_inputs(workflow: dict) -> list[tuple[str, str, dict]]:
        """Every writable input in the workflow: (node id, field, node)."""
        found: list[tuple[str, str, dict]] = []
        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs")
            if isinstance(inputs, dict):
                for field in inputs:
                    found.append((str(node_id), str(field), node))
        return found

    def _map_parameters(self, workflow: dict, request: VideoRequest,
                        *, seed: int) -> tuple[dict, dict]:
        """Work the request into the workflow's own field names."""
        source = {
            "prompt": str(request.prompt or ""),
            "negative_prompt": str(request.negative_prompt or ""),
            "seed": int(seed),
            "steps": int(request.steps or 0),
            "guidance": float(request.guidance or 0.0),
            "sampler": str(request.sampler or ""),
            "width": int(request.width or 0), "height": int(request.height or 0),
            "duration": float(request.duration or 0.0),
            "fps": int(request.fps or 0),
            "strength": float(request.strength or 0.0),
            "model": str(request.model or ""), "camera": str(request.camera or ""),
            "batch": int(request.batch or 1),
        }
        mapping: dict = {}
        mapped_workflow = json.loads(json.dumps(workflow))
        for field, value in source.items():
            if value in ("", 0, 0.0) and field not in ("prompt",):
                continue
            candidates = PARAMETER_MAP.get(field, ())
            if field == "duration":
                # A workflow that counts frames wants frames, not seconds.
                fps = int(request.fps or 24)
                target_field = None
                target_node = None
                for node_id, name, node in self._node_inputs(mapped_workflow):
                    lowered = name.lower()
                    if lowered in ("frames", "num_frames", "video_length"):
                        target_field, target_node = name, node
                        break
                if target_field and target_node is not None:
                    target_node["inputs"][target_field] = int(
                        round(float(value) * fps))
                    mapping[field] = {"node": "frames", "field": target_field,
                                      "value": int(round(float(value) * fps))}
                    continue
            for node_id, name, node in self._node_inputs(mapped_workflow):
                if name.lower() not in candidates:
                    continue
                # Only replace values the workflow exposes writably; a link
                # (a list) means the input is driven by another node.
                current = node["inputs"].get(name)
                if isinstance(current, list):
                    continue
                node["inputs"][name] = value
                mapping[field] = {"node": node_id, "field": name, "value": value}
                break
        return mapped_workflow, mapping

    def _submit(self, workflow: dict) -> str:
        payload = {"prompt": workflow, "client_id": self._client_id}
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.endpoint}/prompt", data=body, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30.0) as response:
                answer = json.loads(response.read(1_000_000).decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(1200).decode("utf-8", "replace").strip()
            except Exception:  # noqa: BLE001
                pass
            raise _HttpProblem(
                "COMFYUI_REJECTED",
                f"ComfyUI refused the workflow (HTTP {exc.code}).",
                detail[:600] or "The server rejected the workflow.",
                "Check the workflow in ComfyUI itself; a missing model or an "
                "unknown node is the usual cause.")
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            raise _HttpProblem(
                "COMFYUI_UNREACHABLE",
                f"ComfyUI could not be reached: {getattr(exc, 'reason', exc)}",
                "The workflow was not submitted.",
                "Start ComfyUI, then try again.")
        except ValueError as exc:
            raise _HttpProblem("COMFYUI_BAD_ANSWER",
                               f"ComfyUI's answer was not valid JSON: {exc}",
                               "The server is not answering in its documented shape.",
                               "Check the ComfyUI version and its log.")
        prompt_id = str((answer or {}).get("prompt_id") or "").strip()
        if not prompt_id:
            raise _HttpProblem(
                "COMFYUI_NO_JOB_ID",
                "ComfyUI accepted the workflow but returned no job id.",
                "Without a job id the run cannot be followed.",
                "Check the ComfyUI log; the workflow may have failed at once.")
        return prompt_id

    def _wait(self, prompt_id: str, *, cancel: Any,
              report: Callable[[str, float], None]) -> Optional[dict]:
        """Poll until the run finishes.  ``None`` means it was cancelled."""
        deadline = time.monotonic() + max(1.0, self.timeout)
        while True:
            if cancel is not None and cancel.is_cancelled():
                return None
            if time.monotonic() > deadline:
                raise _HttpProblem(
                    "COMFYUI_TIMEOUT",
                    f"ComfyUI did not finish within {self.timeout:.0f} seconds.",
                    "The run was abandoned so the application would not hang "
                    "for ever.",
                    "Ask for a shorter clip, or raise the timeout in Settings.")
            try:
                with urllib.request.urlopen(
                        urllib.request.Request(
                            f"{self.endpoint}/history/{prompt_id}"),
                        timeout=15.0) as response:
                    history = json.loads(response.read(4_000_000).decode("utf-8", "replace"))
            except urllib.error.HTTPError as exc:
                if exc.code == 404:  # not finished yet on some versions
                    time.sleep(self.poll_interval)
                    continue
                raise _HttpProblem("COMFYUI_HISTORY_ERROR",
                                   f"ComfyUI answered HTTP {exc.code} while "
                                   f"checking the run.",
                                   "The run's progress could not be read.",
                                   "Check the ComfyUI log.")
            except (urllib.error.URLError, socket.timeout, OSError) as exc:
                raise _HttpProblem(
                    "COMFYUI_UNREACHABLE",
                    f"ComfyUI stopped answering: {getattr(exc, 'reason', exc)}",
                    "The connection to the local server was lost.",
                    "Check that ComfyUI is still running.")
            except ValueError:
                time.sleep(self.poll_interval)
                continue
            entry = (history or {}).get(prompt_id)
            if entry:
                status = entry.get("status") or {}
                if str(status.get("status_str", "")).lower() == "error" or \
                        status.get("completed") is False and status.get("messages"):
                    messages = status.get("messages") or []
                    detail = ""
                    for item in messages:
                        if isinstance(item, list) and len(item) > 1 \
                                and str(item[0]) == "execution_error":
                            payload = item[1] if isinstance(item[1], dict) else {}
                            detail = str(payload.get("exception_message") or "")
                    raise _HttpProblem(
                        "COMFYUI_RUN_FAILED",
                        "ComfyUI reported that the workflow failed.",
                        detail or "The server returned an execution error.",
                        "Open the workflow in ComfyUI to see the failed node.")
                return dict(entry.get("outputs") or {})
            # Progress, when the server offers it; the percentage stays inside
            # the range the job is actually in (section 33).
            fraction = 0.05 + min(0.75, (time.monotonic() - (deadline - self.timeout))
                                  / max(1.0, self.timeout) * 0.75)
            report("RUNNING", fraction)
            time.sleep(self.poll_interval)

    def _interrupt(self) -> None:
        """Ask ComfyUI to stop the run.  Failure here is reported, not raised."""
        try:
            request = urllib.request.Request(f"{self.endpoint}/interrupt",
                                             data=b"{}", method="POST",
                                             headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=5.0):
                pass
        except Exception as exc:  # noqa: BLE001 - cancellation must not fail
            log_event("AI_CANCEL_INTERRUPT_FAILED",
                      f"ComfyUI interrupt could not be sent: {exc}",
                      level="WARNING")

    @staticmethod
    def _first_video(outputs: dict) -> Optional[dict]:
        """The first video file a finished run describes."""
        for node in (outputs or {}).values():
            if not isinstance(node, dict):
                continue
            for key in OUTPUT_KEYS:
                for item in node.get(key) or []:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("filename") or "")
                    if name.lower().endswith((".mp4", ".webm", ".mov", ".mkv", ".gif")):
                        return item
        return None

    def _download(self, item: dict, target: Path) -> None:
        query = urllib.parse.urlencode({
            "filename": item.get("filename", ""),
            "subfolder": item.get("subfolder", ""),
            "type": item.get("type", "output"),
        })
        url = f"{self.endpoint}/view?{query}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url),
                                        timeout=120.0) as response:
                with open(target, "wb") as handle:
                    shutil.copyfileobj(response, handle, length=1024 * 1024)
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            _remove_quietly(target)
            raise _HttpProblem(
                "COMFYUI_DOWNLOAD_FAILED",
                f"The produced clip could not be copied back: "
                f"{getattr(exc, 'reason', exc)}",
                "ComfyUI finished the run but the file did not transfer.",
                "Check free space in the output folder, then try again.")

    @staticmethod
    def _fresh_seed(request: VideoRequest) -> int:
        import hashlib

        raw = f"{request.prompt}|{request.mode}|{time.time()}".encode("utf-8")
        return int.from_bytes(hashlib.sha256(raw).digest()[:4], "big") or 1

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


def _remove_quietly(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:  # pragma: no cover
        pass
