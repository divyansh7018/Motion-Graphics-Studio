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


_DEFAULT_TEMPLATES = [
    SceneTemplate("blank", "Blank scene", "An empty frame to build from scratch.", "blank", _build_blank),
    SceneTemplate("title", "Title card", "A large heading with an optional subtitle and rule.", "title", _build_title),
    SceneTemplate("body", "Text scene", "A heading over a paragraph of body text.", "text", _build_body),
    SceneTemplate("image", "Image + caption", "One image with a caption underneath.", "image", _build_image_caption),
    SceneTemplate("stat", "Big number", "A single statistic with a unit and label.", "number", _build_stat),
    SceneTemplate("quote", "Quote", "A pull quote with an optional attribution.", "text", _build_quote),
    SceneTemplate("bullets", "Bullet list", "A heading over a bulleted list.", "text", _build_bullets),
    SceneTemplate("chart", "Chart", "A responsive chart built from your data.", "graphic", _build_chart),
    SceneTemplate("cta", "Call to action", "A pill button with a label, for endings.", "cta", _build_cta),
    SceneTemplate("divider", "Chapter divider", "A rule and short label between sections.", "title", _build_divider),
]


def default_templates_registered() -> int:
    """Register the built-in templates once; returns how many are registered."""
    if _registry:
        return len(_registry)
    for template in _DEFAULT_TEMPLATES:
        register_template(template)
    return len(_registry)
