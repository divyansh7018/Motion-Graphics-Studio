# Stage E Report - Audio, Subtitles, Timeline, Render and QC

**Version:** 0.5.0 · **Stage:** E · **Project schema:** 3 (unchanged) · **Settings schema:** 1

This report was produced by the final Stage E hardening and verification pass.
Every number in it came from a command that was actually run on the
verification machine, and every claim is labelled with one of:

| Label | Meaning |
| --- | --- |
| **VERIFIED** | It was executed and the result was measured. |
| **NOT VERIFIED** | It could not be executed here, and the reason is stated. |
| **NOT TESTED** | Nothing was attempted; no claim is made either way. |
| **LIMITED** | Partially verified, with the uncovered part named. |
| **OPTIONAL** | Not required for the Stage E gate. |

---

## 0. Acceptance evidence table

| Feature | Status | Evidence |
| --- | --- | --- |
| Audio (narration, music, SFX, ducking, fades, master) | **VERIFIED** | Matrix 03-05: 4 narration placements, master mix measured 10.20 s `pcm_s16le` 48 kHz 2 ch, ducked master differs from unducked. 35 tests in `tests/test_audio_service.py`. |
| Subtitles (generate, SRT, VTT, burn-in, styling) | **VERIFIED** | Matrix 06-08, 11: 4 cues from measured narration, SRT+VTT+ASS written, `SUBTITLE_OVERLAP` caught, burnt-in file differs byte-wise from the plain one. |
| Timeline (scene/narration/music/SFX/caption timing, transitions) | **VERIFIED** | Matrix 01-02: 0:10.2 over 4 scenes, 0 errors; 4/4 scene lengths from measured narration. `TimelineService` shared by GUI, CLI and engine. |
| Final render (real MP4) | **VERIFIED** | `docs/evidence/StageE_Test_Video1.mp4` - 1280x720, 30.0 fps, 12.00 s, h264/yuv420p, aac 48 kHz 2 ch, 446,514 bytes. |
| FFmpeg | **VERIFIED** | 6.0-static; real encodes in every render above; `libx264`, `libx265`, `libvpx-vp9`, `libaom-av1`, `libass` present. |
| FFprobe | **VERIFIED** | 6.0-static; matrix 26: `source=ffprobe`, per-stream durations read. Every measurement in this report came from FFprobe, not from the fallback. |
| Output validation (file, container, streams, size, fps, duration, codec, audio) | **VERIFIED** | Matrix 28: the render's own QC report records **18 checks**, none missing, none unavailable. |
| QC verdicts (PASS / WARNING / FAIL / CHECK NOT AVAILABLE) | **VERIFIED** | Matrix 10, 27, 30; `tests/test_stage_e_hardening.py`. |
| Cancellation | **VERIFIED** | Matrix 16, 17, 34: `CANCELLED`, no finished file, 0 FFmpeg children left alive. |
| Failure handling | **VERIFIED** | Matrix 18-22: missing narration, unavailable codec, unusable output folder, empty project, missing asset - all blocked before encoding. |
| Output numbering (`_Video1`, `_Video2`, …) | **VERIFIED** | Matrix 33: two clicks → exactly two new files, one per click, first file's md5 unchanged. |
| Quality presets (Draft/Medium/High/Ultra) | **VERIFIED** | Matrix 14: `crf30/veryfast`, `crf23/medium`, `crf20/slow`, `crf15/veryslow` in the real encoder command. |
| Multiple resolutions | **VERIFIED** | Matrix 13: 1280x720, 1920x1080, 1080x1920, 1080x1350, 1080x1080 - each measured through FFprobe. |
| Two-pass encoding | **VERIFIED** | Matrix 31 + engine log: `-pass 1 -passlogfile …/pass_N` then `-pass 2 …` per segment, output valid. Refused up front without a bitrate (matrix 32). |
| CPU-only path | **VERIFIED** | No GPU present or assumed; every encode above used a CPU encoder. |
| Long-form (no 30/45/60 s cap) | **LIMITED** | Matrix 24, 35: 50 scenes → 625 s / 18,750 frames / 50 segments planned in 0.13 s, +0.0 MiB. **The full 625 s render was NOT performed.** |
| Memory / performance | **VERIFIED (measured)** | `scripts/stage_e_profile.py`; peak resident set 143.7 MiB. See §6. |
| Regression suite (Stages A-E) | **VERIFIED** | **1492 passed, 5 skipped, 0 failed** in 101.8 s (2026-10-07). |
| Manual matrix | **VERIFIED** | **35/35 scenarios passed.** |
| Documentation | **VERIFIED** | This report plus README, ARCHITECTURE, TESTING, DEVELOPMENT, PROJECT_FORMAT, ROADMAP. |
| **Kokoro-82M** | **NOT VERIFIED** | Package 0.9.4 installed; **no model weights on this machine**. See §1. |
| **Windows 10/11** | **NOT VERIFIED** | This verification ran on Linux. See §3. |

---

## 1. Kokoro verification state

> **KOKORO NOT VERIFIED - TEST FALLBACK USED**

`motion-studio voice selftest` output on this machine, verbatim:

```
KOKORO NOT VERIFIED - TEST FALLBACK USED
  reason: No Kokoro model weights were found (searched: ~/.cache/kokoro,
          ~/.cache/huggingface/hub, ~/AppData/Local/kokoro,
          ~/AppData/Local/huggingface/hub).
  to do : Download Kokoro 82M weights into the app's models folder, or set the
          Kokoro model path in Settings.
```

* Kokoro is the **only** TTS engine. There is no cloud, paid or alternate engine
  behind it, and no online lookup.
* The `kokoro` package (0.9.4) and `onnxruntime` (1.30.0) are installed here, so
  this is a **weights** gap, not a packaging gap.
* All narration used by tests and scripts is real audio on disk, labelled
  `synthetic (TEST/DEV FALLBACK - not Kokoro)` in every report line.

### The self-test is a real test, not a version check

`app/tts/selftest.py` → `verify_kokoro()`:

1. initialises the actual runtime (`KokoroEngine().load()`);
2. discovers voices from the installed model - none are hard-coded;
3. synthesises one sentence;
4. writes a real WAV and measures `frames / sample_rate`;
5. **rejects anything ≤ 0.1 s** as not-verified;
6. only then reports `KOKORO VERIFIED`.

`status_headline()` returns one of exactly two strings, so the two states cannot
be blurred:

```
KOKORO VERIFIED
KOKORO NOT VERIFIED - TEST FALLBACK USED
```

### What runs when weights are present

Both of these perform the real test, and neither was run successfully here:

```
motion-studio voice selftest
python scripts/stage_e_manual_matrix.py --narration kokoro
```

`--narration kokoro` **refuses to run** rather than quietly substituting
synthetic audio.

---

## 2. FFprobe

> **FFPROBE VERIFIED - 6.0-static, and actually used**

The previous verification pass had no `ffprobe` binary and parsed `ffmpeg -i`
instead. That is a legitimate fallback, but it is not FFprobe verification, and
this pass does not present it as such. A real FFprobe was installed and used.

Matrix 26:

```
[PASS] 26 Finished video is inspected by FFprobe itself:
        source=ffprobe label='FFprobe' video stream 10.20s
```

### The distinction is enforced in code, not in prose

`app/media/probe.py` carries two separate questions:

* `MediaInfo.ok` - could the file be read at all;
* `MediaInfo.used_ffprobe` - did the real FFprobe answer.

`MediaInfo.source_label` returns either `"FFprobe"` or
`"FFprobe fallback / limited probe (ffmpeg -i)"`. A QC report prints which one
it used (`Inspected with: …`), and when FFprobe did **not** run the report
records `ffprobe_inspection: CHECK NOT AVAILABLE` and the verdict can no longer
be a bare PASS (matrix 27).

The system check now states the consequence explicitly:

> Without FFprobe, finished videos are measured by parsing `ffmpeg -i` - a
> limited probe. … FFprobe is **REQUIRED for final sign-off**: a release must not
> claim FFprobe verification unless FFprobe actually ran.

`CheckReport.render_ready` already treats `media.ffprobe` as required.

---

## 3. Windows verification

> **WINDOWS END-TO-END VERIFICATION PENDING**
> **WINDOWS NOT VERIFIED**

Every measurement in this report was produced on **Linux** (Python 3.11.2). No
claim of Windows end-to-end verification is made, and none can be inferred from
this run.

Still to be done on a Windows 10/11 64-bit machine:

| Item | What to check |
| --- | --- |
| Application launch | Double-click the shortcut; no console window; no dependency on the current working directory. |
| Project paths | Create, save, Save As, duplicate, reopen from the recent list. |
| Temp paths | Render scratch under `%TEMP%`; cleaned after a successful render. |
| Workspace | Data root under `%LOCALAPPDATA%`. |
| FFmpeg / FFprobe | Both discovered from the bundled `tools` folder and from `PATH`. |
| Kokoro | Weights found, `voice selftest` reports `KOKORO VERIFIED`. |
| Rendering | One full render to MP4, verified by FFprobe. |
| Cancellation | Cancel mid-render; Task Manager shows no orphaned `ffmpeg.exe`. |
| Output naming | `_Video1`, `_Video2`, `_Video3`; earlier files untouched. |
| Paths with spaces | A project folder such as `C:\My Projects\Test Show`. |
| Separators | `pathlib` only; no `\` or `/` assumptions (already enforced in code and lint). |

Windows-specific code that is **already unit-tested on Linux** but not exercised
on Windows: `subprocess.CREATE_NO_WINDOW` suppression (`_no_window_flags`),
`ffprobe.exe`/`ffmpeg.exe` discovery order, and illegal-filename handling.

---

## 4. Two-pass encoding

> **VERIFIED** - real two passes, not argument construction

Before this pass the "Two-pass encoding" checkbox in the Render page was a
**dead control**: `video_encoder_args()` supported `-pass`, but both call sites
called it without `two_pass`, so the flag never reached FFmpeg. That is now
fixed and verified end to end.

Engine log from a real render (`two_pass=True`, bitrate 900 kbps):

```
RENDER_ENCODE_START | Encoding segment_0000.mp4 (pass 1 of 2)
  … -b:v 900k -pass 1 -passlogfile …/render_work/pass_0 …
RENDER_ENCODE_PASS_DONE | Analysis pass finished | stats=…/pass_0
RENDER_ENCODE_START | Encoding segment_0000.mp4 (pass 2 of 2)
  … -b:v 900k -pass 2 -passlogfile …/render_work/pass_0 …
RENDER_ENCODE_DONE | Encoded 25 frames
```

* One statistics file **per segment** (`pass_<index>`), so segments cannot
  overwrite each other's stats.
* The stats files are deleted afterwards; the test asserts no `*-0.log`
  survives.
* Pass 1 verifies that a statistics file appeared; if it did not, the render
  fails rather than pretending a two-pass encode happened.
* **Validation:** `TWO_PASS_NEEDS_BITRATE` (constant quality selected) and
  `TWO_PASS_UNSUPPORTED` (encoder without `-pass`) are raised before any frame
  is drawn. Matrix 31 (real render) and 32 (refused) both pass.
* Encoders that accept `-pass`: `libx264`, `libx265`, `libvpx-vp9`
  (`presets.TWO_PASS_ENCODERS`).

---

## 5. Long-form

> **LIMITED - timeline and planning verified; the full render was not performed**

Matrix 24 and 35:

```
[PASS] 24 A 50-scene timeline plans without a duration cap:
        50 scene(s), 10:25.0 (625s)
[PASS] 35 A 50-scene project plans without a memory climb or a length cap:
        625s / 18750 frames / 50 segments, +0.0 MiB;
        the full 625s render was NOT performed
```

* There is **no** 30/45/60-second limit anywhere in the pipeline.
* Planning 18,750 frames took 0.13 s and added no measurable resident memory,
  because segments are planned lazily and frames are streamed one at a time.
* Save/load of a many-scene project is covered (matrix 25, and
  `tests/test_stage_e_services.py`).
* **Not done:** an actual 625-second encode. On this machine that is roughly
  25 minutes of CPU time at 1280x720, and running it would not exercise any code
  path that the 12-second render does not already cover - but it is not the same
  evidence, so it is not claimed.

---

## 6. Memory and performance - measured

Produced by `scripts/stage_e_profile.py` (new). Numbers are the resident set of
the process, measured with `psutil`, at the end of each phase. 1280x720 @ 30 fps.

| Phase | RSS (MiB) | Change | Peak | Note |
| --- | --- | --- | --- | --- |
| start | 13.6 | +0.0 | 13.6 | bare interpreter |
| import | 22.8 | +9.2 | 22.8 | app modules, no Qt yet |
| tools | 22.8 | +0.0 | 22.8 | FFmpeg/FFprobe probed |
| qt | 49.3 | +26.5 | 49.3 | `QApplication` |
| gui | 116.3 | +67.0 | 116.3 | `MainWindow` built and shown |
| project | 116.3 | +0.0 | 116.3 | project created |
| scenes | 116.3 | +0.0 | 116.3 | 4 scenes added and saved |
| load | 116.6 | +0.3 | 116.6 | reopened from disk |
| preview | 122.2 | +5.7 | 139.6 | 180 raw frames produced |
| plan | 122.7 | +0.5 | 139.6 | 50 scenes / 625 s / 18,750 frames in 0.13 s |
| render | 122.8 | +0.0 | 143.7 | real render, COMPLETED in 7.5 s, QC PASS |
| done | 122.8 | +0.0 | 143.7 | end |

**Highest resident set seen: 143.7 MiB.**

What the numbers actually show:

* The GUI is the largest single cost (+67.0 MiB), not the render.
* Preview memory **peaks and falls back** (139.6 → 122.2): frames are streamed
  to the encoder, never accumulated.
* Planning a 625-second timeline adds **+0.5 MiB**. Timeline length is not a
  memory variable.
* A real render adds **+0.0 MiB** of resident memory in the Python process; the
  encoder runs in a child FFmpeg process.

This is one measurement on one machine (2 CPU, 3.8 GB RAM reported). It is
evidence, not a guarantee for another machine.

## 7. The QC contract

A QC report has exactly three verdicts - **PASS**, **WARNING**, **FAIL** - and
each individual check is exactly one of **PASS**, **FAIL** or
**CHECK NOT AVAILABLE**. Nothing else is representable.

The rule that matters: **a check that could not run is never a pass.** If any
check is unavailable, the overall verdict is at worst-case WARNING, never PASS.

The render's own report for the reference video records 18 checks (matrix 28):

```
file_exists, file_not_empty, file_readable, container_readable,
container_complete, ffprobe_inspection, video_stream, resolution, frame_rate,
duration, video_codec, audio_codec, audio_stream, av_sync,
audio_levels, silence, black_frames, subtitle_span
```

A check that simply does not apply is **absent**, not unavailable - a silent
video that nobody asked for audio on is not "under-verified"
(`test_a_check_that_does_not_apply_is_absent_not_unavailable`).

### Final output validation (directive section 9)

A render is reported successful only after all of these were looked at in the
file that was written:

file exists · size > 0 · container readable by FFmpeg · video stream present ·
expected width · expected height · expected FPS · duration within tolerance ·
expected **video codec** · audio stream present when enabled · expected **audio
codec** · audio length aligned with the picture · QC completed.

QC runs **before** the file is renamed into place, and a FAIL deletes the staged
file rather than leaving a broken take behind.

---

## 8. Bugs found and fixed by this hardening pass

These were found *after* the suite was green, which is the point of the pass.
Nine of them are real defects in the application (8.1, 8.2, 8.3, 8.8, 8.9,
8.10, 8.11, 8.12, 8.13); 8.4 and 8.5 are contract gaps, and 8.6 was a test
that checked the wrong thing.

### 8.1 A dead A/V sync check (real bug)

`QCService` compared the container duration with **itself**:

```python
duration = float(info.duration or 0.0)
if duration and info.duration and abs(duration - float(info.duration)) > 0.15:
```

`abs(x - x)` is always 0, so a video whose audio track was 4 seconds shorter
than its picture passed QC. Measured proof:

```
container duration : 6.0
video stream dur   : 6.0
audio stream dur   : 2.0
OLD check value    : 0.0   -> never fires
NEW check value    : 4.0   -> fires at > 0.15
```

Fixed to compare `info.stream_duration("video")` with
`info.stream_duration("audio")`, with `AV_LENGTH_NOT_MEASURED`
(`CHECK NOT AVAILABLE`) when the probe cannot give per-stream lengths.
Regression test: `test_a_short_audio_track_is_actually_detected`.

### 8.2 The two-pass checkbox did nothing (real bug)

See §4. Regression tests: `test_two_pass_arguments_include_the_pass_number…`,
`test_a_real_two_pass_render_through_the_engine`,
`test_two_pass_is_refused_before_any_frame_is_drawn`.

### 8.3 Rendering a project with no audio crashed (real bug)

```python
log_event("RENDER_AUDIO_EMPTY", "No audio tracks to mix", message=audio.message)
```

`log_event(event, message, **fields)` - `message` was passed both positionally
and as a keyword, raising `TypeError: log_event() got multiple values for
argument 'message'`. Any project with no narration **and** no music failed to
render with a confusing internal error instead of rendering silently, which is a
legitimate thing to want. Found by the profiler, not by a test.
Regression test: `test_a_project_with_no_audio_at_all_still_renders`.

### 8.4 QC could not say "I could not check that"

Added `NOT_AVAILABLE = "CHECK NOT AVAILABLE"`, `QCReport.checks`, and the
verdict rule in §7. Applies to: FFprobe absent, deep scans skipped, audio stats
unreadable, black-frame scan failed.

### 8.5 QC never verified the codec

A file encoded with the wrong codec passed. Added `expected_video_codec` /
`expected_audio_codec`, translated from codec ids by
`presets.stream_codec_name()` (`h264_cpu` → `h264`), because FFprobe names the
stream, not the encoder. Matrix 30.

### 8.6 The matrix checked the wrong QC report

Scenario 28 first asserted against a hand-run `QCService.check(...)` with fewer
expectations, and correctly failed. It now asserts against `result.qc` - the
report the render produced for itself, which is what directive section 9 is
about.

### 8.7 Evidence regenerated

The committed evidence files were produced with a different FFmpeg
(imageio-ffmpeg 7.0.2, no ffprobe). All of `docs/evidence/` was regenerated with
FFmpeg 6.0-static + FFprobe 6.0-static and the current code. Old files were
replaced, not kept alongside.

### 8.8 Ducking removed the wrong amount (real bug)

`app/audio/mix.py` fed `audio.ducking_level` - the music level the user *keeps*,
default 0.35 - into a filter whose parameter is the fraction *removed*, so a
setting of "25 % while speaking" ducked the bed by 25 % instead of leaving 25 %.
The control understated its own effect. Fixed by deriving the removed fraction:
`ducking.amount` when `DuckingSettings` supplies one, otherwise
`1.0 - ducking_level`, clamped to 0..1.

Measured (2 x 2.0 s scenes, silence narration so only the bed is measured, 25 %
kept level, 0.05 s attack/release, normalisation off):

```
before the fix   ducked -27.5 dB   flat -25.0 dB   ->  2.5 dB, barely audible
after  the fix   ducked            ~12 dB below flat inside a window,
                 recovering to within 1 dB of flat between scenes
filter chain     volume=volume='0.9*(1-0.25*(...))':eval=frame
```

Regression test: `test_ducking_lowers_the_music_while_the_narration_speaks`
(`tests/test_hardening_audio_captions.py`).

### 8.9 A cancelled job left its child process running (real bug)

Two faults in the same path:

* `app/image/backends/base.py` called `cancel.register(process)` /
  `cancel.unregister(process)`, but `CancelToken` names those methods
  `register_process` / `unregister_process`. The lookup failed silently, so no
  child was ever tracked. Cancelling a generation marked the job cancelled
  *after the child had already finished*, leaving a model running for as long as
  it liked in the background.
* `CancelToken.cancel()` only set a flag. Nothing stopped the work that was
  already running.

Fixed: both spellings are accepted (and `CancelToken` now exposes the short names
as aliases, so neither name can silently do nothing again), and `cancel()`
terminates the children it is tracking immediately - from the calling thread,
without blocking it, with a short-lived watchdog thread forcing anything that
ignores the request. Regression tests:
`test_a_cancelled_generation_stops_the_child_process`,
`test_a_cancelled_render_stops_its_child_processes`.

### 8.10 Caption files were written in place, and could be replaced

`write_subtitle_file` wrote directly to the target path, so a crash during the
write left a truncated `.srt`, and an export over an existing file replaced a
caption file the user may have hand-edited. It now writes through
`atomic_write_text`, and when the target exists with *different* text it writes
the next free name instead and returns the path it really used. This
application's own derived files - the captions a render regenerates into its
private work folder - pass `overwrite=True`. Regression tests:
`test_a_caption_file_is_written_atomically`,
`test_exporting_the_same_captions_twice_does_not_litter_or_replace`.

### 8.11 Output numbering counted files that were not this project's takes

The next take number was read from the trailing digits of every file in the
output folder, so one unrelated `Holiday 2024.mp4` beside the project made the
first take `Project_Video2024.mp4`. The scan now recognises only names that
match the project's own template (`name_skeleton` / `sequence_from_template`,
anchored on `{seq}`) rather than any trailing number. Regression tests:
`test_the_render_output_only_ever_gains_files`,
`test_an_unrelated_file_in_the_output_folder_does_not_derail_the_naming`,
`tests/test_hardening_render_chain.py::test_three_generate_clicks_make_exactly_project_video_1_2_3`.

### 8.12 An audio check that could not run blocked silent exports (real bug)

`AudioService.validate` ran `probe_media` on every narration file and raised
`NARRATION_UNREADABLE` - an error - when the probe failed. Without FFmpeg
available the probe fails for *every* file, so a project with valid narration was
reported as broken over something that was never inspected. Two changes:

* a check that cannot run is not reported as a failure: the narration
  readability pass is skipped entirely when there is no FFmpeg to inspect with
  (directive section 8);
* `include_audio=False` demotes every audio finding to a warning with the reason
  stated ("The export has audio turned off, so the video will have no sound."),
  so an intentionally silent export is not blocked by audio (directive 12).

Regression tests: `tests/test_audio_service.py::test_a_present_narration_passes`,
`test_rendering_with_no_audio_at_all_still_works`.

### 8.13 "Create variation" raised an AttributeError (real bug, Stage F)

`ImageService.variant_of` built a request with `GenerationMode.IMG2IMG`, which
does not exist - the constant is `GenerationMode.IMAGE_TO_IMAGE`. The variation
button in Image Studio would have raised rather than generating. Fixed;
regression test `test_a_chain_of_variations_keeps_its_lineage_after_a_restart`
(`tests/test_hardening_image_jobs.py`).

---

## 9. The blackdetect correction (retained)

Correct parameters, unchanged from the previous pass:

```
blackdetect=d=<minimum>:pix_th=0.05:pic_th=0.98
```

* `pix_th` - per-pixel luma below which a pixel counts as black.
* `pic_th` - fraction of such pixels needed before a **frame** is black.

Measured justification: a navy background has ~98.6% of pixels below luma 0.10
but only 0.12% below 0.05, while a genuinely black frame is 100% at both. At
`pix_th=0.10` the navy theme trips `pic_th=0.98`; at `0.05` it does not, and real
black still reads 100%.

Both directions are regression-tested in `tests/test_render_qc.py`:

* `test_a_black_video_is_reported` - real black is still found.
* `test_a_dark_but_not_black_video_is_not_called_black` - a dark theme is not.
* `test_a_video_with_a_picture_is_not_called_black`.

All QC evidence in this report post-dates the correction.

---

## 10. Test results

```
1492 passed, 5 skipped, 0 failed in 101.75s
```

* Stated baseline before Stage E: **830**. Actual suite today: **1497 collected**,
  **1492 passed, 5 skipped, 0 failed**.
* Stage E added `tests/test_stage_e_hardening.py` (**20 tests**: FFprobe
  detection/use/fallback, QC contract, A/V alignment, codec verification,
  two-pass, and the regressions in §8).
* Cross-stage hardening modules added in this pass:

| Module | Tests | What it covers |
| --- | --- | --- |
| `tests/test_hardening_render_chain.py` | 21 | plan → argv → real file → FFprobe; numbering; atomic caption writes; resolutions; quality. |
| `tests/test_hardening_audio_captions.py` | 21 | master mix placement, fades/trim/gain, ducking, normalisation, QC silence, broken narration, caption provenance, cue editing, timeline edge cases. |
| `tests/test_hardening_image_jobs.py` | 40 | Stage F image contract, command adapter, batches, cancellation, library/history, device probe. |

* `ruff check app tests scripts installer --select F,E9` → **All checks passed!**

The 5 skips are environmental, and each scenario is covered by another test
that fabricates the missing capability:

| Skip | Reason |
| --- | --- |
| `test_render_engine.py:312` | "this FFmpeg has every codec, so none can be shown missing" |
| `test_system_check.py:71` | "FFmpeg is installed on this machine" |
| `test_image_backends.py:100` | "standard is available on this machine" |
| `test_image_backends.py:117` | "http supports every mode" |
| `test_image_backends.py:117` | "comfyui supports every mode" |

---

## 11. Manual matrix

`scripts/stage_e_manual_matrix.py --data-root … ` → **35/35 passed.**

```
01 Timeline builds and validates: 0:10.2 over 4 scene(s); 0 error(s), 1 warning(s)
02 Scene lengths come from measured narration: 4 of 4 scene(s) measured
03 Audio validates: 4 narration placement(s), 0 error(s)
04 Master mix renders and is measurable: 10.20s pcm_s16le 48000Hz 2ch
05 Ducking changes the mix: the ducked master differs from the unducked one
06 Captions generated from narration: 4 cue(s), timing source 'narration'
07 SRT, VTT and ASS are written
08 Overlapping captions are caught: reported SUBTITLE_OVERLAP
09 First render completes: 1280x720 @ 30.0 fps, 10.20s, h264/aac, 386,612 bytes
10 QC measures the finished file: QC PASS
11 Burnt-in captions are drawn in: the burnt-in file differs from the plain one
12 Second render does not touch the first
13 Five resolutions render at exact sizes (all five verified through FFprobe)
14 Quality presets reach the encoder command
15 An explicit bitrate replaces CRF
16 A cancelled render stops cleanly
17 No FFmpeg process is left running
18 Missing narration blocks the render
19 An unavailable codec stops the render early: CODEC_UNAVAILABLE
20 An unusable output folder is reported
21 A project with no scenes is refused
22 A missing asset blocks the render: IMAGE_MISSING
23 A resume render completes: 7 segment(s)
24 A 50-scene timeline plans without a duration cap: 10:25.0 (625s)
25 The project saves and reloads intact
26 Finished video is inspected by FFprobe itself: source=ffprobe
27 QC cannot claim a clean pass when FFprobe is missing: WARNING / CHECK NOT AVAILABLE
28 The render's own QC report covers every required output check: 18 check(s), none missing
29 Audio length is aligned with the picture length: drift 0.000s
30 A wrong codec is caught rather than accepted: asked hevc, file holds h264 -> FAIL
31 Two-pass encoding runs both passes and produces a valid file
32 Two-pass with no bitrate is refused before rendering
33 Two Generate clicks make exactly two files and change nothing else
34 Cancelling a render leaves no FFmpeg process behind: 0 still alive
35 A 50-scene project plans without a memory climb or a length cap: +0.0 MiB
```

Modes: `--narration auto` (default; uses Kokoro when installed, otherwise
labelled synthetic) and `--narration kokoro` (refuses to run without Kokoro).
Synthetic audio is never presented as Kokoro output.

---

## 12. Render result and QC

`scripts/stage_e_end_to_end.py` → `docs/evidence/StageE_Test_Video1.mp4` and
`StageE_Test_Video2.mp4`.

| Property | Video1 | Video2 |
| --- | --- | --- |
| Status | COMPLETED | COMPLETED |
| Resolution (FFprobe) | 1280x720 | 1280x720 |
| Frame rate | 30.0 fps | 30.0 fps |
| Duration | 12.00 s (timeline 12.00 s) | 12.00 s |
| Video codec | h264 / yuv420p | h264 / yuv420p |
| Audio | aac, 48000 Hz, 2 ch | aac, 48000 Hz, 2 ch |
| Size | 446,514 bytes | 446,514 bytes |
| md5 | `a9741295ee358446fc192be1535087cb` | `a9741295ee358446fc192be1535087cb` |
| QC | **PASS** | **PASS** |

* Video1 was **not** modified by the second render (md5 compared before and
  after); Video2 is an independent file.
* 5 subtitle cues, 7 segments, 360 frames; renders took 13.4 s and 13.7 s
  (2026-10-07 run, FFmpeg/FFprobe 6.0-static).
* The two files are byte-identical because the same content encoded with the
  same deterministic settings produces the same output - not because one was
  copied.
* Also in `docs/evidence/`: `StageE_Matrix_1.mp4` (386,612 bytes,
  md5 `f5bdc0824042ac73b8668b90cc338313`) and `subtitles.{srt,vtt,ass}`.

### Output numbering

`{name}_Video{seq}` produced `StageE_Test_Video1.mp4` then
`StageE_Test_Video2.mp4`, and matrix 33 confirmed two consecutive Generate
actions produce exactly one file each, with the earlier file's md5 unchanged.
Existing output is never overwritten; one user action creates exactly one job.

---

## 13. Versioning

| Value | Before | After | Why |
| --- | --- | --- | --- |
| `APP_VERSION` | 0.4.0 | **0.5.0** | Stage D → Stage E. |
| `APP_STAGE` | D | **E** | |
| `PROJECT_SCHEMA_VERSION` | 3 | **3 (unchanged)** | Stage E's additions are additive with defaults; existing schema-3 files load unchanged. A cosmetic bump would force migrations for no reason. |

Schema compatibility is proven, not asserted, by `tests/test_schema_stage_e.py`:
a pre-Stage-E schema-3 dict loads with defaults filled (music fades 1.0/2.0,
ducking on at 0.35, `output_dir="renders"`), a saved project round-trips
exactly, and an unknown key survives in `extra`.

`app/project/presets.py` gained `CODEC_STREAM_NAMES`, `TWO_PASS_ENCODERS` and
`stream_codec_name()` - data and a pure function, no schema change.

---

## 14. Known limitations

Disclosed rather than hidden:

1. **Kokoro is not verified here.** No model weights on the verification
   machine. Everything else in the pipeline is verified with labelled synthetic
   narration.
2. **Windows is not verified here.** The whole pass ran on Linux. See §3 for the
   checklist that remains.
3. **The full 625-second render was not performed.** Timeline, planning,
   save/load and memory behaviour for long-form are verified; a 10-minute encode
   is not.
4. **FFprobe is required for full verification.** Without it the app still works
   through the limited `ffmpeg -i` fallback, but per-stream checks
   (`av_sync`, truncation) become `CHECK NOT AVAILABLE` and QC can no longer
   return a bare PASS.
5. **Two-pass doubles encode time** and needs a target bitrate; with constant
   quality it is refused rather than ignored.
6. **No word-level / karaoke caption timing.** Cue timing comes from measured
   narration lengths and the scene timeline. Nothing claims per-word accuracy,
   because no reliable word timing exists in this pipeline.
7. **5 skips** remain, all environmental: this FFmpeg has every codec, FFmpeg is
   installed, the standard backend is available, and `http`/`comfyui` advertise
   every mode - each "missing capability" scenario is covered by another test
   that fabricates the missing capability.
8. **Memory figures are one machine, one run.** 2 CPU, ~3.8 GB RAM reported.
   Peak 143.7 MiB is a measurement, not a promise.

---

## 15. Stage E gate

Mandatory release criteria:

| Criterion | State |
| --- | --- |
| Audio | VERIFIED |
| Subtitles | VERIFIED |
| Timeline | VERIFIED |
| Final render | VERIFIED |
| FFmpeg | VERIFIED |
| Output validation | VERIFIED |
| QC | VERIFIED |
| Cancellation | VERIFIED |
| Failure handling | VERIFIED |
| Output numbering | VERIFIED |
| Quality settings | VERIFIED |
| Multiple resolutions | VERIFIED |
| CPU-only path | VERIFIED |
| Regression suite | VERIFIED (1492 passed, 5 skipped, 0 failed) |
| Manual matrix | VERIFIED (35/35) |
| Documentation | VERIFIED |

Separately labelled, and deliberately **not** merged into the table above:

> **KOKORO NOT VERIFIED** - no model weights on this machine.
> **WINDOWS NOT VERIFIED** - this verification ran on Linux.

### Conclusion

Every mandatory Stage E criterion above is verified by a real run on this
machine. Two required-for-release items are **not** verified here, for stated
environmental reasons, and neither is claimed:

* Kokoro needs a machine with the 82M weights, then
  `motion-studio voice selftest` and `--narration kokoro`.
* Windows needs an actual Windows 10/11 machine and the §3 checklist.

**The Stage E gate is therefore not declared complete.** The pipeline is
verified; the two platform/dependency verifications remain outstanding and are
the only thing standing between the current state and sign-off.
