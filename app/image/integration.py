"""Image Studio to Project integration (Stage F, sections 26, 27, 28, 64).

The rule that keeps this honest: **the scene engine consumes the canonical asset
by ID.**  Sending an image to a scene imports it once into the project's asset
library and adds an element that points at that asset's ID.  Nothing is copied a
second time, and no parallel image system is created - so a project that is
moved, reopened or rendered resolves the same single file.

Every step reports what it did.  If no project is open the caller is told and
offered the two real options (create a project, or save to the asset library);
nothing is quietly dropped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..core.logging_setup import log_event
from ..project.model import AssetSpec, ElementSpec
from .metadata import read_metadata
from .validation import validate_image_file

__all__ = ["Placement", "PlacementResult", "SendToSceneOutcome",
           "send_to_scene", "use_in_project", "PLACEMENTS"]


class Placement:
    """Where an image can be placed in a scene."""

    OVERLAY = "overlay"
    BACKGROUND = "background"
    CHARACTER = "character"
    REFERENCE = "reference"


PLACEMENTS: tuple[str, ...] = (
    Placement.OVERLAY, Placement.BACKGROUND, Placement.CHARACTER,
    Placement.REFERENCE,
)

PLACEMENT_LABELS: dict[str, str] = {
    Placement.OVERLAY: "Overlay element",
    Placement.BACKGROUND: "Scene background",
    Placement.CHARACTER: "Character element",
    Placement.REFERENCE: "Reference (not rendered)",
}

#: Default layout per placement.  Positions are normalised 0..1, never pixels,
#: so the same project lays out correctly at any resolution.
PLACEMENT_LAYOUT: dict[str, dict] = {
    Placement.OVERLAY: {"anchor": "center",
                        "position": {"x": 0.5, "y": 0.5},
                        "size": {"mode": "relative", "value": 0.45}},
    Placement.CHARACTER: {"anchor": "bottom-center",
                          "position": {"x": 0.5, "y": 0.72},
                          "size": {"mode": "relative", "value": 0.55}},
    Placement.REFERENCE: {"anchor": "center",
                          "position": {"x": 0.5, "y": 0.5},
                          "size": {"mode": "relative", "value": 0.4}},
}


class PlacementResult:
    """What one send-to-scene action actually did."""

    def __init__(self, *, ok: bool = False, message: str = "",
                 asset: Optional[AssetSpec] = None,
                 element: Optional[ElementSpec] = None,
                 scene_id: str = "", scene_name: str = "",
                 placement: str = "", asset_id: str = "",
                 created_scene: bool = False,
                 why: str = "", what_to_do: str = "", code: str = "",
                 notes: Any = None) -> None:
        self.ok = bool(ok)
        self.message = str(message or "")
        self.asset = asset
        self.element = element
        self.scene_id = str(scene_id or "")
        self.scene_name = str(scene_name or "")
        self.placement = str(placement or "")
        self.asset_id = str(asset_id or "")
        self.created_scene = bool(created_scene)
        self.why = str(why or "")
        self.what_to_do = str(what_to_do or "")
        self.code = str(code or "")
        self.notes: list[str] = [str(item) for item in (notes or [])]

    def describe(self) -> str:
        if not self.ok:
            text = self.message or "The image could not be sent to the scene."
            if self.what_to_do:
                text += f" {self.what_to_do}"
            return text
        where = PLACEMENT_LABELS.get(self.placement, self.placement)
        return (f"{where} in '{self.scene_name}' using asset "
                f"'{self.asset_id}'" + (f" ({len(self.notes)} note(s))"
                                        if self.notes else ""))

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "message": self.message, "placement": self.placement,
            "scene_id": self.scene_id, "scene_name": self.scene_name,
            "asset_id": self.asset_id, "created_scene": self.created_scene,
            "why": self.why, "what_to_do": self.what_to_do, "code": self.code,
            "notes": list(self.notes),
            "asset_path": str(self.asset.path) if self.asset is not None else "",
            "element_id": self.element.id if self.element is not None else "",
        }


#: The two options offered when no project is open (section 27).
class SendToSceneOutcome:
    NO_PROJECT = "no_project"
    SENT = "sent"
    FAILED = "failed"


def _project_is_open(project_service: Any) -> bool:
    """Whether a project is open.

    ``ProjectService.is_open()`` is a **method** while the GUI's
    ``ProjectController.is_open`` is a **property**, so both shapes have to be
    handled - treating a property as a method (or the reverse) would silently
    report "no project" with one open.
    """
    checker = getattr(project_service, "is_open", None)
    if callable(checker):
        return bool(checker())
    if isinstance(checker, bool):
        return checker
    if checker is not None:
        return bool(checker)
    return bool(getattr(project_service, "has_project", False))


def _service_of(project_service: Any) -> Any:
    """The object that can actually change a project.

    The GUI holds a ``ProjectController`` (which exposes ``project``,
    ``layout`` and ``is_open``) while the CLI holds a ``ProjectService``.
    Mutations such as ``import_asset`` and ``edit`` live on the service, so a
    controller is unwrapped here rather than duplicating every call site.
    """
    inner = getattr(project_service, "service", None)
    if inner is not None and hasattr(inner, "import_asset"):
        return inner
    return project_service


def _layout_of(project_service: Any) -> Any:
    """The project layout, from either a controller or a service."""
    layout = getattr(project_service, "layout", None)
    if layout is None:
        layout = getattr(project_service, "current_layout", None)
    return layout


def _current_project(project_service: Any) -> Any:
    """The open project, or None."""
    project = getattr(project_service, "current", None)
    if project is None:
        project = getattr(project_service, "project", None)
    return project


def _metadata_notes(source: Path) -> list[str]:
    """The generation facts worth carrying into the asset record.

    Only what really exists is stored: a hand-drawn rectangle has no seed and
    gets none, rather than an invented one.
    """
    metadata = read_metadata(source)
    if metadata is None:
        return []
    notes: list[str] = []
    if metadata.prompt:
        prompt = metadata.prompt.strip().replace("\n", " ")
        notes.append("Prompt: " + (prompt[:160] + "..." if len(prompt) > 160
                                   else prompt))
    if metadata.model:
        notes.append(f"Model: {metadata.model}")
    if metadata.backend:
        notes.append(f"Backend: {metadata.backend}")
    if metadata.seed is not None:
        notes.append(f"Seed: {metadata.seed}")
    if metadata.width and metadata.height:
        notes.append(f"Resolution: {metadata.width}x{metadata.height}")
    if metadata.origin:
        notes.append(f"Origin: {metadata.origin}")
    return notes


def use_in_project(service: Any, source: Any, *, name: str = "",
                   copy: bool = True) -> PlacementResult:
    """Import an image into the open project's asset library.

    This is the "Use in Project" action: the file becomes a project asset the
    scenes can reference.  It does not add an element - that is
    :func:`send_to_scene`.
    """
    project_service = _service_of(getattr(service, "projects", service))
    source_path = Path(source)
    check = validate_image_file(source_path, required=True,
                                label="The image")
    if not check.ok:
        return PlacementResult(
            ok=False, message=check.error, what_to_do=check.what_to_do,
            code="IMAGE_INVALID")

    if not _project_is_open(project_service):
        return PlacementResult(
            ok=False,
            message="No project is open, so there is nowhere to add this image.",
            why="Images are added to a project's asset library.",
            what_to_do=("Create or open a project first, or save the image to "
                        "the asset library."),
            code=SendToSceneOutcome.NO_PROJECT)

    existing = _already_in_project(project_service, source_path)
    if existing is not None:
        return PlacementResult(
            ok=True, asset=existing, asset_id=existing.id,
            message=(f"This image is already in the project as asset "
                     f"'{existing.id}'. It was not copied again."),
            notes=[])

    report = project_service.import_asset(source_path, kind="image", copy=copy,
                                          name=name or source_path.stem)
    if not report.ok or report.asset is None:
        return PlacementResult(
            ok=False,
            message=report.error or "The image could not be added to the project.",
            why="The project refused the file.",
            what_to_do=("Check the file opens in an image viewer, and that the "
                        "project folder is writable."),
            code="IMPORT_FAILED")

    notes = list(report.notes or []) + _metadata_notes(source_path)
    if notes:
        _apply_asset_notes(project_service, report.asset.id, notes)
    log_event("IMAGE_ADDED_TO_PROJECT", f"Asset {report.asset.id}",
              asset=report.asset.id, copied=report.copied,
              path=report.asset.path)
    return PlacementResult(
        ok=True, asset=report.asset, asset_id=report.asset.id,
        message=(f"Added to the project as asset '{report.asset.id}'"
                 + (" (copied into the project folder)" if report.copied
                    else " (referenced where it is)")),
        notes=notes)


def _already_in_project(project_service: Any, source: Path) -> Any:
    """An asset in the project that holds this exact file, if there is one.

    Matched by checksum, so the same picture is reused rather than copied a
    second time - including when it was imported under a different name.  The
    files are compared by size first, so a large library is not hashed for
    nothing.
    """
    project = _current_project(project_service)
    layout = _layout_of(project_service)
    if project is None or layout is None:
        return None
    try:
        size = Path(source).stat().st_size
    except OSError:
        return None

    from ..project.assets import file_checksum

    wanted = ""
    for asset in getattr(project, "assets", []) or []:
        if asset.kind not in ("image", "logo", "svg"):
            continue
        if int(asset.size_bytes or 0) != size:
            continue
        try:
            target = asset.resolve(layout.root)
        except Exception:  # noqa: BLE001 - an unresolvable asset is skipped
            continue
        if not target.is_file():
            continue
        if not wanted:
            wanted = file_checksum(Path(source))
        if file_checksum(target) == wanted:
            return asset
    return None


def _apply_asset_notes(project_service: Any, asset_id: str,
                       notes: list[str]) -> None:
    """Store the generation facts on the asset, as one undoable edit."""
    text = " | ".join(notes)

    def mutate(project: Any) -> None:
        asset = project.asset_by_id(asset_id)
        if asset is not None:
            asset.notes = text

    try:
        project_service.edit("Record image metadata", mutate)
    except Exception as exc:  # noqa: BLE001 - a note is never worth failing for
        log_event("IMAGE_ASSET_NOTES_FAILED", "Asset notes could not be stored",
                  asset=asset_id, error=str(exc))


def send_to_scene(service: Any, source: Any, *, scene_id: str = "",
                  placement: str = Placement.OVERLAY,
                  name: str = "", create_scene: bool = False,
                  copy: bool = True) -> PlacementResult:
    """One click: import the image and place it in a scene.

    The element points at the imported asset's ID, so the scene and every other
    reference resolve the same single file (section 28).
    """
    project_service = _service_of(getattr(service, "projects", service))
    kind = str(placement or Placement.OVERLAY).lower()
    if kind not in PLACEMENTS:
        return PlacementResult(
            ok=False, message=f"'{placement}' is not a placement this build knows.",
            what_to_do=("Choose Overlay, Background, Character or Reference."),
            code="UNKNOWN_PLACEMENT")

    if not _project_is_open(project_service):
        return PlacementResult(
            ok=False,
            message="No project is open, so the image cannot be sent to a scene.",
            why="A scene belongs to a project.",
            what_to_do=("Create a project, or save the image to the asset "
                        "library and add it later."),
            code=SendToSceneOutcome.NO_PROJECT)

    imported = use_in_project(project_service, source, name=name, copy=copy)
    if not imported.ok or imported.asset is None:
        imported.placement = kind
        return imported

    project = _current_project(project_service)
    if project is None:  # pragma: no cover - guarded by is_open above
        return PlacementResult(ok=False, message="The project is not available.",
                               code="NO_PROJECT")

    target_scene = None
    created_scene = False
    if scene_id:
        target_scene = project.scene_by_id(scene_id)
        if target_scene is None:
            return PlacementResult(
                ok=False,
                message=f"There is no scene with the id '{scene_id}'.",
                why="It may have been deleted.",
                what_to_do="Choose one of the scenes listed in the project.",
                code="SCENE_NOT_FOUND")
    elif create_scene:
        target_scene = project_service.add_scene("blank",
                                                 name=f"{imported.asset.name} scene")
        created_scene = True
    else:
        # No scene chosen: use the first one, and say so.
        target_scene = project.scenes[0] if project.scenes else None
        if target_scene is None:
            target_scene = project_service.add_scene("blank", name="Scene 1")
            created_scene = True

    if kind == Placement.BACKGROUND:
        _set_background(project_service, target_scene.id, imported.asset)
        log_event("IMAGE_SENT_TO_SCENE", f"Background of {target_scene.name}",
                  asset=imported.asset.id, scene=target_scene.id)
        return PlacementResult(
            ok=True, asset=imported.asset, asset_id=imported.asset.id,
            scene_id=target_scene.id, scene_name=target_scene.name,
            placement=kind, created_scene=created_scene,
            message=(f"Set as the background of '{target_scene.name}' "
                     f"(asset '{imported.asset.id}')"),
            notes=imported.notes)

    element = _build_element(imported.asset, kind)
    added = project_service.add_element(target_scene.id, element)
    if added is None:
        return PlacementResult(
            ok=False,
            message=f"The element could not be added to '{target_scene.name}'.",
            why="The project refused the change.",
            what_to_do="Check the scene is not locked, then try again.",
            code="ELEMENT_FAILED")
    log_event("IMAGE_SENT_TO_SCENE", f"{PLACEMENT_LABELS.get(kind, kind)} added",
              asset=imported.asset.id, scene=target_scene.id, element=added.id)
    return PlacementResult(
        ok=True, asset=imported.asset, element=added,
        asset_id=imported.asset.id, scene_id=target_scene.id,
        scene_name=target_scene.name, placement=kind,
        created_scene=created_scene,
        message=(f"{PLACEMENT_LABELS.get(kind, kind)} added to "
                 f"'{target_scene.name}' (asset '{imported.asset.id}')"),
        notes=imported.notes)


def _set_background(project_service: Any, scene_id: str, asset: AssetSpec) -> None:
    """Point the scene's background at the asset ID, not at a file path."""
    def mutate(project: Any) -> None:
        scene = project.scene_by_id(scene_id)
        if scene is not None:
            # The stored value is the asset id; the renderer resolves it through
            # the project's asset table, exactly like an element does.
            scene.background = asset.id

    project_service.edit("Set scene background", mutate)


def _build_element(asset: AssetSpec, placement: str) -> ElementSpec:
    """An image element that references the asset by ID."""
    layout = dict(PLACEMENT_LAYOUT.get(placement, PLACEMENT_LAYOUT[Placement.OVERLAY]))
    return ElementSpec(
        kind="image",
        asset_id=asset.id,
        anchor=str(layout.get("anchor", "center")),
        position=dict(layout.get("position", {"x": 0.5, "y": 0.5})),
        size=dict(layout.get("size", {"mode": "relative", "value": 0.45})),
        z_index=0,
    )


def resolve_element_image(project: Any, element: ElementSpec,
                          project_dir: Any = None) -> Optional[Path]:
    """The file an image element points at, or None if the asset has gone.

    Resolution is delegated to the scene module's own asset table, so Image
    Studio and the renderer agree by construction and there is no second image
    system to fall out of step (section 28).
    """
    if element is None or element.kind != "image" or not element.asset_id:
        return None
    if project_dir is None:
        project_dir = getattr(project, "directory", None) \
            or getattr(project, "root", None)
    if project_dir is None:
        return None
    from ..scene.elements import build_asset_paths

    resolved = build_asset_paths(project, Path(project_dir)).get(element.asset_id)
    if resolved is None:
        return None
    target = Path(resolved)
    return target if target.is_file() else None
