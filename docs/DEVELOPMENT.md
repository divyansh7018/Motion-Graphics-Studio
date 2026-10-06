# Development guide

Everything a developer needs to run, test and debug Motion Graphics Studio.

---

## 1. Environment

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt -r requirements-dev.txt
python installer/setup_windows.py --data-root ./scratch/data
```

Use a `--data-root` that is not the repository root while developing, so your
test projects and logs do not mix with the source tree.

---

## 2. Running

```bash
# GUI
python run_studio.py --data-root ./scratch/data

# GUI, more log detail
python run_studio.py --data-root ./scratch/data --log-level DEBUG

# GUI, close automatically after 8 seconds (used by automation)
python run_studio.py --data-root ./scratch/data --exit-after 8

# CLI
python -m app.cli.main --data-root ./scratch/data check --deep
python -m app.cli.main --data-root ./scratch/data smoke-test
python -m app.cli.main --data-root ./scratch/data info
```

Useful flags: `--data-root`, `--log-level`, `--no-single-instance`,
`--reset-settings` (ignores the saved settings for one run without touching the
file), `--version`.

---

## 3. Tests

```bash
# everything
python -m pytest tests -q

# one file
python -m pytest tests/test_jobs.py -q

# only the fast, Qt-free tests
python -m pytest tests -q -k "not gui and not job_manager"
```

Test layout:

| File | Covers |
|---|---|
| `test_paths.py` | data-root resolution, traversal guards, filename safety, unique output names |
| `test_settings.py` | defaults, round trips, clamping, damaged-file recovery, backups |
| `test_atomicio.py` | atomic writes, backup pruning, quarantine, temp hygiene |
| `test_jobs.py` | state machine, cancel tokens, single execution, progress |
| `test_job_manager.py` | duplicate rejection, one completion event, cancellation, shutdown (Qt) |
| `test_ffmpeg.py` | discovery across locations, rejecting unrunnable binaries, safe subprocess use |
| `test_packages.py` | package detection (regression: missing packages reported as missing) |
| `test_maintenance.py` | cleanup safety: protected folders, symlink escape, stale temps |
| `test_system_check.py` | check registry, report aggregation, error boundary for broken checks |
| `test_smoke.py` | the end-to-end smoke test itself, with a stand-in toolchain |
| `test_cli.py` | every CLI command, exit codes, working-directory independence |
| `test_gui.py` | window construction, navigation, background checks, theme, clean close |

GUI tests run with Qt's `offscreen` platform (`tests/conftest.py` sets it), so no
window appears and no display is required.

Stage E added twelve more files, 268 tests. These render real video with a real
FFmpeg and read the results back through the probe, so they are slower (the
whole suite is around 80 s) but check the bytes on disk rather than the model
that produced them:

| File | Covers |
|---|---|
| `test_media_probe.py` | parsing real facts out of `ffmpeg -i` (no ffprobe) |
| `test_audio_service.py` | mix graph, fades, ducking, audio validation |
| `test_subtitles.py` | cue generation, SRT/VTT/ASS, caption editing |
| `test_stage_e_services.py` | `TimelineService` and `SubtitleService` |
| `test_render_segments.py` | segment planning and transition overlap |
| `test_render_capabilities.py` | codec detection and the validation matrix |
| `test_render_output.py` | naming, sequence, staging, output folder |
| `test_render_qc.py` | every QC check against real files |
| `test_render_engine.py` | real renders, states, resume, cancellation |
| `test_gui_stage_e.py` | the four Stage E pages, including "no dead buttons" |
| `test_kokoro_selftest.py` | the two Kokoro verification states |
| `test_schema_stage_e.py` | old projects load, new ones round-trip |

---

## 4. Headless containers

Qt on Linux needs system graphics libraries (`libGL`, `libEGL`, `libxkbcommon`,
`libdbus`). On a normal desktop these are already installed. In a build container
without them, `import PySide6.QtWidgets` fails with `libGL.so.1: cannot open
shared object file`.

Two options:

1. Install them (`apt-get install -y libgl1 libegl1 libxkbcommon-x11-0
   libdbus-1-3`), or
2. Generate stub shared objects with the helper in this repository:

```bash
python scripts/make_qt_stubs.py          # writes /tmp/stublib

export LD_LIBRARY_PATH=/tmp/stublib
export QT_QPA_PLATFORM=offscreen
python -m pytest tests -q
```

`scripts/make_qt_stubs.py` reads the symbols **and their version nodes**
straight out of the Qt libraries (`readelf --dyn-syms`, `readelf -V`), so the
dynamic loader accepts the stubs exactly as it would the real libraries. That
detail matters: a plain `STUB_1.0 { global: ... }` version script is not enough,
because Qt asks for versioned symbols such as `xkb_*@V_0.5.0` and
`dbus_*@LIBDBUS_1_3`. Two other things the helper handles:

* `libGL.so.1` has no `DT_NEEDED` referrer among the Qt libraries (the platform
  plugins `dlopen` it), so it is found by prefix-matching every Qt library
  rather than by following dependencies;
* `readelf --dyn-syms -W` prints each symbol followed by an index such as
  ` (12)`, which must be stripped before splitting on `@`.

The script loops: it tries `import PySide6.QtWidgets`, reads the missing library
name out of the `ImportError`, builds that stub, and repeats until the import
succeeds.

The stubs are a build-container workaround only — they are never shipped and
never referenced by the application.

---

## 5. Debugging

* **Log file** – `<data folder>/logs/studio.log`. Every line carries a session
  id; `grep EVENT=RENDER_START logs/studio.log` finds a step.
* **Crash log** – `<data folder>/logs/crash-python.log` (written by
  `faulthandler` if the process dies hard).
* **Diagnostics page** – copy everything relevant in one click.
* **Smoke test** – `python -m app.cli.main smoke-test` proves the pipeline works
  without starting the UI.
* **Stuck job?** The job manager logs `JOB_CANCEL_TIMEOUT` and shows a message
  instead of remaining "Cancelling…" forever.

---

## 6. Conventions

* No work in a widget slot that can take longer than a few milliseconds; submit a
  job instead.
* Never `shell=True`. Use argument lists (`app/tools/ffmpeg.py`).
* Never write files directly; use `app/core/atomicio.py`.
* Never `print()` in application code — log an event.
* Add a setting rather than a constant when the value is something a user might
  reasonably want to change.
* One new module per concern; if a file grows past ~600 lines, it is probably
  doing too much.
* Keep the GUI text plain and specific: say what happened and what to do, not
  "an error occurred".

---

## 7. Adding a background job

```python
# 1. add a key
class JobKeys:
    MY_TASK = "my.task"

# 2. write the body - it must poll the cancel token and report progress
def my_task_body(context) -> MyResult:
    context.progress.start(total=steps, message="Working", unit="steps")
    for index, item in enumerate(items, start=1):
        context.raise_if_cancelled()
        do_work(item)
        context.progress.update(current=index)
    return result

# 3. build the spec
spec = JobSpec(key=JobKeys.MY_TASK, title="My task", body=my_task_body,
               settings=self.settings, paths=self.paths)

# 4. submit - submit() refuses a duplicate while one is running
job = self.jobs.submit(spec)
if job is None:
    self.notify("That task is already running.")
```

Handle the result in one place (`MainWindow._on_job_finished` or a page-specific
slot) and show a dialog only for failures or explicitly user-started tasks.

---

## 8. Working on the render pipeline (Stage E)

Three rules keep this part of the codebase honest, and each one exists because
breaking it produced a bug that was hard to see:

* **Measure, don't assume.** Anything claimed about a finished file comes from
  `app/media/probe.py` or `QCService`, never from the model that produced it.
  A test that asserts "the render is 1080p" must read the file back.
* **One timeline, one judgement.** `TimelineService` is the only place timings
  are built and validated; the engine calls it rather than keeping a second
  opinion. If you add a timing rule, add it there so the GUI, the CLI and the
  render all agree.
* **Heavy work goes in a job.** Capability detection, planning, mixing and
  rendering all start subprocesses. `test_gui_stage_e.py` asserts they are
  submitted as jobs, so a change that runs one inline will fail a test.
* **Say which tool measured it.** `MediaInfo.ok` and `MediaInfo.used_ffprobe`
  are different questions. A QC report prints `Inspected with: FFprobe` or
  `FFprobe fallback / limited probe (ffmpeg -i)`, and records
  `ffprobe_inspection: CHECK NOT AVAILABLE` when FFprobe did not run. Never
  write "verified with FFprobe" from a parsed `ffmpeg -i`.
* **An unrun check is not a pass.** `QCReport.checks` holds PASS, FAIL or
  `CHECK NOT AVAILABLE` and nothing else, and `_verdict()` refuses to return
  PASS while any check is unavailable. A check that merely does not apply is
  *absent* - do not invent an entry for it, or a silent video will look
  under-verified.
* **A control that does nothing is a bug.** If the UI exposes an option, it must
  reach the encoder or be refused with a reason. Two-pass is verified by reading
  `-pass 1` / `-pass 2` out of the real FFmpeg command, not by looking at the
  arguments a function builds.
* **Compare like with like.** A container has one duration; each stream has its
  own. `info.stream_duration("video")` versus `info.stream_duration("audio")` is
  the A/V sync check - comparing the container duration with itself always
  returns 0 and silently passes.

FFmpeg gotchas worth knowing before you debug an encode:

* `blackdetect` prints `black_start:0` with **colons**, not `=`, and takes
  `pix_th` (per-pixel luma) and `pic_th` (fraction of pixels) as two different
  knobs. Confusing them flags a dark theme as a black screen.
* `astats` reports `-inf` dB for complete silence, which is a real reading and
  not a missing one.
* A mix command using `apad` or `stream_loop=-1` never terminates without an
  explicit `-t <duration>` on the output. This once wrote 9.75 GB for an 8.4 s
  timeline, and the runaway process survived its parent being killed.
* A flat single-colour frame encodes *smaller* at CRF 16 than at CRF 36, so a
  test asserting "higher quality means a bigger file" needs real content.
* `log_event(event, message, **fields)` takes the message **positionally**.
  Passing `message=` as well raises `TypeError` at the moment the event is
  logged, which is usually inside an error handler - a render with no audio
  tracks failed for exactly this reason.
* Two-pass writes a statistics file per pass. Give every segment its own
  `-passlogfile` (`pass_<index>`) or concurrent segments overwrite each other's
  stats, and delete the `*-0.log` files afterwards.

## 8b. Profiling

`scripts/stage_e_profile.py` measures resident set at startup, GUI construction,
project create/load, preview, long-form planning and during a real render:

```
python scripts/stage_e_profile.py --data-root /tmp/mgs_profile --scenes 50
```

Use it before claiming anything about memory. "Controlled by design" is not a
measurement, and the profiler is what found the no-audio crash in §8.

## 9. Release checklist (Stage A)

1. `python -m pytest tests -q` → all green.
2. `python -m app.cli.main check --deep` → required items ✓.
3. `python -m app.cli.main smoke-test` → PASSED.
4. `python run_studio.py --data-root <fresh folder> --exit-after 10` → clean
   start and clean close, no `ERROR` lines in the log.
5. Re-run 3 and 4 launching from a *different* working directory.
6. `python installer/setup_windows.py` on a clean machine → exit code 0.

Stage E adds to the checklist:

7. `python scripts/stage_e_manual_matrix.py --data-root <fresh folder>` → 25/25.
8. `python scripts/stage_e_end_to_end.py --data-root <fresh folder>` → both
   renders COMPLETED, both QC PASS, Video1 proven untouched.
9. `motion-studio voice selftest` → record whichever of the two states it
   prints. Do not write "Kokoro verified" unless it printed `KOKORO VERIFIED`.
