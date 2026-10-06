"""Validation for image requests and files (Stage F, sections 7, 36, 43, 46).

The rule throughout: **report, never adjust.** If a model cannot make a 1080x1920
image, the user is told and asked to choose - the request is not quietly changed
to 1024x1024 behind their back, because then the file on disk would not be what
they asked for.

Nothing here touches a model.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .capabilities import ImageCapabilities
from .provider import MODE_FEATURE, GenerationMode, ImageIssue, ImageModel

__all__ = [
    "validate_request",
    "validate_prompt",
    "validate_resolution",
    "validate_image_file",
    "validate_batch",
    "PROMPT_MAX_CHARS",
    "MAX_DIMENSION",
    "MIN_DIMENSION",
    "IMAGE_FORMATS",
    "ALPHA_FORMATS",
]

#: Generous, but a 100 KB prompt is a mistake rather than a prompt.
PROMPT_MAX_CHARS = 4000

#: Absolute bounds the UI offers for a custom size.  A backend's own limits are
#: checked on top of these.
MIN_DIMENSION = 64
MAX_DIMENSION = 8192

#: Formats written and read.  Everything else goes through Pillow when it can.
IMAGE_FORMATS: tuple[str, ...] = ("png", "jpg", "jpeg", "webp", "bmp", "tiff", "gif")
#: Formats that can actually store transparency.
ALPHA_FORMATS: tuple[str, ...] = ("png", "webp", "tiff")

#: Modes where the prompt is the *only* input, so an empty one cannot work.
#: The image-derived modes (variation, image-to-image, inpaint, outpaint) take a
#: source image as their input, and a prompt is an optional steer - requiring one
#: there would block an action that is perfectly valid.
_PROMPT_REQUIRED = (
    GenerationMode.TEXT_TO_IMAGE,
)

#: Modes that need a source image.
_SOURCE_REQUIRED = (
    GenerationMode.IMAGE_TO_IMAGE,
    GenerationMode.INPAINT,
    GenerationMode.OUTPAINT,
    GenerationMode.VARIATION,
    GenerationMode.UPSCALE,
)


@dataclass
class ImageCheck:
    """The outcome of validating a file that should be an image."""

    ok: bool = False
    path: str = ""
    error: str = ""
    what_to_do: str = ""
    width: int = 0
    height: int = 0
    format: str = ""
    mode: str = ""
    size_bytes: int = 0
    has_alpha: bool = False
    animated: bool = False

    def to_dict(self) -> dict:
        return {"ok": self.ok, "path": self.path, "error": self.error,
                "what_to_do": self.what_to_do, "width": self.width,
                "height": self.height, "format": self.format, "mode": self.mode,
                "size_bytes": self.size_bytes, "has_alpha": self.has_alpha,
                "animated": self.animated}


def validate_prompt(prompt: str, *, required: bool = True,
                    field: str = "prompt") -> list[ImageIssue]:
    """Check a prompt without changing a character of it."""
    issues: list[ImageIssue] = []
    text = str(prompt or "")
    if not text.strip():
        if required:
            issues.append(ImageIssue(
                "PROMPT_EMPTY",
                f"The {field} is empty.",
                "Describe the image you want. A short sentence is enough."))
        return issues
    if len(text) > PROMPT_MAX_CHARS:
        issues.append(ImageIssue(
            "PROMPT_TOO_LONG",
            f"The {field} is {len(text)} characters long, which is over the "
            f"{PROMPT_MAX_CHARS} limit.",
            "Shorten it. Most models ignore text beyond a few hundred "
            "characters anyway."))
    if "\x00" in text:
        issues.append(ImageIssue(
            "PROMPT_INVALID_CHARACTERS",
            f"The {field} contains a null byte, which cannot be passed to a "
            "model.",
            "Retype the prompt rather than pasting it from a binary file."))
    return issues


def validate_resolution(width: int, height: int, caps: ImageCapabilities,
                        *, required: bool = True) -> list[ImageIssue]:
    """Check a requested size against the backend's real limits."""
    issues: list[ImageIssue] = []
    width = int(width or 0)
    height = int(height or 0)

    if width <= 0 or height <= 0:
        if required:
            issues.append(ImageIssue(
                "RESOLUTION_MISSING",
                "No output size was given.",
                "Choose a preset size, or type a width and height."))
        return issues

    if not (MIN_DIMENSION <= width <= MAX_DIMENSION) or \
            not (MIN_DIMENSION <= height <= MAX_DIMENSION):
        issues.append(ImageIssue(
            "RESOLUTION_OUT_OF_RANGE",
            f"{width}x{height} is outside the supported range "
            f"({MIN_DIMENSION}-{MAX_DIMENSION} pixels per side).",
            "Choose a standard size such as 1024x1024 or 1280x720."))
        return issues

    if caps.min_dimension and (width < caps.min_dimension
                               or height < caps.min_dimension):
        issues.append(ImageIssue(
            "RESOLUTION_BELOW_MODEL_MINIMUM",
            f"The selected model cannot produce {width}x{height}; its smallest "
            f"side is {caps.min_dimension} pixels.",
            f"Use at least {caps.min_dimension}x{caps.min_dimension}, or choose "
            "another model.", "warning"))

    if caps.max_dimension and (width > caps.max_dimension
                               or height > caps.max_dimension):
        issues.append(ImageIssue(
            "RESOLUTION_ABOVE_MODEL_MAXIMUM",
            f"The selected model cannot produce {width}x{height}; its largest "
            f"side is {caps.max_dimension} pixels.",
            f"Reduce the size to at most {caps.max_dimension} pixels per side, "
            "or choose another model. The size was not changed for you."))

    multiple = int(caps.dimension_multiple or 0)
    if multiple > 1:
        if width % multiple or height % multiple:
            issues.append(ImageIssue(
                "RESOLUTION_NOT_A_MULTIPLE",
                f"The selected model needs sizes in multiples of {multiple}; "
                f"{width}x{height} is not.",
                f"Round each side to a multiple of {multiple} "
                f"(for example {width - width % multiple}x"
                f"{height - height % multiple})."))
    elif width % 2 or height % 2:
        # Not a hard rule for images, but an odd size will be rounded by the
        # video encoder later, so it is worth saying now.
        issues.append(ImageIssue(
            "RESOLUTION_ODD",
            f"{width}x{height} has an odd dimension, which the video encoder "
            "will round later.",
            "Use even numbers to avoid a surprise resize at render time.",
            "warning"))
    return issues


def validate_batch(batch: int, caps: ImageCapabilities) -> list[ImageIssue]:
    batch = int(batch or 1)
    if batch < 1:
        return [ImageIssue("BATCH_TOO_SMALL", "The batch size must be at least 1.",
                           "Use 1 for a single image.")]
    limit = max(1, int(caps.max_batch or 1))
    if batch > limit:
        return [ImageIssue(
            "BATCH_TOO_LARGE",
            f"The selected backend allows at most {limit} image(s) per "
            f"generation, but {batch} was asked for.",
            f"Set the batch to {limit} or fewer, or generate again.")]
    return []


def validate_image_file(path: Any, *, required: bool = True,
                        label: str = "image") -> ImageCheck:
    """Open a file and prove it is a usable image.

    A corrupt file is reported with what to do, never allowed to reach a model
    or a scene.
    """
    target = Path(path) if path else Path("")
    check = ImageCheck(path=str(target))
    if not str(target):
        if required:
            check.error = f"No {label} was chosen."
            check.what_to_do = f"Choose the {label} to work on."
        else:
            check.ok = True
        return check
    if not target.exists():
        check.error = f"The {label} was not found at {target}."
        check.what_to_do = "It may have been moved or deleted. Choose it again."
        return check
    if not target.is_file():
        check.error = f"The {label} path {target} is a folder, not a file."
        check.what_to_do = "Choose the image file itself."
        return check
    try:
        check.size_bytes = target.stat().st_size
    except OSError as exc:
        check.error = f"The {label} could not be read: {exc}"
        check.what_to_do = "Check the file is not on a disconnected drive."
        return check
    if check.size_bytes <= 0:
        check.error = f"The {label} is 0 bytes long."
        check.what_to_do = "The file is empty or was not saved. Export it again."
        return check

    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:  # pragma: no cover - Pillow is a core requirement
        check.error = "Pillow is not installed, so the image could not be checked."
        check.what_to_do = "Install the application's requirements."
        return check

    try:
        with Image.open(target) as image:
            # A header is not proof; loading one pixel proves the data is there.
            image.load()
            check.width, check.height = int(image.width), int(image.height)
            check.format = str(image.format or "").lower()
            check.mode = str(image.mode or "")
            check.has_alpha = "A" in check.mode or check.mode in ("LA", "PA")
            check.animated = bool(getattr(image, "is_animated", False))
    except UnidentifiedImageError:
        check.error = (f"'{target.name}' is not a recognisable image file - the "
                       "header could not be read.")
        check.what_to_do = ("It may be corrupt, truncated, or renamed from "
                            "another format. Open it in an image viewer to check.")
        return check
    except Exception as exc:  # noqa: BLE001 - any decode failure is a report
        check.error = f"'{target.name}' could not be decoded: {exc}"
        check.what_to_do = ("The file is probably corrupt or only partly "
                            "downloaded. Try the original.")
        return check

    if check.width <= 0 or check.height <= 0:
        check.error = f"'{target.name}' reports a size of {check.width}x{check.height}."
        check.what_to_do = "The file is malformed. Export the image again."
        return check

    check.ok = True
    return check


def validate_request(request: Any, caps: ImageCapabilities, *,
                     model: Optional[ImageModel] = None) -> list[ImageIssue]:
    """Everything that can be checked before a model is loaded.

    This is the fatal validation that stops an expensive generation: the model
    is never loaded for a request that was going to fail.
    """
    issues: list[ImageIssue] = []
    mode = str(getattr(request, "mode", "") or "")
    if mode not in MODE_FEATURE:
        issues.append(ImageIssue(
            "MODE_UNKNOWN", f"'{mode}' is not an image generation mode.",
            "Choose one of: " + ", ".join(sorted(MODE_FEATURE))))
        return issues

    feature = MODE_FEATURE[mode]
    if not caps.supports(feature):
        # Upscale has a non-AI path, so it is only a problem when the caller
        # specifically asked for the AI one.
        issues.append(ImageIssue(
            "FEATURE_UNSUPPORTED",
            f"The selected backend does not support "
            f"{feature.replace('_', ' ')}.",
            "Choose a backend that supports it, or use another mode. The "
            "backend was not switched for you."))

    needs_prompt = mode in _PROMPT_REQUIRED
    issues.extend(validate_prompt(request.prompt, required=needs_prompt))
    if getattr(request, "negative_prompt", "") and not caps.negative_prompt:
        issues.append(ImageIssue(
            "NEGATIVE_PROMPT_UNSUPPORTED",
            "This backend ignores negative prompts, so one was not used.",
            "Remove it, or choose a backend that supports negative prompts.",
            "warning"))

    issues.extend(validate_resolution(getattr(request, "width", 0),
                                      getattr(request, "height", 0), caps,
                                      required=mode != GenerationMode.UPSCALE))
    issues.extend(validate_batch(getattr(request, "batch", 1), caps))

    if mode in _SOURCE_REQUIRED:
        source = validate_image_file(getattr(request, "source_image", ""),
                                     required=True, label="source image")
        if not source.ok:
            issues.append(ImageIssue("SOURCE_IMAGE_INVALID", source.error,
                                     source.what_to_do))
    if mode == GenerationMode.INPAINT:
        mask = validate_image_file(getattr(request, "mask_image", ""),
                                   required=True, label="mask")
        if not mask.ok:
            issues.append(ImageIssue("MASK_INVALID", mask.error, mask.what_to_do))

    if getattr(request, "reference_image", ""):
        reference = validate_image_file(request.reference_image, required=True,
                                        label="reference image")
        if not reference.ok:
            issues.append(ImageIssue("REFERENCE_INVALID", reference.error,
                                     reference.what_to_do))
    elif getattr(request, "reference_image", "") == "" and \
            getattr(request, "style_reference", ""):
        style = validate_image_file(request.style_reference, required=True,
                                    label="style reference")
        if not style.ok:
            issues.append(ImageIssue("STYLE_REFERENCE_INVALID", style.error,
                                     style.what_to_do))

    output_format = str(getattr(request, "output_format", "") or "").lower()
    if output_format and output_format not in IMAGE_FORMATS:
        issues.append(ImageIssue(
            "FORMAT_UNSUPPORTED",
            f"'{output_format}' is not a supported image format.",
            f"Choose one of {', '.join(IMAGE_FORMATS)}."))

    strength = float(getattr(request, "strength", 0.0) or 0.0)
    if strength and not (0.0 < strength <= 1.0):
        issues.append(ImageIssue(
            "STRENGTH_OUT_OF_RANGE",
            f"Strength {strength} is outside 0.0-1.0.",
            "Use a value between 0.0 (keep the source) and 1.0 (ignore it)."))

    return [issue for issue in issues if issue.severity == "error"] + \
        [issue for issue in issues if issue.severity != "error"]
