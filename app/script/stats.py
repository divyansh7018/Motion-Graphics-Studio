"""Script statistics and duration estimates (directive section 16).

Every number here is an **estimate** except the counts.  The real narration
duration comes from the generated audio and always wins over these numbers -
``ScriptStats.estimated`` is labelled as an estimate wherever it is shown.

Word and sentence counting is Unicode aware: Devanagari (Hindi) uses the danda
``।`` as a sentence end and has no spaces inside a word the way English does, so
the patterns here deliberately avoid anything Latin-only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .parser import ParsedScript, ScriptBlock, parse

#: Words per minute used for the estimate.  Spoken narration sits near 140-160.
DEFAULT_WORDS_PER_MINUTE = 150

#: Sentence enders: Latin punctuation plus the Devanagari danda and double
#: danda, and the CJK full stop.  Followed by whitespace/end or a quote/bracket.
SENTENCE_END = re.compile(r"[.!?।॥。！？…]+[\s\"')\]]*|$")

#: A "word" is any run of non-space characters.  This is deliberately simple: it
#: counts Hindi, English and mixed text the same way, and never claims a
#: linguistic token count it cannot deliver.
WORD = re.compile(r"\S+")

#: Characters that are not counted as "characters" in the UI count: none.  The
#: count is the true length of the text, including spaces, which is what a user
#: expects when they compare with a text editor.


@dataclass(frozen=True)
class ScriptStats:
    """Counts plus a clearly-labelled duration estimate."""

    words: int = 0
    characters: int = 0
    characters_no_spaces: int = 0
    sentences: int = 0
    paragraphs: int = 0
    lines: int = 0
    words_per_minute: int = DEFAULT_WORDS_PER_MINUTE
    estimated_seconds: float = 0.0
    #: Real duration from generated audio, when narration exists.  Authoritative.
    actual_seconds: float = 0.0

    @property
    def has_actual(self) -> bool:
        return self.actual_seconds > 0.0

    def duration_seconds(self) -> float:
        """The best duration available: real audio when there is any."""
        return self.actual_seconds if self.has_actual else self.estimated_seconds

    def duration_label(self) -> str:
        """``2m 14s (estimated)`` or ``32.45s (from audio)``."""
        seconds = self.duration_seconds()
        if seconds <= 0:
            return "not estimated yet"
        text = format_duration(seconds)
        return f"{text} (from audio)" if self.has_actual else f"{text} (estimated)"


def count_sentences(text: str) -> int:
    """Count sentences, treating a line break as a boundary too.

    A numbered list or a short caption should not be counted as one long
    sentence, so a newline that ends a non-empty line also ends a sentence.
    """
    if not text or not text.strip():
        return 0
    total = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Split on sentence enders; count the non-empty pieces.
        pieces = [piece for piece in SENTENCE_END.split(stripped) if piece and piece.strip()]
        total += max(1, len(pieces))
    return total


def count_words(text: str) -> int:
    return len(WORD.findall(text or ""))


def count_paragraphs(text: str) -> int:
    if not text or not text.strip():
        return 0
    return len([block for block in re.split(r"\n\s*\n", text) if block.strip()])


def estimate_seconds(words: int, words_per_minute: int = DEFAULT_WORDS_PER_MINUTE) -> float:
    """Rough spoken duration for *words* at *words_per_minute*."""
    wpm = words_per_minute if words_per_minute > 0 else DEFAULT_WORDS_PER_MINUTE
    return round(words / wpm * 60.0, 2)


def format_duration(seconds: float) -> str:
    """``32.45s``, ``2m 14s``, ``1h 02m 03s`` - compact and unambiguous."""
    if seconds is None or seconds < 0:
        return "0s"
    if seconds < 60:
        return f"{seconds:.2f}s" if seconds < 10 else f"{seconds:.1f}s"
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s"


def stats_for_text(
    text: str,
    words_per_minute: int = DEFAULT_WORDS_PER_MINUTE,
    actual_seconds: float = 0.0,
) -> ScriptStats:
    """Statistics for one block of text (used for a selection preview too)."""
    body = text or ""
    words = count_words(body)
    return ScriptStats(
        words=words,
        characters=len(body),
        characters_no_spaces=len(re.sub(r"\s+", "", body)),
        sentences=count_sentences(body),
        paragraphs=count_paragraphs(body),
        lines=len([line for line in body.splitlines() if line.strip()]),
        words_per_minute=words_per_minute,
        estimated_seconds=estimate_seconds(words, words_per_minute),
        actual_seconds=max(0.0, float(actual_seconds or 0.0)),
    )


def stats_for_script(
    text: str,
    words_per_minute: int = DEFAULT_WORDS_PER_MINUTE,
    actual_seconds: float = 0.0,
) -> ScriptStats:
    """Statistics for a whole script.

    For a structured script the narration is what gets spoken, so the word count
    counts narration lines - on-screen text and visual directions are not read
    aloud and must not inflate the estimate.
    """
    parsed: ParsedScript = parse(text)
    if parsed.structured:
        spoken = parsed.narration_text()
    else:
        spoken = text or ""
    stats = stats_for_text(spoken, words_per_minute=words_per_minute, actual_seconds=actual_seconds)
    # Report the document size, not just the spoken part, for the character count.
    document = text or ""
    return ScriptStats(
        words=stats.words,
        characters=len(document),
        characters_no_spaces=len(re.sub(r"\s+", "", document)),
        sentences=count_sentences(spoken),
        paragraphs=count_paragraphs(document),
        lines=len([line for line in document.splitlines() if line.strip()]),
        words_per_minute=stats.words_per_minute,
        estimated_seconds=stats.estimated_seconds,
        actual_seconds=stats.actual_seconds,
    )


def block_stats(
    block: ScriptBlock,
    words_per_minute: int = DEFAULT_WORDS_PER_MINUTE,
    actual_seconds: float = 0.0,
) -> ScriptStats:
    """Statistics for one scene/section."""
    return stats_for_text(block.narration, words_per_minute=words_per_minute, actual_seconds=actual_seconds)


__all__ = [
    "DEFAULT_WORDS_PER_MINUTE",
    "ScriptStats",
    "block_stats",
    "count_paragraphs",
    "count_sentences",
    "count_words",
    "estimate_seconds",
    "format_duration",
    "stats_for_script",
    "stats_for_text",
]
