"""Getting a generated result into the project (sections 29, 30, 31, 47).

Three real actions, each of which does the whole job rather than half of it:

``send_to_project``
    Copies the file into the project as an asset, so it appears in the Asset
    Manager and can be used by anything.  A second clip with the same name gets
    its own file: nothing is overwritten (section 44).
``send_to_scene``
    Turns the clip into a scene that really plays it.  The scene is an ordinary
    scene: it appears in the timeline with the clip's own duration and it is
    rendered by the same renderer as everything else (sections 30, 47).
``send_to_timeline``
    The same, plus an explicit placement - at the start, at the end, or before
    or after a named scene - so "send to the timeline" means something precise
    (section 29).

The clip's metadata travels with it: backend, model, prompt, seed, duration,
generator label, parent and references are written into the scene's own
``extra`` block and into the asset's metadata, so a clip in a project can always
be traced back to how it was made (section 100).

Nothing here is specific to AI: an imported video from anywhere behaves the
same way.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..core.logging_setup import log_event
from ..project.model import ElementSpec, SceneSpec
from ..render.frames import VIDEO_SCENE_TYPE
from .models import human_bytes

__all__ = ["PLACEMENTS", "PLACEMENT_LABELS", "SendReport", "send_to_project",
           "send_to_scene", "send_to_timeline", "clip_metadata",
           "video_asset_for", "describe_send"]

#: Where a clip can be placed on the timeline.
PLACEMENTS: tuple[str, ...] = ("end", "start", "after", "before")

PLACEMENT_LABELS: dict[str, str] = {
    "end": "At the end",
    "start": "At the start",
    "after": "After a scene",
    "before": "Before a scene",
}


class SendReport:
    """What one send action did, in the user's words."""

    def __init__(self, *, ok: bool, action: str, message: str = "",
                 what_to_do: str = "", asset_id: str = "", scene_id: str = "",
                 path: str = "", position: int = 0, notes: list = None) -> None:
        self.ok = bool(ok)
        self.action = action
        self.message = message
        self.what_to_do = what_to_do
        self.asset_id = asset_id
        self.scene_id = scene_id
        self.path = path
        self.position = int(position)
        self.notes = list(notes or [])

    def describe(self) -> str:
        if self.ok:
            return self.message
        return f"{self.message} {self.what_to_do}".strip()

    def to_dict(self) -> dict:
        return {"ok": self.ok, "action": self.action, "message": self.message,
                "what_to_do": self.what_to_do, "asset_id": self.asset_id,
                "scene_id": self.scene_id, "path": self.path,
                "position": self.position, "notes": list(self.notes)}


def clip_metadata(path: Any, *, extra: Optional[dict] = None,
                  tools: Any = None) -> dict:
    """Describe a clip file, so the project stores facts rather than a guess.

    The caller's own notes (prompt, seed, backend) are kept, but the numbers -
    length, frame rate, size, codec - come from the file itself, measured with
    FFprobe.  When the file cannot be measured the difference is recorded
    instead of filled in with an assumption (sections 44, 100).
    """
    target = Path(path)
    metadata: dict = {"file": target.name, "generator": "imported"}
    try:
        if target.is_file():
            size = target.stat().st_size
            metadata["size_bytes"] = size
            metadata["size"] = human_bytes(size)
    except OSError:
        pass
    metadata.update(dict(extra or {}))

    check = _measure_clip(target, tools)
    if check is not None and getattr(check, "measured_with", ""):
        metadata.update({
            "measured": True, "measured_with": check.measured_with,
            "width": int(check.width or 0), "height": int(check.height or 0),
            "duration": float(check.duration or 0.0),
            "fps": float(check.fps or 0.0), "frames": int(check.frames or 0),
            "video_codec": str(check.video_codec or ""),
            "has_audio": bool(check.has_audio)})
    else:
        metadata["measured"] = False
        metadata["measured_with"] = ""
        metadata["note"] = ("The clip's length and frame rate could not be read "
                            "from the file. Install FFmpeg, or set the scene's "
                            "length by hand.")
    return metadata


def _measure_clip(target: Path, tools: Any = None) -> Any:
    """Measure a produced file, or None when that is not possible here."""
    if not target.is_file():
        return None
    try:
        from .video_validation import validate_video_file

        return validate_video_file(target, tools=tools)
    except Exception as exc:  # noqa: BLE001 - a measurement never blocks a send
        log_event("AI_CLIP_MEASURE_FAILED",
                  f"{target.name} could not be measured: {exc}", level="WARNING")
        return None


def video_asset_for(project: Any, asset_id: str) -> Any:
    """The project's asset record for an id, or None."""
    finder = getattr(project, "asset_by_id", None)
    if callable(finder):
        try:
            return finder(str(asset_id))
        except Exception:  # noqa: BLE001
            return None
    for asset in getattr(project, "assets", []) or []:
        if str(getattr(asset, "id", "")) == str(asset_id):
            return asset
    return None


def send_to_project(project_service: Any, source: Any, *,
                    name: str = "", metadata: Optional[dict] = None) -> SendReport:
    """Copy a generated file into the project as an asset (section 47)."""
    path = Path(str(source or ""))
    if not path.is_file():
        return SendReport(ok=False, action="send_to_project",
                          message=f"There is nothing to add: {path} is not there.",
                          what_to_do="Generate the clip again, or pick another "
                                     "file.")
    if project_service is None or not getattr(project_service, "is_open", False):
        return SendReport(
            ok=False, action="send_to_project",
            message="No project is open, so the clip has nowhere to go.",
            what_to_do="Open or create a project, then send it again.")
    try:
        report = project_service.import_asset(path, kind="video",
                                              name=str(name or ""))
    except Exception as exc:  # noqa: BLE001 - reported, never a crash
        return SendReport(ok=False, action="send_to_project",
                          message=f"The clip could not be added: {exc}",
                          what_to_do="Check the project folder is writable and "
                                     "has free space.")
    asset = getattr(report, "asset", None)
    if asset is None:
        return SendReport(
            ok=False, action="send_to_project",
            message=str(getattr(report, "error", "") or
                        "The clip could not be added to the project."),
            what_to_do=str(getattr(report, "what_to_do", "") or
                           "Check the project folder and try again."))
    if metadata:
        try:
            store = getattr(asset, "metadata", None)
            if isinstance(store, dict):
                store.update(dict(metadata))
            elif hasattr(asset, "extra") and isinstance(asset.extra, dict):
                asset.extra.update(dict(metadata))
        except Exception:  # noqa: BLE001 - metadata is a bonus
            pass
    log_event("AI_CLIP_SENT_TO_PROJECT", f"{path.name} was added to the project",
              asset=str(getattr(asset, "id", "")))
    return SendReport(ok=True, action="send_to_project",
                      message=f"{path.name} was added to the project's assets.",
                      asset_id=str(getattr(asset, "id", "")), path=str(path))


def send_to_scene(project_service: Any, source: Any, *, name: str = "",
                  scene_id: str = "", metadata: Optional[dict] = None,
                  duration: float = 0.0, fps: float = 0.0,
                  fit: str = "cover") -> SendReport:
    """Add a clip to the project as a scene that plays it (section 30).

    Without ``scene_id`` a new scene is created and placed at the end of the
    timeline.  With one, that scene is updated to play this clip - which is how
    "Send to Scene" is used from the scene list.
    """
    if project_service is None or not getattr(project_service, "is_open", False):
        return SendReport(
            ok=False, action="send_to_scene",
            message="No project is open, so the clip has nowhere to go.",
            what_to_do="Open or create a project, then send it again.")
    added = send_to_project(project_service, source, name=name, metadata=metadata)
    if not added.ok:
        return SendReport(ok=False, action="send_to_scene",
                          message=added.message, what_to_do=added.what_to_do)
    payload = dict(metadata or {})
    payload.update({
        "asset_id": added.asset_id,
        "duration": float(duration or payload.get("duration", 0.0) or 0.0),
        "fps": float(fps or payload.get("fps", 0.0) or 0.0),
        "fit": str(fit or "cover"),
    })
    try:
        project = project_service.current
        target_id = str(scene_id or "")
        if target_id:
            scene = _find_scene(project, target_id)
            if scene is None:
                return SendReport(
                    ok=False, action="send_to_scene",
                    message=f"The scene to update was not found: {target_id}",
                    what_to_do="Reload the project and try again.",
                    asset_id=added.asset_id)
            extra = {**dict(getattr(scene, "extra", {}) or {}), "video": payload}
            applied = project_service.update_scene_field(
                target_id, type=VIDEO_SCENE_TYPE, extra=extra)
            position = _index_of(project, target_id)
        else:
            scene = project_service.add_scene(scene_type=VIDEO_SCENE_TYPE,
                                              name=str(name or
                                                       Path(str(source)).stem))
            applied = project_service.update_scene_field(
                str(scene.id), type=VIDEO_SCENE_TYPE,
                duration=float(payload.get("duration", 0.0) or 0.0),
                extra={"video": payload})
            project = project_service.current
            position = _index_of(project, str(scene.id))
            target_id = str(scene.id)
        if not applied or not _scene_plays(project_service, target_id,
                                          added.asset_id):
            # The file is in the project, the scene is not pointing at it: say
            # so rather than reporting a success the user would not see.
            return SendReport(
                ok=False, action="send_to_scene",
                message=(f"{Path(str(source)).name} was added to the project's "
                         f"assets, but the scene was not changed to play it."),
                what_to_do=("Set the scene's clip in the scene inspector: the "
                            "scene editor refused the change."),
                asset_id=added.asset_id, scene_id=target_id, path=str(source))
    except Exception as exc:  # noqa: BLE001
        return SendReport(
            ok=False, action="send_to_scene",
            message=f"The clip was added as an asset, but the scene could not "
                    f"be created: {exc}",
            what_to_do="Add a scene yourself and use Send to Timeline, or check "
                       "the log.",
            asset_id=added.asset_id, path=str(source))
    notes = []
    if not float(payload.get("duration", 0.0) or 0.0):
        notes.append("The clip's length could not be read, so the scene keeps "
                     "its current duration. Set it in the scene settings.")
    log_event("AI_CLIP_SENT_TO_SCENE",
              f"{Path(str(source)).name} became scene {target_id}",
              asset=added.asset_id, scene=target_id)
    return SendReport(
        ok=True, action="send_to_scene",
        message=(f"{Path(str(source)).name} was added as a scene that plays it, "
                 f"at position {position + 1}."),
        asset_id=added.asset_id, scene_id=target_id, path=str(source),
        position=position, notes=notes)


def send_to_timeline(project_service: Any, source: Any, *, name: str = "",
                     placement: str = "end", relative_to: str = "",
                     metadata: Optional[dict] = None, duration: float = 0.0,
                     fps: float = 0.0) -> SendReport:
    """Add a clip to the timeline at a stated place (section 29).

    The timeline is the list of scenes, so a placement is a position in that
    list: at the end, at the start, before or after a named scene.  The clip's
    real duration comes with it, so the timeline shows the clip's own length.
    """
    placement = str(placement or "end")
    if placement not in PLACEMENTS:
        return SendReport(
            ok=False, action="send_to_timeline",
            message=f"'{placement}' is not a placement this application offers.",
            what_to_do="Choose one of: "
                       + ", ".join(PLACEMENT_LABELS[item] for item in PLACEMENTS)
                       + ".")
    report = send_to_scene(project_service, source, name=name, metadata=metadata,
                           duration=duration, fps=fps)
    if not report.ok:
        report.action = "send_to_timeline"
        return report
    report.action = "send_to_timeline"
    try:
        project = project_service.current
        scene_id = report.scene_id
        position = report.position
        if placement == "start":
            project_service.move_scene(scene_id, 0)
            position = 0
        elif placement == "end":
            total = len(list(getattr(project, "scenes", []) or []))
            project_service.move_scene(scene_id, max(0, total - 1))
            position = max(0, total - 1)
        elif placement in ("before", "after"):
            anchor = _find_scene(project, relative_to)
            if anchor is None:
                report.notes.append(
                    f"The scene to place it {placement} was not found, so the "
                    f"clip stayed at the end.")
            else:
                index = _index_of(project, str(getattr(anchor, "id", "")))
                wanted = index if placement == "before" else index + 1
                project_service.move_scene(scene_id, max(0, wanted))
                position = max(0, wanted)
        report.position = position
    except Exception as exc:  # noqa: BLE001
        report.notes.append(f"The clip was added, but it could not be moved "
                            f"({exc}).")
    report.message = (f"{Path(str(source)).name} was added to the timeline "
                      f"at position {report.position + 1}"
                      + (f" ({PLACEMENT_LABELS[placement].lower()})"
                         if placement != "end" else "") + ".")
    log_event("AI_CLIP_SENT_TO_TIMELINE",
              f"{Path(str(source)).name} placed at {report.position + 1}",
              asset=report.asset_id, scene=report.scene_id)
    return report


def describe_send(report: SendReport) -> str:
    """One line for the status bar, with any notes."""
    text = report.describe()
    if report.notes:
        text += " " + " ".join(report.notes)
    return text


def _find_scene(project: Any, scene_id: str) -> Optional[Any]:
    for scene in getattr(project, "scenes", []) or []:
        if str(getattr(scene, "id", "")) == str(scene_id):
            return scene
    return None


def _scene_plays(project_service: Any, scene_id: str, asset_id: str) -> bool:
    """Whether a scene really ended up pointing at this clip."""
    scene = _find_scene(getattr(project_service, "current", None), scene_id)
    if scene is None:
        return False
    spec = dict(getattr(scene, "extra", {}) or {}).get("video")
    if not isinstance(spec, dict):
        return False
    return str(spec.get("asset_id", "")) == str(asset_id)


def _index_of(project: Any, scene_id: str) -> int:
    for index, scene in enumerate(list(getattr(project, "scenes", []) or [])):
        if str(getattr(scene, "id", "")) == str(scene_id):
            return index
    return 0


def make_video_scene(origin: dict) -> SceneSpec:
    """A scene that plays a clip, ready for a project that has no service.

    Used by the CLI and the tests, which build a scene and hand it to
    :meth:`ProjectService.add_scene`.  The renderer treats it exactly like a
    scene created through the interface.
    """
    payload = dict(origin or {})
    # ``fit`` is how the renderer scales a clip into the frame; cover is the
    # sensible default and is always written, so nothing downstream has to guess.
    payload.setdefault("fit", "cover")
    scene = SceneSpec(type=VIDEO_SCENE_TYPE,
                      duration=float(payload.get("duration", 0.0) or 0.0))
    scene.extra = {"video": payload}
    return scene


def element_for(scene: Any) -> Optional[ElementSpec]:  # pragma: no cover - helper
    """A video scene has no elements; this keeps callers honest about it."""
    return None
