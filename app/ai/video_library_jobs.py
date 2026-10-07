"""Video Library jobs (directive sections 14, 15, 29, 41, 42).

Scanning a folder, extracting a frame, re‑measuring a clip and importing a
render all read the disk, so none of them runs on the GUI thread.  Each one is a
plain function with the :class:`~app.jobs.spec.JobContext` signature, which is
what lets the window, the CLI and the tests run exactly the same code.

One user action is one job (section 9): a scan is a scan, a thumbnail batch is a
thumbnail batch, and nothing here starts work the user did not ask for.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..core.logging_setup import get_logger
from ..jobs.keys import JobKeys
from ..jobs.spec import JobContext, JobSpec

LOGGER = get_logger("ai.video_library_jobs")

__all__ = [
    "library_for", "library_scan_body", "library_scan_spec",
    "library_import_body", "library_import_spec",
    "library_thumbnails_body", "library_thumbnails_spec",
    "library_remove_body", "library_remove_spec",
    "library_recheck_body", "library_recheck_spec",
]


def library_for(context: JobContext) -> Any:
    """The library this job works on: the one it was given, or a fresh one."""
    library = context.get("library")
    if library is not None:
        return library
    from .video_library import VideoLibrary

    paths = context.paths
    root = Path(str(getattr(paths, "videos_dir",
                            Path(str(getattr(paths, "data_root", ""))) / "videos")))
    tools = context.get("tools")
    return VideoLibrary(root, tools=tools)


def _scan_progress(context: JobContext) -> Any:
    reporter = context.progress

    def report(done: int, total: int, name: str) -> None:
        if getattr(reporter.progress, "total", 0) != float(max(1, total)):
            reporter.start(total=float(max(1, total)), message="Scanning clips",
                           unit="clip")
        reporter.update(current=float(done), message=f"{name} ({done}/{total})")

    return report


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

def library_scan_body(context: JobContext) -> dict:
    """Index the folder (or one sub-folder) and measure what is new."""
    context.raise_if_cancelled()
    library = library_for(context)
    folder = str(context.get("folder") or "")
    measure = bool(context.get("measure", True))
    before = library.counts().get("total", 0)
    found = library.scan(folder=folder or None, measure=measure,
                         progress=_scan_progress(context), cancel=context.cancel)
    added = max(0, library.counts().get("total", 0) - before)
    context.raise_if_cancelled()
    return {
        "ok": True, "found": int(found), "added": int(added),
        "measured": bool(measure), "folder": folder or str(library.root),
        "counts": library.counts(),
        "message": (f"{found} clip(s) in the library"
                    + (f", {added} new." if added else ".")),
        "videos": [entry.to_dict() for entry in library.all()[:50]],
    }


def library_scan_spec(payload: dict, **options: Any) -> JobSpec:
    return JobSpec(
        key=JobKeys.VIDEO_LIBRARY_SCAN, title="Scan the video library",
        body=library_scan_body,
        description="Reading the clip folder and measuring what is new.",
        cancellable=True, allow_parallel=False,
        payload=dict(payload, **options))


# ---------------------------------------------------------------------------
# import one clip (a render, or a file the user picked)
# ---------------------------------------------------------------------------

def library_import_body(context: JobContext) -> dict:
    """Add one file to the library, with what is known about it."""
    context.raise_if_cancelled()
    library = library_for(context)
    source = str(context.get("path") or "")
    entry = library.add(source, source=str(context.get("source") or ""),
                        name=str(context.get("name") or ""),
                        metadata=dict(context.get("metadata") or {}),
                        measure=True)
    if entry is None:
        return {"ok": False, "message":
                f"{Path(source).name or 'That file'} is not there, so nothing "
                f"was added.", "what_to_do":
                "Pick the file again - it may have been moved or renamed.",
                "options": ["choose_file", "cancel"]}
    return {"ok": True, "id": entry.id, "path": entry.path,
            "entry": entry.to_dict(), "message":
            f"{entry.filename} was added to the video library.",
            "options": ["open", "send_to_project", "cancel"]}


def library_import_spec(payload: dict, **options: Any) -> JobSpec:
    return JobSpec(
        key=JobKeys.VIDEO_LIBRARY_IMPORT, title="Add a clip to the library",
        body=library_import_body,
        description="Indexing one file and measuring it.",
        cancellable=True, allow_parallel=False,
        payload=dict(payload, **options))


# ---------------------------------------------------------------------------
# thumbnails
# ---------------------------------------------------------------------------

def library_thumbnails_body(context: JobContext) -> dict:
    """Make the missing thumbnails, one frame at a time, cancellably."""
    context.raise_if_cancelled()
    library = library_for(context)
    requested = list(context.get("ids") or [])
    if requested:
        entries = [entry for entry in (library.find(str(item))
                                       for item in requested) if entry is not None]
    else:
        entries = [entry for entry in library.all()
                   if not entry.thumbnail or
                   not Path(str(entry.thumbnail)).is_file()]
    limit = int(context.get("limit") or 0)
    if limit > 0:
        entries = entries[:limit]
    reporter = context.progress
    if getattr(reporter.progress, "total", 0) != float(max(1, len(entries))):
        reporter.start(total=float(max(1, len(entries))),
                       message="Making clip pictures", unit="clip")

    made: list[dict] = []
    failed: list[dict] = []
    for index, entry in enumerate(entries):
        if context.is_cancelled():
            break
        target = library.thumbnail_for(entry.id, cancel=context.cancel)
        (made if target else failed).append(
            {"id": entry.id, "name": entry.filename,
             "path": str(target or ""), "reason": "" if target else
             "A picture could not be taken from this clip."})
        reporter.update(current=float(index + 1),
                        message=f"{len(made)} of {len(entries)} picture(s)")
    cancelled = context.is_cancelled()
    message = (f"{len(made)} clip picture(s) made."
               + (f" {len(failed)} could not be read." if failed else "")
               + (" Cancelled; the pictures already made were kept."
                  if cancelled else ""))
    return {"ok": not failed or bool(made), "cancelled": bool(cancelled),
            "made": len(made), "failed": failed, "items": made,
            "message": message,
            "options": ["retry", "cancel"] if failed else ["cancel"]}


def library_thumbnails_spec(payload: dict, **options: Any) -> JobSpec:
    return JobSpec(
        key=JobKeys.VIDEO_LIBRARY_THUMBNAILS,
        title="Make clip pictures", body=library_thumbnails_body,
        description="Taking one frame from each clip that needs a picture.",
        cancellable=True, allow_parallel=False,
        payload=dict(payload, **options))


# ---------------------------------------------------------------------------
# remove / re-measure
# ---------------------------------------------------------------------------

def library_remove_body(context: JobContext) -> dict:
    """Forget clips - deleting the files only when that was asked for."""
    context.raise_if_cancelled()
    library = library_for(context)
    ids = [str(item) for item in (context.get("ids") or [])]
    delete_file = bool(context.get("delete_file", False))
    removed: list[str] = []
    for entry_id in ids:
        entry = library.find(entry_id)
        if entry is None:
            continue
        if library.remove(entry_id, delete_file=delete_file):
            removed.append(str(getattr(entry, "filename", entry_id)))
    return {"ok": True, "removed": removed, "deleted_files": delete_file,
            "message": (f"{len(removed)} clip(s) removed from the library"
                        + (" and deleted from disk." if delete_file else
                           "; the files were left where they are.")),
            "options": ["cancel"]}


def library_remove_spec(payload: dict, **options: Any) -> JobSpec:
    return JobSpec(
        key=JobKeys.VIDEO_LIBRARY_REMOVE, title="Remove clip(s) from the library",
        body=library_remove_body,
        description="Updating the library index.",
        cancellable=True, allow_parallel=False,
        payload=dict(payload, **options))


def library_recheck_body(context: JobContext) -> dict:
    """Measure a clip again, because its file may have been replaced."""
    context.raise_if_cancelled()
    library = library_for(context)
    entry_id = str(context.get("id") or "")
    entry = library.recheck(entry_id)
    if entry is None:
        return {"ok": False, "message": "That clip is no longer in the library.",
                "what_to_do": "Scan the folder again.", "options": ["scan"]}
    return {"ok": entry.status == "READY", "entry": entry.to_dict(),
            "message": (f"{entry.filename} was measured again: "
                        f"{entry.measurements() or 'no usable numbers'}."),
            "what_to_do": "" if entry.status == "READY" else
            "The file could not be read. Check the file, or remove it from the "
            "library.",
            "options": ["open", "cancel"]}


def library_recheck_spec(payload: dict, **options: Any) -> JobSpec:
    return JobSpec(
        key=JobKeys.VIDEO_LIBRARY_RECHECK, title="Measure the clip again",
        body=library_recheck_body,
        description="Reading this clip's real length, size and frame rate.",
        cancellable=True, allow_parallel=False,
        payload=dict(payload, **options))


def video_library_service(context: Any = None, **kwargs: Any) -> Any:
    """Build a library from a GUI context, a paths object, or nothing at all."""
    from .video_library import VideoLibrary

    if isinstance(context, VideoLibrary):
        return context
    paths = getattr(context, "paths", None) or kwargs.pop("paths", None)
    tools = kwargs.pop("tools", None) or getattr(context, "tools", None)
    root = kwargs.pop("root", None)
    if root is None and paths is not None:
        root = Path(str(getattr(paths, "videos_dir",
                                Path(str(getattr(paths, "data_root", ""))) /
                                "videos")))
    library = VideoLibrary(root, tools=tools)
    thumbs = getattr(paths, "video_thumbnails_dir", None) if paths else None
    if thumbs:
        library.thumbnails.cache_dir = Path(thumbs)
    return library


def find_library(*candidates: Any) -> Optional[Any]:
    """The first candidate that really is a library (used by the GUI and CLI)."""
    for candidate in candidates:
        if candidate is not None and hasattr(candidate, "query") and \
                hasattr(candidate, "add"):
            return candidate
    return None
