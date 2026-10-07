"""Presets that can be reused across projects (sections 47, 54).

Four kinds of preset, in one store: **image**, **video**, **prompt** and
**backend**.  A preset is plain settings - no files - so it can be exported,
copied between projects and shared without dragging a library along.

Applying a preset never applies part of it silently: :meth:`PresetStore.apply`
returns which keys were applied and which the current backend does not support,
so the interface can say "three of these settings were not applied because this
backend has no camera control".
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from ..core.logging_setup import log_event

__all__ = ["Preset", "PresetStore", "KINDS"]

#: The kinds of preset the studio keeps.
KINDS: tuple[str, ...] = ("image", "video", "prompt", "backend")

KIND_LABELS: dict[str, str] = {
    "image": "Image settings",
    "video": "Video settings",
    "prompt": "Prompt",
    "backend": "Backend settings",
}


def _new_id() -> str:
    return f"preset_{uuid.uuid4().hex[:10]}"


@dataclass
class Preset:
    """One saved set of settings."""

    id: str = ""
    name: str = ""
    kind: str = "video"
    values: dict = field(default_factory=dict)
    description: str = ""
    backend: str = ""
    model: str = ""
    favourite: bool = False
    created: float = field(default_factory=time.time)
    used_count: int = 0

    def label(self) -> str:
        return self.name or f"{KIND_LABELS.get(self.kind, self.kind)} preset"

    def summary(self) -> str:
        bits = [f"{key}={value}" for key, value in sorted(self.values.items())
                if value not in (None, "", 0, 0.0)]
        text = ", ".join(bits[:5])
        return text or "(no settings)"

    def to_dict(self) -> dict:
        data = dict(self.__dict__)
        data["label"] = self.label()
        data["summary"] = self.summary()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Preset":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{key: value for key, value in dict(data or {}).items()
                      if key in known})


class PresetStore:
    """Saved settings, kept in one JSON file beside the other studio state."""

    def __init__(self, path: Any = None, *, limit: int = 400) -> None:
        self.path = Path(path) if path else None
        self.limit = max(20, int(limit or 400))
        self._presets: list[Preset] = []
        self.loaded = False

    # -- storage -----------------------------------------------------------

    def load(self) -> list[Preset]:
        if self.loaded:
            return self._presets
        self.loaded = True
        if self.path is None or not self.path.is_file():
            return self._presets
        try:
            from ..core.atomicio import load_json

            result = load_json(self.path, default={},
                               quarantine_dir=self.path.parent / "corrupt")
            data = (result.data if getattr(result, "data", None) is not None
                    else {}) or {}
        except Exception as exc:  # noqa: BLE001
            log_event("AI_PRESETS_UNREADABLE",
                      f"The preset file could not be read: {exc}", level="WARNING")
            return self._presets
        for item in (data.get("presets") or []):
            try:
                self._presets.append(Preset.from_dict(item))
            except (TypeError, ValueError):
                continue
        return self._presets

    def save(self) -> bool:
        if self.path is None:
            return False
        try:
            from ..core.atomicio import save_with_backup

            self.path.parent.mkdir(parents=True, exist_ok=True)
            save_with_backup(self.path, {
                "schema_version": 1,
                "presets": [preset.to_dict() for preset in self._presets]})
            return True
        except Exception as exc:  # noqa: BLE001
            log_event("AI_PRESETS_SAVE_FAILED",
                      f"The preset file could not be saved: {exc}", level="WARNING")
            return False

    # -- reading -----------------------------------------------------------

    def all(self) -> list[Preset]:
        return self.load()

    def of_kind(self, kind: str) -> list[Preset]:
        return [preset for preset in self.all()
                if preset.kind == str(kind or "")]

    def find(self, preset_id: str) -> Optional[Preset]:
        for preset in self.all():
            if preset.id == preset_id:
                return preset
        return None

    def by_name(self, name: str, *, kind: str = "") -> Optional[Preset]:
        wanted = str(name or "").strip().lower()
        for preset in self.all():
            if preset.name.strip().lower() == wanted and (not kind or
                                                          preset.kind == kind):
                return preset
        return None

    def favourites(self) -> list[Preset]:
        return [preset for preset in self.all() if preset.favourite]

    # -- writing -----------------------------------------------------------

    def save_preset(self, name: str, values: dict, *, kind: str = "video",
                    description: str = "", backend: str = "",
                    model: str = "", favourite: bool = False) -> Preset:
        """Save settings under a name.  A repeated name is kept, not merged."""
        self.load()
        preset = Preset(id=_new_id(), name=str(name or "").strip(),
                        kind=str(kind or "video"), values=dict(values or {}),
                        description=str(description or ""), backend=str(backend or ""),
                        model=str(model or ""), favourite=bool(favourite))
        self._presets.insert(0, preset)
        self._trim()
        self.save()
        log_event("AI_PRESET_SAVED", f"Preset '{preset.label()}' was saved",
                  kind=preset.kind)
        return preset

    def update(self, preset_id: str, *, name: str = "", values: Optional[dict] = None,
               description: str = "") -> bool:
        preset = self.find(preset_id)
        if preset is None:
            return False
        if name:
            preset.name = str(name)
        if values is not None:
            preset.values = dict(values)
        if description:
            preset.description = str(description)
        self.save()
        return True

    def set_favourite(self, preset_id: str, favourite: bool = True) -> bool:
        preset = self.find(preset_id)
        if preset is None:
            return False
        preset.favourite = bool(favourite)
        self.save()
        return True

    def remove(self, preset_id: str) -> bool:
        self.load()
        before = len(self._presets)
        self._presets = [preset for preset in self._presets
                         if preset.id != preset_id]
        if len(self._presets) == before:
            return False
        self.save()
        return True

    def duplicate(self, preset_id: str, name: str = "") -> Optional[Preset]:
        preset = self.find(preset_id)
        if preset is None:
            return None
        return self.save_preset(name or f"{preset.name} (copy)", preset.values,
                                kind=preset.kind,
                                description=preset.description,
                                backend=preset.backend, model=preset.model)

    def apply(self, preset_id: str, request: Any, capabilities: Any = None) -> dict:
        """Apply a preset to a request, reporting what could not be applied.

        Returns ``{"applied": {...}, "skipped": {...}, "reasons": [...]}``.  A
        setting the current backend does not support is *skipped with a reason*
        rather than quietly dropped (section 54).
        """
        preset = self.find(preset_id)
        if preset is None:
            return {"applied": {}, "skipped": {}, "reasons": ["That preset is "
                                                              "no longer saved."],
                    "ok": False}
        skip_features = {
            "camera": "camera_control", "strength": "strength",
            "negative_prompt": "negative_prompt", "seed": "seed_control",
            "steps": "steps", "guidance": "guidance", "sampler": "sampler",
            "duration": "duration", "fps": "fps", "batch": "batch",
        }
        # Size, frame rate and duration are plain numbers, but a value outside
        # what the backend says it can do is refused here rather than sent and
        # rejected later (sections 41, 54).
        limits = {
            "width": ("min_dimension", "max_dimension"),
            "height": ("min_dimension", "max_dimension"),
            "fps": ("min_fps", "max_fps"),
            "duration": ("min_duration", "max_duration"),
        }
        applied: dict = {}
        skipped: dict = {}
        reasons: list[str] = []
        for key, value in preset.values.items():
            name = str(key)
            if capabilities is not None and name in limits:
                low_name, high_name = limits[name]
                low = getattr(capabilities, low_name, 0) or 0
                high = getattr(capabilities, high_name, 0) or 0
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    number = None
                if number is not None and ((high and number > float(high))
                                           or (low and number < float(low))):
                    skipped[name] = value
                    reasons.append(
                        f"'{name}' was not applied: this backend works between "
                        f"{low:g} and {high:g}, and {value} is outside that.")
                    continue
            feature = skip_features.get(name, "")
            if capabilities is not None and feature and \
                    not capabilities.supports_setting(feature):
                skipped[name] = value
                reasons.append(f"'{name}' was not applied: this backend has no "
                               f"setting for it.")
                continue
            if hasattr(request, name) or hasattr(request, "__setitem__"):
                try:
                    if hasattr(request, name):
                        setattr(request, name, value)
                    else:
                        request[name] = value
                    applied[name] = value
                    continue
                except Exception as exc:  # noqa: BLE001
                    skipped[name] = value
                    reasons.append(f"'{name}' could not be applied: {exc}")
                    continue
            skipped[name] = value
            reasons.append(f"'{name}' is not a setting this request has.")
        preset.used_count += 1
        self.save()
        return {"ok": True, "applied": applied, "skipped": skipped,
                "reasons": reasons}

    def capture(self, request: Any, *, names: Iterable[str]) -> dict:
        """Read the settings named from a request, for saving as a preset."""
        values: dict = {}
        for name in names:
            if hasattr(request, name):
                value = getattr(request, name)
                if value not in (None, ""):
                    values[name] = value
        return values

    def export(self, kind: str = "") -> dict:
        return {"schema_version": 1,
                "presets": [preset.to_dict() for preset in self.of_kind(kind)
                            if kind] if kind else
                           [preset.to_dict() for preset in self.all()]}

    def import_presets(self, payload: Any) -> int:
        """Bring presets in from another project's file.  Duplicate ids are kept."""
        self.load()
        items = payload.get("presets") if isinstance(payload, dict) else payload
        added = 0
        for item in list(items or []):
            try:
                preset = Preset.from_dict(item)
            except (TypeError, ValueError):
                continue
            preset.id = _new_id()
            self._presets.append(preset)
            added += 1
        if added:
            self._trim()
            self.save()
        return added

    def _trim(self) -> None:
        if len(self._presets) > self.limit:
            self._presets = sorted(self._presets, key=lambda item: item.created,
                                   reverse=True)[:self.limit]
