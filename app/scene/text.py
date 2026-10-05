"""Font discovery, measurement and responsive text fitting (Stage D).

Three jobs live here, and they are deliberately separate:

1. **Discovery** (:class:`FontResolver`) - find a real font file on this
   machine.  Windows, macOS and Linux are all searched; when nothing matches,
   Pillow's bundled scalable font is used and the substitution is *reported*,
   never hidden.
2. **Measurement** (:func:`measure_text`) - how big a string actually is in the
   font that was resolved.  Real metrics, never a characters-per-line guess.
3. **Fitting** (:func:`fit_text`) - choose the largest font size, within the
   caller's limits, that still fits the box.  This is what makes one scene read
   correctly in 16:9, 9:16 and 1:1 without any hard-coded coordinates.

Text is never rewritten here.  Fitting changes the *size*, not the wording.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from PIL import ImageFont

__all__ = [
    "FONT_EXTENSIONS",
    "FONT_SEARCH_DIRS",
    "DEFAULT_FONT_FAMILIES",
    "MIN_FONT_PIXELS",
    "MAX_FONT_PIXELS",
    "FontMatch",
    "FontResolver",
    "TextMetrics",
    "measure_text",
    "wrap_text",
    "split_words",
    "FitOptions",
    "FittedText",
    "fit_text",
    "missing_glyphs",
    "default_resolver",
]

FONT_EXTENSIONS: tuple[str, ...] = (".ttf", ".otf", ".ttc", ".otc")

#: Directories searched for fonts, in order.  Windows first (the shipping
#: target) but every platform is covered so development is not a special case.
FONT_SEARCH_DIRS: tuple[str, ...] = (
    os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Fonts"),
    "/System/Library/Fonts",
    "/Library/Fonts",
    os.path.expanduser("~/Library/Fonts"),
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    os.path.expanduser("~/.fonts"),
    os.path.expanduser("~/.local/share/fonts"),
)

#: Preferred families when the project does not name one, or names one that is
#: not installed.  Order matters: the first match wins.
DEFAULT_FONT_FAMILIES: tuple[str, ...] = (
    "Segoe UI",
    "Arial",
    "Helvetica Neue",
    "Noto Sans",
    "DejaVu Sans",
    "Liberation Sans",
    "sans-serif",
)

#: Absolute guard rails for a rendered glyph size.  Below 4px text is
#: unreadable in a video; above 4096px a single glyph can exhaust memory.
MIN_FONT_PIXELS = 4
MAX_FONT_PIXELS = 4096

_WORD_SPLIT = re.compile(r"(\s+)")


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

@dataclass
class FontMatch:
    """One font file found on disk."""

    path: Path
    family: str
    bold: bool = False
    italic: bool = False
    index: int = 0

    @property
    def name(self) -> str:
        return self.path.name

    def describe(self) -> str:
        style = []
        if self.bold:
            style.append("bold")
        if self.italic:
            style.append("italic")
        suffix = f" ({', '.join(style)})" if style else ""
        return f"{self.family}{suffix}"


def _family_from_filename(path: Path) -> tuple[str, bool, bool]:
    """Guess a family name and style from a file name.

    Reading a font's internal name table needs fontTools, which is not a
    dependency of this project.  File names on every major platform carry the
    family and style, so this is reliable enough for selection and it is
    honest about being a guess.
    """
    stem = path.stem
    lowered = stem.lower()
    bold = bool(re.search(r"(?:^|[-_ ])(?:bold|black|heavy|semibold|demi)(?:[-_ ]|$)", lowered))
    italic = bool(re.search(r"(?:^|[-_ ])(?:italic|oblique)(?:[-_ ]|$)", lowered))
    cleaned = re.split(r"[-_ ](?:bold|black|heavy|semibold|demi|italic|oblique|regular|medium|light|thin|book|condensed)",
                       stem, maxsplit=1, flags=re.IGNORECASE)[0]
    cleaned = cleaned.replace("-", " ").replace("_", " ").strip()
    return (cleaned or stem), bold, italic


class FontResolver:
    """Finds font files and hands back loaded Pillow fonts.

    The catalogue is scanned lazily and cached, so importing this module has no
    side effects (directive section 56) and a machine with thousands of font
    files is scanned at most once per process.
    """

    def __init__(self, search_dirs: Optional[Iterable[str]] = None, *, allow_scan: bool = True) -> None:
        self._search_dirs: tuple[str, ...] = tuple(search_dirs) if search_dirs is not None else FONT_SEARCH_DIRS
        self._allow_scan = allow_scan
        self._matches: Optional[list[FontMatch]] = None
        self._loaded: dict[tuple[str, int, bool, bool], Any] = {}
        self._scanned_dirs: list[str] = []
        self._scan_errors: list[str] = []

    # -- catalogue -------------------------------------------------------

    @property
    def matches(self) -> list[FontMatch]:
        if self._matches is None:
            self._matches = self._scan()
        return self._matches

    @property
    def is_scanned(self) -> bool:
        """True once the font folders have been walked (lazily, at most once)."""
        return self._matches is not None

    @property
    def scanned_dirs(self) -> list[str]:
        return list(self._scanned_dirs)

    @property
    def scan_errors(self) -> list[str]:
        return list(self._scan_errors)

    def families(self) -> list[str]:
        """Sorted unique family names available on this machine."""
        return sorted({match.family for match in self.matches}, key=str.lower)

    def find(self, family: str = "", *, bold: bool = False, italic: bool = False) -> Optional[FontMatch]:
        """Best match for a family name, tolerating case and punctuation.

        Returns ``None`` when a *specific* family was requested and nothing on
        this machine matches it.  Guessing a different font here would let the
        caller believe the user's choice was honoured, so the caller decides
        what to fall back to and says so out loud.
        """
        wanted = _normalise_family(family)
        candidates = self.matches
        if not candidates:
            return None

        if not wanted:
            return self._best(candidates, bold, italic)

        exact = [m for m in candidates if _normalise_family(m.family) == wanted]
        if exact:
            return self._best(exact, bold, italic)
        partial = [m for m in candidates if wanted in _normalise_family(m.family)]
        if partial:
            return self._best(partial, bold, italic)
        return None

    @staticmethod
    def _best(candidates: Sequence[FontMatch], bold: bool, italic: bool) -> FontMatch:
        def score(match: FontMatch) -> tuple[int, int, int]:
            # Prefer the requested style, then the shortest (least decorated) name.
            style_penalty = (0 if match.bold == bold else 1) + (0 if match.italic == italic else 1)
            return (style_penalty, len(match.family), match.index)

        return sorted(candidates, key=score)[0]

    def _scan(self) -> list[FontMatch]:
        found: list[FontMatch] = []
        self._scanned_dirs = []
        self._scan_errors = []
        if not self._allow_scan:
            return found
        for raw in self._search_dirs:
            root = Path(raw)
            if not root.exists():
                continue
            self._scanned_dirs.append(str(root))
            try:
                walker: Iterable[Path] = root.rglob("*") if root.is_dir() else [root]
                for path in walker:
                    try:
                        if not path.is_file() or path.suffix.lower() not in FONT_EXTENSIONS:
                            continue
                        family, bold, italic = _family_from_filename(path)
                        found.append(FontMatch(path=path, family=family, bold=bold, italic=italic))
                    except OSError as exc:  # unreadable entry - skip it
                        self._scan_errors.append(f"{path}: {exc}")
            except OSError as exc:
                self._scan_errors.append(f"{root}: {exc}")
        # Stable order so the same machine always resolves the same font.
        found.sort(key=lambda m: (m.family.lower(), m.bold, m.italic, str(m.path).lower()))
        return found

    # -- loading ---------------------------------------------------------

    def load(self, family: str = "", *, size: int = 32, bold: bool = False,
             italic: bool = False) -> tuple[Any, FontMatch | None, str]:
        """Return ``(font, match, note)``.

        ``note`` is non-empty whenever something other than the requested font
        is being used, so the caller can tell the user instead of quietly
        drawing with a different typeface.
        """
        size = max(MIN_FONT_PIXELS, min(MAX_FONT_PIXELS, int(size)))
        requested = (family or "").strip()

        match = self.find(requested, bold=bold, italic=italic)
        note = ""
        if match is None and requested:
            # Not installed: try the well-known families before giving up.
            for candidate_name in DEFAULT_FONT_FAMILIES:
                candidate = self.find(candidate_name, bold=bold, italic=italic)
                if candidate is not None:
                    match = candidate
                    note = (f"The font '{requested}' is not installed on this PC, so "
                            f"'{candidate.describe()}' is being used instead.")
                    break

        if match is None:
            font = self._builtin(size)
            subject = requested or "any installed font"
            note = (f"No usable font file was found for '{subject}'. Pillow's bundled font is being used, "
                    "so this preview will not match the final video.")
            return font, None, note

        key = (str(match.path), size, bold, italic)
        cached = self._loaded.get(key)
        if cached is not None:
            return cached, match, note
        try:
            font = ImageFont.truetype(str(match.path), size, index=match.index)
        except (OSError, ValueError) as exc:
            note = f"The font file '{match.path.name}' could not be opened ({exc}). Using the bundled font."
            return self._builtin(size), match, note
        self._loaded[key] = font
        return font, match, note

    @staticmethod
    def _builtin(size: int) -> Any:
        """Pillow's bundled scalable font - always present, no file needed."""
        try:
            return ImageFont.load_default(size=size)
        except TypeError:  # very old Pillow without a scalable default
            return ImageFont.load_default()

    def resolve_fallback_chain(self, family: str = "") -> list[str]:
        """The families this resolver would try, in order."""
        requested = (family or "").strip()
        chain = [requested] if requested else []
        for candidate in DEFAULT_FONT_FAMILIES:
            if candidate not in chain:
                chain.append(candidate)
        return chain


def _normalise_family(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


_default_resolver: Optional[FontResolver] = None


def default_resolver(*, reset: bool = False) -> FontResolver:
    """The process-wide resolver, created on first use.

    Created lazily so importing this module never walks the font folders
    (directive section 56).  Pass ``reset=True`` after a font is installed to
    pick it up without restarting.
    """
    global _default_resolver
    if _default_resolver is None or reset:
        _default_resolver = FontResolver()
    return _default_resolver


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------

@dataclass
class TextMetrics:
    """Real pixel metrics for one string in one font."""

    width: float = 0.0
    height: float = 0.0
    ascent: int = 0
    descent: int = 0
    line_spacing: float = 0.0

    @property
    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0


def _font_line_metrics(font: Any) -> tuple[int, int]:
    ascent, descent = font.getmetrics()
    return int(ascent), int(descent)


def measure_text(font: Any, text: str, *, line_spacing: float = 1.2) -> TextMetrics:
    """Measure *text* including newlines, using the font's real metrics."""
    content = text or ""
    ascent, descent = _font_line_metrics(font)
    line_height = max(1.0, (ascent + descent) * max(0.5, float(line_spacing)))
    lines = content.split("\n")
    widest = 0.0
    for line in lines:
        if not line:
            continue
        try:
            box = font.getbbox(line)
        except (OSError, ValueError):
            continue
        widest = max(widest, float(box[2] - box[0]))
    return TextMetrics(
        width=widest,
        height=line_height * max(1, len(lines)),
        ascent=ascent,
        descent=descent,
        line_spacing=line_height,
    )


def split_words(text: str) -> list[str]:
    """Split into words keeping the whitespace, so re-joining is lossless.

    Whitespace is attached to the word that precedes it.  This keeps wrapping
    reversible: the original text can always be reconstructed, which matters
    because the engine must never rewrite the user's wording.
    """
    content = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    parts: list[str] = []
    for chunk in _WORD_SPLIT.split(content):
        if not chunk:
            continue
        parts.append(chunk)
    # Merge each whitespace run into the token before it.
    merged: list[str] = []
    for part in parts:
        if part.strip() == "" and merged:
            merged[-1] += part
        else:
            merged.append(part)
    return merged


def wrap_text(font: Any, text: str, max_width: float, *, line_spacing: float = 1.2,
              hard_break: bool = True) -> list[str]:
    """Greedy word wrap to a pixel width.

    Long words with no break point are broken mid-word when ``hard_break`` is
    set (the default), because an overflowing word is worse than a hyphen-less
    break in a video frame.
    """
    if max_width <= 0:
        return [text or ""]
    content = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for paragraph in content.split("\n"):
        current = ""
        for token in split_words(paragraph):
            candidate = current + token if current else token
            width = _text_width(font, candidate.rstrip())
            if width <= max_width or not current:
                # First word on a line always goes on, even if too wide; then we
                # break it so it cannot overflow the frame.
                if not current and hard_break:
                    pieces = _break_long_word(font, token, max_width)
                    if len(pieces) > 1:
                        for piece in pieces[:-1]:
                            lines.append(piece)
                        current = pieces[-1]
                        continue
                current = candidate
            else:
                lines.append(current.rstrip())
                current = token.lstrip()
        lines.append(current.rstrip())
    return lines


def _text_width(font: Any, text: str) -> float:
    if not text:
        return 0.0
    try:
        box = font.getbbox(text)
    except (OSError, ValueError):
        return 0.0
    return float(box[2] - box[0])


def _break_long_word(font: Any, word: str, max_width: float) -> list[str]:
    """Split a single over-long token so no piece exceeds ``max_width``."""
    stripped = word.rstrip()
    trailing = word[len(stripped):]
    if _text_width(font, stripped) <= max_width:
        return [word]
    pieces: list[str] = []
    current = ""
    for char in stripped:
        if current and _text_width(font, current + char) > max_width:
            pieces.append(current)
            current = char
        else:
            current += char
    if current:
        pieces.append(current)
    if pieces and trailing:
        pieces[-1] += trailing
    return pieces


# --------------------------------------------------------------------------
# Fitting
# --------------------------------------------------------------------------

@dataclass
class FitOptions:
    """Limits for one fit.  All values are explicit; nothing is global."""

    #: Largest font size to try, in pixels.
    max_size: float = 64.0
    #: Smallest font size allowed.  Fitting stops shrinking here and reports
    #: overflow instead of producing unreadable text.
    min_size: float = 8.0
    max_lines: int = 0  # 0 = unlimited
    line_spacing: float = 1.2
    #: Allow the text to grow to fill the box (titles), or only shrink (body).
    allow_grow: bool = False
    #: Add "..." when text had to be cut.
    ellipsis: bool = True
    #: Break words that are longer than the box.
    hard_break: bool = True
    #: Stop the binary search once the size is within this many pixels.
    tolerance: float = 0.5

    def clamp_size(self, size: float) -> float:
        return max(MIN_FONT_PIXELS, min(MAX_FONT_PIXELS, size))


@dataclass
class FittedText:
    """The result of a fit: what to draw and whether it truly fitted."""

    lines: list[str] = field(default_factory=list)
    font_size: float = 0.0
    #: ``1.0`` means the requested size was used; smaller means it was shrunk.
    scale: float = 1.0
    #: True when the text needed cutting or could not shrink far enough.
    overflow: bool = False
    reason: str = ""
    width: float = 0.0
    height: float = 0.0
    #: Lines the text needed before any cutting.
    lines_needed: int = 0

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def line_count(self) -> int:
        return len(self.lines)

    def describe(self) -> str:
        base = f"{self.font_size:.1f}px, {self.line_count} line(s)"
        if self.overflow:
            return f"{base} - {self.reason}"
        return base


def fit_text(font_factory, text: str, box_width: float, box_height: float,
             options: Optional[FitOptions] = None) -> FittedText:
    """Choose the largest font size that fits ``text`` in a pixel box.

    ``font_factory`` is called with a size and must return a Pillow font, so the
    caller owns font discovery and caching.  The search is a binary search over
    sizes, which is exact for real metrics and cheap enough for interactive
    previews.

    Nothing here edits the wording.  When the text cannot fit at the minimum
    size the result is flagged ``overflow`` with a reason, and the caller turns
    that into a validation message rather than silently cropping.
    """
    opts = options or FitOptions()
    content = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not content.strip():
        return FittedText(lines=[content] if content else [], font_size=opts.clamp_size(opts.max_size),
                          scale=1.0, width=0.0, height=0.0)
    if box_width <= 0 or box_height <= 0:
        return FittedText(lines=content.split("\n"), font_size=opts.clamp_size(opts.min_size),
                          scale=0.0, overflow=True, reason="The element has no usable size.")

    min_size = opts.clamp_size(min(opts.min_size, opts.max_size))
    max_size = opts.clamp_size(opts.max_size)
    if not opts.allow_grow:
        max_size = min(max_size, opts.clamp_size(opts.max_size))

    best: Optional[FittedText] = None
    low, high = min_size, max_size
    # A handful of iterations is plenty: 40 halvings covers any sane range.
    for _ in range(40):
        size = (low + high) / 2.0
        candidate = _layout(font_factory, content, size, box_width, box_height, opts)
        if candidate.overflow:
            high = size
        else:
            best = candidate
            low = size
        if high - low <= max(0.01, opts.tolerance):
            break

    if best is None:
        # Nothing fits, even at the minimum size.  Lay out at the minimum and
        # cut the text so the frame still renders, but say so.
        fallback = _layout(font_factory, content, min_size, box_width, box_height, opts, force_cut=True)
        fallback.reason = fallback.reason or (
            f"The text does not fit at the smallest allowed size ({min_size:.0f}px)."
        )
        return fallback

    # Use the largest size found, but never grow past what the caller asked for.
    if best.font_size < max_size - max(0.01, opts.tolerance):
        refined = _layout(font_factory, content, min(high, max_size), box_width, box_height, opts)
        if not refined.overflow:
            best = refined
    return best


def _layout(font_factory, content: str, size: float, box_width: float, box_height: float,
            opts: FitOptions, *, force_cut: bool = False) -> FittedText:
    """Lay *content* out at one size and report whether it fits."""
    size = max(1.0, float(size))
    font = font_factory(size)
    lines = wrap_text(font, content, box_width, line_spacing=opts.line_spacing, hard_break=opts.hard_break)
    lines_needed = len(lines)

    overflow = False
    reason = ""
    if opts.max_lines and lines_needed > opts.max_lines:
        overflow = True
        reason = f"It needs {lines_needed} lines but only {opts.max_lines} fit."
        if force_cut or opts.ellipsis:
            lines = lines[: opts.max_lines]
            if opts.ellipsis and lines:
                lines[-1] = _ellipsize(font, lines[-1], box_width)

    metrics = measure_text(font, "\n".join(lines), line_spacing=opts.line_spacing)
    if metrics.height > box_height + 0.5:
        overflow = True
        reason = reason or (
            f"The block is {metrics.height:.0f}px tall but the element is {box_height:.0f}px tall."
        )
        if force_cut and lines:
            # Drop whole lines until the block fits.
            while lines and measure_text(font, "\n".join(lines), line_spacing=opts.line_spacing).height > box_height:
                lines.pop()
            if opts.ellipsis and lines:
                lines[-1] = _ellipsize(font, lines[-1], box_width)
            metrics = measure_text(font, "\n".join(lines), line_spacing=opts.line_spacing)

    widest = max((_text_width(font, line) for line in lines), default=0.0)
    if widest > box_width + 0.5 and not force_cut:
        overflow = True
        reason = reason or f"The widest line is {widest:.0f}px but the element is {box_width:.0f}px wide."

    return FittedText(
        lines=lines,
        font_size=size,
        scale=1.0,
        overflow=overflow,
        reason=reason,
        width=widest,
        height=metrics.height,
        lines_needed=lines_needed,
    )


def _ellipsize(font: Any, line: str, max_width: float) -> str:
    """Trim a line and add an ellipsis so it fits ``max_width``."""
    if _text_width(font, line) <= max_width:
        return line
    ellipsis = "\u2026"
    if _text_width(font, ellipsis) > max_width:
        return ""
    text = line.rstrip()
    while text and _text_width(font, text + ellipsis) > max_width:
        text = text[:-1]
    return (text.rstrip() + ellipsis) if text else ellipsis


# --------------------------------------------------------------------------
# Missing-glyph detection
# --------------------------------------------------------------------------

#: Codepoint that no shipping font maps, so its bitmap is the .notdef glyph.
_NOTDEF_PROBE = "\uffff"


def missing_glyphs(font: Any, text: str, *, cache: Optional[dict] = None) -> list[str]:
    """Characters the resolved font cannot draw.

    Compared against U+FFFF, which fonts do not map, so its bitmap is the
    "missing glyph" box.  This is a **heuristic**: it reliably catches whole
    scripts that are absent (Devanagari, CJK, Arabic) but may miss a single
    decorative codepoint.  It is reported as a warning, never as a hard error.
    """
    content = text or ""
    if not content:
        return []
    store = cache if cache is not None else {}
    reference = store.get("__notdef__")
    if reference is None:
        try:
            mask = font.getmask(_NOTDEF_PROBE, mode="L")
            reference = (mask.size, bytes(mask))
        except (OSError, ValueError, TypeError):
            reference = None
        store["__notdef__"] = reference
    if reference is None:
        return []

    missing: list[str] = []
    seen: set[str] = set()
    for char in content:
        if char in seen or char.isspace() or unicodedata.combining(char):
            continue
        try:
            mask = font.getmask(char, mode="L")
            got = (mask.size, bytes(mask))
        except (OSError, ValueError, TypeError):
            continue
        if got == reference:
            seen.add(char)
            missing.append(char)
        else:
            seen.add(char)
    return missing
