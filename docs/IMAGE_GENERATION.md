# Image generation

The image engine is Stage F's, unchanged; Stage G put it behind the unified AI
backend manager, gave it a shared queue, and made every result land in a history
and a library that the video half also uses. Nothing was rebuilt: the same
`ImageService`, the same six adapters, the same metadata.

For the backend types see `docs/AI_BACKENDS.md`; for the studio screens see
`docs/AI_STUDIO.md` and `docs/IMAGE_STUDIO.md`; for the CLI, `mgs ai image` and
`motion-studio image`.

---

## 1. What it does

| Operation | Notes |
|---|---|
| **Text to image** | prompt, negative prompt, size, seed, steps/guidance where the backend supports them |
| **Image to image** | a source image is **copied into the request, never modified**; strength controls how much changes |
| **Inpainting** | a mask editor: brush, erase, clear, invert, feather, zoom, pan; the masked area is regenerated and the rest of the picture is kept |
| **Outpainting** | the canvas is extended asymmetrically (left / right / top / bottom independently) and the new area is filled |
| **Variations** | a new file with a recorded parent, so the family tree stays visible |
| **Upscaling** | **standard resize** (never called AI) or an AI upscaler when one is installed |
| **Background removal** | an abstraction with a real contract; on a machine with no remover, it is `NOT_INSTALLED` and says so |

Every one of these is a **new file**. The source is kept, its bytes are compared
in the tests, and nothing is overwritten unless the user confirms it - and then a
backup is kept.

---

## 2. References and consistency

Source images and masks are stored **separately** from the results (the
references folder), so "which picture was this made from?" is always answerable.
Consistency between generations is offered only where the metadata supports it:
same model, same seed, same style configuration. Where it does not - because the
backend ignores the seed, or the model is not installed - the application says so
instead of promising likeness.

---

## 3. Style and prompt library

* A project can carry a **style** (a named set of prompt fragments and settings).
* The **prompt library** stores prompts you keep, with the settings they were
  used with.
* Applying either is explicit: it fills the form, and you can see and edit what
  will be sent before pressing Generate. Nothing is added to your prompt behind
  your back.

---

## 4. History and the library

Every image the engine produces is recorded with its full request and the facts
measured from the file (size, dimensions, format, checksum). From a history row
you can:

* **Regenerate** - the same request again, into a new file;
* **make a Variation** - a new seed, recorded as a child of the original;
* **Edit** - open the editor on it (non-destructively);
* **Upscale** - into a new file;
* **Send to project / scene** - copy it in as an asset and reference it by id;
* **Delete** - with a confirmation, and never touching a file that a project
  still points at.

The library is paged, searchable, taggable and caches thumbnails, keyed to the
file's size and modification time so a replaced file never shows a stale
picture.

---

## 5. Honesty rules that apply to images

* **Standard resize is never AI.** The operation is labelled *Standard Resize*,
  and the report says `method=standard`.
* **Test backends are labelled.** The built-in backend writes real files and
  says it is not a model.
* **An unavailable operation is refused**, not approximated:
  `background_removal` with nothing installed returns `NOT_INSTALLED` and
  instructions.
* **No placeholders.** If a generation cannot run, no file appears.
* **Only real checks earn VERIFIED** - see `docs/AI_BACKENDS.md`.

---

## 6. Verified on the build machine

From `docs/STAGE_G_REPORT.md` and Stage F's report, the parts that were actually
run here:

* text to image, image to image, inpaint and outpaint through the built-in and
  local-command adapters - real files, real measurements;
* standard resize upscaling (`320x200 (2x)`, labelled as resize);
* an AI upscaler: **not installed** → refused;
* background removal: **not installed** → refused;
* a real diffusion model: **not verified** - no model is installed on the build
  machine, and `diffusers` stops at the model-loading boundary and says so.
