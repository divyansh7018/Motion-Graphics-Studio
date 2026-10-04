# Stage B report — project model and lifecycle

**Verdict: STAGE B COMPLETE**, with the known limitations in section 7 listed
honestly. This does not mean the build is bug-free; it means every item on the
gate below was checked and what remains is written down.

* Build: Motion Graphics Studio 0.2.0, stage B, project schema 2
* Tests: **360 passing** (153 carried over from Stage A + 207 added here)
* Release check: **10/10 steps passed**
* Manual matrix: **10/10 scenarios passed**

---

## 1. What was built

A project is a folder with one versioned `project.json`, a byte-exact
`script.txt`, and subfolders for assets, audio, scenes, generated files,
previews, renders, backups and autosave. Everything that creates, opens,
changes or saves a project goes through `ProjectService` — the GUI, the command
line and the tests all use the same code, so they cannot drift apart.

New modules:

| Path | Lines | Responsibility |
|---|---|---|
| `app/project/presets.py` | 558 | every codec / quality / format / template table |
| `app/project/model.py` | 1067 | the typed project model and (de)serialisation |
| `app/project/migrations.py` | 377 | schema detection, v1 → v2 migration |
| `app/project/validation.py` | 822 | validation that collects every issue + pre-render check |
| `app/project/layout.py` | 234 | folder layout and path safety |
| `app/project/store.py` | 761 | load/save, backups, autosave, recovery, rotation |
| `app/project/lock.py` | 248 | advisory locking (Windows-safe pid probing) |
| `app/project/recent.py` | 361 | recent projects, channel profiles |
| `app/project/assets.py` | 390 | import, verify, relink, replace, ignore |
| `app/project/thumbnails.py` | 182 | cached previews (Pillow only) |
| `app/project/history.py` | 109 | bounded undo/redo |
| `app/project/service.py` | 1072 | the one service the whole app uses |
| `app/cli/project.py` | 362 | `project` subcommands |
| `app/ui/project_controller.py` | 460 | Qt glue: dialogs, autosave timer, dirty state |
| `app/ui/wizard/new_project.py` | 646 | the 9-step wizard |
| `app/ui/dialogs/project_dialogs.py` | 320 | the four decision dialogs |
| `app/ui/views/project_view.py` | 390 | the open-project page |
| `app/ui/views/project_settings.py` | 640 | the eight-tab settings page |
| `app/ui/views/project_browser.py` | 295 | search / sort / filter browser |

Modified: `app/cli/main.py` (the `project` group), `app/core/version.py`
(stage/schema), `app/core/events.py` (10 new events), `app/core/errors.py`
(`ProjectVersionError`, `ProjectConflictError`), `app/tools/kokoro.py` (voice
catalogue discovery), `app/ui/context.py` (owns the controller),
`app/ui/main_window.py` (real project menus, pages, dirty title, recovery on
startup, lock release on close), `app/ui/views/welcome.py` (now the dashboard),
`app/ui/widgets/common.py` (`KeyValueGrid.value/set_value`),
`scripts/release_check.py` (two new steps).

---

## 2. The gate

| # | Requirement | Status |
|---|---|---|
| 1 | One versioned `project.json` per project; GUI, CLI and tests share one service | ✅ `ProjectService`, used by both `MainWindow` and `app/cli/project.py` |
| 2 | Typed sections: project, format, script, voice, theme, audio, scenes, assets, export | ✅ dataclasses in `model.py`, per-section `extra` keeps unknown keys |
| 3 | Explicit `schema_version`, migration, hard error for newer files | ✅ v1→v2 tested; newer schema raises `ProjectVersionError` |
| 4 | `script.txt` byte-exact, never rewritten | ✅ store writes the text unchanged (no added newline) |
| 5 | Presets stored separately, project stores resolved settings | ✅ `presets.py` holds the tables; `resolve_quality()` writes real values |
| 6 | Quality presets resolve, never silently downgrade | ✅ resolution + validation tests |
| 7 | Advanced UI shows only settings valid for the chosen encoder | ✅ codec/container/pixel-format/CRF-range are all derived |
| 8 | 9-step wizard, templates as presets not content | ✅ `NewProjectWizard`, 9 pages asserted in a test |
| 9 | Voice step lists only actually available voices | ✅ `discover_voices()`; empty catalogue shows a reason, no invented names |
| 10 | Folder layout created on project creation | ✅ `layout.py`, asserted by the store and service tests |
| 11 | Create validates the name, cleans up partial state, readable error | ✅ tested, including "no folder left behind" |
| 12 | Save: atomic, validate-before-write, backup of previous, `modified_at`, keeps `schema_version` | ✅ reuses Stage A `atomicio` |
| 13 | Save / Save As / Duplicate, all independent | ✅ matrix scenarios 3 and 4 |
| 14 | Autosave: change detection, configurable, never blocks the UI | ✅ `QTimer` + background job writing a snapshot |
| 15 | Rolling backups with retention | ✅ `backups/project_<date>_<n>.json`, default keep 10 |
| 16 | Crash recovery prompt: Restore / Open original / Ignore | ✅ `RecoveryDialog`, never replaces healthy data silently |
| 17 | Validation collects **all** issues ("3 issues found") | ✅ `ValidationReport` + CLI/UI output |
| 18 | Project Settings: GENERAL/FORMAT/QUALITY/VOICE/AUDIO/THEME/EXPORT/ASSETS, editable after creation | ✅ eight tabs, save/discard tested |
| 19 | Dirty-state tracking with Save/Discard/Cancel | ✅ `" *"` in the title + `UnsavedChangesDialog` |
| 20 | Dashboard cards, browser with search/sort/filters, Remove from Recent ≠ delete | ✅ tested, files survive removal |
| 21 | Open: validate → migrate → check paths → lazy load, warnings never block | ✅ `open_project`, notes surfaced |
| 22 | Missing assets never crash; name/path/location + Relink/Replace/Ignore | ✅ matrix scenario 7 |
| 23 | Relative paths preferred, absolute marked, folder portable | ✅ matrix scenario 5 (moved folder, 0 absolute paths) |
| 24 | External modification detected: Reload / Keep / Save as | ✅ content-hash based; UI test asserts the dialog appears |

---

## 3. Tests

207 tests were added; the Stage A 153 still pass unchanged apart from one
expectation that Stage B deliberately reversed (the File-menu project actions
used to be disabled placeholders and are now real).

| File | Tests | Covers |
|---|---|---|
| `test_project_model.py` | 17 | serialisation, ids, durations, hashes, summaries |
| `test_project_migrations.py` | 10 | version detection, v1→v2, newer-file refusal |
| `test_project_validation.py` | 16 | every rule, the pre-render check, report text |
| `test_project_presets.py` | 14 | codec/container agreement, quality resolution |
| `test_project_store.py` | 23 | atomic save, backups, rotation, script bytes, autosave |
| `test_project_recovery.py` | 9 | interrupted saves, restore, ignore, damaged files |
| `test_project_lock.py` | 9 | acquire, stale removal, foreign host, release |
| `test_project_assets.py` | 13 | import, verify, relink, replace, delete, checksum |
| `test_project_history.py` | 13 | undo/redo, bounded history |
| `test_project_service.py` | 35 | create/open/save/save-as/duplicate/rename/delete/recent |
| `test_project_cli.py` | 18 | every `project` subcommand, plus Stage A CLI intact |
| `test_project_ui.py` | 25 | dashboard, wizard, project page, settings, dialogs, browser |

Commands:

```bash
# full suite
LD_LIBRARY_PATH=/tmp/stublib QT_QPA_PLATFORM=offscreen \
    python -m pytest tests -q                      # 360 passed

# Stage B manual matrix (10 scenarios, printed pass/fail)
LD_LIBRARY_PATH=/tmp/stublib QT_QPA_PLATFORM=offscreen \
    python scripts/stage_b_manual_matrix.py --data-root /tmp/mgs_matrix

# release check, 10 steps, on a throw-away folder
LD_LIBRARY_PATH=/tmp/stublib QT_QPA_PLATFORM=offscreen \
    python scripts/release_check.py --data-root /tmp/mgs_release --with-pytest
```

`LD_LIBRARY_PATH=/tmp/stublib` is only needed in a container without the Qt
system libraries (`python scripts/make_qt_stubs.py` creates them). On Windows
the plain `python -m pytest tests -q` is the command.

---

## 4. Manual matrix (actually run)

```
[PASS] 01 create / save / close / reopen: reopened 'Matrix One' with the script intact, version 2
[PASS] 02 autosave + force-close + recovery: autosave written, 1 candidate(s) offered, restored text matches
[PASS] 03 Save As is independent: original kept 'Only in the original.', copy has 'Only in the copy.', different ids and folders
[PASS] 04 duplicate is independent: 1 asset copied into the duplicate, relative path kept
[PASS] 05 folder moved + relative assets: 1 asset(s) still found after the move, 0 absolute path(s)
[PASS] 06 corrupt project.json recovery: reported 'Project problem' and kept 1 quarantined copy(ies) in backups/
[PASS] 07 missing asset report + relink: reported 'will-move.png' at .../assets/will-move.png, relinked to renamed-elsewhere.png
[PASS] 08 1080p / High persists: 1920x1080@30 high h264_cpu CRF 20
[PASS] 09 invalid render settings caught: 3 error(s): ['AUDIO_CODEC', 'CODEC_CONTAINER', 'NO_SCENES']
[PASS] 10 large project stays responsive: 300 scenes, 149 kB - save 40 ms, open 14 ms, validate 1 ms (0 error(s))
10/10 scenarios passed
```

Release check on a prepared data folder:

```
[PASS] Application folders
[PASS] System check
[PASS] End-to-end smoke test
[PASS] Project lifecycle (Stage B)
[PASS] Project command line
[PASS] Output naming never overwrites
[PASS] Application starts and closes cleanly
[PASS] Restart keeps settings intact
[PASS] Crash leaves data intact
[PASS] Automated test suite        360 passed
RELEASE CHECK PASSED (10 steps).
```

---

## 5. Events logged

`PROJECT_CREATE`, `PROJECT_OPEN`, `PROJECT_SAVE`, `PROJECT_SAVE_AS`,
`PROJECT_DUPLICATE`, `PROJECT_RENAME`, `PROJECT_RECOVERY`, `PROJECT_MIGRATION`,
`PROJECT_VALIDATE`, `PROJECT_ASSET_RELINK`, plus `PROJECT_AUTOSAVE`,
`PROJECT_DELETED`, `PROJECT_INDEXED`, `PROJECT_STALE_LOCK_REMOVED` and
`VOICES_SCANNED`.

---

## 6. Launching

```bash
python run_studio.py                     # normal start
python run_studio.py --data-root D:\MGS  # explicit data folder
python -m app.cli.main project --help    # the project command line
```

---

## 7. Known limitations (disclosed, not hidden)

1. **No narration, scenes, preview or render yet.** A project stores script text
   and an (empty) scene list; the scene editor is Stage D and narration is
   Stage C. The scene list on the Project page is read-only on purpose.
2. **Kokoro voices are usually empty.** No voice engine is installed in this
   environment, so the voice step offers "first available voice" and says why.
   This is deliberate: inventing voice names would fail at narration time.
3. **Thumbnails are placeholders.** The dashboard shows a cached preview only if
   one exists; nothing generates a preview yet, so cards read "No preview yet".
   No video is rendered to make one.
4. **Autosave writes from a background job.** If the application is killed in the
   milliseconds between the snapshot and the write, that tick is lost — the
   previous autosave remains. This is the intended trade-off for never blocking
   the interface.
5. **Locking is advisory.** It protects against a second window on the same
   machine and reports a foreign host, but it cannot stop another program from
   writing the file. That case is caught by the external-modification check.
6. **Delete is permanent.** `delete_project` removes the folder for good; only
   projects inside the application's `projects/` folder can be deleted from the
   UI, and it requires an explicit confirmation.
7. **The 10-scenario matrix runs headless here.** It exercises the real service
   and controller code paths, but it answers the modal dialogs programmatically
   instead of a person clicking them. A click-through on the Windows desktop is
   still worth doing before Stage C.
8. **Not verified on Windows in this environment.** The Windows-specific paths
   (`OpenProcess` pid probing, `os.replace` over a locked file, reserved names)
   are implemented and unit-tested, but this sandbox is Linux; the target
   machine check happens in Stage J.

---

## 8. Bugs found and fixed while building Stage B

Each of these was caught by a test written before the fix, and stays covered:

1. A save that hit a locked file (`PermissionError` on Windows: OneDrive,
   antivirus, an open editor) raised instead of reporting "not saved"; the
   previous `project.json` is now left intact and the error is friendly.
2. Recovery detection compared file timestamps. Saves and autosaves can land in
   the same clock tick, so it now compares content and uses an explicit
   interrupted-save marker.
3. An unparsable recovery file was offered to the user; it is now skipped.
4. Wizard/template defaults of `1920`/`"high"` meant a template could never
   apply; defaults are now falsy so the template wins unless the user overrides.
5. Renaming a project folder recreated the *old* folder, because the lock kept
   the old layout; the lock is now released, swapped and re-acquired.
6. `PROJECT_SAVE` notifications were missing from `service.save()`.
7. The CLI left a project lock behind after `project create`, so the next open
   said the project was already in use.
8. `verify_assets()` returned checks but never refreshed the stored `missing`
   flags, so the UI could show a stale "ready".
9. `script.txt` gained a trailing newline the user had not typed; it is now
   byte-exact.
10. `KeyValueGrid` had no `set_value`, which the Project page needed to move the
    dirty `*` without rebuilding the page.

---

## 9. Next stage

Stage C — Kokoro narration — starts only after this gate is accepted. Nothing in
Stage B needs to be undone for it: the voice settings, the per-scene narration
slot and the audio section are already in the schema.
