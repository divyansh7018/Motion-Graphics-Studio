# Stage E render evidence

Real files produced by `scripts/stage_e_end_to_end.py` and
`scripts/stage_e_manual_matrix.py` on the Stage E test machine. They are kept
here so the numbers in `docs/STAGE_E_REPORT.md` can be checked against something
you can actually open, rather than taken on trust.

## Important: the narration in these files is not Kokoro

**KOKORO NOT VERIFIED - TEST FALLBACK USED.**

The Stage E machine has no Kokoro model weights and cannot reach Hugging Face,
so the narration track in every file here is an explicitly labelled synthetic
audio file (a tone) of a *measured* length. Everything else about these renders
is real: the timeline, the mix, the ducking, the caption timings, the scene
frames, the encode, the mux and the quality check.

What this evidence proves is the **render pipeline**. It does not prove the
Kokoro runtime path. Run `motion-studio voice selftest` on a machine with the
weights installed for that.

## The files

| File | Source | What it shows |
| --- | --- | --- |
| `StageE_Test_Video1.mp4` | `stage_e_end_to_end.py`, render 1 | The deliverable from directive section 71. |
| `StageE_Test_Video2.mp4` | `stage_e_end_to_end.py`, render 2 | Proves the second render did not overwrite the first. |
| `StageE_Matrix_1.mp4` | `stage_e_manual_matrix.py`, scenario 9 | The matrix project: image, chart, number, music, SFX. |
| `subtitles.srt` / `.vtt` / `.ass` | matrix scenario 7 | Caption side-cars from measured narration timings. |

`StageE_Test_Video1.mp4` and `StageE_Test_Video2.mp4` are byte-identical
(md5 `0fafa97a663052cfa5e4911625568d12`) because the encode is deterministic and
the project did not change between the two renders. They have different names
because the output sequence advanced past the existing file - which is exactly
the never-overwrite behaviour section 54 asks for. Identical content in two
differently named files is the expected result, not a sign that the second
render was skipped.

## Measured, not claimed

Both `StageE_Test` files, read back with the probe:

```
1280x720, 30.0 fps, 12.00s, h264 / yuv420p, aac 48000 Hz 2 ch, 446,659 bytes
QC: PASS
```

`StageE_Matrix_1.mp4`:

```
1280x720, 30.0 fps, 10.20s, h264 / yuv420p, aac 48000 Hz 2 ch, 386,921 bytes
QC: PASS
```

## Regenerating

```
LD_LIBRARY_PATH=/tmp/stublib QT_QPA_PLATFORM=offscreen \
    python scripts/stage_e_end_to_end.py --data-root /tmp/stage_e
```

Add `--narration kokoro` to the matrix script to refuse to run at all unless
real Kokoro is available, instead of falling back to labelled synthetic audio.
