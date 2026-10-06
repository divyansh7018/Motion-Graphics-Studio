# Stage F report - Image Studio, local generation, editing and asset integration

**Version 0.6.0 / Stage F.** Schema `PROJECT_SCHEMA_VERSION` stays **3** (every
Stage F addition is additive; `tests/test_schema_stage_e.py` still proves
compatibility).

This report states what was **verified by running it**, what is **not verified
and why**, and what is **limited**. Nothing here is inferred from the existence
of code: a capability is only called verified when something actually ran and
produced the result quoted beside it.

---

## 0. Evidence table

| Feature | Status | Evidence |
|---|---|---|
| Image Studio GUI | **VERIFIED** | 27 offscreen GUI tests drive the real page; the page builds inside the real `MainWindow`; `test_the_image_studio_is_in_the_navigation` |
| Image import | **VERIFIED** | matrix 1-2 (PNG and JPEG imported, indexed, edited); `test_an_image_can_be_added_to_the_open_project` |
| Image editor (16 operations) | **VERIFIED** | matrix 2, 19; `test_every_offered_operation_really_runs` executes all 16 and saves each |
| Non-destructive editing | **VERIFIED** | matrix 2 and 19; `test_an_edit_leaves_the_original_alone` (bytes compared before/after) |
| Overwrite protection | **VERIFIED** | matrix 19; `test_overwriting_the_original_requires_confirmation`, `test_a_confirmed_overwrite_keeps_a_backup` |
| Asset integration (project) | **VERIFIED** | matrix 9; `test_the_file_is_copied_into_the_project` |
| Send to scene | **VERIFIED** | matrix 10; `test_every_placement_works` (all four placements) |
| Scene really draws the image | **VERIFIED** | matrix 10 (`centre pixel (20, 160, 120) at 1920x1080`); `test_a_sent_image_is_actually_painted_into_the_scene` |
| References survive reopen | **VERIFIED** | matrix 11; `test_the_reference_survives_save_and_reopen` |
| Relative paths / moved project | **VERIFIED** | matrix 12; `test_a_moved_project_still_finds_its_image` |
| Missing asset handling | **VERIFIED** | matrix 13; `test_a_deleted_source_asset_is_reported_not_crashed` |
| Provider architecture | **VERIFIED** | 6 adapters behind one contract; 70 contract tests iterate **every** registered adapter |
| Backend detection | **VERIFIED** | matrix detection block (6 backends, each with state + reason); profile: **2 ms**, detection loads no model |
| Model detection | **VERIFIED** | `ModelFolderScanner` finds model files by suffix and de-duplicates; `test_the_scanner_finds_model_files_without_loading_them` |
| Capability detection | **VERIFIED** | each adapter's flags drive the UI; `test_the_built_in_backend_does_not_claim_to_draw_prompts`, `test_unsupported_settings_are_disabled_not_left_live` |
| Text to image | **VERIFIED** (via the local-command adapter) | matrix 3: `lighthouse.png 256x256 seed=2468 0.07s`, file re-validated |
| Text to image, real diffusion model | **NOT VERIFIED** | no diffusion model is installed on the test machine (`diffusers` not installed). The refusal path is verified instead |
| Seed system | **VERIFIED** | matrix 4-5: same prompt+seed → identical md5 (`74a727ce6d7c`); new seed → different file; seed stored and replayed |
| Image to image | **VERIFIED** (built-in backend) | matrix 6: `styled.png`, source kept |
| Inpainting | **VERIFIED** (built-in backend, real mask) | matrix 7: `patched.png` from a hand-built mask |
| Outpainting | **VERIFIED** (built-in backend) | `test_the_standard_backend_extends_a_canvas_for_outpainting` (96x96 → 192x96) |
| Upscaling - Standard Resize | **VERIFIED** | matrix 8: `320x200 (2x)`; labelled *Standard Resize* |
| Upscaling - AI | **NOT INSTALLED** | matrix 8: no upscaler on this machine; the request is refused, no file written |
| Background removal | **NOT INSTALLED** | matrix; `rembg`/`backgroundremover`/`transparent_background` all absent. No fake cut-out is produced; manual masking works |
| Manual masking | **VERIFIED** | `test_a_manual_mask_cuts_a_real_hole`, `test_masking_twice_does_not_bring_invisible_pixels_back` |
| Image history | **VERIFIED** | matrix 3-5; `test_a_result_is_recorded_with_its_seed`, `test_a_failure_is_recorded_with_its_reason` |
| Prompt history | **VERIFIED** | `test_a_favourite_sorts_first`, `test_editing_a_prompt_is_explicit` (both caught real bugs - §9) |
| Metadata | **VERIFIED** | `test_a_png_carries_its_prompt_inside_the_file` (side-car deleted, prompt still read back); `test_only_present_fields_are_stored` |
| Variations | **VERIFIED** | matrix 5; `test_a_variation_records_its_parent` |
| Version graph | **VERIFIED** | matrix 19, `test_a_lineage_can_be_walked_to_the_original`, `test_a_missing_parent_is_reported_not_hidden` |
| Reference-image architecture | **VERIFIED** (architecture only) | `control_image`/`reference_image`/`lora`/`style_reference` exist on the request and capability flags gate them. **No backend on this machine exposes them**, so the controls are hidden |
| Thumbnails | **VERIFIED** | profile: 40 cold thumbnails in 467 ms (11.7 ms each), 0.04 ms cached; `test_changing_the_image_invalidates_its_thumbnail` |
| Library stays responsive | **VERIFIED** | profile: 500 images scanned in 0.10 s (5,050/s); a page of 24 in **0.1 ms**; index reload 3 ms |
| Image search / tags / collections | **VERIFIED** | `test_search_finds_by_prompt_and_by_name`, `test_tags_can_be_added_and_searched`, `test_collections_are_counted` |
| Duplicate detection | **VERIFIED** | matrix + `test_identical_files_are_reported_as_duplicates`; nothing is deleted automatically |
| Batch generation | **VERIFIED** | matrix 17: `tile_01..tile_04`, seeds `[500, 501, 502, 503]`; second batch does not overwrite |
| Concurrency safety | **VERIFIED** | profile: batch of 6 where 5 workers write into one folder |
| Cancellation | **VERIFIED** | matrix 14: `state=CANCELLED`, child processes `0 -> 0`; later request answered honestly |
| Failure handling | **VERIFIED** | every failure path carries what happened / why / what to do (matrix 15, 18) |
| No silent backend switching | **VERIFIED** | `test_a_failure_message_never_switches_the_backend`; `result.backend` names the chosen adapter |
| No fake AI | **VERIFIED** | matrix 18: a prompt with no model is refused with `NO_MODEL_INSTALLED` and a real alternative |
| Image validation (corrupt/empty/truncated) | **VERIFIED** | 7 validation tests incl. `test_a_truncated_png_is_caught` |
| Memory discipline | **VERIFIED (measured)** | profile: peak RSS **41.5 MiB**; a 1920x1080 generation peaks at 33.8 MiB |
| Settings persistence | **VERIFIED** | `test_the_chosen_backend_is_remembered` |
| Command line | **VERIFIED** | 10 `image` sub-commands exercised; all use the same services as the GUI |
| Windows | **NOT VERIFIED** | see §6 - everything ran on Linux |
| Stage A-E regression | **VERIFIED** | 1410 passed, 5 skipped, 0 failed (§1) |

---

## 1. Tests

```
1415 collected: 1410 passed, 5 skipped, 0 failed in 91.04s
ruff check app tests scripts installer --select F,E9   -> All checks passed
```

| | Count |
|---|---|
| Collected before Stage F (end of Stage E) | 1183 (1181 passed, 2 skipped) |
| **Added by Stage F** | **232 (229 passed, 3 skipped)** |
| Collected now | 1415 |
| Result | **1410 passed, 5 skipped, 0 failed** |

The per-stage totals for A-E are in each stage's own report; the figures above
are what this run actually collected and ran.

Stage F's 232 tests:

| File | Tests | Covers |
|---|---|---|
| `tests/test_image_studio.py` | 110 | capabilities, validation, saving, metadata, editing, upscaling, background removal, library, thumbnails, history, version graph |
| `tests/test_image_backends.py` | 70 (3 skipped) | the contract every adapter must keep, run against all six; the command adapter end-to-end |
| `tests/test_image_integration.py` | 25 | project/scene integration, reopen, move, delete |
| `tests/test_image_gui.py` | 27 | the real page offscreen, capabilities, jobs, generation, editing, scenes |

**The 5 skips are all environmental, and none hides a failure:**

| Skip | Why |
|---|---|
| `test_render_engine.py:312` | this FFmpeg has every codec, so none can be shown missing |
| `test_system_check.py:71` | FFmpeg is installed on this machine |
| `test_image_backends.py:100` | the standard backend **is** available, so the unavailable-path test cannot run |
| `test_image_backends.py:117` ×2 | `http`/`comfyui` report every mode, so no mode can be shown unsupported |

The last three are the honest kind of skip: the same code path is covered on the
other adapters that *are* unavailable, and by the `standard`-specific tests.

---

## 2. The manual matrix

`scripts/stage_f_manual_matrix.py` -> **29 checks, 29 PASS, 0 FAIL, 0 NOT
AVAILABLE** (run against a fresh data root).

The 17 numbered tests from the directive are all covered, plus contract and
honesty checks. Highlights, verbatim:

```
[PASS] 01 Import a PNG into the asset library: import_me.png: 400x250 png, 694 bytes
[PASS] 02 Edit an imported JPEG into a new version: photo_edited.png: 400x250 png; original untouched: True
[PASS] 03 Text to image with a local backend: lighthouse.png 256x256 seed=2468 0.07s
[PASS] 04 Same prompt and seed regenerate identically: md5 74a727ce6d7c both; recorded seed 1357
[PASS] 05 A new seed produces different output: seeds 1357 vs 24680 differ
[PASS] 08 AI upscale with no model is refused, not faked
[PASS] 10 The scene actually paints the image: centre pixel (20, 160, 120) at 1920x1080
[PASS] 12 Move the project folder: relative references still work: resolved from the new location: hero.png
[PASS] 13 A deleted asset is reported, and the project still renders
[PASS] 14 Cancel a generation: it stops and no process is left running: state=CANCELLED; child processes 0 -> 0
[PASS] 16 A 300-image library scans and pages without loading everything: scan 0.07s; page 24 of 300 in 0ms
[PASS] 17 Batch generation produces four unique, distinct files: seeds [500, 501, 502, 503]
[PASS] 18 A text prompt with no model is refused, not faked: No local image-generation model is installed...
[PASS] 19 Overwriting the original requires confirmation
```

---

## 3. Performance measurements

`scripts/stage_f_profile.py`, one run, one machine (2 CPU threads, 3.8 GB RAM,
no GPU), Python 3.11.2. These are **single-run measurements, not a benchmark**.

| What | Measured | Note |
|---|---|---|
| Import `app.image.service` | 39 ms | loads no model |
| Construct `ImageService` | < 1 ms | |
| **Detect 6 backends and their models** | **2 ms** | **RSS +0.0 MiB** |
| Generate 512x512 | 0.19 s | peak RSS 27.8 MiB |
| Generate 1024x1024 | 0.56 s | peak RSS 30.0 MiB |
| Generate 1920x1080 | 1.10 s | peak RSS 33.8 MiB |
| 40 thumbnails (cold) | 467 ms | 11.7 ms each |
| 40 thumbnails (cached) | 1 ms | 0.04 ms each |
| Scan 500 images | 0.10 s | 5,050 images/s |
| Query one page of 24 | 0.1 ms | median of 5 |
| Search by name | 0.4 ms | |
| Duplicate scan (size + dimensions) | < 1 ms | nothing hashed |
| Batch of 6 | 0.56 s | 0.09 s each |
| **Highest RSS seen** | **41.5 MiB** | at the end: 39.6 MiB |

### A real memory bug this measurement found

The first profiling run showed detection costing **1,421 ms and +476.7 MiB of
RSS**. The cause: `detect_device()` imported **torch** merely to ask whether a
GPU existed, and torch allocates roughly half a gigabyte on import. Worse, the
probe then reported *"torch: not installed"* when torch **was** installed and
simply had no accelerator - an untrue statement a user could check.

Both are fixed: detection no longer imports torch (**2 ms, +0.0 MiB**), the deep
probe distinguishes *not installed* from *installed with no accelerator*, and
`motion-studio image backends --deep` is the opt-in path. This is the single
largest measurable improvement in Stage F and it was found by measuring rather
than by assuming.

### Concurrency safety

The `command` adapter runs its program `max_parallel` images at a time
(`Stage A`'s job pool, 5 workers). A profile run of a 6-image batch exercised 5
concurrent writers into one folder. Uniqueness of the file names is decided
**before** each process starts, so this is safe by construction rather than by
luck - and `test_a_batch_produces_distinct_files_with_distinct_seeds` asserts it.

---

## 4. What actually generated images here

Being precise about this, because "text to image works" would be misleading:

| | |
|---|---|
| Backend used | **local command adapter** (`command`) and the **built-in `standard`** adapter |
| Program run | `tests/fake_image_generator.py` - a deterministic test program, **not a model** |
| Real AI diffusion model | **NOT INSTALLED** on the test machine |
| Resolution produced | 128x128, 192x192, 256x256, 512x512, 1024x1024, 1920x1080 |
| Generation time | 0.07 s (256x256) to 1.10 s (1920x1080) |
| Memory | 27.8 MiB peak at 512x512, 33.8 MiB at 1920x1080 |
| Determinism | same prompt + seed → identical md5; different seed → different file |

What this proves is the **whole adapter path**: argument substitution, a real
subprocess, cancellation that kills it, output verification, metadata, history,
the library and the scene. What it does **not** prove is any particular
diffusion model, because none is installed here.

---

## 5. Known limitations

1. **No AI model is installed on the verification machine.** `diffusers`,
   a ComfyUI server, a local HTTP endpoint and an AI upscaler were all absent.
   Text-to-image, image-to-image, inpaint, outpaint and upscale were therefore
   verified through the built-in and local-command adapters - real code paths,
   but not real models.
2. **The Diffusers, ComfyUI and ONNX adapters stop at the model-loading
   boundary.** They discover models, report capabilities, validate requests and
   load what they can (`onnxruntime` sessions load; a Diffusers pipeline raises
   for an undescribed model). They do **not** run inference, and a call that
   would need one raises with an explanation rather than returning a wrong
   image. **NOT VERIFIED** for inference.
3. **Background removal is NOT INSTALLED.** No cut-out was produced here. The
   manual mask works and is tested; automatic removal is refused, never faked.
4. **AI upscaling is NOT INSTALLED.** Standard Resize works; the AI path refuses
   (`matrix 8`).
5. **Reference images, ControlNet-style conditioning and LoRA are architecture
   only.** The fields and capability flags exist and gate the interface, but no
   installed backend exposes them, so no control is shown and nothing was
   verified.
6. **Word-level/timing claims do not apply to images.** Nothing in Image Studio
   invents a caption timing.
7. **The measurements are one machine, one run.** They say nothing about a
   machine with a real diffusion model installed, whose memory use will be
   dominated by the model's weights.
8. **`torch` is not imported during normal use.** That is deliberate, but it
   means a torch-only accelerator (for example a DirectML or MPS setup) is only
   seen with `--deep` or when a model backend needs it.

---

## 6. Windows

**NOT VERIFIED.** Everything in this report ran on Linux.

The code follows the Windows-first rules: `pathlib` throughout, no fork, no
`/content`, no Colab or Jupyter assumptions, `subprocess` with
`CREATE_NO_WINDOW`, no import-time work, and no Linux-only paths. The test suite
covers `win_amd64`-relevant behaviour (spaces in paths, relative asset paths, a
moved project folder), but **running on Windows is the only way to verify it**,
and that has not been done. Carried forward from Stage E unchanged.

---

## 7. Stage E items carried forward

These remain exactly as Stage E left them. Nothing in Stage F changed them, and
none of them was upgraded on the strength of Stage F's work:

* **KOKORO NOT VERIFIED - TEST FALLBACK USED.** No model weights on the test
  machine. Stage F does not touch narration, and makes no claim about it.
* **WINDOWS NOT VERIFIED** (see §6).
* **Long-form: LIMITED.** Stage E validated a 625 s / 50-scene timeline and did
  not complete a full long-form render. Image Studio imposes no duration limit;
  `test_an_image_...` scenarios use short timelines, and no long-form image
  project was rendered.

---

## 8. Bugs found and fixed during Stage F

Every one of these was found by running something, and each has a regression
test.

1. **The `command` backend overwrote its previous output.** Found by matrix 17
   (a second batch produced the *same* four file names). All four image-writing
   adapters picked their output name by hand; they now use `unique_path()`, so a
   second run steps around the first. *This violated the no-overwrite rule that
   Stage E had already established for renders.*
2. **`ImageService.validate` ignored the backend's own validation.** The generic
   "unsupported mode" message hid the useful one. Found by matrix 18: a user with
   no model was told "this backend does not support text to image" instead of
   "no local image-generation model is installed". The service now delegates to
   the adapter, which knows more.
3. **`detect_device()` imported torch.** +476.7 MiB and 1.4 s on every studio
   open, just to answer "is there a GPU?" - and it then reported "torch: not
   installed" when torch *was* installed with no accelerator. Found by the
   profiler. Fixed: detection no longer imports torch, and the deep probe tells
   the truth (§3).
4. **`PromptLibrary` favouriting, renaming and editing saved nothing.** The
   methods mutated an object from one read and then wrote a *different* read back
   to disk. Found by `test_a_favourite_sorts_first` and
   `test_editing_a_prompt_is_explicit`. Fixed with an `_update()` helper that
   changes the entry *inside* the list it writes.
5. **A deleted image vanished from the library instead of being reported.**
   `ImageEntry.missing` was dead code: files were dropped from the index, so the
   user's image simply disappeared. Found by
   `test_a_deleted_file_is_marked_missing_not_crashed`. Fixed: the entry is kept
   and marked missing.
6. **Variation wrongly required a prompt.** `_PROMPT_REQUIRED` listed every
   image-derived mode, blocking a valid action with "the prompt is empty". Found
   by a contract test. Fixed: a prompt is required only for *text to image*,
   where it is the entire input.
7. **`StandardBackend.models()` passed an unknown field** (`instructions`), so
   merely listing models raised `TypeError`. Found by the contract test that
   iterates every adapter.
8. **`ModelFolderScanner.files()` listed the same model twice** when a nested
   folder was also a scan root - the model manager would have shown duplicated
   weights. Found by a scanner test.
9. **The upscale job body used a stale API** (`result.output_path`,
   `result.label`, `result.message`, `result.why`; none exist), so **every
   upscale from the GUI failed** with an unexpected error. Found while
   investigating a hung GUI test: the failure popped a modal dialog, which is why
   the test appeared to hang rather than fail.
10. **`send_to_scene` did not work with the GUI's controller.** The controller
    exposes `is_open` as a *property*, not a method, and has no `import_asset`
    (that lives on its service). Both are now handled.
11. **The same image sent twice was imported twice**, creating duplicate assets.
    Fixed: an existing asset with the same checksum is reused.
12. **Image Studio's advanced-settings toggle was the only button in the
    application not connected to `clicked`** (it used `toggled`), which the
    application's own no-dead-buttons test flags. Now connected to `clicked`.
13. **Detection and model listing could raise** in a way that would stop the
    studio opening. The registry now records the failure per backend and shows it
    instead of propagating.

A note on how these were found: **nine of the thirteen came from running the
code** (the profiler, the matrix, the contract tests), not from reading it. The
hung GUI test is the clearest example - it hid a genuine, total failure of the
upscale feature that no amount of inspection had caught.

---

## 9. Files

**Created**

```
app/image/__init__.py                    app/image/provider.py
app/image/capabilities.py                app/image/registry.py
app/image/validation.py                  app/image/service.py
app/image/saving.py                      app/image/jobs.py
app/image/metadata.py                    app/image/integration.py
app/image/editor.py                      app/image/variants.py
app/image/library.py                     app/image/device.py
app/image/history.py                     app/image/upscale.py
app/image/background_removal.py
app/image/backends/__init__.py           app/image/backends/base.py
app/image/backends/standard.py           app/image/backends/command.py
app/image/backends/http.py               app/image/backends/model_backends.py
app/ui/views/image_studio_view.py        app/cli/image.py
tests/test_image_studio.py               tests/test_image_backends.py
tests/test_image_integration.py          tests/test_image_gui.py
tests/fake_image_generator.py            scripts/stage_f_manual_matrix.py
scripts/stage_f_profile.py               docs/IMAGE_STUDIO.md
docs/IMAGE_BACKENDS.md                   docs/STAGE_F_REPORT.md
```

**Modified**

```
app/core/paths.py           images/ and the thumbnail cache folder
app/core/settings.py        ImageSettings gains the Stage F fields
app/jobs/keys.py            seven image job keys
app/cli/main.py             registers and dispatches `image`
app/ui/main_window.py       Image Studio in the navigation; the Visuals
                            placeholder is gone
README.md  ROADMAP.md  TESTING.md  ARCHITECTURE.md  PROJECT_FORMAT.md
```

Nothing from Stages A-E was rebuilt, and no existing test was removed or
weakened.

---

## 10. The Stage F gate

**Not declared complete.** Five criteria are not satisfied on this machine, and
each is stated rather than hidden:

| Gate criterion | State | Why |
|---|---|---|
| Text-to-image works for at least one installed backend, *or* clearly reports that none is installed while the infrastructure is validated | **SATISFIED, second form** | No AI model is installed; every path reports that plainly, and the infrastructure is validated by 229 tests and a 29/29 matrix |
| Image-to-image / inpaint / outpaint / upscale work where supported | **SATISFIED for the built-in backend**, NOT VERIFIED for AI models | §4 |
| Windows | **NOT VERIFIED** | §6 |
| AI upscaling, background removal | **NOT INSTALLED** | §5 |
| Real diffusion inference | **NOT VERIFIED** | §5 |

Everything else in the gate list is verified by a real run, with the evidence in
§0. **Kokoro and Windows are carried forward from Stage E unchanged and are not
combined with any Stage F result.**

The honest summary: **the Image Studio, its editing, its asset integration and
its provider architecture are verified; no AI model has been run on this
machine, and nothing here claims otherwise.**
