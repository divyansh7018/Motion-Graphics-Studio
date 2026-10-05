"""Script parsing, statistics, Unicode handling and import/export (sections 11-19, 34-35).

Every test here uses real text - English, Hindi and mixed Hinglish - because the
failure mode that matters is silent corruption of the user's words.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.script.io import (
    MAX_IMPORT_BYTES,
    decode_text,
    export_script,
    import_script,
    strip_markdown,
    to_markdown,
)
from app.script.parser import (
    DURATION_AUTO,
    block_to_text,
    is_structured,
    parse,
    plain_to_structured,
    to_text,
)
from app.script.stats import (
    DEFAULT_WORDS_PER_MINUTE,
    count_paragraphs,
    count_sentences,
    count_words,
    estimate_seconds,
    format_duration,
    stats_for_text,
)

ENGLISH = (
    "Welcome back to the channel. Today we look at index funds.\n\n"
    "They are simple, cheap and boring - which is exactly what you want."
)

HINDI = "नमस्ते दोस्तों। आज हम बात करेंगे निवेश की। यह वीडियो आपके लिए फायदेमंद होगा।"

HINGLISH = "आज का topic है mutual funds. चलिए शुरू करते हैं, ठीक है?"

STRUCTURED = """[SCENE 01]

Narration:
This is the narration for scene one.

On Screen:
The headline text

Visual:
Show a rising chart.

Music:
Soft background music.

SFX:
A soft impact.

Duration:
6

[SCENE 02]

Narration:
Scene two narration here.

On Screen:
Second headline

Visual:
Cut to the presenter.

Music:
Continue.

SFX:
None.

Duration:
AUTO
"""


# --------------------------------------------------------------------------
# Plain text
# --------------------------------------------------------------------------

def test_plain_text_becomes_one_narration_block() -> None:
    """A beginner pastes a normal script; it must not be mangled (section 13)."""
    parsed = parse(ENGLISH)

    assert parsed.structured is False
    assert len(parsed.blocks) == 1
    assert parsed.blocks[0].narration == ENGLISH
    assert parsed.warnings == []


def test_plain_text_survives_a_round_trip_byte_for_byte() -> None:
    parsed = parse(ENGLISH)

    assert parse(to_text(parsed)).blocks[0].narration == ENGLISH


def test_empty_script_is_not_an_error() -> None:
    parsed = parse("")

    assert parsed.blocks == [] or parsed.blocks[0].is_empty()
    assert parsed.word_count() == 0


# --------------------------------------------------------------------------
# Structured scripts
# --------------------------------------------------------------------------

def test_structured_script_extracts_every_field() -> None:
    parsed = parse(STRUCTURED)

    assert parsed.structured is True
    assert len(parsed.blocks) == 2

    first = parsed.blocks[0]
    assert first.number == 1
    assert first.narration == "This is the narration for scene one."
    assert first.on_screen == "The headline text"
    assert first.visual == "Show a rising chart."
    assert first.music == "Soft background music."
    assert first.sfx == "A soft impact."
    assert first.duration == "6"
    assert first.duration_seconds() == 6.0

    second = parsed.blocks[1]
    assert second.number == 2
    assert second.duration == DURATION_AUTO
    assert second.duration_seconds() is None


def test_a_field_the_parser_does_not_know_is_kept_not_dropped() -> None:
    """Section 12: the parser must never destroy text it does not understand."""
    text = "[SCENE 01]\n\nNarration:\nHello.\n\nCamera:\nSlow zoom in.\n\nNotes:\nCheck audio.\n"

    parsed = parse(text)

    assert parsed.blocks[0].extra["Camera"] == "Slow zoom in."
    assert parsed.blocks[0].notes == "Check audio."
    assert any("Camera" in warning for warning in parsed.warnings)


def test_unknown_header_text_is_preserved_as_a_preamble() -> None:
    text = "TITLE: My video\nAUTHOR: Divyansh\n\n[SCENE 01]\n\nNarration:\nHello.\n"

    parsed = parse(text)

    assert "My video" in parsed.preamble
    assert parsed.blocks[0].narration == "Hello."


def test_field_aliases_are_understood() -> None:
    """Writers use many names for the same field; all must be recognised."""
    for narration_label in ("Narration", "Voiceover", "VO", "Voice over"):
        block = parse(f"[SCENE 01]\n\n{narration_label}:\nSpoken words.\n").blocks[0]
        assert block.narration == "Spoken words.", narration_label

    for screen_label in ("On Screen", "Text", "Title", "Onscreen"):
        block = parse(f"[SCENE 01]\n\n{screen_label}:\nHeadline.\n").blocks[0]
        assert block.on_screen == "Headline.", screen_label

    for duration_label in ("Duration", "Length", "Time"):
        block = parse(f"[SCENE 01]\n\n{duration_label}:\n5\n").blocks[0]
        assert block.duration_seconds() == 5.0, duration_label


def test_structured_round_trip_keeps_every_field() -> None:
    parsed = parse(STRUCTURED)

    again = parse(to_text(parsed))

    assert len(again.blocks) == 2
    for original, restored in zip(parsed.blocks, again.blocks):
        assert restored.narration == original.narration
        assert restored.on_screen == original.on_screen
        assert restored.visual == original.visual
        assert restored.music == original.music
        assert restored.sfx == original.sfx
        assert restored.duration == original.duration


def test_block_to_text_writes_a_readable_block() -> None:
    parsed = parse(STRUCTURED)

    text = block_to_text(parsed.blocks[0])

    assert "[SCENE 01]" in text
    assert "Narration:" in text
    assert "This is the narration for scene one." in text


def test_is_structured_detects_scene_headers() -> None:
    assert is_structured(STRUCTURED) is True
    assert is_structured(ENGLISH) is False


def test_plain_script_can_be_converted_to_scenes() -> None:
    converted = plain_to_structured(ENGLISH)

    parsed = parse(converted)

    assert parsed.structured is True
    assert len(parsed.blocks) == 2, "one block per paragraph"
    assert parsed.blocks[0].narration.startswith("Welcome back")


def test_parse_never_raises_on_rubbish() -> None:
    """Section 12: garbage in, structured answer out - never an exception."""
    for junk in ("[[[", "Narration:", "[SCENE]", "[SCENE abc]\nNarration:\n", "\x00\x01binary"):
        parsed = parse(junk)
        assert parsed is not None


# --------------------------------------------------------------------------
# Statistics (section 17)
# --------------------------------------------------------------------------

def test_word_and_sentence_counts_for_english() -> None:
    stats = stats_for_text(ENGLISH)

    assert stats.words == count_words(ENGLISH) == len(ENGLISH.split()) == 24
    assert stats.sentences == 3
    assert stats.paragraphs == count_paragraphs(ENGLISH) == 2
    assert stats.characters == len(ENGLISH) == 127
    assert stats.characters_no_spaces == 103
    assert stats.lines == 2, "blank lines are layout, not content"


def test_hindi_counts_use_the_devanagari_sentence_end() -> None:
    """The danda (।) ends a Hindi sentence; a period-based count would say one."""
    stats = stats_for_text(HINDI)

    assert count_sentences(HINDI) == 3
    assert stats.sentences == 3
    assert stats.words == len(HINDI.split()) == 14
    # Counting must never alter the text itself.
    assert parse(HINDI).blocks[0].narration == HINDI


def test_mixed_hinglish_is_counted_without_losing_text() -> None:
    stats = stats_for_text(HINGLISH)

    assert stats.words == count_words(HINGLISH)
    assert stats.words > 0
    assert "mutual funds" in HINGLISH


def test_duration_is_labelled_as_an_estimate() -> None:
    """Section 17: the estimate must never look like a measured duration."""
    stats = stats_for_text(ENGLISH)

    assert stats.has_actual is False
    assert "estimated" in stats.duration_label()
    assert stats.estimated_seconds == estimate_seconds(stats.words, DEFAULT_WORDS_PER_MINUTE)
    assert stats.estimated_seconds > 0


def test_measured_duration_replaces_the_estimate_in_the_label() -> None:
    stats = stats_for_text(ENGLISH, actual_seconds=42.0)

    assert stats.has_actual is True
    assert stats.duration_seconds() == 42.0
    assert "estimated" not in stats.duration_label()


def test_format_duration_is_unambiguous() -> None:
    assert format_duration(3.2) == "3.20s"
    assert format_duration(83) == "1m 23s"
    assert format_duration(3725) == "1h 02m 05s"


# --------------------------------------------------------------------------
# Unicode safety (sections 34-35)
# --------------------------------------------------------------------------

UNICODE_CASES = [
    ("english", "It's a test — with “quotes”, (parentheses) and 1,000 numbers."),
    ("hindi", HINDI),
    ("hinglish", HINGLISH),
    ("currency", "Invest ₹5,000 or $50 today."),
    ("punctuation", "Wait... what?! Really; yes - no?"),
    ("linebreaks", "Line one.\nLine two.\r\nLine three."),
    ("emoji_and_symbols", "Great job! 🎉 100% ★ ₹"),
]


@pytest.mark.parametrize("name,text", UNICODE_CASES, ids=[c[0] for c in UNICODE_CASES])
def test_unicode_survives_parsing_and_serialisation(name: str, text: str) -> None:
    """No corruption through parse -> to_text -> parse (section 34)."""
    parsed = parse(text)

    restored = parse(to_text(parsed))

    assert restored.blocks[0].narration == parsed.blocks[0].narration
    assert parse(to_text(restored)).blocks[0].narration == parsed.blocks[0].narration


@pytest.mark.parametrize("name,text", UNICODE_CASES, ids=[c[0] for c in UNICODE_CASES])
def test_unicode_survives_statistics(name: str, text: str) -> None:
    stats = stats_for_text(text)

    assert stats.characters == len(text)
    assert stats.words >= 1


# --------------------------------------------------------------------------
# Import / export (sections 18-19)
# --------------------------------------------------------------------------

def test_decode_handles_utf8_with_and_without_a_bom() -> None:
    body = "नमस्ते world"

    text, encoding, notes = decode_text(body.encode("utf-8"))
    assert text == body
    assert encoding == "utf-8"
    assert notes == []

    text, encoding, notes = decode_text(body.encode("utf-8-sig"))
    assert text == body, "the BOM must not end up inside the script"
    assert encoding == "utf-8-sig"
    assert any("byte-order mark" in note.lower() for note in notes)


def test_decode_handles_utf16_from_windows_editors() -> None:
    """Notepad and PowerShell write UTF-16; it must import cleanly."""
    body = "नमस्ते world"

    text, encoding, _ = decode_text(body.encode("utf-16"))

    assert text == body
    assert "16" in encoding


def test_decode_refuses_binary_instead_of_garbling_it() -> None:
    """Section 18: a wrong file must be rejected, not turned into junk text."""
    with pytest.raises(ValueError):
        decode_text(b"\x00\x01\x02\x03\xff\xfe" * 64)


def test_import_reads_a_text_file(tmp_path: Path) -> None:
    source = tmp_path / "script.txt"
    source.write_text(HINDI, encoding="utf-8")

    result = import_script(source)

    assert result.ok, result.error
    assert result.text == HINDI
    assert result.encoding.lower().replace("-", "") in ("utf8", "utf8sig")


def test_import_reads_markdown_and_its_syntax_can_be_stripped(tmp_path: Path) -> None:
    source = tmp_path / "script.md"
    source.write_text("# Title\n\n**Bold** narration here.\n", encoding="utf-8")

    result = import_script(source)

    assert result.ok, result.error
    assert "**Bold**" in result.text, "import keeps the file exactly as written"

    cleaned = strip_markdown(result.text)
    assert "#" not in cleaned
    assert "**" not in cleaned
    assert "Bold narration here." in cleaned


def test_import_rejects_an_unsupported_extension(tmp_path: Path) -> None:
    source = tmp_path / "movie.mp4"
    source.write_bytes(b"not a script")

    result = import_script(source)

    assert result.ok is False
    assert result.error


def test_import_rejects_a_file_that_is_too_large(tmp_path: Path) -> None:
    source = tmp_path / "huge.txt"
    source.write_text("x" * (MAX_IMPORT_BYTES + 10), encoding="utf-8")

    result = import_script(source)

    assert result.ok is False
    assert "20" in (result.error or "") and "MB" in (result.error or "")


def test_import_reports_a_missing_file_clearly(tmp_path: Path) -> None:
    result = import_script(tmp_path / "absent.txt")

    assert result.ok is False
    assert result.error


def test_export_writes_a_file_and_never_replaces_one(tmp_path: Path) -> None:
    """Section 19: an existing file is never silently overwritten."""
    target = tmp_path / "out.txt"

    first = export_script(target, HINDI)
    assert first.ok, first.error
    assert first.bytes_written == len(HINDI.encode("utf-8"))
    assert target.read_text(encoding="utf-8") == HINDI

    second = export_script(target, "other text")
    assert second.ok is False, "an existing file must never be replaced silently"
    assert "already exists" in (second.error or "")
    assert target.read_text(encoding="utf-8") == HINDI, "the original text is untouched"

    # A different name works, and so does replacing the file deliberately.
    other = tmp_path / "out2.txt"
    assert export_script(other, "other text").ok
    target.unlink()
    assert export_script(target, "other text").ok
    assert target.read_text(encoding="utf-8") == "other text"


def test_export_markdown_keeps_every_field(tmp_path: Path) -> None:
    """Section 19: exporting must not lose the structured metadata."""
    markdown = to_markdown(STRUCTURED)
    target = tmp_path / "script.md"

    result = export_script(target, markdown, target="md")

    assert result.ok, result.error
    written = target.read_text(encoding="utf-8")
    assert "Scene 01" in written
    assert "This is the narration for scene one." in written
    assert "The headline text" in written
    assert "Show a rising chart." in written
    assert "Soft background music." in written
    assert "A soft impact." in written


def test_structured_survives_a_full_round_trip_through_markdown(tmp_path: Path) -> None:
    """structured -> markdown -> structured must not lose narration or visuals."""
    from app.script.io import convert

    as_markdown = convert(STRUCTURED, "markdown")
    back = convert(as_markdown, "structured", markdown_input=True)
    parsed = parse(back)

    narration = " ".join(block.narration for block in parsed.blocks)
    assert "This is the narration for scene one." in narration
    assert "Scene two narration here." in narration
