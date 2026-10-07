# AI backends

A backend is anything this application can ask to make a picture, a clip, an
upscale or a background-free image. Stage F built the image adapters; Stage G put
them and the video adapters behind **one** contract and one manager, so the page,
the queue, the history, the CLI and the tests all speak about backends the same
way.

See also: `docs/AI_STUDIO.md`, `docs/IMAGE_BACKENDS.md` (the image side in
detail), `docs/VIDEO_GENERATION.md`.

---

## 1. The hierarchy

```
AIBackend                      one object per way of generating
├── ImageProvider   (app/image/provider.py)     text_to_image, image_to_image,
│                                               inpaint, outpaint, variation,
│                                               upscale, background_removal
└── VideoProvider   (app/ai/video.py)           text_to_video, image_to_video,
                                                video_to_video, video_extend,
                                                storyboard_to_video
```

Every backend publishes the same metadata:

| Field | Meaning |
|---|---|
| `id`, `name`, `kind` | identity, and whether it makes images or clips |
| `version` | the backend adapter's own version string |
| `capabilities` | exactly which operations it supports |
| `status()` | state + reason + what to do |
| `device_requirement` | `CPU_SUPPORTED`, `GPU_RECOMMENDED`, `GPU_REQUIRED`, `UNKNOWN` |
| `licence`, `homepage` | attribution, for the licence record |
| `settings_schema()` | the fields the user may configure |
| `is_model` | `False` for every fixture, so nothing can label it as a model |

`App/ai/backend.py` holds the shared behaviour: capability checks, health checks,
model listing, the settings schema and the "explain yourself" text that every
page and report uses.

**Backend types** are the transports, all of them local:

| Type | Used by | What it is |
|---|---|---|
| local Python | `standard`, `standard_video` | in-process code |
| local CLI | `command`, `command_video` | a program on this machine, arguments templated |
| local HTTP | `http`, `http_video` | a local server: host, port, endpoint, timeout |
| ComfyUI | `comfyui`, `comfyui_video` | a local ComfyUI instance, workflow driven |
| Diffusers | `diffusers`, `diffusers_video` | a local model loaded in-process |
| ONNX | `onnx` | an ONNX Runtime session |
| cloud | `cloud` | **architecture only**: reports `NOT SUPPORTED` here |

---

## 2. The manager

`AIBackendManager` (app/ai/registry.py) owns the list and everything the user can
do to it:

* **enable / disable** - a disabled backend is never chosen automatically
* **configure** - per-backend settings with the backend's own schema
* **test** - light or deep (see below)
* **refresh** - re-run detection, rebuilding only what changed
* **logs** - the events it produced
* **models** - what it offers and what was found on disk
* **defaults** - a default backend and model per kind, per project

Detection is deliberately cheap. On the Stage G build machine it costs
**6 ms and +0.3 MiB** (§`STAGE_G_REPORT.md`), because detection asks the
operating system and the file system questions - it never imports torch and never
loads weights to answer "is a GPU present?".

### Light check and deep check

| | Light check | Deep check |
|---|---|---|
| What it does | path exists, command runs `--version`, folder readable, port open, module importable | initialise the backend and make one small real result |
| What it may claim | `AVAILABLE` | `VERIFIED` (and only then) |
| Cost | milliseconds | seconds to minutes, and it writes a file |

A deep check that fails reports `NOT_VERIFIED` with the reason, and its result
file is removed. A backend that is not installed reports `NOT_INSTALLED` and
tells you what to install. `mgs ai selftest` runs the deep check on every usable
backend, one at a time.

---

## 3. States

| State | Means |
|---|---|
| `AVAILABLE` | usable now, checked lightly |
| `VERIFIED` | a deep check ran here and produced a real result |
| `NOT_INSTALLED` | the program, package or model is not here |
| `NOT_VERIFIED` | it is here, but a deep check did not produce a result (and says why) |
| `NOT_SUPPORTED` | this build does not do this at all (the cloud adapter) |
| `LIMITED` | it works, with a stated restriction |
| `DISABLED` | the user turned it off |

---

## 4. Capabilities

A backend declares what it can do, and the interface obeys it:

* the operations it does not support are **disabled**, not left live;
* a request for an unsupported operation is refused **before** anything runs,
  with the reason and a suggestion;
* the built-in backends (`standard`, `standard_video`) explicitly declare that
  they **cannot understand a prompt**, and are labelled as fixtures wherever
  their output appears;
* an `image_to_video` request to a text-only backend fails with
  `VIDEO_IMAGE_TO_VIDEO_UNSUPPORTED` rather than ignoring the source image.

---

## 5. Cancellation, timeouts and failure

* **Cancellation** ends the child process (or the HTTP request, or the loop in
  process) and leaves no zombie; `cancel` is checked between steps and between
  batch items. Work already saved stays saved.
* **Timeouts** are per backend and configurable. A timeout is a failure with the
  duration named, never a silent retry.
* **Failure** is reported with what happened, why, and what to do, plus an error
  code a test can assert (`NO_BACKEND_AVAILABLE`, `BACKEND_NOT_AVAILABLE`,
  `BACKEND_DISABLED`, `BACKEND_CRASHED`, `OPERATION_NOT_SUPPORTED`,
  `VIDEO_OUTPUT_MISSING`, `VIDEO_OUTPUT_TOO_SMALL`,
  `VIDEO_IMAGE_TO_VIDEO_UNSUPPORTED`, ...).
* **No hidden retries anywhere.** A retry is a button the user presses.

---

## 6. Security rules (section 65)

* A backend never runs a program the user did not configure, and never with a
  shell (`shell=False`, argument list only).
* Endpoints are `127.0.0.1`/`localhost` by default; a non-local address must be
  typed by the user, and the address is shown wherever the backend is used.
* Nothing is downloaded or installed automatically - see `docs/AI_MODELS.md`.
* Project assets and prompts are never sent anywhere except to the endpoint the
  user configured, and never to a cloud service in this build.
* A backend's stderr is captured and shown as technical detail, never executed.

---

## 7. Adding a backend

1. Subclass `ImageProvider` or `VideoProvider` and set `id`, `name`, `kind`,
   `version`, `capabilities`, `device_requirement`, `licence` and `is_model`.
2. Implement `describe()`, `location()`, `availability()` and the operations you
   claim in `capabilities`. Implement `settings_schema()` if it needs settings.
3. Implement `check(deep=True)` so a deep check can earn `VERIFIED`.
4. Register it in `AIBackendManager.build()` (or pass it as `extra_backends` in
   tests, which is how the fixtures are wired in).
5. Add contract tests: `tests/test_ai_backends.py` iterates **every** registered
   backend, so a new adapter is tested by existing code - and it must pass the
   error-injection cases (missing model, invalid response, timeout, cancel,
   crash) without a crash of its own.

The rule for any new adapter: **if it cannot do something, say so** - never
return a placeholder file and never claim a result it did not produce.
