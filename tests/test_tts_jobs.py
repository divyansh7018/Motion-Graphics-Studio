"""The TTS job bodies, run with the real Stage A job plumbing.

BUG REGRESSION: every job in :mod:`app.tts.jobs` called
``context.progress.report(fraction, message)``, but Stage A's
``ProgressReporter`` has no ``report`` method - it works in ``current``/``total``
units.  Each TTS job therefore raised ``AttributeError`` the first time it
reported progress, which meant preview and narration could never run through the
job manager at all.  The other suites missed it because they call
``generate_narration`` directly, bypassing the job body.

These tests build a genuine :class:`JobContext` with a genuine
:class:`ProgressReporter`, so a job that reports progress in a way Stage A does
not support fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import app.tts.jobs as tts_jobs
from app.jobs.cancel import CancelToken
from app.jobs.progress import ProgressReporter
from app.jobs.spec import JobContext
from app.project.service import CreateRequest, ProjectService
from app.tts.preprocess import PreprocessOptions
from tests.fake_tts import FakeKokoroEngine

VOICE_IDS = ["af_bella", "am_adam", "hf_alpha", "hm_ishaan"]

PREPROCESSING_KEYS = (
    "collapse_spaces", "normalize_newlines", "normalize_typography",
    "strip_markdown", "expand_numbers", "paragraph_pauses",
)


def preprocessing_dict(options: PreprocessOptions) -> dict:
    return {key: getattr(options, key) for key in PREPROCESSING_KEYS}


@pytest.fixture()
def kokoro_dir(paths) -> Path:
    """A synthetic model folder inside the app's own models directory.

    Built under ``paths.kokoro_model_dir`` because that is where the scan job
    looks; a stray temp folder would prove nothing.
    """
    root = Path(paths.kokoro_model_dir)
    (root / "voices").mkdir(parents=True, exist_ok=True)
    (root / "kokoro-82m-v1.0.onnx").write_bytes(b"ONNXFAKE" * 4096)
    for voice in VOICE_IDS:
        (root / "voices" / f"{voice}.pt").write_bytes(b"VOICEFAKE" * 64)
    return root


@pytest.fixture()
def service(paths, settings, tmp_path: Path) -> ProjectService:
    instance = ProjectService(paths, settings)
    instance.create_project(CreateRequest(name="Job Test", folder=tmp_path / "Job Test"))
    return instance


@pytest.fixture()
def fake_engine(monkeypatch):
    """Route the job's engine factory to the test double."""
    engine = FakeKokoroEngine()
    monkeypatch.setattr(tts_jobs, "build_engine", lambda paths=None, model_path=None: engine)
    return engine


def make_context(service, paths, settings, **payload) -> JobContext:
    body = {
        "project": service.current,
        "project_dir": service.current_layout.root,
        "paths": paths,
    }
    body.update(payload)
    return JobContext(
        job_id="job-under-test",
        key="tts.test",
        cancel=CancelToken("job-under-test"),
        progress=ProgressReporter(),
        settings=settings,
        paths=paths,
        payload=body,
    )


# --------------------------------------------------------------------------
# every job body must run against the real ProgressReporter
# --------------------------------------------------------------------------

def test_the_narration_job_reports_progress_and_writes_audio(service, paths, settings, fake_engine) -> None:
    service.set_script_text("Hello. This is a local narration test.")
    service.set_voice_settings(voice="hf_alpha", language="h")
    options = PreprocessOptions()
    context = make_context(
        service, paths, settings,
        voice="hf_alpha", language="h", speed=1.0, volume=1.0,
        model_version="kokoro-82m-v1.0",
        preprocessing=preprocessing_dict(options), preprocess_options=options,
    )

    result = tts_jobs.narration_job(context)

    assert "narration file generated" in result["summary"]
    written = list((service.current_layout.root / "audio" / "narration").glob("*.wav"))
    assert len(written) == 1
    assert context.progress.progress.fraction == pytest.approx(1.0)


def test_the_voice_scan_job_reports_progress(service, paths, settings, kokoro_dir, monkeypatch) -> None:
    context = make_context(service, paths, settings, model_dir=kokoro_dir)

    result = tts_jobs.voice_scan_job(context)

    assert sorted(voice["id"] for voice in result["voices"]) == sorted(VOICE_IDS)
    assert result["languages"], "languages are discovered from the model, never hard-coded"
    assert context.progress.progress.fraction == pytest.approx(1.0)


def test_the_scan_job_honours_a_configured_model_dir(service, paths, settings, kokoro_dir) -> None:
    """A model dir set in Settings must be the one that is scanned."""
    settings.voice.model_dir = str(kokoro_dir)
    context = make_context(service, paths, settings)

    result = tts_jobs.voice_scan_job(context)

    assert len(result["voices"]) == len(VOICE_IDS)


def test_a_configured_dir_that_does_not_exist_reports_zero_voices(service, paths, settings) -> None:
    """No voice files means no voices - but the languages the installed pipeline
    declares are still reported, and no voice is claimed to work (section 6)."""
    settings.voice.model_dir = str(paths.data_root / "no-such-model")
    context = make_context(service, paths, settings)

    result = tts_jobs.voice_scan_job(context)

    assert result["voices"] == []
    assert all(not voice["available"] for voice in result["voices"])
    assert result["blocker"], "an empty catalogue must explain itself"


def test_the_preview_job_reports_progress_and_writes_a_temp_file(
    service, paths, settings, fake_engine
) -> None:
    context = make_context(
        service, paths, settings,
        voice="hf_alpha", language="h", speed=1.0, volume=1.0,
        text="A short preview sentence.",
    )

    result = tts_jobs.voice_preview_job(context)

    assert Path(result["path"]).is_file()
    assert result["duration_seconds"] > 0
    assert context.progress.progress.fraction == pytest.approx(1.0)


def test_a_cancelled_job_stops_and_leaves_no_file(service, paths, settings, fake_engine) -> None:
    from app.core.errors import JobCancelled

    service.set_script_text("A sentence that will never be spoken.")
    service.set_voice_settings(voice="hf_alpha", language="h")
    options = PreprocessOptions()
    context = make_context(
        service, paths, settings,
        voice="hf_alpha", language="h", speed=1.0, volume=1.0,
        model_version="kokoro-82m-v1.0",
        preprocessing=preprocessing_dict(options), preprocess_options=options,
    )
    context.cancel.cancel("Cancelled by the test.")

    with pytest.raises(JobCancelled):
        tts_jobs.narration_job(context)

    assert not list((service.current_layout.root / "audio" / "narration").glob("*.wav"))


def test_a_failed_generation_becomes_a_failed_job_not_a_silent_success(
    service, paths, settings, monkeypatch
) -> None:
    monkeypatch.setattr(
        tts_jobs, "build_engine",
        lambda paths=None, model_path=None: FakeKokoroEngine(fail_with=RuntimeError("boom")),
    )
    service.set_script_text("A sentence that cannot be spoken.")
    service.set_voice_settings(voice="hf_alpha", language="h")
    options = PreprocessOptions()
    context = make_context(
        service, paths, settings,
        voice="hf_alpha", language="h", speed=1.0, volume=1.0,
        model_version="kokoro-82m-v1.0",
        preprocessing=preprocessing_dict(options), preprocess_options=options,
    )

    with pytest.raises(RuntimeError):
        tts_jobs.narration_job(context)

    assert not list((service.current_layout.root / "audio" / "narration").glob("*.wav"))


# --------------------------------------------------------------------------
# job factories
# --------------------------------------------------------------------------

def test_narration_is_a_single_non_parallel_job(service, paths, settings) -> None:
    from app.jobs.keys import JobKeys

    spec = tts_jobs.narration_spec(service.current, service.current_layout.root, paths=paths)

    assert spec.key == JobKeys.TTS_NARRATION
    assert spec.allow_parallel is False, "one Generate must not run two Kokoro jobs"


def test_preview_uses_its_own_job_key(service, paths, settings) -> None:
    from app.jobs.keys import JobKeys

    spec = tts_jobs.preview_spec(voice="hf_alpha", text="Hello.", paths=paths)

    assert spec.key == JobKeys.VOICE_PREVIEW
    assert spec.key != JobKeys.TTS_NARRATION, "preview must never create a narration job"


# --------------------------------------------------------------------------
# kokoro_init_job - never executed before, the same gap that hid the
# progress.report crash
# --------------------------------------------------------------------------

def test_the_init_job_reports_a_missing_engine_as_not_ready(service, paths, settings, monkeypatch) -> None:
    """Section 4: a probe that cannot initialise must say so, not pretend."""
    import app.tts.capabilities as capabilities

    monkeypatch.setattr(
        capabilities, "probe_package",
        lambda name="kokoro": (False, "", "No module named 'kokoro'"),
    )
    monkeypatch.setattr(capabilities, "_language_codes_from_engine", lambda: {})
    context = make_context(service, paths, settings)

    result = tts_jobs.kokoro_init_job(context)

    assert result["ready"] is False
    assert result["installed"] is False
    assert result["problems"], "the user must be told what is wrong"


def test_the_init_job_loads_and_releases_the_model(service, paths, settings, kokoro_dir, monkeypatch) -> None:
    """Sections 4 and 51: verify it really initialises, then release it."""
    from types import SimpleNamespace

    import app.tts.capabilities as capabilities

    model_file = kokoro_dir / "kokoro-82m-v1.0.onnx"
    monkeypatch.setattr(capabilities, "probe_kokoro", lambda **_kw: SimpleNamespace(
        ready=True, installed=True, package_version="0.9.4",
        model=SimpleNamespace(present=True, path=model_file),
        runtime=SimpleNamespace(name="onnxruntime", version="1.30.0", describe="onnxruntime"),
        voices=["hf_alpha"], languages=["h"], language_source="engine",
        phonemizer=["misaki"], missing_phonemizers=[], problems=[],
        verified=False, headline=lambda: "ready",
    ))

    released = []

    class _Engine:
        backend = "onnxruntime"

        def __init__(self, *a, **k):
            self.loaded = False

        def load(self):
            self.loaded = True

        def unload(self):
            released.append(True)

    monkeypatch.setattr(tts_jobs, "build_engine", lambda paths=None, model_path=None: _Engine())
    context = make_context(service, paths, settings)

    result = tts_jobs.kokoro_init_job(context)

    assert result["ready"] is True
    assert released, "the model must be released after the check (section 51)"
    assert context.progress.progress.fraction == pytest.approx(1.0)


def test_the_init_job_reports_a_model_that_fails_to_load(service, paths, settings, monkeypatch) -> None:
    """A model file that will not load is a failure, not a silent success."""
    from types import SimpleNamespace

    import app.tts.capabilities as capabilities

    monkeypatch.setattr(capabilities, "probe_kokoro", lambda **_kw: SimpleNamespace(
        ready=True, installed=True, package_version="0.9.4",
        model=SimpleNamespace(present=True, path=paths.kokoro_model_dir / "m.onnx"),
        runtime=SimpleNamespace(name="onnxruntime", version="1.30.0", describe="onnxruntime"),
        voices=[], languages=[], language_source="engine",
        phonemizer=[], missing_phonemizers=[], problems=[],
        verified=False, headline=lambda: "ready",
    ))

    class _BrokenEngine:
        backend = ""

        def load(self):
            raise RuntimeError("the ONNX file is truncated")

        def unload(self):
            pass

    monkeypatch.setattr(tts_jobs, "build_engine", lambda paths=None, model_path=None: _BrokenEngine())
    context = make_context(service, paths, settings)

    result = tts_jobs.kokoro_init_job(context)

    assert result["ready"] is False
    assert "truncated" in result["error"]
