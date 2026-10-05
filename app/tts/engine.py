"""The Kokoro engine wrapper (directive sections 10, 22, 29, 51, 52).

Rules this module enforces:

* **Lazy loading** — constructing the wrapper does no work.  The model is read
  only when :meth:`KokoroEngine.load` is called, which the job runner does on a
  worker thread so the interface never freezes (sections 29, 52).
* **One instance, one job at a time** — a lock serialises generation, so two
  Kokoro instances can never run in parallel on a low-memory machine.
* **Cancellable** — long scripts are generated in chunks and the cancel flag is
  checked between them, so cancelling stops promptly and cleans up (section 31).
* **No hidden fallbacks** — if Kokoro is missing the engine raises a friendly
  error.  It never reaches for a different engine or an online service.

Importing this module has no side effects: it never loads a model, spawns a
thread or writes a file.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from app.core.errors import AppError, FriendlyError, JobCancelled, Severity
from app.core.logging_setup import get_logger

LOGGER = get_logger(__name__)

#: Kokoro 82M outputs 24 kHz mono.
DEFAULT_SAMPLE_RATE = 24000

#: Speed limits the Kokoro pipeline documents.  Outside this range the model
#: produces degraded audio, so the interface clamps rather than pretending.
MIN_SPEED = 0.5
MAX_SPEED = 2.0

#: Voices are generated in chunks so cancellation stays responsive.
DEFAULT_CHUNK_CHARS = 400


class KokoroError(AppError):
    """Kokoro is unavailable or refused to generate."""

    default_title = "Narration generation failed"
    default_severity = Severity.ERROR


@dataclass
class GenerationRequest:
    """Everything needed for one generation call."""

    text: str
    voice: str
    language: str = ""
    speed: float = 1.0
    #: 1.0 = original level.  Applied after synthesis with clipping protection.
    volume: float = 1.0
    #: Chunk size in characters; 0 uses the default.
    chunk_chars: int = 0


@dataclass
class GenerationResult:
    """The audio produced by one request."""

    samples: np.ndarray
    sample_rate: int = DEFAULT_SAMPLE_RATE
    chunks: int = 1
    #: Wall clock seconds spent synthesising.
    elapsed_seconds: float = 0.0

    @property
    def duration_seconds(self) -> float:
        if self.sample_rate <= 0:
            return 0.0
        return len(self.samples) / float(self.sample_rate)


def clamp_speed(speed: float) -> float:
    """Clamp to the engine's documented range."""
    try:
        value = float(speed)
    except (TypeError, ValueError):
        return 1.0
    if value != value:  # NaN
        return 1.0
    return max(MIN_SPEED, min(MAX_SPEED, value))


def clamp_volume(volume: float) -> float:
    """Clamp volume to the supported 0-125% range (section 25)."""
    try:
        value = float(volume)
    except (TypeError, ValueError):
        return 1.0
    if value != value:
        return 1.0
    return max(0.0, min(1.25, value))


class KokoroEngine:
    """A single, lazily loaded Kokoro pipeline.

    Not thread-safe by design for *generation*: :meth:`synthesize` holds a lock so
    a second caller waits instead of loading a duplicate model.  Loading and
    unloading are guarded by the same lock.
    """

    def __init__(self,
                 model_path: Optional[Path] = None,
                 voice_dir: Optional[Path] = None,
                 runtime: Optional[str] = None,
                 device: str = "cpu",
                 threads: int = 0) -> None:
        self.model_path = Path(model_path) if model_path else None
        self.voice_dir = Path(voice_dir) if voice_dir else (
            self.model_path.parent if self.model_path else None
        )
        self.runtime = (runtime or "").strip().lower() or None
        self.device = device or "cpu"
        self.threads = max(0, int(threads or 0))
        self._pipeline = None
        self._backend = ""
        self._lock = threading.RLock()
        self._cancel = threading.Event()

    # -- state -----------------------------------------------------------
    @property
    def loaded(self) -> bool:
        return self._pipeline is not None

    @property
    def backend(self) -> str:
        return self._backend

    def request_cancel(self) -> None:
        """Ask an in-flight generation to stop at the next chunk boundary."""
        self._cancel.set()

    def reset_cancel(self) -> None:
        """Clear a previous cancellation.

        Called by the **job runner** when a new job starts - never from inside
        :meth:`synthesize`.  Clearing it there would silently discard a cancel
        the user pressed a moment earlier, and the generation would carry on as
        if nothing happened.
        """
        self._cancel.clear()

    # -- lifecycle -------------------------------------------------------
    def load(self) -> None:
        """Load the model.  Safe to call repeatedly; only loads once."""
        with self._lock:
            if self._pipeline is not None:
                return
            if self.model_path is None or not self.model_path.is_file():
                raise KokoroError(
            (
                        f"Expected the model at {self.model_path}."
                        if self.model_path else "No model path is configured."
                    ),
            actions=((
                        "Open Settings and point the Kokoro model path at the "
                        "downloaded weights, then try again."
                    ),),
            title="The Kokoro model file is missing"
        )
            LOGGER.info("kokoro loading model",
                        extra={"path": str(self.model_path), "runtime": self.runtime or "auto"})
            self._pipeline, self._backend = self._construct()
            LOGGER.info("kokoro loaded", extra={"backend": self._backend})

    def unload(self) -> None:
        """Release the model and its memory (section 52)."""
        with self._lock:
            if self._pipeline is None:
                return
            self._pipeline = None
            self._backend = ""
            LOGGER.info("kokoro unloaded")

    def _construct(self) -> tuple[object, str]:
        """Build the real pipeline for the preferred available backend."""
        errors: list[str] = []

        if self.runtime in (None, "", "onnxruntime"):
            try:
                pipeline, backend = self._construct_onnx()
                return pipeline, backend
            except Exception as error:  # noqa: BLE001 - collected, reported below
                errors.append(f"ONNX Runtime: {error}")

        if self.runtime in (None, "", "torch"):
            try:
                pipeline, backend = self._construct_torch()
                return pipeline, backend
            except Exception as error:  # noqa: BLE001
                errors.append(f"PyTorch: {error}")

        raise KokoroError(
            " ".join(errors) or "No backend could be initialised.",
            actions=((
                "Run System Check to see what is missing, then install the "
                "reported package (usually:  python -m pip install kokoro onnxruntime)."
            ),),
            title="Kokoro could not be started"
        )

    def _construct_onnx(self) -> tuple[object, str]:
        from kokoro_onnx import Kokoro as OnnxKokoro  # local import: optional dep

        voices_file = self._find_voices_file()
        pipeline = OnnxKokoro(str(self.model_path), str(voices_file) if voices_file else None)
        return pipeline, "onnxruntime"

    def _construct_torch(self) -> tuple[object, str]:
        from kokoro import KPipeline  # local import: optional dep

        pipeline = KPipeline(lang_code=self._lang_code_hint(), device=self.device)
        return pipeline, "torch"

    def _find_voices_file(self) -> Optional[Path]:
        if self.voice_dir is None or not self.voice_dir.is_dir():
            return None
        for name in ("voices.bin", "voices.onnx", "voices.pt"):
            candidate = self.voice_dir / name
            if candidate.is_file():
                return candidate
        for entry in sorted(self.voice_dir.iterdir()):
            if entry.is_file() and entry.stem.lower() == "voices":
                return entry
        return None

    def _lang_code_hint(self) -> str:
        return "a"  # American English default; the voice id selects the real one

    # -- generation ------------------------------------------------------
    def synthesize(self,
                   request: GenerationRequest,
                   progress: Optional[Callable[[int, int, str], None]] = None) -> GenerationResult:
        """Generate audio for *request*, chunk by chunk.

        Raises :class:`app.core.errors.JobCancelled` if cancellation is requested
        between chunks, so partial audio is never written to disk.
        """
        text = (request.text or "").strip()
        if not text:
            raise KokoroError(
            "The narration text is empty after preprocessing.",
            actions=("Type or import a script, then generate again.",),
            title="There is no text to speak"
        )
        if not request.voice:
            raise KokoroError(
            "A voice identifier is required before generating audio.",
            actions=("Choose a voice in the Voice panel, then generate again.",),
            title="No voice selected"
        )

        with self._lock:
            self.load()
            import time

            started = time.monotonic()
            chunks = self._split(text, request.chunk_chars)
            pieces: list[np.ndarray] = []
            speed = clamp_speed(request.speed)

            for index, chunk in enumerate(chunks, start=1):
                if self._cancel.is_set():
                    raise JobCancelled(
                        f"Narration generation cancelled after "
                        f"{index - 1} of {len(chunks)} parts."
                    )
                pieces.append(self._synthesize_chunk(chunk, request.voice, speed))
                if progress:
                    progress(index, len(chunks), chunk[:40])

            samples = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)
            samples = apply_volume(samples, request.volume)
            elapsed = time.monotonic() - started
            LOGGER.info("kokoro generated",
                        extra={"voice": request.voice, "chunks": len(chunks),
                               "duration": round(len(samples) / DEFAULT_SAMPLE_RATE, 3)})
            return GenerationResult(samples=samples,
                                    sample_rate=DEFAULT_SAMPLE_RATE,
                                    chunks=len(chunks),
                                    elapsed_seconds=elapsed)

    def _split(self, text: str, chunk_chars: int) -> list[str]:
        from app.tts.preprocess import chunk_text

        limit = int(chunk_chars or DEFAULT_CHUNK_CHARS)
        parts = chunk_text(text, limit)
        return parts or [text]

    def _synthesize_chunk(self, text: str, voice: str, speed: float) -> np.ndarray:
        pipeline = self._pipeline
        if pipeline is None:
            raise KokoroError(
            "Generation was attempted before loading.",
            actions=("Try again; the model loads on the first request.",),
            title="The Kokoro model is not loaded"
        )
        if self._backend == "onnxruntime":
            samples, rate = pipeline.create(audio=text, voice=voice, speed=speed)
            return _to_float32(samples, rate)
        # torch pipeline yields (graphemes, phonemes, audio) tuples
        collected: list[np.ndarray] = []
        rate = DEFAULT_SAMPLE_RATE
        for item in pipeline(text, voice=voice, speed=speed):
            audio = getattr(item, "audio", None)
            if audio is None and isinstance(item, (tuple, list)) and len(item) >= 3:
                audio = item[2]
            if audio is None:
                continue
            chunk = _to_float32(audio, rate)
            collected.append(chunk)
        if not collected:
            raise KokoroError(
            "The engine returned nothing for this text and voice.",
            actions=((
                    "Check the text contains speakable characters, then try again. "
                    "If it keeps happening, choose a different voice."
                ),),
            title="Kokoro produced no audio"
        )
        return np.concatenate(collected)

    # -- self test -------------------------------------------------------
    def self_test(self, text: str = "") -> FriendlyError | GenerationResult:
        """Run the real mini self-test from directive section 50.

        Returns the generated audio on success, or a friendly error explaining
        what failed.  Never returns a bare "looks fine".
        """
        from app.tts.capabilities import SELF_TEST_TEXT

        body = text or SELF_TEST_TEXT
        try:
            return self.synthesize(GenerationRequest(text=body, voice=self._first_voice()))
        except Exception as error:  # noqa: BLE001 - converted to a friendly error
            return FriendlyError(
            str(error),
            actions=((
                    "Open System Check for the full report. The most common causes "
                    "are a missing runtime (install onnxruntime) or missing voice "
                    "files next to the model."
                ),),
            title="The Kokoro self-test failed"
        )

    def _first_voice(self) -> str:
        from app.tts.voices import discover_voices

        catalogue = discover_voices()
        voice = catalogue.default_voice()
        if voice is None:
            raise KokoroError(
            "The Kokoro voice catalogue is empty.",
            actions=("Copy the Kokoro voice files next to the model weights, then retry.",),
            title="No voices are available for the self-test"
        )
        return voice.id


def _to_float32(audio: object, rate: object) -> np.ndarray:
    """Normalise whatever the backend returned into a mono float32 array."""
    if hasattr(audio, "detach"):
        audio = audio.detach().cpu().numpy()  # type: ignore[union-attr]
    array = np.asarray(audio, dtype=np.float32)
    if array.ndim > 1:
        array = array.reshape(-1)
    return np.nan_to_num(array, nan=0.0, posinf=0.0, neginf=0.0)


def apply_volume(samples: np.ndarray, volume: float) -> np.ndarray:
    """Scale with clipping protection (section 25).

    Peaks are limited to 0.99 so a volume above 100% cannot produce hard
    clipping in the final WAV.
    """
    factor = clamp_volume(volume)
    if factor == 0.0:
        return np.zeros_like(samples, dtype=np.float32)
    if factor == 1.0:
        return samples.astype(np.float32, copy=False)
    scaled = samples.astype(np.float32, copy=True) * factor
    peak = float(np.max(np.abs(scaled))) if scaled.size else 0.0
    if peak > 0.99:
        scaled *= 0.99 / peak
    return scaled


__all__ = [
    "DEFAULT_CHUNK_CHARS",
    "DEFAULT_SAMPLE_RATE",
    "GenerationRequest",
    "GenerationResult",
    "KokoroEngine",
    "KokoroError",
    "MAX_SPEED",
    "MIN_SPEED",
    "apply_volume",
    "clamp_speed",
    "clamp_volume",
]
