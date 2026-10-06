"""Background removal (Stage F, section 18).

Three honest states, and the interface shows whichever applies:

``AVAILABLE``
    A supported local model is installed and usable.
``NOT INSTALLED``
    Nothing is installed.  This is the normal state on a fresh machine.
``NOT SUPPORTED``
    Something is installed, but nothing that can do this job.

There is deliberately **no** fallback that thresholds a colour and calls the
result a cut-out.  That would produce a wrong picture the user would trust, which
is the one outcome this stage forbids.  When no model exists, the manual mask is
offered instead - a real tool the user controls, not a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.logging_setup import log_event
from .saving import SaveReport, save_image
from .validation import validate_image_file

__all__ = [
    "REMOVAL_AVAILABLE",
    "REMOVAL_NOT_INSTALLED",
    "REMOVAL_NOT_SUPPORTED",
    "STATES",
    "RemovalStatus",
    "detect_background_removal",
    "remove_background",
    "mask_from_alpha",
    "apply_mask",
]

REMOVAL_AVAILABLE = "AVAILABLE"
REMOVAL_NOT_INSTALLED = "NOT INSTALLED"
REMOVAL_NOT_SUPPORTED = "NOT SUPPORTED"

#: Shown in the interface, so the states are never blurred together.
STATES: tuple[str, ...] = (
    REMOVAL_AVAILABLE, REMOVAL_NOT_INSTALLED, REMOVAL_NOT_SUPPORTED,
)

#: Packages that provide a real local segmentation model.  Checked by import
#: spec only - nothing is imported and no weights are loaded by detection.
CANDIDATE_PACKAGES: tuple[str, ...] = ("rembg", "backgroundremover",
                                       "transparent_background")


@dataclass
class RemovalStatus:
    """Whether background removal can run, and why not if it cannot."""

    state: str = REMOVAL_NOT_INSTALLED
    backend: str = ""
    reason: str = ""
    instructions: list = field(default_factory=list)
    #: The manual fallback is always available, and always said so.
    manual_available: bool = True

    @property
    def available(self) -> bool:
        return self.state == REMOVAL_AVAILABLE

    def describe(self) -> str:
        lines = [f"Background removal: {self.state}"]
        if self.backend:
            lines.append(f"  backend: {self.backend}")
        if self.reason:
            lines.append(f"  {self.reason}")
        for step in self.instructions:
            lines.append(f"  -> {step}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"state": self.state, "backend": self.backend,
                "reason": self.reason, "available": self.available,
                "manual_available": self.manual_available,
                "instructions": list(self.instructions)}


def detect_background_removal() -> RemovalStatus:
    """Look for a real local background-removal model.  Loads nothing."""
    try:
        import importlib.util
    except ImportError:  # pragma: no cover - the import system always exists
        return RemovalStatus(
            state=REMOVAL_NOT_SUPPORTED,
            reason="The Python import system is not available, so no model "
                   "could be looked for.")

    found: list[str] = []
    for name in CANDIDATE_PACKAGES:
        try:
            if importlib.util.find_spec(name) is not None:
                found.append(name)
        except (ImportError, ValueError):
            continue

    if found:
        backend = found[0]
        return RemovalStatus(
            state=REMOVAL_AVAILABLE, backend=backend,
            reason=f"Using the installed '{backend}' package.",
            instructions=["Removal creates a new file; the original is kept."])

    return RemovalStatus(
        state=REMOVAL_NOT_INSTALLED,
        reason=("No local background-removal model is installed (looked for: "
                + ", ".join(CANDIDATE_PACKAGES) + ")."),
        instructions=[
            "Use the manual mask instead: paint what to keep and the rest "
            "becomes transparent. It needs no model.",
            "Or install one of those packages to enable automatic removal.",
        ])


def remove_background(source: Any, target: Any, *,
                      requested_format: str = "png") -> SaveReport:
    """Remove the background with a real model, or refuse.

    Refusing is the correct behaviour when nothing is installed.  Nothing is
    invented, and the source file is never modified.
    """
    status = detect_background_removal()
    if not status.available:
        return SaveReport(
            error=("Background removal is not available on this machine, so no "
                   "image was produced."),
            what_to_do=("Use the manual mask tool, or install a "
                        "background-removal model. The original is unchanged."))

    check = validate_image_file(source, required=True, label="The image")
    if not check.ok:
        return SaveReport(error=check.error, what_to_do=check.what_to_do)

    try:
        cut_out = _run_model(Path(source), status.backend)
    except NotImplementedError as exc:
        return SaveReport(
            error=str(exc),
            what_to_do="Use the manual mask tool; it needs no model.")
    except Exception as exc:  # noqa: BLE001 - a model failure is reported
        log_event("IMAGE_BACKGROUND_FAILED", "Background removal failed",
                  path=str(source), error=str(exc))
        return SaveReport(
            error=f"Background removal failed: {exc}",
            what_to_do=("Check the model files are complete, or use the manual "
                        "mask tool."))

    # A cut-out without transparency is not a cut-out; the format is forced.
    report = save_image(cut_out, target, requested_format=requested_format)
    if report.ok:
        log_event("IMAGE_BACKGROUND_REMOVED",
                  f"Background removed from {Path(source).name}",
                  path=str(report.path), backend=status.backend)
    return report


def _run_model(source: Path, backend: str) -> Any:
    """Call the installed model.

    Each adapter is thin and imports lazily.  A package that is present but has
    no adapter raises, and the caller reports that rather than quietly producing
    a resize or a colour threshold instead.
    """
    if backend == "rembg":
        from rembg import remove  # type: ignore

        from PIL import Image

        with Image.open(source) as image:
            image.load()
            return remove(image)
    raise NotImplementedError(
        f"The '{backend}' package was found, but this build has no adapter for "
        "it, so no cut-out was produced.")


# --------------------------------------------------------------------------
# Manual masking - the fallback that always works
# --------------------------------------------------------------------------

def mask_from_alpha(image: Any) -> Any:
    """The alpha channel as a greyscale mask, for editing in the mask tool."""
    return image.convert("RGBA").getchannel("A").convert("L")


def apply_mask(image: Any, mask: Any, *, feather: float = 0.0,
               invert: bool = False) -> Any:
    """Cut an image out with a mask, optionally softening the edge.

    ``feather`` is a blur radius in pixels; a small value stops the cut-out
    looking like it was done with scissors.  Existing transparency is
    *combined* with the mask rather than replaced, so masking twice does not
    bring back pixels that were already invisible.
    """
    from PIL import Image, ImageChops, ImageFilter, ImageOps

    base = image.convert("RGBA")
    cut = mask.convert("L")
    if cut.size != base.size:
        cut = cut.resize(base.size, Image.LANCZOS)
    if invert:
        cut = ImageOps.invert(cut)
    if feather and float(feather) > 0:
        cut = cut.filter(ImageFilter.GaussianBlur(radius=float(feather)))
    combined = ImageChops.multiply(base.getchannel("A"), cut)
    base.putalpha(combined)
    return base
