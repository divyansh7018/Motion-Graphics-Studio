# Image Studio

Create, import, edit, generate and organise images, and put them into a project
scene. **It works with no image model installed** - importing, editing,
upscaling, masking, organising and sending to a scene all run without one, and
that is the normal state of a fresh install.

---

## 1. Opening it

* In the application: **Image Studio** in the sidebar, under *Create*.
* From a terminal: `motion-studio image backends` (and the other `image`
  sub-commands listed in §7).

The first thing the page does is look for backends. That detection is cheap and
loads nothing (§3), so it is safe to open the page whenever you like.

---

## 2. The screen

**Left** - the mode, the prompts, the backend, the model and the settings.

**Centre** - a large preview, and the edit panel.

**Right** - the generation history and the details of whatever is selected.

### Modes

| Mode | What it needs | What it does |
|---|---|---|
| Text to Image | a prompt, and a backend that can draw one | generates from the prompt |
| Image to Image | a source image | restyles the source |
| Inpaint | a source image and a mask | fills the masked area |
| Outpaint | a source image, and at least one side to extend | grows the canvas |
| Variation | a source image | derives a new image, leaving the original alone |
| Upscale | a source image | enlarges it (§5) |

A mode the selected backend does not support is **disabled with the reason
shown**. Nothing is switched for you: if the model you chose cannot do the job,
you are told, and you choose again.

### Beginner and advanced

Beginner shows prompt, backend, model, size and Generate. **Advanced settings**
adds the seed, steps, guidance, batch count and the source image. Controls a
backend does not implement are disabled rather than hidden, so you can see what
the backend is and is not capable of.

### Sizes

1:1, 16:9, 9:16, 4:5, 4:3, 3:4 and Custom, with 512 through 1920 presets. A
request the model cannot make is **reported, never adjusted**: the size you typed
stays in the box while you decide what to do.

---

## 3. Backends and models

Detection reports what is on the machine, per backend, with the reason and the
fix. A backend that is not usable is still *listed*, greyed out, with its
explanation on the tooltip - hiding it would leave you wondering why a feature
is missing.

Detection loads **no** model, and since Stage F it also does not import torch
(which costs about 500 MB of memory just to answer "is there a GPU?"). Use
`motion-studio image backends --deep` when you specifically want the torch probe.

See `IMAGE_BACKENDS.md` for the six adapters and what each reports.

---

## 4. Generating

1. Choose the mode and the backend.
2. Type a prompt (only *Text to Image* requires one).
3. Choose a size, and a seed if you want to reproduce the result.
4. **Generate**.

`Seed 0` means *pick one at random*, and the seed actually used is shown in the
status line and stored with the image, so you can come back to it later.

**Regenerate** replays the selected history entry, including its seed.
**Variation** makes a new image from the selected one and never touches the
original.

### Batches

The batch field is an explicit action: `Batch = 4` produces four images, each
with its own seed, its own file name and its own history entry. A batch is never
started by accident, and a second batch never overwrites the first.

---

## 5. Upscaling

Two operations that are never confused with each other:

| | Availability | What it does |
|---|---|---|
| **Standard Resize** | always | a high-quality Lanczos resample; enlarges, invents no detail |
| **AI Upscale** | only when a local upscaler is installed | runs the real model |

Asking for an AI upscale with no upscaler installed is **refused with an
explanation** - it is not quietly turned into a resize and called AI. Where a
fallback is explicitly permitted, the result says so ("no AI upscaler is
installed, so a standard resize was used").

---

## 6. Editing

The edit panel works on a **list of operations**, not on a modified file:

* crop, resize, rotate, flip
* brightness, contrast, saturation, exposure, colour temperature
* sharpen, blur, grayscale
* opacity, canvas size, rounded corners
* **mask** - cut out with a mask image, with a feather radius

Edits are replayed over the original each time, which is what makes **Reset**
possible without a copy of the original. Nothing is written until you press
**Save as new image**, and the original is never modified.

Overwriting an original is a separate action that has to be confirmed
(`apply_to_source(..., confirmed=True)`), and it keeps a `.bak` copy first,
because "are you sure?" is not the same as "this cannot be undone".

---

## 7. The command line

```
motion-studio image backends                 # what is installed, and why not
motion-studio image backends --deep          # also probe torch for a GPU
motion-studio image generate --prompt "..." --width 512 --height 512
motion-studio image info <file>              # size, format and recorded metadata
motion-studio image library --scan --search "fox"
motion-studio image upscale <file> --scale 2
motion-studio image import <source> <destination>
motion-studio image edit <file> --op brightness=amount:1.2 --output <new>
motion-studio image history
motion-studio image send <file> --project <folder> --placement background
motion-studio image background                # is background removal installed?
```

Every command uses the same services the page does, so the terminal and the
window can never disagree about what a backend supports.

---

## 8. The library

Images live in the data folder's `images/` directory (never inside a project
unless you send one there). The library:

* indexes the folder and reloads the index without reopening files;
* **pages** results, so a folder of thousands does not freeze the interface;
* searches by name, prompt, model, collection, project and tags;
* reports **duplicates** by size and dimensions (or checksums on request) and
  never deletes anything by itself;
* keeps user-defined collections and tags - the presets offered
  (Backgrounds, Characters, Logos, …) are suggestions, not a fixed taxonomy.

An image that has been **deleted from disk keeps its index entry, marked
missing**, so you are told your image has gone instead of finding it silently
absent.

### Thumbnails

Thumbnails are cached outside the library, keyed on the source's size and
modification time - so replacing an image invalidates its thumbnail rather than
showing the old one. They are built in the background, and a transparent image's
thumbnail is composited onto a solid square so it is visible at all.

---

## 9. History and prompts

Every generation is recorded with its seed and settings; failures are recorded
too, with the reason. From a history row you can regenerate, make a variation,
send the image to a project or a scene, or read its metadata.

Prompts are remembered separately: recents, favourites and named saves. A prompt
is **never** edited automatically - only by an explicit save, rename or delete.

---

## 10. Metadata and versions

Each image carries a record: prompt, negative prompt, model, backend, seed,
resolution, steps, guidance, sampler, source image, generation time, edit
history and lineage. Only fields that actually exist are stored - an absent
sampler stays absent rather than becoming an empty string.

The record is written **twice**: a JSON side-car next to the image, and the
generation fields inside a PNG's own text chunks. Copy just the PNG somewhere
and its prompt and seed are still with it.

Images form a **version graph**:

```
Source -> Variation -> Edit -> Upscale
```

Children point at their parent, so a lineage can always be walked back to the
original. If a parent has been moved or deleted, that is reported rather than
hidden - the graph says which image is now missing.

---

## 11. Projects and scenes

**Use in project** copies the image into the project's `assets/` folder and
records its prompt and seed in the asset's notes.

**Send to scene** does that and then adds the image to a scene as an *overlay*,
*background*, *character* or *reference*. The scene stores the **asset id**, not
a file path and not a copy of the file, so:

* the project can be moved to another folder and still find its images;
* the same image sent twice is one asset, not two copies;
* the platform's own renderer draws it - there is no second image system.

If no project is open, both actions say so and explain the two real options:
create a project, or keep the image in the asset library.

Deleting an image that a project uses produces a **missing asset report**, not a
crash, and the project still renders.

---

## 12. What this build does not do

* **No cloud, no paid image API.** Every backend is local. A non-loopback HTTP
  endpoint is refused as *not supported* rather than used.
* **No fake AI.** With no model installed, a prompt is refused with an
  explanation. Nothing generates a placeholder and calls it art.
* **No silent backend switching.** A failure names the backend that failed and
  stops; retry, choose another model, or cancel.
* **No automatic overwriting**, anywhere.
* Background removal, AI upscaling and real diffusion/inpainting/outpainting
  need their own packages and weights, which are **not bundled**. See
  `STAGE_F_REPORT.md` for exactly which of them were verified on the Stage F
  test machine - most were not, because the machine has no such model.
