"""Non-destructive image editing (Stage F, sections 16, 17, 53).

An edit is a **list of operations**, not a modified file.  The original image is
opened, the operations are replayed in order, and the result is written
somewhere new.  Nothing is destroyed by default, which is what makes undo, redo
and "reset" possible without keeping copies of every intermediate state.

Overwriting the original is a separate, explicit, confirmed action
(:func:`apply_to_source`), not the default path.

Every operation is plain data, so the exact same list can be stored in the
image's metadata as its edit history and replayed later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from ..core.logging_setup import log_event
from .metadata import ImageMetadata
from .saving import save_image, SaveReport
from .validation import validate_image_file

__all__ = [
    "EditOperation",
    "ImageEditSession",
    "OPERATIONS",
    "OPERATION_LABELS",
    "apply_operations",
]


class _Ops:
    CROP = "crop"
    RESIZE = "resize"
    ROTATE = "rotate"
    FLIP_H = "flip_horizontal"
    FLIP_V = "flip_vertical"
    BRIGHTNESS = "brightness"
    CONTRAST = "contrast"
    SATURATION = "saturation"
    EXPOSURE = "exposure"
    SHARPEN = "sharpen"
    BLUR = "blur"
    OPACITY = "opacity"
    GRAYSCALE = "grayscale"
    CANVAS = "canvas"
    ROUNDED = "rounded_corners"
    TEMPERATURE = "temperature"
    MASK = "mask"


OPERATIONS: tuple[str, ...] = (
    _Ops.CROP, _Ops.RESIZE, _Ops.ROTATE, _Ops.FLIP_H, _Ops.FLIP_V,
    _Ops.BRIGHTNESS, _Ops.CONTRAST, _Ops.SATURATION, _Ops.EXPOSURE,
    _Ops.SHARPEN, _Ops.BLUR, _Ops.OPACITY, _Ops.GRAYSCALE, _Ops.CANVAS,
    _Ops.ROUNDED, _Ops.TEMPERATURE, _Ops.MASK,
)

OPERATION_LABELS: dict[str, str] = {
    _Ops.CROP: "Crop",
    _Ops.RESIZE: "Resize",
    _Ops.ROTATE: "Rotate",
    _Ops.FLIP_H: "Flip horizontal",
    _Ops.FLIP_V: "Flip vertical",
    _Ops.BRIGHTNESS: "Brightness",
    _Ops.CONTRAST: "Contrast",
    _Ops.SATURATION: "Saturation",
    _Ops.EXPOSURE: "Exposure",
    _Ops.SHARPEN: "Sharpen",
    _Ops.BLUR: "Blur",
    _Ops.OPACITY: "Opacity",
    _Ops.GRAYSCALE: "Grayscale",
    _Ops.CANVAS: "Canvas size",
    _Ops.ROUNDED: "Rounded corners",
    _Ops.TEMPERATURE: "Colour temperature",
    _Ops.MASK: "Apply a mask",
}

#: Operations whose parameters are a factor around 1.0.
FACTOR_OPS = (_Ops.BRIGHTNESS, _Ops.CONTRAST, _Ops.SATURATION, _Ops.EXPOSURE)


@dataclass
class EditOperation:
    """One edit, as data."""

    operation: str = ""
    params: dict = field(default_factory=dict)

    def label(self) -> str:
        name = OPERATION_LABELS.get(self.operation, self.operation)
        details = ", ".join(f"{key}={value}" for key, value in
                            sorted(self.params.items()))
        return f"{name} ({details})" if details else name

    def to_dict(self) -> dict:
        return {"operation": self.operation, "params": dict(self.params or {})}

    @classmethod
    def from_dict(cls, data: dict) -> "EditOperation":
        return cls(operation=str(data.get("operation", "")),
                   params=dict(data.get("params") or {}))


@dataclass
class ImageEditSession:
    """An editing session over one source image.

    The source is never written to.  ``preview()`` returns a Pillow image with
    the operations applied; ``save_as()`` writes it somewhere new.
    """

    source: Path = Path()
    operations: list = field(default_factory=list)
    #: Operations that were undone, so redo can bring them back.
    _redo: list = field(default_factory=list)
    metadata: Optional[ImageMetadata] = None

    # -- stack -------------------------------------------------------------

    def add(self, operation: str, **params: Any) -> "ImageEditSession":
        if operation not in OPERATIONS:
            raise ValueError(f"'{operation}' is not a known edit operation.")
        self.operations.append(EditOperation(operation=operation, params=params))
        # A new edit after an undo invalidates the redo stack, exactly as a
        # user expects from any editor.
        self._redo.clear()
        return self

    def undo(self) -> Optional[EditOperation]:
        if not self.operations:
            return None
        operation = self.operations.pop()
        self._redo.append(operation)
        return operation

    def redo(self) -> Optional[EditOperation]:
        if not self._redo:
            return None
        operation = self._redo.pop()
        self.operations.append(operation)
        return operation

    def reset(self) -> None:
        self._redo.clear()
        self.operations.clear()

    @property
    def can_undo(self) -> bool:
        return bool(self.operations)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def history(self) -> list[str]:
        return [operation.label() for operation in self.operations]

    # -- rendering ---------------------------------------------------------

    def preview(self) -> Any:
        """The image with every operation applied, in memory."""
        from PIL import Image

        check = validate_image_file(self.source)
        if not check.ok:
            raise ValueError(check.error)
        with Image.open(self.source) as image:
            image.load()
            return apply_operations(image, self.operations)

    def save_as(self, target: Any, *, requested_format: str = "png",
                overwrite: bool = False, record_history: bool = True) -> SaveReport:
        """Write the edited image as a **new** file.

        ``overwrite=True`` writes over ``target`` itself and is only ever passed
        after the UI has asked for confirmation.
        """
        from PIL import Image

        check = validate_image_file(self.source)
        if not check.ok:
            return SaveReport(error=check.error, what_to_do=check.what_to_do)
        try:
            with Image.open(self.source) as image:
                image.load()
                result = apply_operations(image, self.operations)
                report = save_image(result, target,
                                    requested_format=requested_format,
                                    overwrite=overwrite)
        except Exception as exc:  # noqa: BLE001 - editing must never raise
            return SaveReport(error=f"The edit could not be applied: {exc}",
                              what_to_do="Undo the last change and try again.")
        if report.ok and record_history and self.metadata is not None:
            for operation in self.operations:
                self.metadata.add_edit(operation.operation, operation.params,
                                       output=str(report.path or ""))
            from .metadata import write_metadata

            write_metadata(report.path, self.metadata)
            log_event("IMAGE_EDIT_SAVED", f"{len(self.operations)} edit(s) saved",
                      source=str(self.source), path=str(report.path))
        return report


def apply_operations(image: Any, operations: Sequence[Any]) -> Any:
    """Replay a list of operations onto an image and return the result."""

    result = image
    for item in operations or ():
        operation = getattr(item, "operation", "") if not isinstance(item, dict) \
            else str(item.get("operation", ""))
        params = (getattr(item, "params", {}) if not isinstance(item, dict)
                  else dict(item.get("params") or {}))
        result = _apply_one(result, operation, params)
    return result


def _apply_one(image: Any, operation: str, params: dict) -> Any:
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    if operation == _Ops.CROP:
        box = (int(params.get("left", 0)), int(params.get("top", 0)),
               int(params.get("right", image.width)),
               int(params.get("bottom", image.height)))
        box = (max(0, box[0]), max(0, box[1]),
               min(image.width, box[2]), min(image.height, box[3]))
        if box[2] <= box[0] or box[3] <= box[1]:
            raise ValueError("The crop area is empty.")
        return image.crop(box)

    if operation == _Ops.RESIZE:
        width = int(params.get("width", 0) or 0)
        height = int(params.get("height", 0) or 0)
        if width <= 0 or height <= 0:
            raise ValueError("Resize needs a width and a height above zero.")
        resample = Image.LANCZOS
        return image.resize((width, height), resample)

    if operation == _Ops.ROTATE:
        angle = float(params.get("angle", 0.0) or 0.0)
        expand = bool(params.get("expand", True))
        return image.rotate(angle, expand=expand,
                            resample=Image.BICUBIC,
                            fillcolor=params.get("fill", None))

    if operation == _Ops.FLIP_H:
        return ImageOps.mirror(image)
    if operation == _Ops.FLIP_V:
        return ImageOps.flip(image)

    if operation in FACTOR_OPS:
        factor = float(params.get("factor", 1.0) or 1.0)
        enhancer = {
            _Ops.BRIGHTNESS: ImageEnhance.Brightness,
            _Ops.CONTRAST: ImageEnhance.Contrast,
            _Ops.SATURATION: ImageEnhance.Color,
            _Ops.EXPOSURE: ImageEnhance.Brightness,
        }[operation]
        return enhancer(image).enhance(factor)

    if operation == _Ops.SHARPEN:
        factor = float(params.get("factor", 1.0) or 1.0)
        return ImageEnhance.Sharpness(image).enhance(factor)

    if operation == _Ops.BLUR:
        radius = float(params.get("radius", 2.0) or 2.0)
        return image.filter(ImageFilter.GaussianBlur(radius=radius))

    if operation == _Ops.OPACITY:
        opacity = float(params.get("opacity", 1.0))
        opacity = min(1.0, max(0.0, opacity))
        rgba = image.convert("RGBA")
        alpha = rgba.getchannel("A").point(lambda value: int(value * opacity))
        rgba.putalpha(alpha)
        return rgba

    if operation == _Ops.GRAYSCALE:
        grey = ImageOps.grayscale(image)
        # Keep an alpha channel if there was one, so transparency survives.
        if "A" in str(image.mode or ""):
            alpha = image.convert("RGBA").getchannel("A")
            grey = grey.convert("RGBA")
            grey.putalpha(alpha)
        return grey

    if operation == _Ops.TEMPERATURE:
        shift = float(params.get("shift", 0.0) or 0.0)
        return _temperature(image, shift)

    if operation == _Ops.CANVAS:
        width = int(params.get("width", image.width) or image.width)
        height = int(params.get("height", image.height) or image.height)
        colour = params.get("background", None)
        anchor = str(params.get("anchor", "center"))
        return _canvas(image, width, height, colour, anchor)

    if operation == _Ops.MASK:
        path = str(params.get("path", "") or "")
        if not path:
            raise ValueError("Applying a mask needs the mask image path.")
        from .background_removal import apply_mask

        with Image.open(path) as handle:
            handle.load()
            cut = handle.convert("L")
        return apply_mask(image, cut,
                          feather=float(params.get("feather", 0.0) or 0.0),
                          invert=bool(params.get("invert", False)))

    if operation == _Ops.ROUNDED:
        radius = int(params.get("radius", 24) or 0)
        if radius <= 0:
            return image
        return _rounded_corners(image, radius)

    raise ValueError(f"'{operation}' is not a known edit operation.")


def _temperature(image: Any, shift: float) -> Any:
    """Warm (positive) or cool (negative) by scaling the R and B channels."""
    rgba = image.convert("RGBA")
    red, green, blue, alpha = rgba.split()
    warm = 1.0 + min(0.5, max(-0.5, shift))
    cool = 1.0 - min(0.5, max(-0.5, shift))
    red = red.point(lambda value: min(255, int(value * warm)))
    blue = blue.point(lambda value: min(255, int(value * cool)))
    from PIL import Image

    return Image.merge("RGBA", (red, green, blue, alpha))


def _canvas(image: Any, width: int, height: int, colour: Any, anchor: str) -> Any:
    """Place the image on a larger (or smaller) canvas."""
    from PIL import Image

    has_alpha = "A" in str(image.mode or "") or colour in (None, "transparent")
    mode = "RGBA" if has_alpha else "RGB"
    if colour in (None, "transparent"):
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    else:
        canvas = Image.new(mode, (width, height), colour)
    offsets = {
        "center": ((width - image.width) // 2, (height - image.height) // 2),
        "top-left": (0, 0),
        "top-right": (width - image.width, 0),
        "bottom-left": (0, height - image.height),
        "bottom-right": (width - image.width, height - image.height),
    }
    canvas.paste(image.convert(mode) if image.mode != mode else image,
                 offsets.get(anchor, offsets["center"]),
                 image.convert("RGBA").getchannel("A") if has_alpha else None)
    return canvas


def _rounded_corners(image: Any, radius: int) -> Any:
    from PIL import Image, ImageDraw

    rgba = image.convert("RGBA")
    mask = Image.new("L", rgba.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, rgba.width, rgba.height), radius=radius,
                           fill=255)
    rgba.putalpha(mask)
    return rgba


def apply_to_source(session: ImageEditSession, *, confirmed: bool = False) -> SaveReport:
    """Overwrite the original file with the edited result.

    Refuses unless ``confirmed`` is True: the caller must have asked the user
    first.  A backup of the original is written next to it, because "are you
    sure" is not the same as "cannot be undone".
    """
    if not confirmed:
        return SaveReport(
            error="Overwriting the original image was not confirmed.",
            what_to_do="Ask the user to confirm, then pass confirmed=True.")
    source = Path(session.source)
    if not source.is_file():
        return SaveReport(error=f"The original file {source} was not found.",
                          what_to_do="It may have been moved or deleted.")
    backup = source.with_suffix(source.suffix + ".bak")
    try:
        import shutil

        shutil.copy2(source, backup)
    except OSError as exc:
        return SaveReport(
            error=f"A backup of {source.name} could not be made: {exc}",
            what_to_do="Check the folder is writable and has free space.")
    return session.save_as(source, requested_format=source.suffix.lstrip("."),
                           overwrite=True)
