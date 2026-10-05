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
  "music": { "path": "", "volume": 0.18, "loop": true, "fade_in": 1.0, "fade_out": 2.0 },
  "sfx": [],
  "ducking_enabled": true, "ducking_level": 0.35,
  "normalize_enabled": true, "target_lufs": -16.0, "sample_rate": 48000
}
```

`music.path` and every `sfx[].path` are project-relative. Stage B stores these
settings; the mixing itself arrives in Stage E.

### 3.7 `scenes` (ordered)

```json
{
  "id": "scene-1", "name": "Intro", "type": "title", "script": "Hello.",
  "notes": "", "duration": 4.0, "background": "",
  "transition_in":  { "type": "none", "duration": 0.0 },
  "transition_out": { "type": "none", "duration": 0.0 },
  "narration": { "file": "", "duration": 0.0, "voice": "", "speed": 1.0, "text": "" },
  "elements": []
}
```

Order in the array **is** the timeline order. A blank project has an empty
array - Stage B never creates placeholder scenes, narration or media.

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
