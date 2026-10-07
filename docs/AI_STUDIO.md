# AI Studio

The AI Studio is where this application talks to local AI. It is a **local-first,
CPU-first, offline-capable** studio: nothing is generated on a server, nothing is
uploaded, and nothing is ever called "AI" unless a model really ran.

Read this together with:

* `docs/AI_BACKENDS.md` - the backend types and the manager
* `docs/AI_MODELS.md` - models, discovery and the download policy
* `docs/IMAGE_GENERATION.md` - the image half (Stage F's engine, reused)
* `docs/VIDEO_GENERATION.md` - the video half and the video library
* `docs/STAGE_G_REPORT.md` - what was actually verified on the build machine

---

## 1. Where it is

| Page | What it is for |
|---|---|
| **AI Studio** | Backends, models, the generation queue, image and video generation, storyboards |
| **Image Studio** | Importing, editing, upscaling and organising stills |
| **Video library** | Every clip the studio knows: generated, rendered and imported |

The Video library sits under **PRODUCE**, between Timeline and Render, because a
clip is an asset: it goes to a scene, to the timeline and into the final video
like anything else.

---

## 2. The one rule that shapes the whole page

> A capability is only described as verified when something actually ran and
> produced a result.

That rule shows up everywhere:

* **States are exact.** A backend is `AVAILABLE`, `NOT_INSTALLED`,
  `NOT_VERIFIED`, `NOT_SUPPORTED` or `LIMITED` - never "probably fine".
* **Only a deep check earns VERIFIED.** A light check reads metadata. A deep
  check initialises the backend and produces a real file; only then may the
  report say `VERIFIED`.
* **Test backends are labelled everywhere.** The built-in clip writer appears as
  `TEST BACKEND (fixture - not an AI model)` next to every clip it makes, in the
  queue, in the library, in the history and in the command line. Its metadata
  records `test_backend`.
* **No model installed is a state, not an error.** The application says *"AI
  model not installed"* and does not create a placeholder image or video.
* **Nothing is switched behind your back.** If the chosen backend, model, seed,
  size or mode cannot be honoured, the job fails or is refused, and you choose:
  Retry, Choose backend, Choose model, or Cancel.
* **The endpoint is always visible.** An HTTP backend shows the host and port it
  talks to. There are no other network calls.

---

## 3. The screens

### 3.1 Backends

Every registered backend with its id, kind, version, capabilities, device
requirement, state and the sentence that explains the state. Buttons:

* **Refresh** - re-run the detection pass (no model is loaded).
* **Test (light)** - metadata check: is the program there, is the folder
  readable, is the port open.
* **Test (deep)** - really initialise and generate one small result. This is the
  only path that can report `VERIFIED`.
* **Enable / disable** - a disabled backend is never chosen automatically.
* **Settings** - the schema the backend publishes (host, port, endpoint, command,
  model folder, timeout...).
* **Logs** - the last events for that backend.

### 3.2 Models

Models offered by usable backends, models found on disk, favourites, and the
project default for each kind. **Loading is lazy**: nothing is loaded to look at
this list, and a large model is never loaded automatically (§`AI_MODELS.md`).

### 3.3 The queue

One row per job: what it is, which backend and model, the kind, the progress, the
elapsed time, the estimated time left, and the result. Controls: **Cancel**,
**Retry**, **Show result**, **Open the result's folder**, **Logs**, **Clear
finished**.

* One press of Generate = **one job**. A duplicate press while that job is
  running is refused by name ("already running as job 4f3c1a2b"), not queued
  silently.
* A batch is only ever started by the explicit **Generate batch** button.
* Cancel really cancels: the child process is terminated, and the queue stops
  saying "running".
* **Retry** re-runs the *recorded* request - same backend, same model, same
  seed, same size, same mode. It is offered for a job that **failed or was
  cancelled**; a finished job is not re-run into a duplicate. If the original
  backend or model is not available any more, the retry is **refused** with the
  reason, and nothing is substituted.
* Cancelling a batch keeps every clip that was already finished.

### 3.4 Generate

Prompts are yours. The page shows exactly what will be sent (prompt, negative
prompt, mode, backend, model, width, height, fps, duration, seed) before you
press the button, and a storyboard sends **nothing** until you approve its plan.

---

## 4. What happens to a result

1. The backend writes a file into the output folder.
2. The file is **read back and measured** - exists, non-zero, readable,
   dimensions, frames, fps, duration, codec. A file that fails this is `FAILED`,
   not "completed with a warning".
3. Measured facts and the full request (prompt, seed, backend, model, settings)
   are recorded in the history, and the clip is indexed in the Video library.
4. From the result you can **Send to project**, **Send to scene** or **Send to
   timeline**, open the folder, or delete it - and the library entry keeps its
   provenance, so a clip always says who made it.

Nothing is overwritten: a second run with the same name becomes
`clip_1.mp4`, and the first file is left exactly as it was.

---

## 5. When something is wrong

Every failure answers three questions: **what happened**, **why**, and **what to
do**. Examples taken from this build:

| Situation | What the application says |
|---|---|
| No model installed | "AI model not installed. Nothing will be generated until you install one: a local video model for Diffusers, a ComfyUI workflow, your own command, or a local endpoint." |
| Test backend chosen | "TEST BACKEND (fixture - not an AI model) - it writes a real file with FFmpeg and does not use a model." |
| Backend gone at retry time | "'removed_backend' is not available now, so the same clip cannot be made again. Choose a backend instead." |
| Output too small | "broken.mp4 is only 19 bytes, which is too small to hold a video." |
| File moved | The entry is marked so you can find it again, instead of vanishing from the list. |
| No FFmpeg | The clip is listed as **not measured here** - never as broken. |
| Cannot play video in-app | "This machine cannot play video inside the application (the media component is not available). The file itself is fine: open it in your usual player, or open the folder and play it from there." |

The log file is always named, and every step is an `EVENT=` line.

---

## 6. The command line

The command line runs the **same services** as the pages; there is no second
implementation.

```
mgs ai backends                  # every backend, its state and what it needs
mgs ai models                    # models offered, and what is on disk
mgs ai check --backend <id>      # light check
mgs ai check --backend <id> --deep
mgs ai selftest --kind video     # deep check every usable backend
mgs ai job status [<job id>]     # the queue as text or JSON
mgs ai video ...                 # generate a clip
mgs ai image ...                 # Stage F's still generator
mgs ai storyboard <project>      # build, approve and run a storyboard plan
mgs ai library list|inspect|thumbnail|scan|add|recheck|remove|thumbnails
```

`mgs ai library list --json` prints the library as JSON for a script, and
`--search`, `--source`, `--sort`, `--tag`, `--collection`, `--favourites` and
`--missing` filter it the same way the page filters it.

---

## 7. What this build does not do

* **Cloud generation.** The cloud adapter is architecture, not a feature: it
  reports `NOT SUPPORTED` in this local-first build.
* **Automatic downloads.** Nothing is fetched from the internet unless you press
  the download button and confirm the size and the destination
  (§`AI_MODELS.md`).
* **Sanitised claims.** There is no path that reports `VERIFIED` without a real
  model initialisation and a real result.
* **Background removal, a real video model, and in-application playback** are
  not installed on the build machine. Every one of those says so, in those
  words, where it is used.
