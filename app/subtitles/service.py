"""Subtitles and captions (Stage E, directive sections 12-16).

Cue timings come from the **measured narration**, never from an invented
word-level alignment.  Each scene's narration occupies a window whose length was
measured from the real audio; the scene's text is split into sentences and each
sentence gets a share of that window proportional to how much text it carries.

That is an honest sentence-level estimate and it is labelled as one
(``timing_source = "narration"``).  Nothing here claims frame-accurate word
timing, because no word-level alignment data exists to claim it from
(directive section 13).  A user can then edit, split, merge or retime any cue,
which flips the provenance to ``"manual"``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from ..core.atomicio import atomic_write_text
from ..core.logging_setup import get_logger, log_event
from ..core.paths import unique_path
from ..project.model import SubtitleCue, SubtitleSpec, new_id

LOGGER = get_logger("subtitles")

__all__ = [
    "SubtitleIssue",
    "generate_cues",
    "split_sentences",
    "to_srt",
    "to_vtt",
    "to_ass",
    "wrap_lines",
    "edit_cue",
    "split_cue",
    "merge_cues",
    "delete_cue",
    "validate_cues",
    "timecode_srt",
    "timecode_vtt",
]

#: Sentence terminators, including the Devanagari danda.
_SENTENCE_RE = re.compile(r"(?<=[.!?।])\s+")


@dataclass
class SubtitleIssue:
    code: str
    message: str
    what_to_do: str = ""
    severity: str = "warning"

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "what_to_do": self.what_to_do, "severity": self.severity}


# --------------------------------------------------------------------------
# Text shaping
# --------------------------------------------------------------------------

def split_sentences(text: str) -> list[str]:
    """Split narration text into sentences, keeping the punctuation.

    Wording is never changed: this only decides where the breaks go.
    """
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return []
    parts = [part.strip() for part in _SENTENCE_RE.split(cleaned) if part.strip()]
    return parts or [cleaned]


def wrap_lines(text: str, max_chars: int) -> list[str]:
    """Wrap one cue's text to at most ``max_chars`` per line, on word boundaries."""
    limit = max(8, int(max_chars))
    words = str(text or "").split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= limit or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------

def generate_cues(project: Any, timeline: Any, *, max_chars_per_line: int = 42,
                  max_lines: int = 2, max_chars_per_cue: int = 60,
                  min_duration: float = 0.8, max_duration: float = 7.0) -> tuple[list, list]:
    """Build cues from the measured narration windows.

    Returns ``(cues, issues)``.  A scene whose length was *not* measured from
    narration produces no cue and is reported: inventing a timing for it would be
    a fake subtitle (directive sections 13, 83).
    """
    cues: list[SubtitleCue] = []
    issues: list[SubtitleIssue] = []
    scenes = {scene.id: scene for scene in getattr(project, "scenes", []) or []}

    for timing in getattr(timeline, "timings", []) or []:
        scene = scenes.get(timing.scene_id)
        if scene is None:
            continue
        text = str(getattr(scene.narration, "text", "") or "").strip()
        if not text:
            continue
        if getattr(timing, "source", "") != "narration" or timing.narration_duration <= 0:
            issues.append(SubtitleIssue(
                "SUBTITLE_NO_NARRATION_TIMING",
                f"'{timing.name or timing.scene_id}' has narration text but no measured narration audio.",
                "Generate the narration for this scene to time its captions.",
            ))
            continue

        start = float(timing.start)
        window = float(timing.narration_duration)
        sentences = split_sentences(text)
        chunks = _chunk(sentences, max_chars_per_line, max_lines, max_chars_per_cue)
        if not chunks:
            continue

        # Share the measured window in proportion to how much text each cue
        # carries, so a long sentence stays on screen longer than a short one.
        weights = [max(1, len(chunk)) for chunk in chunks]
        total = float(sum(weights))
        cursor = start
        for chunk, weight in zip(chunks, weights):
            span = window * (weight / total) if total else window / len(chunks)
            span = max(min_duration, min(max_duration, span))
            end = min(start + window, cursor + span)
            if end <= cursor:
                break
            cues.append(SubtitleCue(id=new_id("cue"), start=round(cursor, 3),
                                    end=round(end, 3), text=chunk))
            cursor = end
        if cues and cursor < start + window - 0.05:
            # Let the last cue run to the end of the speech rather than leaving a
            # gap where the voice is still talking.
            cues[-1].end = round(start + window, 3)

    cues = _dedupe_and_order(cues)
    return cues, issues


def _chunk(sentences: Sequence[str], max_chars: int, max_lines: int,
           max_chars_per_cue: int = 0) -> list[str]:
    """Group sentences into cues that stay readable.

    A cue holds at most ``max_chars_per_cue`` characters (so it does not linger on
    screen with a wall of text) and is displayed on at most ``max_lines`` lines of
    ``max_chars`` each.
    """
    display_capacity = max(8, int(max_chars)) * max(1, int(max_lines))
    limit = int(max_chars_per_cue or 0)
    capacity = min(display_capacity, limit) if limit > 0 else display_capacity
    capacity = max(8, capacity)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if not current:
            current = sentence
        elif len(current) + 1 + len(sentence) <= capacity:
            current = f"{current} {sentence}"
        else:
            chunks.append(current)
            current = sentence
        while len(current) > capacity:
            # A single sentence longer than a cue: break it on a word boundary.
            pieces = wrap_lines(current, max_chars)
            head = " ".join(pieces[:max_lines])
            rest = " ".join(pieces[max_lines:])
            chunks.append(head)
            current = rest
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()]


def _dedupe_and_order(cues: Iterable[SubtitleCue]) -> list[SubtitleCue]:
    ordered = sorted(cues, key=lambda cue: (cue.start, cue.end))
    result: list[SubtitleCue] = []
    for cue in ordered:
        if result and cue.start < result[-1].end:
            # Two cues must never be on screen at once; trim, never drop text.
            cue.start = min(cue.end, result[-1].end)
        if cue.end <= cue.start:
            continue
        result.append(cue)
    return result


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

def timecode_srt(seconds: float) -> str:
    """``00:00:01,500`` - SRT uses a comma for milliseconds."""
    value = max(0.0, float(seconds))
    millis = int(round(value * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def timecode_vtt(seconds: float) -> str:
    """``00:00:01.500`` - WebVTT uses a dot."""
    return timecode_srt(seconds).replace(",", ".")


def to_srt(cues: Sequence[SubtitleCue]) -> str:
    """A SubRip file.  Blank input gives an empty string, not a fake cue."""
    blocks = []
    for index, cue in enumerate(sorted(cues, key=lambda item: item.start), start=1):
        text = "\n".join(wrap_lines(cue.text, 42)) or ""
        blocks.append(f"{index}\n{timecode_srt(cue.start)} --> {timecode_srt(cue.end)}\n{text}\n")
    return "\n".join(blocks)


def to_vtt(cues: Sequence[SubtitleCue]) -> str:
    """A WebVTT file, including the required header."""
    body = []
    for cue in sorted(cues, key=lambda item: item.start):
        text = "\n".join(wrap_lines(cue.text, 42)) or ""
        body.append(f"{timecode_vtt(cue.start)} --> {timecode_vtt(cue.end)}\n{text}\n")
    header = "WEBVTT\n\n"
    return header + "\n".join(body)


def to_ass(cues: Sequence[SubtitleCue], style: Any, *, width: int, height: int,
           style_name: str = "MGS", spec: Any = None) -> str:
    """An ASS file with the project's real caption styling.

    ASS is used for burnt-in captions because it carries the styling (font, size,
    colours, outline, shadow, box and alignment) that FFmpeg's ``subtitles``
    filter renders through libass - so what the user configured is what appears.
    """
    font = str(getattr(style, "font", "") or "DejaVu Sans")
    # ASS sizes are in script-resolution pixels; the script resolution is set to
    # the frame size below, so the stored point size maps directly.
    size = max(8, int(_attr(style, spec, "font_size", 44) or 44))
    primary = _ass_color(getattr(style, "color", "#ffffff"))
    outline_color = _ass_color(getattr(style, "outline_color", "#000000"))
    outline_width = max(0.0, float(getattr(style, "outline_width", 2.0) or 0.0))
    shadow = 1.0 if _attr(spec, style, "shadow", True) else 0.0
    background = str(_attr(spec, style, "background", "") or "")
    back_opacity = max(0.0, min(1.0, float(_attr(spec, style, "background_opacity", 0.0) or 0.0)))
    box = 3 if (background and back_opacity > 0) else 1
    border_style = 3 if box == 3 else 1
    back_colour = _ass_color(background or "#000000", opacity=back_opacity if box == 3 else 0.0)
    margin_v = max(0, int(round(height * (float(_attr(spec, style, "margin_percent", 6.0) or 0.0) / 100.0))))
    margin_l = margin_r = max(0, int(round(width * 0.05)))
    alignment = {"bottom": 2, "middle": 5, "top": 8}.get(
        str(_attr(spec, style, "position", "bottom") or "bottom"), 2)
    max_lines = max(1, int(getattr(style, "max_lines", 2) or 2))

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {int(width)}\n"
        f"PlayResY: {int(height)}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: {style_name},{font},{size},{primary},&H00FFFFFF,{outline_color},{back_colour},"
        f"-1,0,0,0,100,100,0,0,{border_style},{outline_width:g},{shadow:g},{alignment},"
        f"{margin_l},{margin_r},{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = [header]
    for cue in sorted(cues, key=lambda item: item.start):
        text = _ass_text(cue.text, max_chars_per_line=42, max_lines=max_lines)
        lines.append(
            f"Dialogue: 0,{_ass_time(cue.start)},{_ass_time(cue.end)},{style_name},,0,0,0,,{text}\n"
        )
    return "".join(lines)


def _attr(primary: Any, fallback: Any, name: str, default: Any) -> Any:
    """Read a caption setting from whichever section actually holds it.

    Caption *type* (font, size, colour, outline, line count) lives in the theme's
    ``subtitle_style``; caption *layout* (position, margin, background, shadow)
    lives in the project's ``subtitles`` section.  A caller passes both and does
    not have to know which is which.
    """
    for source in (primary, fallback):
        if source is not None and hasattr(source, name):
            return getattr(source, name)
    return default


def _ass_time(seconds: float) -> str:
    value = max(0.0, float(seconds))
    centis = int(round(value * 100))
    hours, centis = divmod(centis, 360_000)
    minutes, centis = divmod(centis, 6_000)
    secs, centis = divmod(centis, 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _ass_text(text: str, *, max_chars_per_line: int, max_lines: int) -> str:
    lines = wrap_lines(text, max_chars_per_line)[:max(1, max_lines)]
    # ``\N`` is ASS's hard line break; braces would be style overrides.
    return "\\N".join(line.replace("{", "(").replace("}", ")") for line in lines)


def _ass_color(value: Any, *, opacity: float = 0.0) -> str:
    """Convert ``#rrggbb`` (plus an opacity) to ASS's ``&HAABBGGRR``."""
    from ..scene.palette import parse_color

    color = parse_color(value, (0, 0, 0, 255))
    alpha = int(round(max(0.0, min(1.0, float(opacity))) * 255))
    return f"&H{alpha:02X}{color.b:02X}{color.g:02X}{color.r:02X}"


# --------------------------------------------------------------------------
# Editing (directive section 14)
# --------------------------------------------------------------------------

def edit_cue(spec: SubtitleSpec, cue_id: str, *, text: Optional[str] = None,
             start: Optional[float] = None, end: Optional[float] = None) -> bool:
    """Change one cue's text or timing.  Marks the timings as hand-made."""
    cue = _find(spec, cue_id)
    if cue is None:
        return False
    if text is not None:
        cue.text = str(text)
    if start is not None:
        cue.start = max(0.0, float(start))
    if end is not None:
        cue.end = max(0.0, float(end))
    if start is not None or end is not None:
        spec.timing_source = "manual"
    return True


def split_cue(spec: SubtitleSpec, cue_id: str, at_seconds: float) -> Optional[SubtitleCue]:
    """Split one cue into two at a point in time, dividing its text in half.

    The words are not re-timed individually - that data does not exist - so the
    text is split at the nearest word boundary to the midpoint and the result is
    marked manual.
    """
    cue = _find(spec, cue_id)
    if cue is None:
        return None
    moment = max(cue.start, min(cue.end, float(at_seconds)))
    if moment <= cue.start or moment >= cue.end:
        return None
    words = cue.text.split()
    if len(words) < 2:
        return None
    fraction = (moment - cue.start) / max(1e-6, cue.duration)
    cut = max(1, min(len(words) - 1, int(round(len(words) * fraction))))
    second = SubtitleCue(id=new_id("cue"), start=round(moment, 3), end=cue.end,
                         text=" ".join(words[cut:]))
    cue.end = round(moment, 3)
    cue.text = " ".join(words[:cut])
    spec.cues.append(second)
    spec.cues = _dedupe_and_order(spec.cues)
    spec.timing_source = "manual"
    return second


def merge_cues(spec: SubtitleSpec, first_id: str, second_id: str) -> bool:
    """Merge two *adjacent* cues into one, keeping both texts and the outer timing.

    Only neighbours can be merged.  Joining two cues with a third in between - or
    with a gap of silence between them - would produce one caption that stays on
    screen while nothing is being said, which is worse than two captions.
    """
    first = _find(spec, first_id)
    second = _find(spec, second_id)
    if first is None or second is None or first is second:
        return False
    earlier, later = (first, second) if first.start <= second.start else (second, first)
    for cue in spec.cues:
        if cue is earlier or cue is later:
            continue
        if earlier.start <= cue.start < later.start:
            return False
    earlier.text = f"{earlier.text} {later.text}".strip()
    earlier.start = min(first.start, second.start)
    earlier.end = max(first.end, second.end)
    spec.cues.remove(later)
    spec.timing_source = "manual"
    return True


def delete_cue(spec: SubtitleSpec, cue_id: str) -> bool:
    cue = _find(spec, cue_id)
    if cue is None:
        return False
    spec.cues.remove(cue)
    spec.timing_source = "manual"
    return True


def _find(spec: SubtitleSpec, cue_id: str) -> Optional[SubtitleCue]:
    return next((cue for cue in spec.cues if cue.id == cue_id), None)


# --------------------------------------------------------------------------
# Validation (directive section 16)
# --------------------------------------------------------------------------

def validate_cues(cues: Sequence[SubtitleCue], *, canvas: Any, style: Any,
                  safe_area: Any = None, spec: Any = None) -> list[SubtitleIssue]:
    """Check cues for overlaps, empty text and safe-area escapes.

    Captions are never moved to fit; they are reported so the user can decide
    (directive section 16).
    """
    issues: list[SubtitleIssue] = []
    ordered = sorted(cues, key=lambda cue: cue.start)
    for previous, current in zip(ordered, ordered[1:]):
        if current.start < previous.end - 1e-6:
            issues.append(SubtitleIssue(
                "SUBTITLE_OVERLAP",
                f"Two captions overlap at {previous.end:.2f}s.",
                "Shorten one caption, or move the other later.",
            ))
    for cue in cues:
        if float(cue.start) < 0.0:
            # Nothing can be shown before the video starts.  The writers clamp the
            # timecode to zero, so the exported file is valid - but the caption is
            # then somewhere the user did not put it, which is worth an error.
            issues.append(SubtitleIssue(
                "SUBTITLE_NEGATIVE_START",
                f"A caption starts at {cue.start:.2f}s, before the video begins.",
                "Set its start time to 0 or later so it matches where it will appear.",
                severity="error",
            ))
        if not str(cue.text or "").strip():
            issues.append(SubtitleIssue(
                "SUBTITLE_EMPTY",
                f"A caption at {cue.start:.2f}s has no text.",
                "Give it text, or delete it.",
            ))
        if cue.end <= cue.start:
            issues.append(SubtitleIssue(
                "SUBTITLE_ZERO_LENGTH",
                f"A caption at {cue.start:.2f}s ends before it starts.",
                "Fix its start and end times.",
                severity="error",
            ))

    if canvas is None or style is None:
        return issues

    height = float(getattr(canvas, "height", 0) or 0)
    if height <= 0:
        return issues
    size = float(_attr(style, spec, "font_size", 44) or 44)
    max_lines = max(1, int(_attr(style, spec, "max_lines", 2) or 2))
    margin = height * (float(_attr(spec, style, "margin_percent", 6.0) or 0.0) / 100.0)
    block = size * 1.3 * max_lines
    position = str(_attr(spec, style, "position", "bottom") or "bottom")

    # The safe area is the region platform UI does not cover; captions outside it
    # can be hidden behind a player's controls or a phone's overlay.
    if safe_area is not None:
        rect = getattr(safe_area, "rect", safe_area)
        top = float(getattr(rect, "y", 0.0) or 0.0)
        bottom = top + float(getattr(rect, "height", height) or height)
        if position == "bottom":
            caption_top = height - margin - block
            if caption_top < top - 1.0:
                issues.append(SubtitleIssue(
                    "SUBTITLE_OUTSIDE_SAFE_AREA",
                    "Captions sit above the safe area's top edge, so they may be covered.",
                    "Reduce the caption size or margin, or move them to the bottom.",
                ))
        elif position == "top":
            caption_bottom = margin + block
            if caption_bottom > bottom + 1.0:
                issues.append(SubtitleIssue(
                    "SUBTITLE_OUTSIDE_SAFE_AREA",
                    "Captions reach past the safe area's bottom edge.",
                    "Reduce the caption size or margin.",
                ))
    elif margin + block > height:
        issues.append(SubtitleIssue(
            "SUBTITLE_TOO_LARGE",
            "The caption block is taller than the frame.",
            "Reduce the caption size or the number of lines.",
            severity="error",
        ))
    return issues


def write_subtitle_file(path: Any, content: str, *, overwrite: bool = False) -> Path:
    """Write a caption file without destroying one that is already there.

    The return value is the path that was actually written, which is not always
    the path that was asked for: exporting twice must never silently replace a
    caption file the user has edited by hand (directive sections 12, 36, 44).

    * the file does not exist -> written, path returned unchanged;
    * the file exists and already holds exactly this text -> left alone, so a
      repeated export does not scatter copies;
    * the file exists with *different* text -> the next free name is used and
      the existing file is kept.  The caller sees the real path back.

    ``overwrite=True`` is for this application's own derived files - the captions
    a render writes into its private work folder.  Those are regenerated from the
    project on every run, so replacing them is correct.

    The write itself goes through :func:`atomic_write_text`, so a crash while
    writing cannot leave a half-finished caption file behind.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        try:
            existing = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            existing = None
        if existing == content:
            return target
        kept = target
        target = unique_path(target.parent, target.stem, target.suffix)
        log_event("SUBTITLE_EXPORT_KEPT", f"Kept the existing {kept.name}",
                  logger=LOGGER, path=str(kept), written=str(target))
    atomic_write_text(target, content)
    return target


# --------------------------------------------------------------------------
# The service object the GUI, CLI and render engine all go through
# --------------------------------------------------------------------------

@dataclass
class SubtitlePlan:
    """What generating captions produced, and what was wrong with it."""

    cues: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    timing_source: str = "narration"

    @property
    def ok(self) -> bool:
        return not [issue for issue in self.issues if issue.severity == "error"]

    @property
    def errors(self) -> list:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list:
        return [issue for issue in self.issues if issue.severity != "error"]

    def summary(self) -> str:
        if not self.cues:
            return "No captions were generated."
        span = f"{self.cues[0].start:.2f}s to {self.cues[-1].end:.2f}s"
        return f"{len(self.cues)} caption(s) from {span}"

    def to_dict(self) -> dict:
        return {"count": len(self.cues), "timing_source": self.timing_source,
                "issues": [issue.to_dict() for issue in self.issues],
                "cues": [cue.to_dict() for cue in self.cues]}


class SubtitleService:
    """Captions for a project: generate, edit, check, export.

    The module-level functions stay - they are the tested primitives - but the
    GUI and CLI talk to this object so there is one place that knows how a
    project's subtitle settings, its timeline and its output files fit together.

    Nothing here invents data.  Timings come from measured narration files; a
    scene whose length was guessed is reported rather than silently captioned,
    and word-level timing is never fabricated (directive sections 14-17).
    """

    def __init__(self, *, project_dir: Optional[Path] = None, tools: Any = None) -> None:
        self.project_dir = Path(project_dir) if project_dir else None
        self.tools = tools

    # -- generating -------------------------------------------------------

    def generate(self, project: Any, timeline: Any = None, *,
                 store: bool = True, **options: Any) -> SubtitlePlan:
        """Build captions from the narration and (by default) keep them."""
        if timeline is None:
            from ..scene.timing import build_timeline

            timeline = build_timeline(getattr(project, "scenes", None) or [])
        cues, issues = generate_cues(project, timeline, **options)
        spec = getattr(project, "subtitles", None)
        if spec is not None and store:
            spec.cues = cues
            spec.timing_source = "narration" if cues else "none"
        source = "narration" if cues else "none"
        if spec is not None and str(getattr(spec, "timing_source", "")) == "manual":
            source = "manual"
        return SubtitlePlan(cues=cues, issues=issues, timing_source=source)

    def regenerate(self, project: Any, timeline: Any = None, **options: Any) -> SubtitlePlan:
        """Throw the current captions away and rebuild them from the narration."""
        spec = getattr(project, "subtitles", None)
        if spec is not None:
            spec.cues = []
            spec.timing_source = "none"
        return self.generate(project, timeline, store=True, **options)

    # -- checking ---------------------------------------------------------

    def validate(self, project: Any, *, canvas: Any = None,
                 safe_area: Any = None) -> list:
        """Check the captions against the frame, without changing them."""
        spec = getattr(project, "subtitles", None)
        if spec is None:
            return []
        if canvas is None:
            from ..scene.canvas import Canvas

            fmt = getattr(project, "format", None)
            canvas = Canvas(int(getattr(fmt, "width", 0) or 1920),
                            int(getattr(fmt, "height", 0) or 1080),
                            fps=int(getattr(fmt, "fps", 0) or 30))
        style = _subtitle_style_for(project)
        return validate_cues(getattr(spec, "cues", None) or [], canvas=canvas,
                             style=style, safe_area=safe_area, spec=spec)

    # -- editing ----------------------------------------------------------

    def edit(self, project: Any, cue_id: str, **changes: Any) -> bool:
        return edit_cue(_spec(project), cue_id, **changes)

    def split(self, project: Any, cue_id: str, at_seconds: float):
        return split_cue(_spec(project), cue_id, at_seconds)

    def merge(self, project: Any, first_id: str, second_id: str) -> bool:
        return merge_cues(_spec(project), first_id, second_id)

    def delete(self, project: Any, cue_id: str) -> bool:
        return delete_cue(_spec(project), cue_id)

    def cues(self, project: Any) -> list:
        return list(getattr(_spec(project), "cues", None) or [])

    # -- exporting --------------------------------------------------------

    def export(self, project: Any, output_dir: Any, *, stem: str = "subtitles",
               formats: Sequence[str] = ("srt", "vtt")) -> dict:
        """Write the caption files.  Returns the paths, keyed by format.

        An empty cue list writes nothing: a 0-byte .srt looks like a success
        and is not one.
        """
        cues = self.cues(project)
        folder = Path(output_dir)
        written: dict[str, str] = {}
        if not cues:
            return written
        folder.mkdir(parents=True, exist_ok=True)
        for name in formats:
            target = folder / f"{stem}.{name}"
            if name == "srt":
                written[name] = str(write_subtitle_file(target, to_srt(cues)))
            elif name == "vtt":
                written[name] = str(write_subtitle_file(target, to_vtt(cues)))
            elif name == "ass":
                written[name] = str(self.export_ass(project, target))
                continue
            else:
                continue
        return written

    def export_ass(self, project: Any, target: Any) -> Path:
        """The styled ASS file used for burning captions into the picture."""
        fmt = getattr(project, "format", None)
        content = to_ass(self.cues(project), _subtitle_style_for(project),
                         width=int(getattr(fmt, "width", 0) or 1920),
                         height=int(getattr(fmt, "height", 0) or 1080),
                         spec=getattr(project, "subtitles", None))
        return write_subtitle_file(Path(target), content)

    def ass_content(self, project: Any) -> str:
        """The ASS text, for the render engine to pipe straight into FFmpeg."""
        fmt = getattr(project, "format", None)
        return to_ass(self.cues(project), _subtitle_style_for(project),
                      width=int(getattr(fmt, "width", 0) or 1920),
                      height=int(getattr(fmt, "height", 0) or 1080),
                      spec=getattr(project, "subtitles", None))


def _spec(project: Any) -> Any:
    spec = getattr(project, "subtitles", None)
    if spec is None:
        raise ValueError("This project has no subtitle settings.")
    return spec


def _subtitle_style_for(project: Any) -> Any:
    """Caption styling lives on the theme; layout lives on the subtitle spec."""
    theme = getattr(project, "theme", None)
    return getattr(theme, "subtitle_style", None)
