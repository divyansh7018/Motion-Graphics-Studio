"""Output naming, atomic moves and render history (Stage E, sections 51-56).

A render never destroys an existing file.  The next free number in the user's
naming template is chosen, the finished video is written to a temporary file
first, verified, and only then moved into place - so a crash during the write
leaves the previous take exactly as it was.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from pathlib import Path
from typing import Any

from ..core.logging_setup import log_event

__all__ = [
    "OutputDecision",
    "HistoryEntry",
    "OutputService",
    "export_settings",
    "DEFAULT_TEMPLATE",
    "render_template",
    "sanitize_component",
    "sequence_from_name",
]

#: Matches the project model default and the UI preview, so the name shown
#: before a render is the name the render produces.
DEFAULT_TEMPLATE = "{name}_{seq}"

#: Characters that are not safe in a Windows file name.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class OutputDecision:
    """Where a render will be written, decided before it starts."""

    directory: Path
    filename: str
    path: Path
    sequence: int = 1
    template: str = DEFAULT_TEMPLATE
    project_name: str = ""
    container: str = "mp4"
    quality: str = ""
    resolution: str = ""
    #: True when the chosen name already exists (should never happen - the
    #: sequence is advanced past it).
    existed: bool = False
    #: Human-readable note shown next to the path in the UI.
    note: str = ""

    def to_dict(self) -> dict:
        return {"directory": str(self.directory), "filename": self.filename,
                "path": str(self.path), "sequence": self.sequence,
                "template": self.template, "note": self.note}


@dataclass
class HistoryEntry:
    """One finished (or failed) render, remembered in the project folder."""

    at: str = ""
    path: str = ""
    status: str = ""
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    size_bytes: int = 0
    quality: str = ""
    resolution: str = ""
    qc: str = ""
    notes: str = ""

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "at", "path", "status", "duration", "width", "height", "fps",
            "size_bytes", "quality", "resolution", "qc", "notes")}

    @classmethod
    def from_dict(cls, data: Any) -> "HistoryEntry":
        if not isinstance(data, dict):
            return cls()
        known = ("at", "path", "status", "duration", "width", "height", "fps",
                 "size_bytes", "quality", "resolution", "qc", "notes")
        return cls(**{key: data.get(key, getattr(cls(), key)) for key in known})


def sanitize_component(text: str, *, replacement: str = "_") -> str:
    """Make one path component safe on Windows without changing the meaning."""
    cleaned = _UNSAFE.sub(replacement, str(text or "")).strip().rstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "Video"


def render_template(template: str, *, project: str, sequence: int,
                    quality: str = "", resolution: str = "",
                    channel: str = "", date: str = "", when: Optional[datetime] = None) -> str:
    """Expand a naming template.

    The placeholder names are the ones the project model, the migrations and the
    export dialog already use (``{name}``, ``{seq}``, ...), so the name previewed
    in the UI is the name the render produces.  ``{project}``, ``{n}`` and
    ``{sequence}`` are accepted as aliases.  Unknown placeholders are left alone
    rather than raising: a typo in a template must not stop a finished render
    from being saved.
    """
    moment = when or datetime.now()
    text = str(template or DEFAULT_TEMPLATE)
    if not any(marker in text for marker in ("{seq}", "{n}", "{sequence}")):
        text = f"{text}_{{seq}}"
    number = str(int(sequence))
    values = {
        "name": sanitize_component(project),
        "project": sanitize_component(project),
        "channel": sanitize_component(channel) or "Channel",
        "seq": number,
        "n": number,
        "sequence": number,
        "quality": sanitize_component(quality),
        "resolution": sanitize_component(resolution),
        "date": date or moment.strftime("%Y-%m-%d"),
        "time": moment.strftime("%H%M"),
    }
    out = text
    for key, value in values.items():
        out = out.replace("{" + key + "}", str(value))
    return sanitize_component(out)


def sequence_from_name(name: str, pattern: Optional[str] = None) -> int:
    """Read the take number back out of a file name (0 when there is none)."""
    stem = Path(str(name)).stem
    match = re.search(r"(\d+)\s*$", stem)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return 0
    return 0


def export_settings(project: Any) -> Any:
    """The section that says *where* a render goes and what it is called.

    ``output_dir``, ``filename_template`` and ``next_sequence_number`` live on
    the project's export section, not on ``project.format``.  Passing the format
    spec here - as an earlier version did - silently used the defaults, so a
    folder the user picked was ignored and the render landed in ``renders/``
    anyway.  Falling back to the format spec keeps older callers working.
    """
    export = getattr(project, "export", None)
    return export if export is not None else getattr(project, "format", None)


class OutputService:
    """Decides output paths, moves finished files, and keeps render history."""

    HISTORY_FILE = "render_history.json"
    MAX_HISTORY = 50

    def __init__(self, project_dir: Path, *, history_path: Optional[Path] = None) -> None:
        self.project_dir = Path(project_dir)
        self.history_path = Path(history_path) if history_path else self.project_dir / self.HISTORY_FILE

    # -- planning --------------------------------------------------------

    def output_directory(self, settings: Any) -> Path:
        """The directory a render goes to, created only when a render starts."""
        raw = str(getattr(settings, "output_dir", "") or "").strip()
        if raw:
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = self.project_dir / candidate
            return candidate
        return self.project_dir / "renders"

    def decide(self, settings: Any, *, project_name: str, quality: str = "",
               resolution: str = "", container: str = "") -> OutputDecision:
        """Pick the next free file name.  Never overwrites an existing take."""
        directory = self.output_directory(settings)
        template = str(getattr(settings, "filename_template", "") or DEFAULT_TEMPLATE)
        container = str(container or getattr(settings, "container", "mp4") or "mp4")
        start = max(1, int(getattr(settings, "next_sequence_number", 1) or 1))

        # Start from the stored number, then walk forward past anything present.
        sequence = start
        highest_seen = 0
        for existing in directory.glob("*") if directory.is_dir() else []:
            number = sequence_from_name(existing.name)
            highest_seen = max(highest_seen, number)
        sequence = max(sequence, highest_seen + 1) if highest_seen else sequence

        filename = ""
        chosen = sequence
        for attempt in range(sequence, sequence + 1000):
            candidate = render_template(template, project=project_name, sequence=attempt,
                                        quality=quality, resolution=resolution)
            filename = f"{candidate}.{container}"
            chosen = attempt
            if not (directory / filename).exists():
                break
        else:  # pragma: no cover - 1000 existing takes
            filename = f"{render_template(template, project=project_name, sequence=sequence)}.{container}"

        return OutputDecision(directory=directory, filename=filename,
                              path=directory / filename, sequence=chosen,
                              template=template, project_name=str(project_name or ""),
                              container=container, quality=str(quality or ""),
                              resolution=str(resolution or ""))

    # -- staging ---------------------------------------------------------

    def staging_path(self, decision: OutputDecision) -> Path:
        """A temporary path in the *same* folder as the target.

        Same folder matters: a move across drives is a copy, and a copy can fail
        half-way.  Within one folder the final step is an atomic rename.
        """
        decision.directory.mkdir(parents=True, exist_ok=True)
        return decision.directory / f".{decision.path.stem}.rendering{decision.path.suffix}"

    def finalize(self, staged: Path, decision: OutputDecision) -> Path:
        """Move a verified file into place.  Refuses to overwrite a real take."""
        target = decision.path
        if target.exists():
            # Extremely unlikely (the name was chosen to be free) but the user's
            # existing file always wins.
            decision.note = "The chosen name appeared while rendering; saving as a new take."
            decision = self.decide_next_from(decision)
            target = decision.path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(target))
        log_event("RENDER_OUTPUT_SAVED", f"Saved {target.name}", path=str(target),
                  sequence=decision.sequence)
        return target

    def decide_next_from(self, decision: OutputDecision) -> OutputDecision:
        """Bump a decision to the next free take number."""
        sequence = int(decision.sequence) + 1
        for _ in range(1000):
            stem = render_template(decision.template, project=decision.project_name,
                                   sequence=sequence, quality=decision.quality,
                                   resolution=decision.resolution)
            filename = f"{stem}.{decision.container}"
            if not (decision.directory / filename).exists():
                return OutputDecision(
                    directory=decision.directory, filename=filename,
                    path=decision.directory / filename, sequence=sequence,
                    template=decision.template, project_name=decision.project_name,
                    container=decision.container, quality=decision.quality,
                    resolution=decision.resolution,
                    note="The original name was taken, so this is the next free take.",
                )
            sequence += 1
        raise OSError(
            f"Could not find a free file name in {decision.directory}. "
            f"Free some space or change the naming template."
        )

    # -- history ---------------------------------------------------------

    def history(self) -> list[HistoryEntry]:
        if not self.history_path.is_file():
            return []
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = data.get("renders", []) if isinstance(data, dict) else []
        return [HistoryEntry.from_dict(item) for item in items if isinstance(item, dict)]

    def record(self, entry: HistoryEntry) -> None:
        """Append a render to the history file (best effort, never fatal)."""
        entries = self.history()
        entries.insert(0, entry)
        entries = entries[: self.MAX_HISTORY]
        payload = {"version": 1, "renders": [item.to_dict() for item in entries]}
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.history_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temporary.replace(self.history_path)
        except OSError as exc:  # pragma: no cover - disk problems
            log_event("RENDER_HISTORY_FAILED", f"Could not write render history: {exc}")
