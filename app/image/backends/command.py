"""Local command-line inference backend (Stage F, sections 2, 72).

Runs a program the *user* configured, once per image, and reads back the file it
wrote.  This is the adapter that makes the whole provider architecture testable
without a diffusion model: any local inference tool - including a small script -
can be driven through exactly the same code path a real model would use.

Nothing is downloaded and no command is invented.  If no program is configured,
the backend reports ``not_installed`` and stays out of the way.
"""

from __future__ import annotations

import shlex
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

from ...core.logging_setup import log_event
from ..capabilities import ImageCapabilities
from ..provider import (GenerationRequest, GenerationResult, GenerationState,
                        ImageModel, ImageProvider, ProviderStatus)
from ..validation import validate_request
from ..saving import unique_path
from .base import (cancelled_result, failed_result, run_process, seed_from_request,
                   verify_output)

__all__ = ["CommandBackend"]

#: Placeholders substituted into the user's command template.
PLACEHOLDERS: tuple[str, ...] = (
    "{prompt}", "{negative_prompt}", "{width}", "{height}", "{seed}",
    "{steps}", "{guidance}", "{sampler}", "{output}", "{source_image}",
    "{mask_image}", "{reference_image}", "{model}",
)


class CommandBackend(ImageProvider):
    """One external program, invoked per image."""

    id = "command"
    label = "Local command"
    kind = "command"

    def __init__(self, command: str = "", *, model: str = "command-model",
                 capabilities: Optional[ImageCapabilities] = None,
                 timeout: float = 3600.0) -> None:
        #: The template, e.g.
        #: ``my-gen --prompt {prompt} --seed {seed} --out {output}``.
        self.command = str(command or "")
        self.model_name = str(model or "command-model")
        self._capabilities = capabilities or ImageCapabilities(
            text_to_image=True, image_to_image=True, inpaint=True,
            outpaint=False, upscale=False, negative_prompt=True,
            seed_control=True, steps=True, guidance=True, sampler=False,
            batch=True, strength=True, max_batch=8, max_dimension=4096,
            min_dimension=64, dimension_multiple=8,
            notes="A local program configured by the user.")
        self.timeout = float(timeout or 3600.0)

    # -- discovery ---------------------------------------------------------

    def status(self) -> ProviderStatus:
        template = self.command.strip()
        if not template:
            return ProviderStatus(
                available=False, state="not_installed",
                reason="No local image command has been configured.",
                instructions=[
                    "Set a command in Settings -> Image Studio, using "
                    "{prompt}, {width}, {height}, {seed} and {output} as "
                    "placeholders.",
                    "The program must write the image to {output} and exit 0.",
                ])
        program = self._program(template)
        if program is None:
            return ProviderStatus(
                available=False, state="not_installed",
                reason=(f"The configured command could not be split into a "
                        f"program and its arguments: {template!r}"),
                instructions=["Use a normal command line, for example "
                              "'my-gen --prompt {prompt} --out {output}'."])
        resolved = shutil.which(program)
        if resolved is None and not Path(program).is_file():
            return ProviderStatus(
                available=False, state="not_installed",
                reason=f"'{program}' was not found on this machine.",
                instructions=[
                    "Install the program, or put it on PATH.",
                    "Or give its full path in the command.",
                ])
        return ProviderStatus(available=True, state="available",
                              reason=f"Found {resolved or program}.",
                              version=str(resolved or program))

    def capabilities(self) -> ImageCapabilities:
        return self._capabilities

    def models(self) -> list[ImageModel]:
        if not self.status().available:
            return []
        return [ImageModel(id=self.model_name, name=self.model_name,
                           backend=self.id, kind="external",
                           capabilities=self._capabilities,
                           status="available",
                           notes="Runs the configured local command.")]

    # -- generation --------------------------------------------------------

    def generate(self, request: GenerationRequest, *,
                 progress: Optional[Callable[[str, float], None]] = None,
                 cancel: Any = None) -> GenerationResult:
        import time

        started = time.monotonic()
        template = self.command.strip()
        if not template:
            return failed_result(
                request, "No local image command is configured.",
                why="This backend runs a program you choose, and none is set.",
                what_to_do="Set a command in Settings -> Image Studio.",
                code="COMMAND_NOT_CONFIGURED", backend=self.id)

        issues = [issue for issue in validate_request(request, self.capabilities())
                  if issue.severity == "error"]
        if issues:
            return failed_result(
                request, issues[0].message, why="The request cannot be run as "
                "it is.", what_to_do=issues[0].what_to_do,
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
                progress(GenerationState.GENERATING, (index + 0.1) / batch)

            seed = seed_from_request(request) if index == 0 else \
                seed_from_request(_with_seed(request, seeds[0] + index))
            seeds.append(seed)
            stem = f"{request.name_stem or 'image'}"
            suffix = f".{request.output_format or 'png'}"
            # A name that already exists gets a sequence number: a second
            # batch must never overwrite the first (sections 45, 70).
            target = unique_path(
                output_dir,
                stem if batch == 1 else f"{stem}_{index + 1:02d}", suffix)
            argv = self._argv(template, request, seed=seed, output=target)
            code, stdout, stderr, cancelled = run_process(
                argv, timeout=self.timeout, cancel=cancel, cwd=str(output_dir))
            if cancelled:
                return cancelled_result(request)
            if code != 0:
                detail = (stderr or stdout or "").strip()[-500:]
                return failed_result(
                    request,
                    f"The image program exited with code {code}.",
                    why=detail or "The program reported no reason.",
                    what_to_do=("Run the same command in a terminal to see the "
                                "full output. The image was not saved."),
                    code="COMMAND_FAILED", backend=self.id)

            ok, reason = verify_output(target, expected_width=request.width,
                                       expected_height=request.height)
            if not ok:
                return failed_result(
                    request, reason,
                    why="The program finished without writing the image that was "
                        "asked for.",
                    what_to_do=("Check the command writes to {output} at exactly "
                                "the requested size."),
                    code="OUTPUT_NOT_WRITTEN", backend=self.id)
            paths.append(target)

        from ..validation import validate_image_file

        check = validate_image_file(paths[0]) if paths else None
        width = check.width if check and check.ok else int(request.width or 0)
        height = check.height if check and check.ok else int(request.height or 0)
        seconds = time.monotonic() - started
        log_event("IMAGE_GENERATED", f"{len(paths)} image(s) from the local command",
                  backend=self.id, images=len(paths), seconds=round(seconds, 2))
        return GenerationResult(
            ok=True, state=GenerationState.COMPLETED, paths=[str(p) for p in paths],
            seeds=seeds, model=self.model_name, backend=self.id,
            mode=request.mode, width=width, height=height,
            output_format=request.output_format or "png", seconds=seconds,
            quality={
                "model": self.model_name, "backend": self.id,
                "resolution": f"{width}x{height}", "seed": seeds[0] if seeds else "",
                "format": request.output_format or "png",
                "size_bytes": paths[0].stat().st_size if paths else 0,
                "seconds": round(seconds, 2), "status": "COMPLETED",
            },
            metadata={"backend": self.id, "command": template})

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _program(template: str) -> Optional[str]:
        try:
            parts = shlex.split(template, posix=True)
        except ValueError:
            return None
        return parts[0] if parts else None

    def _argv(self, template: str, request: GenerationRequest, *, seed: int,
              output: Path) -> list[str]:
        try:
            parts = shlex.split(template, posix=True)
        except ValueError:
            parts = template.split()
        values = {
            "prompt": str(request.prompt or ""),
            "negative_prompt": str(request.negative_prompt or ""),
            "width": str(int(request.width or 0)),
            "height": str(int(request.height or 0)),
            "seed": str(int(seed)),
            "steps": str(int(request.steps or 0)),
            "guidance": str(float(request.guidance or 0.0)),
            "sampler": str(request.sampler or ""),
            "output": str(output),
            "source_image": str(request.source_image or ""),
            "mask_image": str(request.mask_image or ""),
            "reference_image": str(request.reference_image or ""),
            "model": str(request.model or self.model_name),
        }
        argv: list[str] = []
        for part in parts:
            replaced = part
            for key, value in values.items():
                replaced = replaced.replace("{" + key + "}", value)
            argv.append(replaced)
        return argv


def _with_seed(request: GenerationRequest, seed: int) -> GenerationRequest:
    """A copy of the request with one seed, for a batch item."""
    clone = GenerationRequest.from_dict(request.to_dict())
    clone.seed = int(seed)
    return clone
