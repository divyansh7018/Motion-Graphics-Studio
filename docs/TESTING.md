# Testing

This project follows the directive's rule that a build is only called stable
when the suite passes, the smoke test passes, and the end-to-end, cancellation,
restart, duplicate-generation and output-validation tests have actually been run.

**Current status: 566 tests passing** (Stage A 153 + Stage B 208 + Stage C 205). See
`STAGE_A_REPORT.md` and `STAGE_B_REPORT.md` for the exact runs.

---

## 1. Levels of testing

| Level | What it proves | Where |
|---|---|---|
| Unit | pure logic: paths, settings, state machine, progress, naming | `tests/test_paths.py`, `test_settings.py`, `test_atomicio.py`, `test_jobs.py`, `test_packages.py` |
| Project model | schema, sections, migration, validation, presets, history | `test_project_model.py`, `test_project_migrations.py`, `test_project_validation.py`, `test_project_presets.py`, `test_project_history.py` |
| Project storage | atomic save, backups, rotation, autosave, recovery, locking, assets | `test_project_store.py`, `test_project_recovery.py`, `test_project_lock.py`, `test_project_assets.py` |
| Project service | create/open/save/save-as/duplicate/rename/delete/recent | `test_project_service.py` |
| Integration | real files, real subprocesses, real Qt objects | `test_ffmpeg.py`, `test_maintenance.py`, `test_system_check.py`, `test_job_manager.py`, `test_smoke.py`, `test_cli.py`, `test_project_cli.py` |
| GUI | the shell and every project page build, navigate, save, stay responsive, close cleanly | `test_gui.py`, `test_project_ui.py` |
| Script | plain/structured parsing, Unicode, import, export, counts, duration estimates | `test_script.py` |
| Narration pipeline | preprocessing, cache keys, staleness, WAV validation, cancellation, per-section output | `test_tts.py`, `test_narration.py` |
| Narration jobs | the real job bodies with the real `ProgressReporter` and `CancelToken` | `test_tts_jobs.py` |
| Voice / System Check | engine detection, voice + language discovery, filtering, the TTS self-test | `test_checks_voice.py` |
| Script and Narration pages | every control does something real; nothing claims to work when it cannot | `test_narration_ui.py` |
| Voice CLI | `script`, `voice` and `narration` commands, exit codes and messages | `test_cli_voice.py` |
| End-to-end | folders → settings → FFmpeg → real encode → validation → cleanup → jobs → cancel | `app/diagnostics/smoke.py` |
| Release check | 10 steps on a throw-away data folder, including the project lifecycle | `scripts/release_check.py` |
| Manual | start, click, watch, close on the real desktop | `docs/STAGE_A_REPORT.md`, `docs/STAGE_B_REPORT.md`, `scripts/stage_b_manual_matrix.py` |

---

## 2. Running

```bash
python -m pytest tests -q                        # everything
python -m pytest tests -q -k "not gui"           # without the UI tests
python -m pytest tests -q --tb=short             # compact failures
python -m pytest tests/test_gui.py -q            # only the interface
python -m pytest tests/test_project_ui.py -q     # only the project pages
python -m pytest tests -q -k project             # everything project related

# the sixteen Stage C manual scenarios, headless, with a printed pass/fail table
# (--engine real uses the installed Kokoro instead of the test double)
python scripts/stage_c_manual_matrix.py --data-root /tmp/mgs_evidence --engine fake

# the ten Stage B manual scenarios, headless, with a printed pass/fail table
python scripts/stage_b_manual_matrix.py --data-root /tmp/mgs_matrix

# the whole release check (10 steps) on a throw-away folder
python scripts/release_check.py --data-root /tmp/mgs_release --with-pytest
```

In a container without the Qt system libraries, prefix the test command with
`LD_LIBRARY_PATH=/tmp/stublib QT_QPA_PLATFORM=offscreen` after running
`python scripts/make_qt_stubs.py` (see `DEVELOPMENT.md` §4).

Requirements: `pip install -r requirements-dev.txt`. The GUI tests set
`QT_QPA_PLATFORM=offscreen` themselves, so a headless machine works.

---

## 3. Guarantees covered by tests

### Data safety
* an atomic write leaves no temporary file behind and replaces content in one step
* a previous version is backed up before being replaced, and old backups are pruned
* a damaged JSON file is copied aside and defaults are used — never silently dropped
* out-of-range settings are clamped **and reported**, not ignored
* cleanup can only delete inside `cache/ previews/ temp/ workspace/`; a symlink
  pointing outside is skipped; projects, assets, themes and output are untouched
* numbered output names never overwrite an existing video

### Job system (the stability core)
* a job body runs exactly once
* a second submission with the same key is refused while one is running
* exactly one completion event is delivered per job
* every job ends in SUCCEEDED, FAILED or CANCELLED — never "still running"
* a cancelled job exposes no partial value
* cancellation during a running job stops it promptly and cleans child processes
* `shutdown()` cancels, waits, refuses new work and reports stragglers
* failures inside a job become a friendly result, not a crash

### Interface
* the window and all pages build with an empty data folder
* navigating between pages never raises
* a system check runs off the UI thread while the UI keeps responding
* three clicks on "Run system check" produce exactly one job and one result
* theme and advanced-mode changes are persisted to disk
* window geometry and last page are remembered
* closing with a running job cancels it without hanging
* unimplemented pages are disabled and explain themselves (no dead buttons)

### External tools
* FFmpeg/FFprobe are found in the settings path, the `tools/` folder and PATH
* a file that exists but cannot run is **rejected**, with the reason reported
* a folder given where a file was expected produces an explanation
* commands run without a shell, with a timeout, bounded output and cancellation
* FFprobe JSON that cannot be parsed is reported, not swallowed

### Command line and setup
* every command runs and returns a meaningful exit code
* commands work from an unrelated working directory
* a corrupt settings file does not stop the CLI
* an unwritable data folder produces a friendly message and exit code 2

---

## 4. The built-in smoke test

`python -m app.cli.main smoke-test` (or **Tools → Run smoke test**) runs:

1. application folders exist and are writable
2. settings save → reload → recovery from a damaged file
3. atomic write + backup + no temp leftovers
4. output naming: `Video.mp4` then `Video2.mp4`, original intact
5. FFmpeg and FFprobe discovery
6. a **real one-second video is encoded** and validated (resolution, duration, codec)
7. the system check runs and produces a report
8. cache cleanup removes temp files but not assets (verified with a canary file)
9. one action = one job = one result
10. cancellation reaches a terminal state, both before start and mid-run

It is the fastest way to answer "is this installation healthy?" and it is what
the release checklist runs on a new machine.

---

## 5. Stage-by-stage test plan

Later stages add the files named in the directive, in this order:

| Stage | New test files |
|---|---|
| B | `test_project.py` (serialisation, migration, damaged file, recovery, undo/redo) |
| C | `test_script.py`, `test_tts.py`, `test_narration.py`, `test_tts_jobs.py`, `test_narration_ui.py`, `test_cli_voice.py`, `test_checks_voice.py` (parsing, Unicode, import/export, detection, voice + language discovery, preview, generation, WAV validation, staleness, cancellation, failure modes) |
| D | `test_scene.py`, `test_timeline.py` (determinism, responsive layout, text fitting, invalid timelines) |
| E | `test_audio.py` (mix, ducking, clipping, missing tracks) |
| F | `test_render.py` (frame streaming, timing from real durations, cancel mid-render, no hidden retries) |
| G | `test_output.py`, `test_qc.py` (FFprobe validation, black frames, silence, PASS/WARNING/FAIL) |
| H | `test_images.py` (import validation, corrupt files, thumbnails) |
| I | `test_ui_advanced.py` |

Existing tests are extended rather than replaced, and every fixed bug gets a
regression test in the file that owns the behaviour.

---

## 6. Regression log

| Bug | Risk | Test |
|---|---|---|
| `probe_package` marked every package as installed before verifying it | the system check would claim the voice engine was ready when it was missing | `test_packages.py::test_missing_package_is_reported_missing` |
| a file that existed but could not run was accepted as FFmpeg | renders would fail with a confusing error halfway through | `test_ffmpeg.py::test_file_that_cannot_run_is_rejected` |
| the recovery path could create the application folder as the data root | data written into a read-only install folder | `test_paths.py::test_missing_source_folder_is_not_used_for_portable_data` |
| a cancelled job could expose a partial value | a half-finished result mistaken for a finished video | `test_jobs.py::test_job_body_returning_after_cancel_is_reported_as_cancelled` |
| progressive updates could leave the bar stuck below 100% | misleading progress (section 55) | `test_jobs.py::test_job_reports_progress_through_the_context` |
| the smoke test built cancellation specs without a body | smoke test crashed instead of reporting | `test_smoke.py::test_smoke_test_passes_with_a_working_toolchain` |
| the setup tool printed library log lines before the log folder existed | confusing output on a fresh machine, plus missing log file | `test_installer.py::test_setup_never_leaks_log_lines_and_creates_the_log_folder` |
| the system-check page deleted its own placeholder label when clearing rows | the page showed nothing after a re-check and the next report touched a dead widget | `test_gui.py::test_system_check_page_survives_repeated_reports` |
| the smoke test kept the previous run's scratch files | a second run in the same folder reported a false failure | `test_smoke.py::test_smoke_test_can_be_run_twice_in_the_same_folder` |
| developer tooling lived in `tools/`, the git-ignored runtime FFmpeg folder | the verification script would never have been committed | n/a (repository layout, see `docs/DEVELOPMENT.md`) |
| a test stand-in `ffmpeg` redirected its last argument to a file unconditionally | a stray `-version` file appeared in the working directory | `test_smoke.py::test_smoke_test_never_writes_into_the_working_directory` |
| fixed event-loop sleeps in Qt tests | slow, flaky GUI suite (62 s) | `tests/test_job_manager.py` (`wait_until` helper) |
