"""The scene registry (Stage D).

A registry of **templates**: named recipes that build a :class:`SceneSpec` from
a few pieces of content.  Two jobs:

* the Storyboard's "Add scene" menu is generated from whatever is registered,
  so it can never list a scene type the engine cannot draw; and
* a new scene starts out already laid out and responsive, instead of an empty
  frame the user has to fill in by hand.

Registering a template is the only thing needed to expose a new one - the menu,
the renderer and the validator all discover templates from the registry.
Everything a template builds is in normalised units, so templates are
responsive by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from ..project.model import ElementSpec, SceneSpec, new_id
from .animation import DEFAULT_PRESET_BY_KIND

__all__ = [
    "SceneTemplate",
    "register_template",
    "get_template",
    "template_keys",
    "template_summaries",
    "create_scene_from_template",
    "default_templates_registered",
    "reset_templates",
]


@dataclass
class SceneTemplate:
    """One registered recipe."""

    key: str
    label: str
    description: str
    #: The stored scene type this recipe produces (must be in SCENE_TYPES).
    scene_type: str = "blank"
    #: ``(content, options) -> list[ElementSpec]``.
    build: Callable[..., list] = field(default=lambda content, options: [])
    #: Suggested background, or "" to use the project theme.
    background: str = ""
    tags: tuple = ()

    def create(self, content: Optional[dict] = None, **options) -> SceneSpec:
        """Build a fresh, usable scene.  Never mutates shared state."""
        content = content or {}
        scene = SceneSpec(
            id=new_id("scene"),
            type=self.scene_type,
            name=str(content.get("name", "") or self.label),
            background=str(content.get("background", "") or self.background),
        )
        for element in self.build(content, options):
            if not element.id:
                element.id = new_id("el")
            scene.elements.append(element)
        scene.duration = float(content.get("duration", 3.0) or 3.0)
        return scene


_registry: dict[str, SceneTemplate] = {}


def register_template(template: SceneTemplate) -> None:
    _registry[template.key] = template


def get_template(key: str) -> Optional[SceneTemplate]:
    return _registry.get(key)


def template_keys() -> list[str]:
    return list(_registry)


def template_summaries() -> list[dict]:
    """A menu-ready description of every registered template."""
    return [
        {
            "key": template.key,
            "label": template.label,
            "description": template.description,
            "scene_type": template.scene_type,
            "tags": list(template.tags),
        }
        for template in _registry.values()
    ]


def create_scene_from_template(key: str, content: Optional[dict] = None, **options) -> SceneSpec:
    template = _registry.get(key)
    if template is None:
        raise KeyError(f"No scene template named '{key}'. Known: {', '.join(template_keys()) or 'none'}.")
    return template.create(content, **options)


def reset_templates() -> None:
    """Empty the registry (tests use this to prove registration is explicit)."""
    _registry.clear()


# --------------------------------------------------------------------------
# Content helpers shared by the built-in templates
# --------------------------------------------------------------------------

def _text(text: str, *, x: float, y: float, size: float, align: str = "center",
          max_lines: int = 3, animation: str = "", bold: bool = False,
          color: str = "", width: float = 0.86, min_scale: float = 0.5,
          extra: Optional[dict] = None) -> ElementSpec:
    merged_extra = {"align": align, "bold": bold}
    if color:
        merged_extra["color_role"] = color
    if extra:
        merged_extra.update(extra)
    return ElementSpec(
        id="", kind="text", text=text, anchor="center",
        position={"x": x, "y": y},
        size={"mode": "relative", "value": size},
        fit={"max_lines": max_lines, "min_scale": min_scale},
        color=color,
        animation={"preset": animation or DEFAULT_PRESET_BY_KIND["text"]},
        extra=merged_extra,
    )


def _element(kind: str, **kwargs) -> ElementSpec:
    return ElementSpec(id="", kind=kind, **kwargs)


# --------------------------------------------------------------------------
# Built-in templates
# --------------------------------------------------------------------------

def _build_blank(content: dict, options: dict) -> list:
    return []


def _build_title(content: dict, options: dict) -> list:
    title = str(content.get("title", "") or content.get("text", "") or "Your title")
    subtitle = str(content.get("subtitle", "") or "")
    elements = [
        _text(title, x=0.5, y=0.42, size=0.095, max_lines=3, bold=True, animation="fade up"),
    ]
    if subtitle:
        elements.append(_text(subtitle, x=0.5, y=0.60, size=0.045, max_lines=2,
                              animation="fade up", color="auto"))
        elements.insert(1, _element("divider", anchor="center", position={"x": 0.5, "y": 0.53},
                                    extra={"fill": "theme", "thickness": 0.004},
                                    animation={"preset": "fade"}))
    return elements


def _build_body(content: dict, options: dict) -> list:
    body = str(content.get("text", "") or content.get("body", "") or "")
    heading = str(content.get("title", "") or "")
    elements = []
    if heading:
        elements.append(_text(heading, x=0.5, y=0.22, size=0.06, max_lines=2, bold=True))
    elements.append(_text(body, x=0.5, y=0.55 if heading else 0.5, size=0.042,
                          max_lines=int(options.get("max_lines", 8)), align="center",
                          min_scale=0.4, animation="reveal"))
    return elements


def _build_image_caption(content: dict, options: dict) -> list:
    caption = str(content.get("text", "") or content.get("caption", "") or "")
    asset_id = str(content.get("asset_id", "") or "")
    elements = [
        _element("image", asset_id=asset_id, anchor="center", position={"x": 0.5, "y": 0.40},
                 size={"width": {"mode": "fraction", "value": 0.72},
                       "height": {"mode": "fraction", "value": 0.5}},
                 extra={"fit": "contain", "radius": 0.02}, animation={"preset": "fade"}),
    ]
    if caption:
        elements.append(_text(caption, x=0.5, y=0.78, size=0.045, max_lines=2))
    return elements


def _build_stat(content: dict, options: dict) -> list:
    value = content.get("value", content.get("text", ""))
    elements = [
        _element("number", anchor="center", position={"x": 0.5, "y": 0.42},
                 size={"mode": "relative", "value": 0.16},
                 extra={"value": value, "unit": str(content.get("unit", "") or ""),
                        "decimals": content.get("decimals", 0), "align": "center"},
                 animation={"preset": "pop"}),
    ]
    label = str(content.get("label", "") or "")
    if label:
        elements[0].extra["label"] = label
    caption = str(content.get("text", "") or "")
    if caption and not label:
        elements.append(_text(caption, x=0.5, y=0.66, size=0.045, max_lines=2))
    return elements


def _build_quote(content: dict, options: dict) -> list:
    quote = str(content.get("text", "") or content.get("quote", "") or "")
    attribution = str(content.get("attribution", "") or "")
    elements = [
        _text(f"\u201c{quote}\u201d" if quote else "", x=0.5, y=0.44, size=0.052,
              max_lines=int(options.get("max_lines", 6)), align="center", min_scale=0.4,
              animation="reveal"),
    ]
    if attribution:
        elements.append(_text(f"\u2014 {attribution}", x=0.5, y=0.66, size=0.038, max_lines=1,
                              color="auto"))
    return elements


def _build_bullets(content: dict, options: dict) -> list:
    lines = [line.strip() for line in str(content.get("text", "") or "").splitlines() if line.strip()]
    if not lines and isinstance(content.get("items"), (list, tuple)):
        lines = [str(item) for item in content["items"]]
    heading = str(content.get("title", "") or "")
    elements = []
    if heading:
        elements.append(_text(heading, x=0.5, y=0.18, size=0.06, max_lines=2, bold=True))
    body = "\n".join(f"\u2022 {line}" for line in lines)
    elements.append(_text(body, x=0.5, y=0.55 if heading else 0.5, size=0.042,
                          max_lines=int(options.get("max_lines", 8)), align="left", min_scale=0.4,
                          extra={"align": "left"}))
    return elements


def _build_chart(content: dict, options: dict) -> list:
    values = content.get("values") or [12, 30, 18, 44, 27]
    labels = content.get("labels") or [f"#{i + 1}" for i in range(len(values))]
    chart_kind = str(content.get("chart", "column"))
    heading = str(content.get("title", "") or "")
    elements = []
    if heading:
        elements.append(_text(heading, x=0.5, y=0.16, size=0.055, max_lines=2, bold=True))
    elements.append(_element("chart", anchor="center", position={"x": 0.5, "y": 0.62},
                             size={"width": {"mode": "fill"}, "height": {"mode": "fraction", "value": 0.5}},
                             extra={"chart": chart_kind, "values": values, "labels": labels},
                             animation={"preset": "grow"}))
    return elements


def _build_cta(content: dict, options: dict) -> list:
    action = str(content.get("text", "") or content.get("action", "") or "Subscribe")
    sub = str(content.get("subtitle", "") or "")
    elements = [
        _element("shape", anchor="center", position={"x": 0.5, "y": 0.45},
                 size={"width": {"mode": "fraction", "value": 0.6},
                       "height": {"mode": "design", "value": 0.16}},
                 extra={"shape": "pill", "fill": "theme"}, animation={"preset": "pop"}),
        _text(action, x=0.5, y=0.45, size=0.055, max_lines=1, bold=True, animation="fade"),
    ]
    if sub:
        elements.append(_text(sub, x=0.5, y=0.66, size=0.04, max_lines=2, color="auto"))
    return elements


def _build_divider(content: dict, options: dict) -> list:
    label = str(content.get("text", "") or content.get("title", "") or "")
    elements = [
        _element("divider", anchor="center", position={"x": 0.5, "y": 0.46},
                 extra={"fill": "theme"}, animation={"preset": "fade"}),
    ]
    if label:
        elements.append(_text(label, x=0.5, y=0.56, size=0.05, max_lines=1, bold=True))
    return elements


def _build_hook(content: dict, options: dict) -> list:
    """An attention-grabbing opener: big headline, accent rule, optional stat."""
    headline = str(content.get("title", "") or content.get("text", "") or "Wait for it...")
    elements = [
        _text(headline, x=0.5, y=0.40, size=0.10, max_lines=3, bold=True, animation="fade up"),
        _element("divider", anchor="center", position={"x": 0.5, "y": 0.55},
                 extra={"fill": "theme", "thickness": 0.006}, animation={"preset": "fade"}),
    ]
    sub = str(content.get("subtitle", "") or "")
    if sub:
        elements.append(_text(sub, x=0.5, y=0.64, size=0.05, max_lines=2, color="auto"))
    return elements


def _build_paragraph(content: dict, options: dict) -> list:
    """A readable paragraph of body copy, left aligned, auto-fit (never cut)."""
    body = str(content.get("text", "") or content.get("body", "") or "")
    heading = str(content.get("title", "") or "")
    elements = []
    if heading:
        elements.append(_text(heading, x=0.5, y=0.18, size=0.06, max_lines=2, bold=True,
                              align="left", extra={"align": "left"}))
    elements.append(_text(body, x=0.5, y=0.56 if heading else 0.5, size=0.042,
                          max_lines=int(options.get("max_lines", 12)), align="left",
                          min_scale=0.35, animation="reveal", extra={"align": "left"}))
    return elements


def _build_list(content: dict, options: dict) -> list:
    """A numbered list."""
    lines = [line.strip() for line in str(content.get("text", "") or "").splitlines() if line.strip()]
    if not lines and isinstance(content.get("items"), (list, tuple)):
        lines = [str(item) for item in content["items"]]
    heading = str(content.get("title", "") or "")
    elements = []
    if heading:
        elements.append(_text(heading, x=0.5, y=0.18, size=0.06, max_lines=2, bold=True))
    body = "\n".join(f"{i + 1}. {line}" for i, line in enumerate(lines))
    elements.append(_text(body, x=0.5, y=0.55 if heading else 0.5, size=0.044,
                          max_lines=int(options.get("max_lines", 10)), align="left", min_scale=0.4,
                          extra={"align": "left"}))
    return elements


def _build_counter(content: dict, options: dict) -> list:
    """A statistic that counts up from zero (directive section 31)."""
    value = content.get("value", content.get("text", ""))
    element = _element("number", anchor="center", position={"x": 0.5, "y": 0.44},
                       size={"mode": "relative", "value": 0.16},
                       extra={"value": value, "unit": str(content.get("unit", "") or ""),
                              "decimals": content.get("decimals", 0), "align": "center"},
                       animation={"preset": "count up", "duration": float(content.get("duration", 1.4) or 1.4)})
    elements = [element]
    label = str(content.get("label", "") or "")
    if label:
        element.extra["label"] = label
    return elements


def _build_progress(content: dict, options: dict) -> list:
    """A labelled progress bar (directive section 25)."""
    label = str(content.get("label", "") or content.get("title", "") or "")
    elements = []
    if label:
        elements.append(_text(label, x=0.5, y=0.40, size=0.05, max_lines=2, bold=True))
    elements.append(_element("progress", anchor="center", position={"x": 0.5, "y": 0.54},
                             size={"width": {"mode": "fraction", "value": 0.7},
                                   "height": {"mode": "design", "value": 0.05}},
                             extra={"value": content.get("value", 60), "max": content.get("max", 100),
                                    "fill": "theme", "radius": 0.02},
                             animation={"preset": "progress fill",
                                        "duration": float(content.get("duration", 1.2) or 1.2)}))
    return elements


def _build_comparison(content: dict, options: dict) -> list:
    """Two panels side by side - "this vs that", before/after, pros/cons."""
    left_title = str(content.get("left_title", "") or "Before")
    right_title = str(content.get("right_title", "") or "After")
    left_body = str(content.get("left", "") or content.get("left_text", "") or "")
    right_body = str(content.get("right", "") or content.get("right_text", "") or "")
    return [
        _element("card", anchor="center", position={"x": 0.27, "y": 0.52},
                 size={"width": {"mode": "fraction", "value": 0.42},
                       "height": {"mode": "fraction", "value": 0.5}},
                 extra={"fill": "surface", "border": True}, animation={"preset": "slide from left"}),
        _element("card", anchor="center", position={"x": 0.73, "y": 0.52},
                 size={"width": {"mode": "fraction", "value": 0.42},
                       "height": {"mode": "fraction", "value": 0.5}},
                 extra={"fill": "surface", "border": True}, animation={"preset": "slide from right"}),
        _text(left_title, x=0.27, y=0.34, size=0.05, max_lines=1, bold=True, width=0.4),
        _text(right_title, x=0.73, y=0.34, size=0.05, max_lines=1, bold=True, width=0.4),
        _text(left_body, x=0.27, y=0.55, size=0.038, max_lines=6, min_scale=0.4, width=0.4),
        _text(right_body, x=0.73, y=0.55, size=0.038, max_lines=6, min_scale=0.4, width=0.4),
    ]


def _build_before_after(content: dict, options: dict) -> list:
    """Two images side by side with labels - a visual before/after."""
    left_id = str(content.get("left_asset_id", "") or content.get("asset_id", "") or "")
    right_id = str(content.get("right_asset_id", "") or "")
    return [
        _element("image", asset_id=left_id, anchor="center", position={"x": 0.27, "y": 0.46},
                 size={"width": {"mode": "fraction", "value": 0.42},
                       "height": {"mode": "fraction", "value": 0.5}},
                 extra={"fit": "cover", "radius": 0.02}, animation={"preset": "fade"}),
        _element("image", asset_id=right_id, anchor="center", position={"x": 0.73, "y": 0.46},
                 size={"width": {"mode": "fraction", "value": 0.42},
                       "height": {"mode": "fraction", "value": 0.5}},
                 extra={"fit": "cover", "radius": 0.02}, animation={"preset": "fade"}),
        _text(str(content.get("left_title", "") or "Before"), x=0.27, y=0.78, size=0.045, max_lines=1),
        _text(str(content.get("right_title", "") or "After"), x=0.73, y=0.78, size=0.045, max_lines=1),
    ]


def _build_timeline(content: dict, options: dict) -> list:
    """A vertical list of dated events with a connecting rule."""
    raw = content.get("events") or content.get("items") or []
    if isinstance(raw, str):
        raw = [line.strip() for line in raw.splitlines() if line.strip()]
    events = []
    for item in raw:
        if isinstance(item, dict):
            events.append((str(item.get("when", "") or ""), str(item.get("what", item.get("text", "")) or "")))
        else:
            text = str(item)
            when, _, what = text.partition(" ")
            events.append((when, what or text))
    heading = str(content.get("title", "") or "")
    elements = []
    if heading:
        elements.append(_text(heading, x=0.5, y=0.14, size=0.055, max_lines=2, bold=True))
    elements.append(_element("divider", anchor="center", position={"x": 0.16, "y": 0.55},
                             extra={"fill": "theme", "thickness": 0.004, "vertical": True},
                             animation={"preset": "fade"}))
    top, bottom = 0.28, 0.84
    count = max(1, len(events))
    for i, (when, what) in enumerate(events[:8]):
        y = top + (bottom - top) * (i / count)
        elements.append(_element("shape", anchor="center", position={"x": 0.16, "y": y},
                                 size={"mode": "design", "value": 0.02},
                                 extra={"shape": "circle", "fill": "theme"},
                                 animation={"preset": "pop", "delay": i * 0.12}))
        elements.append(_text(f"{when}  {what}".strip(), x=0.55, y=y, size=0.04, max_lines=2,
                              align="left", min_scale=0.4, extra={"align": "left"},
                              animation="fade up"))
    return elements


def _build_bento(content: dict, options: dict) -> list:
    """A 2x2 grid of cards, each with a short label - a "bento" layout."""
    items = content.get("items") or []
    if isinstance(items, str):
        items = [line.strip() for line in items.splitlines() if line.strip()]
    cells = [(0.28, 0.30), (0.72, 0.30), (0.28, 0.70), (0.72, 0.70)]
    elements = []
    for i, (cx, cy) in enumerate(cells):
        label = str(items[i]) if i < len(items) else ""
        elements.append(_element("card", anchor="center", position={"x": cx, "y": cy},
                                 size={"width": {"mode": "fraction", "value": 0.4},
                                       "height": {"mode": "fraction", "value": 0.32}},
                                 extra={"fill": "surface", "border": True},
                                 animation={"preset": "fade up", "delay": i * 0.1}))
        if label:
            elements.append(_text(label, x=cx, y=cy, size=0.04, max_lines=3, min_scale=0.4, width=0.36))
    return elements


def _build_collage(content: dict, options: dict) -> list:
    """A responsive grid of up to four images."""
    ids = [str(a) for a in (content.get("asset_ids") or []) if str(a)]
    if not ids and content.get("asset_id"):
        ids = [str(content["asset_id"])]
    cells = [(0.28, 0.30), (0.72, 0.30), (0.28, 0.70), (0.72, 0.70)]
    elements = []
    for i, (cx, cy) in enumerate(cells):
        asset = ids[i] if i < len(ids) else ""
        elements.append(_element("image", asset_id=asset, anchor="center", position={"x": cx, "y": cy},
                                 size={"width": {"mode": "fraction", "value": 0.4},
                                       "height": {"mode": "fraction", "value": 0.34}},
                                 extra={"fit": "cover", "radius": 0.02},
                                 animation={"preset": "fade", "delay": i * 0.08}))
    return elements


def _build_end_screen(content: dict, options: dict) -> list:
    """A closing card: logo, thanks line and a call to action."""
    logo_id = str(content.get("asset_id", "") or content.get("logo_asset_id", "") or "")
    message = str(content.get("text", "") or content.get("message", "") or "Thanks for watching")
    cta = str(content.get("cta", "") or "Subscribe")
    elements = []
    if logo_id:
        elements.append(_element("image", asset_id=logo_id, anchor="center", position={"x": 0.5, "y": 0.30},
                                 size={"width": {"mode": "fraction", "value": 0.3},
                                       "height": {"mode": "fraction", "value": 0.22}},
                                 extra={"fit": "contain"}, animation={"preset": "fade"}))
    elements.append(_text(message, x=0.5, y=0.55, size=0.06, max_lines=2, bold=True, animation="fade up"))
    elements.append(_element("shape", anchor="center", position={"x": 0.5, "y": 0.72},
                             size={"width": {"mode": "fraction", "value": 0.5},
                                   "height": {"mode": "design", "value": 0.13}},
                             extra={"shape": "pill", "fill": "theme"}, animation={"preset": "pop"}))
    elements.append(_text(cta, x=0.5, y=0.72, size=0.05, max_lines=1, bold=True, animation="fade"))
    return elements


def _build_logo(content: dict, options: dict) -> list:
    """A centred logo with the name beneath it."""
    asset_id = str(content.get("asset_id", "") or "")
    name = str(content.get("title", "") or content.get("text", "") or "")
    elements = [
        _element("image", asset_id=asset_id, anchor="center", position={"x": 0.5, "y": 0.42},
                 size={"width": {"mode": "fraction", "value": 0.4},
                       "height": {"mode": "fraction", "value": 0.3}},
                 extra={"fit": "contain"}, animation={"preset": "zoom"}),
    ]
    if name:
        elements.append(_text(name, x=0.5, y=0.66, size=0.055, max_lines=2, bold=True))
    return elements


_DEFAULT_TEMPLATES = [
    SceneTemplate("blank", "Blank scene", "An empty frame to build from scratch.", "blank", _build_blank),
    SceneTemplate("hook", "Hook", "A bold opening headline to stop the scroll.", "title", _build_hook),
    SceneTemplate("title", "Title card", "A large heading with an optional subtitle and rule.", "title", _build_title),
    SceneTemplate("body", "Text scene", "A heading over a paragraph of body text.", "text", _build_body),
    SceneTemplate("image", "Image + caption", "One image with a caption underneath.", "image", _build_image_caption),
    SceneTemplate("stat", "Big number", "A single statistic with a unit and label.", "number", _build_stat),
    SceneTemplate("quote", "Quote", "A pull quote with an optional attribution.", "text", _build_quote),
    SceneTemplate("bullets", "Bullet list", "A heading over a bulleted list.", "text", _build_bullets),
    SceneTemplate("chart", "Chart", "A responsive chart built from your data.", "graphic", _build_chart),
    SceneTemplate("cta", "Call to action", "A pill button with a label, for endings.", "cta", _build_cta),
    SceneTemplate("divider", "Chapter divider", "A rule and short label between sections.", "title", _build_divider),
    SceneTemplate("paragraph", "Paragraph", "A readable block of body copy.", "text", _build_paragraph),
    SceneTemplate("list", "Numbered list", "A heading over a numbered list.", "text", _build_list),
    SceneTemplate("counter", "Count-up number", "A statistic that counts up from zero.", "number", _build_counter),
    SceneTemplate("progress", "Progress bar", "A labelled bar that fills to a value.", "graphic", _build_progress),
    SceneTemplate("comparison", "Comparison", "Two panels side by side (this vs that).", "graphic", _build_comparison),
    SceneTemplate("before_after", "Before / after", "Two images side by side with labels.", "image", _build_before_after),
    SceneTemplate("timeline", "Timeline", "A vertical list of dated events.", "graphic", _build_timeline),
    SceneTemplate("bento", "Bento grid", "A 2x2 grid of labelled cards.", "graphic", _build_bento),
    SceneTemplate("collage", "Photo collage", "A responsive grid of up to four images.", "image", _build_collage),
    SceneTemplate("end_screen", "End screen", "Logo, thanks line and a call to action.", "cta", _build_end_screen),
    SceneTemplate("logo", "Logo reveal", "A centred logo with the name beneath.", "image", _build_logo),
]


def default_templates_registered() -> int:
    """Register the built-in templates once; returns how many are registered."""
    if _registry:
        return len(_registry)
    for template in _DEFAULT_TEMPLATES:
        register_template(template)
    return len(_registry)
