"""The script layer: parsing, statistics and import/export.

Pure logic with no Qt and no I/O side effects at import time, so it can be used
from the interface, the command line, a background job or a test.
"""

from .parser import (
    DURATION_AUTO,
    ParsedScript,
    ScriptBlock,
    block_to_text,
    is_structured,
    parse,
    plain_to_structured,
    to_text,
)
from .stats import (
    DEFAULT_WORDS_PER_MINUTE,
    ScriptStats,
    block_stats,
    count_paragraphs,
    count_sentences,
    count_words,
    estimate_seconds,
    format_duration,
    stats_for_script,
    stats_for_text,
)

__all__ = [
    "DEFAULT_WORDS_PER_MINUTE",
    "DURATION_AUTO",
    "ParsedScript",
    "ScriptBlock",
    "ScriptStats",
    "block_stats",
    "block_to_text",
    "count_paragraphs",
    "count_sentences",
    "count_words",
    "estimate_seconds",
    "format_duration",
    "is_structured",
    "parse",
    "plain_to_structured",
    "stats_for_script",
    "stats_for_text",
    "to_text",
]
