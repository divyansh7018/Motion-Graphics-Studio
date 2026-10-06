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
* 591 automated tests in total (230 added in this stage)

## Stage D — scene engine and storyboard (complete)

See ``docs/STAGE_D_REPORT.md`` for the full evidence.  Highlights:

* A resolution-independent scene engine: positions are fractions of the frame and
  sizes are fractions of the shorter edge, so one project lays out correctly in
  landscape, portrait, square and 4K.  An AST test forbids hard-coded resolutions.
* Visual elements: text (responsive fitting that never rewrites wording), images
  (cover/contain/fill with a labelled placeholder when missing), shapes, cards,
  groups, numbers, progress bars and charts (bar/column/line/area/pie/donut/sparkline).
  Backgrounds can be a colour, a gradient or a cover-fitted image with an overlay.
* A scene registry of twenty-two templates (hook, title, body, image, stat, quote,
  bullets, chart, cta, divider, paragraph, list, counter, progress, comparison,
  before/after, timeline, bento, collage, end screen, logo, blank); registration is
  the only step to add a type.
* Animation as a pure function of time: enter/exit presets and keyframe-ready
  tracks, plus value animations (count-up, progress-fill), slide up/down, scale
  out, fade out and a deterministic shake; direction/intensity/repeat/stagger
  parameters.  Transitions: cut/fade/slide/zoom/wipe/push/dip/dip-to-white.
* Narration-driven timing with opt-in head/tail padding and **no maximum length**
  (a ~3-hour timeline is tested); scenes can be disabled (dropped from the cut but
  kept in the project) or locked against accidental edits.
* A Storyboard page: add-from-template, duplicate, rename, reorder, delete,
  enable/disable and lock as single undoable edits, with live thumbnails and
  previews rendered off the Qt thread; a scene editor with an element list and a
  property inspector (z-order, duplicate, lock, text) - all undoable.
* A shared `SceneService` and `motion-studio scene list|validate|info` CLI, so the
  command line reports exactly what the storyboard sees.
* 889 automated tests in total (298 added in this stage).

---

## Stage E — audio, subtitles, timeline, render and QC (implemented; gate NOT signed off)

See ``docs/STAGE_E_REPORT.md`` for the full evidence.  Highlights:

* A real audio subsystem: narration, music beds, ambience, intro/outro and
  sound effects with volume, trim, fades, loop and mute; a Narration → Music →
  SFX → Master chain with normalisation and gentle ducking by default.
* Captions generated from **measured** narration timings, exported as SRT,
  WebVTT and styled ASS, with burn-in through libass.  No word-level timings are
  invented, and captions outside the safe area are reported rather than moved.
* ``TimelineService`` builds the one timeline the preview, audio, subtitles and
  render all use, so they cannot disagree.  Fatal errors block a render;
  warnings are reported and the user decides.
* A streaming render engine: scene frames → segments → assemble → mux → QC →
  atomic move.  Never loads the whole video in RAM, has no duration cap, caches
  segments so an interrupted render resumes, and cancels by killing FFmpeg.
* Codec availability is detected from the local FFmpeg, so an unavailable codec
  is refused before a frame is drawn.
* Output numbering never overwrites an earlier take; a finished file is written
  to a temporary name and moved atomically after QC.
* A quality check that measures the real file - dimensions, frame rate,
  duration, codecs, audio, silence, clipping, black frames - and returns
  PASS / WARNING / FAIL.  A FAIL is never reported as success.
* Four working pages (Audio, Subtitles, Timeline, Render) and
  ``motion-studio render|audio|subtitles …`` commands sharing the same services.
* 1181 automated tests in total (268 added in Stage E, plus 20 in the final
  hardening pass).

### The final hardening pass

A verification pass ran after the suite was already green, and found four things
it was hiding:

* a QC check that compared the container duration with **itself**, so a video
  whose audio was seconds shorter than its picture passed;
* a two-pass checkbox whose value never reached FFmpeg;
* ``log_event(..., message=...)`` duplicating a positional argument, so a
  project with no audio at all failed to render with an internal error;
* a QC report with no way to say "I could not run that check", which let an
  unavailable dependency look like a pass.

It also produced the first real measurements rather than design claims:
FFprobe 6.0 verified and actually used, two-pass encoding verified with both
passes in the log, and a memory profile (peak 138.9 MiB) from
``scripts/stage_e_profile.py``.

### Why the gate is not signed off

Two required-for-release items were **not** verified on the verification
machine, for environmental reasons, and are labelled separately rather than
being folded into the rest:

* **KOKORO NOT VERIFIED** - no model weights available.  The pipeline was
  verified with explicitly labelled synthetic test narration, and
  ``motion-studio voice selftest`` reports which of the two states applies.
* **WINDOWS NOT VERIFIED** - the verification ran on Linux, so Windows paths,
  temp handling, shortcut independence and ``ffmpeg.exe`` discovery are
  untested.

Every other mandatory Stage E criterion is verified by a real run.  See
``docs/STAGE_E_REPORT.md`` §15.

## Stage F - Image Studio and local image generation (implemented; gate NOT signed off)

See ``docs/STAGE_F_REPORT.md`` for the full evidence.  Highlights:

* An ``app/image`` package: a provider contract with **six** backend adapters
  (built-in, local command, local HTTP, ComfyUI, Diffusers, ONNX), a model
  manager, a capability system and a device report that does not need a GPU.
* The studio works with **no model installed** - importing, editing, upscaling,
  masking, organising and sending to a scene need none.  A prompt with no model
  is refused with an explanation, never faked.
* Non-destructive editing built on a list of 16 operations, so Reset costs
  nothing and the original is never modified.  Overwriting one is a separate,
  confirmed action that keeps a backup.
* Metadata written twice (a side-car and inside the PNG), a version graph
  (source -> variation -> edit -> upscale), generation and prompt history.
* A paged, searchable, tagged library with cached thumbnails; a deleted file is
  reported as missing rather than vanishing.
* Images go into projects and scenes **by asset id**, so a project can be moved
  and still find them, and there is only one image system.
* ``motion-studio image ...``: ten sub-commands sharing the same services.

### Why the gate is not signed off

* **No AI model is installed on the verification machine.**  Text to image,
  image to image, inpaint, outpaint and upscale were verified through the
  built-in and local-command adapters - real code paths, not real models - and
  the AI upscaler and background-removal paths are **NOT INSTALLED**.
* **The Diffusers, ComfyUI and ONNX adapters stop at the model-loading
  boundary** and raise rather than guess.  Real inference is **NOT VERIFIED**.
* **Windows remains NOT VERIFIED** (carried from Stage E).
* **Kokoro remains NOT VERIFIED - TEST FALLBACK USED** (carried from Stage E).

### The measurement that mattered most

The first profiling run showed detection costing 1.4 s and **+476.7 MiB of RSS**
because it imported torch to ask whether a GPU existed - and then reported
"torch: not installed" when it was installed with no accelerator.  Detection now
costs **2 ms and +0.0 MiB**, and the deep probe tells the truth.

## Later stages (summary)

Stage F built the image side of the application.  Stage E pulled forward most of
what was originally scoped for F and G, because a
render cannot be verified without them: streaming frames to FFmpeg,
memory-bounded rendering, cancellation that terminates FFmpeg, numbered output
files, post-render validation, black-frame and silent-audio detection and
PASS/WARNING/FAIL reporting all exist now. What is left for F and G is listed
below; neither is complete.

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
