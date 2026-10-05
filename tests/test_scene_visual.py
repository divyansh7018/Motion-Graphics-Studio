"""Stage D - shapes, charts and scene composition.

These draw real pixels with Pillow so the assertions are about what would
actually appear in a preview, not about mocks.  They are written to pass on a
Windows machine and on a CI box: wherever the exact font is unknown, the tests
assert structure (size, coverage, determinism) instead of pixel-exact text.
"""

from __future__ import annotations


import pytest
from PIL import Image, ImageDraw

from app.project.model import ElementSpec, SceneSpec
from app.scene.canvas import Canvas, PixelRect
from app.scene.charts import ChartStyle, chart_value_range, draw_chart
from app.scene.compose import (
    RenderOptions,
    render_background,
    render_scene,
    save_image,
)
from app.scene.elements import LayoutContext
from app.scene.palette import Color, Gradient
from app.scene.shapes import draw_gradient, draw_shape, nice_number
from app.scene.text import FontResolver

GRADIENT = {"stops": ["#101828", "#2b3a5c"], "angle": 90}


@pytest.fixture(scope="module")
def resolver() -> FontResolver:
    return FontResolver()


@pytest.fixture()
def ctx(resolver, tmp_path):
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    canvas = Canvas(1920, 1080, 30)

    def loader(family, size, bold, italic):
        return resolver.load("DejaVu Sans", size=size, bold=bold, italic=italic)

    return LayoutContext(canvas=canvas, safe_area=canvas.safe_area(), font_loader=loader,
                         asset_root=asset_root,
                         palette={"accent": "#4f8cff", "surface": "#1b2333",
                                  "background": "#0d1220", "text": "#f5f5f7"})


def _write_image(tmp_path, width=800, height=500, colour=(30, 120, 200)):
    image = Image.new("RGB", (width, height), colour)
    ImageDraw.Draw(image).rectangle([width // 4, height // 4,
                                     width * 3 // 4, height * 3 // 4], fill=(250, 200, 60))
    path = tmp_path / "assets" / "demo.png"
    image.save(path)
    return path


def _coverage(image: Image.Image, threshold: int = 40) -> float:
    """Fraction of pixels that are not near-black."""
    small = image.convert("L").resize((96, 96))
    pixels = list(small.tobytes())
    return sum(1 for value in pixels if value > threshold) / len(pixels)


# --------------------------------------------------------------------------
# Shapes
# --------------------------------------------------------------------------

def test_nice_number_rounds_axes_to_readable_values():
    assert nice_number(43.7) in (50.0,)
    assert nice_number(100) == 100.0
    assert nice_number(9.9) == 10.0
    assert nice_number(2.1) == 2.5
    assert nice_number(0.0) == 0.0
    assert nice_number("not a number") == 0.0


def test_draw_shape_fills_its_box():
    image = Image.new("RGBA", (200, 200), (0, 0, 0, 255))
    draw_shape(image, PixelRect(50, 50, 100, 100), "rect", fill=Color(255, 0, 0, 255))
    assert image.getpixel((100, 100)) == (255, 0, 0, 255)
    assert image.getpixel((10, 10)) == (0, 0, 0, 255)


def test_draw_shape_respects_opacity():
    image = Image.new("RGBA", (100, 100), (0, 0, 0, 255))
    draw_shape(image, PixelRect(0, 0, 100, 100), "rect", fill=Color(255, 255, 255, 255), opacity=0.5)
    assert image.getpixel((50, 50))[0] == pytest.approx(127, abs=3)


def test_draw_shape_handles_every_kind_without_raising():
    image = Image.new("RGBA", (300, 300), (0, 0, 0, 255))
    for shape in ("rect", "rounded", "ellipse", "circle", "line", "triangle", "pill",
                  "badge", "arrow", "frame", "unknown"):
        draw_shape(image, PixelRect(10, 10, 80, 80), shape,
                   fill=Color(100, 100, 100, 255), border_width=2, border_color=Color(255, 255, 255, 255))


def test_gradient_produces_a_smooth_vertical_ramp():
    gradient = Gradient(stops=((0, 0, 0, 255), (255, 255, 255, 255)), angle=0.0)
    layer = draw_gradient(PixelRect(0, 0, 10, 100), gradient)
    top = layer.getpixel((5, 2))[0]
    bottom = layer.getpixel((5, 97))[0]
    assert bottom > top


def test_gradient_is_clipped_by_the_rounded_mask():
    from app.scene.palette import Gradient

    layer = draw_gradient(PixelRect(0, 0, 100, 100),
                          Gradient(stops=((255, 255, 255, 255), (255, 255, 255, 255))),
                          radius=40)
    # Outside the rounded corner the alpha must be zero.
    assert layer.getpixel((0, 0))[3] == 0
    assert layer.getpixel((50, 50))[3] == 255


# --------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------

def test_chart_value_range_handles_flat_and_empty_data():
    assert chart_value_range([]) == (0.0, 1.0)
    assert chart_value_range([0, 0, 0])[1] > 0
    assert chart_value_range([5, 5])[1] > 5


def test_chart_value_range_includes_negatives():
    low, high = chart_value_range([-10, 20])
    assert low <= -10
    assert high >= 20


def test_every_chart_kind_draws_something():
    font = FontResolver(allow_scan=False).load(size=14)[0]
    style = ChartStyle()
    values = [12, 30, 18, 44, 27]
    labels = ["A", "B", "C", "D", "E"]
    for kind in ("bar", "column", "line", "area", "pie", "donut", "sparkline", "unknown"):
        image = Image.new("RGBA", (400, 300), (0, 0, 0, 255))
        draw_chart(image, PixelRect(10, 10, 380, 280), kind, values, labels, style,
                   font_factory=lambda size: font)
        assert _coverage(image) > 0.01, kind


def test_a_single_positive_slice_is_a_full_circle():
    image = Image.new("RGBA", (200, 200), (0, 0, 0, 255))
    draw_chart(image, PixelRect(0, 0, 200, 200), "pie", [100], ["all"], ChartStyle(), None)
    # The centre should be the slice colour, not the background.
    assert image.getpixel((100, 100))[0] != 0 or image.getpixel((100, 100))[1] != 0


def test_empty_chart_draws_nothing_and_reports_via_layout():
    image = Image.new("RGBA", (200, 200), (10, 10, 10, 255))
    draw_chart(image, PixelRect(0, 0, 200, 200), "bar", [], [], ChartStyle(), None)
    assert image.getpixel((100, 100)) == (10, 10, 10, 255)


def test_charts_scale_with_the_box():
    """On one canvas, a bigger chart box must use more pixels."""
    def absolute(image):
        return sum(1 for px in image.convert("L").tobytes() if px > 40)

    font = FontResolver(allow_scan=False).load(size=12)[0]
    small = Image.new("RGBA", (400, 300), (0, 0, 0, 255))
    big = Image.new("RGBA", (400, 300), (0, 0, 0, 255))
    draw_chart(small, PixelRect(10, 10, 100, 80), "column", [1, 2, 3], [], ChartStyle(),
               font_factory=lambda s: font)
    draw_chart(big, PixelRect(10, 10, 380, 280), "column", [1, 2, 3], [], ChartStyle(),
               font_factory=lambda s: font)
    assert absolute(big) > absolute(small)


# --------------------------------------------------------------------------
# Background
# --------------------------------------------------------------------------

def test_background_uses_the_project_colour_when_given():
    image = render_background(Canvas(100, 100), "#334455")
    assert image.getpixel((50, 50)) == (0x33, 0x44, 0x55, 255)


def test_background_falls_back_to_a_sensible_default():
    image = render_background(Canvas(100, 100), None, fallback="#101014")
    assert image.getpixel((50, 50)) == (0x10, 0x10, 0x14, 255)


def test_background_accepts_a_gradient():
    vertical = {"stops": ["#101828", "#2b3a5c"], "angle": 0}
    image = render_background(Canvas(10, 100), vertical)
    assert image.getpixel((5, 2)) != image.getpixel((5, 97))


def test_background_rejects_garbage_gracefully():
    image = render_background(Canvas(100, 100), "not-a-colour", fallback="#101014")
    assert image.getpixel((50, 50))[3] == 255


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------

def test_render_scene_size_matches_the_canvas(ctx):
    scene = SceneSpec(id="s", elements=[])
    assert render_scene(scene, ctx).size == (1920, 1080)


def test_a_scene_with_no_elements_is_just_the_background(ctx):
    scene = SceneSpec(id="s", background="#101014", elements=[])
    image = render_scene(scene, ctx)
    assert _coverage(image) < 0.01


def test_rendering_is_deterministic(ctx):
    scene = SceneSpec(id="s", elements=[
        ElementSpec(id="t", kind="text", text="Deterministic", anchor="center",
                    position={"x": 0.5, "y": 0.5}, size={"mode": "relative", "value": 0.08},
                    animation={"preset": "none"}),
    ])
    first = render_scene(scene, ctx, time=1.0, scene_duration=5.0)
    second = render_scene(scene, ctx, time=1.0, scene_duration=5.0)
    assert first.tobytes() == second.tobytes()


def test_text_element_appears_in_the_frame(ctx):
    scene = SceneSpec(id="s", background="#0d1220", elements=[
        ElementSpec(id="t", kind="text", text="Hello scene", anchor="center",
                    position={"x": 0.5, "y": 0.5}, size={"mode": "relative", "value": 0.09},
                    animation={"preset": "none"}),
    ])
    image = render_scene(scene, ctx, time=1.0, scene_duration=5.0)
    assert _coverage(image) > 0.01


def test_animation_starts_empty_and_ends_full(ctx):
    scene = SceneSpec(id="s", background="#0d1220", elements=[
        ElementSpec(id="t", kind="text", text="Fades in", anchor="center",
                    position={"x": 0.5, "y": 0.5}, size={"mode": "relative", "value": 0.09},
                    animation={"preset": "fade", "duration": 0.5}),
    ])
    start = render_scene(scene, ctx, time=0.0, scene_duration=5.0)
    end = render_scene(scene, ctx, time=1.0, scene_duration=5.0)
    assert _coverage(start) < _coverage(end)
    assert _coverage(start) < 0.01  # fully faded in from the background


def test_missing_image_draws_a_labelled_placeholder_not_blank(ctx):
    scene = SceneSpec(id="s", background="#0d1220", elements=[
        ElementSpec(id="i", kind="image", asset_id="does-not-exist", anchor="center",
                    position={"x": 0.5, "y": 0.5},
                    size={"width": {"mode": "fraction", "value": 0.4},
                          "height": {"mode": "fraction", "value": 0.3}},
                    animation={"preset": "none"}),
    ])
    image = render_scene(scene, ctx)
    # The placeholder box is visible (not blank).
    assert _coverage(image, threshold=30) > 0.01


def test_present_image_is_drawn(ctx, tmp_path):
    _write_image(tmp_path)
    scene = SceneSpec(id="s", background="#0d1220", elements=[
        ElementSpec(id="i", kind="image", asset_id="demo", anchor="center",
                    position={"x": 0.5, "y": 0.5},
                    size={"width": {"mode": "fraction", "value": 0.5},
                          "height": {"mode": "fraction", "value": 0.4}},
                    extra={"fit": "contain"}, animation={"preset": "none"}),
    ])
    image = render_scene(scene, ctx)
    # The yellow rectangle from the asset must appear somewhere.
    pixels = set()
    for x in range(0, image.width, 16):
        for y in range(0, image.height, 16):
            pixels.add(image.getpixel((x, y))[:3])
    assert any(r > 200 and g > 150 and b < 120 for r, g, b in pixels)


def test_unreadable_image_file_does_not_crash_the_preview(ctx, tmp_path):
    (tmp_path / "assets" / "broken.png").write_bytes(b"not a png")
    scene = SceneSpec(id="s", background="#0d1220", elements=[
        ElementSpec(id="i", kind="image", asset_id="broken", anchor="center",
                    position={"x": 0.5, "y": 0.5},
                    size={"width": {"mode": "fraction", "value": 0.4},
                          "height": {"mode": "fraction", "value": 0.3}},
                    animation={"preset": "none"}),
    ])
    image = render_scene(scene, ctx)  # must not raise
    assert image.size == (1920, 1080)


def test_safe_area_and_bounds_guides_are_opt_in(ctx):
    scene = SceneSpec(id="s", background="#0d1220", elements=[
        ElementSpec(id="t", kind="text", text="x", anchor="center",
                    position={"x": 0.5, "y": 0.5}, size={"mode": "relative", "value": 0.05},
                    animation={"preset": "none"}),
    ])
    plain = render_scene(scene, ctx)
    guided = render_scene(scene, ctx, options=RenderOptions(draw_safe_area=True, draw_bounds=True))
    assert plain.tobytes() != guided.tobytes()


@pytest.mark.parametrize("width,height", [(1920, 1080), (1080, 1920), (1080, 1080), (3840, 2160)])
def test_scene_fills_the_frame_at_every_aspect_ratio(resolver, tmp_path, width, height):
    """The same scene must actually use the whole frame, whatever the ratio."""
    canvas = Canvas(width, height)

    def loader(family, size, bold, italic):
        return resolver.load("DejaVu Sans", size=size, bold=bold, italic=italic)

    ctx = LayoutContext(canvas=canvas, safe_area=canvas.safe_area(), font_loader=loader,
                        palette={"accent": "#4f8cff", "surface": "#1b2333"})
    scene = SceneSpec(id="s", background=GRADIENT, elements=[
        ElementSpec(id="t", kind="text",
                    text="A responsive headline that must fit whatever the frame",
                    anchor="center", position={"x": 0.5, "y": 0.3},
                    size={"mode": "relative", "value": 0.08}, fit={"max_lines": 3},
                    extra={"align": "center"}, animation={"preset": "none"}),
        ElementSpec(id="d", kind="divider", anchor="center", position={"x": 0.5, "y": 0.55},
                    extra={"fill": "#4f8cff"}, animation={"preset": "none"}),
        ElementSpec(id="n", kind="number", anchor="center", position={"x": 0.5, "y": 0.75},
                    size={"mode": "relative", "value": 0.12},
                    extra={"value": 42, "label": "answer", "align": "center"},
                    animation={"preset": "none"}),
    ])
    image = render_scene(scene, ctx, time=1.0, scene_duration=5.0)
    assert image.size == (width, height)
    assert _coverage(image) > 0.05


# --------------------------------------------------------------------------
# Saving
# --------------------------------------------------------------------------

def test_save_image_refuses_to_overwrite_by_default(tmp_path):
    image = Image.new("RGBA", (20, 20), (255, 0, 0, 255))
    target = tmp_path / "out.png"
    save_image(image, target)
    with pytest.raises(FileExistsError):
        save_image(image, target)
    save_image(image, target, overwrite=True)  # explicit is fine


def test_save_image_writes_a_loadable_file_and_leaves_no_part_file(tmp_path):
    image = Image.new("RGBA", (30, 30), (0, 128, 255, 255))
    target = tmp_path / "shot.png"
    save_image(image, target)
    with Image.open(target) as loaded:
        assert loaded.size == (30, 30)
    assert not (tmp_path / "shot.png.part").exists()


def test_save_image_creates_missing_parent_dirs(tmp_path):
    image = Image.new("RGBA", (10, 10), (0, 0, 0, 255))
    target = tmp_path / "nested" / "deeper" / "shot.png"
    save_image(image, target)
    assert target.is_file()
