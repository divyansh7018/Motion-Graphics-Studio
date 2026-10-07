# Motion Graphics Studio

A **local, offline desktop studio** for producing narrated videos: script →
voice → images → scenes → timeline → audio → preview → MP4, all on one PC.
No account, no cloud, no subscription, no internet connection required for any
core function.

**Build stage: F — Image Studio, local image generation, editing and asset
integration.** This build adds a local image studio: create or import pictures,
edit them non-destructively, generate with a local backend if one is installed,
organise them in a paged library, and send them into a project or a scene. It
keeps the Stage A shell, the Stage B project system, the Stage C script + Kokoro
narration, the Stage D scene engine and the Stage E audio/subtitle/timeline/render
pipeline intact.

> **Image Studio works with no image model installed.** Importing, editing,
> upscaling, masking, organising and sending to a scene need none, and a prompt
> with no model is refused with an explanation rather than faked. See
> `docs/STAGE_F_REPORT.md` for exactly which backends were verified here — most
> AI ones were not, because the test machine has none installed.

**Stage E**, kept intact by this build, turns a project into a real MP4: a
narration/music/effects mix with ducking, captions generated from measured
narration timings, one timeline shared by every part of the pipeline, a
streaming render with resume and cancellation, and a quality check that measures
the finished file.

> **Four statuses that must not be blurred together:**
>
> * **PIPELINE VERIFIED** — 1492 tests, a 35/35 Stage E matrix and a 29/29
>   Stage F matrix, real MP4 renders measured with FFprobe, and 22 real bugs
>   found and fixed by the hardening passes - 13 in Stage F (`STAGE_F_REPORT.md`
>   §8) and 9 more in the Stage E render/audio/caption chain
>   (`STAGE_E_REPORT.md` §8).
> * **KOKORO NOT VERIFIED — TEST FALLBACK USED** — the `kokoro` package is
>   installed but there are no model weights on the verification machine, so the
>   narration in every test render is explicitly labelled synthetic audio. Run
>   `motion-studio voice selftest` on a machine with the weights for the real
>   verdict.
> * **WINDOWS NOT VERIFIED** — the verification ran on Linux. Windows paths,
>   `%TEMP%`, shortcut independence and `ffmpeg.exe` discovery are still to be
>   checked on a real Windows 10/11 machine.
> * **NO AI IMAGE MODEL INSTALLED** — text-to-image and friends were verified
>   through the built-in and local-command adapters (real code paths, not real
>   models). AI upscaling and background removal report NOT INSTALLED, and real
>   diffusion inference is NOT VERIFIED.
>
> Neither the Stage E gate nor the Stage F gate is therefore **declared
> complete**. The Stage E evidence table is in `docs/STAGE_E_REPORT.md` §15 and
> the Stage F one in `docs/STAGE_F_REPORT.md` §0, with the outstanding items in
> §10.

---

## What works in this build

| Area | State |
|---|---|
| Application window, navigation, status bar | ✅ working |
| Settings (all sections, validated, backed up) | ✅ working |
| Application folder layout (`config/ projects/ assets/ …`) | ✅ working |
| Structured logging with session ids | ✅ working |
| Background jobs with progress, ETA and cancel | ✅ working |
| System check (FFmpeg, Python, folders, disk, packages, voice engine) | ✅ working |
| FFmpeg / FFprobe detection and a real encode self-test | ✅ working |
| Cache and temporary-file cleanup | ✅ working |
| Built-in end-to-end smoke test | ✅ working |
| Projects: create, open, save, save as, duplicate, rename, delete | ✅ working |
| Project settings (format, quality, voice, audio, theme, export, assets) | ✅ working |
| Autosave, backups and crash recovery | ✅ working |
| Recent projects, dashboard cards and project browser | ✅ working |
| Missing-asset reporting with relink | ✅ working |
| Project validation, including a pre-render check | ✅ working |
| Project command line (`motion-studio project …`) | ✅ working |
| Script editor: plain text, structured scenes, counts, duration estimate | ✅ working |
| Script import (TXT/Markdown) and export (TXT/Markdown/structured) | ✅ working |
| Kokoro engine detection with a real initialisation self-test | ✅ working |
| Dynamic voice and language discovery from the installed model | ✅ working |
| Voice preview with its own text, speed and volume | ✅ working |
| Narration generation to validated WAV, with measured duration | ✅ working |
| Narration staleness, missing-file handling and regeneration | ✅ working |
| Script and narration command line (`motion-studio script` / `voice` / `narration`) | ✅ working |
| Automated test suite (1163 tests) | ✅ passing |
| Scenes, storyboard, scene preview | ✅ working |
| Audio mix: narration, music, effects, ducking, normalisation | ✅ Stage E (this build) |
| Audio preview without rendering video | ✅ Stage E (this build) |
| Subtitles: SRT, WebVTT, styled ASS, burnt-in | ✅ Stage E (this build) |
| Caption editing: split, merge, delete, reword, style, safe areas | ✅ Stage E (this build) |
| Timeline page with validation (errors block, warnings continue) | ✅ Stage E (this build) |
| Render page: platform presets, resolutions, fps, quality, codecs | ✅ Stage E (this build) |
| Codec detection from the local FFmpeg build | ✅ Stage E (this build) |
| Output numbering — never overwrites an earlier take | ✅ Stage E (this build) |
| Quality check on the finished file (PASS / WARNING / FAIL) | ✅ Stage E (this build) |
| Render cancellation and resume after interruption | ✅ Stage E (this build) |
| Render, audio, subtitle and timeline command line | ✅ Stage E (this build) |
| Image Studio page: import, edit, generate, organise, send to scene | ✅ Stage F (this build) |
| Six image backends behind one provider contract, capability-driven UI | ✅ Stage F (this build) |
| Non-destructive editing (16 operations, undo/redo/reset) | ✅ Stage F (this build) |
| Image library: paging, search, tags, collections, duplicates, thumbnails | ✅ Stage F (this build) |
| Image metadata (side-car + inside PNG), history, version graph | ✅ Stage F (this build) |
| Images in projects and scenes by asset id, surviving a move | ✅ Stage F (this build) |
| Kokoro narration verified on this machine | ⚠️ NOT VERIFIED — no model weights |
| Image generation with a real AI model | ⚠️ NOT VERIFIED — none installed here |
| Background removal, AI upscaling | ⚠️ NOT INSTALLED — refused, never faked |
| Windows 10/11 end-to-end run | ⚠️ NOT VERIFIED — verification ran on Linux |
| FFprobe used to measure finished files | ✅ verified (6.0-static) |
| Two-pass encoding | ✅ verified — both passes really run |
| Image generation and a video library | ⏳ later stages |
| Licensing, accounts, payments | ❌ not in this build (Phase 2, later) |

Nothing in the interface pretends to work: the only pages still shown as
`(later)` are Visuals (Stage H) and Video library (Stage G). A test asserts
that no button on the Stage E pages is wired to nothing.

---

## Requirements

* **Windows 10/11, 64-bit** (first supported release target)
* Python 3.10 or newer, 64-bit
* 8 GB RAM minimum, 16 GB recommended
* **CPU rendering is the default**: no NVIDIA GPU, no CUDA, no large VRAM is
  assumed or required
* FFmpeg + FFprobe (the setup tool tells you exactly where to put them)
* Kokoro-82M for narration — **optional**, installable later:
  `python -m pip install kokoro onnxruntime` plus the model weights. Without it
  the application still starts and every other function works; the Narration page
  and the System Check say plainly what is missing and how to fix it

The application also runs on macOS and Linux for development; the release
target is Windows.

---

## Install

```bat
:: 1. Get the code, then create an environment (once)
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

:: 2. Verify everything and create the folders
python installer\setup_windows.py

:: 3. Start the application
python run_studio.py
```

To check the machine without starting the interface:

```bat
python -m app.cli.main check --deep
python -m app.cli.main smoke-test
```

### FFmpeg

The application never assumes FFmpeg is installed globally. It looks, in order,
in:

1. the path you set in **Settings → Media tools**
2. the `tools/` folder inside the application folder
3. common install locations
4. the Windows `PATH`

Put `ffmpeg.exe` and `ffprobe.exe` in `tools/` and press **Re-check system**.

### Voice (narration)

Narration is optional and installed separately, because the model is a large
download:

```bat
pip install -r requirements-voice.txt
```

The application detects the engine, the runtime, the model files and the
available voices. Voice names are **always read from the installed model** —
nothing is hard-coded. If the engine is missing, the System check page says so
and explains how to install it; everything else keeps working.

---

## Everyday use

* **System check** – tells you what is ready (✓), optional (⚠) and missing (✗).
* **Settings** – every option is validated and saved atomically, keeping a
  backup of the previous file.
* **Maintenance** – see and clear cached/temporary files. Your projects, assets
  and finished videos are never in that list.
* **Diagnostics** – machine details, folders, settings and the recent log, all
  copyable in one click for support.
* **Tools → Run smoke test** – proves end to end that folders, settings,
  FFmpeg, encoding, job handling and cancellation all work.

### Command line

| Command | Purpose |
|---|---|
| `motion-studio setup` | create/verify folders and print a readiness report |
| `motion-studio check [--deep]` | run the system check (`--deep` also encodes a test video) |
| `motion-studio info` | folders, machine facts, settings, disk usage |
| `motion-studio clean [--all]` | clear cached/temporary files |
| `motion-studio smoke-test` | full end-to-end self-test (includes the narration workflow when Kokoro is installed) |
| `motion-studio script show <project>` | print the script with its word/sentence/duration counts |
| `motion-studio script import <project> <file>` | import a `.txt`/`.md` script, stored exactly as written |
| `motion-studio script export <project> <file> [--format md\|script]` | export the script (refuses to overwrite without `--force`) |
| `motion-studio script convert <project> --to structured` | print the script in another format without changing it |
| `motion-studio voice list [--language h] [--gender female]` | voices discovered from the installed model |
| `motion-studio voice check` | engine, runtime, model, voices, languages and whether it initialises |
| `motion-studio voice preview --voice <id> [--text …]` | speak one sentence to a WAV file (never touches a project) |
| `motion-studio narration generate <project> [--voice … --speed …]` | generate the narration audio for a project |
| `motion-studio narration status <project>` | narration state, staleness, duration and files |
| `motion-studio voice selftest` | generate one real narration file and report `KOKORO VERIFIED` or `KOKORO NOT VERIFIED - TEST FALLBACK USED` |
| `motion-studio render timeline [--project …]` | scene timings and whether they are valid (same service the GUI uses) |
| `motion-studio render validate [--project …]` | every problem that would block a render, before drawing a frame |
| `motion-studio render preview [--project …]` | length, frames, output name and estimated size |
| `motion-studio render run [--project …] [--burn-subtitles]` | render the finished MP4, then quality-check it |
| `motion-studio render status [--project …]` | encoders this FFmpeg has, plus render history |
| `motion-studio audio validate <project>` | check every audio track exists and fits the timeline |
| `motion-studio audio mix <project>` | mix the master audio to a WAV you can listen to |
| `motion-studio subtitles export <project>` | write the `.srt` / `.vtt` caption files |
| `motion-studio gui` | start the interface |

Exit codes: `0` success, `1` problems found, `2` blocked (a required component
is missing), `3` unexpected error.

---

## Where your files live

```
<data folder>/            (the app folder, or %LOCALAPPDATA%\MotionGraphicsStudio)
    config/     settings.json, session state
    projects/   one folder per project
    assets/     imported images, music, sound effects
    templates/  reusable project templates
    themes/     visual themes
    models/     local voice and image models
    workspace/  per-project scratch data
    cache/      regenerable caches
    previews/   preview renders and storyboard thumbnails
    output/     finished videos (Video.mp4, Video2.mp4, … never overwritten)
    logs/       studio.log, crash log
    backups/    automatic backups of settings and projects
    temp/       short-lived working files (cleaned safely)
```

The folder is chosen automatically (application folder first, otherwise the
per-user location), can be overridden with `--data-root` or the
`MGS_DATA_ROOT` environment variable, and is remembered between runs. The
application works the same when started from a shortcut or from any working
directory.

---

## Data safety

* Writes are **atomic**: a temporary file is flushed and then renamed, so a
  crash or power cut cannot leave a half-written file behind.
* A backup of the previous version is kept before settings (and later,
  projects) are replaced.
* A damaged file is **recovered, never discarded**: it is copied to
  `backups/` for inspection and the defaults are used.
* Starting a render never overwrites an existing video: outputs are numbered.
* Cleaning caches can only touch the cache/temp folders, and refuses any path
  that resolves outside the data folder.

---

## Troubleshooting

The application always says **what happened**, **why** and **what you can do** —
never just "something went wrong". Technical details and the log path are one
click away in every error dialog (`Copy details`).

* **Nothing starts.** Run `python -m app.cli.main check` and read the report.
* **FFmpeg not found.** Put `ffmpeg.exe` and `ffprobe.exe` in `tools/`, or set
  the path in Settings → Media tools.
* **The interface feels stuck.** Heavy work always runs in the background with
  a Cancel button; if a task is slow the status bar shows elapsed time and an
  estimate. Nothing runs on the interface thread.
* **Something looks wrong.** `Tools → Run smoke test` and send the log from
  `logs/studio.log` (Help → Copy diagnostics for support).

---

## Documentation

| File | Contents |
|---|---|
| `docs/ARCHITECTURE.md` | how the code is organised and why |
| `docs/ROADMAP.md` | the staged plan (A → J) and what each stage delivers |
| `docs/DEVELOPMENT.md` | running, testing and debugging the app |
| `docs/TESTING.md` | what the test suite covers and how to run it |
| `docs/STAGE_A_REPORT.md` | evidence that this build meets the Stage A gate |
| `docs/STAGE_B_REPORT.md` | evidence that this build meets the Stage B gate |
| `docs/STAGE_C_REPORT.md` | evidence that this build meets the Stage C gate |
| `docs/STAGE_D_REPORT.md` | evidence that this build meets the Stage D gate |
| `docs/STAGE_E_REPORT.md` | evidence for the Stage E gate, and what is still unverified |
| `docs/STAGE_F_REPORT.md` | evidence for the Stage F gate: what was verified, what was not, and why |
| `docs/IMAGE_STUDIO.md` | how to use Image Studio |
| `docs/IMAGE_BACKENDS.md` | the six backends, their capabilities and how to add one |
| `docs/evidence/` | the actual rendered MP4s and caption files |
| `docs/PROJECT_FORMAT.md` | the versioned project file format (schema v2) |

---

## Licence and commercial features

Licensing, activation, payments, accounts and DRM are **not** part of this build
and will not be added until the local production workflow (script → video) is
proven stable on the target machine. See `docs/ROADMAP.md`, stage K.
