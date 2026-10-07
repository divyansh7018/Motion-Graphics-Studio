# Video generation and the video library

Stage G adds the video half of the AI Studio: five ways to make a clip, one
queue for all of them, a library that knows every clip the studio has, and a
"Send to" path that treats a generated clip exactly like any other asset.

Read with: `docs/AI_STUDIO.md`, `docs/AI_BACKENDS.md`, `docs/STAGE_G_REPORT.md`.

---

## 1. The five modes

| Mode | What it needs | What it does |
|---|---|---|
| **Text to video** | a prompt | makes a clip from the prompt and the seed |
| **Image to video** | a source image | animates a still: pan, tilt, zoom, dolly, orbit, handheld - the source is copied, never changed |
| **Video to video** | a source clip | re-grades or restyles a clip, keeping its timing exactly |
| **Extend** | a clip | keeps the original frames and continues after them |
| **Storyboard to video** | a project with scenes | builds **one prompt per scene**, you review and approve them, then one clip per scene |

A request the chosen backend cannot honour is refused **before** anything runs,
with the reason and the suggestion ("this backend makes clips from text only:
pick a source image or another backend"). Nothing is silently downgraded: if the
mode, size, duration, fps or seed cannot be delivered, the clip that comes back
is checked against the request and the **mismatch is shown** ("asked for 8 s, the
model produced 4.0 s").

---

## 2. Everything a clip knows about itself

A clip in this studio is not a loose file. Each one carries:

* the file's measured facts - resolution, duration, frames, fps, video and audio
  codec, container, size, checksum, and who measured it (FFprobe),
* **provenance** - generated / render / imported, and for a generated clip the
  backend, model, prompt, seed, parameters and the date,
* what it was made from (`parent`), the project and scene it belongs to,
* your own labels: name, tags, collection, favourite.

A clip that came from a fixture says so, in the same field: `TEST BACKEND
(fixture - not an AI model)`. A clip that came from a render says *"Rendered from
the project"*, and is never called a generation.

---

## 3. Variations and extending without loss

* A **variation** is a new file with the original recorded as its parent. The
  family is browsable, and the original is untouched.
* **Extend** writes a new, longer clip: the source keeps its exact frames and
  timing, and only the new part is generated.
* The output folder is never overwritten: a second `harbour.mp4` becomes
  `harbour_1.mp4`, and the tests compare the first file's bytes before and after
  to prove nothing moved.

---

## 4. The Video library

Every clip the studio knows about is indexed in one place - the studio's own
`videos` folder - with an index file (`videos.json`) and a probe cache.

* **Scan** indexes a folder (the library folder by default) and measures what is
  new. Measuring costs an FFprobe call, and the result is cached against the
  file's size and modification time, so a rescan is cheap and a *changed* file is
  measured again.
* **A file that has gone is marked, not dropped.** The entry says the file is
  missing so you can find it again; if it comes back, it is `READY` again.
* **A file that cannot be read is `UNREADABLE`** - and a file that cannot be
  *measured here* (no FFprobe on this machine) is **not** called broken: it stays
  `READY` with "not measured here", because the clip is fine and the check is
  what is missing.
* **Search, filter, sort and page**: name, prompt, model, tag, collection,
  source (generated / render / imported), favourites, missing-only; sort by
  newest, oldest, name, duration, size or resolution; paging reports the total
  and whether there is more.
* **Pictures**: one frame per clip, made with FFmpeg, cached on disk and keyed to
  the file, so a replaced clip never shows the old picture.
* **Remove** forgets the entry and leaves the file where it is. Deleting the file
  itself is a separate, explicit button.

The same library is reachable from the command line:

```
mgs ai library list --search harbour --source generated --sort duration
mgs ai library inspect <id|path|name>
mgs ai library thumbnail <id> [--again]
mgs ai library scan [folder] [--no-measure]
mgs ai library add <file> [--source generated|render|imported] [--name "…"]
mgs ai library recheck <id>
mgs ai library remove <id> [--delete-file]
mgs ai library thumbnails
```

---

## 5. Playback, honestly

Playing video inside the application needs Qt's multimedia module, which needs a
system media stack. When that is not available - a stripped build, a missing
system library, a headless machine - the page says so:

> This machine cannot play video inside the application (the media component is
> not available). The file itself is fine: open it in your usual player, or open
> the folder and play it from there.

There are always two working alternatives: **Open in the system player** and
**Show in the folder**. The application never decodes video itself - it already
has FFmpeg for rendering and QC, and a second decoder would be one more thing to
disagree with it.

**On the build machine QtMultimedia is not available**, so in-application
playback is `NOT VERIFIED` here (§`STAGE_REPORT`).

---

## 6. Sending a clip into the project

| Button | What it does |
|---|---|
| **Send to project** | copies the clip into `assets/` and indexes it as a video asset |
| **Send to scene** | adds a scene that plays the clip, carrying its real duration and fps, `fit: cover` |
| **Send to timeline** | places the clip in the scene list at the end (or at a chosen position) |

All three record the clip's origin in the project, so a scene can say where its
picture came from. The project references the asset **by id and relative path**,
so a moved project still finds it, and a missing file is reported rather than
guessed at.

---

## 7. What is not verified here

* **A real AI video model.** None is installed on the build machine. The five
  modes were exercised through the labelled fixture, which writes real MP4 files
  and real measurements but does not understand a prompt.
* **Diffusers, ComfyUI, HTTP, ONNX and local-command video generation.** The
  adapters exist, refuse honestly with no endpoint/model configured, and stop at
  the model-loading boundary.
* **In-application playback** (QtMultimedia unavailable here).
* **Windows** (§carried from Stage E).

None of those is counted as a pass anywhere: see `docs/STAGE_G_REPORT.md`.
