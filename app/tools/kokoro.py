"""Kokoro TTS detection (directive sections 15, 16, 47, 63).

Stage A only **detects** the engine; it never loads the model.  Loading is
lazy by design (section 47): the model is initialised the first time the user
generates speech, so application startup stays fast and the app still opens on
a machine where the optional voice stack is not installed yet.

What is checked:

1. Is the ``kokoro`` Python package importable?  (``pip install kokoro``)
2. Are the runtime dependencies present (``torch`` or ``onnxruntime``, plus a
   phonemiser such as ``misaki``)?
3. Are model files present locally, or will the first run need a download?
4. Can voices be enumerated from the model/voice catalogue?

Nothing here touches the network.  A missing component produces a *status*
(✗ Missing / ⚠ Optional) with a concrete instruction, never an exception.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from ..core.events import Event
from ..core.logging_setup import get_logger, log_event
from ..core.packages import PackageStatus, probe_package

LOGGER = get_logger("kokoro")

#: Kokoro package on PyPI.
KOKORO_PACKAGE = "kokoro"

#: Acceptable runtime backends, in order of preference on a CPU-only machine.
#: ONNX runtime is generally the fastest CPU path for Kokoro-82M; torch is the
#: default used by the reference package.
RUNTIME_BACKENDS: tuple[tuple[str, str], ...] = (
    ("onnxruntime", "ONNX Runtime (fastest on CPU)"),
    ("torch", "PyTorch"),
)

#: Packages needed to convert text to phonemes for Kokoro.
PHONEMIZER_PACKAGES: tuple[tuple[str, str], ...] = (
    ("misaki", "Misaki (recommended G2P for Kokoro)"),
    ("espeakng_loader", "eSpeak NG data (fallback G2P)"),
    ("phonemizer", "Phonemizer (fallback G2P)"),
)

#: Files that indicate a locally provisioned model folder.
MODEL_FILE_HINTS: tuple[str, ...] = (
    "kokoro-v1_0.pth",
    "kokoro-v1.0.onnx",
    "kokoro-v1_0.onnx",
    "model.onnx",
    "config.json",
    "voices-v1.0.bin",
    "voices.json",
)

INSTALL_HINT = "pip install kokoro"


@dataclass
class KokoroStatus:
    """Everything the UI needs to describe the voice engine's readiness."""

    package: PackageStatus
    runtime: Optional[PackageStatus] = None
    phonemizer: Optional[PackageStatus] = None
    model_dir: Optional[Path] = None
    model_files: list[Path] = field(default_factory=list)
    voices_found: int = 0
    voices_source: str = ""
    notes: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def package_ready(self) -> bool:
        return self.package.importable

    @property
    def runtime_ready(self) -> bool:
        return self.runtime is not None and self.runtime.importable

    @property
    def phonemizer_ready(self) -> bool:
        return self.phonemizer is not None and self.phonemizer.importable

    @property
    def local_model_present(self) -> bool:
        return bool(self.model_files)

    @property
    def usable(self) -> bool:
        """Enough is installed to attempt speech generation."""
        return self.package_ready and self.runtime_ready

    @property
    def fully_configured(self) -> bool:
        """Ready to generate **offline** (model files are already local)."""
        return self.usable and self.local_model_present

    # -- messaging ---------------------------------------------------------

    def headline(self) -> str:
        if self.fully_configured:
            return "Kokoro voice engine is ready and works offline."
        if self.usable:
            return "Kokoro is installed. The model files will be downloaded on first use (one time)."
        if self.package_ready and not self.runtime_ready:
            return "Kokoro is installed but no supported runtime (PyTorch or ONNX Runtime) was found."
        return "Kokoro voice engine is not installed yet."

    def details(self) -> list[str]:
        lines = [self.package.line()]
        if self.runtime is not None:
            lines.append(self.runtime.line())
        if self.phonemizer is not None:
            lines.append(self.phonemizer.line())
        if self.model_dir is not None:
            lines.append(f"model folder: {self.model_dir} ({len(self.model_files)} file(s))")
        if self.voices_found:
            lines.append(f"voices discovered: {self.voices_found} ({self.voices_source})")
        lines.extend(self.notes)
        return lines

    def as_dict(self) -> dict[str, object]:
        return {
            "package": self.package.name,
            "package_installed": self.package.installed,
            "package_importable": self.package.importable,
            "package_version": self.package.version,
            "runtime": self.runtime.name if self.runtime else None,
            "runtime_importable": self.runtime_ready,
            "phonemizer": self.phonemizer.name if self.phonemizer else None,
            "model_dir": str(self.model_dir) if self.model_dir else None,
            "model_files": len(self.model_files),
            "voices_found": self.voices_found,
            "usable": self.usable,
            "offline_ready": self.fully_configured,
        }


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------

def probe_kokoro(
    model_dir: Optional[Path] = None,
    deep_import_check: bool = False,
    voice_count: Optional[int] = None,
    voices_source: str = "",
) -> KokoroStatus:
    """Probe the Kokoro installation.

    *deep_import_check* is ``False`` during startup (fast, no heavy imports) and
    ``True`` when the user explicitly re-runs the check or opens the voice page.
    """
    package = probe_package(KOKORO_PACKAGE, "Kokoro-82M text to speech", import_check=deep_import_check)

    runtime: Optional[PackageStatus] = None
    for candidate, description in RUNTIME_BACKENDS:
        status = probe_package(candidate, description, import_check=False)
        if status.installed:
            runtime = status
            break

    phonemizer: Optional[PackageStatus] = None
    for candidate, description in PHONEMIZER_PACKAGES:
        status = probe_package(candidate, description, import_check=False)
        if status.installed:
            phonemizer = status
            break

    files: list[Path] = []
    if model_dir is not None:
        model_dir = Path(model_dir)
        if model_dir.is_dir():
            for hint in MODEL_FILE_HINTS:
                candidate = model_dir / hint
                if candidate.exists():
                    files.append(candidate)
            # Also accept any .onnx/.pth file the user placed in the folder.
            try:
                for pattern in ("*.onnx", "*.pth", "*.bin"):
                    for extra in model_dir.glob(pattern):
                        if extra not in files:
                            files.append(extra)
            except OSError:
                pass

    notes: list[str] = []
    if package.installed and not runtime:
        notes.append(
            "Install a runtime with: pip install torch  (CPU build is enough - no GPU required)."
        )
    if package.installed and not phonemizer:
        notes.append("Install the phonemiser with: pip install misaki  (needed to read text correctly).")
    if not package.installed:
        notes.append(f"Install the voice engine with: {INSTALL_HINT}")

    status = KokoroStatus(
        package=package,
        runtime=runtime,
        phonemizer=phonemizer,
        model_dir=model_dir,
        model_files=files,
        voices_found=int(voice_count or 0),
        voices_source=voices_source,
        notes=notes,
        error=package.error,
    )

    if status.usable:
        log_event(
            Event.KOKORO_DETECTED,
            "Kokoro voice engine detected",
            logger=LOGGER,
            package=package.version or "unknown",
            runtime=runtime.name if runtime else None,
            phonemizer=phonemizer.name if phonemizer else None,
            local_model=status.local_model_present,
            voices=status.voices_found,
        )
    else:
        log_event(
            Event.KOKORO_MISSING,
            "Kokoro voice engine is not available",
            logger=LOGGER,
            package_installed=package.installed,
            runtime=runtime.name if runtime else None,
            hint=INSTALL_HINT,
        )
    return status


def install_instructions() -> Sequence[str]:
    """Ordered setup steps shown to the user when Kokoro is missing."""
    return (
        "Open a terminal (Command Prompt) in the application folder.",
        f"Install the voice engine:  {INSTALL_HINT}",
        "Install a runtime:  pip install torch   (or onnxruntime for a smaller CPU-only install)",
        "Install the phonemiser:  pip install misaki",
        "Return to the app and press 'Re-check system'.",
    )
