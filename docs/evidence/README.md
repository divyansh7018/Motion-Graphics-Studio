# Stage E render evidence

Real files produced by `scripts/stage_e_end_to_end.py` and
`scripts/stage_e_manual_matrix.py` on the Stage E verification machine. They are
kept here so the numbers in `docs/STAGE_E_REPORT.md` can be checked against
something you can actually open, rather than taken on trust.

These files were **regenerated** during the final Stage E hardening pass. The
previous copies were produced with a different FFmpeg build (7.0.2, no FFprobe)
and predated three bug fixes, so they were replaced rather than kept alongside -
stale evidence is worse than none.

## Measured with FFprobe, not with a fallback

The toolchain that produced and measured these files:

```
ffmpeg  6.0-static  (libx264, libx265, libvpx-vp9, libaom-av1, libass)
ffprobe 6.0-static
```

Every duration, resolution, frame rate, codec and stream length quoted here came
from FFprobe itself (`MediaInfo.source == "ffprobe"`), not from parsing
`ffmpeg -i`. QC records which tool it used, and refuses a bare `PASS` when
FFprobe did not run.

## Important: the narration in these files is not Kokoro

**KOKORO NOT VERIFIED - TEST FALLBACK USED.**

The verification machine has the `kokoro` package (0.9.4) installed but no model
weights, so the narration track in every file here is an explicitly labelled
synthetic audio file (a tone) of a *measured* length. Everything else about
these renders is real: the timeline, the mix, the ducking, the caption timings,
the scene frames, the encode, the mux and the quality check.

What this evidence proves is the **render pipeline**. It does not prove the
Kokoro runtime path. Run `motion-studio voice selftest` on a machine with the
weights installed for that.

## Important: this is not Windows evidence

**WINDOWS NOT VERIFIED.** These renders were produced on Linux. Nothing here
demonstrates Windows paths, `%TEMP%` handling, shortcut independence or
`ffmpeg.exe` discovery. See `docs/STAGE_E_REPORT.md` §3.

## The files

| File | Source | What it shows |
| --- | --- | --- |
| `StageE_Test_Video1.mp4` | `stage_e_end_to_end.py`, render 1 | The deliverable from directive section 71. |
| `StageE_Test_Video2.mp4` | `stage_e_end_to_end.py`, render 2 | Proves the second render did not overwrite the first. |
| `StageE_Matrix_1.mp4` | `stage_e_manual_matrix.py`, scenario 9 | The matrix project: image, chart, number, music, SFX. |
| `subtitles.srt` / `.vtt` / `.ass` | `SubtitleService.export` on the `StageE_Test` project | Caption side-cars from measured narration timings (5 cues). |

## Measured, not claimed

Both `StageE_Test` files, read back with FFprobe:

```
1280x720, 30.0 fps, 12.00s, h264 / yuv420p, aac 48000 Hz 2 ch, 446,683 bytes
QC: PASS
md5 fbc999135ee51c637a1f6152ef37f301   (both files)
```

`StageE_Matrix_1.mp4`:

```
1280x720, 30.0 fps, 10.20s, h264 / yuv420p, aac 48000 Hz 2 ch, 386,862 bytes
QC: PASS
md5 4a88eea1569f96ffdf9f6ff75bcf250f
```

`StageE_Test_Video1.mp4` and `StageE_Test_Video2.mp4` are byte-identical because
the encode is deterministic and the project did not change between the two
renders. They have different names because the output sequence advanced past the
existing file - which is exactly the never-overwrite behaviour section 54 asks
for. Identical content in two differently named files is the expected result,
not a sign that the second render was skipped: the end-to-end script hashes
Video1 before and after the second render and asserts it did not change.

## Regenerating

```
python scripts/stage_e_end_to_end.py --data-root /tmp/stage_e
python scripts/stage_e_manual_matrix.py --data-root /tmp/mgs_stage_e
python scripts/stage_e_profile.py --data-root /tmp/mgs_profile
```

On Linux without a display, prefix with
`LD_LIBRARY_PATH=/tmp/stublib QT_QPA_PLATFORM=offscreen`.

Add `--narration kokoro` to the matrix script to refuse to run at all unless
real Kokoro is available, instead of falling back to labelled synthetic audio.
