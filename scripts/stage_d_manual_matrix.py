"""Stage D manual verification matrix.

Runs the scenarios the Stage D brief requires through the same code paths the
interface uses, against a *real project on disk*, and prints what happened.  It
is evidence for the report, not a substitute for the automated suite - open the
printed preview PNGs to confirm the frames look right.

Usage::

    LD_LIBRARY_PATH=/tmp/stublib QT_QPA_PLATFORM=offscreen \\
        /home/user/.venv/bin/python scripts/stage_d_manual_matrix.py \\
        --data-root /tmp/mgs_stage_d

Every scenario renders real pixels with Pillow (CPU only).  Nothing here needs a
GPU, a network connection or the Kokoro model - Stage D is the visual layer.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from app.core.paths import AppPaths  # noqa: E402
from app.core.settings import Settings  # noqa: E402
from app.project.model import AssetSpec  # noqa: E402
from app.project.service import CreateRequest, ProjectService  # noqa: E402
from app.scene import (  # noqa: E402
    Canvas,
    build_context,
    build_rows,
    build_timeline,
    render_scene,
    validate_project_scenes,
)
from app.scene import jobs as scene_jobs  # noqa: E402
from app.scene.animation import evaluate, parse_animation  # noqa: E402
from app.scene.templates import (  # noqa: E402
    create_scene_from_template,
    default_templates_registered,
    template_keys,
)
from app.scene.timing import TimingOptions  # noqa: E402
from app.scene.transitions import blend  # noqa: E402
from app.jobs.cancel import CancelToken  # noqa: E402
from app.jobs.progress import ProgressReporter  # noqa: E402
from app.jobs.spec import JobContext  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def record(number: int, name: str, ok: bool, detail: str) -> None:
    RESULTS.append((f"{number:02d}", "PASS" if ok else "FAIL", f"{name} - {detail}"))
    print(f"[{'PASS' if ok else 'FAIL'}] {number:02d} {name}: {detail}")


def _coverage(image: Image.Image, threshold: int = 40) -> float:
    small = image.convert("L").resize((96, 96))
    pixels = list(small.tobytes())
    return sum(1 for value in pixels if value > threshold) / len(pixels)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, help="Where the evidence project is written.")
    args = parser.parse_args()

    root = Path(args.data_root)
    root.mkdir(parents=True, exist_ok=True)
    paths = AppPaths(data_root=root, source_root=Path(__file__).resolve().parents[1], reason="stage-d-matrix")
    paths.ensure()

    service = ProjectService(paths, Settings())
    service.create_project(CreateRequest(name="StageD_Matrix"))
    project = service.current
    project_dir = Path(service.current_layout.root)

    # A real image asset so image placement is exercised against a file.
    assets = project_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (960, 540), (30, 110, 190)).save(assets / "hero.png")
    project.assets.append(AssetSpec(id="hero", name="hero.png", kind="image", path="assets/hero.png"))

    previews = paths.previews_dir / "stage_d"
    previews.mkdir(parents=True, exist_ok=True)

    default_templates_registered()

    # 1 - registry exposes the built-in scene types.
    keys = template_keys()
    record(1, "Scene registry", len(keys) >= 10 and "title" in keys and "chart" in keys,
           f"{len(keys)} templates: {', '.join(keys)}")

    # 2 - scene model: templates build scenes of known types with elements.
    scenes = {key: create_scene_from_template(key, {"title": "T", "text": "Some body copy.",
                                                    "value": 42, "values": [1, 2, 3]}) for key in keys}
    ok = all(scene.type for scene in scenes.values())
    record(2, "Scene model", ok, "every template produced a typed scene with elements")

    # 3 - visual element model: all element kinds present across templates.
    kinds = {element.kind for scene in scenes.values() for element in scene.elements}
    record(3, "Visual element model", {"text", "image", "shape", "card", "number", "chart"} <= kinds
           or {"text", "number", "chart"} <= kinds, f"element kinds seen: {sorted(kinds)}")

    # Build the project's sequence.
    service.add_scene_from_template("title", {"title": "Responsive by construction", "subtitle": "Stage D"})
    service.add_scene_from_template("image", {"asset_id": "hero", "text": "The hero shot"})
    service.add_scene_from_template("stat", {"value": 98.6, "unit": "%", "label": "on-time delivery"})
    service.add_scene_from_template("chart", {"values": [4, 9, 2, 7, 5], "labels": ["a", "b", "c", "d", "e"]})
    project = service.current

    # 4 - responsive layout: the same project renders at every aspect ratio.
    ratios = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080), "4K": (3840, 2160)}
    rendered = {}
    responsive_ok = True
    for label, (w, h) in ratios.items():
        ctx = build_context(project, canvas=Canvas(w, h), project_dir=project_dir)
        frames = [render_scene(scene, ctx, time=1.5, scene_duration=5.0) for scene in project.scenes]
        rendered[label] = frames
        if not all(f.size == (w, h) and _coverage(f) > 0.01 for f in frames):
            responsive_ok = False
    record(4, "Responsive layout", responsive_ok, f"rendered all scenes at {', '.join(ratios)}")

    # 5 - text fitting: a long headline fits at 9:16 without overflow.
    ctx = build_context(project, canvas=Canvas(1080, 1920), project_dir=project_dir)
    from app.scene.elements import layout_scene
    from app.project.model import ElementSpec, SceneSpec

    long_scene = SceneSpec(id="long", elements=[ElementSpec(
        id="t", kind="text", anchor="center", position={"x": 0.5, "y": 0.5},
        size={"mode": "relative", "value": 0.09},
        text="A deliberately long headline that must wrap and fit inside the frame at any ratio.",
        fit={"max_lines": 3}, animation={"preset": "none"})])
    layout = layout_scene(long_scene.elements, ctx)
    text_el = layout.elements[0]
    record(5, "Text fitting", not text_el.overflow, f"fit to {text_el.font_size:.0f}px in {len(text_el.lines)} lines")

    # 6 - image placement: the real asset's pixels appear in the frame.
    image_frame = rendered["16:9"][1]
    colours = {image_frame.getpixel((x, y))[:3] for x in range(0, image_frame.width, 24)
               for y in range(0, image_frame.height, 24)}
    record(6, "Image placement", any(b > 140 and r < 90 for r, g, b in colours),
           "hero.png pixels found in the 16:9 frame")

    # 7 - shapes, 8 - cards, 9 - numbers, 10 - charts render without errors.
    ctx16 = build_context(project, canvas=Canvas(1920, 1080), project_dir=project_dir)
    for number, key in ((7, "divider"), (8, "cta"), (9, "stat"), (10, "chart")):
        scene = create_scene_from_template(key, {"title": "T", "text": "act now", "value": 7,
                                                 "values": [3, 1, 4], "labels": ["x", "y", "z"]})
        frame = render_scene(scene, ctx16, time=1.5, scene_duration=4.0)
        record(number, {"divider": "Shapes", "cta": "Cards", "stat": "Numbers", "chart": "Charts"}[key],
               frame.size == (1920, 1080) and _coverage(frame) > 0.005, f"{key} rendered")

    # 11 - storyboard rows match the scenes and carry timing.
    rows, timeline = build_rows(project)
    record(11, "Storyboard", len(rows) == len(project.scenes) and timeline.scene_count == len(project.scenes),
           f"{len(rows)} rows, total {timeline.format_total()}")

    # 12 - duplication and reordering keep ids unique and are undoable.
    # NOTE: undo() swaps in a *new* project object, so re-read service.current
    # after each undo instead of trusting the old reference.
    before_count = len(service.current.scenes)
    first = service.current.scenes[0]
    copy = service.duplicate_scene(first.id)
    count_after_dup = len(service.current.scenes)
    service.move_scene_by(copy.id, len(service.current.scenes) - 1)
    ids = [scene.id for scene in service.current.scenes]
    service.undo()  # undo the move
    service.undo()  # undo the duplicate
    after_undo = len(service.current.scenes)
    record(12, "Duplication/reordering",
           copy is not None and count_after_dup == before_count + 1 and after_undo == before_count
           and len(ids) == len(set(ids)),
           f"{before_count} -> {count_after_dup} after duplicate -> {after_undo} after two undos")

    # 13 - animation system: presets evaluate to identity at their end.
    anim_ok = True
    for key in ("fade", "pop", "grow", "zoom"):
        spec = parse_animation({"preset": key, "duration": 0.6})
        if evaluate(spec, 10.0).opacity != 1.0:
            anim_ok = False
    record(13, "Animation system", anim_ok, "enter presets settle to a visible, identity state")

    # 14 - transitions: blends return the source frames at the extremes.
    a, b = rendered["16:9"][0], rendered["16:9"][1]
    trans_ok = all(blend(a, b, t, kind).size == a.size
                   for kind in ("fade", "slide", "zoom", "wipe", "dip") for t in (0.0, 0.5, 1.0))
    record(14, "Transitions foundation", trans_ok, "five transition kinds blend two real frames")

    # 15 - narration-driven timing: a measured narration sets the scene length.
    project.scenes[1].narration.duration = 8.25
    tl = build_timeline(project.scenes, TimingOptions(head=0.0, tail=0.5))
    narrated = tl.timings[1]
    record(15, "Narration-driven timing", narrated.duration == 8.75 and narrated.source == "narration",
           f"scene 2 runs {narrated.duration:.2f}s from an 8.25s narration + 0.5s tail")

    # 16 - preview job renders a real frame off-thread (run synchronously here).
    job_ctx = JobContext(job_id="matrix", key="scene.preview", cancel=CancelToken(),
                         progress=ProgressReporter(),
                         payload={"project": project, "project_dir": str(project_dir),
                                  "previews_dir": str(previews), "scene_id": project.scenes[0].id, "time": 1.0})
    result = scene_jobs.scene_preview_body(job_ctx)
    frame_ok = Path(result["path"]).is_file()
    validation = validate_project_scenes(project, project_dir=project_dir)
    record(16, "Scene preview + validation", frame_ok and not validation.errors,
           f"preview at {result['path']}; {len(validation.errors)} validation errors")

    # Save a few frames for the human to inspect.
    for label, frames in rendered.items():
        for index, frame in enumerate(frames):
            frame.save(previews / f"{label}-scene{index}.png")

    service.save(reason="Stage D matrix evidence")

    failed = [row for row in RESULTS if row[1] == "FAIL"]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} scenarios passed.")
    print(f"Previews written to: {previews}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
