# Image backends

Image generation goes through one contract (`app/image/provider.py`), and every
backend is an adapter behind it. Nothing in the application is wired to a
particular model.

**A backend reports what it can do, and is never believed beyond that.** A
feature that is not reported is disabled in the interface with the reason shown;
it is never faked. There is no cloud backend: Stage F is local-only, and a
non-loopback address is refused as *not supported*.

---

## The six adapters

| id | Backend | Needs | Always usable? |
|---|---|---|---|
| `standard` | Built-in operations | nothing | **yes** |
| `command` | A local program you configure | the program | when configured |
| `http` | A local HTTP endpoint | a server on this machine | when running |
| `comfyui` | A local ComfyUI server | ComfyUI on this machine | when running |
| `diffusers` | A local Diffusers-compatible model | the `diffusers` package **and** weights | when both exist |
| `onnx` | A local ONNX model | `onnxruntime` **and** a `.onnx` model file | when both exist |

### `standard` - the built-in one

No model, and it says so: `text_to_image` is reported **False**, because it
cannot draw a prompt. What it can do is real: variations, image-to-image,
inpainting with a mask, outpainting by canvas extension, and standard resize. Its
results are labelled *"Standard operation - not an AI model"*.

This is what makes the studio useful on a machine with nothing installed.

### `command` - a program you choose

You give it a command line with placeholders:

```
my-generator --prompt {prompt} --seed {seed} --width {width} \
    --height {height} --out {output}
```

Placeholders: `{prompt}` `{negative_prompt}` `{width}` `{height}` `{seed}`
`{steps}` `{guidance}` `{sampler}` `{output}` `{source_image}` `{mask_image}`
`{reference_image}` `{model}`.

The program must write the image to `{output}` and exit 0. The adapter then
**verifies the file** - it must exist, be non-empty, be a readable image, and
have the size that was asked for. A program that exits 0 without writing
anything is reported as a failure, not as a success.

Nothing is downloaded and no command is invented: with nothing configured, the
backend reports `not_installed` and stays out of the way.

### `http` and `comfyui` - servers on this machine

The default endpoint is `http://127.0.0.1:8188`. A **non-loopback** address is
refused as `not_supported`, so a prompt is never sent somewhere you did not ask
for it. No API key is stored, and the endpoint must answer on this machine.

A `http` response may be raw image bytes or JSON holding base64 image data.

### `diffusers` and `onnx` - local model files

These scan `models/image`, `models/diffusers`, `models/upscaler`, `models` under
the application data folder and your home folder, for `.safetensors`, `.ckpt`,
`.onnx`, `.pt`, `.pth`, `.bin` and `.gguf` files. **Scanning only lists files -
no weights are loaded.**

Loading happens when a generation is requested, and **one model is held at a
time**: loading a second unloads the first, so a machine with 8 GB of RAM is not
asked to hold three models. `unload()` releases the pipeline and collects.

If the package or the weights are missing, the backend reports `not_installed`
and names what it looked for.

> **Status.** These two adapters are implemented up to the model-loading
> boundary and then **stop honestly**: a pipeline that has not been described is
> not guessed at, so the call raises and the user is told rather than being
> handed a wrong picture. See `STAGE_F_REPORT.md` §5.

---

## Capabilities

Each backend reports these flags, and the interface follows them exactly:

```
text_to_image  image_to_image  inpaint  outpaint  upscale
reference_image  control  lora
```

plus the settings it honours (`negative_prompt`, `seed_control`, `steps`,
`guidance`, `sampler`, `batch`, `strength`, `style_reference`) and its limits
(`min_dimension`, `max_dimension`, `dimension_multiple`, `max_batch`,
`supports_alpha`).

`motion-studio image backends` prints them:

```
[yes] Standard (no AI model) (standard) - available
      Built in. Needs no model. It cannot draw a prompt ...
[no ] Diffusers (local model) (diffusers) - not_installed
      The 'Diffusers' package is not installed.
      -> Install Diffusers to enable this backend.
```

---

## Cancellation

Cancelling passes a token all the way down. The `command`, `http` and model
adapters check it between images and hand their subprocess to the token, which
kills it - so a cancelled generation leaves **no FFmpeg-style orphan process**
behind, and the images already written survive. The `standard` backend checks
between images.

---

## Failure

A failing backend returns **what happened, why, and what to do**:

```
An AI upscale needs a local upscaler model, and none is installed on this machine.
  what to do: Use Standard Resize instead (it needs no model), or install an
              upscaler. No image was created.
```

The result always names the backend that ran. **There is no silent fallback to a
different backend**: if the model you chose fails, you are told, and you choose
again. The one permitted downgrade - AI upscale → standard resize - only happens
when the caller explicitly allows it, and the result is labelled as a resize.

---

## Adding a backend

1. Subclass `ImageProvider` with a stable `id`.
2. Implement `status()`, `capabilities()` and `models()`. **Load nothing** in any
   of them.
3. Implement `generate()` using `run_process()` (for a program) or your own
   loop, honouring the cancel token.
4. **Verify the output** with `verify_output()` before reporting success.
5. Return failures through `failed_result()` so they carry what happened, why and
   what to do.
6. Register the class in `BACKEND_CLASSES` and add it to `BACKEND_ORDER`.
7. Run `tests/test_image_backends.py`: the contract tests iterate every
   registered adapter, so a new one is checked against the same expectations -
   capability metadata, validation, job creation, cancellation, output
   validation and error propagation.

A backend that cannot implement a capability must report **False**, never guess.
The contract tests fail if an unavailable backend raises instead of explaining.
