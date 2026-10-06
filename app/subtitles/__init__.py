"""Subtitles and captions (Stage E).

Importing this package does nothing but expose names - no files are read, no
subprocess is started and nothing is rendered (directive section 84).
"""

from __future__ import annotations

from .service import (
    SubtitleIssue,
    delete_cue,
    edit_cue,
    generate_cues,
    merge_cues,
    split_cue,
    split_sentences,
    timecode_srt,
    timecode_vtt,
    to_ass,
    to_srt,
    to_vtt,
    validate_cues,
    wrap_lines,
    write_subtitle_file,
)

__all__ = [
    "SubtitleIssue",
    "delete_cue",
    "edit_cue",
    "generate_cues",
    "merge_cues",
    "split_cue",
    "split_sentences",
    "timecode_srt",
    "timecode_vtt",
    "to_ass",
    "to_srt",
    "to_vtt",
    "validate_cues",
    "wrap_lines",
    "write_subtitle_file",
]
