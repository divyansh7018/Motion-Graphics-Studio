"""Device detection (Stage F, sections 38, 39).

The application must open and stay usable on a CPU-only machine, so nothing here
requires a GPU and nothing loads a driver.  The numbers are measured, not
assumed: a machine with no NVIDIA card reports that plainly, and the UI uses it
to warn about slow generations rather than to refuse to start.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Optional

__all__ = ["DeviceInfo", "detect_device", "recommend_size"]


@dataclass
class DeviceInfo:
    """What this machine can actually run."""

    cpu_count: int = 0
    cpu_name: str = ""
    ram_gb: float = 0.0
    #: "cpu" | "cuda" | "directml" | "mps"
    accelerator: str = "cpu"
    gpu_name: str = ""
    vram_gb: float = 0.0
    cuda_available: bool = False
    #: What was checked, so a report can show its work.
    probes: list = field(default_factory=list)

    @property
    def has_gpu(self) -> bool:
        return self.accelerator != "cpu" and bool(self.gpu_name)

    @property
    def cpu_only(self) -> bool:
        return not self.has_gpu

    def describe(self) -> str:
        parts = [f"{self.cpu_count} CPU thread(s)"]
        if self.cpu_name:
            parts.append(self.cpu_name)
        parts.append(f"{self.ram_gb:.1f} GB RAM")
        if self.has_gpu:
            parts.append(f"{self.gpu_name} ({self.vram_gb:.1f} GB VRAM, "
                         f"{self.accelerator})")
        else:
            parts.append("CPU only - no GPU detected")
        return ", ".join(parts)

    def to_dict(self) -> dict:
        return {"cpu_count": self.cpu_count, "cpu_name": self.cpu_name,
                "ram_gb": round(self.ram_gb, 2), "accelerator": self.accelerator,
                "gpu_name": self.gpu_name, "vram_gb": round(self.vram_gb, 2),
                "cuda_available": bool(self.cuda_available),
                "has_gpu": self.has_gpu, "cpu_only": self.cpu_only,
                "probes": list(self.probes)}


def detect_device(*, deep: bool = False) -> DeviceInfo:
    """Measure the machine.  Never raises, never loads a model.

    ``deep=False`` (the default) deliberately does **not** import torch.
    Importing it costs roughly half a gigabyte of resident memory just to report
    whether a GPU exists, and Image Studio is opened far more often than a model
    is run - measured at +485 MiB on the Stage F test machine.  ``nvidia-smi``,
    which is a cheap subprocess, covers the common case.

    Pass ``deep=True`` only for an explicit device report.
    """
    info = DeviceInfo()

    try:
        info.cpu_count = int(os.cpu_count() or 0)
    except Exception:  # noqa: BLE001
        info.cpu_count = 0

    try:
        import psutil

        info.ram_gb = psutil.virtual_memory().total / (1024 ** 3)
        info.probes.append("psutil: CPU and RAM measured")
    except ImportError:
        info.probes.append("psutil not installed: RAM unknown")

    info.cpu_name = _cpu_name()

    gpu = _probe_nvidia_smi()
    if gpu is not None:
        info.gpu_name, info.vram_gb = gpu
        info.accelerator = "cuda"
        info.cuda_available = True
        info.probes.append("nvidia-smi: CUDA GPU found")
    else:
        info.probes.append("nvidia-smi: not found or no GPU")

    if not info.has_gpu and deep:
        installed, name, vram, accelerator = _probe_torch()
        if not installed:
            info.probes.append("torch: not installed, so no accelerator probed")
        elif not accelerator:
            # Saying "not installed" here would be untrue: it is installed and
            # simply has no accelerator available.
            info.probes.append("torch: installed, but no accelerator is "
                               "available")
        else:
            info.gpu_name, info.vram_gb = name, vram
            info.accelerator = accelerator
            info.cuda_available = accelerator == "cuda"
            info.probes.append(f"torch: {accelerator} available")
    elif not info.has_gpu:
        info.probes.append("torch: not probed (importing it costs about "
                           "500 MB; use a deep check to include it)")

    return info


def _cpu_name() -> str:
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as handle:
            for line in handle:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def _probe_nvidia_smi() -> Optional[tuple[str, float]]:
    """Ask ``nvidia-smi`` if it exists.  A subprocess, not a driver import."""
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    first = (result.stdout or "").strip().splitlines()
    if not first:
        return None
    parts = [part.strip() for part in first[0].split(",")]
    name = parts[0] if parts else ""
    vram = 0.0
    if len(parts) > 1:
        try:
            vram = float(parts[1]) / 1024.0
        except ValueError:
            vram = 0.0
    return name, vram


def _probe_torch() -> tuple[bool, str, float, str]:
    """Ask torch for an accelerator, distinguishing the three real outcomes.

    Returns ``(installed, name, vram_gb, accelerator)``.  "Installed but no
    accelerator" is a different answer from "not installed", and reporting the
    second when the first is true is a lie the user can check.
    """
    try:
        import torch  # type: ignore
    except ImportError:
        return False, "", 0.0, ""
    try:
        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            vram = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
            return True, str(name), float(vram), "cuda"
        backend = getattr(torch.backends, "mps", None)
        if backend is not None and backend.is_available():
            return True, "Apple Silicon GPU", 0.0, "mps"
    except Exception:  # noqa: BLE001 - a broken torch is not our problem
        return True, "", 0.0, ""
    return True, "", 0.0, ""


def recommend_size(device: DeviceInfo, requested_width: int,
                   requested_height: int) -> tuple[int, int, str]:
    """A smaller size for a CPU-only machine, offered never forced.

    Returns ``(width, height, reason)``.  The caller shows "Use Recommended" and
    "Continue Anyway"; the requested size is never changed behind the user's
    back (section 39).
    """
    width = int(requested_width or 0)
    height = int(requested_height or 0)
    if width <= 0 or height <= 0:
        return width, height, ""
    if not device.cpu_only:
        return width, height, ""
    pixels = width * height
    # About a megapixel is a reasonable CPU target; beyond that a diffusion
    # model on CPU can take many minutes per image.
    limit = 1024 * 1024
    if pixels <= limit:
        return width, height, ""
    scale = (limit / float(pixels)) ** 0.5
    new_width = max(64, int(round(width * scale / 8.0)) * 8)
    new_height = max(64, int(round(height * scale / 8.0)) * 8)
    return (new_width, new_height,
            f"No GPU was detected. {width}x{height} on a CPU can take many "
            f"minutes per image; {new_width}x{new_height} keeps the same shape "
            "and is much faster.")
