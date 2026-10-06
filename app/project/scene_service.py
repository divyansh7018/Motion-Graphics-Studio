"""A thin scene layer shared by the GUI and the CLI (directive section 45).

:class:`SceneService` sits on top of :class:`ProjectService` and answers the
questions both the storyboard and ``motion-studio scene`` need - list the
scenes, validate them, describe one - using the *same* scene engine the preview
and the renderer use.  There is no second implementation here: listing, timing
and validation all delegate to :mod:`app.scene`, so the CLI can never report
something the GUI would not.

The service is deliberately read-mostly.  Mutating operations (add, reorder,
enable, duplicate, copy/paste, element edits) live on :class:`ProjectService`
so they stay undoable in one place; :class:`SceneService` re-exposes the few the
CLI needs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..scene.storyboard import build_context, build_rows
from ..scene.validate import validate_project_scenes, validate_scene
from .service import ProjectService

__all__ = ["SceneService", "SceneSummary", "SceneDetail"]


class SceneSummary:
    """One row of ``scene list`` - no pixels, just facts."""

    def __init__(self, *, index: int, scene_id: str, name: str, type: str, start: float,
                 duration: float, duration_source: str, narration_duration: float,
                 element_count: int, enabled: bool, locked: bool, errors: int, warnings: int):
        self.index = index
        self.scene_id = scene_id
        self.name = name
        self.type = type
        self.start = start
        self.duration = duration
        self.duration_source = duration_source
        self.narration_duration = narration_duration
        self.element_count = element_count
        self.enabled = enabled
        self.locked = locked
        self.errors = errors
        self.warnings = warnings

    @property
    def narration_status(self) -> str:
        """A plain-English state for the narration, never a guess."""
        if self.narration_duration > 0:
            return f"ready ({self.narration_duration:.2f}s)"
        return "none"

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "id": self.scene_id,
            "name": self.name,
            "type": self.type,
            "start": round(self.start, 3),
            "duration": round(self.duration, 3),
            "duration_source": self.duration_source,
            "narration": self.narration_status,
            "elements": self.element_count,
            "enabled": self.enabled,
            "locked": self.locked,
            "errors": self.errors,
            "warnings": self.warnings,
        }


class SceneDetail:
    """The full description of one scene for ``scene info``."""

    def __init__(self, *, summary: SceneSummary, elements: list, issues: list, transition_in: str,
                 transition_out: str):
        self.summary = summary
        self.elements = elements
        self.issues = issues
        self.transition_in = transition_in
        self.transition_out = transition_out

    def to_dict(self) -> dict:
        data = self.summary.to_dict()
        data["transition_in"] = self.transition_in
        data["transition_out"] = self.transition_out
        data["elements"] = self.elements
        data["issues"] = self.issues
        return data


class SceneService:
    """Read-only scene queries plus the mutations the CLI needs."""

    def __init__(self, service: ProjectService):
        self._service = service

    # -- access ------------------------------------------------------------

    @property
    def project(self):
        return self._service.current

    @property
    def project_dir(self) -> Optional[Path]:
        session = getattr(self._service, "session", None)
        layout = getattr(session, "layout", None) if session is not None else None
        root = getattr(layout, "root", None)
        return Path(root) if root is not None else None

    def _require_project(self):
        project = self.project
        if project is None:
            raise ValueError("No project is open.")
        return project

    # -- queries -----------------------------------------------------------

    def list_scenes(self) -> list[SceneSummary]:
        """Every scene with its timing, narration state and issue counts."""
        project = self._require_project()
        rows, timeline = build_rows(project, validate=True)
        # build_rows only walks scenes on the timeline; disabled scenes are not
        # on it, so add them back so `list` shows the whole project.
        by_id = {scene.id: scene for scene in getattr(project, "scenes", []) or []}
        summaries: list[SceneSummary] = []
        seen = set()
        for row in rows:
            scene = by_id.get(row.scene_id)
            summaries.append(SceneSummary(
                index=row.index, scene_id=row.scene_id, name=row.name, type=row.type,
                start=row.start, duration=row.duration, duration_source=row.duration_source,
                narration_duration=row.narration_duration, element_count=row.element_count,
                enabled=bool(getattr(scene, "enabled", True)) if scene else True,
                locked=bool(getattr(scene, "locked", False)) if scene else False,
                errors=row.errors, warnings=row.issues,
            ))
            seen.add(row.scene_id)
        for index, scene in enumerate(getattr(project, "scenes", []) or []):
            if scene.id in seen:
                continue
            summaries.append(SceneSummary(
                index=index, scene_id=scene.id, name=scene.name or f"Scene {index + 1}",
                type=scene.type, start=0.0, duration=0.0, duration_source="disabled",
                narration_duration=float(getattr(scene.narration, "duration", 0.0) or 0.0),
                element_count=len(scene.elements or []),
                enabled=bool(getattr(scene, "enabled", True)),
                locked=bool(getattr(scene, "locked", False)),
                errors=0, warnings=0,
            ))
        return summaries

    def validate(self) -> Any:
        """Validate every scene; returns a :class:`SceneValidation`."""
        project = self._require_project()
        return validate_project_scenes(project, project_dir=self.project_dir)

    def scene_info(self, scene_id: str) -> Optional[SceneDetail]:
        """Full detail for one scene, or ``None`` if it does not exist."""
        project = self._require_project()
        scene = project.scene_by_id(scene_id)
        if scene is None:
            return None
        summary = next((s for s in self.list_scenes() if s.scene_id == scene_id), None)
        if summary is None:  # pragma: no cover - list_scenes covers every scene
            return None
        # Validate just this scene, so the issues belong to it unambiguously.
        context = build_context(project, project_dir=self.project_dir)
        result = validate_scene(scene, canvas=context.canvas, ctx=context,
                                project=project, project_dir=self.project_dir)
        issues = [
            {"code": issue.code, "message": issue.message, "severity": issue.severity,
             "element_id": issue.element_id, "what_to_do": issue.what_to_do}
            for issue in result.issues
        ]
        elements = [
            {"id": el.id, "kind": el.kind, "anchor": el.anchor,
             "text": el.text, "locked": bool(getattr(el, "locked", False))}
            for el in scene.elements or []
        ]
        return SceneDetail(
            summary=summary,
            elements=elements,
            issues=issues,
            transition_in=str(getattr(scene.transition_in, "type", "none") or "none"),
            transition_out=str(getattr(scene.transition_out, "type", "none") or "none"),
        )

    # -- mutations (delegate to the undoable ProjectService) ---------------

    def set_enabled(self, scene_id: str, enabled: bool) -> bool:
        return self._service.set_scene_enabled(scene_id, enabled)

    def set_locked(self, scene_id: str, locked: bool) -> bool:
        return self._service.set_scene_locked(scene_id, locked)

    def duplicate(self, scene_id: str):
        return self._service.duplicate_scene(scene_id)
