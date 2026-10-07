"""Prompts: library, favourites, recents and templates (sections 48, 49).

A prompt is the user's writing, so this module treats it that way: nothing here
rewrites a prompt, and nothing here sends a prompt anywhere.  Templates are
filled in only when the user asks, and the filled text is shown for approval
before it can be used (section 49).

The store is shared with the image half of the studio - the same prompt library
feeds Image Studio and the AI Studio - so there is one place a user's wording
lives, not two.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from ..core.logging_setup import log_event

__all__ = ["PromptEntry", "PromptTemplate", "PromptWorkspace", "TEMPLATES",
           "PURPOSES"]

#: What a prompt is for.  The interface groups by this.
PURPOSES: tuple[str, ...] = ("image", "video", "scene", "style", "character",
                             "negative", "other")


def _new_id() -> str:
    return f"prompt_{uuid.uuid4().hex[:10]}"


@dataclass
class PromptEntry:
    """One saved prompt, with the metadata the workflow needs."""

    id: str = ""
    text: str = ""
    negative: str = ""
    name: str = ""
    purpose: str = "image"
    tags: list = field(default_factory=list)
    favourite: bool = False
    used_count: int = 0
    created: float = field(default_factory=time.time)
    last_used: float = 0.0
    template: str = ""
    source: str = ""
    variables: dict = field(default_factory=dict)

    def label(self) -> str:
        if self.name:
            return self.name
        text = " ".join(str(self.text or "").split())
        return (text[:48] + "...") if len(text) > 51 else text or "(empty)"

    def to_dict(self) -> dict:
        data = dict(self.__dict__)
        data["label"] = self.label()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "PromptEntry":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{key: value for key, value in dict(data or {}).items()
                      if key in known})


@dataclass
class PromptTemplate:
    """A prompt with named slots, filled in on request and then shown."""

    name: str
    text: str
    purpose: str = "video"
    description: str = ""
    variables: tuple = ()

    def fill(self, values: Optional[dict] = None) -> str:
        """Substitute the slots, leaving any that were not filled visibly in place.

        An unfilled slot stays as ``{name}`` rather than becoming an empty
        string: a prompt with a hole in it should look like it has one.
        """
        values = dict(values or {})
        text = str(self.text)
        for key in self.variables:
            replacement = values.get(key)
            if replacement not in (None, ""):
                text = text.replace("{" + key + "}", str(replacement))
        return text

    def to_dict(self) -> dict:
        return {"name": self.name, "text": self.text, "purpose": self.purpose,
                "description": self.description,
                "variables": list(self.variables),
                "filled_example": self.fill()}


#: Built-in templates.  They are starting points, not scripts: nothing is sent
#: until the user edits and approves the filled text (section 49).
TEMPLATES: tuple[PromptTemplate, ...] = (
    PromptTemplate(
        name="Scene description",
        purpose="video",
        description="Describe what happens in one scene.",
        variables=("subject", "action", "setting", "mood", "camera"),
        text=("{subject} {action} in {setting}. {mood} mood, {camera} camera "
              "movement, consistent lighting.")),
    PromptTemplate(
        name="Product shot",
        purpose="video",
        description="A clean product scene with a slow move.",
        variables=("product", "surface", "lighting"),
        text=("A close shot of {product} on {surface}, {lighting}, slow dolly "
              "in, shallow depth of field, no text.")),
    PromptTemplate(
        name="Character reference",
        purpose="character",
        description="Describe a character consistently across clips.",
        variables=("character", "age", "clothing", "features"),
        text=("{character}, {age}, wearing {clothing}, {features}, neutral "
              "background, even light, full body.")),
    PromptTemplate(
        name="Style reference",
        purpose="style",
        description="Describe a look to keep across generations.",
        variables=("style", "palette", "light"),
        text=("{style} look, {palette} palette, {light}, film grain, no text.")),
    PromptTemplate(
        name="Negative prompt",
        purpose="negative",
        description="What to keep out of a generation.",
        variables=("unwanted",),
        text=("blurry, low detail, distorted, watermark, text, logo, {unwanted}")),
)


class PromptWorkspace:
    """The prompt library: saved prompts, favourites, recents and templates."""

    def __init__(self, path: Any = None, *, limit: int = 500) -> None:
        self.path = Path(path) if path else None
        self.limit = max(20, int(limit or 500))
        self._entries: list[PromptEntry] = []
        self.loaded = False

    # -- storage -----------------------------------------------------------

    def load(self) -> list[PromptEntry]:
        if self.loaded:
            return self._entries
        self.loaded = True
        if self.path is None or not self.path.is_file():
            return self._entries
        try:
            from ..core.atomicio import load_json

            result = load_json(self.path, default={},
                               quarantine_dir=self.path.parent / "corrupt")
            data = (result.data if getattr(result, "data", None) is not None
                    else {}) or {}
        except Exception as exc:  # noqa: BLE001
            log_event("AI_PROMPTS_UNREADABLE",
                      f"The prompt library could not be read: {exc}",
                      level="WARNING")
            return self._entries
        for item in (data.get("prompts") or []):
            try:
                self._entries.append(PromptEntry.from_dict(item))
            except (TypeError, ValueError):
                continue
        return self._entries

    def save(self) -> bool:
        if self.path is None:
            return False
        try:
            from ..core.atomicio import save_with_backup

            self.path.parent.mkdir(parents=True, exist_ok=True)
            save_with_backup(self.path, {
                "schema_version": 1,
                "prompts": [entry.to_dict() for entry in self._entries]})
            return True
        except Exception as exc:  # noqa: BLE001
            log_event("AI_PROMPTS_SAVE_FAILED",
                      f"The prompt library could not be saved: {exc}",
                      level="WARNING")
            return False

    # -- reading -----------------------------------------------------------

    def all(self) -> list[PromptEntry]:
        return self.load()

    def recents(self, limit: int = 20) -> list[PromptEntry]:
        used = [entry for entry in self.all() if entry.last_used]
        return sorted(used, key=lambda item: item.last_used, reverse=True)[:limit]

    def library(self, limit: int = 15) -> list[PromptEntry]:
        """The prompts to show in the library (section 55).

        Recently used first, then everything else that is saved: a prompt the
        user saved but has not used yet is still theirs, and leaving it out of
        the list until it happens to be used would look like a lost save.
        """
        entries = list(self.recents(limit))
        seen = {entry.id for entry in entries}
        for entry in self.all():
            if len(entries) >= max(1, int(limit)):
                break
            if entry.id in seen:
                continue
            entries.append(entry)
            seen.add(entry.id)
        return entries

    def favourites(self) -> list[PromptEntry]:
        return [entry for entry in self.all() if entry.favourite]

    def find(self, prompt_id: str) -> Optional[PromptEntry]:
        for entry in self.all():
            if entry.id == prompt_id:
                return entry
        return None

    def by_text(self, text: str) -> Optional[PromptEntry]:
        wanted = str(text or "").strip()
        for entry in self.all():
            if entry.text.strip() == wanted:
                return entry
        return None

    def for_purpose(self, purpose: str) -> list[PromptEntry]:
        return [entry for entry in self.all() if entry.purpose == str(purpose)]

    def search(self, text: str) -> list[PromptEntry]:
        needle = str(text or "").strip().lower()
        if not needle:
            return self.all()
        return [entry for entry in self.all()
                if needle in (entry.text + " " + entry.name + " " +
                              " ".join(entry.tags or [])).lower()]

    # -- writing -----------------------------------------------------------

    def record_use(self, text: str, *, negative: str = "", purpose: str = "image",
                   name: str = "", source: str = "") -> PromptEntry:
        """Note that a prompt was used, so it appears under Recents."""
        self.load()
        cleaned = str(text or "").strip()
        entry = self.by_text(cleaned) if cleaned else None
        if entry is None:
            entry = PromptEntry(id=_new_id(), text=cleaned, negative=negative,
                                purpose=purpose, name=name, source=source,
                                created=time.time())
            self._entries.insert(0, entry)
        entry.used_count += 1
        entry.last_used = time.time()
        if negative and not entry.negative:
            entry.negative = negative
        self._trim()
        self.save()
        return entry

    def save_prompt(self, text: str, *, name: str = "", negative: str = "",
                    purpose: str = "image", tags: Iterable[str] = (),
                    template: str = "", source: str = "",
                    variables: Optional[dict] = None) -> PromptEntry:
        """Save a prompt the user wrote.  The text is stored exactly as typed."""
        self.load()
        entry = PromptEntry(
            id=_new_id(), text=str(text or ""), negative=str(negative or ""),
            name=str(name or ""), purpose=str(purpose or "image"),
            tags=[str(item) for item in (tags or []) if str(item).strip()],
            template=str(template or ""), source=str(source or ""),
            variables=dict(variables or {}), created=time.time())
        self._entries.insert(0, entry)
        self._trim()
        self.save()
        log_event("AI_PROMPT_SAVED", f"Prompt '{entry.label()}' was saved",
                  purpose=entry.purpose)
        return entry

    def rename(self, prompt_id: str, name: str) -> bool:
        entry = self.find(prompt_id)
        if entry is None:
            return False
        entry.name = str(name or "")
        self.save()
        return True

    def edit(self, prompt_id: str, text: str, *, negative: str = "") -> bool:
        entry = self.find(prompt_id)
        if entry is None:
            return False
        entry.text = str(text or "")
        if negative:
            entry.negative = str(negative)
        self.save()
        return True

    def set_favourite(self, prompt_id: str, favourite: bool = True) -> bool:
        entry = self.find(prompt_id)
        if entry is None:
            return False
        entry.favourite = bool(favourite)
        self.save()
        return True

    def remove(self, prompt_id: str) -> bool:
        self.load()
        before = len(self._entries)
        self._entries = [entry for entry in self._entries
                         if entry.id != prompt_id]
        if len(self._entries) == before:
            return False
        self.save()
        return True

    def clear(self) -> int:
        self.load()
        count = len(self._entries)
        self._entries = []
        self.save()
        return count

    # -- templates ---------------------------------------------------------

    def templates(self, *, purpose: str = "") -> list[PromptTemplate]:
        if not purpose:
            return list(TEMPLATES)
        return [item for item in TEMPLATES if item.purpose == str(purpose)]

    @staticmethod
    def from_template(template: Any, values: Optional[dict] = None) -> str:
        if isinstance(template, str):
            for item in TEMPLATES:
                if item.name == template:
                    return item.fill(values)
            return str(template)
        return template.fill(values)

    def variants(self, text: str, *, count: int = 3) -> list[str]:
        """Suggest rewordings, clearly marked as suggestions (section 48).

        These are deterministic rearrangements of what the user already wrote -
        not a language model, and not presented as one.  Nothing is used until
        the user picks one and approves it.
        """
        base = " ".join(str(text or "").split()).strip()
        if not base:
            return []
        pieces = [part.strip(" .,") for part in base.replace(";", ",").split(",")
                  if part.strip()]
        ideas: list[str] = []
        if len(pieces) > 1:
            ideas.append(", ".join(pieces[::-1]))
            ideas.append(", ".join([pieces[0]] + pieces[2:] + [pieces[1]])
                         if len(pieces) > 2 else ", ".join(pieces))
        ideas.append(base + ", wider shot, consistent lighting")
        ideas.append(base + ", closer shot, same subject, same style")
        seen: list[str] = []
        for idea in ideas:
            cleaned = idea.strip()
            if cleaned and cleaned != base and cleaned not in seen:
                seen.append(cleaned)
            if len(seen) >= max(1, int(count or 3)):
                break
        return seen

    def _trim(self) -> None:
        if len(self._entries) > self.limit:
            self._entries = sorted(self._entries, key=lambda item: item.created,
                                   reverse=True)[:self.limit]
