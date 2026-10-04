"""Application directory system and path resolution.

Design rules (directive sections 10, 11, 69):

* Everything is :class:`pathlib.Path` based - no string concatenation of paths.
* Nothing depends on the current working directory.  The application resolves
  every path from either (a) an explicit override, (b) the application source
  root, or (c) the user data root.  Launching from a shortcut, from ``Program
  Files`` or from an unrelated working directory therefore behaves identically.
* Windows first: no ``/content``, ``/tmp``, Colab or Jupyter assumptions.
* The module has **no import-time side effects**.  Directories are only created
  when :meth:`AppPaths.ensure` is called explicitly at startup.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional

from .version import APP_ID

# --------------------------------------------------------------------------
# Layout definition
# --------------------------------------------------------------------------

#: Sub-directories of the data root, in creation order.
#: ``App/ <these> /`` - see directive section 11 "Application directory system".
DATA_SUBDIRECTORIES: tuple[str, ...] = (
    "config",
    "projects",
    "assets",
    "templates",
    "themes",
    "models",
    "workspace",
    "cache",
    "previews",
    "output",
    "logs",
    "backups",
    "temp",
    "tools",
)

#: Directories that must never be cleared by cache/temp cleanup, and must
#: never be deleted by the application.
PROTECTED_DIRECTORIES: tuple[str, ...] = (
    "config",
    "projects",
    "assets",
    "templates",
    "themes",
    "models",
    "output",
)

#: Directories that are safe to clear (they only ever hold regenerable data).
CLEARABLE_DIRECTORIES: tuple[str, ...] = (
    "cache",
    "previews",
    "temp",
    "workspace",
)

#: Environment variable that overrides the data root (useful for portable
#: installs, network drives and for tests).
DATA_ROOT_ENV_VAR = "MGS_DATA_ROOT"

#: File (inside the per-user fallback location) that remembers a custom data
#: root chosen by the user at first run.
DATA_ROOT_POINTER_FILENAME = "data_root.txt"


class PathResolutionError(Exception):
    """Raised when a usable data root cannot be resolved."""


# --------------------------------------------------------------------------
# Helper functions (pure, unit-testable)
# --------------------------------------------------------------------------

def is_within(child: Path, parent: Path) -> bool:
    """Return ``True`` when *child* is inside *parent* (or equals it).

    Both paths are resolved first so that ``..`` segments and symlinks cannot
    escape the parent - this is the guard used by the cleanup helpers.
    """
    try:
        child_r = Path(child).resolve()
        parent_r = Path(parent).resolve()
    except OSError:  # pragma: no cover - exotic filesystems
        return False
    return child_r == parent_r or parent_r in child_r.parents


def directory_is_writable(directory: Path) -> bool:
    """Return ``True`` when *directory* can actually be written to.

    ``os.access`` lies on Windows for some protected locations, so this
    performs a real probe write (create + fsync + delete) and cleans up after
    itself.  Never raises.
    """
    directory = Path(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    probe: Optional[Path] = None
    try:
        fd, raw_name = tempfile.mkstemp(prefix=".mgs_write_test_", dir=str(directory))
        probe = Path(raw_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(b"mgs")
            handle.flush()
            os.fsync(handle.fileno())
        return True
    except OSError:
        return False
    finally:
        if probe is not None:
            try:
                probe.unlink()
            except OSError:
                pass


def default_user_data_root(
    env: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
    platform_name: Optional[str] = None,
) -> Path:
    """Return the per-user fallback data root for the current platform.

    * Windows: ``%LOCALAPPDATA%\\MotionGraphicsStudio``
    * macOS:   ``~/Library/Application Support/MotionGraphicsStudio``
    * Linux:   ``~/.local/share/motion-graphics-studio``
    """
    env = os.environ if env is None else env
    platform_name = os.name if platform_name is None else platform_name
    home = Path.home() if home is None else Path(home)

    if platform_name == "nt":
        base = env.get("LOCALAPPDATA") or env.get("APPDATA")
        if base:
            return Path(base) / "MotionGraphicsStudio"
        return home / "AppData" / "Local" / "MotionGraphicsStudio"

    if platform_name == "darwin":
        return home / "Library" / "Application Support" / "MotionGraphicsStudio"

    # Linux / other POSIX (development environments).
    xdg = env.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / APP_ID
    return home / ".local" / "share" / APP_ID


def pointer_file_path(
    env: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
    platform_name: Optional[str] = None,
) -> Path:
    """Return the location of the 'remember my data root' pointer file."""
    return default_user_data_root(env=env, home=home, platform_name=platform_name) / DATA_ROOT_POINTER_FILENAME


def read_data_root_pointer(
    env: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
    platform_name: Optional[str] = None,
) -> Optional[Path]:
    """Read the remembered data root, or ``None`` when absent/invalid."""
    pointer = pointer_file_path(env=env, home=home, platform_name=platform_name)
    try:
        raw = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        return None
    return candidate


def write_data_root_pointer(
    data_root: Path,
    env: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
    platform_name: Optional[str] = None,
) -> Optional[Path]:
    """Remember *data_root* for the next launch.  Returns the pointer path.

    Never raises: a failure to remember the location is not fatal because the
    caller already has a working data root.
    """
    pointer = pointer_file_path(env=env, home=home, platform_name=platform_name)
    try:
        pointer.parent.mkdir(parents=True, exist_ok=True)
        tmp = pointer.with_name(pointer.name + ".tmp")
        tmp.write_text(str(Path(data_root).resolve()), encoding="utf-8")
        os.replace(tmp, pointer)
        return pointer
    except OSError:
        return None


def resolve_data_root(
    source_root: Path,
    cli_override: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
    platform_name: Optional[str] = None,
    prefer_portable: bool = True,
) -> tuple[Path, str]:
    """Resolve the directory that holds ``config/ projects/ assets/ ...``.

    Resolution order (first usable candidate wins):

    1. explicit ``--data-root`` command line override
    2. ``MGS_DATA_ROOT`` environment variable
    3. remembered data root (pointer file written by a previous session)
    4. the application directory itself, if it is writable (portable install)
    5. the per-user location (``%LOCALAPPDATA%`` on Windows)

    Returns a ``(path, reason)`` tuple; *reason* is a short machine friendly
    tag that is written to the log so support can see why a location was used.
    """
    env = os.environ if env is None else env
    source_root = Path(source_root).resolve()

    if cli_override is not None:
        candidate = Path(cli_override).expanduser()
        if not candidate.is_absolute():
            candidate = (source_root / candidate).resolve()
        return candidate, "command-line override"

    env_value = env.get(DATA_ROOT_ENV_VAR)
    if env_value:
        candidate = Path(env_value).expanduser()
        if not candidate.is_absolute():
            candidate = (source_root / candidate).resolve()
        return candidate, f"environment variable {DATA_ROOT_ENV_VAR}"

    remembered = read_data_root_pointer(env=env, home=home, platform_name=platform_name)
    if remembered is not None:
        return remembered, "remembered from previous session"

    if prefer_portable and source_root.is_dir() and directory_is_writable(source_root):
        return source_root, "portable install (application directory is writable)"

    return (
        default_user_data_root(env=env, home=home, platform_name=platform_name),
        "per-user application data directory",
    )


# --------------------------------------------------------------------------
# AppPaths
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class DirectoryStatus:
    """Result of probing one directory."""

    name: str
    path: Path
    exists: bool
    writable: bool


class AppPaths:
    """Resolved locations used by every subsystem of the application.

    ``AppPaths`` is the single source of truth for *where things live*.  GUI,
    CLI, renderer, tests and future workers all receive an ``AppPaths``
    instance instead of computing paths themselves.
    """

    def __init__(self, data_root: Path, source_root: Path, reason: str = "unspecified") -> None:
        self.data_root = Path(data_root).resolve()
        self.source_root = Path(source_root).resolve()
        self.reason = reason

    # -- construction ------------------------------------------------------

    @classmethod
    def bootstrap(
        cls,
        source_root: Optional[Path] = None,
        cli_override: Optional[Path] = None,
        env: Optional[Mapping[str, str]] = None,
        home: Optional[Path] = None,
        platform_name: Optional[str] = None,
        prefer_portable: bool = True,
    ) -> "AppPaths":
        """Resolve paths for a real application launch."""
        source_root = default_source_root() if source_root is None else Path(source_root)
        data_root, reason = resolve_data_root(
            source_root=source_root,
            cli_override=cli_override,
            env=env,
            home=home,
            platform_name=platform_name,
            prefer_portable=prefer_portable,
        )
        return cls(data_root=data_root, source_root=source_root, reason=reason)

    # -- directories -------------------------------------------------------

    def _sub(self, name: str) -> Path:
        return self.data_root / name

    @property
    def config_dir(self) -> Path:
        return self._sub("config")

    @property
    def projects_dir(self) -> Path:
        return self._sub("projects")

    @property
    def assets_dir(self) -> Path:
        return self._sub("assets")

    @property
    def templates_dir(self) -> Path:
        return self._sub("templates")

    @property
    def themes_dir(self) -> Path:
        return self._sub("themes")

    @property
    def models_dir(self) -> Path:
        return self._sub("models")

    @property
    def workspace_dir(self) -> Path:
        return self._sub("workspace")

    @property
    def cache_dir(self) -> Path:
        return self._sub("cache")

    @property
    def previews_dir(self) -> Path:
        return self._sub("previews")

    @property
    def output_dir(self) -> Path:
        return self._sub("output")

    @property
    def logs_dir(self) -> Path:
        return self._sub("logs")

    @property
    def backups_dir(self) -> Path:
        return self._sub("backups")

    @property
    def temp_dir(self) -> Path:
        return self._sub("temp")

    @property
    def tools_dir(self) -> Path:
        return self._sub("tools")

    @property
    def kokoro_model_dir(self) -> Path:
        """Default location for locally installed Kokoro model files."""
        return self.models_dir / "kokoro"

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "settings.json"

    @property
    def settings_backup_dir(self) -> Path:
        return self.backups_dir / "settings"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "studio.log"

    @property
    def session_file(self) -> Path:
        return self.config_dir / "session.json"

    @property
    def resources_dir(self) -> Path:
        """Bundled, read-only resources shipped with the source tree."""
        return self.source_root / "app" / "resources"

    @property
    def docs_dir(self) -> Path:
        return self.source_root / "docs"

    def named_directories(self) -> dict[str, Path]:
        """All layout directories keyed by name (used by the system check)."""
        return {name: self._sub(name) for name in DATA_SUBDIRECTORIES}

    # -- lifecycle ---------------------------------------------------------

    def ensure(self) -> list[Path]:
        """Create every layout directory.  Returns the directories that were
        newly created (useful for logging).  Raises :class:`PathResolutionError`
        with a friendly message when the data root cannot be created at all."""
        created: list[Path] = []
        try:
            self.data_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PathResolutionError(
                f"Could not create the application data folder:\n{self.data_root}\n\n{exc}"
            ) from exc
        for name in DATA_SUBDIRECTORIES:
            directory = self._sub(name)
            if not directory.exists():
                try:
                    directory.mkdir(parents=True, exist_ok=True)
                    created.append(directory)
                except OSError as exc:
                    raise PathResolutionError(
                        f"Could not create the '{name}' folder:\n{directory}\n\n{exc}"
                    ) from exc
        return created

    def directory_statuses(self) -> list[DirectoryStatus]:
        """Probe existence + writability of every layout directory."""
        statuses: list[DirectoryStatus] = []
        for name in DATA_SUBDIRECTORIES:
            directory = self._sub(name)
            statuses.append(
                DirectoryStatus(
                    name=name,
                    path=directory,
                    exists=directory.is_dir(),
                    writable=directory_is_writable(directory) if directory.is_dir() else False,
                )
            )
        return statuses

    # -- project helpers (contract used by Stage B and later) --------------

    def project_dir(self, project_id: str) -> Path:
        """Return the folder for a project id, guarding against traversal.

        ``project_id`` is sanitised: only letters, digits, ``-`` and ``_`` are
        allowed so a malformed manifest can never point outside ``projects/``.
        """
        safe = "".join(ch for ch in str(project_id) if ch.isalnum() or ch in "-_")
        if not safe:
            raise ValueError("A project id may only contain letters, digits, '-' and '_'.")
        return self.projects_dir / safe

    def is_inside_data_root(self, path: Path) -> bool:
        return is_within(path, self.data_root)

    # -- diagnostics -------------------------------------------------------

    def describe(self) -> str:
        lines = [
            f"Application source : {self.source_root}",
            f"Data root          : {self.data_root}",
            f"Data root chosen by: {self.reason}",
            f"Settings file      : {self.settings_file}",
            f"Log file           : {self.log_file}",
            f"Output folder      : {self.output_dir}",
        ]
        return "\n".join(lines)

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "source_root": str(self.source_root),
            "data_root": str(self.data_root),
            "data_root_reason": self.reason,
        }
        for name in DATA_SUBDIRECTORIES:
            data[f"{name}_dir"] = str(self._sub(name))
        return data

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AppPaths(data_root={self.data_root!r}, source_root={self.source_root!r})"


def default_source_root() -> Path:
    """Return the application source root (the folder containing ``app/``).

    Resolved from ``__file__`` - never from the working directory - so the app
    behaves the same when launched from a shortcut.
    """
    return Path(__file__).resolve().parents[2]


def safe_filename(name: str, fallback: str = "untitled", max_length: int = 120) -> str:
    """Turn arbitrary user text into a file-name-safe string.

    Used for project folder names and output file names.  Windows reserved
    device names are escaped so ``CON``/``NUL`` style names cannot be created.
    """
    invalid = '<>:"/\\|?*'
    cleaned = "".join(ch if ch not in invalid and ord(ch) >= 32 else "_" for ch in str(name))
    cleaned = cleaned.strip(" .")
    cleaned = "_".join(part for part in cleaned.split("_") if part) or ""
    cleaned = cleaned[:max_length].strip(" .")
    if not cleaned:
        cleaned = fallback
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if cleaned.upper() in reserved:
        cleaned = f"{cleaned}_file"
    return cleaned


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    """Return ``stem<suffix>``, ``stem2<suffix>``... inside *directory*.

    Never overwrites an existing file (directive section 36: ``Video1.mp4``,
    ``Video2.mp4`` instead of silently replacing a finished video).
    """
    directory = Path(directory)
    suffix = suffix if suffix.startswith(".") or not suffix else f".{suffix}"
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    index = 2
    while True:
        candidate = directory / f"{stem}{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def iter_existing(paths: Iterable[Path]) -> list[Path]:
    """Return only the paths that currently exist (order preserved)."""
    return [Path(p) for p in paths if Path(p).exists()]
