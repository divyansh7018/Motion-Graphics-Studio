"""Engine-level scene validation (Stage D).

The project model's validator (:mod:`app.project.validation`) checks the stored
shape of scenes; this module checks what a scene will *actually* look like when
drawn, by running the same layout pass the preview uses.  The two are kept
separate on purpose: a project can be structurally valid and still have a
headline that overflows at 9:16, and vice versa.

Every problem is returned as a friendly issue - what happened, why, and what to
do - because the whole point of validation here is to tell a beginner how to
fix their scene, not to fail a machine-readable gate.

Validation never mutates the scene and never raises for a malformed scene; a
broken scene yields issues instead of an exception.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from ..project.model import SCENE_TYPES, TRANSITION_TYPES
from .canvas import Canvas
from .elements import (
    ELEMENT_KINDS,
    ElementIssue,
    LayoutContext,
    build_asset_paths,
    layout_scene,
)
from .text import default_resolver

__all__ = [
    "SceneValidation",
    "validate_scene",
    "validate_scenes",
    "validate_project_scenes",
    "scene_issue_count",
]


@dataclass
class SceneValidation:
    """The outcome of validating one or more scenes."""

    issues: list = field(default_factory=list)

    def by_severity(self, severity: str) -> list:
        return [issue for issue in self.issues if issue.severity == severity]

    @property
    def errors(self) -> list:
        return self.by_severity("error")

    @property
    def warnings(self) -> list:
        return self.by_severity("warning")

    @property
    def ok(self) -> bool:
        """No errors.  Warnings alone do not block rendering."""
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _context(canvas: Canvas, project: Any = None, ctx: Optional[LayoutContext] = None,
             project_dir: Optional[Any] = None) -> LayoutContext:
    if ctx is not None:
        return ctx
    palette: dict = {}
    theme = getattr(project, "theme", None)
    if theme is not None:
        colors = getattr(theme, "colors", None)
        if isinstance(colors, dict):
            palette.update(colors)
        background = getattr(getattr(project, "format", None), "background", "")
        if background:
            palette["background"] = background
    resolver = default_resolver()

    def loader(family, size, bold, italic):
        return resolver.load(family, size=size, bold=bold, italic=italic)

    from pathlib import Path as _Path

    asset_root = None
    if project_dir is not None:
        asset_root = _Path(project_dir) / "assets"
    return LayoutContext(
        canvas=canvas,
        safe_area=canvas.safe_area(),
        palette=palette,
        font_loader=loader,
        font_family=str((palette or {}).get("font_family", "") or ""),
        asset_root=asset_root,
        asset_paths=build_asset_paths(project, _Path(project_dir) if project_dir else None),
    )


def validate_scene(scene: Any, *, canvas: Canvas, ctx: Optional[LayoutContext] = None,
                   project: Any = None, project_dir: Optional[Any] = None) -> SceneValidation:
    """Validate one scene for one canvas."""
    result = SceneValidation()
    known_assets = set()
    if project is not None:
        known_assets = {asset.id for asset in getattr(project, "assets", []) or []}

    _check_scene_model(scene, result, known_assets)

    layout_ctx = _context(canvas, project, ctx, project_dir)
    layout = layout_scene(getattr(scene, "elements", []) or [], layout_ctx)
    seen_codes: set = set()
    for issue in layout.issues:
        key = (issue.element_id, issue.code)
        if key in seen_codes:
            continue
        seen_codes.add(key)
        result.issues.append(issue)

    _check_text_width_for_orientation(layout, result, canvas)
    return result


def _check_scene_model(scene: Any, result: SceneValidation, known_assets: set) -> None:
    label = getattr(scene, "name", "") or getattr(scene, "id", "") or "scene"

    scene_type = str(getattr(scene, "type", "blank") or "blank")
    if scene_type not in SCENE_TYPES:
        result.issues.append(ElementIssue(
            "", "SCENE_TYPE", f"'{label}' uses the unknown scene type '{scene_type}'.",
            f"Choose one of: {', '.join(SCENE_TYPES)}.", severity="error"))

    duration = _number(getattr(scene, "duration", 0.0))
    narration = getattr(scene, "narration", None)
    has_narration = _number(getattr(narration, "duration", 0.0)) > 0
    if not has_narration and duration <= 0:
        result.issues.append(ElementIssue(
            "", "SCENE_DURATION",
            f"'{label}' has no narration and a duration of {duration:g}s, so it would not appear.",
            "Give it a duration of at least a second, or attach narration.", severity="error"))

    for name, transition in (("transition_in", getattr(scene, "transition_in", None)),
                             ("transition_out", getattr(scene, "transition_out", None))):
        kind = str(getattr(transition, "type", "none") or "none")
        length = _number(getattr(transition, "duration", 0.0))
        if kind not in TRANSITION_TYPES:
            result.issues.append(ElementIssue(
                "", "TRANSITION_TYPE", f"'{label}' uses the unknown transition '{kind}'.",
                f"Choose one of: {', '.join(TRANSITION_TYPES)}.", severity="error"))
        elif length < 0 or length > 5:
            result.issues.append(ElementIssue(
                "", "TRANSITION_DURATION",
                f"'{label}' {name} is {length:g}s; transitions run 0-5s.",
                "Use a length between 0 and 5 seconds.", severity="warning"))

    element_ids: set = set()
    elements = getattr(scene, "elements", []) or []
    if not elements and scene_type not in ("blank",):
        result.issues.append(ElementIssue(
            "", "SCENE_EMPTY", f"'{label}' is a {scene_type} scene with no elements.",
            "Add at least one element, or use a blank scene.", severity="warning"))

    for index, element in enumerate(elements):
        if element.id and element.id in element_ids:
            result.issues.append(ElementIssue(
                element.id, "ELEMENT_ID_DUPLICATE",
                f"'{label}' has two elements with the id '{element.id}'.",
                "Give each element a unique id.", severity="error"))
        element_ids.add(element.id or f"__{index}")

        kind = str(getattr(element, "kind", "text") or "text")
        if kind not in ELEMENT_KINDS:
            result.issues.append(ElementIssue(
                element.id, "ELEMENT_KIND", f"Unknown element kind '{kind}'.",
                f"Choose one of: {', '.join(ELEMENT_KINDS)}.", severity="error"))

        position = getattr(element, "position", None) or {}
        for axis in ("x", "y"):
            value = _number(position.get(axis, 0.5), 0.5)
            if not 0.0 <= value <= 1.0:
                result.issues.append(ElementIssue(
                    element.id, "POSITION_OUT_OF_RANGE",
                    f"An element's {axis} position is {value:g}; positions are 0..1.",
                    "Move the element inside the frame.", severity="error"))

        asset_id = str(getattr(element, "asset_id", "") or "")
        if asset_id and known_assets and asset_id not in known_assets:
            result.issues.append(ElementIssue(
                element.id, "ASSET_MISSING_REF",
                f"An element refers to the asset '{asset_id}', which is not in the project.",
                "Add the asset, or point the element at one that exists.", severity="error"))


def _check_text_width_for_orientation(layout: Any, result: SceneValidation, canvas: Canvas) -> None:
    """On narrow portrait frames, warn when body text is set very large.

    A 20% headline is fine in 16:9 but becomes two giant lines in 9:16.  This is
    a suggestion, not an error - fitting already guarantees it will not overflow.
    """
    if canvas.orientation != "portrait":
        return
    for element in layout.elements:
        if element.kind not in ("text", "number") or not element.visible:
            continue
        if element.font_size and element.font_size > canvas.reference_dimension * 0.16:
            result.issues.append(ElementIssue(
                element.element_id, "PORTRAIT_TEXT_LARGE",
                "This text is very large for a portrait frame.",
                "Consider a smaller size, or check how it reads on a phone.",
                severity="info"))


def validate_scenes(scenes: Sequence[Any], *, canvas: Canvas,
                    ctx: Optional[LayoutContext] = None, project: Any = None,
                    project_dir: Optional[Any] = None) -> SceneValidation:
    result = SceneValidation()
    for index, scene in enumerate(scenes):
        for issue in validate_scene(scene, canvas=canvas, ctx=ctx, project=project,
                                    project_dir=project_dir).issues:
            prefixed = ElementIssue(
                issue.element_id, issue.code,
                f"Scene {index + 1}: {issue.message}", issue.what_to_do, issue.severity)
            result.issues.append(prefixed)
    return result


def validate_project_scenes(project: Any, *, canvas: Optional[Canvas] = None,
                          project_dir: Optional[Any] = None) -> SceneValidation:
    """Validate every scene of a project at the project's own format."""
    if canvas is None:
        canvas = Canvas.from_project(project)
    return validate_scenes(getattr(project, "scenes", []) or [], canvas=canvas, project=project,
                           project_dir=project_dir)


def scene_issue_count(validation: SceneValidation, *codes: str) -> int:
    if not codes:
        return len(validation.issues)
    return sum(1 for issue in validation.issues if issue.code in codes)
