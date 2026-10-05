"""Kokoro detection, voices, preprocessing, audio and cache keys (sections 3-10, 20-27, 33, 42).

These tests never need the real Kokoro weights.  Discovery is exercised against a
synthetic model folder built in ``tmp_path``, which is the point: the voice list
must come from the files on disk, not from a table in the code.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from app.tts.audio import (
    duration_seconds,
    float_to_pcm16,
    format_seconds,
    read_wav,
    validate_wav,
    write_wav,
)
from app.tts.cache import cache_key, compare, settings_hash, source_hash
from app.tts.capabilities import (
    probe_kokoro,
    probe_model,
    probe_runtime,
    requirements_summary,
)
from app.tts.engine import (
    MAX_SPEED,
    MIN_SPEED,
    GenerationRequest,
    KokoroEngine,
    apply_volume,
    clamp_speed,
    clamp_volume,
)
from app.tts.preprocess import (
    PreprocessOptions,
    chunk_text,
    integer_to_words,
    preprocess,
)
from app.tts.voices import (
    discover_voices,
    filter_voices,
    language_label,
    validate_voice_choice,
)

#: A realistic Kokoro layout: one model file plus a voices folder.
VOICE_IDS = [
    "af_bella", "af_heart", "af_nicole",
    "am_adam", "am_michael",
    "bf_emma", "bm_george",
    "hf_alpha", "hm_ishaan",
    "jf_kohaku", "zf_xiaobei",
]


@pytest.fixture()
def kokoro_dir(tmp_path: Path) -> Path:
    """A synthetic model folder shaped like a real Kokoro installation."""
    root = tmp_path / "models" / "kokoro"
    (root / "voices").mkdir(parents=True)
    (root / "kokoro-82m-v1.0.onnx").write_bytes(b"ONNXFAKE" * 4096)
    for voice in VOICE_IDS:
        (root / "voices" / f"{voice}.pt").write_bytes(b"VOICEFAKE" * 64)
    return root


@pytest.fixture()
def gender_metadata(kokoro_dir: Path) -> Path:
    (kokoro_dir / "voices" / "voices.json").write_text(
        json.dumps({
            "af_bella": {"gender": "female"},
            "am_adam": {"gender": "male"},
        }),
        encoding="utf-8",
    )
    return kokoro_dir


# --------------------------------------------------------------------------
# Capability detection (sections 3, 10, 18, 22)
# --------------------------------------------------------------------------

def test_a_missing_engine_is_reported_not_pretended_away() -> None:
    """Section 4: never claim Kokoro works because a package imported."""
    status = probe_kokoro(model_dir=Path("/nonexistent-kokoro-dir"))

    assert status.ready is False
    assert status.verified is False
    assert status.headline()
    assert status.problems, "the user must be told what is wrong"
    assert status.instructions, "and how to fix it"


def test_model_and_voices_are_found_on_disk(kokoro_dir: Path) -> None:
    model = probe_model(kokoro_dir)

    assert model.present is True
    assert model.path is not None and model.path.name.startswith("kokoro-82m")
    assert model.size_bytes > 0
    assert len(model.voice_files) == len(VOICE_IDS)


def test_voice_count_comes_from_the_files_not_from_the_code(kokoro_dir: Path) -> None:
    """Sections 5-6: adding a voice file must add a voice, with no code change."""
    before = probe_kokoro(model_dir=kokoro_dir)
    assert len(before.voices) == len(VOICE_IDS)

    (kokoro_dir / "voices" / "pf_newvoice.pt").write_bytes(b"VOICEFAKE" * 64)
    after = probe_kokoro(model_dir=kokoro_dir)

    assert len(after.voices) == len(VOICE_IDS) + 1
    assert "pf_newvoice" in after.voices


def test_removing_a_voice_file_removes_it_from_the_catalogue(kokoro_dir: Path) -> None:
    (kokoro_dir / "voices" / "af_heart.pt").unlink()

    status = probe_kokoro(model_dir=kokoro_dir)

    assert "af_heart" not in status.voices
    assert len(status.voices) == len(VOICE_IDS) - 1


def test_languages_are_derived_from_the_installed_catalogue(kokoro_dir: Path) -> None:
    status = probe_kokoro(model_dir=kokoro_dir)

    assert status.languages, "languages must be discovered, never hard-coded"
    assert status.language_source in ("engine", "voice-id prefix")
    # Hindi voices exist in the fixture, so a Hindi code must be reported.
    assert any(code.startswith("h") for code in status.languages)


def test_a_model_without_voices_is_reported_as_unusable(kokoro_dir: Path) -> None:
    for voice_file in (kokoro_dir / "voices").iterdir():
        voice_file.unlink()

    status = probe_kokoro(model_dir=kokoro_dir)

    assert status.voices == []
    assert any("voice" in problem.lower() for problem in status.problems)


def test_runtime_probe_never_claims_a_gpu_is_needed() -> None:
    info = probe_runtime()

    assert "cuda" not in info.note.lower()
    assert "gpu" not in info.note.lower()


def test_requirements_summary_is_serialisable(kokoro_dir: Path) -> None:
    summary = requirements_summary(probe_kokoro(model_dir=kokoro_dir))

    json.dumps(summary)  # must not raise
    assert summary["voice_count"] == len(VOICE_IDS)
    assert summary["engine"] == "kokoro"


# --------------------------------------------------------------------------
# Voice catalogue (sections 5-8, 33)
# --------------------------------------------------------------------------

def test_voices_are_listed_even_when_the_engine_cannot_run(kokoro_dir: Path) -> None:
    """Section 33: the user must see what was found, and why it cannot be used."""
    catalogue = discover_voices(status=probe_kokoro(model_dir=kokoro_dir))

    assert catalogue.count == len(VOICE_IDS)
    assert catalogue.blocker(), "an unusable catalogue must say why"
    assert all(voice.available is False for voice in catalogue.voices)
    assert all(voice.note for voice in catalogue.voices)


def test_a_voice_whose_file_is_missing_is_marked_unavailable(kokoro_dir: Path) -> None:
    (kokoro_dir / "voices" / "af_heart.pt").unlink()

    catalogue = discover_voices(status=probe_kokoro(model_dir=kokoro_dir))
    missing = catalogue.find("af_heart")

    assert missing is None or missing.available is False
    assert catalogue.count == len(VOICE_IDS) - 1


def test_language_filter_narrows_the_list(kokoro_dir: Path) -> None:
    catalogue = discover_voices(status=probe_kokoro(model_dir=kokoro_dir))

    hindi = filter_voices(catalogue, language="h")
    english = filter_voices(catalogue, language="a")

    assert {v.id for v in hindi} == {"hf_alpha", "hm_ishaan"}
    assert {v.id for v in english} == {"af_bella", "af_heart", "af_nicole", "am_adam", "am_michael"}
    assert len(filter_voices(catalogue)) == len(VOICE_IDS)


def test_gender_filter_and_search_work_together(kokoro_dir: Path) -> None:
    catalogue = discover_voices(status=probe_kokoro(model_dir=kokoro_dir))

    assert [v.id for v in filter_voices(catalogue, language="h", gender="male")] == ["hm_ishaan"]
    assert [v.id for v in filter_voices(catalogue, language="h", gender="female")] == ["hf_alpha"]
    assert [v.id for v in filter_voices(catalogue, search="bella")] == ["af_bella"]
    assert filter_voices(catalogue, search="zzzznotavoice") == []


def test_published_gender_metadata_beats_the_heuristic(gender_metadata: Path) -> None:
    catalogue = discover_voices(status=probe_kokoro(model_dir=gender_metadata))

    assert catalogue.find("af_bella").gender == "female"
    assert catalogue.find("am_adam").gender == "male"
    # Voices with no published entry still get a best-effort answer.
    assert catalogue.find("hm_ishaan").gender == "male"


def test_a_language_change_clears_an_incompatible_voice(kokoro_dir: Path) -> None:
    """Section 7: switching language must not leave a mismatched voice selected.

    The fixture has no engine installed, so every discovered voice is
    unavailable and the availability check fires first.  To reach the language
    rule the voices are marked available, which is what a working install looks
    like.
    """
    catalogue = discover_voices(status=probe_kokoro(model_dir=kokoro_dir))
    for voice in catalogue.voices:
        voice.available = True
        voice.note = ""

    ok, message = validate_voice_choice(catalogue, "af_bella", "a")
    assert ok is True, message

    blocked, message = validate_voice_choice(catalogue, "af_bella", "h")
    assert blocked is False
    assert "does not support" in message
    assert "English" in language_label("a")


def test_unknown_and_empty_voices_are_validation_errors(kokoro_dir: Path) -> None:
    catalogue = discover_voices(status=probe_kokoro(model_dir=kokoro_dir))

    ok, message = validate_voice_choice(catalogue, "no_such_voice")
    assert ok is False and "not in the installed Kokoro catalogue" in message

    ok, message = validate_voice_choice(catalogue, "")
    assert ok is False and "No narration voice is selected" in message


def test_an_empty_model_folder_explains_itself(tmp_path: Path) -> None:
    catalogue = discover_voices(status=probe_kokoro(model_dir=tmp_path))

    assert catalogue.count == 0
    assert catalogue.reason, "an empty list must come with a reason"


def test_language_labels_fall_back_to_the_code() -> None:
    assert language_label("hi") == "Hindi"
    assert language_label("qq") == "qq", "an unknown code is shown as-is, never invented"
    assert language_label("") == "Not specified"


# --------------------------------------------------------------------------
# Preprocessing (section 20)
# --------------------------------------------------------------------------

def test_default_preprocessing_never_changes_the_words() -> None:
    text = "Invest Rs. 500 at 12% interest in 2020."

    result = preprocess(text)

    assert result.text == text
    assert "Rs." in result.text and "500" in result.text and "12%" in result.text


def test_whitespace_and_line_endings_are_cleaned() -> None:
    result = preprocess("Hello   world.\r\n\r\n\r\nNext  paragraph.\nline inside.")

    assert "   " not in result.text
    assert "\r" not in result.text
    assert result.text.count("\n") == 1, "one paragraph break, kept as a pause"
    assert result.changes, "the user is told what was changed"


def test_numbers_are_written_out_only_when_asked() -> None:
    text = "Invest Rs. 500 at 12% interest."

    assert "500" in preprocess(text).text
    expanded = preprocess(text, PreprocessOptions(expand_numbers=True)).text

    assert "five hundred" in expanded
    assert "twelve percent" in expanded
    assert "interest." in expanded, "abbreviations must not match inside a word"


def test_indian_grouping_is_available() -> None:
    text = "1,00,000 people"

    western = preprocess(text, PreprocessOptions(expand_numbers=True)).text
    indian = preprocess(text, PreprocessOptions(expand_numbers=True, number_style="indian")).text

    assert "1,00,000" in western, "western grouping leaves indian grouping alone"
    assert "one lakh" in indian


def test_decimals_and_version_numbers_are_never_expanded() -> None:
    text = "Version 2.5 at 04:30 with ratio 3:4."

    expanded = preprocess(text, PreprocessOptions(expand_numbers=True)).text

    assert "2.5" in expanded
    assert "04:30" in expanded
    assert "3:4" in expanded


def test_integer_to_words_is_correct_in_both_styles() -> None:
    assert integer_to_words(0) == "zero"
    assert integer_to_words(21) == "twenty one"
    assert integer_to_words(105000) == "one hundred five thousand"
    assert integer_to_words(105000, style="indian") == "one lakh five thousand"
    assert integer_to_words(-7) == "minus seven"


def test_hindi_text_is_never_rewritten_by_preprocessing() -> None:
    text = "नमस्ते।  यह   परीक्षण है।\n\nदूसरा अनुच्छेद।"

    result = preprocess(text)

    assert "नमस्ते।" in result.text
    assert "दूसरा अनुच्छेद।" in result.text
    assert "   " not in result.text


def test_markdown_is_kept_by_default_and_removed_on_request() -> None:
    text = "# Title\n\n**Bold** words and [a link](http://x)."

    assert "**Bold**" in preprocess(text).text
    stripped = preprocess(text, PreprocessOptions(strip_markdown=True)).text
    assert "**" not in stripped and "# " not in stripped
    assert "Bold" in stripped and "a link" in stripped


def test_chunking_never_splits_a_sentence() -> None:
    text = "Sentence one is here. " * 40

    chunks = chunk_text(text, 120)

    assert len(chunks) > 1
    assert max(len(chunk) for chunk in chunks) <= 120
    assert all(chunk.rstrip().endswith(".") for chunk in chunks)


def test_chunking_returns_the_text_unchanged_when_it_is_short() -> None:
    assert chunk_text("Short text.", 400) == ["Short text."]
    assert chunk_text("", 400) == []
    assert chunk_text("No limit text.", 0) == ["No limit text."]


# --------------------------------------------------------------------------
# Speed / volume limits (sections 25-26)
# --------------------------------------------------------------------------

def test_speed_is_clamped_to_the_engine_range() -> None:
    assert clamp_speed(3.0) == MAX_SPEED
    assert clamp_speed(0.1) == MIN_SPEED
    assert clamp_speed(1.0) == 1.0
    assert clamp_speed(float("nan")) == 1.0
    assert clamp_speed("bad") == 1.0


def test_volume_is_limited_to_125_percent() -> None:
    assert clamp_volume(5.0) == 1.25
    assert clamp_volume(-1.0) == 0.0
    assert clamp_volume(1.0) == 1.0


def test_loud_volume_cannot_clip() -> None:
    samples = np.full(1000, 0.9, dtype=np.float32)

    boosted = apply_volume(samples, 1.25)

    # float32 cannot hold 0.99 exactly, so compare with a small tolerance.
    assert float(np.max(np.abs(boosted))) <= 0.99 + 1e-6
    assert float(np.max(np.abs(apply_volume(samples, 0.0)))) == 0.0


# --------------------------------------------------------------------------
# Engine construction (no model needed)
# --------------------------------------------------------------------------

def test_building_an_engine_does_no_work() -> None:
    """Sections 52/56: importing and constructing must not load a model."""
    engine = KokoroEngine()

    assert engine.loaded is False
    assert engine.backend == ""


def test_loading_without_a_model_gives_a_friendly_error() -> None:
    from app.tts.engine import KokoroError

    engine = KokoroEngine(model_path=Path("/nonexistent/model.onnx"))

    with pytest.raises(KokoroError) as raised:
        engine.load()

    friendly = raised.value.friendly()
    assert friendly.what_happened
    assert friendly.actions


def test_synthesizing_without_a_voice_is_refused() -> None:
    from app.tts.engine import KokoroError

    engine = KokoroEngine()

    with pytest.raises(KokoroError):
        engine.synthesize(GenerationRequest(text="Hello.", voice=""))


def test_synthesizing_without_text_is_refused() -> None:
    from app.tts.engine import KokoroError

    engine = KokoroEngine()

    with pytest.raises(KokoroError):
        engine.synthesize(GenerationRequest(text="   ", voice="af_bella"))


# --------------------------------------------------------------------------
# WAV writing and validation (sections 23-24, 40)
# --------------------------------------------------------------------------

@pytest.fixture()
def tone() -> np.ndarray:
    rate = 24000
    t = np.linspace(0, 2.0, rate * 2, endpoint=False, dtype=np.float32)
    return (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def test_a_written_wav_is_real_readable_audio(tmp_path: Path, tone: np.ndarray) -> None:
    target = tmp_path / "narration_full.wav"

    info = write_wav(target, tone, 24000)

    assert info.valid, info.problems
    assert target.exists()
    assert info.sample_rate == 24000
    assert info.channels == 1
    assert info.size_bytes == target.stat().st_size
    assert abs(info.duration_seconds - 2.0) < 0.01


def test_audio_can_be_read_back(tmp_path: Path, tone: np.ndarray) -> None:
    target = tmp_path / "n.wav"
    write_wav(target, tone, 24000)

    samples, rate = read_wav(target)

    assert rate == 24000
    assert len(samples) == len(tone)
    assert float(np.max(np.abs(samples[:500] - tone[:500]))) < 1e-3


def test_duration_is_measured_from_the_file(tmp_path: Path, tone: np.ndarray) -> None:
    target = tmp_path / "n.wav"
    write_wav(target, tone, 24000)

    assert abs(duration_seconds(target) - 2.0) < 0.01


def test_a_truncated_file_is_never_accepted(tmp_path: Path, tone: np.ndarray) -> None:
    """An interrupted write must fail validation, not look like audio."""
    target = tmp_path / "n.wav"
    write_wav(target, tone, 24000)
    full = target.read_bytes()

    for cut in (64, len(full) // 2):
        broken = tmp_path / f"cut{cut}.wav"
        broken.write_bytes(full[:cut])
        info = validate_wav(broken)
        assert info.valid is False, f"a {cut}-byte file must not validate"
        assert info.problems


def test_empty_missing_and_junk_files_are_all_reported(tmp_path: Path) -> None:
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"not a wav file at all" * 4)

    assert "empty" in validate_wav(empty).problems[0].lower()
    assert validate_wav(tmp_path / "absent.wav").exists is False
    assert validate_wav(junk).readable is False


def test_a_silent_stub_is_treated_as_a_failure(tmp_path: Path) -> None:
    """Section 40: a file too short to be speech means generation failed."""
    target = tmp_path / "tiny.wav"
    write_wav(target, np.zeros(240, dtype=np.float32), 24000)

    info = validate_wav(target)

    assert info.valid is False
    assert any("seconds" in problem for problem in info.problems)


def test_writing_empty_samples_is_refused(tmp_path: Path) -> None:
    from app.tts.audio import AudioError

    with pytest.raises(AudioError):
        write_wav(tmp_path / "n.wav", np.zeros(0, dtype=np.float32), 24000)


def test_overwrite_can_be_refused(tmp_path: Path, tone: np.ndarray) -> None:
    from app.tts.audio import AudioError

    target = tmp_path / "n.wav"
    write_wav(target, tone, 24000)

    with pytest.raises(AudioError):
        write_wav(target, tone, 24000, overwrite=False)


def test_float_to_pcm16_clips_instead_of_wrapping() -> None:
    data = float_to_pcm16(np.array([2.0, -2.0, 0.0], dtype=np.float32))

    assert len(data) == 6
    values = np.frombuffer(data, dtype="<i2")
    assert values[0] > 0 and values[1] < 0


def test_format_seconds_is_readable() -> None:
    assert format_seconds(3.2) == "3.20s"
    assert format_seconds(83) == "1:23"
    assert format_seconds(3725) == "1:02:05"
    assert format_seconds(-5) == "0.00s"


# --------------------------------------------------------------------------
# Cache keys and staleness (sections 27, 42)
# --------------------------------------------------------------------------

def test_the_same_inputs_produce_the_same_key() -> None:
    kwargs = dict(voice="af_bella", language="a", speed=1.0, volume=1.0,
                  model_version="v1", preprocessing={"a": 1})

    assert cache_key(text="Hello.", **kwargs) == cache_key(text="Hello.", **kwargs)


def test_every_input_changes_the_key() -> None:
    base = dict(text="Hello.", voice="af_bella", language="a", speed=1.0,
                volume=1.0, model_version="v1", preprocessing={})
    original = cache_key(**base)

    for change in (
        {"text": "Hello!"},
        {"voice": "am_adam"},
        {"language": "h"},
        {"speed": 1.25},
        {"volume": 0.9},
        {"model_version": "v2"},
        {"preprocessing": {"expand_numbers": True}},
    ):
        assert cache_key(**{**base, **change}) != original, change


def test_equivalent_unicode_spellings_hash_the_same() -> None:
    """Two byte sequences that read identically must count as the same script.

    A script pasted from one editor and retyped in another can differ only in
    Unicode composition.  Without normalisation that would mark good narration
    stale for no visible reason (section 42).
    """
    import unicodedata

    composed = "caf\u00e9"          # é as one code point
    decomposed = "cafe\u0301"      # e + combining acute

    assert composed != decomposed, "the test needs two distinct byte sequences"
    assert unicodedata.normalize("NFC", decomposed) == composed
    assert source_hash(composed) == source_hash(decomposed)
    assert cache_key(text=composed, voice="v", language="l", speed=1.0, volume=1.0) == \
        cache_key(text=decomposed, voice="v", language="l", speed=1.0, volume=1.0)


def test_hindi_hashes_are_stable_across_a_round_trip() -> None:
    """Devanagari has no NFC/NFD variants, but it must still hash identically."""
    text = "नमस्ते दोस्तों। आज हम बात करेंगे निवेश की।"

    assert source_hash(text) == source_hash(str(text))
    assert source_hash(text) != source_hash(text + " ")


def test_staleness_detects_a_script_change() -> None:
    stored = source_hash("Original script.")

    report = compare(text="Changed script.", stored_source=stored,
                     voice="v", language="l", speed=1.0, volume=1.0,
                     stored_settings=settings_hash(voice="v", language="l", speed=1.0, volume=1.0))

    assert report.stale is True
    assert any("script" in reason for reason in report.reasons)


def test_staleness_detects_a_settings_change() -> None:
    text = "Same script."
    stored_source = source_hash(text)
    stored_settings = settings_hash(voice="v", language="l", speed=1.0, volume=1.0)

    report = compare(text=text, stored_source=stored_source,
                     voice="v", language="l", speed=1.5, volume=1.0,
                     stored_settings=stored_settings)

    assert report.stale is True
    assert any("voice" in reason or "speed" in reason for reason in report.reasons)


def test_unchanged_inputs_are_not_stale() -> None:
    text = "Same script."

    report = compare(
        text=text,
        stored_source=source_hash(text),
        voice="v", language="l", speed=1.0, volume=1.0,
        stored_settings=settings_hash(voice="v", language="l", speed=1.0, volume=1.0),
    )

    assert report.stale is False
    assert report.reasons == []


def test_a_track_with_no_recorded_details_is_stale() -> None:
    report = compare(text="Anything.", stored_source="", voice="v", language="l",
                     speed=1.0, volume=1.0, stored_settings="")

    assert report.stale is True
    assert "no recorded generation details" in report.reasons
