# Project file format (schema v2)

**Status: implemented in Stage B.** Everything below describes what the code
actually writes today - the sample values come from a project created by
`ProjectService.create_project(...)` and read back from disk.

---

## 1. Principles

1. **One file, one truth.** A project is a folder containing `project.json` plus
   its assets. Every part of the application reads that one model; the GUI, the
   command line and the tests all go through `ProjectService`.
2. **Versioned.** The file carries `schema_version`. A newer file is refused with
   a clear message; an older file is migrated by a logged, tested step.
3. **Readable and diff-able.** Plain JSON, two-space indent, stable key order, no
   binary blobs.
4. **Deterministic.** Any randomness carries an explicit seed stored in the file
   (`project.random_seed`).
5. **Never destroyed.** Writes are atomic, a backup chain is kept, and autosave
   writes to a separate recovery file so a crash cannot corrupt the project.
6. **Portable.** Every path inside the project is relative to the project folder,
   so the folder can be copied to another Windows machine and still work.

---

## 2. Folder layout

```
projects/
    My Project/
        project.json          the project (the only source of truth)
        script.txt            the script, byte-identical to script.source_text
        .project.lock.json    advisory lock (pid + host); removed on close
        scenes/               per-scene working files (later stages)
        assets/               imported media (copied in, never linked)
        audio/                generated narration and mixes (later stages)
        generated/            regenerable intermediates - safe to delete
        previews/             regenerable previews - safe to delete
        renders/              finished output
        backups/              previous project.json versions + quarantined files
        autosave/             recovery copies (never project.json)
```

`generated/` and `previews/` are listed as regenerable, so Maintenance can clear
them without touching anything that cannot be rebuilt.

---

## 3. Top-level structure

```json
{
  "schema_version": 3,
  "application_version": "0.3.0",
  "project":    { ... },
  "format":     { ... },
  "script":     { ... },
  "voice":      { ... },
  "theme":      { ... },
  "audio":      { ... },
  "narration":  { ... },
  "scenes":     [ ... ],
  "assets":     [ ... ],
  "export":     { ... }
}
```

Unknown keys are preserved: every section keeps an `extra` dict, so a file
written by a newer build does not lose data when an older build re-saves it.

### 3.1 `project`

```json
{
  "id": "project-8f28a615",
  "name": "Documentation Sample",
  "description": "",
  "channel_id": "",
  "channel_name": "My Channel",
  "created_at": "2026-10-04T22:58:17Z",
  "modified_at": "2026-10-04T22:58:17Z",
  "project_version": 2,
  "application_version": "0.2.0",
  "template": "youtube",
  "favorite": false,
  "archived": false,
  "random_seed": 38647801
}
```

`project_version` increases on every successful save; it is what the output file
name uses and what tells the user which copy is newest. `template` records where
the project started, for information only - changing a template later never
alters an existing project.

### 3.2 `format`

```json
{
  "width": 1920, "height": 1080, "aspect_ratio": "16:9", "fps": 30,
  "quality_preset": "high", "codec": "h264_cpu", "crf": 20,
  "bitrate_kbps": 0, "encoder_preset": "slow", "pixel_format": "yuv420p",
  "keyframe_interval": 2, "audio_codec": "aac", "audio_bitrate_kbps": 192,
  "sample_rate": 48000, "container": "mp4", "background": "#101014"
}
```

* `quality_preset` is one of `draft | standard | high | ultra | custom`. The
  preset is **resolved** into the explicit fields at creation time, so the file
  always contains the real encoder settings. Selecting a preset never silently
  downgrades anything: if the requested preset cannot be honoured, validation
  reports it.
* `bitrate_kbps` of `0` means "use CRF".
* `codec` values end in `_cpu` because this build targets CPU-only machines; no
  NVIDIA/NVENC path is offered.
* Codec, container and pixel format must agree - `validate_for_render()` reports
  `CODEC_CONTAINER` / `AUDIO_CODEC` errors before a render can start.

### 3.3 `script`

```json
{
  "source_text": "",
  "notes": "Write one idea per paragraph. Each paragraph becomes a scene later.",
  "sections": [],
  "estimated_duration_seconds": 0.0,
  "words_per_minute": 150
}
```

`source_text` is the user's text, stored **byte-exact**. `script.txt` is a copy
of it - the application never reformats, re-wraps or adds a trailing newline.
`sections` (titled blocks) and the estimate are filled in by later stages.

### 3.4 `voice`

```json
{
  "engine": "kokoro", "language": "en-us", "gender": "",
  "voice": "", "speed": 1.0, "volume": 1.0, "sample_rate": 24000
}
```

An empty `voice` means "use the first voice the installed engine offers". The
interface never invents voice names: it lists only voices found in a real
catalogue (`models/voices.json`), and says so plainly when there are none.
Kokoro-82M is the only V1 engine.

### 3.5 `theme`

```json
{
  "id": "clean-dark",
  "background": "#101014",
  "accent": "#4c8dff",
  "colors": { "background": "#101014", "accent": "#4c8dff", "text": "#ffffff" },
  "typography": {
    "heading_font": "DejaVu Sans", "body_font": "DejaVu Sans",
    "heading_scale": 1.0, "body_scale": 1.0, "line_height": 1.2
  },
  "subtitle_style": {
    "enabled": false, "font": "DejaVu Sans", "font_size": 44,
    "color": "#ffffff", "outline_color": "#000000", "outline_width": 2.0,
    "max_lines": 2, "safe_area_percent": 8.0
  }
}
```

Fonts are stored by family name only. If a family is not installed on the
machine, the renderer substitutes and validation reports it - the project file
is never rewritten behind the user's back.

### 3.6 `audio`

```json
{
  "narration_enabled": true, "narration_volume": 1.0,
  "music": {
    "id": "music-1", "path": "assets/music.wav", "asset_id": "",
    "volume": 0.18, "loop": true, "fade_in": 1.0, "fade_out": 2.0,
    "start": 0.0, "end": 0.0, "trim_in": 0.0, "trim_out": 0.0,
    "mute": false, "enabled": true, "measured_duration": 0.0
  },
  "music_tracks": [],
  "ambience": { "path": "" }, "intro": { "path": "" }, "outro": { "path": "" },
  "sfx": [{
    "id": "sfx-1", "path": "assets/ping.wav", "asset_id": "",
    "volume": 0.6, "at_seconds": 0.4, "offset": 0.0,
    "anchor": "project", "scene_id": "",
    "duration": 0.0, "trim_in": 0.0, "fade_in": 0.02, "fade_out": 0.15,
    "repeat": 0, "mute": false, "enabled": true, "measured_duration": 0.0
  }],
  "master_volume": 1.0,
  "ducking_enabled": true, "ducking_level": 0.35,
  "ducking_attack": 0.25, "ducking_release": 0.75,
  "normalize_enabled": true, "target_lufs": -16.0,
  "sample_rate": 48000, "channels": 2
}
```

`music.path` and every `sfx[].path` are project-relative. Volumes are fractions
(1.0 = unchanged), and times are seconds on the project timeline.

`sfx[].anchor` says what `at_seconds` is measured from: `project` (the
timeline), `scene` (that scene's start, named by `scene_id`), or `narration`.
This lets an effect follow the picture without the user doing the arithmetic.

`music.end` of `0` means "run to the end of the video". `trim_in`/`trim_out`
choose which part of the *file* plays, so a long track can be used without
importing an edit.

The mix chain is Narration → Music → SFX → Master; `master_volume` applies
after the three have been combined. Ducking is deliberately gentle by default
(music at 35 % while the voice speaks) so it never disappears.

### 3.7 `scenes` (ordered)

```json
{
  "id": "scene-1", "name": "Intro", "type": "title", "script": "Hello.",
  "notes": "", "duration": 4.0, "enabled": true, "locked": false, "background": "",
  "transition_in":  { "type": "none", "duration": 0.0 },
  "transition_out": { "type": "none", "duration": 0.0 },
  "narration": { "file": "", "duration": 0.0, "voice": "", "speed": 1.0, "text": "" },
  "elements": [
    {
      "id": "el-1", "kind": "text", "text": "Hello", "asset_id": "",
      "anchor": "center", "position": { "x": 0.5, "y": 0.42 },
      "size": { "mode": "relative", "value": 0.095 },
      "fit": { "auto_fit": true, "max_lines": 3, "min_scale": 0.5 },
      "color": "#ffffff", "animation": { "preset": "fade up" },
      "z_index": 1, "locked": false, "extra": { "align": "center", "bold": true }
    }
  ]
}
```

Order in the array **is** the timeline order, and an element's position in the
list **is** its paint order (later = on top). A blank project has an empty
array - Stage B never creates placeholder scenes, narration or media.

Scene fields added by the scene engine (all optional; absent means the default):

| Field | Meaning |
|-------|---------|
| `enabled` | `false` drops the scene from the timeline and the final cut, but keeps it in the project so it can be re-enabled. Defaults to `true`. |
| `locked` | A safety catch: the editor refuses accidental edits. Rendering is unaffected. |
| `background` | A colour, a gradient, an asset id / file path (cover-fitted image), or `{"image": ..., "overlay": ...}`. |

Element fields:

| Field | Meaning |
|-------|---------|
| `position`, `size` | Normalised `0..1`, never pixels, so a scene is resolution independent. |
| `z_index` | Explicit stack order, kept in step with the list order by the z-order operations. `0` means "use the list order". |
| `locked` | Locked elements draw normally but the editor will not move or delete them on a stray click. |
| `kind` | `text`, `image`, `shape`, `card`, `group`, `number`, `chart`, `divider`, `progress`. |
| `animation` | `{}` = the default preset for the kind; `{"preset": "none"}` = off. Value presets `count up` and `progress fill` animate a number/bar rather than a transform. |

`type` is one of the registered scene templates (`blank`, `title`, `body`,
`image`, `stat`, `quote`, `bullets`, `chart`, `cta`, `divider`, `hook`,
`paragraph`, `list`, `counter`, `progress`, `comparison`, `before_after`,
`timeline`, `bento`, `collage`, `end_screen`, `logo`). `transition_in` /
`transition_out` `type` is one of `none`, `cut`, `fade`, `slide`, `zoom`,
`wipe`, `push`, `dip` (to black), `dip white`.

### 3.7a A scene that plays a clip (Stage G)

A clip generated by a backend, or imported from disk, becomes an ordinary scene.
Its measured facts live in the scene's ``extra["video"]`` block, written by
``app/ai/integration.py::clip_metadata`` - so the numbers come from the file
(FFprobe) and never from the request:

```json
{
  "type": "video",
  "duration": 1.0,
  "extra": {
    "video": {
      "asset_id": "asset-1a2b3c4d", "file": "clip.mp4",
      "generator": "ai", "backend": "standard_video",
      "model": "test-clip-writer", "prompt": "a harbour at dawn",
      "seed": 2468, "test_backend": true, "is_ai_model": false,
      "measured": true, "measured_with": "FFprobe",
      "width": 160, "height": 96, "duration": 1.0, "fps": 12.0,
      "frames": 12, "video_codec": "h264", "has_audio": false,
      "size_bytes": 4193, "size": "4.1 KB", "fit": "cover"
    }
  }
}
```

* ``asset_id`` is the reference; the project resolves it through the same asset
  table everything else uses, so a moved project still finds its clip.
* ``fit`` is how the renderer scales the clip into the frame; ``cover`` is the
  default and is always written, so nothing downstream has to guess.
* ``measured: false`` means the file could not be read back here (no FFprobe);
  the scene then says its length could not be measured instead of inventing one.
* ``is_ai_model`` is ``true`` only when a real model produced the clip.  A
  fixture's clip carries ``test_backend: true`` and ``is_ai_model: false``, so a
  project can never present a fixture's output as an AI result.
* Stage G adds **no new required field** to a scene and does not change the
  project schema, so schema-3 projects written before it load unchanged
  (``tests/test_schema_stage_e.py``).

### 3.8 `assets`

```json
{
  "id": "asset-9b45101e", "name": "logo.png", "kind": "image",
  "path": "assets/logo.png", "absolute_path": "",
  "size_bytes": 72, "width": 0, "height": 0, "duration": 0.0,
  "imported_at": "2026-10-04T22:45:16Z",
  "checksum": "5158ccdeb93a5397fe14d70bede56d80",
  "notes": "", "missing": false
}
```

* `path` is project-relative and preferred. `absolute_path` is only used when a
  file genuinely cannot be copied, and the interface marks it clearly;
  validation warns (`PATH_ABSOLUTE`).
* A path that escapes the project folder (`..`) is an **error** (`PATH_ESCAPES`).
* `missing` is refreshed by the asset check; a missing file is data, never a
  crash, and the interface offers Relink / Replace / Ignore.
* `checksum` lets a relinked or replaced file be recognised later.

### 3.9 `export`

```json
{
  "output_dir": "renders", "output_dir_absolute": false,
  "filename_template": "{name}_{seq}", "next_sequence_number": 1,
  "container": "mp4", "codec": "h264_cpu", "crf": 20, "bitrate_kbps": 0,
  "encoder_preset": "slow", "pixel_format": "yuv420p", "keyframe_interval": 2,
  "audio_codec": "aac", "audio_bitrate_kbps": 192, "sample_rate": 48000,
  "overwrite_policy": "never"
}
```

`filename_template` supports `{name}`, `{project}`, `{channel}`, `{seq}`,
`{version}`, `{date}`. Invalid filename characters are replaced. Output is never
overwritten: the sequence number increases until the name is free
(`overwrite_policy` is always `"never"`).

### 3.10 `narration` (added in v3)

Generated narration audio is described here, never implied by a boolean.

```json
{
  "enabled": true,
  "mode": "full_script",
  "output_dir": "audio/narration",
  "status": "ready",
  "last_generated_at": "2026-10-05T06:27:34Z",
  "last_error": "",
  "section_gap_seconds": 0.0,
  "preprocessing": {
    "collapse_spaces": true,
    "normalize_newlines": true,
    "normalize_typography": true,
    "strip_markdown": false,
    "expand_numbers": false,
    "paragraph_pauses": true
  },
  "tracks": [
    {
      "id": "track-full",
      "kind": "full",
      "section_id": "",
      "path": "audio/narration/narration_full.wav",
      "status": "ready",
      "source_hash": "46a7d125d51c7cf8",
      "settings_hash": "65bd99373f1a4380",
      "estimated_duration_seconds": 4.8,
      "actual_duration_seconds": 4.214,
      "sample_rate": 24000,
      "channels": 1,
      "size_bytes": 202328,
      "voice": "hf_alpha",
      "language": "hi",
      "speed": 1.0,
      "volume": 1.0,
      "engine": "kokoro",
      "model_version": "kokoro-82m-v1.0",
      "generated_at": "2026-10-05T06:27:34Z",
      "message": ""
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `mode` | `full_script`, `section_scene` or `selected_preview` |
| `status` | one of `not_generated`, `generating`, `ready`, `stale`, `failed`, `cancelled`, `missing` |
| `tracks[].path` | project-relative, so the project can be moved or copied |
| `tracks[].actual_duration_seconds` | measured from the written WAV; **authoritative** for later stages |
| `tracks[].estimated_duration_seconds` | word-count estimate, labelled as such in the UI |
| `tracks[].source_hash` | hash of the script text that produced the audio |
| `tracks[].settings_hash` | hash of voice, language, speed, volume, sample rate, model version and preprocessing |

**Staleness.** On load and on refresh, each track's two hashes are recomputed
from the current script and settings. A mismatch sets `status` to `stale` and the
UI offers Regenerate; audio is never silently reused. The hashes are only
comparable because the generation records every input it used back into the
project (`voice.*`, `narration.preprocessing`) - see `docs/STAGE_C_REPORT.md`.

**Missing files.** A `tracks[].path` that no longer exists sets that track to
`missing` with the message "Generated narration file is missing." plus a
Regenerate / Relink / Ignore choice. Nothing is deleted and the project still
opens.

---

### 3.11 `subtitles`

```json
{
  "enabled": false, "burn_in": false, "language": "",
  "cues": [{ "id": "cue-1", "start": 0.0, "end": 4.26, "text": "Hello there." }],
  "position": "bottom", "margin_percent": 6.0,
  "shadow": true, "background": "", "background_opacity": 0.0,
  "timing_source": "none"
}
```

Caption **content and layout** live here; caption **type** (font, size, colour,
outline, `max_lines`, `safe_area_percent`) lives in `theme.subtitle_style`. The
split is deliberate - a theme can restyle captions across a project without
touching their timings - and both are read together when captions are rendered.

`cues[].start`/`end` are seconds on the project timeline, so a caption follows
the picture whatever the frame rate or resolution.

`timing_source` records where the timings came from and is never faked:

| Value | Meaning |
| --- | --- |
| `none` | No captions generated yet. |
| `narration` | Derived from the measured narration files. |
| `manual` | The user edited a timing, split or merged a caption. |

`language` is independent of the narration language. Captions are never
translated automatically, and no word-level timing is invented: splitting a
caption divides its text at a word boundary and marks the result `manual`.

### 3.12 Codec names and two-pass data are not project state

`app/project/presets.py` gained three additions during the Stage E hardening
pass: `CODEC_STREAM_NAMES`, `TWO_PASS_ENCODERS` and `stream_codec_name()`. They
translate between what the project stores (`codec: "h264_cpu"`) and what a
finished file reports (`h264`), and list the encoders that accept `-pass`.

None of them are written into `project.json`. They are code-level tables, so
**`PROJECT_SCHEMA_VERSION` stays at 3** — a schema bump would force migrations
on every existing project for no change in what is stored.

### 3.13 Images in a project

Images do **not** get a new section. A picture sent from Image Studio becomes an
ordinary :class:`AssetSpec` and its scene reference is an ordinary
:class:`ElementSpec` with ``kind="image"`` and ``asset_id`` set:

```json
"assets": [
  {
    "id": "asset-1a2b3c4d",
    "name": "hero banner",
    "kind": "image",
    "path": "assets/hero banner.png",
    "size_bytes": 18244,
    "width": 1280,
    "height": 720,
    "notes": "Prompt: a blue banner | Model: command-model | Seed: 1234"
  }
],
"scenes": [
  {
    "background": "asset-9f8e7d6c",
    "elements": [
      {"id": "el-...", "kind": "image", "asset_id": "asset-1a2b3c4d",
       "position": {"x": 0.5, "y": 0.5},
       "size": {"mode": "relative", "value": 0.45}}
    ]
  }
]
```

Why this shape:

* **`path` is project-relative** (``assets/...``), so the project survives being
  moved or copied to another machine. An absolute path is stored only when the
  user explicitly references a file in place, and validation flags it.
* **`asset_id` is the reference**, not a path, so renaming or moving the file
  inside the project cannot break the scene, and the same picture used twice
  stays one asset.
* **`scene.background` may hold an asset id** as well as a colour or gradient.
  `app/scene/compose.py:_background_parts()` resolves an id through the project's
  asset table and falls back to treating the value as a colour, so an older
  project renders exactly as it did before.
* **`notes` carries the generation facts** (prompt, model, seed, resolution,
  origin). They are prose rather than new fields on purpose: an asset imported
  from a camera roll has no seed, and inventing one would be worse than storing
  nothing.

The image's own record - the full prompt, seed, steps, lineage and edit history -
lives beside the image file, not in ``project.json``:

```
assets/hero banner.png          the picture
assets/hero banner.png.json     its record (side-car JSON)
```

A PNG also carries the generation fields inside its own text chunks, so an image
copied somewhere on its own still knows its prompt and seed. Losing a side-car
loses the edit history, not the picture.

**No schema change was needed**, so ``PROJECT_SCHEMA_VERSION`` stays at **3**.

## 4. Versioning and migration

| Situation | Behaviour |
|---|---|
| `schema_version == 3` | opened directly |
| `schema_version == 2` | migrated by `migrate_project_data()`, logged as `PROJECT_MIGRATION`, then validated again |
| `schema_version == 1` | migrated through v2, then validated again |
| `schema_version > 3` | **refused** with `ProjectVersionError`: "created by a newer version" |
| missing `schema_version` | treated as v1 and migrated |
| unparsable JSON | not opened; the damaged file is copied to `backups/*.corrupt-<stamp>` and a friendly error is shown |

The v1 → v2 step moves the flat v1 keys into the section layout, fills defaults
for the new sections and keeps anything unrecognised in `extra`.

The v2 → v3 step adds the `narration` section (see 3.8) and leaves every other
section untouched. Existing v2 projects open unchanged and are written back as
v3 on the next save.

`application_version` records which build wrote the file. It is informational;
compatibility is decided by `schema_version` alone.

---

## 5. Saving

`ProjectStore.save()`:

1. validates first - a project with errors is **not** written;
2. writes `backups/.pending-save.json` (an interrupted-save marker);
3. backs the current `project.json` up to `backups/project_<date>_<n>.json`;
4. atomically writes the new `project.json` (temp file + `os.replace`);
5. writes `script.txt` byte-exactly when there is script text;
6. removes the pending marker;
7. prunes backups and autosaves to the configured retention (default 10 each).

A save that fails part-way (disk full, OneDrive lock, antivirus) is caught and
reported as "not saved" - the previous `project.json` stays intact.

**Autosave** never touches `project.json`. It writes `autosave/` and only when
the project actually changed. On the next start, `scan_recovery()` compares
content (never timestamps) and offers **Restore / Open original / Ignore**.

---

## 6. What is *not* in the file

* No absolute paths except the explicitly marked `absolute_path` case.
* No caches, no thumbnails, no render output. Thumbnails live in the
  application cache; renders live in `renders/`.
* No secrets, no accounts, no licence or activation data - there is no
  licensing, cloud or subscription feature in this build.
