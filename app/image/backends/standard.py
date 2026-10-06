"""The always-available non-AI backend (Stage F, sections 13, 18, 82).

Image Studio must be useful on a machine with no model installed, so this backend
offers the operations that need no intelligence at all: solid and gradient
canvases, standard resizing, variation by transform, and canvas extension for
outpainting.

It is deliberately *not* presented as an AI model.  ``text_to_image`` is
reported ``False`` because it cannot draw a prompt, and every output is labelled
``Standard``.  The alternative - a fake generator that produces noise and calls
it art - is exactly what this stage forbids.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Optional

from ..capabilities import ImageCapabilities
from ..provider import (GenerationMode, GenerationRequest, GenerationResult,
                        GenerationState, ImageIssue, ImageModel, ImageProvider,
                        ProviderStatus)
from ..validation import validate_request
from ..saving import unique_path
from .base import cancelled_result, failed_result, seed_from_request, verify_output

__all__ = ["StandardBackend"]


class StandardBackend(ImageProvider):
    """Deterministic, model-free image operations."""

    id = "standard"
    label = "Standard (no AI model)"
    kind = "builtin"

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            available=True, state="available",
            reason=("Built in. Needs no model. It cannot draw a prompt - it "
                    "creates canvases, resizes, varies and extends images."),
            version="1.0", device="cpu")

    def capabilities(self) -> ImageCapabilities:
        return ImageCapabilities(
            text_to_image=False,          # honest: no model, no prompt drawing
            image_to_image=True,
            inpaint=True,
            outpaint=True,
            upscale=True,
            reference_image=False,
            control=False,
            lora=False,
            negative_prompt=False,
            seed_control=True,            # seeds still make variations reproducible
            steps=False,
            guidance=False,
            sampler=False,
            batch=True,
            strength=True,
            style_reference=False,
            max_batch=16,
            max_dimension=8192,
            min_dimension=64,
            dimension_multiple=1,
            notes="No AI model required. Cannot draw a prompt.")

    def models(self) -> list[ImageModel]:
        return [ImageModel(
            id="standard", name="Standard (no AI model)", backend=self.id,
            kind="builtin", status="available", capabilities=self.capabilities(),
            notes=("Built-in image operations. It does not understand prompts - "
                   "install a local model for that."))]

    def validate(self, request: GenerationRequest,
                 model: Optional[ImageModel] = None) -> list:
        """Report honestly *why* a prompt cannot be drawn.

        The generic answer ("this backend does not support text to image") is
        true but unhelpful; the real reason is that no model is installed, and
        that is what the user needs to be told.
        """

        issues = validate_request(request, self.capabilities(), model=model)
        replaced: list = []
        for issue in issues:
            if issue.code == "FEATURE_UNSUPPORTED" and \
                    request.mode == GenerationMode.TEXT_TO_IMAGE:
                replaced.append(ImageIssue(
                    "NO_MODEL_INSTALLED",
                    "No local image-generation model is installed, so a prompt "
                    "cannot be drawn.",
                    "Choose Variation, Image to Image, Inpaint, Outpaint or "
                    "Upscale to work from an existing image, or install a local "
                    "backend such as Diffusers or ComfyUI."))
                continue
            replaced.append(issue)
        return replaced

    # -- generation --------------------------------------------------------

    def generate(self, request: GenerationRequest, *,
                 progress: Optional[Callable[[str, float], None]] = None,
                 cancel: Any = None) -> GenerationResult:
        started = time.monotonic()
        issues = [issue for issue in self.validate(request)
                  if issue.severity == "error"]
        if issues:
            return failed_result(
                request, issues[0].message,
                why="The request does not meet this backend's requirements.",
                what_to_do=issues[0].what_to_do, code=issues[0].code,
                backend=self.id)
        if request.mode == GenerationMode.TEXT_TO_IMAGE:
            return failed_result(
                request,
                "No local image-generation model is installed, so a prompt "
                "cannot be drawn.",
                why=("This built-in backend edits and resizes existing images; "
                     "it has no model that understands text."),
                what_to_do=("Choose Variation, Image to Image, Inpaint, Outpaint "
                            "or Upscale to work from an existing image, or install "
                            "a local backend such as Diffusers or ComfyUI."),
                code="NO_MODEL_INSTALLED", backend=self.id)

        output_dir = Path(request.output_dir or ".")
        output_dir.mkdir(parents=True, exist_ok=True)
        batch = max(1, int(request.batch or 1))
        seeds: list[int] = []
        paths: list[Path] = []

        for index in range(batch):
            if cancel is not None and cancel.is_cancelled():
                return cancelled_result(request)
            if progress is not None:
                progress(GenerationState.PROCESSING, (index + 0.5) / batch)
            seed = seed_from_request(request) if index == 0 else seeds[0] + index
            seeds.append(seed)
            stem = request.name_stem or "image"
            suffix = f".{request.output_format or 'png'}"
            # A name that already exists gets a sequence number: a second
            # batch must never overwrite the first (sections 45, 70).
            target = unique_path(
                output_dir,
                stem if batch == 1 else f"{stem}_{index + 1:02d}", suffix)
            try:
                image = self._render(request, seed=seed)
            except Exception as exc:  # noqa: BLE001 - reported, never faked
                return failed_result(
                    request, f"The standard operation failed: {exc}",
                    why="The source image or the requested size could not be used.",
                    what_to_do="Check the source file opens, and the size is valid.",
                    code="STANDARD_FAILED", backend=self.id)
            if progress is not None:
                progress(GenerationState.SAVING, 0.95)
            from ..saving import save_image

            report = save_image(image, target,
                                requested_format=request.output_format or "png")
            if not report.ok:
                return failed_result(request, report.error, why=report.why,
                                     what_to_do=report.what_to_do,
                                     code="SAVE_FAILED", backend=self.id)
            ok, reason = verify_output(report.path)
            if not ok:
                return failed_result(request, reason, why="Verification failed.",
                                     what_to_do="Try again; the file was removed.",
                                     code="OUTPUT_INVALID", backend=self.id)
            paths.append(Path(report.path))

        from ..validation import validate_image_file

        check = validate_image_file(paths[0]) if paths else None
        width = check.width if check and check.ok else int(request.width or 0)
        height = check.height if check and check.ok else int(request.height or 0)
        seconds = time.monotonic() - started
        return GenerationResult(
            ok=True, state=GenerationState.COMPLETED, paths=[str(p) for p in paths],
            seeds=seeds, model="standard", backend=self.id, mode=request.mode,
            width=width, height=height,
            output_format=request.output_format or "png", seconds=seconds,
            quality={"model": "standard (no AI model)", "backend": self.id,
                     "resolution": f"{width}x{height}",
                     "seed": seeds[0] if seeds else "",
                     "format": request.output_format or "png",
                     "size_bytes": paths[0].stat().st_size if paths else 0,
                     "seconds": round(seconds, 2), "status": "COMPLETED",
                     "method": "Standard operation - not an AI model"})

    # -- operations --------------------------------------------------------

    def _render(self, request: GenerationRequest, *, seed: int) -> Any:
        from PIL import Image, ImageFilter

        width = max(1, int(request.width or 512))
        height = max(1, int(request.height or 512))

        source = Image.open(request.source_image)
        source.load()
        if request.mode == GenerationMode.IMAGE_TO_IMAGE and source.mode != "RGBA":
            source = source.convert("RGB")

        if request.mode == GenerationMode.OUTPAINT:
            return self._extend(source, request, background=(16, 16, 20, 255))

        if request.mode == GenerationMode.UPSCALE:
            from ..upscale import standard_resize

            factor = 2.0
            try:
                requested = float(request.scale or 0.0)
                if requested > 0:
                    factor = max(1.0, requested)
            except (TypeError, ValueError):
                factor = 2.0
            return standard_resize(source, factor)

        if request.mode == GenerationMode.INPAINT and request.mask_image:
            mask = Image.open(request.mask_image).convert("L")
            fill = Image.new("RGBA", source.size, (24, 24, 28, 255))
            base = source.convert("RGBA")
            base.paste(fill, (0, 0), mask)
            return base

        # Variation and image-to-image: a visible but honest transform.
        result = source.convert("RGBA") if source.mode == "RGBA" \
            else source.convert("RGB")
        amount = float(request.strength or 0.35)
        result = result.resize((max(8, int(width * (1.0 - 0.04 * amount))),
                                max(8, int(height * (1.0 - 0.04 * amount)))))
        result = result.resize((width, height), Image.Resampling.LANCZOS)
        if (seed % 2) == 0:
            result = result.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if amount > 0.2:
            result = result.filter(ImageFilter.SMOOTH)
        return result

    def _canvas(self, width: int, height: int, *, seed: int) -> Any:
        from PIL import Image, ImageDraw

        palette = [(24, 32, 48), (40, 24, 48), (16, 40, 40), (44, 36, 20),
                   (28, 28, 36), (12, 28, 44)]
        top = palette[seed % len(palette)]
        bottom = palette[(seed + 3) % len(palette)]
        image = Image.new("RGB", (width, height), top)
        draw = ImageDraw.Draw(image)
        for row in range(height):
            ratio = row / max(1, height - 1)
            colour = tuple(int(top[channel] + (bottom[channel] - top[channel]) * ratio)
                           for channel in range(3))
            draw.line([(0, row), (width, row)], fill=colour)
        return image

    @staticmethod
    def _extend(image: Any, request: GenerationRequest, *, background: Any) -> Any:
        from PIL import Image

        pad = dict(request.extend or {})
        left = max(0, int(pad.get("left", 0) or 0))
        right = max(0, int(pad.get("right", 0) or 0))
        top = max(0, int(pad.get("top", 0) or 0))
        bottom = max(0, int(pad.get("bottom", 0) or 0))
        if not any((left, right, top, bottom)):
            raise ValueError("Outpainting needs at least one side to extend.")
        canvas = Image.new("RGBA",
                           (image.width + left + right, image.height + top + bottom),
                           background)
        canvas.paste(image.convert("RGBA"), (left, top))
        return canvas
