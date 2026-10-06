"""FFmpeg encode commands and frame streaming (Stage E, sections 32-37, 45-46).

Frames are piped to the encoder one at a time.  A 60-minute 1080p video holds
one frame in memory here, not a hundred and eight thousand - which is the whole
reason rendering is allowed to be arbitrarily long (directive sections 33, 34).

Every command is built from settings that :mod:`app.render.capabilities` has
already checked against the installed FFmpeg, so a codec that does not exist
never reaches this module.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from ..core.logging_setup import log_event
from ..project.presets import CODEC_ENCODERS
from .capabilities import rate_control_modes

__all__ = [
    "EncodeResult",
    "video_encoder_args",
    "raw_input_args",
    "format_encoder_args",
    "stream_encode",
    "concat_and_mux",
    "burn_subtitles",
    "probe_detect",
]


@dataclass
class EncodeResult:
    ok: bool
    output: Optional[Path] = None
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    what_to_do: str = ""
    cancelled: bool = False
    frames_written: int = 0
    seconds: float = 0.0
    command: str = ""
    details: dict = field(default_factory=dict)

    def describe_failure(self) -> str:
        if self.cancelled:
            return "The render was cancelled."
        parts = [self.error or f"FFmpeg exited with code {self.returncode}."]
        if self.what_to_do:
            parts.append(self.what_to_do)
        tail = (self.stderr or "").strip().splitlines()[-6:]
        if tail:
            parts.append("FFmpeg said: " + " ".join(line.strip() for line in tail))
        return "\n".join(parts)


# --------------------------------------------------------------------------
# Command building
# --------------------------------------------------------------------------

def raw_input_args(width: int, height: int, fps: int) -> list[str]:
    """Input arguments for a raw RGB24 frame stream on stdin."""
    return [
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{int(width)}x{int(height)}",
        "-r", str(int(fps)),
        "-i", "-",
    ]


def video_encoder_args(settings: Any, *, two_pass: int = 0,
                       stats_file: Optional[Path] = None) -> list[str]:
    """The video encoder arguments for one quality setting.

    Rate control follows what the encoder actually supports: x264/x265 take
    ``-crf``, VP9 takes ``-cq``.  Only the modes that are valid for the chosen
    codec are ever emitted.
    """
    codec = str(getattr(settings, "codec", "h264_cpu") or "h264_cpu")
    encoder = CODEC_ENCODERS.get(codec, "libx264")
    modes = rate_control_modes(codec)
    args: list[str] = ["-c:v", encoder]

    preset = str(getattr(settings, "encoder_preset", "") or "")
    if preset and encoder in ("libx264", "libx265"):
        args += ["-preset", preset]

    bitrate = int(getattr(settings, "bitrate_kbps", 0) or 0)
    crf = int(getattr(settings, "crf", 20) or 0)

    if bitrate > 0:
        if two_pass:
            args += ["-b:v", f"{bitrate}k", "-pass", str(two_pass)]
            if stats_file is not None:
                args += ["-passlogfile", str(stats_file)]
        else:
            args += ["-b:v", f"{bitrate}k"]
    elif "cq" in modes:
        args += ["-cq", str(crf)]
    else:
        args += ["-crf", str(crf)]

    pixel_format = str(getattr(settings, "pixel_format", "") or "")
    if pixel_format:
        args += ["-pix_fmt", pixel_format]

    keyframe_interval = int(getattr(settings, "keyframe_interval", 0) or 0)
    fps = max(1, int(getattr(settings, "fps", 30) or 30))
    if keyframe_interval > 0:
        args += ["-g", str(max(1, keyframe_interval * fps))]

    # Deterministic playback and a fast start on the web.
    args += ["-movflags", "+faststart"] if str(getattr(settings, "container", "mp4")) == "mp4" else []
    args += ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
    return args


def format_encoder_args(settings: Any) -> list[str]:
    """Arguments that force the output container regardless of the file name."""
    container = str(getattr(settings, "container", "mp4") or "mp4")
    return ["-f", container]


def stream_encode(*, tools: Any, frames: Any, output: Path, width: int,
                  height: int, fps: int, settings: Any, cancel_token: Any = None,
                  timeout: float = 7200.0, progress: Optional[Callable[[int], None]] = None,
                  extra_args: Optional[Sequence[str]] = None,
                  audio: Optional[Path] = None, two_pass: bool = False,
                  pass_stats: Optional[Path] = None) -> EncodeResult:
    """Encode a stream of raw RGB24 frames straight into a file.

    The frames are consumed lazily, so the caller can be a generator that renders
    one frame at a time and never holds the video.

    ``two_pass`` runs a real two-pass encode: one analysis pass that writes the
    encoder's statistics file, then the encode that uses it.  Both passes are
    separate FFmpeg runs with ``-pass 1`` and ``-pass 2``; the statistics file is
    named explicitly so parallel or repeated renders cannot share one.  Two
    passes need the frames twice, so ``frames`` may also be a callable that
    returns a fresh iterator - with a single-use stream there is nothing to read
    a second time and that is reported rather than silently halved.
    """
    import time

    tools.ensure_ffmpeg()
    started = time.monotonic()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    bitrate = int(getattr(settings, "bitrate_kbps", 0) or 0)
    do_two_pass = bool(two_pass) and bitrate > 0
    if two_pass and not do_two_pass:
        # Never pretend a pass happened.  The caller validated this combination
        # already; this is the last line of defence.
        return EncodeResult(
            ok=False, output=output, returncode=-1,
            error="Two-pass encoding needs a target bitrate, but none is set.",
            what_to_do="Set a target bitrate in the export settings, or turn "
                       "two-pass off.",
            command="")
    if do_two_pass and not callable(frames):
        return EncodeResult(
            ok=False, output=output, returncode=-1,
            error="Two-pass encoding needs to read the frames twice, but this "
                  "frame source can only be read once.",
            what_to_do="Turn off two-pass encoding, or render again.",
            command="")

    stats = Path(pass_stats) if pass_stats is not None else \
        output.parent / f"{output.stem}.pass"
    passes: tuple[int, ...] = (1, 2) if do_two_pass else (0,)
    frame_source: Callable[[], Iterable[bytes]] = frames if callable(frames) \
        else (lambda: frames)

    result: Optional[EncodeResult] = None
    for position, number in enumerate(passes):
        final = position == len(passes) - 1
        result = _run_stream_pass(
            tools=tools, frames=frame_source(), output=output, width=width,
            height=height, fps=fps, settings=settings, cancel_token=cancel_token,
            timeout=timeout, progress=progress if final else None,
            extra_args=extra_args, audio=audio if final else None,
            pass_number=number, pass_stats=stats, started=started)
        if not result.ok:
            _remove_pass_logs(stats)
            return result
    _remove_pass_logs(stats)
    if result is not None and len(passes) > 1:
        result.details["passes"] = len(passes)
    return result


def _remove_pass_logs(stats: Path) -> None:
    """Delete the encoder's statistics files; they are scratch, not output."""
    for suffix in ("-0.log", "-0.log.mbtree", "-0.log.temp", "-0.log.x264"):
        try:
            Path(f"{stats}{suffix}").unlink(missing_ok=True)
        except OSError:
            pass


def _run_stream_pass(*, tools: Any, frames: Iterable[bytes], output: Path,
                     width: int, height: int, fps: int, settings: Any,
                     cancel_token: Any, timeout: float,
                     progress: Optional[Callable[[int], None]],
                     extra_args: Optional[Sequence[str]], audio: Optional[Path],
                     pass_number: int, pass_stats: Path,
                     started: float) -> EncodeResult:
    """One FFmpeg encode.  ``pass_number`` is 0 for a normal single-pass run."""
    import time

    analysis_only = pass_number == 1
    argv: list[str] = [str(tools.ffmpeg), "-hide_banner", "-nostdin", "-y", "-v", "error", "-stats"]
    argv += raw_input_args(width, height, fps)
    if audio is not None and not analysis_only:
        argv += ["-i", str(audio)]
    if extra_args:
        argv += [str(a) for a in extra_args]
    argv += video_encoder_args(settings, two_pass=pass_number,
                               stats_file=pass_stats if pass_number else None)
    if audio is not None and not analysis_only:
        audio_codec = str(getattr(settings, "audio_codec", "aac") or "aac")
        argv += ["-c:a", audio_codec,
                 "-b:a", f"{int(getattr(settings, 'audio_bitrate_kbps', 192) or 192)}k",
                 "-ar", str(int(getattr(settings, 'sample_rate', 48000) or 48000)),
                 "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    else:
        argv += ["-an"]
    if analysis_only:
        # The first pass exists only to measure the picture, so its output is
        # discarded rather than written anywhere.
        argv += ["-f", "null", os.devnull]
    else:
        argv += [str(output)]
    command = " ".join(_quote(part) for part in argv)

    label = f"Encoding {output.name}" if not pass_number else \
        f"Encoding {output.name} (pass {pass_number} of 2)"
    log_event("RENDER_ENCODE_START", label, command=command,
              width=width, height=height, fps=fps,
              two_pass=pass_number or None)

    written = 0
    cancelled = False
    error_text = ""
    what_to_do = ""
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=_no_window_flags(),
        )
    except OSError as exc:
        return EncodeResult(ok=False, returncode=-1, error=f"FFmpeg could not be started: {exc}",
                            what_to_do="Check that FFmpeg is installed and runnable.", command=command)

    if cancel_token is not None:
        cancel_token.register(process)
    try:
        assert process.stdin is not None
        for chunk in frames:
            if cancel_token is not None and cancel_token.is_cancelled():
                cancelled = True
                break
            try:
                process.stdin.write(chunk)
            except (BrokenPipeError, OSError):
                break
            written += 1
            if progress is not None:
                progress(written)
        try:
            process.stdin.close()
        except OSError:
            pass
        # communicate() would try to flush the stream we just closed, so drop the
        # reference and read the pipes directly.
        process.stdin = None
        try:
            _, stderr_bytes = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            error_text = f"FFmpeg did not finish within {timeout:.0f} seconds."
            what_to_do = "Try a shorter video, a lower resolution, or a faster quality preset."
    finally:
        if cancel_token is not None:
            cancel_token.unregister(process)

    stderr = (stderr_bytes.decode("utf-8", "replace") if stderr_bytes else "")
    code = process.returncode if process.returncode is not None else -1
    seconds = time.monotonic() - started

    if cancelled or (cancel_token is not None and cancel_token.is_cancelled()):
        log_event("RENDER_ENCODE_CANCELLED", "Encoding stopped by the user", output=str(output),
            frames_written=written)
        return EncodeResult(ok=False, output=output, returncode=code, stderr=stderr,
                            cancelled=True, frames_written=written, seconds=seconds,
                            command=command, error="The render was cancelled.")

    if code != 0:
        log_event("RENDER_ENCODE_FAILED", f"FFmpeg exited with code {code}", output=str(output),
            frames_written=written)
        return EncodeResult(ok=False, output=output, returncode=code, stderr=stderr,
                            error=error_text or f"FFmpeg exited with code {code}.",
                            what_to_do=what_to_do or "See the FFmpeg output below.",
                            frames_written=written, seconds=seconds, command=command)

    if pass_number == 1:
        if not Path(f"{pass_stats}-0.log").exists():
            log_event("RENDER_ENCODE_FAILED", "Pass 1 produced no statistics file",
                      output=str(output))
            return EncodeResult(
                ok=False, output=output, returncode=code, stderr=stderr,
                error="The first encoding pass produced no statistics file, so a "
                      "second pass would be no different from a single pass.",
                what_to_do="Turn off two-pass encoding, or check the FFmpeg output "
                           "below.",
                frames_written=written, seconds=seconds, command=command)
        log_event("RENDER_ENCODE_PASS_DONE", "Analysis pass finished",
                  output=str(output), frames_written=written,
                  stats=str(pass_stats))
        return EncodeResult(ok=True, output=output, returncode=0, stderr=stderr,
                            frames_written=written, seconds=seconds, command=command)

    log_event("RENDER_ENCODE_DONE", f"Encoded {written} frames in {seconds:.1f}s",
        output=str(output), frames_written=written, seconds=round(seconds, 2))
    return EncodeResult(ok=True, output=output, returncode=0, stderr=stderr,
                        frames_written=written, seconds=seconds, command=command,
                        details={"encode_fps": round(written / seconds, 2) if seconds > 0 else 0.0})


def concat_and_mux(*, tools: Any, segments: Sequence[Path], output: Path,
                   audio: Optional[Path], settings: Any,
                   cancel_token: Any = None, timeout: float = 3600.0) -> EncodeResult:
    """Join the finished segments and mix in the master audio.

    The video streams are copied, never re-encoded, so joining a 60-minute video
    takes seconds and cannot lose quality (directive sections 38, 41).
    """
    import time

    tools.ensure_ffmpeg()
    list_path = output.parent / f"{output.stem}.concat.txt"
    lines = [f"file '{Path(segment).as_posix()}'" for segment in segments]
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    started = time.monotonic()
    argv: list[str] = [str(tools.ffmpeg), "-hide_banner", "-nostdin", "-y", "-v", "error",
                       "-f", "concat", "-safe", "0", "-i", str(list_path)]
    if audio is not None:
        argv += ["-i", str(audio)]
    argv += ["-map", "0:v:0", "-c:v", "copy"]
    if audio is not None:
        audio_codec = str(getattr(settings, "audio_codec", "aac") or "aac")
        argv += ["-map", "1:a:0", "-c:a", audio_codec,
                 "-b:a", f"{int(getattr(settings, 'audio_bitrate_kbps', 192) or 192)}k",
                 "-ar", str(int(getattr(settings, 'sample_rate', 48000) or 48000)),
                 "-shortest"]
    else:
        argv += ["-an"]
    if str(getattr(settings, "container", "mp4")) == "mp4":
        argv += ["-movflags", "+faststart"]
    argv += [str(output)]
    command = " ".join(_quote(part) for part in argv)

    log_event("RENDER_ASSEMBLE_START", f"Assembling {len(segments)} segment(s) into {output.name}",
        command=command)
    result = tools.run(argv[1:], timeout=timeout, cancel_token=cancel_token)
    seconds = time.monotonic() - started
    if not result.ok:
        log_event("RENDER_ASSEMBLE_FAILED", result.describe_failure()[:400])
        return EncodeResult(ok=False, output=output, returncode=result.returncode,
                            stdout=result.stdout, stderr=result.stderr,
                            error=result.describe_failure(),
                            what_to_do="Check that there is disk space and that the "
                                       "segment files still exist.",
                            seconds=seconds, command=command)
    log_event("RENDER_ASSEMBLE_DONE", f"Assembled in {seconds:.1f}s", output=str(output))
    return EncodeResult(ok=True, output=output, returncode=0, stdout=result.stdout,
                        stderr=result.stderr, seconds=seconds, command=command)


def burn_subtitles(*, tools: Any, source: Path, ass_file: Path, output: Path,
                   settings: Any, cancel_token: Any = None,
                   timeout: float = 7200.0) -> EncodeResult:
    """Re-encode with captions drawn onto the picture (libass).

    This is the only step that re-encodes the picture, and it only happens when
    the user asked for burnt-in subtitles - otherwise the segments are copied.
    """
    import time

    tools.ensure_ffmpeg()
    started = time.monotonic()
    # libass reads the path itself, so it must be escaped for the filter parser.
    escaped = str(ass_file).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    argv: list[str] = [str(tools.ffmpeg), "-hide_banner", "-nostdin", "-y", "-v", "error", "-stats",
                       "-i", str(source),
                       "-vf", f"subtitles='{escaped}'",
                       "-c:a", "copy"]
    argv += video_encoder_args(settings)
    argv += [str(output)]
    command = " ".join(_quote(part) for part in argv)
    log_event("RENDER_BURN_START", f"Burning captions from {ass_file.name}", command=command)
    result = tools.run(argv[1:], timeout=timeout, cancel_token=cancel_token)
    seconds = time.monotonic() - started
    if not result.ok:
        log_event("RENDER_BURN_FAILED", result.describe_failure()[:400])
        return EncodeResult(ok=False, output=output, returncode=result.returncode,
                            stdout=result.stdout, stderr=result.stderr,
                            error=result.describe_failure(),
                            what_to_do="This FFmpeg build may not include libass. "
                                       "Export the subtitles as a .srt file instead, or "
                                       "turn off burnt-in captions.",
                            seconds=seconds, command=command)
    log_event("RENDER_BURN_DONE", f"Burned captions in {seconds:.1f}s", output=str(output))
    return EncodeResult(ok=True, output=output, returncode=0, stdout=result.stdout,
                        stderr=result.stderr, seconds=seconds, command=command)


def probe_detect(*, tools: Any, source: Path, duration: float = 30.0,
                 pixel_threshold: float = 0.05, ratio: float = 0.98,
                 minimum: float = 0.5) -> dict:
    """Real black-frame detection using FFmpeg's ``blackdetect`` filter.

    Returns where the picture is actually black, rather than guessing from the
    scene model (directive section 40).

    ``blackdetect`` takes two different thresholds and they are easy to confuse:

    * ``pix_th`` - per-pixel luma below which a pixel counts as black.
    * ``pic_th`` - fraction of such pixels needed before a *frame* is black.

    Earlier this passed the ratio (0.98) in as ``pix_th``, which made almost
    every pixel count as black and flagged an entire dark-themed video.  The
    measured difference matters: a navy background has about 98.6% of pixels
    below luma 0.10 but only 0.12% below 0.05, while a genuinely black frame is
    100% at both.  So ``pix_th=0.05`` reports real black and leaves a dark but
    perfectly normal design alone.
    """
    result = tools.run(
        ["-hide_banner", "-nostdin", "-i", str(source), "-vf",
         f"blackdetect=d={minimum}:pix_th={pixel_threshold}:pic_th={ratio}",
         "-an", "-f", "null", "-"],
        timeout=max(60.0, duration * 4.0),
    )
    text = (result.stdout or "") + (result.stderr or "")
    ranges: list[dict] = []
    for line in text.splitlines():
        if "black_start" not in line:
            continue
        # blackdetect reports "black_start:0 black_end:1.96 black_duration:1.96".
        fields: dict[str, str] = {}
        for part in line.split():
            for separator in (":", "="):
                if separator in part:
                    key, _, value = part.partition(separator)
                    fields[key.strip()] = value.strip()
                    break
        try:
            start = float(fields.get("black_start", ""))
            end = float(fields.get("black_end", ""))
        except ValueError:
            continue
        ranges.append({"start": start, "end": end, "duration": end - start})
    return {"ok": result.ok, "ranges": ranges,
            "black_seconds": round(sum(item["duration"] for item in ranges), 3)}


# --------------------------------------------------------------------------

def _quote(value: Any) -> str:
    text = str(value)
    if any(char.isspace() for char in text) or "'" in text:
        return "'{}'".format(text.replace("'", "'\\''"))
    return text


def _no_window_flags() -> int:
    """Hide the console window on Windows; a no-op elsewhere."""
    import sys

    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0
