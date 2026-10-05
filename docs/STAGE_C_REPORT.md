# Stage C report — script, Kokoro 82M narration and voice

**Build:** 0.3.0 (stage C) · **Project schema:** v3 · **Date:** 2026-10-05
**Branch:** `arena/01a1086f-motion-graphics-studio`

This report states what was actually run and what came back. Where something
could not be verified in this environment, it says so instead of implying
otherwise (section 64).

---

## 1. What Stage C delivers

The Stage C workflow, end to end and fully local:

```
open project → write or import script → see counts and an estimated duration
→ pick narration language → pick a discovered Kokoro voice → preview it
→ set speed → set volume → generate narration → validated WAV on disk
→ measured duration stored → close → reopen → everything remembered
```

Two new interface pages (**Script**, **Narration**) and three new CLI groups
(`script`, `voice`, `narration`). No paid API, no cloud TTS, no mandatory
internet connection once the model and runtime are installed.

Stage A (shell, logging, jobs, cancellation, system check, FFmpeg detection,
storage, atomic writes, backups, cache, diagnostics, smoke test) and Stage B
(project model, `project.json`, versioning, save/load, save-as, duplicate,
autosave, recovery, recents, browser, settings, assets, validation) are
**unchanged in behaviour** — only extended. Every Stage A and Stage B test still
passes (§9).

---

## 2. Updated file tree

New in Stage C:

```
app/script/                  script parsing, statistics, import/export
    __init__.py
    parser.py                plain <-> structured blocks, 24 field aliases
    stats.py                 counts + labelled duration estimates
    io.py                    encoding validation, Markdown, import/export

app/tts/                     the narration pipeline (Kokoro is the only engine)
    __init__.py
    preprocess.py            safe, configurable text preparation
    capabilities.py          probe: package, runtime, model, voices, languages
    voices.py                catalogue, discovery, filtering, choice validation
    engine.py                lazy load, synthesis, cancellation, self-test
    audio.py                 WAV writing + strict validation
    cache.py                 source/settings hashes, staleness comparison
    narration.py             script -> WAV pipeline and the seven status states
    jobs.py                  four job bodies + specs (init, scan, preview, narrate)

app/ui/views/script_view.py      the Script page
app/ui/views/narration_view.py   the Narration page
app/cli/voice.py                 script / voice / narration CLI groups

scripts/stage_c_manual_matrix.py 16 manual scenarios, repeatable evidence

tests/fake_tts.py            test-only Kokoro double (real float32 audio)
tests/test_script.py         43 tests
tests/test_tts.py            52 tests
tests/test_narration.py      39 tests
tests/test_narration_ui.py   27 tests
tests/test_cli_voice.py      14 tests
tests/test_checks_voice.py   11 tests
tests/test_tts_jobs.py        9 tests
```

Modified:

```
app/core/version.py              0.3.0, stage C, schema v3
app/project/model.py             NarrationPlan + NarrationTrack (v3)
app/project/migrations.py        v2 -> v3 migration
app/project/service.py           8 narration/script methods + 3 result dataclasses
app/checks/items.py              voice block + real TTS self-test
app/diagnostics/smoke.py         an eleventh step: the Stage C narration workflow
app/ui/main_window.py            two new pages, voice job dispatch
app/cli/main.py                  registers the three new CLI groups
docs/PROJECT_FORMAT.md           the narration section and v3 migration
docs/TESTING.md, docs/ROADMAP.md, docs/ARCHITECTURE.md, README.md
tests/test_smoke.py              +3 tests for the new smoke step
tests/test_project_store.py      schema assertions made version-independent
```

`app/tools/kokoro.py` is retained for its Stage B callers
(`app/checks/items.py`, `app/ui/views/settings_view.py`) but is superseded by
`app/tts/capabilities.py`. Reconciling or removing it is Stage D work.

---

## 3. Detected language and voice counts

The `kokoro` package **is** installed in this environment (0.9.4), with ONNX
Runtime 1.30.0 and the misaki phonemiser. Detection therefore ran against the
real package, not a simulation:

```
$ python -m app.cli.main voice check
Kokoro      : Kokoro is installed but its model weights are missing.
Package     : installed 0.9.4
Runtime     : onnxruntime 1.30.0 — CPU execution
Model       : No Kokoro model weights were found.
Voices      : 0
Languages   : a, b, e, f, h, i, j, p, z
Phonemiser  : misaki, espeakng_loader, phonemizer
Initialised : no
```

**Detected language count: 9.** Taken from the installed pipeline's own table
(`kokoro.pipeline.LANG_CODES`), not from a list in this codebase:

| Code | Language | Code | Language |
|---|---|---|---|
| `a` | English (US) | `i` | Italian |
| `b` | English (UK) | `j` | Japanese |
| `e` | Spanish | `p` | Portuguese |
| `f` | French | `z` | Mandarin Chinese |
| `h` | Hindi | | |

**Detected voice count: 0** — the model weights are not present, so there are no
voice files to read. The panel explains that rather than showing a blank list.

Against a synthetic model folder laid out like a real installation, the same code
discovers what is on disk — no fixed list, no fixed count:

```
$ python -m app.cli.main voice list --model-dir <synthetic model>
Voices   : 4
af_bella         English (US)       female   …
am_adam          English (US)       male     …
hf_alpha         Hindi              female   …
hm_ishaan        Hindi              male     …
```

On a machine with the weights installed, this reports that model's actual
catalogue. Nothing caps or assumes the number, and Hindi/English are not
special-cased.

## 4. Kokoro self-test result

The System Check's `voice.selftest` step generates real audio through the
configured pipeline and validates the WAV. With the real package installed but no
weights:

```
⚠ voice.kokoro    : Kokoro is installed but its model weights are missing.
⚠ voice.voices    : no voices discovered
✓ voice.languages : 9 languages (detected from engine)
- voice.selftest  : Skipped: Kokoro is not ready
```

`voice.kokoro` is reported as **optional**, so the application still starts and
every other function works (matrix scenario 12 confirms this: the project was
created and saved, with 0 start-up blockers).

**The self-test has not produced audio in this environment**, because it needs
the model weights — see §8.

## 5. Narration generation result

Run through the real pipeline with the test double standing in for the model
(`tests/fake_tts.py` produces real float32 samples, so the WAV writing,
validation, duration measurement and staleness logic all run for real):

| Check | Result |
|---|---|
| Output file | `audio/narration/narration_full.wav` |
| Measured duration | 9.00 s (24 kHz, mono) |
| Validation | exists, non-zero, readable, valid rate/channels, duration > 0 |
| Status after generation | `ready` |
| Status after reopening | `ready`, duration read back from the file |

End-to-end smoke step, which creates `StageC_Test`, generates, saves, closes,
reopens and reads the metadata back:

```
[OK  ] Stage C: script to narration to reopened project (0.06s)
        voice hf_alpha, 2.71s of audio at 24000 Hz, status ready after reopening
        file: …/workspace/smoke/StageC_Test/audio/narration/narration_full.wav
```

When Kokoro is absent this step **skips** with the reason, rather than passing —
a skip is honest, a pass would not be.

Estimated vs actual duration, measured here for the Hindi fixture script
(`नमस्ते दोस्तों। आज हम बात करेंगे निवेश की। …`):

```
estimated_duration_seconds: 5.6      <- labelled as an estimate in the UI
actual_duration_seconds   : 5.571    <- measured from the written WAV
duration_seconds()        : 5.571    <- the value later stages use
sample_rate/channels/size : 24000 / 1 / 267472
status                    : ready
```

The estimate is kept for display before generation exists; the measured value is
authoritative afterwards (directive section 36).

---

## 6. Manual test matrix (directive section 59)

`scripts/stage_c_manual_matrix.py --data-root /tmp/mgs_evidence --engine fake`
— **16 of 16 scenarios passed**:

| # | Scenario | Result |
|---|---|---|
| 1 | English script → voice → narration | `narration_full.wav`, 9.00 s, 24 kHz |
| 2 | Hindi script → Hindi voice → narration | 4.50 s, language `h` |
| 3 | Mixed Hindi/English text | text preserved through save/reload, 5.07 s audio |
| 4 | Change voice | `ready` → `stale` |
| 5 | Change script | `stale` after the edit, back to `ready` after reverting |
| 6 | Change speed | 9.00 s @1.0x → 6.00 s @1.5x, stale in between |
| 7 | Close and reopen | voice `hf_alpha`, 1.25x, `ready`, 7.20 s |
| 8 | Delete the WAV | `missing` + "Generated narration file is missing…" , no crash |
| 9 | Cancel mid-generation | cancelled in 0.001 s, no file left, project saved |
| 10 | Generate once | job key `voice.narration`, `allow_parallel=False`, 1 file |
| 11 | Generate again | 1 file, new timestamp, no duplicates |
| 12 | Kokoro unavailable | project saved, `voice.kokoro=optional`, 0 start-up blockers |
| 13 | Model unavailable | friendly refusal before touching a model |
| 14 | Invalid voice | "not in the installed Kokoro catalogue" |
| 15 | Long script (40 scenes, 1 320 words) | parsed + stored in 0.002 s, 563.64 s of audio |
| 16 | Close mid-edit | the saved script came back unchanged, schema 3 |

These scenarios were run with the test double. Re-run with `--engine real` on a
machine that has the model installed for the real-weights evidence.

---

## 7. Automated tests

```
LD_LIBRARY_PATH=/tmp/stublib QT_QPA_PLATFORM=offscreen \
    python -m pytest tests -q
→ 566 passed
```

| Group | Tests |
|---|---|
| Stage A | 153 |
| Stage B | 208 |
| **Stage C** | **205** |
| **Total** | **566** |

Stage C by file: `test_script` 43, `test_tts` 52, `test_narration` 39,
`test_narration_ui` 27, `test_cli_voice` 14, `test_checks_voice` 12,
`test_tts_jobs` 9, plus 3 new smoke-test cases.

Lint: `ruff check app/ tests/ scripts/ installer/ run_studio.py --select F,E9`
→ **All checks passed.**

### Bugs found and fixed during Stage C

Each of these was found by a test, not by inspection, and each now has a
regression test:

| Bug | Consequence if shipped | Test |
|---|---|---|
| Every TTS job called `context.progress.report(...)`, a method Stage A's `ProgressReporter` does not have | **every** preview and narration job crashed the instant it reported progress | `test_tts_jobs.py` (all four job bodies) |
| `generate_narration` never recorded the preprocessing config it used | the settings hash could never be reproduced, so **every** track read as STALE on the next refresh | `test_narration.py::test_a_track_is_not_stale_after_reopening_without_a_probe` |
| The staleness snapshot omitted the model version | same permanent-STALE symptom | same test, plus `test_a_real_model_change_is_still_reported_as_stale` |
| `generate_narration` did not write the voice/speed/volume back to the project | project said voice `""` while the track said `hf_alpha` — permanently stale | same test |
| The Refresh voices button ran `probe_kokoro()` on the Qt thread | the window froze for seconds while `kokoro`/`torch` imported | now a background job; `test_narration_ui.py` |
| Volume and narration-mode controls were never connected to a save handler | two visibly dead controls | `test_narration_ui.py::test_changing_speed_is_stored_on_the_project` |
| `voice_scan_job` ignored a model dir configured in Settings | the panel and the job could scan different folders | `test_tts_jobs.py::test_the_scan_job_honours_a_configured_model_dir` |
| The language dropdown listed `af` and `am` separately | "English (US)" appeared twice in the picker | `test_narration_ui.py::test_the_language_filter_narrows_the_voice_list` |
| An unavailable voice could not be selected at all | the user could not set up a project before installing the engine | `test_narration_ui.py::test_a_voice_can_be_selected_into_the_project` |
| The smoke step used a `NarrationTrack.resolve()` method that does not exist | the new smoke step always failed | `test_smoke.py::test_the_narration_step_runs_the_full_workflow` |

---

## 8. Known limitations

1. **Real Kokoro inference was not executed in this environment.** The model
   weights could not be obtained here. This was verified rather than assumed:
   small JSON API calls to `api.github.com` succeed, while large binary downloads
   (GitHub release assets and anything on `huggingface.co`) fail with
   `TLS/SSL connection has been closed (EOF)`. Kokoro ONNX weights are published
   as GitHub release assets, and the matching `voices.npy` only on HuggingFace,
   so neither can be fetched from this sandbox.
   **What is verified:** the real `kokoro` 0.9.4 package, ONNX Runtime 1.30.0 and
   the misaki phonemiser are detected, and 9 languages are read from the
   installed pipeline's own table (§3). **What is not:** model initialisation,
   preview audio and narration audio. Those need the weights — see §11.
2. **The manual matrix ran with the test double**, not a real voice. Re-run with
   `--engine real` for real-weights evidence.
3. **No scene rendering, no video output, no images.** Stage C stops at narration
   audio, as directed.
4. **Mixed-language pronunciation** depends on the selected voice and the
   installed pipeline. The UI says so rather than promising correct Hinglish.
5. **Preprocessing is conservative by default.** Number and abbreviation
   expansion are off; nothing rewrites the user's wording silently.
6. **`app/tools/kokoro.py` is now redundant** with `app/tts/capabilities.py` but
   still has two Stage B callers. Reconcile in Stage D.
7. **The matrix runs headless.** It exercises the same services the interface
   uses, but it is not a substitute for clicking through the real window on
   Windows.
8. **Windows paths are unit-tested but not executed on Windows here**; this
   environment is Linux.
9. **The test suite is now hermetic with respect to the optional dependency.**
   Installing the real `kokoro` package initially broke four tests that assumed
   it was absent; they now force the condition they need, so they mean the same
   thing on any machine.

## 9. Stage C gate (directive section 62)

| # | Requirement | State | Evidence |
|---|---|---|---|
| 1 | Script editor works | ✅ | `test_narration_ui.py` (Script page) |
| 2 | Plain script works | ✅ | `test_script.py` (plain → one block) |
| 3 | Structured script works | ✅ | `test_script.py` (parser, aliases, unknown labels kept) |
| 4 | Import works | ✅ | `test_script.py`, `test_cli_voice.py` (encoding, binary refused) |
| 5 | Export works | ✅ | `test_script.py`, `test_cli_voice.py` (refuses to overwrite) |
| 6 | Unicode works | ✅ | `test_script.py` (Hindi, mixed, punctuation, currency, quotes) |
| 7 | Script persistence works | ✅ | matrix 16, `test_narration_ui.py` |
| 8 | Kokoro is actually detected | ✅ **real package** | §3: kokoro 0.9.4, onnxruntime 1.30.0, misaki |
| 9 | Kokoro model is actually validated | ✅ code / ⚠️ needs weights | `deep_init_check` loads the model; weights absent (§8.1) |
| 10 | Voices dynamically discovered | ✅ | `test_tts.py`, `test_tts_jobs.py`; §3 |
| 11 | Languages dynamically discovered | ✅ **real package** | 9 languages from `kokoro.pipeline.LANG_CODES`; §3 |
| 12 | Language filtering works | ✅ | `test_narration_ui.py` |
| 13 | Voice filtering works | ✅ | `test_narration_ui.py` (gender, search, favourites) |
| 14 | Voice preview works | ✅ code / ⚠️ needs weights | `test_tts.py`, `test_tts_jobs.py` |
| 15 | Speed works | ✅ | matrix 6 (9.00 s → 6.00 s) |
| 16 | Volume works | ✅ | `test_tts.py` (peak-limited, 0–125%) |
| 17 | Real narration generation works | ✅ pipeline / ⚠️ needs weights | §5 |
| 18 | WAV validation works | ✅ | `test_tts.py` (truncated, zero-length, bad header) |
| 19 | Actual duration recorded | ✅ | matrix 1/7; estimated 5.6 s vs measured 5.571 s |
| 20 | Narration status works | ✅ | seven states, `test_narration.py` |
| 21 | Stale detection works | ✅ | matrix 4/5/6 |
| 22 | Missing narration handling works | ✅ | matrix 8 |
| 23 | Cache invalidation works | ✅ | `test_narration.py`, `test_tts.py` |
| 24 | Cancellation works | ✅ | matrix 9, `test_tts_jobs.py` |
| 25 | UI stays responsive | ✅ | matrix 15; discovery moved off the Qt thread |
| 26 | One Generate = one job | ✅ | matrix 10 (`allow_parallel=False`) |
| 27 | No duplicate generation | ✅ | matrix 11 |
| 28 | Restart/reopen test passes | ✅ | matrix 7, smoke step |
| 29 | CPU-only workflow passes | ✅ | `device="cpu"`, no CUDA path anywhere |
| 30 | Stage A tests still pass | ✅ | 153 passed |
| 31 | Stage B tests still pass | ✅ | 208 passed |
| 32 | Stage C tests pass | ✅ | 198 passed |

**29 of 32 items are fully verified.** Items 8 and 11 are verified against the
real installed `kokoro` package. Items 9, 14 and 17 are verified in code and
against the test double, but not against real Kokoro weights, because the weights
cannot be downloaded in this environment (§8.1).

---

## 10. Commands

**Launch:**

```
python run_studio.py
python run_studio.py --data-root D:\MotionStudio     # portable data folder
```

**Tests:**

```
python -m pytest tests -q                            # 566 tests
python -m pytest tests/test_narration.py -q          # narration pipeline
python scripts/stage_c_manual_matrix.py --data-root /tmp/mgs_evidence --engine real
python -m app.cli.main voice check                   # engine readiness
python -m app.cli.main smoke-test                    # end-to-end self-test
```

---

## 11. What to run on the Windows machine

The one thing this environment cannot do:

```
python -m pip install kokoro onnxruntime
python -m app.cli.main voice check          # expect "Initialised : yes (verified)"
python -m app.cli.main voice list           # expect the real voice count
python -m app.cli.main voice preview --voice <a real voice id>
python scripts/stage_c_manual_matrix.py --data-root %TEMP%\mgs_evidence --engine real
python -m app.cli.main smoke-test           # the Stage C step must be OK, not skipped
python -m pytest tests -q
```

Report back the detected voice count, the language count, the self-test result
and the measured duration of a generated file. Until those are confirmed, the
Stage C gate items 8, 9, 14 and 17 stay marked as code-verified only.

---

## 12. Statement

No known P0 or P1 bugs remain in the Stage C workflow. All 566 automated tests
pass and all 16 manual scenarios pass. Kokoro detection and language discovery
are verified against the real installed package (0.9.4). **Real Kokoro audio
generation has not been verified in this environment** — the model weights cannot
be downloaded here, which was confirmed by testing the network rather than
assuming it — so the stage is **not** declared complete until §11 has been run on
the target machine. Everything else in the gate is verified with the evidence
above.
