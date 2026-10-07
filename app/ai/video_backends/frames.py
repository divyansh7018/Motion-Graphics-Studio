"""Frame sources for video backends (sections 21, 23, 46).

A video backend needs frames from one of three places: nowhere (text to
video), a still image (image to video), or another clip (video to video and
extend).  This module provides all three as plain iterators of raw RGB24
bytes, so they can be handed straight to the same streaming encoder the render
engine uses.

Two properties matter and are guaranteed here:

* **deterministic.**  The same prompt and seed produce the same frames, byte
  for byte.  That is what makes "regenerate" mean something (sections 12, 50).
* **bounded memory.**  Frames are produced one at a time and never
  accumulated, so a long clip costs a frame, not a video (sections 42, 86).

Nothing here is a model.  It is arithmetic, and it is labelled as arithmetic
wherever it surfaces (section 79).
"""

from __future__ import annotations

import hashlib
import subprocess
from typing import Any, Iterator, Optional, Sequence

from ...core.process import no_window_flags, terminate_process

__all__ = [
    "synthetic_frames",
    "image_frames",
    "video_frames",
    "transform_frames",
    "CameraMove",
    "CAMERA_MOVES_SUPPORTED",
    "frame_iterator_from_raw",
]


class CameraMove:
    """The moves the built-in frame sources can really perform."""

    STATIC = "static"
    PAN = "pan"
    TILT = "tilt"
    ZOOM = "zoom"
    DOLLY = "dolly"


#: Only these are offered by the built-in sources.  Orbit, handheld and custom
#: need a real model to mean anything, so they are not claimed (section 21).
CAMERA_MOVES_SUPPORTED: tuple[str, ...] = (
    CameraMove.STATIC, CameraMove.PAN, CameraMove.TILT, CameraMove.ZOOM,
    CameraMove.DOLLY,
)


def _digest(prompt: str, seed: int) -> bytes:
    return hashlib.sha256(f"{prompt}|{int(seed)}".encode("utf-8")).digest()


def _rgb(value: Sequence[int]) -> tuple[int, int, int]:
    return int(value[0]), int(value[1]), int(value[2])


def synthetic_frames(prompt: str, seed: int, width: int, height: int,
                     frames: int, *, camera: str = "",
                     amount: float = 0.0) -> Iterator[bytes]:
    """Deterministic moving picture from the prompt and the seed.

    The picture is a colour field with a bright band that travels across it;
    the colours, the speed and the angle all come from a hash of the prompt and
    seed, so the same recipe always gives the same clip and a different seed
    always gives a different one.
    """
    import numpy as np

    width = max(2, int(width))
    height = max(2, int(height))
    frames = max(1, int(frames))
    digest = _digest(prompt, seed)
    base = np.zeros((height, width, 3), dtype=np.uint8)
    ys = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
    xs = np.linspace(0.0, 1.0, width, dtype=np.float32)[None, :]
    base[..., 0] = np.clip(digest[0] * 0.55 + ys * 90.0, 0, 255).astype(np.uint8)
    base[..., 1] = np.clip(digest[1] * 0.55 + xs * 70.0, 0, 255).astype(np.uint8)
    base[..., 2] = np.clip(digest[2] * 0.55 + (1.0 - ys) * 80.0, 0, 255).astype(np.uint8)

    # The band's travel speed comes from the seed too, so two seeds do not
    # merely recolour the same movement.
    speed = 0.6 + (digest[4] / 255.0) * 0.8
    band = 0.10 + (digest[5] / 255.0) * 0.10
    bright = np.array([160, 190, 210], dtype=np.float32) + \
        np.array([digest[6], digest[7], digest[8]], dtype=np.float32) * 0.25
    amount = float(amount or 0.0)
    move = str(camera or CameraMove.STATIC)

    for index in range(frames):
        progress = index / max(1, frames - 1)
        travel = (progress * speed) % 1.0
        # A travelling band: distance from the band's centre line.
        centre = travel * 1.4 - 0.2
        distance = np.abs((xs * 0.85 + ys * 0.15) * 1.4 - centre)
        in_band = np.clip(1.0 - (distance / max(1e-3, band)), 0.0, 1.0)[..., None]
        frame = base.astype(np.float32) + in_band * bright[None, None, :] * 0.9
        # Camera moves change how much of the picture is seen, and from where.
        scale = 1.0
        offset_x = 0.0
        offset_y = 0.0
        if move == CameraMove.ZOOM:
            scale = 1.0 + amount * progress
        elif move == CameraMove.DOLLY:
            scale = 1.0 + amount * progress * 0.5
        elif move == CameraMove.PAN:
            offset_x = amount * (progress - 0.5) * 0.5
        elif move == CameraMove.TILT:
            offset_y = amount * (progress - 0.5) * 0.5
        if scale != 1.0 or offset_x or offset_y:
            frame = _crop_and_scale(frame, scale, offset_x, offset_y)
        yield np.clip(frame, 0, 255).astype(np.uint8).tobytes()


def _crop_and_scale(frame: Any, scale: float, offset_x: float,
                    offset_y: float) -> Any:
    """A cheap pan/zoom of a float array, without pulling in an image library."""
    import numpy as np

    height, width = frame.shape[0], frame.shape[1]
    scale = max(0.2, float(scale))
    keep_h = max(2, int(height / scale))
    keep_w = max(2, int(width / scale))
    centre_y = int(np.clip((0.5 - offset_y) * height, 0, height - keep_h))
    centre_x = int(np.clip((0.5 - offset_x) * width, 0, width - keep_w))
    window = frame[centre_y:centre_y + keep_h, centre_x:centre_x + keep_w]
    # Nearest-neighbour upscale is exact, instant, and deterministic.
    rows = (np.arange(height) * (keep_h / height)).astype(int).clip(0, keep_h - 1)
    columns = (np.arange(width) * (keep_w / width)).astype(int).clip(0, keep_w - 1)
    return window[rows][:, columns]


def image_frames(source: Any, width: int, height: int, frames: int,
                 *, camera: str = "", amount: float = 0.0,
                 fit: str = "cover") -> Iterator[bytes]:
    """Move a still image: the real meaning of image to video.

    The source is opened once, resized once to the output frame, and then each
    frame is a crop of it - so a 40-frame clip costs one image, not forty.
    """
    import numpy as np
    from PIL import Image

    width = max(2, int(width))
    height = max(2, int(height))
    frames = max(1, int(frames))
    with Image.open(source) as handle:
        handle.load()
        picture = handle.convert("RGB")

    # Zoomed-in master frame so a pan or zoom has pixels to move into.
    move = str(camera or CameraMove.STATIC)
    slack = 1.0 + abs(float(amount or 0.0)) * 0.5 if move != CameraMove.STATIC else 1.0
    master_w = max(width, int(width * slack))
    master_h = max(height, int(height * slack))
    master = _fit(picture, master_w, master_h, fit)
    array = np.asarray(master, dtype=np.uint8)

    for index in range(frames):
        progress = index / max(1, frames - 1)
        scale = 1.0
        offset_x = 0.0
        offset_y = 0.0
        if move == CameraMove.ZOOM:
            scale = 1.0 + float(amount or 0.0) * progress
        elif move == CameraMove.DOLLY:
            scale = 1.0 + float(amount or 0.0) * progress * 0.6
        elif move == CameraMove.PAN:
            offset_x = float(amount or 0.0) * (progress - 0.5) * 0.6
        elif move == CameraMove.TILT:
            offset_y = float(amount or 0.0) * (progress - 0.5) * 0.6
        window = _window(array, width, height, scale, offset_x, offset_y)
        yield np.ascontiguousarray(window).tobytes()


def _fit(picture: Any, width: int, height: int, fit: str) -> Any:
    """Resize to exactly width x height, cropping to fill when asked."""
    from PIL import Image

    if fit == "contain":
        copy = picture.copy()
        copy.thumbnail((width, height), Image.LANCZOS)
        canvas = Image.new("RGB", (width, height), (12, 12, 16))
        canvas.paste(copy, ((width - copy.width) // 2,
                            (height - copy.height) // 2))
        return canvas
    ratio = max(width / max(1, picture.width), height / max(1, picture.height))
    resized = picture.resize((max(1, int(picture.width * ratio)),
                              max(1, int(picture.height * ratio))), Image.LANCZOS)
    left = max(0, (resized.width - width) // 2)
    top = max(0, (resized.height - height) // 2)
    return resized.crop((left, top, left + width, top + height))


def _window(array: Any, width: int, height: int, scale: float,
            offset_x: float, offset_y: float) -> Any:
    """A width x height crop of a larger array, resized back to size."""
    import numpy as np

    source_h, source_w = array.shape[0], array.shape[1]
    scale = max(0.2, float(scale))
    keep_w = max(2, min(source_w, int(width / scale)))
    keep_h = max(2, min(source_h, int(height / scale)))
    centre_x = int(np.clip((0.5 - offset_x) * source_w - keep_w / 2, 0,
                           max(0, source_w - keep_w)))
    centre_y = int(np.clip((0.5 - offset_y) * source_h - keep_h / 2, 0,
                           max(0, source_h - keep_h)))
    window = array[centre_y:centre_y + keep_h, centre_x:centre_x + keep_w]
    if keep_w == width and keep_h == height:
        return window
    rows = (np.arange(height) * (keep_h / height)).astype(int).clip(0, keep_h - 1)
    columns = (np.arange(width) * (keep_w / width)).astype(int).clip(0, keep_w - 1)
    return window[rows][:, columns]


def video_frames(source: Any, *, width: int, height: int, fps: int,
                 limit_frames: int = 0, tools: Any = None,
                 cancel: Any = None, start: float = 0.0) -> Iterator[bytes]:
    """Decode an existing clip into raw frames, one at a time.

    The decoder is an FFmpeg child process whose stdout is read frame by
    frame, so a clip of any length costs one frame of memory.  The child is
    stopped when the iterator is closed - which is what keeps a cancelled
    video-to-video job from leaving a decoder running (section 35).
    """
    from pathlib import Path

    ffmpeg = getattr(getattr(tools, "discovery", None), "ffmpeg", None)
    if ffmpeg is None or not getattr(ffmpeg, "path", None):
        raise RuntimeError("FFmpeg is not available, so the clip cannot be read.")

    width = max(2, int(width))
    height = max(2, int(height))
    fps = max(1, int(fps))
    command = [str(ffmpeg.path), "-hide_banner", "-nostdin", "-v", "error"]
    if float(start or 0.0) > 0.0:
        # Seeking before -i is the fast, accurate-enough form for a clip that
        # starts part-way through: the decoder then reads forwards from there.
        command += ["-ss", f"{float(start):.6f}"]
    command += [
        "-i", str(Path(source)),
        "-vf", f"fps={fps},scale={width}:{height}:flags=bicubic",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE,
                               creationflags=no_window_flags())
    if cancel is not None:
        for name in ("register_process", "register"):
            method = getattr(cancel, name, None)
            if callable(method):
                method(process)
                break
    frame_bytes = width * height * 3
    produced = 0
    try:
        assert process.stdout is not None
        while True:
            if cancel is not None and cancel.is_cancelled():
                break
            chunk = process.stdout.read(frame_bytes)
            if not chunk or len(chunk) < frame_bytes:
                break
            produced += 1
            yield chunk
            if limit_frames and produced >= int(limit_frames):
                break
    finally:
        terminate_process(process)
        for name in ("unregister_process", "unregister"):
            method = getattr(cancel, name, None)
            if callable(method):
                method(process)
                break


def transform_frames(frames: Iterator[bytes], seed: int,
                     width: int, height: int) -> Iterator[bytes]:
    """Apply a deterministic colour transform to every frame.

    This is what "video to video" means without a model: the timing, motion and
    length of the source are preserved exactly, and the look is derived from the
    seed - which is a real, repeatable transformation rather than a claim of
    intelligence.
    """
    import numpy as np

    digest = hashlib.sha256(f"v2v|{int(seed)}".encode("utf-8")).digest()
    gain = np.array([0.7 + digest[0] / 255.0 * 0.6,
                     0.7 + digest[1] / 255.0 * 0.6,
                     0.7 + digest[2] / 255.0 * 0.6], dtype=np.float32)
    lift = np.array([digest[3], digest[4], digest[5]], dtype=np.float32) * 0.18
    for chunk in frames:
        array = np.frombuffer(chunk, dtype=np.uint8).reshape(height, width, 3)
        moved = array.astype(np.float32) * gain[None, None, :] + lift[None, None, :]
        yield np.clip(moved, 0, 255).astype(np.uint8).tobytes()


def frame_iterator_from_raw(source: Any, width: int, height: int) -> Iterator[bytes]:
    """Wrap an already-encoded raw stream (used by tests and simple backends)."""
    frame_bytes = max(1, int(width)) * max(1, int(height)) * 3
    for offset in range(0, len(source) - frame_bytes + 1, frame_bytes):
        yield bytes(source[offset:offset + frame_bytes])


def resolution_for(width: int, height: int, *, aspect: str = "") -> tuple[int, int]:
    """Snap a size to something encodable, keeping the aspect when asked."""
    width = int(width or 0)
    height = int(height or 0)
    if aspect and not (width and height):
        pairs = {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (768, 768),
                 "4:5": (864, 1080), "2:3": (800, 1200)}
        width, height = pairs.get(aspect, (0, 0))
    width = max(0, width - (width % 2))
    height = max(0, height - (height % 2))
    return width, height


def _unused(*args: Any) -> Optional[None]:  # pragma: no cover - keeps linters quiet
    return None
