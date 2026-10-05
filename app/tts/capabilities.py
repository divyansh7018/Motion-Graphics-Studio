"""Deep Kokoro capability detection (directive sections 3, 10, 18, 22).

The rule here: **never claim Kokoro is ready because a package imported.**

Importing ``kokoro`` proves the Python module exists.  It says nothing about
whether the model weights are on disk, whether a runtime backend is installed,
whether the phonemiser for the chosen language is available, or whether the
pipeline can actually be constructed.  Each of those is checked separately and
reported separately, so the user is told the real state of their machine.

Nothing here generates audio, downloads anything or blocks for long: probing is
a directory scan plus a few guarded imports.
"""

from __future__ import annotations

import importlib
import importlib.metadata as importlib_metadata
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from app.core.errors import Severity

#: The only TTS engine in V1 (directive section 2).
ENGINE_ID = "kokoro"
ENGINE_LABEL = "Kokoro 82M (local, on-device)"

#: Runtime backends, in the order we prefer them on a CPU-only machine.
#: ONNX Runtime is first because it needs no PyTorch and has a small footprint.
RUNTIME_BACKENDS: tuple[str, ...] = ("onnxruntime", "torch")

#: Phonemiser packages Kokoro needs for text -> phonemes.  English uses misaki;
#: other languages need espeak-ng on the system.
PHONEMIZER_PACKAGES: tuple[str, ...] = ("misaki", "espeakng_loader", "phonemizer")

#: File names that indicate Kokoro model weights are present.
MODEL_FILE_HINTS: tuple[str, ...] = (
    "kokoro-82m", "kokoro_82m", "model.pt", "model.onnx", "model.bin",
)

#: File names that indicate a voice catalogue is present.
VOICE_FILE_SUFFIXES: tuple[str, ...] = (".pt", ".bin", ".onnx", ".safetensors")

#: Self-test sentence (directive section 50).  Deliberately short.
SELF_TEST_TEXT = "Hello. This is a local narration test."


@dataclass
class ModelInfo:
    """What we found about the Kokoro model weights."""

    path: Optional[Path] = None
    #: Voice files found next to the model.
    voice_files: list[Path] = field(default_factory=list)
    #: Directories that were searched.
    searched: list[Path] = field(default_factory=list)
    size_bytes: int = 0
    #: True when a plausible model file exists on disk.
    @property
    def present(self) -> bool:
        return self.path is not None and self.path.is_file()

    def describe(self) -> str:
        if not self.present:
            return "No Kokoro model weights were found."
        return f"{self.path.name} ({self.size_bytes / (1024 * 1024):.1f} MB)"


@dataclass
class RuntimeInfo:
    """The inference backend Kokoro would use."""

    name: str = ""
    version: str = ""
    available: bool = False
    note: str = ""

    @property
    def describe(self) -> str:
        if not self.available:
            # With nothing found there is no backend name, so say so plainly
            # instead of printing a stray leading colon.
            return f"{self.name}: not installed" if self.name else "not installed"
        suffix = f" {self.version}" if self.version else ""
        return f"{self.name}{suffix}" + (f" — {self.note}" if self.note else "")


@dataclass
class KokoroStatus:
    """Everything we know about the local Kokoro installation."""

    #: The kokoro package can be imported.
    installed: bool = False
    package_version: str = ""
    import_error: str = ""
    runtime: RuntimeInfo = field(default_factory=RuntimeInfo)
    model: ModelInfo = field(default_factory=ModelInfo)
    #: Voice identifiers found in the model catalogue.
    voices: list[str] = field(default_factory=list)
    #: Languages the installed pipeline actually supports.
    languages: list[str] = field(default_factory=list)
    #: Where the language list came from ("engine", "voice-id prefix", "none").
    language_source: str = "none"
    phonemizer: list[str] = field(default_factory=list)
    missing_phonemizers: list[str] = field(default_factory=list)
    #: True only after the pipeline has really been constructed once.
    verified: bool = False
    verification_error: str = ""
    #: Free-text problems, each actionable.
    problems: list[str] = field(default_factory=list)
    #: How to fix them.
    instructions: list[str] = field(default_factory=list)
    checked_at: str = ""

    # -- derived state ----------------------------------------------------
    @property
    def ready(self) -> bool:
        """True only when generation could really work right now."""
        return bool(
            self.installed
            and self.runtime.available
            and self.model.present
            and self.voices
        )

    @property
    def severity(self) -> Severity:
        """How the system check should report this (INFO / WARNING / ERROR)."""
        if self.ready and self.verified:
            return Severity.INFO
        if self.ready:
            # Found but not yet initialised: worth telling the user, not an error.
            return Severity.WARNING
        if self.installed or self.runtime.available or self.model.present:
            # Partly present: something is installed but the pipeline is broken.
            return Severity.WARNING
        return Severity.ERROR

    def headline(self) -> str:
        if self.ready:
            state = "verified" if self.verified else "found but not yet initialised"
            return (
                f"Kokoro is {state}: {len(self.voices)} voices across "
                f"{len(self.languages)} languages, backend {self.runtime.name}."
            )
        if not self.installed:
            return "Kokoro is not installed."
        if not self.runtime.available:
            return "Kokoro is installed but no inference runtime is available."
        if not self.model.present:
            return "Kokoro is installed but its model weights are missing."
        if not self.voices:
            return "Kokoro is installed but no voices were found."
        return "Kokoro is not ready."


# --------------------------------------------------------------------------
# Individual probes
# --------------------------------------------------------------------------

def probe_package(name: str = "kokoro") -> tuple[bool, str, str]:
    """Return (importable, version, error) for a package without side effects."""
    try:
        module = importlib.import_module(name)
    except Exception as error:  # noqa: BLE001 - reported, never raised
        return False, "", str(error)
    version = getattr(module, "__version__", "")
    if not version:
        try:
            version = importlib_metadata.version(name)
        except Exception:  # noqa: BLE001
            version = ""
    return True, version, ""


def probe_runtime(preferred: Optional[str] = None) -> RuntimeInfo:
    """Find the first available inference backend.

    ONNX Runtime is preferred on CPU-only hardware; PyTorch is accepted because
    the original Kokoro pipeline uses it.  A GPU is never required.
    """
    order = [preferred] if preferred else list(RUNTIME_BACKENDS)
    order += [name for name in RUNTIME_BACKENDS if name not in order]
    last_failure: Optional[RuntimeInfo] = None
    for name in order:
        try:
            module = importlib.import_module(name)
        except Exception as error:  # noqa: BLE001
            # Kept so that when nothing is available the report can say why the
            # preferred backend was rejected, instead of a bare "not installed".
            last_failure = RuntimeInfo(name=name, available=False, note=str(error)[:200])
            continue
        version = getattr(module, "__version__", "")
        if not version:
            try:
                version = importlib_metadata.version(name)
            except Exception:  # noqa: BLE001
                version = ""
        note = "CPU execution" if name == "onnxruntime" else "CPU tensors"
        return RuntimeInfo(name=name, version=version, available=True, note=note)
    if last_failure is not None:
        return RuntimeInfo(
            name="",
            available=False,
            note=f"No inference runtime installed ({last_failure.name}: {last_failure.note}).",
        )
    return RuntimeInfo(name="", available=False, note="No inference runtime installed.")


def probe_phonemizers() -> tuple[list[str], list[str]]:
    """Return (present, missing) phonemiser components."""
    present: list[str] = []
    missing: list[str] = []
    for name in PHONEMIZER_PACKAGES:
        found, _, _ = probe_package(name)
        (present if found else missing).append(name)
    if shutil.which("espeak-ng"):
        present.append("espeak-ng (system)")
    return present, missing


def candidate_model_dirs(model_dir: Optional[Path] = None,
                         extra_dirs: Iterable[Path] = ()) -> list[Path]:
    """Directories to search for Kokoro weights, best guess first."""
    dirs: list[Path] = []
    for candidate in [model_dir, *extra_dirs]:
        if candidate:
            path = Path(candidate)
            if path not in dirs:
                dirs.append(path)
    # Standard user locations, in order of likelihood.
    home = Path.home()
    for candidate in (
        home / ".cache" / "kokoro",
        home / ".cache" / "huggingface" / "hub",
        home / "AppData" / "Local" / "kokoro",
        home / "AppData" / "Local" / "huggingface" / "hub",
        Path.cwd() / "models" / "kokoro",
    ):
        if candidate not in dirs:
            dirs.append(candidate)
    return dirs


def _looks_like_model_file(path: Path) -> bool:
    name = path.name.lower()
    return any(hint in name for hint in MODEL_FILE_HINTS) and path.suffix.lower() in (
        ".pt", ".onnx", ".bin", ".safetensors", ".pth",
    )


def probe_model(model_dir: Optional[Path] = None,
                extra_dirs: Iterable[Path] = ()) -> ModelInfo:
    """Locate the Kokoro model weights and the voice files beside them."""
    dirs = candidate_model_dirs(model_dir, extra_dirs)
    info = ModelInfo(searched=[d for d in dirs])
    best: Optional[Path] = None
    for directory in dirs:
        if not directory.is_dir():
            continue
        try:
            entries = sorted(directory.rglob("*"))
        except OSError:
            continue
        for entry in entries:
            if not entry.is_file():
                continue
            if not _looks_like_model_file(entry):
                continue
            if best is None or entry.stat().st_size > best.stat().st_size:
                best = entry
        # Voices live next to the weights in the reference layout.
        for entry in entries:
            if (entry.is_file()
                    and entry.suffix.lower() in VOICE_FILE_SUFFIXES
                    and not _looks_like_model_file(entry)):
                if entry not in info.voice_files:
                    info.voice_files.append(entry)
    if best is not None:
        info.path = best
        try:
            info.size_bytes = best.stat().st_size
        except OSError:
            info.size_bytes = 0
    return info


def _language_codes_from_engine() -> dict[str, str]:
    """Ask the installed Kokoro package for its own language table.

    Returns an empty mapping when the package is absent or the table is not
    exposed, so callers fall back to the voice-id prefix instead of guessing.
    """
    codes: dict[str, str] = {}
    for module_name in ("kokoro", "kokoro.pipeline", "kokoro_onnx"):
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001
            continue
        for attribute in ("LANG_CODES", "LANGUAGE_CODES", "LANGUAGES"):
            table = getattr(module, attribute, None)
            if isinstance(table, dict) and table:
                for key, value in table.items():
                    codes[str(key)] = str(value)
                if codes:
                    return codes
    return codes


def probe_voices(model: ModelInfo,
                 extra_dirs: Iterable[Path] = ()) -> tuple[list[str], list[str], str]:
    """Discover voices from disk.

    Voice identifiers are read from the installed catalogue, never from a list in
    this file (directive sections 5-6).  Returns (voice_ids, languages, source).
    """
    files = list(model.voice_files)
    if model.path is not None and model.path.parent not in {f.parent for f in files}:
        try:
            files.extend(
                entry for entry in sorted(model.path.parent.iterdir())
                if entry.is_file() and entry.suffix.lower() in VOICE_FILE_SUFFIXES
                and not _looks_like_model_file(entry)
            )
        except OSError:
            pass
    for directory in extra_dirs:
        path = Path(directory)
        if not path.is_dir():
            continue
        try:
            files.extend(
                entry for entry in sorted(path.rglob("*"))
                if entry.is_file() and entry.suffix.lower() in VOICE_FILE_SUFFIXES
                and not _looks_like_model_file(entry)
            )
        except OSError:
            continue

    voices: list[str] = []
    seen: set[str] = set()
    for entry in files:
        # "af_bella.pt" -> "af_bella"; skip the model file itself.
        identifier = entry.stem
        if not identifier or identifier.lower() in {"voices", "model", "voice"}:
            continue
        if identifier in seen:
            continue
        seen.add(identifier)
        voices.append(identifier)
    voices.sort()

    codes = _language_codes_from_engine()
    if codes:
        languages = sorted({
            label for voice in voices
            for key, label in codes.items()
            if voice.lower().startswith(key.lower())
        })
        return voices, languages, "engine"

    # No engine table: group by the voice-id prefix Kokoro uses.  Reported with
    # an explicit source so the interface can label it honestly.
    languages = sorted({voice[:2].lower() for voice in voices if len(voice) >= 2})
    if languages:
        return voices, languages, "voice-id prefix"
    return voices, [], "none"


# --------------------------------------------------------------------------
# Full probe
# --------------------------------------------------------------------------

def probe_kokoro(model_dir: Optional[Path] = None,
                 extra_dirs: Iterable[Path] = (),
                 runtime: Optional[str] = None,
                 deep_init_check: bool = False) -> KokoroStatus:
    """Collect the real state of the local Kokoro installation."""
    status = KokoroStatus()
    installed, version, error = probe_package("kokoro")
    status.installed = installed
    status.package_version = version
    status.import_error = error
    if not installed:
        status.problems.append("The Kokoro package could not be imported.")
        status.instructions.append(
            "Install it with:  python -m pip install kokoro onnxruntime"
        )

    status.runtime = probe_runtime(runtime)
    if not status.runtime.available:
        status.problems.append(
            "No inference runtime is available. Kokoro needs ONNX Runtime "
            "(recommended, CPU-only) or PyTorch."
        )
        status.instructions.append("Install a runtime with:  python -m pip install onnxruntime")

    status.model = probe_model(model_dir, extra_dirs)
    if not status.model.present:
        searched = ", ".join(str(p) for p in status.model.searched[:4])
        status.problems.append(f"No Kokoro model weights were found (searched: {searched}).")
        status.instructions.append(
            "Download Kokoro 82M weights into the app's models folder, or set the "
            "Kokoro model path in Settings."
        )

    status.voices, status.languages, status.language_source = probe_voices(
        status.model, extra_dirs
    )
    if status.model.present and not status.voices:
        status.problems.append(
            "The model was found but no voice files were beside it."
        )
        status.instructions.append(
            "Place the Kokoro voice files in the same folder as the model weights."
        )

    present, missing = probe_phonemizers()
    status.phonemizer = present
    status.missing_phonemizers = missing
    if status.ready and not present:
        status.problems.append(
            "No phonemiser was found. English needs 'misaki'; other languages need espeak-ng."
        )
        status.instructions.append(
            "Install with:  python -m pip install 'misaki[en]' espeakng-loader"
        )

    if deep_init_check and status.ready:
        from app.tts.engine import KokoroEngine  # local import: avoids cycles

        engine = KokoroEngine(model_path=status.model.path,
                              runtime=status.runtime.name or None)
        try:
            engine.load()
            status.verified = True
        except Exception as error:  # noqa: BLE001 - reported, never raised
            status.verification_error = str(error)
            status.problems.append(f"Kokoro could not be initialised: {error}")
        finally:
            engine.unload()

    if status.verified:
        status.problems = [p for p in status.problems]
    return status


def requirements_summary(status: KokoroStatus) -> dict[str, Any]:
    """A small, serialisable snapshot for logs, reports and the system check."""
    return {
        "engine": ENGINE_ID,
        "installed": status.installed,
        "package_version": status.package_version,
        "runtime": status.runtime.name,
        "runtime_version": status.runtime.version,
        "model_present": status.model.present,
        "model_path": str(status.model.path) if status.model.path else "",
        "voice_count": len(status.voices),
        "language_count": len(status.languages),
        "language_source": status.language_source,
        "phonemizers": list(status.phonemizer),
        "missing_phonemizers": list(status.missing_phonemizers),
        "verified": status.verified,
        "ready": status.ready,
        "problems": list(status.problems),
        "python": sys.version.split()[0],
        "platform": sys.platform,
    }


__all__ = [
    "ENGINE_ID",
    "ENGINE_LABEL",
    "KokoroStatus",
    "MODEL_FILE_HINTS",
    "ModelInfo",
    "PHONEMIZER_PACKAGES",
    "RUNTIME_BACKENDS",
    "RuntimeInfo",
    "SELF_TEST_TEXT",
    "VOICE_FILE_SUFFIXES",
    "candidate_model_dirs",
    "probe_kokoro",
    "probe_model",
    "probe_package",
    "probe_phonemizers",
    "probe_runtime",
    "probe_voices",
    "requirements_summary",
]
