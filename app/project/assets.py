"""Project assets: import, verify, relink (directive sections 21, 22, 23).

A project owns its media by **copying it into the project folder** and storing a
relative path, which is what makes the folder portable.  Referencing a file in
place is allowed but is stored as an explicit absolute path and flagged, because
it will not survive a move to another PC.

Nothing here blocks on huge files: metadata is read from headers, and a missing
file is reported - never a crash.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..core.atomicio import human_size
from ..core.errors import ProjectError
from ..core.events import Event
from ..core.logging_setup import get_logger, log_event
from ..core.paths import safe_filename, unique_path
from .layout import ProjectLayout
from .model import AssetSpec, Project, new_id, utc_now_iso

LOGGER = get_logger("project.assets")

#: Suffix -> asset kind.  Anything else becomes "other" and is still usable.
KIND_BY_SUFFIX: dict[str, str] = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
    ".bmp": "image", ".gif": "image", ".tif": "image", ".tiff": "image",
    ".svg": "svg",
    ".mp4": "video", ".mov": "video", ".mkv": "video", ".webm": "video", ".avi": "video",
    ".mp3": "audio", ".wav": "audio", ".flac": "audio", ".ogg": "audio", ".m4a": "audio", ".aac": "audio",
    ".ttf": "font", ".otf": "font", ".woff": "font", ".woff2": "font",
}

#: Refuse to copy files this large into a project (they belong on a media drive).
MAX_IMPORT_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB

#: Files bigger than this are checksummed on their first megabyte only.
CHECKSUM_LIMIT_BYTES = 64 * 1024 * 1024


class AssetImportError(ProjectError):
    """An asset could not be added to the project."""

    default_title = "That file could not be added"

    def __init__(self, what_happened: str, why: str = "", actions=(), technical: Optional[str] = None) -> None:
        super().__init__(what_happened, why, actions or (), technical, error_code="ASSET_IMPORT_FAILED")


@dataclass
class AssetCheck:
    """Result of checking one asset against the disk."""

    asset: AssetSpec
    exists: bool
    path: Path
    size_bytes: int = 0
    width: int = 0
    height: int = 0
    message: str = ""

    @property
    def missing(self) -> bool:
        return not self.exists

    def to_line(self) -> str:
        mark = "ok " if self.exists else "MISSING"
        detail = human_size(self.size_bytes) if self.exists else self.message
        return f"[{mark}] {self.asset.label()}: {detail}"


@dataclass
class ImportReport:
    asset: Optional[AssetSpec] = None
    copied: bool = False
    notes: list[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.asset is not None


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------

def detect_kind(path: Path) -> str:
    return KIND_BY_SUFFIX.get(Path(path).suffix.lower(), "other")


def file_checksum(path: Path, limit: int = CHECKSUM_LIMIT_BYTES) -> str:
    """SHA-256 of the file (first *limit* bytes for very large files)."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            read = 0
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                read += len(chunk)
                if read >= limit:
                    break
    except OSError:
        return ""
    return digest.hexdigest()[:32]


def image_dimensions(path: Path) -> tuple[int, int]:
    """Width/height from the file header, or (0, 0) when unknown."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a core requirement
        return 0, 0
    try:
        with Image.open(path) as image:
            return int(image.width), int(image.height)
    except Exception:  # noqa: BLE001 - a broken image is reported, not raised
        return 0, 0


def import_asset(
    project: Project,
    layout: ProjectLayout,
    source: Path,
    *,
    kind: str = "",
    copy: bool = True,
    name: str = "",
    notes: str = "",
) -> ImportReport:
    """Add a file to the project.

    ``copy=True`` (the default) copies the file into ``assets/`` and stores a
    project-relative path, so the project stays portable.  ``copy=False``
    references the file where it is and stores an absolute path, which is
    reported as non-portable by validation.
    """
    source = Path(source)
    if not source.exists():
        return ImportReport(error=f"'{source}' does not exist.")
    if not source.is_file():
        return ImportReport(error=f"'{source}' is a folder, not a file.")

    try:
        size = source.stat().st_size
    except OSError as exc:
        return ImportReport(error=f"'{source}' could not be read: {exc}")
    if size == 0:
        return ImportReport(error=f"'{source}' is empty.")

    asset_kind = kind or detect_kind(source)
    notes_list: list[str] = []

    if copy:
        if size > MAX_IMPORT_BYTES:
            raise AssetImportError(
                what_happened=f"'{source.name}' is {human_size(size)}, which is too large to copy into a project.",
                why="Copying it would duplicate a very large file inside the project folder.",
                actions=(
                    "Add it as a reference instead (untick 'Copy into project').",
                    "Or use a smaller file.",
                ),
            )
        layout.assets_dir.mkdir(parents=True, exist_ok=True)
        stem = safe_filename(name or source.stem, fallback="asset")
        target = unique_path(layout.assets_dir, stem, source.suffix.lower())
        try:
            shutil.copy2(source, target)
        except OSError as exc:
            raise AssetImportError(
                what_happened=f"'{source.name}' could not be copied into the project.",
                why=str(exc),
                actions=(
                    "Check that the project folder is writable and has enough free space.",
                    "Or add the file as a reference instead of copying it.",
                ),
                technical=str(exc),
            ) from exc
        stored_path = layout.relative(target)
        absolute = ""
    else:
        stored_path = ""
        absolute = str(source.resolve())
        notes_list.append("Referenced in place - this project will not open on another PC.")

    width, height = image_dimensions(source) if asset_kind in ("image", "svg", "logo") else (0, 0)
    asset = AssetSpec(
        id=new_id("asset"),
        name=name or source.name,
        kind=asset_kind,
        path=stored_path,
        absolute_path=absolute,
        size_bytes=int(size),
        width=width,
        height=height,
        imported_at=utc_now_iso(),
        checksum=file_checksum(source),
        notes=notes,
    )
    project.add_asset(asset)

    log_event(
        Event.USER_ACTION,
        "Asset added to the project",
        logger=LOGGER,
        asset=asset.id,
        kind=asset_kind,
        copied=copy,
        bytes=size,
    )
    return ImportReport(asset=asset, copied=copy, notes=notes_list)


# --------------------------------------------------------------------------
# Verification / relinking
# --------------------------------------------------------------------------

def verify_assets(project: Project, layout: ProjectLayout) -> list[AssetCheck]:
    """Check every asset against the disk.  Missing files are data, not errors."""
    checks: list[AssetCheck] = []
    for asset in project.assets:
        try:
            path = asset.resolve(layout.root)
        except ValueError:
            checks.append(AssetCheck(asset=asset, exists=False, path=layout.root, message="the stored path is not valid"))
            continue
        exists = path.exists()
        size = 0
        width = height = 0
        message = "the file is not there"
        if exists:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            if asset.kind in ("image", "svg", "logo"):
                width, height = image_dimensions(path)
            message = ""
        checks.append(
            AssetCheck(asset=asset, exists=exists, path=path, size_bytes=size, width=width, height=height, message=message)
        )
    return checks


def update_missing_flags(project: Project, layout: ProjectLayout) -> list[AssetSpec]:
    """Set ``missing`` on every asset whose file is gone.  Returns those assets."""
    missing: list[AssetSpec] = []
    for check in verify_assets(project, layout):
        was_missing = check.asset.missing
        check.asset.missing = check.missing
        if check.missing:
            missing.append(check.asset)
        if check.exists and check.size_bytes:
            check.asset.size_bytes = check.size_bytes
            if check.width:
                check.asset.width = check.width
                check.asset.height = check.height
        if check.asset.missing != was_missing:
            project.touch()
    return missing


def relink_asset(
    project: Project,
    layout: ProjectLayout,
    asset: AssetSpec,
    new_source: Path,
    *,
    copy: bool = True,
) -> AssetSpec:
    """Point an asset at a new file, keeping its id and every reference.

    This is the "Relink" action from the missing-asset dialog (section 21).  The
    asset id never changes, so scenes that use it keep working.
    """
    new_source = Path(new_source)
    if not new_source.is_file():
        raise AssetImportError(
            what_happened=f"'{new_source}' is not a file.",
            why="Relinking needs an existing file to point at.",
            actions=("Choose the file's new location.", "Or replace the asset with a different file."),
        )

    if copy:
        layout.assets_dir.mkdir(parents=True, exist_ok=True)
        target = unique_path(layout.assets_dir, safe_filename(new_source.stem, fallback="asset"), new_source.suffix.lower())
        try:
            shutil.copy2(new_source, target)
        except OSError as exc:
            raise AssetImportError(
                what_happened=f"'{new_source.name}' could not be copied into the project.",
                why=str(exc),
                actions=("Check the free space and the folder permissions.",),
                technical=str(exc),
            ) from exc
        asset.path = layout.relative(target)
        asset.absolute_path = ""
    else:
        asset.path = ""
        asset.absolute_path = str(new_source.resolve())

    asset.size_bytes = int(new_source.stat().st_size)
    asset.checksum = file_checksum(new_source)
    asset.missing = False
    if asset.kind in ("image", "svg", "logo"):
        asset.width, asset.height = image_dimensions(new_source)
    project.touch()

    log_event(
        Event.PROJECT_ASSET_RELINK,
        "Asset relinked",
        logger=LOGGER,
        asset=asset.id,
        path=asset.path or asset.absolute_path,
        used_by=len(project.asset_references(asset.id)),
    )
    return asset


def replace_asset(project: Project, layout: ProjectLayout, asset: AssetSpec, new_source: Path) -> AssetSpec:
    """Swap the file behind an asset, keeping the same name and id."""
    previous_name = asset.name
    relink_asset(project, layout, asset, new_source, copy=True)
    asset.name = previous_name
    return asset


def ignore_missing_asset(project: Project, asset: AssetSpec) -> AssetSpec:
    """Keep the project usable while an asset stays missing (section 21)."""
    asset.missing = True
    note = "Missing file ignored by the user."
    asset.notes = f"{asset.notes}\n{note}".strip() if asset.notes else note
    project.touch()
    log_event(Event.PROJECT_ASSET_RELINK, "A missing asset was ignored", logger=LOGGER, asset=asset.id)
    return asset


def delete_asset(project: Project, layout: ProjectLayout, asset_id: str, *, delete_file: bool = False) -> bool:
    """Remove an asset entry, and optionally its file inside the project.

    Files outside the project are never deleted - the project does not own them.
    """
    asset = project.asset_by_id(asset_id)
    if asset is None:
        return False
    if delete_file and asset.path:
        try:
            target = layout.resolve(asset.path)
            if layout.is_inside(target) and target.is_file():
                target.unlink()
        except (ValueError, OSError) as exc:
            log_event(
                Event.WARNING,
                "The asset file could not be deleted",
                level=logging.WARNING,
                logger=LOGGER,
                asset=asset_id,
                reason=str(exc),
            )
    project.remove_asset(asset_id)
    log_event(Event.USER_ACTION, "Asset removed from the project", logger=LOGGER, asset=asset_id, file_deleted=delete_file)
    return True


__all__ = [
    "AssetCheck",
    "AssetImportError",
    "ImportReport",
    "KIND_BY_SUFFIX",
    "delete_asset",
    "detect_kind",
    "file_checksum",
    "ignore_missing_asset",
    "image_dimensions",
    "import_asset",
    "relink_asset",
    "replace_asset",
    "update_missing_flags",
    "verify_assets",
]
