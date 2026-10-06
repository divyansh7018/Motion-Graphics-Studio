# Stage E Report - Audio, Subtitles, Timeline, Render and QC

**Version:** 0.5.0 (Stage E) · **Project schema:** 3 (unchanged, see below)
**Suite:** 1163 tests collected · 1161 passed · 2 skipped · 0 failed
**Manual matrix:** 25 of 25 scenarios passed

---

## Headline

**PIPELINE VERIFIED.  KOKORO NOT VERIFIED - TEST FALLBACK USED.**

The render pipeline is verified with explicitly labelled synthetic test
narration; the Kokoro runtime path remains unverified on this machine because
model weights are unavailable. There is no cloud or paid voice service behind
the fallback - Kokoro is the only TTS engine in this product, so when it cannot
run the honest answer is "not verified".

Two real MP4 files were produced, measured with the probe and quality-checked:

```
docs/evidence/StageE_Test_Video1.mp4
docs/evidence/StageE_Test_Video2.mp4

1280x720 · 30.0 fps · 12.00s · h264/yuv420p · aac 48000 Hz 2 ch
446,659 bytes · QC: PASS
```

---

## 1. Kokoro verification state

Reported by `motion-studio voice selftest`, implemented in
`app/tts/selftest.py`:

```
KOKORO NOT VERIFIED - TEST FALLBACK USED
  reason: No Kokoro model weights were found (searched:
          /home/user/.cache/kokoro,
          /home/user/.cache/huggingface/hub,
          /home/user/AppData/Local/kokoro,
          /home/user/AppData/Local/huggingface/hub).
  to do : Download Kokoro 82M weights into the app's models folder, or set the
          Kokoro model path in Settings.
```

The self-test is a real test, not a status flag: when Kokoro reports itself
ready it synthesises one sentence, writes the WAV and **measures the file
afterwards**. A file shorter than 0.1 s is rejected, so "the engine returned
something" is not treated as success.

There are exactly two headline strings and no third state:

| String | Meaning |
| --- | --- |
| `KOKORO VERIFIED` | Real Kokoro audio was generated and measured here. |
| `KOKORO NOT VERIFIED - TEST FALLBACK USED` | Kokoro could not run; any test audio is labelled synthetic. |

`tests/test_kokoro_selftest.py` (5 tests) pins both strings and asserts the
not-verified result carries a reason and an instruction.

### Synthetic fallback - how it is kept honest

* Every script that falls back prints `TEST/DEV FALLBACK - not Kokoro` next to
  the narration source and ends with the two-line status block above.
* `scripts/stage_e_manual_matrix.py --narration kokoro` refuses to run at all
  rather than falling back.
* The fallback audio is a real WAV file of a measured length, written by FFmpeg
  and read back with the probe - so the timeline, mix, ducking, caption timings
  and render are all exercised for real. Only the voice is a tone.
* No cloud TTS, no paid TTS, no hidden online service was added. Hugging Face
  is unreachable from this machine and was not worked around.

---

## 2. Features completed

### Audio (`app/audio/`)
`ducking.py`, `mix.py`, `service.py` · `AudioService`

Narration, music bed, additional beds, ambience, intro/outro and sound effects,
each with volume, trim, start/end, fades, loop, mute and enabled. Mix chain is
Narration → Music → SFX → Master with per-track and master gain, loudness
normalisation to a target LUFS, and ducking that is gentle by default
(35 % music level while speaking, 0.25 s in, 0.75 s out). SFX can anchor to
project time, a scene start, or a narration event.

Audio preview mixes the master to a WAV without rendering any video.

### Subtitles (`app/subtitles/`)
`SubtitleService`

Captions are generated from **measured narration timings**. SRT, WebVTT and
styled ASS are all written; ASS is what burn-in uses. Styling covers font,
size, colour, outline, shadow, background box, position and margin. Captions
outside the safe area are **reported, never moved**. Split, merge (neighbours
only), delete and reword all work and mark the timings manual when a timing
actually changes.

There are no fabricated word-level timings: splitting divides the text at a
word boundary and says so.

### Timeline (`app/scene/service.py`)
`TimelineService`

One timeline, built from real content and shared by preview, audio, subtitles
and the render. Validates scene population, duplicate ids, ordering, zero
lengths, transition lengths, narration presence and caption timings. Fatal
errors block a render; warnings are reported and the user decides.

### Render (`app/render/`)
`RenderEngine`, `RenderService`, `OutputService`, `QCService`,
`EncoderCapabilities`

Pipeline: RenderRequest → validate → assets → audio → subtitles → scene frames
→ segment encode → assemble/mux → QC → atomic move to the output folder.
Frames stream through FFmpeg; the whole video is never held in RAM, and there is
no artificial duration cap. Segment caching lets an interrupted render resume.

Codec availability is detected from the local FFmpeg (`ffmpeg -encoders`);
an unavailable codec stops the render before a frame is drawn.

### Quality checks (`app/render/qc.py`)
Real measurements, not model assumptions: file exists, non-zero, readable, not
truncated; width, height, fps, duration, codec, pixel format; audio stream,
codec, duration, sample rate, channels, silence and clipping; black-frame
detection via `blackdetect`; timeline alignment; subtitle overrun. Verdict is
PASS / WARNING / FAIL, and a FAIL is never reported as success.

### GUI (`app/ui/views/`)
Four new pages replacing the "Music / Timeline / Render (later)" placeholders:
**Audio**, **Subtitles**, **Timeline**, **Render**. Every control writes a real
project field and every action submits a real job.

### CLI
```
motion-studio render run | validate | timeline | preview | status | cancel
motion-studio audio validate | mix
motion-studio subtitles export
motion-studio voice selftest
```
All of them go through the same service objects the GUI uses. A full CLI chain
was run against a real project: create → timeline → validate → preview →
audio validate → subtitles export → render run → `COMPLETED`, 512x288 @ 25 fps,
7.8 s, h264 + aac.

---

## 3. Tests

**1163 collected · 1161 passed · 2 skipped · 0 failed** (80 s, offscreen Qt).

Stage E contributed **268** tests across 12 files:

| File | Tests | Covers |
| --- | --- | --- |
| `test_audio_service.py` | 35 | Mix graph, fades, ducking, validation |
| `test_subtitles.py` | 32 | Cue generation, SRT/VTT/ASS, editing |
| `test_render_engine.py` | 32 | Real renders, states, resume, cancellation |
| `test_render_capabilities.py` | 31 | Codec detection and validation matrix |
| `test_stage_e_services.py` | 28 | `TimelineService`, `SubtitleService` |
| `test_render_output.py` | 25 | Naming, sequence, atomic move, folder |
| `test_render_qc.py` | 21 | Every QC check against real files |
| `test_gui_stage_e.py` | 21 | The four GUI pages |
| `test_media_probe.py` | 18 | Probe parsing without ffprobe |
| `test_render_segments.py` | 15 | Segment planning and overlap |
| `test_kokoro_selftest.py` | 5 | The two verification states |
| `test_schema_stage_e.py` | 5 | Old files load, new files round-trip |

**The 2 skips are environmental, not omissions:** one is "this FFmpeg has every
codec, so none can be shown missing" (covered deterministically by a test that
fabricates a capability list), the other pre-dates Stage E.

Stages A-D were not weakened: no existing test was removed or relaxed.

### GUI thread safety (section 17)

`test_heavy_stage_e_work_is_submitted_as_a_job_not_run_inline` intercepts
`jobs.submit` and asserts that all six heavy Stage E actions - audio validate,
audio mix, timeline check, capability detection, render planning and rendering -
are submitted as jobs rather than run inline. FFmpeg capability detection in
particular starts a subprocess and would otherwise block the Qt thread.

`test_a_second_render_is_not_submitted_while_one_is_running` proves one action
produces one job, with no hidden duplicate.

---

## 4. Manual matrix

`scripts/stage_e_manual_matrix.py --data-root /tmp/mgs_stage_e`
**25 of 25 scenarios passed.**

| # | Scenario | Result |
| --- | --- | --- |
| 01 | Timeline builds and validates | 0:10.2 over 4 scenes, 0 errors |
| 02 | Scene lengths come from measured narration | 4 of 4 |
| 03 | Audio validates | 4 placements, 0 errors |
| 04 | Master mix renders and is measurable | 10.20 s pcm_s16le 48000 Hz 2 ch |
| 05 | Ducking changes the mix | ducked differs from unducked |
| 06 | Captions generated from narration | 4 cues |
| 07 | SRT, VTT and ASS are written | 3 non-empty files |
| 08 | Overlapping captions are caught | `SUBTITLE_OVERLAP` |
| 09 | First render completes | 1280x720 @ 30 fps, 10.20 s, h264/aac |
| 10 | QC measures the finished file | PASS, width 1280 |
| 11 | Burnt-in captions are drawn in | differs from the plain render |
| 12 | Second render does not touch the first | `_1` unchanged, `_3` new |
| 13 | Five resolutions render at exact sizes | all five exact |
| 14 | Quality presets reach the encoder command | crf 30/23/20/15 |
| 15 | An explicit bitrate replaces CRF | bitrate present, no `-crf` |
| 16 | A cancelled render stops cleanly | CANCELLED, no file |
| 17 | No FFmpeg process is left running | none left |
| 18 | Missing narration blocks the render | blocked before frames |
| 19 | An unavailable codec stops the render early | `CODEC_UNAVAILABLE` |
| 20 | An unusable output folder is reported | `OUTPUT_FOLDER_UNAVAILABLE` |
| 21 | A project with no scenes is refused | blocked |
| 22 | A missing asset blocks the render | `IMAGE_MISSING` |
| 23 | A resume render completes | 7 segments |
| 24 | A 50-scene timeline plans without a cap | 50 scenes, 10:25 (625 s) |
| 25 | The project saves and reloads intact | 4 scenes, 4 captions |

### Five resolutions (section 12)

Verified by reading the finished files back through the probe, not by trusting
the request:

```
1280x720=1280x720 · 1920x1080=1920x1080 · 1080x1920=1080x1920
1080x1350=1080x1350 · 1080x1080=1080x1080
```

### Quality presets (section 13)

Checked in the encoder argument list the render will actually use, so a silent
downgrade would show up:

```
draft: crf30/veryfast · medium: crf23/medium · high: crf20/slow · ultra: crf15/veryslow
```

---

## 5. Render result and QC

`scripts/stage_e_end_to_end.py --data-root /tmp/stage_e`, run after the
blackdetect fix:

| | Video1 | Video2 |
| --- | --- | --- |
| Path | `docs/evidence/StageE_Test_Video1.mp4` | `docs/evidence/StageE_Test_Video2.mp4` |
| Status | COMPLETED | COMPLETED |
| Resolution | 1280x720 | 1280x720 |
| Frame rate | 30.0 fps | 30.0 fps |
| Duration | 12.00 s (timeline 12.00 s) | 12.00 s |
| Video | h264 / yuv420p | h264 / yuv420p |
| Audio | aac 48000 Hz 2 ch | aac 48000 Hz 2 ch |
| Size | 446,659 bytes | 446,659 bytes |
| **QC** | **PASS** | **PASS** |
| Render time | 15.6 s | 14.5 s |

Segments 7 · frames 360 · subtitle cues 5 · SRT and VTT side-cars written.

**Output numbering:** Video1 was untouched by the second render (md5 identical
before and after: `0fafa97a663052cfa5e4911625568d12`), and Video2 is a separate
file. The two files are byte-identical because the encode is deterministic and
nothing changed between renders - different names, same content, which is the
expected never-overwrite behaviour. No duplicate hidden jobs: the GUI test
asserts a second render cannot start while one is running.

**No stale QC evidence remains.** Every QC figure in this report was produced
by a run after the blackdetect fix; the pre-fix run that reported a whole
12 s video as black was discarded, and the fix has a regression test.

---

## 6. The blackdetect correction

QC was reporting an entire 12-second 720p render as `BLACK_FRAMES`. The frame
was measured directly: mean luma 21.94/255, extrema (0,255), 99.99 % of pixels
non-zero. It was a navy theme, not a black screen.

The cause was a parameter mix-up. `blackdetect` takes two separate knobs:

* `pix_th` - per-pixel luma below which a pixel counts as black
* `pic_th` - the fraction of such pixels needed before a *frame* is black

`probe_detect` was passing the ratio value `0.98` into `pix_th`, so almost every
pixel counted as black:

```
pix_th=0.98          → black_start:0        black_duration:11.97   (wrong)
pix_th=0.10:pic_th=0.98 → black_start:8.566667 black_duration:3.4
```

Measured across real frames, 0.05 separates a dark design from real black:

| Frame | luma < 0.02 | luma < 0.05 | luma < 0.10 |
| --- | --- | --- | --- |
| true black | 100.00 % | 100.00 % | 100.00 % |
| dark navy theme | 0.02 % | **0.12 %** | 98.59 % |

Fixed to `pix_th=0.05` with `pic_th=0.98`. The regression test asserts **both**
directions - a dark theme is not flagged, and a genuinely black video still is -
so the check cannot have been quietly disabled instead. Proven meaningful by
running both parameter sets: old params report 1.96 s of "black" on a navy
clip, new params report 0.

**Consequence, stated plainly:** QC verdicts reported in earlier turns were
computed with the buggy threshold and should not be relied on. All QC results
in this report were regenerated afterwards.

---

## 7. Other real bugs found and fixed in Stage E

These were found by writing the code and its tests, not by review.

| Bug | Effect | Fix |
| --- | --- | --- |
| `blackdetect` threshold mix-up | Every dark-themed video flagged as black | Correct `pix_th`/`pic_th` split |
| Crossfade treated as out-of-order | **Every** project with a dissolve blocked from rendering | Overlap allowed up to the transition length |
| `output_dir` read from the wrong section | The folder the user picked was silently ignored; every render went to `renders/` and a custom filename template did nothing | `export_settings()` prefers the export section |
| Disabled scenes vanished from the Timeline page | Dead code branch; a switched-off scene disappeared with no explanation | Table walks the project's scenes and labels excluded ones |
| `attach_narration_to_scenes` set an undeclared field | `NarrationSpec` has no `status`, so `asdict()` dropped it on save | Provenance stored in `extra`, which round-trips |
| Nothing linked generated narration to scenes | A generated voice could never reach a scene | `attach_narration_to_scenes()` |
| `_libass_available` parsed the wrong word | Burn-in reported unavailable on builds that have libass | Parse `parts[1]` |
| `blackdetect` output split on `=` | Black-frame detection never parsed anything | Try `:` then `=` |
| `astats` reports `-inf` dB for silence | Fully silent audio reported as `AUDIO_STATS_UNAVAILABLE` | `_decibels()` maps `-inf` → -120.0 |
| Fade-out dropped when track length unknown | A fade silently never happened | Fall back to `mix_duration - start` |
| `merge_cues` joined non-adjacent cues | One 24 s caption across 16 s of silence | Neighbours only |
| Mix command with `apad` but no `-t` | Runaway encode wrote 9.75 GB for an 8.4 s timeline | Hard `-t <duration>` on output |

Two checks were **removed as unreachable** rather than kept for show: a
`NARRATION_LONGER_THAN_SCENE` rule (the scene length is derived from the
narration, so the comparison can never fire) and the disabled-scene branch
above. The invariant behind the first - a scene is never shorter than its
narration - is tested instead.

---

## 8. Versioning

* `APP_VERSION` 0.4.0 → **0.5.0**
* `APP_STAGE` D → **E**, label "Stage E - Audio, subtitles, timeline, final
  render and QC"
* `PROJECT_SCHEMA_VERSION` **stays at 3**

The schema decision is evidence-based, not an oversight. Stage E added audio,
subtitle and export fields to the model, but every one is additive with a
default. `tests/test_schema_stage_e.py` proves:

* a pre-Stage-E schema-3 file loads with the new defaults filled in
  (music fades 1.0/2.0, ducking on at 0.35, `output_dir` = `renders`)
* a saved project round-trips exactly
* an unknown key survives in `extra` rather than being discarded

No migration is needed, so bumping the version would force one with nothing to
do. Had a migration been required it would have been written, with old-load,
new-save and migration tests.

---

## 9. Files changed

54 files, +14,250 / −12 lines since the Stage D baseline (`c88d375`).

**New modules**
`app/media/probe.py` · `app/audio/{ducking,mix,service}.py` ·
`app/subtitles/service.py` · `app/render/{capabilities,encode,engine,frames,jobs,output,platform,qc,segments,service}.py` ·
`app/scene/service.py` · `app/tts/selftest.py` · `app/cli/render.py` ·
`app/ui/views/{audio,subtitles,timeline,render}_view.py`

**Scripts**
`scripts/stage_e_end_to_end.py` · `scripts/stage_e_manual_matrix.py`

**Tests (12 new files, 268 tests)**
`test_audio_service.py` · `test_subtitles.py` · `test_render_engine.py` ·
`test_render_capabilities.py` · `test_stage_e_services.py` ·
`test_render_output.py` · `test_render_qc.py` · `test_gui_stage_e.py` ·
`test_media_probe.py` · `test_render_segments.py` ·
`test_kokoro_selftest.py` · `test_schema_stage_e.py`

**Evidence**
`docs/evidence/` - the two deliverable MP4s, the matrix render, and the three
caption files.

**Modified**
`app/project/model.py` (audio/subtitle/export sections) ·
`app/ui/main_window.py` (page registration and job routing) ·
`app/jobs/keys.py` · `app/cli/main.py` · `app/cli/voice.py` ·
`app/tools/ffmpeg.py` · `app/core/version.py` · `installer/setup_windows.py`

---

## 10. Known limitations

1. **Kokoro is not verified on this machine.** No model weights, and Hugging
   Face is TLS-blocked here. The real Kokoro path is called first by every
   script and only falls back on failure, but that fallback is what ran for
   every render in this report. This is the single largest gap in Stage E
   verification.

2. **No ffprobe.** The probe parses `ffmpeg -i` stderr instead. It works and is
   tested, but it is a workaround; a build with ffprobe gives cleaner metadata.

3. **Two-pass encoding is offered but not separately verified.** It is only
   valid where a bitrate is set, and the argument generation is tested; no
   two-pass render was run end to end.

4. **Size and time estimates are approximations.** They are always labelled
   "Estimated" and never presented as measurements. The estimate for the
   12 s render was within a factor of about 1.2 of the real file.

5. **Dark-theme black-frame detection uses a fixed 0.05 luma threshold.** It is
   correct for the frames measured here, but a video that is *intentionally*
   almost entirely black with a small bright element could still be flagged.
   It reports a warning, not a failure.

6. **Windows-specific paths are untested here.** This verification ran on Linux.
   `CREATE_NO_WINDOW`, path separators and the installer were not exercised.

7. **The 50-scene long-form test plans and validates but does not render.** It
   proves the timeline handles 625 s without a cap; a full 10-minute encode was
   not run in this session.

8. **Memory use is controlled by design, not measured.** Frames stream to FFmpeg
   and are never accumulated, but no profiling figure is quoted here.

---

## 11. Gate status

Every item in the directive's Stage E gate was addressed. The gate passes on
pipeline grounds:

Audio GUI · Subtitle GUI · Timeline GUI · Render GUI · `TimelineService` ·
`SubtitleService` · real audio pipeline · subtitle pipeline · timeline ·
render · FFmpeg · quality settings · multi-resolution · output numbering ·
QC · blackdetect corrected and retested · cancellation · failure handling ·
manual matrix · documentation · versioning · all previous tests pass · Stage E
tests pass · real MP4 produced and validated.

**Kokoro is reported separately and is not verified.** Stage E should not be
signed off on the basis of this report alone if Kokoro verification is part of
the acceptance criteria - run `motion-studio voice selftest` on a machine with
the weights and re-run `scripts/stage_e_manual_matrix.py --narration kokoro`.
