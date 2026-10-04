"""FFmpeg / FFprobe discovery and safe invocation (directive sections 29, 30, 42).

Why this module exists as its own unit:

* FFmpeg is **never assumed** to be installed globally.  Discovery searches an
  explicit override, the bundled ``tools/`` folder, next to the Python
  interpreter, the Windows registry-free "known" locations, and finally PATH.
* Every subprocess call uses an argument list (no ``shell=True``), captures
  stdout/stderr/exit code, and can be cancelled deterministically.
* Nothing is executed at import time - discovery only runs when a check or a
  render explicitly asks for it.

The Stage F renderer will reuse :class:`FFmpegTools` for encoding; keeping the
detection and the invocation in one tested place prevents the "works on my
machine" path handling that plagues media tools.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

from ..core.errors import JobCancelled, FFmpegNotFoundError
from ..core.events import Event
from ..core.logging_setup import get_logger, log_event

LOGGER = get_logger("ffmpeg")

VERSION_TIMEOUT_SECONDS = 8.0

#: Names to look for, in order of preference.
FFMPEG_EXE_NAMES = ("ffmpeg.exe", "ffmpeg") if os.name == "nt" else ("ffmpeg",)
FFPROBE_EXE_NAMES = ("ffprobe.exe", "ffprobe") if os.name == "nt" else ("ffprobe",)

#: Sub-folders commonly used when someone unpacks FFmpeg next to the app.
TOOL_SUBFOLDERS = ("tools", "tools/bin", "tools/ffmpeg", "tools/ffmpeg/bin", "bin", "ffmpeg", "ffmpeg/bin")

VERSION_RE = re.compile(r"(?:ffmpeg|ffprobe) version\s+(\S+)", re.IGNORECASE)
BUILD_RE = re.compile(r"built with\s+([^\n]+)", re.IGNORECASE)


class FFmpegCancelToken:
    """Minimal cancellation token shared with long running FFmpeg jobs.

    Kept deliberately small: a render loop (Stage F) checks ``is_cancelled()``
    between frames and calls :meth:`kill_active_process` to stop the encoder.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._processes: list[subprocess.Popen] = []
        self._lock = threading.Lock()

    def cancel(self) -> None:
        self._event.set()
        self.kill_active_process()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise JobCancelled("Cancelled by the user.")

    # -- process tracking --------------------------------------------------

    def register(self, process: subprocess.Popen) -> None:
        with self._lock:
            self._processes.append(process)

    def unregister(self, process: subprocess.Popen) -> None:
        with self._lock:
            if process in self._processes:
                self._processes.remove(process)

    def kill_active_process(self, grace_seconds: float = 3.0) -> int:
        """Terminate any registered child process.  Returns how many were killed."""
        with self._lock:
            processes = list(self._processes)
        killed = 0
        for process in processes:
            if process.poll() is None:
                try:
                    process.terminate()
                    try:
                        process.wait(timeout=grace_seconds)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    killed += 1
                except OSError:  # pragma: no cover - process already gone
                    pass
        return killed


@dataclass
class ToolInfo:
    """A discovered executable plus its version string."""

    name: str                      # "ffmpeg" | "ffprobe"
    path: Path
    version: str = ""
    detail: str = ""
    source: str = ""               # how it was found (for the log)

    @property
    def is_available(self) -> bool:
        return bool(self.path) and Path(self.path).exists()

    def version_line(self) -> str:
        return f"{self.name}: {self.version or 'version unknown'} ({self.path})"


@dataclass
class FFmpegDiscovery:
    """Result of a FFmpeg search."""

    ffmpeg: Optional[ToolInfo] = None
    ffprobe: Optional[ToolInfo] = None
    searched: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cached_at: float = 0.0

    @property
    def has_ffmpeg(self) -> bool:
        return self.ffmpeg is not None and self.ffmpeg.is_available

    @property
    def has_ffprobe(self) -> bool:
        return self.ffprobe is not None and self.ffprobe.is_available

    @property
    def is_complete(self) -> bool:
        """Both tools are present - required for rendering and QC."""
        return self.has_ffmpeg and self.has_ffprobe

    def summary(self) -> str:
        if self.is_complete:
            return f"FFmpeg {self.ffmpeg.version} and FFprobe {self.ffprobe.version} ready."
        if self.has_ffmpeg and not self.has_ffprobe:
            return "FFmpeg found, but FFprobe is missing (needed to verify finished videos)."
        if self.has_ffprobe and not self.has_ffmpeg:
            return "FFprobe found, but FFmpeg is missing (needed to create videos)."
        return "FFmpeg and FFprobe were not found."


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def _candidate_directories(
    source_root: Optional[Path],
    ffmpeg_dir_setting: str = "",
    extra_dirs: Sequence[Path] = (),
) -> list[tuple[Path, str]]:
    """Directories to search, in priority order, with a label for the log."""
    candidates: list[tuple[Path, str]] = []

    setting = (ffmpeg_dir_setting or "").strip()
    if setting:
        candidates.append((Path(setting).expanduser(), "settings"))

    if source_root is not None:
        for sub in TOOL_SUBFOLDERS:
            candidates.append((Path(source_root) / sub, "app tools folder"))

    # A tools folder next to the running executable (frozen builds).
    import sys

    try:
        exe_dir = Path(sys.executable).resolve().parent
        for sub in TOOL_SUBFOLDERS:
            candidates.append((exe_dir / sub, "python/tools folder"))
        candidates.append((exe_dir, "python folder"))
    except OSError:  # pragma: no cover
        pass

    for extra in extra_dirs:
        candidates.append((Path(extra), "extra search path"))

    # Common Windows install locations (checked without touching the registry).
    if os.name == "nt":  # pragma: no cover - Windows only
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
            if base:
                candidates.append((Path(base) / "ffmpeg" / "bin", "common install folder"))
        candidates.append((Path("C:/ffmpeg/bin"), "common install folder"))

    return candidates


def _which(name: str) -> tuple[Optional[Path], str]:
    found = shutil.which(name)
    if found:
        return Path(found), "system PATH"
    return None, ""


def discover_ffmpeg(
    source_root: Optional[Path] = None,
    ffmpeg_dir_setting: str = "",
    ffmpeg_exe_setting: str = "",
    ffprobe_exe_setting: str = "",
    extra_dirs: Sequence[Path] = (),
    verify_version: bool = True,
) -> FFmpegDiscovery:
    """Locate FFmpeg and FFprobe.

    Priority for each tool:
    1. explicit executable path from settings
    2. bundled ``tools/`` folder next to the app
    3. common install folders
    4. ``PATH`` (only when nothing else matched)

    When *verify_version* is set (the default) each candidate is executed once
    with ``-version`` - a file that exists but cannot run is rejected, which is
    the usual cause of "FFmpeg is installed but nothing works".
    """
    discovery = FFmpegDiscovery(cached_at=time.time())
    searched: list[str] = []
    errors: list[str] = []

    ffmpeg_exe = (ffmpeg_exe_setting or "").strip()
    ffprobe_exe = (ffprobe_exe_setting or "").strip()

    # 1. Explicit executable overrides ------------------------------------
    if ffmpeg_exe:
        path = Path(ffmpeg_exe).expanduser()
        searched.append(str(path))
        if path.is_file():
            info, error = _probe_tool("ffmpeg", path, "settings (explicit path)", verify_version)
            if error:
                errors.append(error)
            else:
                discovery.ffmpeg = info
        elif path.is_dir():
            errors.append(
                f"The FFmpeg path in Settings points to a folder, not an executable: {path}. "
                f"Please select {FFMPEG_EXE_NAMES[0]} inside it."
            )

    if ffprobe_exe:
        path = Path(ffprobe_exe).expanduser()
        searched.append(str(path))
        if path.is_file():
            info, error = _probe_tool("ffprobe", path, "settings (explicit path)", verify_version)
            if error:
                errors.append(error)
            else:
                discovery.ffprobe = info

    # 2/3. Search folders ---------------------------------------------------
    directories = _candidate_directories(source_root, ffmpeg_dir_setting, extra_dirs)
    for directory, label in directories:
        if not directory.is_dir():
            continue
        # Record the folder itself so the report can show where we looked, even
        # when it contains no FFmpeg ("searched=0" was misleading before).
        searched.append(f"{directory} [{label}]")
        for names, attribute in ((FFMPEG_EXE_NAMES, "ffmpeg"), (FFPROBE_EXE_NAMES, "ffprobe")):
            if getattr(discovery, attribute) is not None:
                continue
            for name in names:
                candidate = directory / name
                if not candidate.is_file():
                    continue
                searched.append(str(candidate))
                info, error = _probe_tool(attribute, candidate, label, verify_version)
                if error:
                    # A file that cannot be executed is NOT a usable tool: record
                    # the reason and keep searching (this is the common
                    # "ffmpeg.exe is there but Windows blocks it" case).
                    errors.append(error)
                    continue
                setattr(discovery, attribute, info)
                break

    # 4. PATH ---------------------------------------------------------------
    if discovery.ffmpeg is None:
        for name in FFMPEG_EXE_NAMES:
            found, source = _which(name)
            if found:
                searched.append(str(found))
                info, error = _probe_tool("ffmpeg", found, source, verify_version)
                if error:
                    errors.append(error)
                    continue
                discovery.ffmpeg = info
                break
    if discovery.ffprobe is None:
        for name in FFPROBE_EXE_NAMES:
            found, source = _which(name)
            if found:
                searched.append(str(found))
                info, error = _probe_tool("ffprobe", found, source, verify_version)
                if error:
                    errors.append(error)
                    continue
                discovery.ffprobe = info
                break

    discovery.searched = searched
    discovery.errors = errors

    if discovery.is_complete:
        log_event(
            Event.FFMPEG_DETECTED,
            "FFmpeg and FFprobe detected",
            logger=LOGGER,
            ffmpeg=str(discovery.ffmpeg.path),
            ffmpeg_version=discovery.ffmpeg.version,
            ffprobe=str(discovery.ffprobe.path),
            ffprobe_version=discovery.ffprobe.version,
        )
    else:
        log_event(
            Event.FFMPEG_MISSING,
            discovery.summary(),
            level=logging.WARNING,
            logger=LOGGER,
            ffmpeg=str(discovery.ffmpeg.path) if discovery.ffmpeg else None,
            ffprobe=str(discovery.ffprobe.path) if discovery.ffprobe else None,
            searched=len(searched),
        )
    return discovery


def _probe_tool(
    name: str,
    path: Path,
    source: str,
    verify_version: bool,
) -> tuple[ToolInfo, Optional[str]]:
    """Validate a candidate executable by running ``-version``."""
    info = ToolInfo(name=name, path=path, source=source)
    if not verify_version:
        return info, None

    result = run_capture([str(path), "-version"], timeout=VERSION_TIMEOUT_SECONDS)
    if not result.ok:
        message = (
            f"'{path}' was found but could not be run"
            + (f" (exit code {result.returncode})." if result.returncode is not None else ".")
        )
        if result.error:
            message += f" {result.error}"
        return info, message

    text = result.stdout or ""
    match = VERSION_RE.search(text)
    info.version = match.group(1) if match else ""
    build = BUILD_RE.search(text)
    if build:
        info.detail = build.group(1).strip()
    return info, None


# --------------------------------------------------------------------------
# Safe subprocess execution
# --------------------------------------------------------------------------

@dataclass
class CommandResult:
    """Outcome of one subprocess call - stdout, stderr and exit code (section 29)."""

    argv: list[str]
    returncode: Optional[int]
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    duration_seconds: float = 0.0
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.cancelled

    @property
    def output(self) -> str:
        return f"{self.stdout}\n{self.stderr}".strip()

    def tail(self, lines: int = 12) -> str:
        """Last few lines of output - the useful part of an FFmpeg error."""
        combined = [line for line in self.output.splitlines() if line.strip()]
        return "\n".join(combined[-lines:])

    def describe_failure(self) -> str:
        if self.cancelled:
            return "The command was cancelled."
        if self.error:
            return self.error
        return self.tail() or f"Command exited with code {self.returncode}."


def _subprocess_kwargs() -> dict:
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "nt":  # pragma: no cover - Windows only
        # Keep console windows from flashing up when the GUI runs FFmpeg.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return kwargs


def run_capture(
    argv: Sequence[str],
    timeout: float = 30.0,
    cancel_token: Optional[FFmpegCancelToken] = None,
    cwd: Optional[Path] = None,
) -> CommandResult:
    """Run a command safely and capture its output.

    * argument list only (never ``shell=True``)
    * output is bounded - runaway tools cannot exhaust memory
    * cancellation terminates the child process and closes the pipes
    """
    argv_list = [str(item) for item in argv]
    started = time.time()
    result = CommandResult(argv=argv_list, returncode=None)

    if cancel_token is not None and cancel_token.is_cancelled():
        result.cancelled = True
        result.error = "Cancelled before the command started."
        return result

    try:
        process = subprocess.Popen(argv_list, cwd=str(cwd) if cwd else None, **_subprocess_kwargs())
    except FileNotFoundError as exc:
        result.returncode = None
        result.error = f"'{argv_list[0]}' could not be started (file not found). {exc}"
        result.duration_seconds = time.time() - started
        return result
    except OSError as exc:
        result.returncode = None
        result.error = f"'{argv_list[0]}' could not be started: {exc}"
        result.duration_seconds = time.time() - started
        return result

    if cancel_token is not None:
        cancel_token.register(process)

    try:
        stdout, stderr = process.communicate(timeout=timeout if timeout > 0 else None)
        result.returncode = process.returncode
        result.stdout = _bounded(stdout)
        result.stderr = _bounded(stderr)
    except subprocess.TimeoutExpired:
        _terminate(process)
        result.error = f"The command did not finish within {timeout:.0f} seconds and was stopped."
        result.returncode = process.returncode
    except (OSError, ValueError) as exc:  # pragma: no cover - defensive
        _terminate(process)
        result.error = f"Communication with the command failed: {exc}"
    finally:
        if cancel_token is not None:
            cancel_token.unregister(process)
        if process.poll() is None:  # pragma: no cover - safety net
            _terminate(process)

    if cancel_token is not None and cancel_token.is_cancelled():
        result.cancelled = True

    result.duration_seconds = time.time() - started
    return result


def _bounded(text: Optional[str], limit: int = 64_000) -> str:
    """Keep the head and the tail of long output (the useful parts)."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [output truncated] ...\n{text[-half:]}"


def _terminate(process: subprocess.Popen, grace_seconds: float = 3.0) -> None:
    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:  # pragma: no cover
            pass


# --------------------------------------------------------------------------
# Tool wrapper
# --------------------------------------------------------------------------

class FFmpegTools:
    """Convenience wrapper around a discovery result.

    Stage F uses :meth:`run` and :meth:`probe`; Stage A uses :meth:`ensure_available`
    so that a user gets a friendly error *before* a long render starts.
    """

    def __init__(self, discovery: FFmpegDiscovery) -> None:
        self.discovery = discovery

    # -- state -------------------------------------------------------------

    @property
    def ffmpeg(self) -> Optional[Path]:
        return self.discovery.ffmpeg.path if self.discovery.ffmpeg else None

    @property
    def ffprobe(self) -> Optional[Path]:
        return self.discovery.ffprobe.path if self.discovery.ffprobe else None

    @property
    def is_complete(self) -> bool:
        return self.discovery.is_complete

    def ensure_available(self) -> None:
        """Raise a friendly error when media tools are missing."""
        if self.is_complete:
            return
        missing = "FFmpeg" if not self.discovery.has_ffmpeg else "FFprobe"
        raise FFmpegNotFoundError(
            program=missing,
            searched=self.discovery.searched[:6],
            technical="\n".join(self.discovery.errors) or None,
        )

    # -- execution ---------------------------------------------------------

    def run(
        self,
        args: Sequence[str],
        timeout: float = 300.0,
        cancel_token: Optional[FFmpegCancelToken] = None,
        cwd: Optional[Path] = None,
    ) -> CommandResult:
        """Run FFmpeg with *args* appended to the executable path."""
        self.ensure_available()
        return run_capture([str(self.ffmpeg), *[str(a) for a in args]], timeout=timeout, cancel_token=cancel_token, cwd=cwd)

    def probe(
        self,
        args: Sequence[str],
        timeout: float = 60.0,
        cancel_token: Optional[FFmpegCancelToken] = None,
    ) -> CommandResult:
        """Run FFprobe with *args* appended to the executable path."""
        if not self.discovery.has_ffprobe:
            self.ensure_available()
        return run_capture([str(self.ffprobe), *[str(a) for a in args]], timeout=timeout, cancel_token=cancel_token)

    def probe_json(self, args: Sequence[str], timeout: float = 60.0) -> tuple[Optional[dict], str]:
        """Run FFprobe requesting JSON output; returns ``(data, error_text)``."""
        import json

        result = self.probe(["-v", "error", "-print_format", "json", *[str(a) for a in args]], timeout=timeout)
        if not result.ok:
            return None, result.describe_failure()
        try:
            return json.loads(result.stdout or "{}"), ""
        except ValueError as exc:
            return None, f"FFprobe returned output that could not be read: {exc}"


def encoding_capabilities(tools: FFmpegTools, timeout: float = 20.0) -> list[str]:
    """Return the list of available encoders (used to pick a hardware encoder).

    Only called from the Advanced/Diagnostics UI or during an explicit check -
    never during startup, because it costs a subprocess call.
    """
    result = tools.run(["-hide_banner", "-encoders"], timeout=timeout)
    if not result.ok:
        return []
    encoders: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V") and parts[1].isidentifier():
            encoders.append(parts[1])
    return encoders


def format_command(argv: Iterable[str]) -> str:
    """Render a command for logs and the UI (quoted, safe to copy)."""
    parts = []
    for item in argv:
        text = str(item)
        if any(ch.isspace() for ch in text) or '"' in text:
            parts.append('"{}"'.format(text.replace('"', '\\"')))
        else:
            parts.append(text)
    return " ".join(parts)
