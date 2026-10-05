"""Script import and export (directive sections 18-19).

Import reads a file, detects its encoding, and returns the text plus a note
about what was detected.  Export writes the script back out as TXT, Markdown or
the structured format - never losing the fields a user wrote.

Encoding handling matters because scripts are written in Hindi and other
non-Latin scripts: UTF-8 is used internally, a byte-order mark is stripped, and
a file that is not valid text produces a friendly error instead of a crash.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .parser import ParsedScript, parse, plain_to_structured

#: Read limit: a script is text, and a 20 MB "script" is a mistake worth
#: reporting rather than loading.
MAX_IMPORT_BYTES = 20 * 1024 * 1024

#: Encodings tried in order when a file has no byte-order mark.
#: Encodings tried when there is no byte-order mark.  UTF-16/UTF-32 are
#: deliberately absent: without a BOM (handled above) they happily "decode"
#: arbitrary binary into plausible-looking mojibake, which is worse than an
#: error.  Windows editors that write UTF-16 always write the BOM.
FALLBACK_ENCODINGS: tuple[str, ...] = ("utf-8", "cp1252", "latin-1")

#: Whitespace a text script may legitimately contain.
_ALLOWED_CONTROL = {"\t", "\n", "\r", "\f", "\v"}

#: Extensions the import dialog offers.
SUPPORTED_IMPORT_EXTENSIONS: tuple[str, ...] = (".txt", ".md", ".markdown", ".text", ".script")
SUPPORTED_EXPORT_EXTENSIONS: tuple[str, ...] = (".txt", ".md", ".script")


@dataclass
class ImportResult:
    """What an import produced."""

    text: str = ""
    encoding: str = ""
    source: str = ""
    error: str = ""
    #: Human readable notes ("byte-order mark removed", "converted from cp1252").
    notes: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.notes is None:
            self.notes = []

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class ExportResult:
    path: Optional[Path] = None
    bytes_written: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


#: Fraction of "suspicious" characters above which a file is treated as binary.
_BINARY_THRESHOLD = 0.02


def _binary_score(text: str) -> float:
    """How much of *text* looks like binary rather than writing.

    A real script - English, Hindi, mixed, with punctuation - scores 0.  Binary
    data decoded with a permissive codec such as latin-1 is full of control
    characters and unassigned code points, so it scores high.  Scoring a sample
    keeps this cheap even for a 20 MB import.
    """
    import unicodedata

    sample = text[:8192]
    if not sample:
        return 0.0
    suspicious = 0
    for ch in sample:
        if ch in _ALLOWED_CONTROL:
            continue
        code = ord(ch)
        if code < 32 or code == 127 or 0x80 <= code <= 0x9F:
            suspicious += 1
            continue
        # Unassigned, private-use and surrogate code points never appear in a
        # script a person typed, but are common in mis-decoded binary.
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            # Zero-width and directional marks are legitimate in Indic text.
            if code not in (0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0xFEFF):
                suspicious += 1
    return suspicious / len(sample)


def decode_text(data: bytes) -> tuple[str, str, list[str]]:
    """Decode script bytes, returning ``(text, encoding, notes)``.

    Raises ``ValueError`` with a readable message when the bytes are not text.
    """
    notes: list[str] = []
    if not data:
        return "", "utf-8", ["The file was empty."]

    # Byte-order marks name the encoding exactly.
    for bom, encoding in (
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
    ):
        if data.startswith(bom):
            notes.append(f"Byte-order mark detected ({encoding}); it was removed.")
            return data.decode(encoding), encoding, notes

    last_error: Optional[Exception] = None
    for encoding in FALLBACK_ENCODINGS:
        try:
            text = data.decode(encoding)
        except (UnicodeDecodeError, LookupError) as exc:
            last_error = exc
            continue
        if _binary_score(text) > _BINARY_THRESHOLD:
            last_error = ValueError("the file contains binary data")
            continue
        if encoding != "utf-8":
            notes.append(f"The file was not UTF-8; it was read as {encoding}.")
        return text, encoding, notes

    raise ValueError(
        "The file could not be read as text"
        + (f" ({last_error})" if last_error else "")
        + ". Save it as UTF-8 plain text and try again."
    )


def import_script(path: Path) -> ImportResult:
    """Read a script file into text."""
    source = Path(path)
    if not source.exists():
        return ImportResult(source=str(source), error=f"'{source.name}' does not exist.")
    if not source.is_file():
        return ImportResult(source=str(source), error=f"'{source.name}' is a folder, not a file.")

    try:
        size = source.stat().st_size
    except OSError as exc:
        return ImportResult(source=str(source), error=f"The file could not be read: {exc}")
    if size > MAX_IMPORT_BYTES:
        return ImportResult(
            source=str(source),
            error=f"The file is {size / (1024 * 1024):.1f} MB. Scripts should be plain text under 20 MB.",
        )
    if source.suffix.lower() not in SUPPORTED_IMPORT_EXTENSIONS and source.suffix:
        return ImportResult(
            source=str(source),
            error=(
                f"'{source.suffix}' is not a text format this editor reads. "
                f"Supported: {', '.join(SUPPORTED_IMPORT_EXTENSIONS)}."
            ),
        )

    try:
        data = source.read_bytes()
    except OSError as exc:
        return ImportResult(source=str(source), error=f"The file could not be read: {exc}")

    try:
        text, encoding, notes = decode_text(data)
    except ValueError as exc:
        return ImportResult(source=str(source), error=str(exc))

    if not text.strip():
        notes.append("The file contained no text.")
    return ImportResult(text=text, encoding=encoding, source=str(source), notes=notes)


def strip_markdown(text: str) -> str:
    """Remove the Markdown syntax that would be spoken aloud.

    Only the markers are removed - the wording is never changed.  Headings keep
    their text, emphasis keeps its words, and list bullets become plain lines.
    """
    lines: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        # Headings: "# Title" -> "Title"
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
        # Block quotes
        if stripped.startswith(">"):
            stripped = stripped.lstrip(">").strip()
        # List bullets and numbered lists
        stripped = re.sub(r"^\s*[-*+]\s+", "", stripped) if stripped[:1] in "-*+" else stripped
        stripped = re.sub(r"^\s*\d+[.)]\s+", "", stripped)
        # Inline emphasis and code
        stripped = re.sub(r"(\*\*|__)(.*?)\1", r"\2", stripped)
        stripped = re.sub(r"(\*|_)(.*?)\1", r"\2", stripped)
        stripped = stripped.replace("`", "")
        # Links: [text](url) -> text
        stripped = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", stripped)
        # Horizontal rules are not spoken
        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            continue
        lines.append(stripped)
    return "\n".join(lines)


def to_markdown(text: str, title: str = "") -> str:
    """Render a script as Markdown, preserving every structured field."""
    parsed: ParsedScript = parse(text)
    parts: list[str] = []
    if title:
        parts.append(f"# {title}")
        parts.append("")
    if parsed.preamble.strip():
        parts.append(parsed.preamble.strip())
        parts.append("")

    if not parsed.structured:
        parts.append(text.strip())
        return "\n".join(parts).rstrip("\n") + "\n"

    for block in parsed.blocks:
        heading = block.title or (f"Scene {block.number:02d}" if block.number is not None else f"Section {block.index + 1}")
        parts.append(f"## {heading}")
        parts.append("")
        for label, value in (
            ("Narration", block.narration),
            ("On Screen", block.on_screen),
            ("Visual", block.visual),
            ("Music", block.music),
            ("SFX", block.sfx),
            ("Duration", block.duration),
            ("Notes", block.notes),
        ):
            if not str(value).strip():
                continue
            parts.append(f"**{label}:**")
            parts.append(str(value).strip())
            parts.append("")
        for label, value in block.extra.items():
            if not str(value).strip():
                continue
            parts.append(f"**{label}:**")
            parts.append(str(value).strip())
            parts.append("")
    return "\n".join(parts).rstrip("\n") + "\n"


def convert(text: str, target: str, *, markdown_input: bool = False, title: str = "") -> str:
    """Convert script text between ``plain``, ``structured`` and ``markdown``."""
    mode = (target or "plain").lower()
    body = strip_markdown(text) if markdown_input else (text or "")
    if mode == "markdown":
        return to_markdown(body, title=title)
    if mode == "structured":
        return plain_to_structured(body)
    if mode == "plain":
        parsed = parse(body)
        if not parsed.structured:
            return body
        # Plain export keeps the words that are spoken, plus the notes a user
        # wrote, so nothing is silently thrown away.
        parts: list[str] = []
        if parsed.preamble.strip():
            parts.append(parsed.preamble.strip())
        for block in parsed.blocks:
            chunk = [block.narration.strip()] if block.narration.strip() else []
            if block.notes.strip():
                chunk.append(f"[note] {block.notes.strip()}")
            if chunk:
                parts.append("\n".join(chunk))
        return "\n\n".join(parts).rstrip("\n") + "\n"
    raise ValueError(f"Unknown script format '{target}'. Use plain, structured or markdown.")


def export_script(path: Path, text: str, *, target: str = "txt", title: str = "") -> ExportResult:
    """Write a script file.  An existing file is never silently overwritten."""
    destination = Path(path)
    mode = (target or "txt").lower().lstrip(".")
    if mode in ("txt", "text", "plain"):
        content = convert(text, "plain")
    elif mode in ("md", "markdown"):
        content = to_markdown(text, title=title)
    elif mode in ("script", "structured"):
        content = convert(text, "structured")
    else:
        return ExportResult(error=f"Unknown export format '{target}'. Use txt, md or script.")

    if destination.exists():
        return ExportResult(
            path=destination,
            error=f"'{destination.name}' already exists. Choose another name so nothing is replaced.",
        )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        destination.write_bytes(data)
    except OSError as exc:
        return ExportResult(path=destination, error=f"The file could not be written: {exc}")
    return ExportResult(path=destination, bytes_written=len(data))

__all__ = [
    "ExportResult",
    "FALLBACK_ENCODINGS",
    "ImportResult",
    "MAX_IMPORT_BYTES",
    "SUPPORTED_EXPORT_EXTENSIONS",
    "SUPPORTED_IMPORT_EXTENSIONS",
    "convert",
    "decode_text",
    "export_script",
    "import_script",
    "strip_markdown",
    "to_markdown",
]
