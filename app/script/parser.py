"""The structured script format (directive sections 11-14).

A script can be plain prose or structured.  Structured scripts look like this::

    [SCENE 01]

    Narration:
    This is the narration.

    On Screen:
    The important number.

    Visual:
    Show a falling chart.

    Music:
    Low background music.

    SFX:
    Soft impact.

    Duration:
    AUTO

Three rules shape this module:

1. **The parser never destroys text.**  A label it does not recognise is kept in
   ``extra`` and written back out unchanged.
2. **Plain text is valid.**  A script with no scene headers at all parses into a
   single narration block - a beginner can paste a script and move on.
3. **Round-tripping is safe.**  ``parse(to_text(parse(text)))`` gives the same
   blocks, so editing in structured mode does not quietly lose content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

#: Scene header: ``[SCENE 01]``, ``[SCENE 1 - Intro]``, ``[Intro]``.
SCENE_HEADER = re.compile(r"^\s*\[\s*(?:SCENE\s*([0-9]+)\s*[-:–]?\s*|)([^\]]*?)\s*\]\s*$", re.IGNORECASE)

#: Known field labels, in the order they are written back out.
FIELD_ORDER: tuple[str, ...] = (
    "Narration",
    "On Screen",
    "Visual",
    "Music",
    "SFX",
    "Duration",
    "Notes",
)

#: Label spellings accepted on input, mapped to the canonical label.
FIELD_ALIASES: dict[str, str] = {
    "narration": "Narration",
    "voiceover": "Narration",
    "voice over": "Narration",
    "vo": "Narration",
    "on screen": "On Screen",
    "onscreen": "On Screen",
    "text": "On Screen",
    "title": "On Screen",
    "visual": "Visual",
    "visuals": "Visual",
    "video": "Visual",
    "image": "Visual",
    "music": "Music",
    "bgm": "Music",
    "sfx": "SFX",
    "sound": "SFX",
    "sound effect": "SFX",
    "sound effects": "SFX",
    "duration": "Duration",
    "length": "Duration",
    "time": "Duration",
    "notes": "Notes",
    "note": "Notes",
    "comment": "Notes",
}

#: ``Duration: AUTO`` means "take it from the narration audio".
DURATION_AUTO = "AUTO"


@dataclass
class ScriptBlock:
    """One scene/section of a script."""

    index: int = 0
    #: Scene number written in the header, when there was one.
    number: Optional[int] = None
    #: Title from the header (``[SCENE 01 - Intro]``), when there was one.
    title: str = ""
    narration: str = ""
    on_screen: str = ""
    visual: str = ""
    music: str = ""
    sfx: str = ""
    #: Raw duration text as written (``AUTO``, ``3.5``, ``2s``).
    duration: str = DURATION_AUTO
    notes: str = ""
    #: Labels the parser did not recognise, preserved exactly as written.
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """How the block is shown in lists: ``Scene 01`` or ``Section 1``."""
        if self.number is not None:
            return f"Scene {self.number:02d}"
        return f"Section {self.index + 1}"

    @property
    def display_title(self) -> str:
        return self.title or self.label

    def duration_seconds(self) -> Optional[float]:
        """The duration in seconds, or ``None`` when it is AUTO/unparsable."""
        text = (self.duration or "").strip().lower()
        if not text or text == DURATION_AUTO.lower():
            return None
        match = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(ms|s|sec|secs|seconds|m|min|minutes)?$", text)
        if not match:
            return None
        value = float(match.group(1))
        unit = match.group(2) or "s"
        if unit == "ms":
            return round(value / 1000.0, 3)
        if unit in ("m", "min", "minutes"):
            return round(value * 60.0, 3)
        return round(value, 3)

    def is_empty(self) -> bool:
        return not any(
            (
                self.narration.strip(),
                self.on_screen.strip(),
                self.visual.strip(),
                self.music.strip(),
                self.sfx.strip(),
                self.notes.strip(),
                self.extra,
            )
        )

    def narration_words(self) -> int:
        return len(re.findall(r"\S+", self.narration or ""))


@dataclass
class ParsedScript:
    """The result of parsing a script."""

    blocks: list[ScriptBlock] = field(default_factory=list)
    #: True when at least one ``[SCENE ...]`` header was found.
    structured: bool = False
    #: Text that appeared before the first header (kept, never dropped).
    preamble: str = ""
    #: Problems worth showing the user; parsing never fails.
    warnings: list[str] = field(default_factory=list)

    def narration_text(self) -> str:
        """Every narration line, joined for one continuous voice-over."""
        return "\n\n".join(block.narration.strip() for block in self.blocks if block.narration.strip())

    def word_count(self) -> int:
        return sum(block.narration_words() for block in self.blocks)

    def block_by_index(self, index: int) -> Optional[ScriptBlock]:
        for block in self.blocks:
            if block.index == index:
                return block
        return None


def _canonical_label(raw: str) -> str:
    """Map a written label to its canonical form, keeping unknown ones as-is."""
    key = raw.strip().lower().rstrip(":").strip()
    return FIELD_ALIASES.get(key, raw.strip().rstrip(":").strip())


def _split_label_line(line: str) -> Optional[tuple[str, str]]:
    """Return ``(label, rest)`` when *line* starts a labelled field."""
    match = re.match(r"^([A-Za-z][A-Za-z /]{0,24}):\s*(.*)$", line)
    if not match:
        return None
    return _canonical_label(match.group(1)), match.group(2).strip()


def parse(text: str) -> ParsedScript:
    """Parse a script into blocks.

    Never raises: anything the parser does not understand is preserved, and the
    problems it noticed are returned in ``warnings``.
    """
    source = text or ""
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    preamble_lines: list[str] = []
    blocks: list[ScriptBlock] = []
    structured = False
    warnings: list[str] = []

    current: Optional[ScriptBlock] = None
    current_field: Optional[str] = None
    buffer: list[str] = []

    def flush_field() -> None:
        nonlocal current_field, buffer
        if current is None or current_field is None:
            buffer = []
            current_field = None
            return
        value = "\n".join(buffer).strip("\n")
        _assign(current, current_field, value, warnings)
        buffer = []
        current_field = None

    def start_block(number: Optional[int], title: str) -> None:
        nonlocal current
        flush_field()
        current = ScriptBlock(index=len(blocks), number=number, title=title.strip())
        blocks.append(current)

    for line in lines:
        header = SCENE_HEADER.match(line)
        if header:
            structured = True
            number_text, title = header.group(1), header.group(2)
            start_block(int(number_text) if number_text else None, title or "")
            continue

        labelled = _split_label_line(line)
        if labelled is not None and current is not None:
            flush_field()
            current_field, rest = labelled
            buffer = [rest] if rest else []
            continue

        if current is None:
            preamble_lines.append(line)
        elif current_field is None:
            # Text inside a scene with no label: treat it as narration, which is
            # what a human means when they type a sentence under a scene header.
            current_field = "Narration"
            buffer = [line]
        else:
            buffer.append(line)

    flush_field()

    preamble = "\n".join(preamble_lines).strip("\n")
    if preamble and not structured:
        # A plain script: the whole text is one narration block.
        blocks = [ScriptBlock(index=0, narration=preamble)]
        preamble = ""
    elif preamble and structured:
        warnings.append("Text before the first [SCENE] header was kept but is not part of any scene.")

    if not blocks and preamble:
        blocks = [ScriptBlock(index=0, narration=preamble)]
        preamble = ""

    # Number unnumbered blocks so the list is stable and readable.
    for position, block in enumerate(blocks, start=1):
        block.index = position - 1
        if block.number is None and structured:
            block.number = position

    return ParsedScript(blocks=blocks, structured=structured, preamble=preamble, warnings=warnings)


def _assign(block: ScriptBlock, label: str, value: str, warnings: list[str]) -> None:
    """Store a parsed field on the block, keeping unknown labels."""
    lowered = label.lower()
    if lowered == "narration":
        block.narration = _join(block.narration, value)
    elif lowered == "on screen":
        block.on_screen = _join(block.on_screen, value)
    elif lowered == "visual":
        block.visual = _join(block.visual, value)
    elif lowered == "music":
        block.music = _join(block.music, value)
    elif lowered == "sfx":
        block.sfx = _join(block.sfx, value)
    elif lowered == "duration":
        block.duration = value.strip() or DURATION_AUTO
    elif lowered == "notes":
        block.notes = _join(block.notes, value)
    else:
        if label not in block.extra:
            block.extra[label] = value
            warnings.append(f"Unknown script field “{label}” was kept as written.")
        else:
            block.extra[label] = _join(block.extra[label], value)


def _join(existing: str, value: str) -> str:
    if not existing:
        return value
    if not value:
        return existing
    return f"{existing}\n{value}"


# --------------------------------------------------------------------------
# Writing back out
# --------------------------------------------------------------------------

def block_to_text(block: ScriptBlock) -> str:
    """Render one block in the structured format."""
    lines: list[str] = []
    header = f"[SCENE {block.number:02d}]" if block.number is not None else f"[SECTION {block.index + 1:02d}]"
    if block.title:
        header = f"{header[:-1]} - {block.title}]"
    lines.append(header)
    lines.append("")

    for label, value in (
        ("Narration", block.narration),
        ("On Screen", block.on_screen),
        ("Visual", block.visual),
        ("Music", block.music),
        ("SFX", block.sfx),
        ("Duration", block.duration or DURATION_AUTO),
        ("Notes", block.notes),
    ):
        if not str(value).strip():
            continue
        lines.append(f"{label}:")
        lines.extend(str(value).rstrip("\n").split("\n"))
        lines.append("")

    for label, value in block.extra.items():
        if not str(value).strip():
            continue
        lines.append(f"{label}:")
        lines.extend(str(value).rstrip("\n").split("\n"))
        lines.append("")

    return "\n".join(lines).rstrip("\n")


def to_text(parsed: ParsedScript) -> str:
    """Render a parsed script back to text (safe to re-parse)."""
    parts: list[str] = []
    if parsed.preamble.strip():
        parts.append(parsed.preamble.strip())
    parts.extend(block_to_text(block) for block in parsed.blocks)
    return "\n\n".join(parts).rstrip("\n") + "\n"


def is_structured(text: str) -> bool:
    """True when the text contains at least one scene header."""
    return any(SCENE_HEADER.match(line) for line in (text or "").splitlines())


def plain_to_structured(text: str) -> str:
    """Split plain prose into numbered scenes on blank lines.

    Used only when the user asks to switch to structured mode - never silently.
    """
    parsed = parse(text)
    if parsed.structured:
        return text
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text or "") if block.strip()]
    rebuilt = ParsedScript(
        blocks=[
            ScriptBlock(index=index, number=index + 1, narration=block)
            for index, block in enumerate(blocks)
        ],
        structured=True,
    )
    if not rebuilt.blocks:
        rebuilt.blocks = [ScriptBlock(index=0, number=1, narration=(text or "").strip())]
    return to_text(rebuilt)


__all__ = [
    "DURATION_AUTO",
    "FIELD_ALIASES",
    "FIELD_ORDER",
    "ParsedScript",
    "ScriptBlock",
    "block_to_text",
    "is_structured",
    "parse",
    "plain_to_structured",
    "to_text",
]
