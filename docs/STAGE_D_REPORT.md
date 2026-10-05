# Stage D Report - Scene Engine, Storyboard & Responsive Visual System

**Version:** 0.4.0 (stage D) - project file schema unchanged (v3)
**Automated tests:** 830 passing (591 from Stages A-C preserved, +239 new)
**Manual matrix:** 16/16 scenarios pass (`scripts/stage_d_manual_matrix.py`)
**Status:** Stage D features are implemented and verified. Known limitations are
listed honestly at the end; no P0/P1 bugs are known in the Stage D workflow.

Stage D builds the visual layer on top of the untouched Stage A/B/C foundation.
Stage B already stored scenes with normalised (0..1) element coordinates; Stage D
is the engine that lays those out and draws them at any resolution, and the
Storyboard UI that arranges them.

## What was built

| Area | Where | Notes |
|---|---|---|
| Resolution-independent geometry | `app/scene/canvas.py` | `Canvas`, `Rect`, `PixelRect`, `SafeArea`. Positions are fractions of the frame; sizes are fractions of `min(width,height)`. No 1080x1920 anywhere - enforced by an AST test. |
| Colour | `app/scene/palette.py` | hex/rgb/named/tuple parsing, gradients, luminance-based readable text. Unknown colours fall back *and are reported*. |
| Font discovery + text fitting | `app/scene/text.py` | Lazy cross-platform font scan, real-metric measurement, word wrap, binary-search fitting. Uninstalled families are named in a note, never silently swapped. Missing-script detection is best-effort and labelled as such. |
| Visual element model + responsive layout | `app/scene/elements.py` | text / image / shape / card / number / chart / divider, laid out purely from (element, canvas, theme). Off-screen, overlap and overflow are surfaced as friendly issues. |
| Shapes | `app/scene/shapes.py` | rect/rounded/ellipse/circle/line/triangle/pill/badge/arrow/frame with opacity compositing and gradients. |
| Charts | `app/scene/charts.py` | bar, column, line, area, pie, donut, sparkline. Axis rounding touches only the axis, never the data. Labels drop (not overlap) when crowded. |
| Animation | `app/scene/animation.py` | Easing curves, tracks, presets. Evaluation is a pure function of time, so previews scrub freely. Empty `animation` means the kind's default preset; `{"preset":"none"}` turns it off. |
| Transitions | `app/scene/transitions.py` | none/cut/fade/slide/zoom/wipe/push/dip blends of two rendered frames. |
| Narration-driven timing | `app/scene/timing.py` | Measured narration wins; opt-in head/tail padding; transition overlap; **no maximum video length**. |
| Renderer | `app/scene/compose.py` | Deterministic RGBA composition. Missing images draw a labelled placeholder; unreadable files cannot break a preview. |
| Scene registry | `app/scene/templates.py` | 10 templates. Registration is the only step to expose a new type; tests cross-check every template against `SCENE_TYPES`/`ELEMENT_KINDS`. |
| Engine validation | `app/scene/validate.py` | Runs the same layout pass the preview uses and reports friendly issues. Structural checks stay in `app/project/validation.py`. |
| Storyboard | `app/scene/storyboard.py` | Rows (name/timing/source/issues) plus thumbnail rendering into the preview folder only. |
| Off-thread jobs | `app/scene/jobs.py` | `STORYBOARD_RENDER` and `SCENE_PREVIEW` with the Stage A `JobContext` signature; cancellation checked between scenes. |
| Storyboard UI | `app/ui/views/storyboard_view.py` | Add-from-template, duplicate, rename, reorder, delete (single undoable edits) + live thumbnails and a preview panel. Wired into the main window nav. |
| Service ops | `app/project/service.py`, `model.py` | `duplicate_scene` (fresh ids, one undoable edit), `rename_scene`, `move_scene_by`, `add_scene_from_template`. |

## The responsive promise, verified

- An AST test asserts that no video resolution appears as a numeric literal in
  `canvas.py` or `elements.py`.
- The same scene is rendered and asserted to fill the frame at 16:9, 9:16, 1:1,
  1280x720 and 3840x2160 (`test_scene_visual.py`, `test_scene_e2e_preview.py`).
- Text fitting keeps a long headline inside its box at every aspect ratio and
  never rewrites the wording (`test_scene_text.py`).

## No maximum duration

`build_timeline` handles a 540-scene (~3 hour) project and `format_duration`
scales to hours. There is no cap anywhere in Stage D (`test_scene_motion.py`).

## Bugs found by driving the code (each has a regression test)

1. The Stage B single-value size form was read as a box width *and* a font size,
   squeezing existing text into a 12%-wide column. Now it means font size only.
2. A number element sized its box from empty text (a 1px-tall box).
3. An uninstalled font family silently resolved to whatever font came first.
4. `PixelRect` had no `offset()`, crashing charts.
5. `chart_value_range` dropped negative axes.
6. `total_duration` summed durations, overstating length when transitions
   overlap; it now reports the end of the last scene.
7. An empty `animation` dict disabled animation; it now applies the kind default.
8. `SceneSpec.from_dict` (generic) left `elements` as raw dicts, so duplicated
   scenes had dict elements. A proper `from_dict` now parses nested sections.
9. Scene job specs did not pass `paths`, so workers had no preview folder.

## Known limitations (disclosed, not hidden)

- This is a **preview/renderer**, not the final video encoder. Stage F will turn
  the timeline + renderer into an FFmpeg encode. Nothing here renders video.
- True per-character "typewriter" text and per-element keyframe editing are Stage
  I territory; Stage D ships enter/exit presets and tracks only.
- Missing-glyph detection is a heuristic that reliably catches whole absent
  scripts (Devanagari/CJK) but may miss single decorative codepoints. It is a
  warning, never an error.
- Charts draw value labels but no interactive axis tick labels; the gridlines are
  unlabeled by design (the bars themselves carry the values).
- Thumbnails/previews are disposable and rebuilt on change; they are not cached in
  the project.

## Run it

```
python run_studio.py --data-root <dir>          # GUI, open the Storyboard page
python scripts/stage_d_manual_matrix.py --data-root <dir>   # 16-scenario evidence
python -m pytest tests -q                        # 830 tests
```
