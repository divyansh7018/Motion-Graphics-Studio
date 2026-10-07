# AI models

This application runs models, but it never hunts for them, never downloads one
behind your back, and never pretends to have one. This page describes what a
model is here, how models are found, what the Model Manager does, and why the
download policy is what it is.

See also: `docs/AI_BACKENDS.md`, `docs/AI_STUDIO.md`.

---

## 1. What counts as a model

A **model** is a set of weights a backend can load. Three things are *not*
models, and the application says so everywhere:

| Thing | What it is | Label used |
|---|---|---|
| The built-in image backend (`standard`) | code that composes, resizes and varies pictures | *"Built in. Needs no model."* |
| The built-in clip writer (`standard_video`) | code that writes a real MP4 with FFmpeg | `TEST BACKEND (fixture - not an AI model)` |
| A local command you configured | whatever your program does | *"your own command"* |

The JSON every backend publishes carries `is_ai_model`, and it is `True` **only**
when something recorded that a real model ran. A clip made by a fixture is
recorded with `is_ai_model: false`; the model listing says *"not an AI model"*.

---

## 2. Finding models (discovery)

Discovery looks in:

1. the model folders configured in Settings (and the project's own folder),
2. the folders the *backends* declare (a Diffusers cache, a ComfyUI
   `models/` directory),
3. nothing else - no environment scan, no network call, no `~/.cache` sweep
   outside the configured roots.

It reports what it found **without loading it**: file names, sizes, and the
suffixes each backend understands. A file that is present but unreadable is
listed as **found but not loadable**, with the reason.

On the Stage G build machine this finds no model file at all, and the report
says exactly that: *"no AI model is installed on this machine"*. Nothing is
invented to fill the gap.

---

## 3. The Model Manager

* **List** - offered models and models found on disk, with size, path, kind and
  the requirement (`CPU_SUPPORTED`, `GPU_RECOMMENDED`, ...).
* **Favourites** - the ones you use, pinned to the top of the picker.
* **Project defaults** - a project can record which backend and model its AI
  work should use. Changing the default never changes a clip that already
  exists.
* **Lazy loading only** - a model is loaded when a generation needs it and not
  before. Opening the page, listing models, checking a backend and rendering a
  video load nothing.
* **Reuse and unload** - a loaded model is reused for the next job of the same
  backend (so a second generation does not pay the load cost again), and
  **Unload model** releases it immediately. Unloading is always available, and
  the page never unloads something you are using without saying so.
* **Never auto-load a large model.** A model whose declared requirement is
  `GPU_REQUIRED`, or whose weights are large, is never loaded by a background
  task; the user starts it.

---

## 4. The download manager (explicit action only)

Some models are large enough that fetching one is a decision, not a side effect.

* There is **no automatic download**. Nothing is fetched while the application
  starts, while the page opens, or while a check runs.
* A download only starts when the user presses the button for that model, and
  only after the dialog has shown the **size** and the **destination**.
* Progress is real (bytes received), it can be cancelled, and a cancelled or
  failed download leaves no half-file behind - a partial file is written to
  `.part` and removed on failure.
* **Checksums** are verified when the source publishes one; a mismatch fails the
  download and says so.
* Only model **data** is fetched from a model host. No download ever installs an
  executable, and no script from a download is run.
* Every download is logged (`EVENT=MODEL_DOWNLOAD_...`), with the URL, the size
  and the result.

This build ships the policy and the interface; on a machine with no configured
model host there is nothing to download, and the page says so rather than
offering a button that cannot work.

---

## 5. Licences and attribution

Every backend and every discovered model can carry a licence string and a
homepage. They appear in:

* the backend list (and `mgs ai backends`),
* the model list,
* the project's AI notes, and
* the licence record the user is expected to keep with a published video.

Nothing in this application grants a licence for a model: it records what was
declared so the user can check it themselves.

---

## 6. Reproducibility, and refusing to guess

A generated file's metadata records the backend, the model, the prompt, the seed
and the settings. Reproduction means **the same backend and the same model**:

* if the backend is not available now, or the model is no longer installed, the
  request is **refused** with the reason - the application will not swap in a
  different model "to be helpful";
* the user then chooses: install/configure the original, or generate something
  new with a different backend or model (which is a new result, and is labelled
  as one).

The same rule governs the queue's **Retry**: it repeats the recorded request, and
refuses when it cannot repeat it exactly.

---

## 7. Honest states on a machine with no model

A fresh install, with no model and no AI package, looks like this - and this is a
supported, working state, not an error:

```
standard        AVAILABLE       Built in. Needs no model.
standard_video  AVAILABLE       TEST BACKEND (fixture - not an AI model)
diffusers       NOT INSTALLED   The 'Diffusers' package is not installed.
comfyui         NOT INSTALLED   No ComfyUI address has been configured.
http_video      NOT INSTALLED   No local video endpoint has been configured.
cloud           NOT SUPPORTED   This build is local-first and offline-capable.
```

Editing images, importing clips, measuring them, making thumbnails, building
scenes, rendering and publishing all work in that state. Only the generation
buttons that need a model refuse - and they say which model to install.
