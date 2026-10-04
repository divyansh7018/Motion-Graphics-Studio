"""Environment and hardware probe (directive sections 2, 46, 64).

Only facts that the application actually reacts to are collected:

* is this a supported platform for the CPU-first target machine?
* how many CPU threads may we use for Kokoro / FFmpeg?
* how much RAM and free disk space do we have (warn before heavy renders)?

``psutil`` is optional: when it is missing everything still works, just with
less detail.  The probe never raises - a machine where something cannot be
detected reports ``None`` rather than crashing startup.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:  # optional dependency - never required for the application to run
    import psutil  # type: ignore
except Exception:  # pragma: no cover - depends on the environment
    psutil = None  # type: ignore

#: RAM below this is flagged as a warning for video work (section 31).
RECOMMENDED_RAM_BYTES = 16 * 1024**3

#: Free disk below this before rendering is flagged (section 36/67).
LOW_DISK_WARNING_BYTES = 8 * 1024**3

#: Free disk below which the app refuses to start a render (P1 guard).
LOW_DISK_BLOCK_BYTES = 2 * 1024**3


@dataclass(frozen=True)
class EnvironmentInfo:
    """Snapshot of the machine the application is running on."""

    platform: str
    platform_release: str
    platform_version: str
    machine: str
    processor: str
    python_version: str
    python_executable: str
    is_windows: bool
    is_64bit: bool
    cpu_count_logical: int
    cpu_count_physical: Optional[int]
    cpu_frequency_mhz: Optional[float]
    ram_total_bytes: Optional[int]
    ram_available_bytes: Optional[int]
    gpu_names: tuple[str, ...] = ()
    psutil_available: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    # -- derived helpers --------------------------------------------------

    @property
    def ram_total_gb(self) -> Optional[float]:
        return None if self.ram_total_bytes is None else self.ram_total_bytes / 1024**3

    @property
    def ram_available_gb(self) -> Optional[float]:
        return None if self.ram_available_bytes is None else self.ram_available_bytes / 1024**3

    def summary_lines(self) -> list[str]:
        """Lines shown on the System Check page."""
        lines = [
            f"Operating system : {self.platform} {self.platform_release} ({self.machine})",
            f"CPU              : {self.processor or 'unknown'} - {self.cpu_count_logical} logical cores",
            "Memory           : "
            + (f"{self.ram_total_gb:.1f} GB total, {self.ram_available_gb:.1f} GB free" if self.ram_total_bytes else "unknown"),
            f"Python           : {self.python_version} ({'64-bit' if self.is_64bit else '32-bit'})",
            f"Graphics         : {', '.join(self.gpu_names) if self.gpu_names else 'not detected (CPU rendering)'}",
        ]
        for note in self.notes:
            lines.append(f"Note             : {note}")
        return lines

    def as_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "platform_release": self.platform_release,
            "machine": self.machine,
            "processor": self.processor,
            "python": self.python_version,
            "is_windows": self.is_windows,
            "is_64bit": self.is_64bit,
            "cpu_logical": self.cpu_count_logical,
            "cpu_physical": self.cpu_count_physical,
            "cpu_mhz": self.cpu_frequency_mhz,
            "ram_total_bytes": self.ram_total_bytes,
            "ram_available_bytes": self.ram_available_bytes,
            "gpu_names": list(self.gpu_names),
            "psutil": self.psutil_available,
            "notes": list(self.notes),
        }


def probe_environment() -> EnvironmentInfo:
    """Collect environment facts.  Never raises."""
    notes: list[str] = []

    try:
        platform_name = platform.system()
        platform_release = platform.release()
        platform_version = platform.version()
        machine = platform.machine()
        processor = platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "")
    except Exception as exc:  # pragma: no cover - extremely defensive
        platform_name, platform_release, platform_version, machine, processor = "unknown", "", "", "", ""
        notes.append(f"Platform details could not be read: {exc}")

    is_windows = os.name == "nt"
    is_64bit = sys.maxsize > 2**32

    cpu_logical = os.cpu_count() or 1
    cpu_physical: Optional[int] = None
    cpu_mhz: Optional[float] = None
    ram_total: Optional[int] = None
    ram_available: Optional[int] = None

    if psutil is not None:
        try:
            cpu_physical = psutil.cpu_count(logical=False)
            freq = psutil.cpu_freq()
            cpu_mhz = float(freq.max or freq.current) if freq else None
            memory = psutil.virtual_memory()
            ram_total = int(memory.total)
            ram_available = int(memory.available)
        except Exception as exc:  # pragma: no cover - platform dependent
            notes.append(f"psutil could not read some hardware details: {exc}")
    else:
        notes.append("psutil is not installed - RAM monitoring is limited (optional).")

    gpu_names = _detect_gpu_names()

    if not is_windows:
        notes.append(
            "This build's primary target is Windows 10/11. You are running on "
            f"{platform_name or 'an unsupported platform'}, which is supported for development only."
        )
    if not is_64bit:
        notes.append("A 64-bit Python is required for video rendering; 32-bit Python will run out of memory.")
    if ram_total is not None and ram_total < RECOMMENDED_RAM_BYTES:
        notes.append(
            f"Only {ram_total / 1024**3:.1f} GB RAM detected. 16 GB or more is recommended for HD rendering."
        )
    if not gpu_names:
        notes.append("No dedicated GPU detected. This is fine - rendering runs on the CPU by default.")

    return EnvironmentInfo(
        platform=platform_name,
        platform_release=platform_release,
        platform_version=platform_version,
        machine=machine,
        processor=processor,
        python_version=platform.python_version(),
        python_executable=sys.executable,
        is_windows=is_windows,
        is_64bit=is_64bit,
        cpu_count_logical=cpu_logical,
        cpu_count_physical=cpu_physical,
        cpu_frequency_mhz=cpu_mhz,
        ram_total_bytes=ram_total,
        ram_available_bytes=ram_available,
        gpu_names=tuple(gpu_names),
        psutil_available=psutil is not None,
        notes=tuple(notes),
    )


def _detect_gpu_names() -> list[str]:
    """Best-effort GPU names; an empty list simply means 'CPU only'."""
    names: list[str] = []
    try:
        if os.name == "nt":  # pragma: no cover - Windows only
            names.extend(_windows_gpu_names())
    except Exception:
        pass
    return [name for name in names if name]


def _windows_gpu_names() -> list[str]:  # pragma: no cover - Windows only
    import subprocess

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "(Get-CimInstance Win32_VideoController).Name",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=creation_flags,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def effective_worker_threads(reserve: int = 1) -> int:
    """Thread count for CPU-bound work (Kokoro, FFmpeg, Pillow).

    The target machine is CPU-first (section 2), so we use most cores but keep
    one free so the Qt user interface stays responsive (section 7/77).
    """
    logical = os.cpu_count() or 1
    return max(1, logical - reserve)


def disk_free_bytes(path: Path) -> int:
    """Free space on the volume containing *path* (0 when unknown)."""
    try:
        return int(shutil.disk_usage(str(path)).free)
    except OSError:
        return 0


def disk_total_bytes(path: Path) -> int:
    try:
        return int(shutil.disk_usage(str(path)).total)
    except OSError:
        return 0


def check_disk_space(path: Path, required_bytes: int) -> tuple[bool, int]:
    """Return ``(has_enough, free_bytes)`` for a planned write."""
    free = disk_free_bytes(path)
    if free == 0:  # unknown - do not block the user on a failed probe
        return True, free
    return free >= required_bytes, free


def current_memory_usage() -> Optional[tuple[int, int]]:
    """Return ``(used_bytes, total_bytes)`` for the current process, if known."""
    if psutil is None:
        return None
    try:
        process = psutil.Process(os.getpid())
        return int(process.memory_info().rss), int(psutil.virtual_memory().total)
    except Exception:
        return None


def process_cpu_percent() -> Optional[float]:
    """CPU usage of this process (``None`` when psutil is unavailable)."""
    if psutil is None:
        return None
    try:
        return float(psutil.Process(os.getpid()).cpu_percent(interval=None))
    except Exception:
        return None


def system_cpu_percent() -> Optional[float]:
    if psutil is None:
        return None
    try:
        return float(psutil.cpu_percent(interval=None))
    except Exception:
        return None


def bundle_available() -> bool:
    """True when running from a frozen bundle (PyInstaller) rather than source."""
    return bool(getattr(sys, "frozen", False))
