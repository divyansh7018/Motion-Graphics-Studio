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

## 8. Release checklist (Stage A)

1. `python -m pytest tests -q` → all green.
2. `python -m app.cli.main check --deep` → required items ✓.
3. `python -m app.cli.main smoke-test` → PASSED.
4. `python run_studio.py --data-root <fresh folder> --exit-after 10` → clean
   start and clean close, no `ERROR` lines in the log.
5. Re-run 3 and 4 launching from a *different* working directory.
6. `python installer/setup_windows.py` on a clean machine → exit code 0.
