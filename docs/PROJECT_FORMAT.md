# Project file format

> **Status: planned for Stage B.** The project model is not implemented yet. This
> document is written now, before the code, because the format is a contract
> between the GUI, the renderer, the command line and the tests (directive
> section 6) and it is much harder to change later than to design once.

---

## 1. Principles

1. **One file, one truth.** A project is a folder containing `project.json` plus
   its assets. Every part of the application reads that one model.
2. **Versioned.** The file carries `schema_version`. A newer file is never
   silently downgraded; an older file is migrated with a logged, tested step.
3. **Readable and diff-able.** Plain JSON, stable key order, no binary blobs.
   Text is stored as text so a user can open it, and a diff shows what changed.
4. **Deterministic.** Given the same project, assets, theme, resolution and
   frame rate, rendering produces the same result. Any randomness carries an
   explicit seed that is stored in the file.
5. **Never destroyed.** Writes are atomic, a backup chain is kept, and autosave
   writes to a separate recovery file so a crash cannot corrupt the project.

---

## 2. Folder layout

```
projects/
    My Project/
        project.json            the model (this document)
        project.json.bak        previous successful save (rotating)
        autosave/
            project.autosave.json      written by the autosave timer
            project.<timestamp>.json   older autosaves (bounded)
        assets/                 images, music and effects used by this project
        audio/                  generated narration per scene
        thumbnails/             storyboard thumbnails (regenerable)
        previews/               draft/medium preview files (regenerable)
```

* `assets/`, `audio/`, `thumbnails/` and `previews/` are referenced by **relative**
  paths, so a project folder can be moved or copied to another machine.
* Only `project.json`, `assets/` and `audio/` are required to open a project;
  the rest is rebuilt on demand.

---

## 3. Shape of `project.json` (draft)

```jsonc
{
  "schema_version": 1,
  "app_version": "0.2.0",
  "id": "my-project-a1b2c3",          // stable folder id, sanitised
  "name": "My Project",
  "created_at": "2026-01-01T10:00:00Z",
  "modified_at": "2026-01-01T10:42:13Z",

  "video": {
    "width": 1920,
    "height": 1080,
    "fps": 30,
    "background": "#101014"
  },

  "theme": {
    "id": "clean-dark",
    "overrides": { "accent": "#4c8dff" }
  },

  "audio": {
    "narration": { "enabled": true, "voice": "af_heart", "speed": 1.0, "volume": 1.0 },
    "music": { "path": "assets/theme.mp3", "volume": 0.18, "ducking": true },
    "sfx": []
  },

  "subtitles": { "enabled": false, "max_lines": 2, "font_size": 44 },

  "scenes": [
    {
      "id": "scene-1",
      "type": "title",                     // title | text | image | number | cta | video | graphic
      "start": 0.0,                        // seconds, derived from narration when auto-timed
      "duration": 3.4,
      "transition_in": { "type": "fade", "duration": 0.4 },
      "transition_out": { "type": "fade", "duration": 0.4 },
      "script": "Welcome to the studio.",
      "narration": {
        "file": "audio/scene-1.wav",
        "duration": 2.6,                   // measured from the real audio, never estimated
        "voice": "af_heart",
        "speed": 1.0
      },
      "elements": [
        {
          "id": "el-1",
          "kind": "text",
          "text": "Welcome",
          "anchor": "center",
          "position": { "x": 0.5, "y": 0.42 },   // normalised 0..1, never pixels
          "size": { "mode": "relative", "value": 0.12 },
          "fit": { "auto_fit": true, "max_lines": 2, "min_scale": 0.6 },
          "color": "#ffffff",
          "animation": { "in": "fade-up", "out": "fade", "seed": 1234 }
        }
      ]
    }
  ],

  "render": {
    "quality": "final",
    "crf": 20,
    "audio_bitrate_kbps": 192,
    "container": "mp4",
    "encoder": "h264_cpu"
  },

  "random_seed": 20260101
}
```

### Why these choices

* **Normalised positions (0..1) and anchors**, never absolute pixels: this is what
  makes the same project render correctly at 1920×1080, 1080×1920, 1080×1080,
  1080×1350 and 720p without special cases (directive section 23).
* **`duration` on narration is measured**, not estimated, and is authoritative for
  automatic timing (section 27).
* **`fit` describes intent** (`auto_fit`, `max_lines`, `min_scale`) so text can
  never silently leave the frame; the renderer reports a warning before rendering
  if it cannot fit (section 24).
* **Image fitting is explicit** (`contain`/`cover`/`crop`/`center`/`anchor`) - no
  silent stretching (section 25).
* **Seeds are stored** whenever randomness is used, so a render can be repeated
  exactly (section 22).

---

## 4. Rules the code must follow (Stage B checklist)

1. `Project.from_dict` / `to_dict` are the only (de)serialisation entry points.
2. Loading validates: required fields, types, ranges, scene timing (no negative
   durations; overlaps only where allowed), unique ids, and that referenced files
   are inside the project folder.
3. A file with a **newer** `schema_version` is refused with a clear message
   instead of being partially read.
4. A damaged file is never overwritten: it is copied to `backups/` and the user is
   offered the most recent good backup and the newest autosave.
5. Saving writes atomically and keeps a rotating backup.
6. Autosave writes `autosave/project.autosave.json`; it never touches
   `project.json`.
7. On startup, a recovery file newer than the project file triggers the
   "Recovered Project Available → Restore / Ignore / Open backup" prompt. A
   recovery file is never applied automatically.
8. Undo/redo operates on project snapshots through the one model - never on
   widget state.
