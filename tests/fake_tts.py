"""A test-only stand-in for the Kokoro engine.

This lives in ``tests/`` on purpose.  The application has exactly one TTS engine
(Kokoro 82M) and never falls back to anything else; this double exists only so
the *surrounding* pipeline - preprocessing, job plumbing, WAV writing,
validation, duration measurement, staleness, cancellation - can be tested without
the real model weights.

It behaves like the real engine where it matters:

* ``load()`` / ``unload()`` track state, so "lazy load, single instance" is
  testable.
* ``synthesize()`` returns real float32 samples whose length is derived from the
  text, so written files are genuine WAV audio with a real duration.
* It honours ``request_cancel()`` between chunks, so cancellation is testable.
* It can be told to fail, so error paths are testable.

It is **not** a voice model: the audio is a shaped tone, not speech.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

import numpy as np

from app.core.errors import JobCancelled
from app.tts.engine import (
    DEFAULT_SAMPLE_RATE,
    GenerationRequest,
    GenerationResult,
    clamp_speed,
    clamp_volume,
)
from app.tts.preprocess import chunk_text

#: Rough speaking rate used to size the fake audio: characters per second.
CHARS_PER_SECOND = 14.0


class FakeKokoroEngine:
    """Drop-in replacement for :class:`app.tts.engine.KokoroEngine` in tests."""

    def __init__(self,
                 *,
                 sample_rate: int = DEFAULT_SAMPLE_RATE,
                 fail_with: Optional[Exception] = None,
                 empty_audio: bool = False,
                 load_error: Optional[Exception] = None,
                 min_duration: float = 0.0) -> None:
        self.sample_rate = sample_rate
        self.fail_with = fail_with
        self.empty_audio = empty_audio
        self.load_error = load_error
        self.min_duration = min_duration
        self.loaded = False
        self.load_count = 0
        self.unload_count = 0
        self.synth_calls: list[GenerationRequest] = []
        self.backend = "fake"
        self._cancel = threading.Event()

    # -- lifecycle -------------------------------------------------------
    def load(self) -> None:
        if self.load_error is not None:
            raise self.load_error
        self.load_count += 1
        self.loaded = True

    def unload(self) -> None:
        self.unload_count += 1
        self.loaded = False

    def request_cancel(self) -> None:
        self._cancel.set()

    def reset_cancel(self) -> None:
        self._cancel.clear()

    # -- generation ------------------------------------------------------
    def synthesize(self,
                   request: GenerationRequest,
                   progress: Optional[Callable[[int, int, str], None]] = None) -> GenerationResult:
        self.synth_calls.append(request)
        if not self.loaded:
            self.load()
        if self.fail_with is not None:
            raise self.fail_with
        if not (request.text or "").strip():
            raise ValueError("no text")
        if not request.voice:
            raise ValueError("no voice")

        chunks = chunk_text(request.text, 400) or [request.text]
        pieces: list[np.ndarray] = []
        for index, chunk in enumerate(chunks, start=1):
            if self._cancel.is_set():
                raise JobCancelled(
                    f"Narration generation cancelled after "
                    f"{index - 1} of {len(chunks)} parts."
                )
            pieces.append(self._audio_for(chunk, request))
            if progress:
                progress(index, len(chunks), chunk[:40])

        if self.empty_audio:
            samples = np.zeros(0, dtype=np.float32)
        else:
            samples = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)
        return GenerationResult(samples=samples,
                                sample_rate=self.sample_rate,
                                chunks=len(chunks),
                                elapsed_seconds=0.01)

    def _audio_for(self, text: str, request: GenerationRequest) -> np.ndarray:
        """A tone whose length tracks the text length, so durations are real."""
        speed = clamp_speed(request.speed)
        seconds = max(self.min_duration or 0.25, len(text.strip()) / CHARS_PER_SECOND / max(speed, 0.1))
        count = max(1, int(seconds * self.sample_rate))
        # Voice id changes the pitch so two voices produce measurably
        # different audio - useful for proving a voice change is not reused.
        pitch = 180.0 + 37.0 * (sum(ord(c) for c in request.voice) % 7)
        t = np.arange(count, dtype=np.float32) / self.sample_rate
        envelope = np.minimum(1.0, t * 40.0) * np.minimum(1.0, (seconds - t) * 40.0 + 0.2)
        wave = 0.28 * np.sin(2 * np.pi * pitch * t).astype(np.float32)
        samples = (wave * np.clip(envelope, 0.0, 1.0)).astype(np.float32)
        volume = clamp_volume(request.volume)
        if volume != 1.0:
            samples = samples * volume
        return samples

    # -- helpers for tests ----------------------------------------------
    @property
    def last_request(self) -> Optional[GenerationRequest]:
        return self.synth_calls[-1] if self.synth_calls else None
