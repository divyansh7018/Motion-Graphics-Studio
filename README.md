# Motion Graphics Studio

A **local, offline desktop studio** for producing narrated videos: script →
voice → images → scenes → timeline → audio → preview → MP4, all on one PC.
No account, no cloud, no subscription, no internet connection required for any
core function.

**Build stage: A — foundation.** This build is the stable application shell:
settings, folder management, structured logging, a safe background job system
with cancellation, and a full system check. Project creation, narration and
rendering are implemented in later stages (see `docs/ROADMAP.md`).

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
| Automated test suite (147 tests) | ✅ passing |
| Project model, script, voice, scenes, render, QC | ⏳ later stages |
| Licensing, accounts, payments | ❌ not in this build (Phase 2, later) |

Nothing in the interface pretends to work: pages for later stages are shown as
`(later)`, are not clickable, and every one of them explains which stage
delivers it. There are no placeholder buttons.

---

## Requirements

* **Windows 10/11, 64-bit** (first supported release target)
* Python 3.10 or newer, 64-bit
* 8 GB RAM minimum, 16 GB recommended
* **CPU rendering is the default**: no NVIDIA GPU, no CUDA, no large VRAM is
  assumed or required
* FFmpeg + FFprobe (the setup tool tells you exactly where to put them)
* Kokoro-82M for narration — **optional**, installable later

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
| `motion-studio smoke-test` | full end-to-end self-test |
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
| `docs/PROJECT_FORMAT.md` | the versioned project file format (Stage B) |

---

## Licence and commercial features

Licensing, activation, payments, accounts and DRM are **not** part of this build
and will not be added until the local production workflow (script → video) is
proven stable on the target machine. See `docs/ROADMAP.md`, stage K.
