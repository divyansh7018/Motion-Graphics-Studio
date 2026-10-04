"""Project locking that survives a crash (directive section 31).

A lock file records *who* has the project open.  The rules that keep it safe:

* A lock is only honoured when its owner can be shown to be alive **on this
  machine**.  A lock left behind by a crashed process is detected as stale and
  removed, with a log entry - it can never permanently block a project.
* A lock from a different computer (a project on a network share) is never
  removed automatically; the user is told and decides.
* Releasing only ever removes a lock this process owns.
* Locking is advisory and best effort: if the lock file cannot be written the
  project still opens, with a warning.  Losing the ability to open a project
  would be worse than the small risk it protects against.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..core.atomicio import atomic_write_json
from ..core.events import Event
from ..core.logging_setup import get_logger, log_event
from ..core.version import APP_VERSION
from .layout import ProjectLayout

LOGGER = get_logger("project.lock")

STILL_ACTIVE = 259


def process_is_alive(pid: int) -> bool:
    """Is *pid* a running process **on this machine**?

    Windows needs the Win32 API here: ``os.kill(pid, 0)`` would terminate the
    process instead of probing it.
    """
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - an unprobeable pid is treated as dead
            return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, it is just not ours
    except OSError:
        return False
    return True


@dataclass
class LockInfo:
    """Who is holding a project open."""

    pid: int = 0
    host: str = ""
    started_at: str = ""
    app_version: str = ""
    data_root: str = ""

    @property
    def is_local(self) -> bool:
        return self.host == socket.gethostname()

    def describe(self) -> str:
        where = "this computer" if self.is_local else f"another computer ({self.host or 'unknown'})"
        return f"process {self.pid} on {where}" + (f", started {self.started_at}" if self.started_at else "")

    def to_dict(self) -> dict:
        return {
            "pid": int(self.pid),
            "host": self.host,
            "started_at": self.started_at,
            "app_version": self.app_version,
            "data_root": self.data_root,
        }

    @classmethod
    def from_dict(cls, data: object) -> Optional["LockInfo"]:
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                pid=int(data.get("pid") or 0),
                host=str(data.get("host") or ""),
                started_at=str(data.get("started_at") or ""),
                app_version=str(data.get("app_version") or ""),
                data_root=str(data.get("data_root") or ""),
            )
        except (TypeError, ValueError):
            return None


@dataclass
class LockResult:
    ok: bool
    reason: str = ""
    owner: Optional[LockInfo] = None
    stale_removed: bool = False
    #: True when the lock could not be written but opening is still allowed.
    advisory_only: bool = False


class ProjectLock:
    """One project's advisory lock."""

    def __init__(self, layout: ProjectLayout, data_root: str = "") -> None:
        self.layout = layout
        self.data_root = data_root
        self.owned = False

    # -- state -------------------------------------------------------------

    def owner(self) -> Optional[LockInfo]:
        path = self.layout.lock_file
        if not path.exists():
            return None
        try:
            return LockInfo.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            # An unreadable lock is stale by definition: nobody can be identified.
            return LockInfo(pid=0, host="", started_at="", app_version="", data_root="")

    # -- acquire / release -------------------------------------------------

    def acquire(self, force: bool = False) -> LockResult:
        existing = self.owner()
        if existing is not None:
            if existing.is_local and process_is_alive(existing.pid):
                return LockResult(
                    ok=False,
                    reason=(
                        f"This project is already open in {existing.describe()}. "
                        "Close that window first."
                    ),
                    owner=existing,
                )
            if not existing.is_local and existing.pid and not force:
                return LockResult(
                    ok=False,
                    reason=(
                        f"This project is marked as open on {existing.host or 'another computer'}. "
                        "Close it there, or open it read-only."
                    ),
                    owner=existing,
                )
            # Stale: the owner is gone.
            self._remove_lock_file()
            log_event(
                Event.PROJECT_STALE_LOCK_REMOVED,
                "A stale project lock was removed",
                logger=LOGGER,
                path=str(self.layout.lock_file),
                previous_owner=existing.describe(),
            )
            stale = True
        else:
            stale = False

        info = LockInfo(
            pid=os.getpid(),
            host=socket.gethostname(),
            started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            app_version=APP_VERSION,
            data_root=self.data_root,
        )
        try:
            self.layout.root.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.layout.lock_file, info.to_dict())
        except OSError as exc:
            log_event(
                Event.WARNING,
                "The project lock could not be written",
                level=logging.WARNING,
                logger=LOGGER,
                path=str(self.layout.lock_file),
                reason=str(exc),
            )
            return LockResult(ok=True, reason=f"The lock file could not be written: {exc}", advisory_only=True, stale_removed=stale)

        self.owned = True
        log_event(
            Event.PROJECT_LOCKED,
            "Project locked for this session",
            logger=LOGGER,
            path=str(self.layout.lock_file),
            pid=info.pid,
            stale_removed=stale,
        )
        return LockResult(ok=True, stale_removed=stale)

    def release(self) -> bool:
        """Remove the lock if this process owns it."""
        if not self.layout.lock_file.exists():
            self.owned = False
            return True
        info = self.owner()
        if info is not None and info.pid and info.pid != os.getpid():
            return False  # never remove somebody else's lock
        removed = self._remove_lock_file()
        if removed:
            log_event(Event.PROJECT_UNLOCKED, "Project lock released", logger=LOGGER, path=str(self.layout.lock_file))
        self.owned = False
        return removed

    def _remove_lock_file(self) -> bool:
        try:
            Path(self.layout.lock_file).unlink()
            return True
        except OSError:
            return False

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> "ProjectLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


__all__ = ["LockInfo", "LockResult", "ProjectLock", "process_is_alive"]
