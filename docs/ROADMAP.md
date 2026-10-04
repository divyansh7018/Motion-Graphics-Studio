# Roadmap

The application is built in stages. **A stage is not started until the previous
stage passes its tests**, and each stage is finished with implementation, tests,
manual verification and a list of remaining limitations (directive section 74/75).

Anything not implemented yet is shown in the interface as `(later)` and is not
clickable. Nothing is faked.

| Stage | Contents | State |
|---|---|---|
| **A** | Application shell, settings, folder system, logging, job system, system check, smoke test | ✅ **done** (see `STAGE_A_REPORT.md`) |
| **B** | Project model (`project.json`), new/open/save, autosave, backup, crash recovery, recent projects | ⏳ next |
| **C** | Kokoro TTS: engine init, dynamic voice discovery, voice browser, voice preview, narration generation + audio validation | ⏳ |
| **D** | Scene engine, storyboard, responsive layout, text fitting, preview (draft/medium) | ⏳ |
| **E** | Audio: music/SFX mixing, ducking, optional subtitles | ⏳ |
| **F** | FFmpeg renderer: streaming frames, timing from real narration durations, progress, cancel | ⏳ |
| **G** | Output manager, final file validation, QC report (PASS/WARNING/FAIL), video library | ⏳ |
| **H** | Image system: import with validation, thumbnails, optional local AI backends behind an interface | ⏳ |
| **I** | Advanced scene/code mode, element tree, timeline editing | ⏳ |
| **J** | Polish, performance profiling, full regression pass on the target machine | ⏳ |
| **K** | Commercial features: licensing, activation, payments, accounts | ❌ **not before J is signed off** |

---

## Stage A — delivered

**Goal:** a stable, smooth shell that a creator can open, configure, diagnose and
trust, with the machinery the later stages need.

Delivered:

* application window with navigation, status bar, menus and a simple beginner
  layout; advanced tooling is hidden until Advanced mode is on
* settings for every subsystem, validated, clamped, saved atomically with
  backups, and restorable to defaults
* predictable folder layout with a resolution order that works from a shortcut
  or any working directory, portable or per-user
* structured logging with session ids, rotation, a crash log and a Qt message
  bridge
* background job system: measured progress, elapsed time, ETA, cancel,
  duplicate-submission rejection, one completion event per job, clean shutdown
* system check with ✓/⚠/✗ per item, plus a real FFmpeg encode self-test
* FFmpeg/FFprobe discovery across four locations with configurable paths
* optional Kokoro detection (never loaded at startup)
* cache/temp maintenance with hard safety guarantees
* diagnostics page with a bounded log tail and one-click copy
* built-in end-to-end smoke test (10 checks, including a genuine encode)
* 153 automated tests, including GUI, job-manager and CLI tests
* setup tool (`installer/setup_windows.py`) that verifies rather than assumes

Not in this stage (by design): projects, script, narration, scenes, rendering.

---

## Stage B — project model and lifecycle (complete)

* one versioned `project.json` (schema v2) per project, read by the GUI, the CLI
  and the tests through a single `ProjectService`
* 9-step New Project wizard (name → channel → template → format → resolution →
  fps → quality → voice → create); the voice step lists only voices discovered
  from the installed engine
* dashboard with recent-project cards, project browser with search/sort/filters,
  project page and an eight-tab Project settings page
* create / open / save / save-as / duplicate / rename / rename folder / delete,
  all with atomic writes and a rotating backup chain
* autosave on a configurable timer into `autosave/`, never into `project.json`;
  change detection so unchanged projects are not rewritten
* crash recovery on startup: Restore / Open original / Ignore, decided by
  content comparison rather than timestamps
* v1 → v2 migration, and a hard refusal for files from a newer schema
* validation that collects **every** issue ("3 issues found"), plus a stricter
  pre-render check for codec/container/quality combinations
* missing assets reported with name, expected path and Relink / Replace / Ignore
* external-modification detection with Reload / Keep / Save as
* advisory project locking that survives a crash (stale locks are removed)
* undo/redo for project edits with a bounded history
* CLI: `motion-studio project create|open|info|validate|list|duplicate|rename|recovery`
* 360 automated tests in total (207 added in this stage)

---

## Later stages (summary)

**C — Voice.** Kokoro lazy init, dynamic voice discovery from the model, voice
browser (language → gender → voice), preview independent of the render pipeline,
narration generation with per-scene audio files, audio validation before any
video work, explicit TTS error handling for every failure mode.

**D — Scenes and storyboard.** Deterministic scene rendering, responsive layout
for 1920×1080, 1080×1920, 1080×1080, 1080×1350, 1280×720 and custom sizes, text
fitting with warnings before render, storyboard built from the real project
scenes, draft/medium previews that are much faster than a final render.

**E — Audio.** Narration + music + SFX mixing with narration dominant, ducking,
clipping detection, optional subtitles with validation.

**F — Rendering.** Streaming frames to FFmpeg, real narration durations as the
authority for timing, configured padding, memory-bounded rendering, cancellation
that terminates FFmpeg, no hidden retries.

**G — Output and QC.** Numbered output files, post-render validation with
FFprobe (exists, size, resolution, fps, duration, streams, codec, container),
black-frame and silent-audio detection, PASS/WARNING/FAIL reporting, video
library.

**H — Images.** Import with validation (exists, format, dimensions, readable
pixels), asynchronous cached thumbnails, procedural graphics, and an optional
image-AI provider interface that simply reports "no backend installed" when
there is none.

**I — Advanced mode.** Element tree, timeline editing, scene code view.

**J — Polish.** Profiling on the target machine, memory tuning, regression pass,
documentation of every remaining limitation.

**K — Commercial.** Only after J is signed off, and only if the local workflow
(script → voice → scenes → render → QC) is proven reliable on the target
Windows CPU-only machine.
