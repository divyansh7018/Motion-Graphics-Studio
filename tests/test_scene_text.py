"""Stage D - font discovery, measurement and responsive text fitting.

These tests assert *properties* (it fits, it is monotonic, it never rewrites
wording) rather than absolute pixel values, so they pass on a Windows machine
with Segoe UI and on a build machine with only DejaVu.
"""

from __future__ import annotations

import pytest

from app.scene.text import (
    DEFAULT_FONT_FAMILIES,
    MAX_FONT_PIXELS,
    MIN_FONT_PIXELS,
    FitOptions,
    FontResolver,
    fit_text,
    measure_text,
    missing_glyphs,
    split_words,
    wrap_text,
)


@pytest.fixture(scope="module")
def resolver() -> FontResolver:
    return FontResolver()


@pytest.fixture()
def loader(resolver):
    """A font factory that works with whatever fonts this machine has."""

    def factory(size: float, *, bold: bool = False, italic: bool = False):
        return resolver.load(size=int(size), bold=bold, italic=italic)[0]

    return factory


def font_at(resolver, size: int):
    return resolver.load(size=size)[0]


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def test_scanning_is_lazy_so_import_has_no_side_effects():
    resolver = FontResolver()
    assert not resolver.is_scanned  # nothing touched the disk yet
    resolver.families()
    assert resolver.is_scanned


def test_scanning_can_be_disabled_entirely():
    resolver = FontResolver(allow_scan=False)
    assert resolver.matches == []
    font, match, note = resolver.load(size=32)
    assert font is not None
    assert match is None
    assert "bundled font" in note


def test_resolver_caches_so_a_big_font_folder_is_scanned_once():
    resolver = FontResolver()
    first = resolver.matches
    second = resolver.matches
    assert first is second


def test_unknown_family_is_reported_not_silently_swapped(resolver):
    """A font the user asked for but does not have must be named in a note."""
    font, match, note = resolver.load("This Font Does Not Exist 9000", size=32)
    assert font is not None
    assert note
    assert "This Font Does Not Exist 9000" in note


def test_find_returns_none_for_a_family_that_is_not_installed():
    """Guessing a lookalike would hide the substitution from the caller."""
    resolver = FontResolver()
    if not resolver.matches:
        pytest.skip("this machine has no font files")
    assert resolver.find("NoSuchFamilyXYZ123") is None
    assert resolver.find("") is not None  # no request means "give me a default"


def test_no_note_when_the_requested_family_exists(resolver):
    if not resolver.families():
        pytest.skip("this machine has no font files")
    family = resolver.families()[0]
    _font, match, note = resolver.load(family, size=32)
    assert match is not None
    assert note == ""


def test_resolver_returns_a_usable_font_even_with_no_fonts(resolver):
    font, _match, _note = resolver.load(size=32)
    assert measure_text(font, "Hello").width > 0


def test_fallback_chain_starts_with_the_request():
    chain = FontResolver().resolve_fallback_chain("My Custom Font")
    assert chain[0] == "My Custom Font"
    assert chain[1] == DEFAULT_FONT_FAMILIES[0]


def test_loaded_fonts_are_cached_by_size(resolver):
    first, _, _ = resolver.load(size=40)
    second, _, _ = resolver.load(size=40)
    assert first is second
    other, _, _ = resolver.load(size=41)
    assert other is not first


def test_load_clamps_absurd_sizes():
    resolver = FontResolver(allow_scan=False)
    tiny, _, _ = resolver.load(size=-100)
    huge, _, _ = resolver.load(size=999_999)
    assert tiny is not None and huge is not None


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------

def test_measure_uses_real_metrics_and_grows_with_size(resolver):
    small = measure_text(font_at(resolver, 20), "Measure me")
    large = measure_text(font_at(resolver, 60), "Measure me")
    assert large.width > small.width
    assert large.height > small.height


def test_measure_handles_multiline_text(resolver):
    font = font_at(resolver, 32)
    one = measure_text(font, "one line")
    three = measure_text(font, "one\ntwo\nthree")
    assert three.height == pytest.approx(one.height * 3, rel=0.01)


def test_measure_of_empty_text_is_empty(resolver):
    assert measure_text(font_at(resolver, 32), "").is_empty


def test_measure_does_not_crash_on_odd_input(resolver):
    font = font_at(resolver, 32)
    for text in ("\n\n\n", " ", "\u200b", "a" * 500, "\U0001F600"):
        measure_text(font, text)


# --------------------------------------------------------------------------
# Wrapping
# --------------------------------------------------------------------------

def test_split_words_is_lossless(resolver):
    text = "The quick  brown\nfox\tjumps "
    assert "".join(split_words(text)) == text.replace("\r\n", "\n").replace("\r", "\n")


def test_wrap_keeps_every_word(resolver):
    font = font_at(resolver, 32)
    text = "alpha beta gamma delta epsilon zeta"
    lines = wrap_text(font, text, 200)
    # Lines are joined with the newline they replaced, not with nothing.
    assert "\n".join(lines).split() == text.split()


def test_wrap_respects_the_width(resolver):
    font = font_at(resolver, 32)
    text = " ".join(f"word{i}" for i in range(60))
    for line in wrap_text(font, text, 300):
        assert measure_text(font, line).width <= 300 + 1


def test_wrap_breaks_a_word_longer_than_the_box(resolver):
    font = font_at(resolver, 32)
    word = "Supercalifragilisticexpialidocious" * 3
    lines = wrap_text(font, word, 150)
    assert len(lines) > 1
    for line in lines:
        assert measure_text(font, line).width <= 150 + 1
    assert "".join(lines).replace(" ", "") == word.replace(" ", "")


def test_wrap_with_no_width_returns_the_text_unchanged(resolver):
    font = font_at(resolver, 32)
    assert wrap_text(font, "nothing to do", 0) == ["nothing to do"]


def test_wrap_preserves_paragraph_breaks(resolver):
    font = font_at(resolver, 32)
    lines = wrap_text(font, "first paragraph\nsecond paragraph", 5000)
    assert lines == ["first paragraph", "second paragraph"]


# --------------------------------------------------------------------------
# Fitting - the responsive core
# --------------------------------------------------------------------------

@pytest.mark.parametrize("width,height", [(1920, 1080), (1080, 1920), (1080, 1080), (1280, 720), (3840, 2160)])
def test_fitted_text_never_exceeds_its_box(loader, width, height):
    """The central promise: one text block fits at any aspect ratio."""
    from app.scene.canvas import Canvas, Rect

    canvas = Canvas(width, height)
    box = canvas.rect_pixels(Rect(0.1, 0.35, 0.8, 0.2))
    text = ("Responsive text fitting must keep this whole sentence inside the "
            "element at every aspect ratio without rewriting a single word.")
    options = FitOptions(max_size=canvas.scale(0.10), min_size=canvas.scale(0.012), max_lines=4)

    fitted = fit_text(lambda size: loader(size), text, box.width, box.height, options)

    assert not fitted.overflow, fitted.reason
    assert fitted.width <= box.width + 1
    assert fitted.height <= box.height + 1
    assert fitted.line_count <= 4
    assert fitted.font_size <= options.max_size + 0.01


def test_fitting_is_monotonic_in_box_size(loader):
    """A bigger box must never produce smaller text."""
    text = "A sentence of moderate length used to compare two box sizes."
    sizes = []
    for width in (400, 800, 1600):
        options = FitOptions(max_size=200, min_size=6, max_lines=4)
        fitted = fit_text(lambda size: loader(size), text, width, 400, options)
        sizes.append(fitted.font_size)
    assert sizes == sorted(sizes)
    assert sizes[-1] > sizes[0]


def test_fitting_never_rewrites_the_wording(loader):
    text = "Revenue grew 24.5% (up from 19.8%) in Q3 - that's the headline."
    options = FitOptions(max_size=80, min_size=6, max_lines=6)
    fitted = fit_text(lambda size: loader(size), text, 900, 400, options)
    assert " ".join(fitted.lines).split() == text.split()


@pytest.mark.parametrize("text", [
    "English only text.",
    "हिंदी में लिखा गया पाठ।",
    "Mixed हिंदी and English with 1,234 numbers and ₹500 currency.",
    "Punctuation: commas, semi; colons: dashes - and (parentheses).",
    "Quotes 'single' and \"double\" plus an apostrophe's test.",
    "Line one\nLine two\n\nLine four after a blank.",
])
def test_fitting_preserves_unicode_and_punctuation(loader, text):
    """Fitting changes the size, never the wording (directive section 21)."""
    options = FitOptions(max_size=60, min_size=5, max_lines=8)
    fitted = fit_text(lambda size: loader(size), text, 1000, 600, options)
    assert "\n".join(fitted.lines).split() == text.split()


def test_fitting_keeps_hard_line_breaks(loader):
    text = "Line one\nLine two\n\nLine four after a blank."
    fitted = fit_text(lambda s: loader(s), text, 4000, 4000,
                      FitOptions(max_size=60, min_size=6, max_lines=0))
    assert fitted.lines == ["Line one", "Line two", "", "Line four after a blank."]


def test_max_lines_is_enforced_and_reported(loader):
    text = "one two three four five six seven eight nine ten " * 20
    options = FitOptions(max_size=60, min_size=60, max_lines=2)
    fitted = fit_text(lambda size: loader(size), text, 400, 10_000, options)
    assert fitted.line_count <= 2
    assert fitted.overflow
    assert "2" in fitted.reason


def test_overflow_is_reported_when_the_box_is_too_small(loader):
    text = "This sentence is far too long for a box this small to hold."
    options = FitOptions(max_size=60, min_size=58, max_lines=0)
    fitted = fit_text(lambda size: loader(size), text, 60, 20, options)
    assert fitted.overflow
    assert fitted.reason
    assert fitted.font_size == pytest.approx(58, abs=2)


def test_a_zero_sized_box_is_reported_not_crashed(loader):
    fitted = fit_text(lambda size: loader(size), "some text", 0, 0, FitOptions())
    assert fitted.overflow
    assert "no usable size" in fitted.reason.lower()


def test_empty_text_fits_trivially(loader):
    options = FitOptions(max_size=64, min_size=8)
    for text in ("", "   ", "\n\n"):
        fitted = fit_text(lambda size: loader(size), text, 500, 300, options)
        assert not fitted.overflow


def test_allow_grow_lets_a_short_title_expand(loader):
    text = "Hi"
    growing = fit_text(lambda s: loader(s), text, 900, 300,
                       FitOptions(max_size=200, min_size=6, allow_grow=True))
    shrinking = fit_text(lambda s: loader(s), text, 900, 300,
                         FitOptions(max_size=200, min_size=6, allow_grow=False))
    assert growing.font_size >= shrinking.font_size


def test_font_size_is_clamped_to_the_engine_limits(loader):
    fitted = fit_text(lambda s: loader(s), "clamped", 5000, 5000,
                      FitOptions(max_size=MAX_FONT_PIXELS * 10, min_size=MIN_FONT_PIXELS))
    assert MIN_FONT_PIXELS <= fitted.font_size <= MAX_FONT_PIXELS


def test_fit_options_clamp_reversed_sizes():
    options = FitOptions(max_size=10, min_size=50)
    assert options.clamp_size(5) >= MIN_FONT_PIXELS


def test_describe_mentions_the_reason_when_it_overflows(loader):
    fitted = fit_text(lambda s: loader(s), "words " * 50, 100, 20,
                      FitOptions(max_size=40, min_size=38))
    assert "px" in fitted.describe()


# --------------------------------------------------------------------------
# Missing glyphs
# --------------------------------------------------------------------------

def test_missing_glyphs_is_empty_for_plain_ascii(resolver):
    font = font_at(resolver, 32)
    assert missing_glyphs(font, "The quick brown fox 0123 !?.,") == []


def test_missing_glyphs_is_empty_for_empty_text(resolver):
    assert missing_glyphs(font_at(resolver, 32), "") == []


def test_missing_glyphs_reports_a_script_the_font_lacks(resolver):
    """Only meaningful when the resolved font really lacks the script."""
    font = font_at(resolver, 32)
    cache: dict = {}
    result = missing_glyphs(font, "\u0915\u0916\u0917", cache=cache)
    latin = missing_glyphs(font, "abc", cache=cache)
    # Either the font covers Devanagari (no report) or it does not (reported).
    # What must never happen is Latin being reported missing from a Latin font.
    assert latin == []
    assert isinstance(result, list)
    assert all(not ch.isascii() for ch in result)


def test_missing_glyphs_caches_its_probe(resolver):
    font = font_at(resolver, 32)
    cache: dict = {}
    missing_glyphs(font, "abc", cache=cache)
    assert "__notdef__" in cache
