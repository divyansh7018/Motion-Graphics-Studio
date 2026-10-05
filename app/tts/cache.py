"""Narration cache keys and staleness detection (directive sections 27, 42).

A narration file may only be reused when **everything** that affects the audio is
unchanged: the text, the voice, the language, the speed, the volume, the model
version and the preprocessing configuration.  Change any one of them and the old
audio is stale - it must never be presented as current.

The keys are content hashes, so they survive a restart and do not depend on file
timestamps (which a copy, a restore or a timezone change would silently alter).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

#: Bumped when the way audio is produced changes in a way the inputs do not
#: capture (for example a different chunking strategy).  Changing this invalidates
#: every cached narration, which is the safe direction.
PIPELINE_VERSION = "1"


def _stable_hash(payload: Any) -> str:
    """A short, stable hash of JSON-serialisable data."""
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def source_hash(text: str) -> str:
    """Hash of the text that was spoken.

    Unicode is normalised first so text that looks identical but differs only in
    composition form is treated as the same script.
    """
    import unicodedata

    body = unicodedata.normalize("NFC", text or "")
    return _stable_hash({"text": body})


def settings_hash(*,
                  voice: str,
                  language: str,
                  speed: float,
                  volume: float,
                  model_version: str = "",
                  preprocessing: Optional[dict] = None,
                  sample_rate: int = 0) -> str:
    """Hash of everything except the text that changes the resulting audio."""
    return _stable_hash({
        "voice": voice or "",
        "language": language or "",
        "speed": round(float(speed or 1.0), 4),
        "volume": round(float(volume or 1.0), 4),
        "model": model_version or "",
        "preprocessing": preprocessing or {},
        "sample_rate": int(sample_rate or 0),
        "pipeline": PIPELINE_VERSION,
    })


def cache_key(*,
              text: str,
              voice: str,
              language: str,
              speed: float,
              volume: float,
              model_version: str = "",
              preprocessing: Optional[dict] = None,
              sample_rate: int = 0) -> str:
    """The full cache key: text plus every setting that affects the audio."""
    return _stable_hash({
        "source": source_hash(text),
        "settings": settings_hash(
            voice=voice, language=language, speed=speed, volume=volume,
            model_version=model_version, preprocessing=preprocessing,
            sample_rate=sample_rate,
        ),
    })


@dataclass
class StalenessReport:
    """Why a narration track is or is not still current."""

    stale: bool = False
    #: What changed, in plain words (shown to the user, section 42).
    reasons: list[str] = field(default_factory=list)
    #: The hashes that were compared, for the technical detail pane.
    current_source: str = ""
    stored_source: str = ""
    current_settings: str = ""
    stored_settings: str = ""

    def describe(self) -> str:
        if not self.stale:
            return "The narration still matches the script and voice settings."
        return "The narration is out of date: " + "; ".join(self.reasons) + "."


def compare(*,
            text: str,
            stored_source: str,
            voice: str,
            language: str,
            speed: float,
            volume: float,
            model_version: str = "",
            preprocessing: Optional[dict] = None,
            sample_rate: int = 0,
            stored_settings: str = "") -> StalenessReport:
    """Compare a generated track against the current script and settings."""
    report = StalenessReport(
        current_source=source_hash(text),
        stored_source=stored_source or "",
        current_settings=settings_hash(
            voice=voice, language=language, speed=speed, volume=volume,
            model_version=model_version, preprocessing=preprocessing,
            sample_rate=sample_rate,
        ),
        stored_settings=stored_settings or "",
    )
    if not report.stored_source and not report.stored_settings:
        report.stale = True
        report.reasons.append("no recorded generation details")
        return report
    if report.current_source != report.stored_source:
        report.reasons.append("the script text changed")
    if report.current_settings != report.stored_settings:
        report.reasons.append("the voice, language, speed, volume or preprocessing changed")
    report.stale = bool(report.reasons)
    return report


def preprocessing_options_dict(options: Any) -> dict:
    """Serialise :class:`app.tts.preprocess.PreprocessOptions` for hashing.

    Kept here so the cache does not import the preprocessor at module level.
    """
    if options is None:
        return {}
    if isinstance(options, dict):
        return {key: value for key, value in sorted(options.items())}
    from dataclasses import asdict, is_dataclass

    if is_dataclass(options):
        return {key: value for key, value in sorted(asdict(options).items())}
    return {"value": str(options)}


def fingerprint_files(paths: Iterable[Any]) -> str:
    """A hash over the narration file paths, used to spot a deleted file set."""
    return _stable_hash(sorted(str(path) for path in paths))


__all__ = [
    "PIPELINE_VERSION",
    "StalenessReport",
    "cache_key",
    "compare",
    "fingerprint_files",
    "preprocessing_options_dict",
    "settings_hash",
    "source_hash",
]
