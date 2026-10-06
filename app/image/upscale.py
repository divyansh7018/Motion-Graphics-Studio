"""Upscaling (Stage F, section 15).

Two different operations that must never be confused in the interface:

* **Standard Resize** - a high-quality Lanczos resample.  Always available, no
  model needed, and honest about what it is: it makes the picture bigger, it
  does not invent detail.
* **AI Upscale** - a real upscaler model, run only when one is actually
  installed and detected.

A result always states which one produced it.  Calling a Lanczos resize an "AI
upscale" would be a claim the user could disprove the moment they zoomed in.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..core.logging_setup import log_event
from .saving import save_image
from .validation import validate_image_file

__all__ = [
    "METHOD_STANDARD",
    "METHOD_AI",
    "METHOD_AUTO",
    "METHODS",
    "METHOD_LABELS",
    "UpscaleStatus",
    "UpscaleResult",
    "detect_upscaler",
    "standard_resize",
    "upscale",
]

METHOD_STANDARD = "standard"
METHOD_AI = "ai"
#: "auto" means "use AI if it is installed, otherwise say plainly that a resize
#: was used".  It never silently relabels a resize as an AI upscale.
METHOD_AUTO = "auto"

METHODS: tuple[str, ...] = (METHOD_STANDARD, METHOD_AI, METHOD_AUTO)

METHOD_LABELS: dict[str, str] = {
    METHOD_STANDARD: "Standard Resize",
    METHOD_AI: "AI Upscale",
    METHOD_AUTO: "Automatic (AI if available)",
}

#: Packages that provide a real local upscaler.
CANDIDATE_PACKAGES: tuple[str, ...] = ("realesrgan", "basicsr", "spandrel")

#: Below this, an "upscale" is not worth a model.
MIN_SCALE = 1.0
MAX_SCALE = 8.0


@dataclass
class UpscaleStatus:
    """Whether AI upscaling is available, and the honest reason either way."""

    ai_available: bool = False
    backend: str = ""
    reason: str = ""
    instructions: list = field(default_factory=list)

    def describe(self) -> str:
        lines = ["Method: Standard Resize (always available)"]
        if self.ai_available:
            lines.append(f"Method: AI Upscale ({self.backend})")
        lines.append(f"  {self.reason}")
        for step in self.instructions:
            lines.append(f"  -> {step}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"ai_available": self.ai_available, "backend": self.backend,
                "reason": self.reason, "instructions": list(self.instructions)}


@dataclass
class UpscaleResult:
    """What an upscale did - including which method really ran."""

    ok: bool = False
    path: Optional[Path] = None
    source_size: tuple = (0, 0)
    output_size: tuple = (0, 0)
    scale: float = 1.0
    #: Always the method that actually produced the file.
    method: str = METHOD_STANDARD
    #: True when AI was requested but a resize was used instead.  Never silent.
    downgraded: bool = False
    seconds: float = 0.0
    error: str = ""
    what_to_do: str = ""
    edit_history: list = field(default_factory=list)

    @property
    def method_label(self) -> str:
        """Which method produced the file - never what was merely requested."""
        return METHOD_LABELS.get(self.method, self.method)

    def describe(self) -> str:
        if not self.ok:
            return self.error or "The image could not be upscaled."
        width, height = self.output_size
        text = (f"{self.method_label}: {width}x{height} "
                f"({self.scale:g}x) in {self.seconds:.2f}s")
        if self.downgraded:
            text += " (no AI upscaler is installed, so a standard resize was used)"
        return text

    def to_dict(self) -> dict:
        return {"ok": self.ok, "path": str(self.path) if self.path else "",
                "source_size": list(self.source_size),
                "output_size": list(self.output_size),
                "scale": float(self.scale or 1.0), "method": self.method,
                "label": self.method_label, "downgraded": self.downgraded,
                "seconds": round(float(self.seconds or 0.0), 3),
                "error": self.error, "what_to_do": self.what_to_do,
                "edit_history": list(self.edit_history)}


def detect_upscaler() -> UpscaleStatus:
    """Look for a real local upscaler.  Loads no model and no weights."""
    try:
        import importlib.util
    except ImportError:  # pragma: no cover
        return UpscaleStatus(
            reason="The Python import system is not available.")

    found: list[str] = []
    for name in CANDIDATE_PACKAGES:
        try:
            if importlib.util.find_spec(name) is not None:
                found.append(name)
        except (ImportError, ValueError):
            continue

    if found:
        return UpscaleStatus(
            ai_available=True, backend=found[0],
            reason=f"Found the '{found[0]}' package.")
    return UpscaleStatus(
        ai_available=False,
        reason=("No local AI upscaler is installed (looked for: "
                + ", ".join(CANDIDATE_PACKAGES) + ")."),
        instructions=[
            "Standard Resize works now and needs no model: it enlarges the "
            "image without inventing detail.",
            "Install one of those packages to enable AI Upscale.",
        ])


def standard_resize(image: Any, scale: float) -> Any:
    """A high-quality Lanczos resample.  Enlarges; invents nothing."""
    from PIL import Image

    factor = float(scale or 1.0)
    if factor <= 0:
        raise ValueError("The scale factor must be above zero.")
    width = max(1, int(round(int(getattr(image, "width", 0)) * factor)))
    height = max(1, int(round(int(getattr(image, "height", 0)) * factor)))
    return image.resize((width, height), Image.LANCZOS)


def upscale(source: Any, target: Any, *, scale: float = 2.0,
            method: str = METHOD_STANDARD, allow_fallback: bool = False,
            requested_format: str = "png") -> UpscaleResult:
    """Make an image bigger, and state plainly how it was done.

    ``method=METHOD_AI`` with no upscaler installed is an error unless
    ``allow_fallback`` is set - and when the fallback is used,
    ``downgraded`` is set so the interface says a resize was used.  The original
    file is never touched.
    """
    started = time.monotonic()
    result = UpscaleResult(scale=float(scale or 1.0), method=str(method or METHOD_STANDARD))
    source_path = Path(source)

    check = validate_image_file(source_path, required=True, label="The image")
    if not check.ok:
        result.error = check.error
        result.what_to_do = check.what_to_do
        return result
    result.source_size = (check.width, check.height)

    factor = float(scale or 0.0)
    if not (MIN_SCALE <= factor <= MAX_SCALE):
        result.error = (f"A scale of {factor:g}x is outside the supported range "
                        f"({MIN_SCALE:g}x to {MAX_SCALE:g}x).")
        result.what_to_do = f"Choose a scale between {MIN_SCALE:g} and {MAX_SCALE:g}."
        return result

    wanted = str(method or METHOD_STANDARD).lower()
    if wanted not in METHODS:
        result.error = f"'{method}' is not an upscale method."
        result.what_to_do = "Choose Standard Resize or AI Upscale."
        return result

    status = detect_upscaler()
    use_ai = False
    if wanted == METHOD_AI:
        if status.ai_available:
            use_ai = True
        elif not allow_fallback:
            result.error = ("An AI upscale needs a local upscaler model, and "
                            "none is installed on this machine.")
            result.what_to_do = ("Use Standard Resize instead (it needs no model), "
                                 "or install an upscaler. No image was created.")
            return result
        else:
            result.downgraded = True
    elif wanted == METHOD_AUTO:
        if status.ai_available:
            use_ai = True
        else:
            # "Auto" on a machine with no upscaler is a standard resize, and the
            # result says so via its label rather than pretending otherwise.
            use_ai = False

    result.method = METHOD_AI if use_ai else METHOD_STANDARD

    try:
        from PIL import Image

        with Image.open(source_path) as handle:
            handle.load()
            if use_ai:
                output = _run_upscaler(handle, factor, status.backend)
                if output is None:
                    raise NotImplementedError(
                        f"The '{status.backend}' package was found, but this "
                        "build has no adapter for it, so no image was produced.")
            else:
                output = standard_resize(handle, factor)
            result.output_size = (int(output.width), int(output.height))
            report = save_image(output, target,
                                requested_format=requested_format or "png",
                                overwrite=False)
    except NotImplementedError as exc:
        result.error = str(exc)
        result.what_to_do = "Use Standard Resize; it needs no model."
        return result
    except Exception as exc:  # noqa: BLE001 - an upscale must never raise
        result.error = f"The image could not be upscaled: {exc}"
        result.what_to_do = "Try a smaller scale, or check the source file opens."
        return result

    result.seconds = time.monotonic() - started
    if not report.ok:
        result.error = report.error
        result.what_to_do = report.what_to_do
        return result

    result.ok = True
    result.path = report.path
    result.edit_history.append({
        "operation": "upscale", "method": result.method,
        "scale": result.scale, "size": list(result.output_size),
    })
    log_event("IMAGE_UPSCALED", result.describe(), path=str(result.path),
              method=result.method, scale=result.scale)
    return result


def _run_upscaler(image: Any, scale: float, backend: str) -> Any:
    """Run the installed upscaler, or return None when no adapter exists."""
    if backend == "realesrgan":
        raise NotImplementedError(
            "The 'realesrgan' package was found, but this build has no adapter "
            "for it yet, so no image was produced. Standard Resize is available.")
    return None
