"""Text preprocessing before it reaches the TTS engine (directive section 20).

The rule this module follows: **clean the delivery, never the wording.**

Whitespace, line endings and repeated spaces are normalised because they change
nothing about what is said.  Numbers, abbreviations and punctuation are left
alone unless the user turns a rule on, because changing "Rs. 500" into "Rupees
five hundred" is a rewrite of the user's script.

Every function returns what it changed, so the interface can show it rather than
doing it behind the user's back.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

#: Paragraph separator used to place a natural pause between blocks.
PARAGRAPH_BREAK = "\n\n"

#: A soft pause marker understood by :mod:`app.tts.engine` (section 21).
#: Two newlines become one pause; we never insert pauses inside a sentence.
PAUSE_SENTINEL = "\n"


@dataclass
class PreprocessOptions:
    """What the user has allowed the preprocessor to do.

    Defaults are deliberately conservative: the only changes made are ones that
    cannot alter the words that get spoken.
    """

    #: Collapse runs of spaces/tabs into one space.
    collapse_spaces: bool = True
    #: Turn CRLF/CR into LF and trim trailing whitespace per line.
    normalize_newlines: bool = True
    #: Collapse three or more newlines into a paragraph break.
    normalize_paragraphs: bool = True
    #: Replace curly quotes/dashes with their plain equivalents (helps some
    #: phonemisers; does not change wording).
    normalize_typography: bool = True
    #: Strip Markdown syntax that would otherwise be spoken ("**", "#", "[]()").
    strip_markdown: bool = False
    #: Expand common abbreviations, currency symbols and whole numbers into
    #: words.  **Off by default**: this rewrites the user's text, so it must be
    #: an explicit choice.  Decimals and numbers inside larger tokens (dates,
    #: version numbers, phone numbers) are always left as written.
    expand_numbers: bool = False
    #: Grouping used when numbers are written out: "western" (thousand, million,
    #: billion) or "indian" (thousand, lakh, crore).
    number_style: str = "western"
    #: Remove stage directions in square brackets, e.g. ``[pause]``.
    strip_bracket_notes: bool = False
    #: Insert a short pause at paragraph boundaries (never inside a sentence).
    paragraph_pauses: bool = True
    #: Maximum characters handed to the engine in one call (0 = no limit).
    max_chunk_chars: int = 0


@dataclass
class PreprocessResult:
    """The cleaned text plus a record of what changed."""

    text: str = ""
    #: Human readable list of the changes that were applied.
    changes: list[str] = field(default_factory=list)
    #: True when nothing at all was altered.
    @property
    def unchanged(self) -> bool:
        return not self.changes

    def describe(self) -> str:
        if self.unchanged:
            return "No changes were needed."
        return "; ".join(self.changes)


# --------------------------------------------------------------------------
# Individual rules
# --------------------------------------------------------------------------

TYPOGRAPHY_MAP = {
    "\u2018": "'",   # left single quote
    "\u2019": "'",   # right single quote / apostrophe
    "\u201c": '"',   # left double quote
    "\u201d": '"',   # right double quote
    "\u2013": "-",   # en dash
    "\u2014": " - ",  # em dash reads as a pause
    "\u2026": "...",  # ellipsis
    "\u00a0": " ",   # non-breaking space
    "\u200b": "",    # zero-width space
    "\ufeff": "",    # zero-width no-break space
}

#: Abbreviations expanded only when the user opts in.  Kept small and honest:
#: anything not listed here is left exactly as written.
ABBREVIATIONS: dict[str, str] = {
    "rs.": "rupees",
    "rs": "rupees",
    "dr.": "doctor",
    "mr.": "mister",
    "mrs.": "misses",
    "ms.": "miss",
    "st.": "saint",
    "etc.": "etcetera",
    "e.g.": "for example",
    "i.e.": "that is",
    "vs.": "versus",
    "approx.": "approximately",
    "no.": "number",
}

#: Currency symbols, expanded only with ``expand_numbers`` on.  The symbol is
#: replaced by its spoken name; the amount is written out separately.
CURRENCY_SYMBOLS: dict[str, str] = {
    "\u20b9": "rupees",
    "$": "dollars",
    "\u20ac": "euros",
    "\u00a3": "pounds",
}

_ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen"
).split()
_TENS = (
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
    "eighty", "ninety",
)

#: Spoken scale words, Western grouping (thousand / million / billion).
SCALE_WESTERN = ((1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand"))
#: Spoken scale words, Indian grouping (thousand / lakh / crore).
SCALE_INDIAN = ((10_000_000, "crore"), (100_000, "lakh"), (1_000, "thousand"))


def _under_thousand(value: int) -> str:
    if value < 20:
        return _ONES[value]
    if value < 100:
        tens, ones = divmod(value, 10)
        return f"{_TENS[tens]}" + (f" {_ONES[ones]}" if ones else "")
    hundreds, rest = divmod(value, 100)
    return f"{_ONES[hundreds]} hundred" + (f" {_under_thousand(rest)}" if rest else "")


def integer_to_words(value: int, *, style: str = "western") -> str:
    """Write an integer out in words using Western or Indian grouping.

    Handles 0 to just under a trillion.  Negative numbers are prefixed with
    "minus".  This helper exists only for the opt-in expansion rule; the default
    preprocessing never touches numbers.
    """
    if value < 0:
        return f"minus {integer_to_words(-value, style=style)}"
    if value == 0:
        return "zero"
    scale = SCALE_INDIAN if style == "indian" else SCALE_WESTERN
    remaining = value
    parts: list[str] = []
    for size, name in scale:
        if remaining >= size:
            count, remaining = divmod(remaining, size)
            parts.append(f"{integer_to_words(count, style=style)} {name}")
    if remaining:
        parts.append(_under_thousand(remaining))
    return " ".join(parts)


def _expand_numbers(text: str, *, style: str = "western") -> str:
    """Write out abbreviations, currency and whole numbers (opt-in only).

    Rules, in order:

    1. Abbreviations match on word boundaries, so "interest." is never mistaken
       for the abbreviation "st.", and the original capitalisation is kept
       ("Dr." becomes "Doctor").
    2. Grouped numbers are converted first: "12,500" in Western style and
       "1,00,000" in Indian style.
    3. Percentages become "<words> percent".
    4. Remaining standalone whole numbers are written out.

    Decimals, version numbers, ratios, times, dates and numbers inside longer
    tokens are deliberately left exactly as written: guessing their spoken form
    would produce wrong audio.
    """
    body = text

    def _abbreviation(match: re.Match[str]) -> str:
        source = match.group(0)
        replacement = ABBREVIATIONS[source.lower()]
        if source[:1].isupper():
            return replacement[:1].upper() + replacement[1:]
        return replacement

    for source in ABBREVIATIONS:
        pattern = r"(?<![\w.])" + re.escape(source) + r"(?![\w])"
        body = re.sub(pattern, _abbreviation, body, flags=re.IGNORECASE)

    for symbol, name in CURRENCY_SYMBOLS.items():
        body = body.replace(symbol, f" {name} ")

    # 2. grouped numbers
    if style == "indian":
        grouped = r"(?<![\d.,\-])(\d{1,2}(?:,\d{2})*,\d{3})(?![\d.,\-])"
    else:
        grouped = r"(?<![\d.,\-])(\d{1,3}(?:,\d{3})+)(?![\d.,\-])"
    body = re.sub(grouped,
                  lambda m: integer_to_words(int(m.group(1).replace(",", "")), style=style),
                  body)

    # 3. percentages
    body = re.sub(r"(?<![\w.,\-])(\d{1,12})\s*%(?![\w])",
                  lambda m: f"{integer_to_words(int(m.group(1)), style=style)} percent",
                  body)

    # 4. plain whole numbers (a trailing "." ends the sentence, not a decimal)
    body = re.sub(r"(?<![\w.,\-/:])\d{1,12}(?![\w\-/])(?!\.\d)(?!,\d)(?!:\d)",
                  lambda m: integer_to_words(int(m.group(0)), style=style),
                  body)

    return re.sub(r"[ ]{2,}", " ", body)


def _normalize_newlines(text: str) -> str:
    """Convert CRLF and lone CR to LF so Windows and Unix scripts behave alike."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _trim_lines(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.split("\n"))


def _collapse_spaces(text: str) -> str:
    # Only horizontal whitespace: newlines carry the paragraph structure.
    return re.sub(r"[ \t\u00a0]{2,}", " ", text)


def _normalize_paragraphs(text: str) -> str:
    return re.sub(r"\n{3,}", PARAGRAPH_BREAK, text)


def _normalize_typography(text: str) -> str:
    """Replace curly quotes, dashes and invisible spaces with plain equivalents.

    This changes no words: it only removes characters some phonemisers choke on.
    """
    for source, replacement in TYPOGRAPHY_MAP.items():
        text = text.replace(source, replacement)
    return text


def _strip_markdown(text: str) -> str:
    """Remove Markdown syntax that would otherwise be spoken aloud."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)          # links -> text
    text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text)                # bold
    text = re.sub(r"(?<![\w*])(\*)(?!\*)(.*?)(?<!\*)(\*)(?![\w*])", r"\2", text)  # italics
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)        # headings
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)             # block quotes
    text = text.replace("`", "")
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)     # bullets
    return text


def _strip_bracket_notes(text: str) -> str:
    return re.sub(r"\[[^\]\n]{0,120}\]", " ", text)


def _apply_pauses(text: str) -> str:
    """Keep paragraph breaks as pause markers; drop stray single newlines.

    Kokoro's phonemiser handles newlines inconsistently, so a paragraph becomes
    a single sentinel newline (a real pause) and a line break inside a paragraph
    becomes a space.  No pause is ever inserted mid-sentence.
    """
    blocks = [block.strip() for block in text.split(PARAGRAPH_BREAK) if block.strip()]
    cleaned = [re.sub(r"\s*\n\s*", " ", block).strip() for block in blocks]
    return PAUSE_SENTINEL.join(cleaned)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def preprocess(text: str, options: Optional[PreprocessOptions] = None) -> PreprocessResult:
    """Clean *text* for speech according to *options* (defaults are safe).

    Returns the cleaned text and a list of what changed, so the interface can
    tell the user exactly what happened instead of doing it quietly.
    """
    settings = options or PreprocessOptions()
    body = text or ""
    original = body
    changes: list[str] = []

    # Unicode normalisation keeps Devanagari and other composed scripts stable
    # without changing what is written.  NFC is what editors and keyboards use.
    normalised = unicodedata.normalize("NFC", body)
    if normalised != body:
        changes.append("Unicode was normalised (NFC); the wording is unchanged.")
        body = normalised

    if settings.normalize_newlines:
        cleaned = _trim_lines(_normalize_newlines(body))
        if cleaned != body:
            changes.append("Line endings were normalised and trailing spaces removed.")
            body = cleaned

    if settings.normalize_typography:
        cleaned = _normalize_typography(body)
        if cleaned != body:
            changes.append("Curly quotes and dashes were replaced with plain ones.")
            body = cleaned

    if settings.collapse_spaces:
        cleaned = _collapse_spaces(body)
        if cleaned != body:
            changes.append("Repeated spaces were collapsed to one.")
            body = cleaned

    if settings.strip_markdown:
        cleaned = _strip_markdown(body)
        if cleaned != body:
            changes.append("Markdown syntax was removed so it is not spoken aloud.")
            body = cleaned

    if settings.strip_bracket_notes:
        cleaned = _strip_bracket_notes(body)
        if cleaned != body:
            changes.append("Bracketed notes such as [pause] were removed.")
            body = cleaned

    if settings.expand_numbers:
        cleaned = _expand_numbers(body, style=settings.number_style)
        if cleaned != body:
            style = "Indian (lakh/crore)" if settings.number_style == "indian" else "Western"
            changes.append(f"Abbreviations and whole numbers were written out as words ({style} grouping).")
            body = cleaned

    if settings.paragraph_pauses:
        cleaned = _apply_pauses(body)
        if cleaned != body:
            changes.append("Paragraph breaks were kept as pauses; line breaks became spaces.")
            body = cleaned

    if settings.max_chunk_chars and settings.max_chunk_chars > 0 and len(body) > settings.max_chunk_chars:
        changes.append(
            f"The text is longer than {settings.max_chunk_chars} characters and will be generated in parts."
        )

    body = body.strip()
    if body != original.strip():
        if not changes:
            changes.append("Leading and trailing whitespace was removed.")
    return PreprocessResult(text=body, changes=changes)


def chunk_text(text: str, max_chars: int, *, keep_pauses: bool = True) -> list[str]:
    """Split text for generation without cutting a sentence in half.

    Splits on paragraph pauses first, then on sentence ends, and only falls back
    to a hard cut when a single sentence is longer than the limit.
    """
    limit = int(max_chars or 0)
    body = (text or "").strip()
    if not body:
        return []
    if limit <= 0 or len(body) <= limit:
        return [body]

    separator = PAUSE_SENTINEL if keep_pauses else "\n"
    units: list[str] = []
    for block in body.split(separator):
        block = block.strip()
        if not block:
            continue
        if len(block) <= limit:
            units.append(block)
            continue
        # Split long blocks on sentence ends.
        pieces = re.split(r"(?<=[.!?।॥])\s+", block)
        for piece in pieces:
            piece = piece.strip()
            while len(piece) > limit:
                units.append(piece[:limit].rstrip())
                piece = piece[limit:].lstrip()
            if piece:
                units.append(piece)

    # Pack the units back together up to the limit so the engine gets as few
    # calls as possible (section 51: avoid reloading work per sentence).
    packed: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current}{separator}{unit}" if current else unit
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                packed.append(current)
            current = unit
    if current:
        packed.append(current)
    return packed


__all__ = [
    "ABBREVIATIONS",
    "CURRENCY_SYMBOLS",
    "PARAGRAPH_BREAK",
    "PAUSE_SENTINEL",
    "PreprocessOptions",
    "PreprocessResult",
    "chunk_text",
    "integer_to_words",
    "preprocess",
]
