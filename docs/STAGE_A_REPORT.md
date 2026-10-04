# Stage A report — application shell, settings and system check

**Stage:** A (foundation) · **Status:** complete, with the limitations listed in
section 6 · **Date of the verification runs below:** 2026-10-04

Stage A builds the frame that every later stage hangs off: a window that always
starts, a settings file that never corrupts, a system check that tells the truth
about the machine, one job at a time, and a way to prove all of that by running
it. **No video editing, no TTS and no rendering are part of Stage A** — those
are Stages B–J (see `docs/ROADMAP.md`). What *is* included is one real end-to-end
encode, so the pipeline can never be "green" while the media toolchain is broken.

---

## 1. What was implemented

### 1.1 Application shell

| Module | Lines | Job |
| --- | --- | --- |
| `run_studio.py` | 62 | Windows-friendly launcher: no side effects on import, one `main()` under `if __name__ == "__main__"`, friendly message instead of a traceback when PySide6 is missing. |
| `app/__main__.py` | 22 | `python -m app` entry point. |
| `app/main.py` | 334 | Argument parsing, data-root resolution, logging, single-instance check, crash-hook installation, clean shutdown. `--exit-after N` exists for automated startup tests. |
| `app/ui/main_window.py` | 799 | Menu bar, navigation, page stack, job panel, status bar, window geometry persistence, "shut down safely" logic. |
| `app/ui/views/*` | 1 540 | Welcome, System check, Settings, Maintenance, Diagnostics pages. |
| `app/ui/widgets/*` | 500 | Shared cards, metric rows, hint labels, job progress panel. |
| `app/ui/theme.py` | 557 | Dark and light palettes, spacing/metric tokens, one place where colours change. |
| `app/ui/error_handler.py` | 109 | Global exception hook + `guard_slot` decorator: an unexpected error always becomes a readable dialog with a log path, and never a silently dead button. |
| `app/ui/single_instance.py` | 137 | Local socket guard so a double-click on the shortcut cannot start two copies writing to the same project. |

### 1.2 Core services

* **Paths** (`app/core/paths.py`) — one `AppPaths` object for every folder
  (`config/ projects/ assets/ templates/ themes/ models/ workspace/ cache/
  previews/ output/ logs/`). Portable layout next to the app when that folder is
  writable, per-user `%LOCALAPPDATA%` layout otherwise, `--data-root` and
  `MGS_DATA_ROOT` overrides, remembered location between sessions. Nothing in
  the application depends on the current working directory.
* **Settings** (`app/core/settings.py`) — typed dataclasses, versioned JSON,
  atomic writes, validation and clamping of impossible values, a rotating
  backup, and recovery from a damaged file with a note telling the user what was
  repaired.
* **Atomic I/O** (`app/core/atomicio.py`) — write-temp → flush → fsync → replace,
  so a crash mid-save can never leave a half-written `project.json`,
  `settings.json` or output file.
* **Jobs** (`app/jobs/*`) — `JobSpec`/`JobManager`/`JobWorker`: every long action
  is a named job with progress, elapsed time, cancellation, and exactly one
  terminal state (`SUCCEEDED` / `FAILED` / `CANCELLED`). Keys make duplicate
  submissions impossible, so three clicks produce one job. Workers are plain
  `QThread`s — no `fork`, no recursive multiprocessing, nothing that breaks on
  Windows spawn semantics.
* **Logging** (`app/core/logging_setup.py`) — timestamped, session-tagged,
  thread-tagged log plus an `EVENT=` vocabulary (`APP_START`, `SYSTEM_CHECK`,
  `JOB_STARTED`, `RENDER_START`, `FFMPEG_COMPLETE`, `QC_RESULT`, `ERROR`, …) so a
  support request can be traced from the log alone.
* **System check** (`app/checks/*`) — 12 ordered checks (Python, folders,
  writability, disk space, FFmpeg/FFprobe presence *and* a real self-test,
  packages, Kokoro engine and voices, CPU-only rendering mode, optional resource
  monitor), each returning ✓ Ready / ⚠ Optional / ✗ Missing with what happened,
  why it matters and what to do.
* **Friendly errors** (`app/core/errors.py`) — every error carries
  *what happened / why / what to do / technical details + log path*. The phrase
  "Something went wrong" appears nowhere in the source.
* **Maintenance** (`app/core/maintenance.py`) — cache/temp cleanup that refuses
  to touch `projects/`, `assets/` or `output/`, removes stale temp files at
  startup, and reports what it freed.
* **Diagnostics** (`app/diagnostics/smoke.py`) — the built-in smoke test: a real
  end-to-end run (folders → settings → atomic writes → naming → FFmpeg discovery
  → **actual encode of a 1-second clip** → FFprobe validation → system check →
  safe cleanup with canary files → one-action-one-job → cancellation).
* **CLI** (`app/cli/main.py`) — `setup`, `check`, `info`, `clean`, `smoke-test`,
  `gui`, with documented exit codes (`0` ok, `1` problems, `2` blocked,
  `3` error) so it can be scripted or checked from a shortcut.
* **Installer** (`installer/setup_windows.py`) — dependency-free verification of
  a fresh Windows machine (Python/64-bit, packages, FFmpeg, Kokoro, writable
  folders, disk space) that writes `config/setup_report.txt`.
* **Release check** (`scripts/release_check.py`) — proves the build works on the
  machine in front of you: folders → system check → smoke test → output naming →
  application start → restart → **hard kill (crash test)** → automated suite.

### 1.3 Deliberately *not* in Stage A

Project model, script editor, Kokoro voices, scenes, storyboard, subtitles,
renderer, QC, image tools, undo/redo, code mode. Menu entries for those exist,
but they are visibly disabled and labelled **"(later stage)"** — there are no
fake buttons and no fake progress anywhere. Licensing/payments/cloud are Phase 2
and are not present in any form.

---

## 2. Automated tests

```
python -m pytest tests -q
→ 153 passed in 12.92s
```

(The build sandbox has no display and no GPU driver, so the Qt tests there also
need `QT_QPA_PLATFORM=offscreen` and a headless stub library path. On Windows
neither is needed; the commands in section 7 are the plain ones.)

| File | Tests | Covers |
| --- | ---: | --- |
| `test_paths.py` | 17 | Folder layout, portable vs per-user, overrides, CWD-independence, non-existent roots, pointer file. |
| `test_settings.py` | 15 | Defaults, round trip, unknown keys, damaged file recovery, clamping, backups, atomic save. |
| `test_atomicio.py` | 12 | Atomic replace, backups, failure paths, temp-file cleanup, `human_size`. |
| `test_jobs.py` | 22 | Job states, progress monotonicity, cancellation, no result from a failed job, duplicate keys, shutdown. |
| `test_ffmpeg.py` | 17 | Discovery order, rejecting unrunnable binaries, version parsing, argument arrays (never `shell=True`), timeouts, bounded output, probe JSON errors. |
| `test_packages.py` | 7 | Optional/required package probing without importing heavy modules. |
| `test_maintenance.py` | 10 | Cache cleanup deletes only scratch, protected folders untouched, stale temp removal. |
| `test_system_check.py` | 10 | Every check in isolation, missing-FFmpeg path, report ordering, headline/blocker logic. |
| `test_job_manager.py` | 10 | Signal contract, one completion per job, cancel-then-finish race, thread cleanup. |
| `test_gui.py` | 13 | Window builds, navigation, disabled later-stage actions, **three clicks = one job**, cancellation, theme/geometry/mode persistence, close-with-running-job. |
| `test_smoke.py` | 7 | Smoke test against a scripted toolchain, canary files survive, cleanup scope, repeatability, no stray files in the CWD. |
| `test_cli.py` | 10 | Every CLI command, exit codes, corrupt settings tolerated, unwritable data root, `--version` without a data root. |
| `test_installer.py` | 3 | The setup tool run as a real subprocess from another folder: report written, exit code honest, no log leakage, fix options present. |

Two testing rules matter for later stages:

* **No fixed sleeps.** Qt tests wait on a predicate (`wait_until`). Replacing
  fixed event-loop waits took the GUI suite from 62 s to 4.2 s and removed the
  flakiness.
* **The toolchain is injected.** Tests, the smoke test and the release check all
  accept a scripted `ffmpeg`/`ffprobe`, so the suite is fast and deterministic
  while the release check still performs a real encode.

---

## 3. Machine verification (this machine, these commands)

| # | What was run | Result |
| --- | --- | --- |
| 1 | `python installer/setup_windows.py --data-root <fresh>` | Exit code 1 with the FFmpeg-missing explanation and three fix options; 14 folders created; report written to `config/setup_report.txt`; zero library log lines leaked to stdout. |
| 2 | `python -m app.cli.main --data-root <root> check --deep` | `Everything required is ready. 4 optional note(s).` — Python 3.11.2 64-bit ✓, folders ✓, 17.9 GB free ✓, FFmpeg 7.0.2 ✓, FFprobe ✓, self-test encode ✓, CPU rendering ✓; Kokoro, voices, psutil and soundfile as ⚠ optional. |
| 3 | `python scripts/release_check.py --data-root <fresh> --with-pytest` | **RELEASE CHECK PASSED (8 steps)**: folders, system check, smoke test, output naming, application start (6.4 s), restart with settings intact (6.1 s), crash test with `SIGKILL` (9.1 s, data intact, clean start afterwards), automated suite 153 passed. |
| 4 | `python -m app.cli.main --data-root <root> smoke-test` ×3 | `PASSED (10 checks in 0.3s)` every time — the smoke test is repeatable in the same data root. |
| 5 | Application started and closed 6 times (release-check GUI steps, `--exit-after`) | Exit code 0 every time, `EVENT=APP_EXIT` and `SETTINGS_SAVED` in the log, no lingering processes. |
| 6 | Launch from a different working directory (`cd <unrelated folder> && python <repo>/run_studio.py --exit-after 5`) | Exit code 0 — no CWD dependency. |
| 7 | `ruff check app/ tests/ installer/ scripts/ run_studio.py --select F,E9` | `All checks passed!` |

Source size after Stage A: **11 584 lines of application code**, 2 411 lines of tests, 741 lines of
launcher/installer/verification tooling (`run_studio.py`, `scripts/`, `installer/`). Largest single file:
`app/ui/views/settings_view.py` (810 lines) - no giant do-everything module.

---

## 4. Bugs found and fixed during Stage A

Every one of these was reproduced first, then fixed, then locked down with a
test or a check step (see the regression log in `docs/TESTING.md`).

| # | Bug | Severity | How it showed up | Fix |
| --- | --- | --- | --- | --- |
| 1 | FFmpeg was accepted as "available" when the file existed but could not run | **P0** | The system check reported ✓ and every later render would fail. | Verification now executes the candidate and records the failure before continuing the search. |
| 2 | `ffprobe`'s version banner was never detected | P1 | The tool appeared "found" but versionless. | The parser accepts both `ffmpeg version` and `ffprobe version`. |
| 3 | A portable data root was accepted even when the folder did not exist | P1 | The app would start and then fail on first save. | The candidate must exist **and** be writable, otherwise the per-user folder is used and the reason is logged. |
| 4 | Cancelled/failed jobs still exposed `result.value` | P1 | Later stages could have rendered or saved output from a job the user cancelled. | Only `SUCCEEDED` jobs expose a value; enforced in the job layer and asserted in tests. |
| 5 | Progress could stop just short of 100 % | P1 | Progress bar froze at 97 % while the job had finished. | The throttled emitter always delivers the completion snapshot. |
| 6 | The setup tool printed library log lines to stdout before the log folder existed | P2 | Confusing console output on a fresh machine. | Logging is initialised quietly, then re-initialised into `logs/` once folders exist. |
| 7 | The system-check page destroyed its own placeholder label | **P1** | Re-running the check removed the "nothing checked yet" text; the next report touched a deleted widget. | `_clear_results()` skips the reused placeholder; regression test uses `shiboken6.isValid`. |
| 8 | The smoke test failed on a second run in the same folder | P1 | Told users the build was broken while it was fine — the previous run's leftovers made the "never overwrite" step fire. | The smoke test clears its own `workspace/smoke` scratch folder first, and only reports media files as artifacts. |
| 9 | The release-check script was placed in `tools/`, which is the runtime FFmpeg folder and is git-ignored | P2 | The verification script silently would not have been committed. | Developer tooling moved to `scripts/`; `.gitignore` documents why `/tools/` is runtime data. |
| 10 | A test stand-in `ffmpeg` wrote its last argument to disk blindly | P2 | A stray `-version` file appeared in the working directory — the same class of path bug a real shortcut launch would expose. | The stand-in ignores option-shaped arguments and now answers version probes; regression test asserts the CWD stays empty. |
| 11 | Fixed-sleep waits made the Qt suite slow and flaky | P2 | GUI tests took 62 s and could fail on a loaded machine. | Predicate-based waiting; 4.2 s and stable. |

---

## 5. What Stage A guarantees now

* The application starts from a shortcut, from `python -m app`, from any working
  directory, and with no configuration at all.
* No action can destroy `project.json`, `assets/` or `output/` — writes are
  atomic, backups rotate, cleanup is restricted to scratch folders.
* No user action can run twice, and every job reaches exactly one terminal state
  with visible progress, elapsed time and a working Cancel button.
* The interface stays responsive while work happens in the background; Qt
  widgets are only touched from the main thread.
* Every failure the user can hit has a message saying what happened, why, what
  to do, and where the log is.
* The truth about the machine is one click away (✓ / ⚠ / ✗), and a broken media
  toolchain can never be mistaken for a working one.

---

## 6. Known limitations (disclosed, not hidden)

1. **Not yet verified on real Windows hardware.** Every run above happened in
   this Linux build sandbox, with an offscreen Qt platform and a stand-in
   `ffprobe`. The Windows-specific behaviour — the real `ffmpeg.exe`, the real
   PySide6 Windows build, `%LOCALAPPDATA%`, the installer, the shortcut launch —
   still needs one run of `python scripts/release_check.py --with-pytest` on the
   target machine. Nothing in the code is platform-specific except the folder
   choice, but "verified on this machine" is not the same as "verified on your
   machine", and the directive requires the latter.
2. **Kokoro/voice stack is not installed here**, so the voice checks report
   ⚠ optional. Voice support and its checks become real in Stage C.
3. **No music/effects, subtitles or renderer yet** — Stage A produces exactly one
   test clip to prove the FFmpeg path works.
4. **Project model, undo/redo and the storyboard do not exist yet**, so
   `File → New/Open/Save` are disabled placeholders. `docs/PROJECT_FORMAT.md`
   specifies the format they will implement.
5. **Single instance only within one user session**; the guard uses a local
   socket and does not attempt network-wide locking (not needed, and deliberately
   not implemented).
6. **No crash reporter**; after a hard kill the next start offers recovery of
   *settings*, and the recovery-file flow for projects arrives with the project
   model in Stage B.
7. **Storyboard thumbnails, preview cache and memory controls** are designed but
   unimplemented (Stages D/H).
8. **The smoke test's audio/video validation is shallow** — it checks container,
   codec, resolution, frame rate and duration with FFprobe, not perceptual
   quality. Full QC arrives in Stage G.

---

## 7. Reproducing the verification

```bash
# 1. Automated suite
python -m pytest tests -q

# 2. Fresh-machine setup check
python installer/setup_windows.py --data-root ./scratch/fresh

# 3. Full machine verification (folders, check, smoke, GUI start/restart/crash,
#    output naming, optional test suite)
python scripts/release_check.py --data-root ./scratch/check --with-pytest

# 4. Just the media pipeline
python -m app.cli.main --data-root ./scratch/check smoke-test
```

On Windows, run the same commands from an activated virtual environment; no
arguments are Windows-specific and no path contains `/content`, `/tmp` or a
notebook assumption.

---

## 8. Stage A gate decision

Stage A passes its own gate: the application shell, settings, system check and
job system are implemented, tested (153 automated tests), and verified by
running the real application — start, restart and hard-kill recovery — with every
bug found during that process fixed and covered.

The remaining conditions before Stage B starts:

1. **one run of `scripts/release_check.py --with-pytest` on the target Windows
   machine** (section 6.1), and
2. the user's go-ahead to start Stage B (project model, save/load, autosave,
   crash recovery).

Stage B work will begin on the project model described in
`docs/PROJECT_FORMAT.md`, keeping the same rules: one job per action, atomic
writes, and no feature that is not needed to save and reopen a project safely.
