"""Local HTTP endpoint backend (Stage F, sections 2, 67).

Talks to an image server the user is already running on their own machine -
ComfyUI, Automatic1111, or anything that answers a simple JSON request.  It is
local-only by construction: the default endpoint is ``127.0.0.1`` and a
non-loopback address has to be typed in explicitly, so the application never
sends a prompt somewhere the user did not ask it to.

No API keys are stored and no paid service is contacted.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from ...core.logging_setup import log_event
from ..capabilities import ImageCapabilities
from ..provider import (GenerationRequest, GenerationResult, GenerationState,
                        ImageModel, ImageProvider, ProviderStatus)
from ..validation import validate_request
from ..saving import unique_path
from .base import cancelled_result, failed_result, seed_from_request, verify_output

__all__ = ["HttpBackend", "DEFAULT_ENDPOINT", "is_loopback"]

DEFAULT_ENDPOINT = "http://127.0.0.1:8188"


def is_loopback(url: str) -> bool:
    """Whether an endpoint is on this machine."""
    text = str(url or "").lower()
    return ("127.0.0.1" in text or "localhost" in text or "[::1]" in text)


class HttpBackend(ImageProvider):
    """A local HTTP image endpoint."""

    id = "http"
    label = "Local HTTP endpoint"
    kind = "http"

    def __init__(self, endpoint: str = "", *, path: str = "/generate",
                 capabilities: Optional[ImageCapabilities] = None,
                 timeout: float = 1800.0) -> None:
        self.endpoint = str(endpoint or "").rstrip("/")
        self.path = str(path or "/generate")
        self.timeout = float(timeout or 1800.0)
        self._capabilities = capabilities or ImageCapabilities(
            text_to_image=True, image_to_image=True, inpaint=True,
            outpaint=True, upscale=True, reference_image=True,
            negative_prompt=True, seed_control=True, steps=True, guidance=True,
            sampler=True, batch=True, strength=True, max_batch=8,
            notes="A local HTTP endpoint configured by the user.")

    # -- discovery ---------------------------------------------------------

    def status(self) -> ProviderStatus:
        if not self.endpoint:
            return ProviderStatus(
                available=False, state="not_installed",
                reason="No local image endpoint has been configured.",
                instructions=[
                    "Start your local image server, then set its address in "
                    "Settings -> Image Studio.",
                    f"The default is {DEFAULT_ENDPOINT}.",
                ])
        if not is_loopback(self.endpoint):
            return ProviderStatus(
                available=False, state="not_supported",
                reason=(f"'{self.endpoint}' is not on this machine, and Stage F "
                        "is local-only."),
                instructions=[
                    "Use an address on this PC such as 127.0.0.1 or localhost.",
                    "Remote and paid image services are not supported.",
                ])
        reachable, detail = self._ping()
        if not reachable:
            return ProviderStatus(
                available=False, state="not_installed",
                reason=f"The local endpoint did not answer: {detail}",
                instructions=["Start the server, then open Image Studio again."])
        return ProviderStatus(available=True, state="available",
                              reason=f"Answering at {self.endpoint}.",
                              version=detail)

    def _ping(self) -> tuple[bool, str]:
        url = f"{self.endpoint}/"
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return True, f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            # Any HTTP answer at all means something is listening.
            return True, f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return False, str(getattr(exc, "reason", exc))[:200]

    def capabilities(self) -> ImageCapabilities:
        return self._capabilities

    def models(self) -> list[ImageModel]:
        if not self.status().available:
            return []
        return [ImageModel(id="http-endpoint", name=self.endpoint,
                           backend=self.id, kind="endpoint",
                           capabilities=self._capabilities,
                           status="available",
                           notes="Whatever the local server offers.")]

    # -- generation --------------------------------------------------------

    def generate(self, request: GenerationRequest, *,
                 progress: Optional[Callable[[str, float], None]] = None,
                 cancel: Any = None) -> GenerationResult:
        started = time.monotonic()
        if not self.status().available:
            return failed_result(
                request, "The local image endpoint is not reachable.",
                why="Nothing answered at the configured address.",
                what_to_do="Start the local server, or choose another backend.",
                code="ENDPOINT_UNREACHABLE", backend=self.id)

        issues = [issue for issue in validate_request(request, self.capabilities())
                  if issue.severity == "error"]
        if issues:
            return failed_result(request, issues[0].message,
                                 why="The request cannot be run as it is.",
                                 what_to_do=issues[0].what_to_do,
                                 code=issues[0].code, backend=self.id)

        output_dir = Path(request.output_dir or ".")
        output_dir.mkdir(parents=True, exist_ok=True)
        batch = max(1, int(request.batch or 1))
        seeds: list[int] = []
        paths: list[Path] = []

        for index in range(batch):
            if cancel is not None and cancel.is_cancelled():
                return cancelled_result(request)
            if progress is not None:
                progress(GenerationState.GENERATING, (index + 0.2) / batch)
            seed = seed_from_request(request) if index == 0 else seeds[0] + index
            seeds.append(seed)
            stem = request.name_stem or "image"
            suffix = f".{request.output_format or 'png'}"
            # A name that already exists gets a sequence number: a second
            # batch must never overwrite the first (sections 45, 70).
            target = unique_path(
                output_dir,
                stem if batch == 1 else f"{stem}_{index + 1:02d}", suffix)
            payload = {
                "prompt": request.prompt,
                "negative_prompt": request.negative_prompt,
                "width": int(request.width or 0),
                "height": int(request.height or 0),
                "seed": int(seed),
                "steps": int(request.steps or 0),
                "guidance": float(request.guidance or 0.0),
                "sampler": request.sampler,
                "strength": float(request.strength or 0.0),
                "mode": request.mode,
            }
            body = json.dumps(payload).encode("utf-8")
            call = urllib.request.Request(
                f"{self.endpoint}{self.path}", data=body, method="POST",
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(call, timeout=self.timeout) as response:
                    raw = response.read()
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                reason = str(getattr(exc, "reason", exc))[:300]
                return failed_result(
                    request, "The local endpoint did not return an image.",
                    why=reason,
                    what_to_do=("Check the server is running and accepts this "
                                "request shape. Nothing was saved."),
                    code="ENDPOINT_ERROR", backend=self.id)

            # The server may answer with image bytes or with JSON holding base64.
            written = self._write_response(raw, target)
            if not written:
                return failed_result(
                    request, "The endpoint answered, but not with an image.",
                    why="The response was neither image bytes nor base64 image data.",
                    what_to_do="Check the server's output format.",
                    code="RESPONSE_NOT_AN_IMAGE", backend=self.id)
            ok, reason = verify_output(target, expected_width=request.width,
                                       expected_height=request.height)
            if not ok:
                return failed_result(request, reason,
                                     why="The endpoint wrote something else.",
                                     what_to_do="Check the server's size handling.",
                                     code="OUTPUT_INVALID", backend=self.id)
            paths.append(target)

        from ..validation import validate_image_file

        check = validate_image_file(paths[0]) if paths else None
        width = check.width if check and check.ok else int(request.width or 0)
        height = check.height if check and check.ok else int(request.height or 0)
        seconds = time.monotonic() - started
        log_event("IMAGE_GENERATED", f"{len(paths)} image(s) from the local endpoint",
                  backend=self.id, images=len(paths), seconds=round(seconds, 2))
        return GenerationResult(
            ok=True, state=GenerationState.COMPLETED, paths=[str(p) for p in paths],
            seeds=seeds, model=self.endpoint, backend=self.id, mode=request.mode,
            width=width, height=height,
            output_format=request.output_format or "png", seconds=seconds,
            quality={"model": self.endpoint, "backend": self.id,
                     "resolution": f"{width}x{height}",
                     "seed": seeds[0] if seeds else "",
                     "format": request.output_format or "png",
                     "size_bytes": paths[0].stat().st_size if paths else 0,
                     "seconds": round(seconds, 2), "status": "COMPLETED"})

    @staticmethod
    def _write_response(raw: bytes, target: Path) -> bool:
        """Accept raw image bytes, or JSON with a base64 image inside."""
        if not raw:
            return False
        text = raw.lstrip()[:1]
        if text in (b"{", b"["):
            try:
                payload = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                return False
            data = ""
            if isinstance(payload, dict):
                for key in ("image", "image_base64", "data", "b64_json"):
                    value = payload.get(key)
                    if isinstance(value, str) and value:
                        data = value
                        break
            if not data:
                return False
            import base64

            try:
                decoded = base64.b64decode(data)
            except (ValueError, TypeError):
                return False
            target.write_bytes(decoded)
            return True
        target.write_bytes(raw)
        return True
