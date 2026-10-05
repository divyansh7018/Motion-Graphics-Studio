"""The voice catalogue: discovery, filtering and validation (sections 5-7, 33).

Voices are **discovered from the installed Kokoro model**, never listed in this
file.  If the user adds voice files, they appear on the next refresh without any
code change.  If a voice is missing, it is not shown.

Language and voice are kept as separate concepts, and a voice is only offered for
languages the installed pipeline actually supports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from app.tts.capabilities import KokoroStatus, probe_kokoro

#: Gender values the interface filters on.  "unknown" is used when the engine
#: does not publish gender for a voice, rather than guessing.
GENDERS: tuple[str, ...] = ("female", "male", "unknown")

GENDER_LABELS = {
    "female": "Female",
    "male": "Male",
    "unknown": "Not specified",
}

#: Kokoro publishes gender in a metadata file beside the voices when it can.
GENDER_METADATA_NAMES: tuple[str, ...] = ("voices.json", "voice_metadata.json", "metadata.json")

#: Display names for the language codes Kokoro uses.  This is presentation only:
#: the set of languages always comes from the installed catalogue, and the raw
#: code is shown alongside so nothing is claimed that the engine did not report.
LANGUAGE_LABELS: dict[str, str] = {
    "a": "English (US)", "b": "English (UK)", "e": "Spanish", "f": "French",
    "h": "Hindi", "i": "Italian", "j": "Japanese", "p": "Portuguese",
    "z": "Mandarin Chinese",
    "en-us": "English (US)", "en-gb": "English (UK)", "es": "Spanish",
    "fr-fr": "French", "hi": "Hindi", "it": "Italian", "ja": "Japanese",
    "pt-br": "Portuguese (BR)", "zh": "Mandarin Chinese",
}


def language_label(code: str) -> str:
    """A human name for a language code, falling back to the code itself."""
    if not code:
        return "Not specified"
    key = code.strip().lower()
    if key in LANGUAGE_LABELS:
        return LANGUAGE_LABELS[key]
    if len(key) >= 2 and key[0] in LANGUAGE_LABELS:
        return LANGUAGE_LABELS[key[0]]
    return code


#: Heuristic used only when the engine publishes no gender table.  Kokoro voice
#: ids start with "a"/"b"/"e"/... for language and then "f_" or "m_" for gender
#: in the reference catalogue; anything else stays "unknown".
_GENDER_HINTS = {"f": "female", "m": "male"}


@dataclass
class VoiceInfo:
    """One voice the installed engine can use."""

    id: str
    #: Language code or label as reported by the engine.
    language: str = ""
    gender: str = "unknown"
    #: Where this entry came from ("model", "engine", "settings").
    source: str = "model"
    #: Human friendly name, e.g. "af_bella" -> "af bella".
    label: str = ""
    #: False when the voice is listed but cannot generate (missing file).
    available: bool = True
    #: Why it is unavailable, in plain words.
    note: str = ""
    favorite: bool = False

    def __post_init__(self) -> None:
        if not self.label:
            self.label = self.id.replace("_", " ").strip() or self.id

    @property
    def display(self) -> str:
        gender = GENDER_LABELS.get(self.gender, self.gender)
        parts = [self.label]
        if self.language:
            parts.append(self.language)
        if gender != "Not specified":
            parts.append(gender)
        text = " · ".join(parts)
        return text if self.available else f"{text} (unavailable)"


@dataclass
class VoiceCatalogue:
    """The voices and languages available right now."""

    voices: list[VoiceInfo] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    #: Where the language list came from, shown to the user (section 33).
    language_source: str = "none"
    #: Where the voices were discovered from.
    source: str = ""
    #: Set when discovery could not complete; explains why the list is empty.
    reason: str = ""

    # -- queries ---------------------------------------------------------
    @property
    def available(self) -> list[VoiceInfo]:
        return [voice for voice in self.voices if voice.available]

    @property
    def count(self) -> int:
        return len(self.voices)

    def genders(self) -> list[str]:
        seen: list[str] = []
        for voice in self.voices:
            if voice.gender not in seen:
                seen.append(voice.gender)
        return [gender for gender in GENDERS if gender in seen]

    def by_language(self, language: str) -> list[VoiceInfo]:
        if not language or language == "all":
            return list(self.voices)
        needle = language.lower()
        return [v for v in self.voices if v.language.lower() == needle
                or v.language.lower().startswith(needle)
                or v.id.lower().startswith(needle)]

    def by_language_and_gender(self, language: str, gender: str) -> list[VoiceInfo]:
        voices = self.by_language(language)
        if not gender or gender == "all":
            return voices
        return [v for v in voices if v.gender == gender]

    def find(self, voice_id: str) -> Optional[VoiceInfo]:
        if not voice_id:
            return None
        needle = voice_id.strip().lower()
        for voice in self.voices:
            if voice.id.lower() == needle:
                return voice
        return None

    def is_available(self, voice_id: str) -> bool:
        voice = self.find(voice_id)
        return bool(voice and voice.available)

    def default_voice(self, language: str = "", gender: str = "") -> Optional[VoiceInfo]:
        """A sensible first choice for the given language/gender."""
        candidates = self.by_language_and_gender(language, gender)
        available = [v for v in candidates if v.available]
        if available:
            return available[0]
        fallback = self.available
        return fallback[0] if fallback else None

    def describe(self) -> str:
        if not self.voices:
            return self.reason or "No voices were discovered."
        where = f" from {self.source}" if self.source else ""
        usable = len(self.available)
        text = f"{len(self.voices)} voices across {len(self.languages)} languages{where}"
        if usable != len(self.voices):
            text += f" — {usable} usable right now"
        return text + "."

    def blocker(self) -> str:
        """Why the listed voices cannot generate yet, if they cannot."""
        for voice in self.voices:
            if not voice.available and voice.note:
                return voice.note
        return ""


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def _read_gender_metadata(directory: Optional[Path]) -> dict[str, str]:
    """Read a published gender table if the engine ships one.

    Returns an empty mapping when there is none, so voices are reported as
    "Not specified" instead of being guessed at.
    """
    if directory is None or not directory.is_dir():
        return {}
    import json

    for name in GENDER_METADATA_NAMES:
        candidate = directory / name
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        table: dict[str, str] = {}
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, dict):
                    gender = str(value.get("gender", "")).strip().lower()
                else:
                    gender = str(value).strip().lower()
                if gender in GENDERS:
                    table[str(key)] = gender
        if table:
            return table
    return {}


def _guess_gender(voice_id: str) -> str:
    """Best-effort gender from the reference id shape; "unknown" when unsure.

    Kokoro's reference identifiers are shaped ``<language><gender>_<name>``, so
    ``hm_ishaan`` is a Hindi male voice and ``af_bella`` an American female one.
    The gender letter is the second character of the prefix.  Anything that does
    not match stays "unknown" rather than being guessed at, and a published
    metadata file always wins over this heuristic.
    """
    identifier = voice_id.strip().lower()
    if len(identifier) >= 2 and identifier[1] in _GENDER_HINTS:
        return _GENDER_HINTS[identifier[1]]
    return "unknown"


def discover_voices(status: Optional[KokoroStatus] = None,
                    model_dir: Optional[Path] = None,
                    extra_dirs: Iterable[Path] = (),
                    favourites: Iterable[str] = ()) -> VoiceCatalogue:
    """Discover the voices the installed Kokoro can actually use.

    Nothing is hard-coded: the list comes from the model directory found by
    :func:`app.tts.capabilities.probe_model`, so adding or removing voice files
    changes the result on the next refresh.
    """
    probe = status or probe_kokoro(model_dir=model_dir, extra_dirs=extra_dirs)
    favourites_set = {name.strip().lower() for name in favourites if name and name.strip()}

    catalogue = VoiceCatalogue(
        languages=list(probe.languages),
        language_source=probe.language_source,
    )
    if not probe.model.present and not probe.voices:
        catalogue.reason = (
            "The Kokoro model weights were not found, so its voice catalogue "
            "cannot be read. Set the model path in Settings and refresh."
        )
        return catalogue

    # Voices are listed even when the engine cannot run yet, marked unavailable
    # with the reason.  Hiding them would leave the user guessing whether their
    # voice files were found at all (directive sections 6 and 33).
    blocker = ""
    if not probe.installed:
        blocker = "The Kokoro package is not installed."
    elif not probe.runtime.available:
        blocker = "No inference runtime is installed (ONNX Runtime or PyTorch)."
    elif not probe.model.present:
        blocker = "The Kokoro model weights were not found."

    voice_files = {entry.stem.lower(): entry for entry in probe.model.voice_files}
    for voice_id in probe.voices:
        entry = voice_files.get(voice_id.lower())
        available = not blocker and (entry is None or entry.is_file())
        if blocker:
            note = blocker
        elif entry is not None and not entry.is_file():
            note = "The voice file is missing from the model folder."
        else:
            note = ""
        catalogue.voices.append(
            VoiceInfo(
                id=voice_id,
                language=_language_for(voice_id, probe.languages),
                gender=_guess_gender(voice_id),
                source="model",
                available=available,
                note=note,
                favorite=voice_id.lower() in favourites_set,
            )
        )

    # Apply any published gender metadata over the heuristic.  The table can sit
    # beside the model weights or inside the voices folder, so check both.
    genders = _read_gender_metadata(
        probe.model.path.parent if probe.model.path else None
    )
    if not genders:
        voice_dirs = {entry.parent for entry in probe.model.voice_files}
        for directory in voice_dirs:
            genders = _read_gender_metadata(directory)
            if genders:
                break
    if genders:
        for voice in catalogue.voices:
            published = genders.get(voice.id) or genders.get(voice.id.lower())
            if published:
                voice.gender = published

    catalogue.source = str(probe.model.path.parent) if probe.model.path else "model folder"
    if not catalogue.voices:
        catalogue.reason = (
            "The model folder was found but contained no voice files. "
            "Copy the Kokoro voice files next to the model weights."
        )
    return catalogue


def _language_for(voice_id: str, languages: list[str]) -> str:
    """Match a voice to the language list the engine reported."""
    lowered = voice_id.lower()
    for language in sorted(languages, key=len, reverse=True):
        if lowered.startswith(language.lower()):
            return language
    if len(lowered) >= 2:
        return lowered[:2]
    return ""


def validate_voice_choice(catalogue: VoiceCatalogue,
                          voice_id: str,
                          language: str = "") -> tuple[bool, str]:
    """Check a stored or user-picked voice is really usable.

    Returns (ok, message).  A voice that is listed but missing is reported as a
    validation error rather than being silently swapped (directive section 42).
    """
    if not voice_id:
        return False, (
            "No narration voice is selected. "
            "Choose a voice from the Voice panel before generating."
        )
    voice = catalogue.find(voice_id)
    if voice is None:
        return False, (
            f"The voice “{voice_id}” is not in the installed Kokoro catalogue. "
            "It may have been removed, or the model folder changed. "
            "Refresh the voice list and choose an available voice."
        )
    if not voice.available:
        return False, (
            f"The voice “{voice_id}” is listed but cannot generate audio. {voice.note}"
        )
    if language and voice.language and not voice.language.lower().startswith(language.lower()):
        return False, (
            f"The voice “{voice_id}” does not support the selected language "
            f"“{language}”. Choose a voice for that language, or change the language."
        )
    return True, ""


def filter_voices(catalogue: VoiceCatalogue,
                  language: str = "",
                  gender: str = "",
                  search: str = "",
                  favourites_only: bool = False) -> list[VoiceInfo]:
    """Apply the browser's filters (section 8)."""
    voices = catalogue.by_language_and_gender(language, gender)
    if favourites_only:
        voices = [voice for voice in voices if voice.favorite]
    needle = (search or "").strip().lower()
    if needle:
        voices = [
            voice for voice in voices
            if needle in voice.id.lower()
            or needle in voice.label.lower()
            or needle in voice.language.lower()
        ]
    return voices


__all__ = [
    "GENDERS",
    "GENDER_LABELS",
    "LANGUAGE_LABELS",
    "VoiceCatalogue",
    "VoiceInfo",
    "discover_voices",
    "filter_voices",
    "language_label",
    "validate_voice_choice",
]
