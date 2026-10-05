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
* 361 automated tests in total (208 added in this stage)

---

## Stage C — script and Kokoro narration (complete)

* script editor with plain-text and structured modes; a beginner can paste an
  ordinary script and it becomes one narration block
* human-readable block syntax (`[SCENE 01]` + `Narration:` / `On Screen:` /
  `Visual:` / `Music:` / `SFX:` / `Duration:`) with 24 field aliases; the parser
  never destroys text it does not understand (unknown labels are kept)
* the user's original text is stored verbatim in `script.source_text`; conversion
  and flattening are explicit actions, never automatic
* word / character / sentence / paragraph counts plus a duration estimate that is
  always labelled an estimate — the measured WAV duration is authoritative
* import TXT/Markdown with encoding validation (binary input is refused), export
  TXT/Markdown/structured, and export refuses to overwrite an existing file
* Kokoro capability probe that verifies the engine can actually initialise, not
  merely that a package imports; the System Check runs a real mini TTS self-test
* voices and languages discovered from the installed model — never a fixed list
  or a fixed count; per-voice Available/Unavailable with a plain-language reason
* Narration page: language → gender → voice → preview → speed → volume →
  generate; technical voice IDs only in an optional advanced panel
* voice preview is a separate job with its own text, writing only a small file in
  `previews/` — it never creates a render, a video or an FFmpeg job
* narration generation to validated WAV in `audio/narration/` with deterministic
  names, supporting both one continuous file and one file per section
* speed 0.75–1.50 plus custom within the engine's range, volume 0–125% with
  peak-limited amplification, both stored in the project
* narration status is an enum (`not_generated`/`generating`/`ready`/`stale`/
  `failed`/`cancelled`/`missing`), never a boolean; script, voice, speed, volume,
  preprocessing or model changes mark the audio **stale** instead of reusing it
* a missing WAV is reported with Regenerate / Relink / Ignore and never crashes
* lazy model load inside the job, one engine instance per job, released
  afterwards; sequential generation, no parallel workers, no fork
* CLI: `motion-studio script|voice|narration …`
* 566 automated tests in total (205 added in this stage)

---

## Later stages (summary)


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
