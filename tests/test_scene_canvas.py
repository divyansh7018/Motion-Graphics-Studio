"""Stage D - resolution-independent canvas geometry.

These tests exist to prove the promise in the Stage D brief: *nothing* is
hard-coded to 1080x1920.  Every assertion below is checked across at least
landscape, portrait and square frames.
"""

from __future__ import annotations

import math

import pytest

from app.project.model import FormatSpec
from app.scene.canvas import (
    ANCHOR_POINTS,
    MAX_CANVAS_DIMENSION,
    Canvas,
    PixelRect,
    Rect,
    SafeArea,
    anchor_point,
    clamp01,
    lerp,
    normalise_size,
)

FRAMES = ((1920, 1080), (1080, 1920), (1080, 1080), (1280, 720), (3840, 2160), (864, 1080))


# --------------------------------------------------------------------------
# Canvas
# --------------------------------------------------------------------------

def test_reference_dimension_is_the_short_edge():
    assert Canvas(1920, 1080).reference_dimension == 1080
    assert Canvas(1080, 1920).reference_dimension == 1080
    assert Canvas(1080, 1080).reference_dimension == 1080
    assert Canvas(3840, 2160).reference_dimension == 2160


def test_orientation_is_derived_not_assumed():
    assert Canvas(1920, 1080).orientation == "landscape"
    assert Canvas(1080, 1920).orientation == "portrait"
    assert Canvas(1080, 1080).orientation == "square"


def test_canvas_never_accepts_a_zero_or_negative_size():
    canvas = Canvas(0, -50, 0)
    assert (canvas.width, canvas.height, canvas.fps) == (1, 1, 1)


def test_from_format_reads_the_project_format():
    spec = FormatSpec(width=720, height=1280, fps=60)
    canvas = Canvas.from_format(spec)
    assert (canvas.width, canvas.height, canvas.fps) == (720, 1280, 60)


def test_from_format_tolerates_a_broken_format():
    class Broken:
        width = None
        height = "tall"
        fps = 0

    canvas = Canvas.from_format(Broken())
    # Dimensions clamp to something drawable; fps falls back to a usable rate.
    assert (canvas.width, canvas.height) == (1, 1)
    assert canvas.fps == 30


def test_from_format_accepts_a_missing_format_object():
    canvas = Canvas.from_format(None)
    assert (canvas.width, canvas.height, canvas.fps) == (1, 1, 30)


#: Pixel sizes of shipping video presets.  None of them may appear as a number
#: in the scene engine's code (docstrings may still mention them as examples).
KNOWN_RESOLUTIONS = {1920, 1080, 1280, 720, 3840, 2160, 2560, 1440, 1350, 864, 768}


def _numeric_constants(module) -> set:
    """Every integer literal in a module's *code* (not its prose)."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
            found.add(node.value)
    return found


def test_no_video_resolution_is_hard_coded_in_the_scene_engine():
    """The brief forbids hard-coded 1080x1920 coordinates - enforced here."""
    import app.scene.canvas as canvas_module
    import app.scene.elements as elements_module

    for module in (canvas_module, elements_module):
        literals = _numeric_constants(module)
        offending = literals & KNOWN_RESOLUTIONS
        assert not offending, f"{module.__name__} hard-codes {sorted(offending)}"


def test_scene_engine_source_never_mentions_a_colab_or_content_path():
    """Shipped code must not assume Linux/Colab paths (directive section 54)."""
    from pathlib import Path

    import app.scene.canvas as canvas_module
    import app.scene.elements as elements_module

    for module in (canvas_module, elements_module):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("/content", "/tmp/", "drive/MyDrive"):
            assert forbidden not in source, f"{module.__name__} mentions {forbidden}"


def test_scale_uses_the_reference_dimension_so_sizes_stay_proportional():
    """0.1 means the same fraction of the *narrow* edge in every orientation."""
    assert Canvas(1920, 1080).scale(0.1) == pytest.approx(108.0)
    assert Canvas(1080, 1920).scale(0.1) == pytest.approx(108.0)
    assert Canvas(3840, 2160).scale(0.1) == pytest.approx(216.0)


def test_scale_rejects_garbage():
    canvas = Canvas(1920, 1080)
    assert canvas.scale("wide") == 0.0
    assert canvas.scale(None) == 0.0


@pytest.mark.parametrize("width,height", FRAMES)
def test_pixel_rect_is_always_inside_the_frame(width, height):
    canvas = Canvas(width, height)
    for norm in (Rect(0, 0, 1, 1), Rect(-5, -5, 20, 20), Rect(0.99, 0.99, 0.5, 0.5)):
        box = canvas.rect_pixels(norm)
        assert box.x >= 0 and box.y >= 0
        assert box.x1 <= canvas.width
        assert box.y1 <= canvas.height


def test_thumbnail_keeps_the_aspect_ratio():
    thumb = Canvas(3840, 2160).thumbnail(960)
    assert thumb.long_dimension == 960
    assert thumb.aspect == pytest.approx(3840 / 2160, rel=1e-3)

    portrait = Canvas(1080, 1920).thumbnail(960)
    assert portrait.long_dimension == 960
    assert portrait.width < portrait.height


def test_aspect_label_reduces_exactly():
    assert Canvas(1920, 1080).aspect_label() == "16:9"
    assert Canvas(1080, 1920).aspect_label() == "9:16"
    assert Canvas(1080, 1080).aspect_label() == "1:1"
    assert Canvas(1000, 333).aspect_label() == "1000:333"


def test_extreme_canvas_is_flagged_not_rejected():
    assert not Canvas(1920, 1080).is_extreme
    assert Canvas(MAX_CANVAS_DIMENSION + 1, 1080).is_extreme


def test_describe_mentions_orientation_and_ratio():
    text = Canvas(1080, 1920, 30).describe()
    assert "1080x1920" in text and "9:16" in text and "Portrait" in text


# --------------------------------------------------------------------------
# Safe area
# --------------------------------------------------------------------------

def test_portrait_keeps_more_room_at_top_and_bottom_than_landscape():
    portrait = SafeArea.for_orientation("portrait")
    landscape = SafeArea.for_orientation("landscape")
    assert portrait.top > landscape.top
    assert portrait.bottom > landscape.bottom


def test_safe_area_rect_stays_inside_the_frame():
    for orientation in ("landscape", "portrait", "square"):
        rect = SafeArea.for_orientation(orientation).rect
        assert 0.0 <= rect.x and rect.x1 <= 1.0
        assert 0.0 <= rect.y and rect.y1 <= 1.0
        assert rect.width > 0 and rect.height > 0


def test_safe_area_margin_zero_is_the_whole_frame():
    rect = SafeArea.for_orientation("portrait", margin=0.0).rect
    assert rect == Rect(0.0, 0.0, 1.0, 1.0)


def test_safe_area_clamp_pulls_an_element_back_inside():
    safe = SafeArea.for_orientation("landscape")
    rect = Rect(0.0, 0.0, 1.0, 1.0)
    clamped = safe.clamp(rect)
    assert clamped.x >= safe.left
    assert clamped.y >= safe.top
    assert clamped.x1 <= 1.0 - safe.right


# --------------------------------------------------------------------------
# Rect
# --------------------------------------------------------------------------

def test_anchor_point_covers_the_nine_documented_anchors():
    assert set(ANCHOR_POINTS) == {
        "top-left", "top", "top-right", "left", "center", "right",
        "bottom-left", "bottom", "bottom-right",
    }
    assert anchor_point("top-left") == (0.0, 0.0)
    assert anchor_point("bottom-right") == (1.0, 1.0)


def test_anchor_point_falls_back_to_centre_for_nonsense():
    assert anchor_point("sideways") == (0.5, 0.5)
    assert anchor_point("") == (0.5, 0.5)
    assert anchor_point(None) == (0.5, 0.5)


def test_rect_point_locates_each_anchor():
    rect = Rect(0.25, 0.25, 0.5, 0.5)
    assert rect.point("top-left") == (0.25, 0.25)
    assert rect.point("center") == (0.5, 0.5)
    assert rect.point("bottom-right") == (0.75, 0.75)


def test_rect_intersect_only_where_they_overlap():
    a = Rect(0.0, 0.0, 0.5, 0.5)
    b = Rect(0.25, 0.25, 0.5, 0.5)
    overlap = a.intersect(b)
    assert (overlap.x, overlap.y, overlap.width, overlap.height) == (0.25, 0.25, 0.25, 0.25)
    assert a.overlaps(b)
    assert not a.overlaps(Rect(0.6, 0.6, 0.2, 0.2))


def test_rect_intersect_of_disjoint_boxes_is_empty():
    empty = Rect(0, 0, 0.2, 0.2).intersect(Rect(0.5, 0.5, 0.2, 0.2))
    assert empty.is_empty


def test_rect_inset_shrinks_and_never_leaves_the_frame():
    rect = Rect(0.0, 0.0, 1.0, 1.0).inset(0.1, 0.1, 0.1, 0.1)
    assert rect == Rect(0.1, 0.1, 0.8, 0.8)
    huge = Rect(0.0, 0.0, 1.0, 1.0).inset(-2.0, -2.0, -2.0, -2.0)
    assert huge.x >= 0.0 and huge.x1 <= 1.0


def test_rect_from_center_and_edges():
    centred = Rect.from_center(0.5, 0.5, 0.4, 0.2)
    assert (centred.x, centred.y) == pytest.approx((0.3, 0.4))
    assert (centred.width, centred.height) == pytest.approx((0.4, 0.2))
    assert Rect.from_edges(0.8, 0.2, 0.1, 0.7).rounded(6) == Rect(0.1, 0.2, 0.7, 0.5)


def test_rect_union_ignores_empty_parts():
    rect = Rect(0.1, 0.1, 0.2, 0.2)
    assert rect.union(Rect(0, 0, 0, 0)) == rect


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def test_clamp01_handles_garbage():
    assert clamp01("x") == 0.0
    assert clamp01(float("nan")) == 0.0
    assert clamp01(5) == 1.0
    assert clamp01(-5) == 0.0
    assert clamp01(0.4) == pytest.approx(0.4)


def test_lerp_interpolates_and_clamps():
    assert lerp(0.0, 10.0, 0.5) == 5.0
    assert lerp(0.0, 10.0, 5.0) == 10.0
    assert lerp(0.0, 10.0, -1.0) == 0.0


def test_pixel_rect_box_is_pillow_shaped():
    assert PixelRect(10, 20, 30, 40).box == (10, 20, 40, 60)
    assert PixelRect(0, 0, 0, 0).is_empty


# --------------------------------------------------------------------------
# normalise_size - the Stage B compatibility surface
# --------------------------------------------------------------------------

def test_normalise_size_reads_the_stage_b_single_value_form():
    result = normalise_size({"mode": "relative", "value": 0.12})
    assert result["width"] == {"mode": "fraction", "value": 0.12}
    assert result["height"]["mode"] == "auto"


def test_normalise_size_reads_explicit_axes():
    result = normalise_size({
        "width": {"mode": "fill", "value": 0},
        "height": {"mode": "fraction", "value": 0.3},
    })
    assert result["width"]["mode"] == "fill"
    assert result["height"] == {"mode": "fraction", "value": 0.3}


def test_normalise_size_accepts_bare_numbers():
    result = normalise_size({"width": 0.4, "height": 0.2})
    assert result["width"] == {"mode": "fraction", "value": 0.4}
    assert result["height"] == {"mode": "fraction", "value": 0.2}


def test_normalise_size_survives_garbage():
    for value in (None, {}, {"mode": "nonsense"}, {"mode": "pixels", "value": "wide"}, "text"):
        result = normalise_size(value)
        assert set(result) == {"width", "height"}
        for axis in result.values():
            assert axis["mode"] in ("fraction", "design", "relative", "pixels", "fill", "auto")


def test_normalise_size_pixels_mode_is_honoured_exactly():
    result = normalise_size({"mode": "pixels", "value": 240})
    assert result["width"] == {"mode": "pixels", "value": 240.0}


def test_frame_seconds_matches_fps():
    assert Canvas(1920, 1080, 30).frame_seconds == pytest.approx(1 / 30)
    assert Canvas(1920, 1080, 60).frame_seconds == pytest.approx(1 / 60)


def test_megapixels_is_reported_for_memory_warnings():
    assert Canvas(1920, 1080).megapixels == pytest.approx(2.074, rel=1e-3)
    assert math.isfinite(Canvas(8192, 8192).megapixels)
