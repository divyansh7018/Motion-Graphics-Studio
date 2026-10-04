"""Python package detection shared by the readiness checks and the voice module.

A module is often shipped inside a differently named distribution (``PIL``
lives in ``Pillow``, ``PySide6`` in ``PySide6-Essentials``), so a naive
``importlib.metadata.version(name)`` lookup would report working packages as
missing.  Every probe in the application goes through this module so the answer
is identical everywhere.

Two rules matter for trustworthiness:

* A package is only recorded as installed **after** it has been positively
  found (distribution metadata or an import spec) - never before.
* Heavy packages (``torch``, ``onnxruntime``) are never imported during a
  probe; ``importlib.util.find_spec`` answers without loading anything, which
  keeps startup fast (directive section 47).

Regression note: an earlier version set ``installed = True`` *before* verifying
the package, so missing packages were reported as present.  The order in
:func:`probe_package` (verify, then record) is deliberate and covered by
``tests/test_packages.py``.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from typing import Optional

#: Import name -> distribution name(s).  Used when the distribution is not named
#: like the module that is imported.
DISTRIBUTION_ALIASES: dict[str, tuple[str, ...]] = {
    "PIL": ("Pillow",),
    "PySide6": ("PySide6", "PySide6-Essentials", "PySide6-Addons"),
    "soundfile": ("soundfile", "PySoundFile"),
    "espeakng_loader": ("espeakng-loader", "espeakng_loader"),
    "onnxruntime": ("onnxruntime", "onnxruntime-gpu", "onnxruntime-directml"),
    "torch": ("torch", "torch-cpu"),
    "cv2": ("opencv-python", "opencv-python-headless"),
    "mgs": ("motion-graphics-studio",),
}


@dataclass
class PackageStatus:
    """Result of probing one Python package."""

    name: str
    installed: bool
    version: str = ""
    importable: bool = False
    error: str = ""
    description: str = ""

    def line(self) -> str:
        if self.installed:
            version = f" {self.version}" if self.version else ""
            return f"{self.name}{version}"
        return f"{self.name} (not installed)"


def _distribution_version(name: str) -> Optional[str]:
    """Return the installed distribution version, or ``None`` when absent.

    Distribution names normalise ``_`` to ``-``, so both spellings are tried
    along with any known alias (see :data:`DISTRIBUTION_ALIASES`).
    """
    from importlib import metadata

    candidates: list[str] = [name.replace("_", "-"), name]
    candidates.extend(DISTRIBUTION_ALIASES.get(name, ()))

    for candidate in dict.fromkeys(candidates):
        try:
            return metadata.version(candidate)
        except metadata.PackageNotFoundError:
            continue
        except Exception:  # pragma: no cover - a damaged metadata folder
            continue
    return None


def module_is_importable(name: str) -> bool:
    """True when ``name`` can be found by the import system.

    ``find_spec`` is used rather than importing, so probing a heavy package
    never loads it.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        # A parent package may be missing, or the module may raise on import.
        return False


def probe_package(name: str, description: str = "", import_check: bool = False) -> PackageStatus:
    """Check whether *name* is installed, optionally importing it.

    *import_check* is ``False`` during startup (fast, no heavy imports) and
    ``True`` only when the user explicitly asks for a detailed check.
    """
    status = PackageStatus(name=name, installed=False, description=description)

    version = _distribution_version(name)
    if version is not None:
        status.installed = True
        status.version = version
    elif module_is_importable(name):
        # Vendored or source-checkout package without distribution metadata.
        status.installed = True

    if import_check and status.installed:
        try:
            module = __import__(name)
            status.importable = True
            module_version = getattr(module, "__version__", "")
            if module_version and not status.version:
                status.version = str(module_version)
        except Exception as exc:
            status.importable = False
            status.error = f"{name} is installed but failed to import: {exc.__class__.__name__}: {exc}"
    else:
        status.importable = status.installed
    return status


def probe_many(
    names: list[tuple[str, str, bool]],
) -> tuple[list[PackageStatus], list[tuple[str, str]], list[tuple[str, str]]]:
    """Probe several packages at once.

    *names* entries are ``(import_name, purpose, required_now)``.  Returns
    ``(statuses, missing_required, missing_optional)``.
    """
    statuses: list[PackageStatus] = []
    missing_required: list[tuple[str, str]] = []
    missing_optional: list[tuple[str, str]] = []
    for name, purpose, required_now in names:
        status = probe_package(name, purpose, import_check=False)
        statuses.append(status)
        if not status.installed:
            (missing_required if required_now else missing_optional).append((name, purpose))
    return statuses, missing_required, missing_optional


def describe(statuses: list[PackageStatus]) -> list[str]:
    """Human readable lines for the system check page."""
    return [status.line() for status in statuses]


__all__ = [
    "DISTRIBUTION_ALIASES",
    "PackageStatus",
    "describe",
    "module_is_importable",
    "probe_many",
    "probe_package",
]
