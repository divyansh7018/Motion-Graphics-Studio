"""Writing images safely (Stage F, sections 19, 20, 21, 45, 46, 61, 62).

Three rules:

1. **Atomic.** An image is written to a temporary file in the same folder,
   flushed to disk, verified, and only then moved into place.  A crash or a full
   disk leaves the previous file alone, never a half-written one that the
   library would show as an asset.
2. **Never overwrite silently.** A target that already exists gets a sequence
   number, exactly like a render does.
3. **Do not degrade.** Quality is preserved unless the user asked otherwise:
   an image with transparency is not flattened into JPEG, and a PNG is not
   round-tripped through JPEG on the way to somewhere else.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..core.logging_setup import log_event
from .validation import ALPHA_FORMATS, validate_image_file

__all__ = [
    "SaveReport",
    "save_image",
    "atomic_write_bytes",
    "unique_path",
    "normalise_format",
    "convert_image",
    "pick_format_for",
    "JPEG_SUFFIXES",
]

JPEG_SUFFIXES = (".jpg", ".jpeg")

#: Default quality when a lossy format is genuinely wanted.
DEFAULT_JPEG_QUALITY = 92
DEFAULT_WEBP_QUALITY = 92


@dataclass
class SaveReport:
    """What a save did, and whether the file was verified afterwards."""

    ok: bool = False
    path: Optional[Path] = None
    error: str = ""
    what_to_do: str = ""
    width: int = 0
    height: int = 0
    format: str = ""
    size_bytes: int = 0
    #: True when the requested format could not hold the image and a different
    #: one was used - always reported, never silent.
    format_changed: str = ""
    verified: bool = False

    def describe(self) -> str:
        if not self.ok:
            return self.error or "The image could not be saved."
        text = (f"{self.path.name}: {self.width}x{self.height} {self.format}, "
                f"{self.size_bytes:,} bytes")
        if self.format_changed:
            text += f" ({self.format_changed})"
        return text

    def to_dict(self) -> dict:
        return {"ok": self.ok, "path": str(self.path) if self.path else "",
                "error": self.error, "what_to_do": self.what_to_do,
                "width": self.width, "height": self.height,
                "format": self.format, "size_bytes": self.size_bytes,
                "format_changed": self.format_changed,
                "verified": self.verified}


def normalise_format(name: str) -> str:
    """``.JPG``/``JPEG``/``jpg`` all become ``jpg``."""
    text = str(name or "").strip().lower()
    if text.startswith("."):
        text = text[1:]
    if text == "jpeg":
        return "jpg"
    return text


def pick_format_for(image: Any, requested: str) -> tuple[str, str]:
    """The format to write, and why it changed if it did.

    Returns ``(format, note)``.  ``note`` is empty when the requested format was
    used, so a caller can tell the user the truth.
    """
    wanted = normalise_format(requested) or "png"
    has_alpha = False
    try:
        mode = str(getattr(image, "mode", "") or "")
        has_alpha = "A" in mode or mode in ("LA", "PA")
        if not has_alpha and hasattr(image, "info"):
            has_alpha = "transparency" in (image.info or {})
    except Exception:  # noqa: BLE001 - a guess is better than a crash
        has_alpha = False

    if has_alpha and wanted not in ALPHA_FORMATS:
        return "png", (f"'{wanted}' cannot store transparency, so PNG was used "
                       "instead")
    return wanted, ""


def unique_path(folder: Path, stem: str, suffix: str) -> Path:
    """``image.png``, then ``image_1.png``, ``image_2.png`` …

    Nothing is ever overwritten: an existing file means a new sequence number.
    """
    folder = Path(folder)
    candidate = folder / f"{stem}{suffix}"
    index = 0
    while candidate.exists():
        index += 1
        candidate = folder / f"{stem}_{index}{suffix}"
    return candidate


def atomic_write_bytes(target: Path, data: bytes) -> Path:
    """Write bytes to ``target`` without ever exposing a partial file."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        dir=str(target.parent), prefix=f".{target.stem}.", suffix=".part",
        delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(target))
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target


def save_image(image: Any, target: Any, *, requested_format: str = "png",
               overwrite: bool = False, jpeg_quality: int = DEFAULT_JPEG_QUALITY,
               webp_quality: int = DEFAULT_WEBP_QUALITY,
               verify: bool = True) -> SaveReport:
    """Save a Pillow image through a temporary file, then verify it.

    ``overwrite=False`` (the default) never touches an existing file; it takes
    the next free sequence number.  Passing ``overwrite=True`` is a deliberate,
    confirmed action from the caller - the UI asks first.
    """
    report = SaveReport()
    target = Path(target)
    folder = target.parent
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        report.error = f"The folder {folder} could not be created."
        report.what_to_do = "Check the location is writable."
        report.error = f"{report.error} ({exc})"
        return report

    wanted = normalise_format(requested_format or target.suffix.lstrip(".")) or "png"
    chosen, note = pick_format_for(image, wanted)
    report.format_changed = note
    suffix = ".jpg" if chosen == "jpg" else f".{chosen}"

    if overwrite:
        final = folder / f"{target.stem}{suffix}"
    else:
        final = unique_path(folder, target.stem or "image", suffix)

    temporary = Path(tempfile.mkstemp(dir=str(folder), prefix=f".{final.stem}.",
                                      suffix=".part")[1])
    try:
        save_kwargs: dict[str, Any] = {}
        if chosen == "jpg":
            # A JPEG has no alpha; flatten onto white rather than onto black,
            # which is what Pillow would otherwise do with a mode-P image.
            if "A" in str(image.mode or "") or image.mode in ("LA", "PA"):
                image = _flatten(image, "#ffffff")
            elif image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            save_kwargs = {"quality": int(jpeg_quality), "optimize": True,
                           "progressive": True, "subsampling": 0}
        elif chosen == "webp":
            save_kwargs = {"quality": int(webp_quality), "method": 6}
        elif chosen == "png":
            save_kwargs = {"optimize": True, "compress_level": 6}

        with temporary.open("wb") as handle:
            image.save(handle, format=chosen.upper() if chosen != "jpg" else "JPEG",
                       **save_kwargs)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(final))
    except OSError as exc:
        _remove_quietly(temporary)
        report.error = f"The image could not be written to {final}."
        report.what_to_do = "Check there is free disk space and that the folder is writable."
        report.error = f"{report.error} ({exc})"
        return report
    except Exception as exc:  # noqa: BLE001 - Pillow raises many types
        _remove_quietly(temporary)
        report.error = f"The image could not be encoded as {chosen.upper()}."
        report.what_to_do = "Try PNG, which accepts every pixel format."
        report.error = f"{report.error} ({exc})"
        return report

    report.path = final
    try:
        report.size_bytes = final.stat().st_size
    except OSError:
        report.size_bytes = 0

    if verify:
        check = validate_image_file(final)
        report.verified = check.ok
        if not check.ok:
            report.error = (f"The image was written but could not be read back: "
                            f"{check.error}")
            report.what_to_do = check.what_to_do
            _remove_quietly(final)
            report.path = None
            log_event("IMAGE_SAVE_FAILED", report.error, path=str(final))
            return report
        report.width, report.height = check.width, check.height
        report.format = check.format or chosen
    else:
        report.width = int(getattr(image, "width", 0) or 0)
        report.height = int(getattr(image, "height", 0) or 0)
        report.format = chosen

    report.ok = True
    log_event("IMAGE_SAVED", f"Saved {final.name}", path=str(final),
              width=report.width, height=report.height, format=report.format,
              bytes=report.size_bytes, verified=report.verified)
    return report


def convert_image(source: Any, target: Any, *, requested_format: str = "",
                  overwrite: bool = False,
                  jpeg_quality: int = DEFAULT_JPEG_QUALITY) -> SaveReport:
    """Transcode one image file to another format without extra resampling.

    The pixels are copied, not resized or colour-converted, unless the target
    format forces it (a JPEG cannot hold alpha, and that is reported).
    """
    from PIL import Image

    source = Path(source)
    check = validate_image_file(source)
    if not check.ok:
        return SaveReport(error=check.error, what_to_do=check.what_to_do)
    wanted = normalise_format(requested_format or Path(target).suffix.lstrip("."))
    if not wanted:
        wanted = check.format or "png"
    try:
        with Image.open(source) as image:
            image.load()
            return save_image(image, target, requested_format=wanted,
                              overwrite=overwrite, jpeg_quality=jpeg_quality)
    except Exception as exc:  # noqa: BLE001
        return SaveReport(error=f"'{source.name}' could not be converted: {exc}",
                          what_to_do="Open the original to check it is intact.")


def _flatten(image: Any, background: str) -> Any:
    """Composite onto a solid background, keeping the alpha as a mask."""
    from PIL import Image

    base = Image.new("RGB", image.size, background)
    mask = image.split()[-1] if "A" in str(image.mode or "") else None
    base.paste(image.convert("RGBA"), (0, 0), mask)
    return base


def _remove_quietly(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def copy_into(folder: Any, source: Any, *, stem: str = "") -> Path:
    """Copy a file into ``folder`` under a unique name."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    source = Path(source)
    target = unique_path(folder, stem or source.stem, source.suffix.lower())
    shutil.copy2(source, target)
    return target
