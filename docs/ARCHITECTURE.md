# Architecture

This document explains how Motion Graphics Studio is put together and why. It is
the reference for anyone adding to it: the rules here exist because breaking
them is what makes desktop media tools unstable.

---

## 1. Principles

1. **One source of truth.** There is exactly one settings object and (from Stage
   B) exactly one project model used by the GUI, the command line, the renderer
   and the tests. No subsystem keeps its own copy of state.
2. **UI never does heavy work.** Anything slower than a few milliseconds runs as
   a background job (section 7 of the directive).
3. **One action = one job.** A click submits one job; the job runs its body
   exactly once; the UI receives exactly one completion event.
4. **Every failure is explainable.** Errors carry what happened, why, and what to
   do next - never a bare traceback on screen.
5. **Nothing happens at import time.** Modules are safe to import; work starts
   only when something explicitly asks for it.
6. **The application resolves its own paths.** No dependency on the working
   directory, ever.
7. **No commercial code.** Licensing, accounts and payments are Phase 2.

---

## 2. Layers

```
        ┌────────────────────────────────────────────────────────┐
        │  app/main.py            GUI entry point + startup       │
        │  app/cli/main.py        command line entry point        │
        │  installer/setup_windows.py   setup + verification      │
        └───────────────┬────────────────────────────────────────┘
                        │
        ┌───────────────▼───────────────┐   ┌────────────────────┐
        │  app/ui/       (PySide6)      │   │  app/diagnostics/  │
        │  main_window, pages, widgets  │   │  smoke test        │
        └───────────────┬───────────────┘   └────────┬───────────┘
                        │                            │
        ┌───────────────▼────────────────────────────▼───────────┐
        │  app/jobs/     job specs, state machine, cancel tokens, │
        │                progress, Qt thread pool manager          │
        ├─────────────────────────────────────────────────────────┤
        │  app/checks/   readiness checks + report aggregation     │
        ├─────────────────────────────────────────────────────────┤
        │  app/tools/    external tool adapters (FFmpeg, Kokoro)   │
        ├─────────────────────────────────────────────────────────┤
        │  app/core/     settings, paths, logging, atomic I/O,     │
        │                errors, environment, maintenance          │
        └─────────────────────────────────────────────────────────┘
```

Dependencies point **downwards only**. `core` knows nothing about Qt, jobs or
the UI, which is what allows the command line and the test suite to exercise the
real logic on a machine without a display.

---

## 3. Module map

| Path | Responsibility |
|---|---|
| `app/core/version.py` | application and schema versions |
| `app/core/paths.py` | application directory system, data-root resolution, filename safety |
| `app/core/settings.py` | the versioned settings model, validation, load/save |
| `app/core/atomicio.py` | atomic writes, backups, quarantine of damaged files, temp hygiene |
| `app/core/events.py` | the closed vocabulary of log events |
| `app/core/logging_setup.py` | rotating structured logging, session ids |
| `app/core/errors.py` | friendly errors (what/why/what to do) + subsystem error types |
| `app/core/env.py` | machine probe: CPU, RAM, disk, GPU (optional psutil) |
| `app/core/packages.py` | Python package detection (with distribution aliases) |
| `app/core/maintenance.py` | cache/temp inspection and safe cleanup |
| `app/jobs/states.py` | job state machine (pure) |
| `app/jobs/cancel.py` | cancellation tokens + child-process handling |
| `app/jobs/progress.py` | measured progress, ETA formatting |
| `app/jobs/spec.py` | `JobSpec`/`Job`/`JobResult`, `execute_job` (pure) |
| `app/jobs/worker.py` | Qt runnable that executes a job off the UI thread |
| `app/jobs/manager.py` | submission, duplicate rejection, cancellation, shutdown |
| `app/jobs/keys.py` | stable job identifiers |
| `app/project/presets.py` | every codec/quality/format/template table - the only place they live |
| `app/project/model.py` | the typed project model: sections, scenes, assets, (de)serialisation |
| `app/project/migrations.py` | schema detection and the v1 → v2 → v3 migrations |
| `app/project/validation.py` | validation that collects every issue, plus the pre-render check |
| `app/project/layout.py` | the project folder layout and path safety |
| `app/project/store.py` | load/save, backups, autosave, recovery, rotation |
| `app/project/lock.py` | advisory project locking (Windows-safe pid probing) |
| `app/project/recent.py` | the recent-projects list and channel profiles |
| `app/project/assets.py` | import, verify, relink, replace, ignore, checksum |
| `app/project/thumbnails.py` | cached previews (Pillow only, never a render) |
| `app/project/history.py` | bounded undo/redo over project snapshots |
| `app/project/service.py` | `ProjectService`: the one place projects are created and changed |
| `app/project/scene_service.py` | `SceneService`: read-only scene queries (list/validate/info) shared by the GUI and the CLI |
| `app/scene/canvas.py` | `Canvas`, safe areas, normalised/pixel rects, anchors - resolution independence |
| `app/scene/text.py` | font resolution, text fitting, missing-glyph detection (never rewrites wording) |
| `app/scene/palette.py` | colours, gradients, theme roles, readable-on helpers |
| `app/scene/elements.py` | the element model and layout: text/image/shape/card/group/number/chart/divider/progress |
| `app/scene/shapes.py` | shape and gradient drawing primitives |
| `app/scene/charts.py` | bar/column/line/area/pie/donut/sparkline drawing from structured data |
| `app/scene/animation.py` | deterministic easings, presets (incl. value presets), keyframe-ready tracks |
| `app/scene/transitions.py` | cut/fade/slide/push/zoom/wipe/dip/dip-white blending |
| `app/scene/timing.py` | narration-driven timeline; no maximum length |
| `app/scene/compose.py` | the compositor: backgrounds (incl. images), elements, transitions, frames |
| `app/scene/templates.py` | the scene registry: named recipes that build responsive scenes |
| `app/scene/validate.py` | visual + timeline validation that collects actionable issues |
| `app/scene/storyboard.py` | storyboard rows, thumbnails, preview frames |
| `app/scene/jobs.py` | storyboard/preview job bodies and specs |
| `app/scene/service.py` | **`TimelineService`**: the one timeline, built once and validated (Stage E) |
| `app/cli/scene.py` | `motion-studio scene list\|validate\|info`, via `SceneService` |
| `app/media/probe.py` | reads a finished file's real facts from `ffmpeg -i` (no ffprobe needed) |
| `app/audio/ducking.py` | narration windows, merged ranges and the duck gain expression |
| `app/audio/mix.py` | the FFmpeg filter graph for narration → music → SFX → master |
| `app/audio/service.py` | **`AudioService`**: resolve, validate, plan, mix and preview |
| `app/subtitles/service.py` | **`SubtitleService`**: cues from measured narration, SRT/VTT/ASS, editing, validation |
| `app/render/capabilities.py` | encoders this FFmpeg actually has, detected not assumed |
| `app/render/segments.py` | segment planning, including transition overlaps |
| `app/render/frames.py` | streaming frames to FFmpeg; never holds the video in RAM |
| `app/render/encode.py` | encoder arguments, assembly, black-frame detection |
| `app/render/output.py` | output naming, sequence, staging, atomic move, render history |
| `app/render/platform.py` | editable platform presets (YouTube, Shorts, Reels, TikTok, Master, Draft) |
| `app/render/qc.py` | **`QCService`**: measures the finished file, returns PASS/WARNING/FAIL |
| `app/render/engine.py` | **`RenderEngine`**: validate → audio → subtitles → scenes → encode → QC |
| `app/render/service.py` | **`RenderService`**: plan, export options, platform presets, render |
| `app/render/jobs.py` | render, audio, subtitle, timeline, QC and capability job bodies |
| `app/tts/selftest.py` | the honest Kokoro verdict: `KOKORO VERIFIED` or `... NOT VERIFIED` |
| `app/cli/render.py` | `motion-studio render\|audio\|subtitles …`, sharing the GUI's services |
| `app/tools/ffmpeg.py` | FFmpeg/FFprobe discovery, safe subprocess execution |
| `app/tools/kokoro.py` | legacy Kokoro probe kept for Stage B callers; superseded by `app/tts/capabilities.py` |
| `app/script/parser.py` | plain ↔ structured script parsing; unknown labels are preserved, never dropped |
| `app/script/stats.py` | word/character/sentence/paragraph counts and labelled duration estimates |
| `app/script/io.py` | encoding validation, Markdown handling, import and export |
| `app/tts/preprocess.py` | safe, configurable text preparation (wording is never rewritten silently) |
| `app/tts/capabilities.py` | Kokoro probe: package, runtime, model, voices, languages, real initialisation |
| `app/tts/voices.py` | voice catalogue, discovery from the model, filtering, choice validation |
| `app/tts/engine.py` | the one Kokoro engine: lazy load, synthesis, cancellation, self-test |
| `app/tts/audio.py` | WAV writing and strict validation (rate, channels, size, duration) |
| `app/tts/cache.py` | source/settings hashes and the staleness comparison |
| `app/tts/narration.py` | the script → preprocess → Kokoro → WAV → duration pipeline and status states |
| `app/tts/jobs.py` | the four voice/narration job bodies and their specs |
| `app/checks/status.py` | check framework: statuses, results, report, registry |
| `app/checks/items.py` | the actual readiness checks |
| `app/checks/job.py` | system check as a background job |
| `app/ui/context.py` | `AppContext`: paths, settings, jobs, report for the UI |
| `app/ui/main_window.py` | shell: navigation, status bar, menus, close handling |
| `app/ui/theme.py` | single source of colours, spacing and styles |
| `app/ui/notifications.py` | dialogs: friendly errors, confirmations, results |
| `app/ui/error_handler.py` | global error boundaries (main thread + guarded slots) |
| `app/ui/single_instance.py` | one instance per data folder |
| `app/ui/project_controller.py` | Qt glue: dialogs, autosave timer, dirty state - no logic of its own |
| `app/ui/wizard/new_project.py` | the 9-step New Project wizard |
| `app/ui/dialogs/project_dialogs.py` | Save/Discard/Cancel, recovery, conflict and missing-asset dialogs |
| `app/ui/views/script_view.py` | the Script page: editor, plain/structured toggle, counts, import/export, narration state |
| `app/ui/views/narration_view.py` | the Narration page: engine card, voice table, preview, generation, status |
| `app/ui/views/audio_view.py` | the Audio page: narration, music, effects, mix and ducking |
| `app/ui/views/subtitles_view.py` | the Subtitles page: caption list, style, split/merge/delete, export |
| `app/ui/views/timeline_view.py` | the Timeline page: real timings, excluded scenes, validation |
| `app/ui/views/render_view.py` | the Render page: platform, resolution, quality, plan, render, QC |
| `app/ui/views/*` | other pages (dashboard, project, project settings, browser, system check, settings, maintenance, diagnostics) |
| `app/ui/widgets/*` | shared widgets (cards, rows, job panel) |
| `app/diagnostics/smoke.py` | end-to-end self-test |

---

## 4. The job system

The job system is the backbone of the application's responsiveness.

```
submit(spec)                 worker thread                UI thread
────────────                 ─────────────                ─────────
JobManager.submit  ───────►  JobWorker.run
  duplicate check              execute_job(job)
  create Job                     raise_if_cancelled()
  start on QThreadPool           body(context)  ── progress ──►  job_progress signal
                                 finalize()     ── result   ──►  job_finished signal
```

Rules enforced in code:

* `JobManager.submit` **refuses** a second job with the same key while one is
  running (unless the spec explicitly allows parallels). This is the "click
  Generate twice" protection.
* `execute_job` is the only place a job body runs. It runs it once, converts any
  exception into a result, and never retries.
* A job that returns while cancelled is reported as **cancelled**, not as
  success — a partial result must never be presented as a finished video.
* `JobStateMachine` guarantees one terminal state; a duplicate completion signal
  is logged and ignored.
* Cancellation is cooperative (`CancelToken.raise_if_cancelled()`), and any child
  process registered with the token is terminated, so FFmpeg cannot be left
  running.
* `JobManager.shutdown()` cancels everything, waits (bounded) for the pool, and
  reports anything that refused to stop.

Threading model: `QRunnable` on a `QThreadPool` capped at two threads. No
`multiprocessing`, no `fork`, no child process that imports the application -
the design is Windows-safe by construction.

---

## 5. Startup sequence

`app/main.py` performs, in order:

1. parse arguments
2. create the Qt application object
3. resolve and create the folder layout (falling back to the per-user folder if
   the first choice is not writable)
4. start logging (+ faulthandler writing `logs/crash-python.log`)
5. route Qt's own messages into the log (deduplicated)
6. clean stale temporary files
7. single-instance guard
8. load settings, apply theme
9. install the global exception hook
10. create the job manager and `AppContext`
11. build and show the main window
12. **then**, off the UI thread, run the readiness check

The window is interactive before any check runs; opening the application never
waits for FFmpeg, a model or a disk scan.

---

## 6. Data safety

* `atomicio.atomic_write_bytes/text/json`: write to a temporary file in the
  destination folder, `flush` + `fsync`, then `os.replace` (atomic on the same
  volume), then best-effort directory fsync.
* `save_with_backup`: copy the current file to `backups/` (pruning old ones)
  before replacing it.
* `load_json`: distinguishes "missing" from "damaged", and copies a damaged file
  to a quarantine folder instead of discarding it.
* `maintenance.clear_folder_safely`: refuses any path that resolves outside the
  data folder, and skips symlinks.
* `unique_path`: never returns a name that already exists, so finished videos
  are never overwritten.

---

## 7. Logging

Every line is `timestamp | level | session | process/thread | logger | message`,
and major actions carry a structured event:

```
EVENT=JOB_START | System check | job=system.check id=c45d2e0c8ae0
EVENT=FFMPEG_DETECTED | FFmpeg and FFprobe detected | ffmpeg_version=7.0.2 ...
```

The complete vocabulary lives in `app/core/events.py`; because it is a closed
list, `grep EVENT=RENDER_START logs/studio.log` keeps working as the application
grows. Logging degrades to a no-op handler if the log folder is not writable -
it never prevents the application from starting.

---

## 8. Error handling

```
AppError (friendly, expected)          unexpected exception
        │                                       │
        └──────────► to_friendly() ◄────────────┘
                          │
             FriendlyError{title, what_happened, why,
                            actions, technical, log path}
                          │
      ┌───────────────────┼────────────────────┐
  job result          dialog              status bar
```

Subsystem failures are contained: a job failure is a result, not a crash; a
broken check is one warning in a report that still completes; an unhandled
exception in a slot is caught by `error_handler` and reported without killing the
application.

---

## 9. Adding a feature (checklist)

1. Does it need to run for more than a few milliseconds? Make it a job with a key
   in `app/jobs/keys.py`.
2. Does it touch the disk? Use `atomicio`, and never inside a protected folder
   without an explicit, tested reason.
3. Can it fail? Raise an `AppError` subclass with what/why/what-to-do, or let the
   job layer convert the exception.
4. Does it need a setting? Add it to `app/core/settings.py` with validation and a
   UI row; never store state in a widget as the source of truth.
5. Does it produce output? Report it through progress and log an event.
6. Add tests: the happy path, the failure path and the cancellation path.
