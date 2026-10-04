"""Where a project's files live.

One folder per project, with a fixed set of sub-folders (directive section 7).
Everything the application writes for a project goes into one of them, so there
are never loose temporary files scattered in the project folder.  Regenerable
data that does not belong to the project (thumbnails, preview caches) stays in
the application's own cache folders from Stage A.

``assets/``, ``audio/``, ``generated/`` and ``scenes/`` are referenced with
**relative** paths so a project folder can be copied to another PC
(sections 22 and 23).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..core.paths import is_within

#: Sub-folders created inside every project.
PROJECT_SUBDIRECTORIES: tuple[str, ...] = (
    "scenes",
    "assets",
    "audio",
    "generated",
    "previews",
    "renders",
    "backups",
    "autosave",
)

#: Folders whose contents can be rebuilt and are therefore safe to clean.
REGENERABLE_SUBDIRECTORIES: tuple[str, ...] = ("previews", "generated")

PROJECT_FILENAME = "project.json"
SCRIPT_FILENAME = "script.txt"
LOCK_FILENAME = ".project.lock.json"
AUTOSAVE_FILENAME = "project.autosave.json"
IGNORED_SUFFIX = ".ignored.json"


@dataclass(frozen=True)
class ProjectLayout:
    """Every path belonging to one project."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))

    # -- well known files --------------------------------------------------

    @property
    def project_file(self) -> Path:
        return self.root / PROJECT_FILENAME

    @property
    def script_file(self) -> Path:
        return self.root / SCRIPT_FILENAME

    @property
    def lock_file(self) -> Path:
        return self.root / LOCK_FILENAME

    # -- folders -----------------------------------------------------------

    @property
    def scenes_dir(self) -> Path:
        return self.root / "scenes"

    @property
    def assets_dir(self) -> Path:
        return self.root / "assets"

    @property
    def audio_dir(self) -> Path:
        return self.root / "audio"

    @property
    def generated_dir(self) -> Path:
        return self.root / "generated"

    @property
    def previews_dir(self) -> Path:
        return self.root / "previews"

    @property
    def renders_dir(self) -> Path:
        return self.root / "renders"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    @property
    def autosave_dir(self) -> Path:
        return self.root / "autosave"

    @property
    def autosave_file(self) -> Path:
        return self.autosave_dir / AUTOSAVE_FILENAME

    def named_directories(self) -> dict[str, Path]:
        return {name: self.root / name for name in PROJECT_SUBDIRECTORIES}

    # -- creation ----------------------------------------------------------

    def exists(self) -> bool:
        return self.project_file.exists()

    def ensure(self) -> list[Path]:
        """Create the project folder tree.  Returns the folders created."""
        created: list[Path] = []
        if not self.root.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            created.append(self.root)
        for name in PROJECT_SUBDIRECTORIES:
            directory = self.root / name
            if not directory.exists():
                directory.mkdir(parents=True, exist_ok=True)
                created.append(directory)
        return created

    # -- path safety -------------------------------------------------------

    def is_inside(self, path: Path) -> bool:
        return is_within(Path(path), self.root)

    def resolve(self, relative: str | Path) -> Path:
        """Resolve a project-relative path, refusing anything that escapes.

        Raises :class:`ValueError` for ``..`` climbs or absolute paths, so a
        hand-edited ``project.json`` can never make the application read or
        write outside the project folder.
        """
        text = str(relative or "").strip()
        if not text:
            raise ValueError("The path is empty.")
        candidate = Path(text)
        if candidate.is_absolute() or (len(text) > 1 and text[1] == ":"):
            raise ValueError(f"'{text}' is an absolute path; project paths must be relative.")
        target = (self.root / candidate).resolve()
        if not is_within(target, self.root.resolve()):
            raise ValueError(f"'{text}' points outside the project folder.")
        return target

    def relative(self, path: Path) -> str:
        """Project-relative form of *path* with forward slashes (portable)."""
        path = Path(path)
        try:
            return path.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    def is_project_file(self, path: Path) -> bool:
        return Path(path).name == PROJECT_FILENAME

    # -- discovery ---------------------------------------------------------

    @classmethod
    def from_project_file(cls, path: Path) -> "ProjectLayout":
        """Layout for the project that owns ``.../project.json``."""
        return cls(root=Path(path).resolve().parent)

    def subdirectory_files(self, name: str) -> list[Path]:
        directory = self.root / name
        if not directory.is_dir():
            return []
        try:
            return sorted(item for item in directory.iterdir() if item.is_file())
        except OSError:
            return []

    def autosave_files(self) -> list[Path]:
        """Newest first: the live autosave plus its bounded history."""
        files = self.subdirectory_files("autosave")
        live = [item for item in files if item.name == AUTOSAVE_FILENAME]
        history = [item for item in files if item.name != AUTOSAVE_FILENAME and not item.name.endswith(IGNORED_SUFFIX)]
        by_time = sorted(live + history, key=lambda item: _mtime(item), reverse=True)
        return by_time

    def backup_files(self) -> list[Path]:
        """Newest first."""
        return sorted(self.subdirectory_files("backups"), key=lambda item: _mtime(item), reverse=True)

    def describe(self) -> str:
        lines = [f"Project folder: {self.root}"]
        for name, directory in self.named_directories().items():
            mark = "ok " if directory.is_dir() else "-- "
            lines.append(f"  [{mark}] {name:<10} {directory}")
        lines.append(f"  [{'ok ' if self.project_file.exists() else '-- '}] project.json {self.project_file}")
        return "\n".join(lines)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def looks_like_project_folder(path: Path) -> bool:
    """True when *path* is a folder that holds a ``project.json``."""
    path = Path(path)
    if path.is_file():
        return path.name == PROJECT_FILENAME
    return (path / PROJECT_FILENAME).is_file()


def find_project_file(path: Path) -> Optional[Path]:
    """Accept either a project folder or the ``project.json`` itself."""
    path = Path(path)
    if path.is_dir():
        candidate = path / PROJECT_FILENAME
        return candidate if candidate.exists() else None
    if path.exists() and path.suffix == ".json":
        return path
    return None


__all__ = [
    "AUTOSAVE_FILENAME",
    "IGNORED_SUFFIX",
    "LOCK_FILENAME",
    "PROJECT_FILENAME",
    "PROJECT_SUBDIRECTORIES",
    "REGENERABLE_SUBDIRECTORIES",
    "SCRIPT_FILENAME",
    "ProjectLayout",
    "find_project_file",
    "looks_like_project_folder",
]
