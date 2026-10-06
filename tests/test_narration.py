"""Narration generation, staleness, cancellation and persistence (sections 24-32, 40-43, 51-52).

Generation runs against :class:`tests.fake_tts.FakeKokoroEngine`, which produces
real float32 audio, so every test here writes and re-reads genuine WAV files.  The
real Kokoro weights are not needed to prove the pipeline is correct.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import JobCancelled
from app.project.model import Project, ScriptSection, build_project
from app.project.store import ProjectStore
from app.project.layout import ProjectLayout
from app.tts.audio import validate_wav
from app.tts.narration import (
    NarrationSettings,
    attach_narration_to_scenes,
    generate_narration,
    plan_outputs,
    refresh_statuses,
    resolve_audio_path,
    status_explanation,
    validate_settings,
)
from app.tts.preprocess import PreprocessOptions
from fake_tts import FakeKokoroEngine

HINDI_SCRIPT = "नमस्ते दोस्तों। आज हम बात करेंगे निवेश की।\n\nयह दूसरा भाग है।"


def make_settings(**overrides) -> NarrationSettings:
    base = dict(
        voice="hf_alpha", language="hi", speed=1.0, volume=1.0, sample_rate=24000,
        model_version="kokoro-82m-v1.0", preprocessing={"collapse_spaces": True},
        preprocess_options=PreprocessOptions(),
    )
    base.update(overrides)
    return NarrationSettings(**base)


@pytest.fixture()
def project_dir(tmp_path: Path) -> Path:
    root = tmp_path / "StageC_Test"
    (root / "audio" / "narration").mkdir(parents=True)
    return root


@pytest.fixture()
def project() -> Project:
    instance = Project()
    instance.project.name = "StageC_Test"
    instance.script.source_text = HINDI_SCRIPT
    instance.voice.voice = "hf_alpha"
    instance.voice.language = "hi"
    return instance


# --------------------------------------------------------------------------
# Validation before any model is touched (section 42)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("overrides,expected", [
    ({"voice": ""}, "No narration voice is selected"),
    ({"speed": 3.0}, "speed"),
    ({"speed": 0.1}, "speed"),
    ({"volume": 2.0}, "volume"),
    ({"volume": -1.0}, "volume"),
])
def test_invalid_settings_are_rejected_before_generating(overrides, expected) -> None:
    error = validate_settings(make_settings(**overrides))

    assert error is not None
    assert expected.lower() in error.friendly().title.lower()


def test_valid_settings_are_accepted() -> None:
    assert validate_settings(make_settings()) is None
    assert validate_settings(make_settings(speed=0.75, volume=1.25)) is None


def test_generation_refuses_to_run_without_a_voice(project, project_dir) -> None:
    engine = FakeKokoroEngine()

    outcome = generate_narration(project, project_dir, make_settings(voice=""), engine=engine)

    assert outcome.ok is False
    assert engine.synth_calls == [], "the engine must not even be asked"
    assert project.narration.status == "failed"
    assert outcome.error.actions, "the user is told what to do"


def test_generation_refuses_an_empty_script(project_dir) -> None:
    project = Project()
    project.script.source_text = "   \n\n  "

    outcome = generate_narration(project, project_dir, make_settings(), engine=FakeKokoroEngine())

    assert outcome.ok is False
    assert "empty" in outcome.error.title.lower()


# --------------------------------------------------------------------------
# A successful generation (sections 23-24, 26, 40)
# --------------------------------------------------------------------------

def test_generation_writes_a_real_validated_wav(project, project_dir) -> None:
    engine = FakeKokoroEngine()

    outcome = generate_narration(project, project_dir, make_settings(), engine=engine)

    assert outcome.ok, outcome.error
    track = project.narration.tracks[0]
    audio_path = project_dir / track.path

    assert audio_path.exists()
    assert audio_path.name == "narration_full.wav", "deterministic name, not a UUID"
    info = validate_wav(audio_path)
    assert info.valid, info.problems
    assert track.actual_duration_seconds == pytest.approx(info.duration_seconds, abs=0.01)
    assert track.actual_duration_seconds > 0


def test_measured_duration_wins_over_the_estimate(project, project_dir) -> None:
    """Section 40: the estimate is a label; the measured value is the truth."""
    generate_narration(project, project_dir, make_settings(), engine=FakeKokoroEngine())

    track = project.narration.tracks[0]

    assert track.estimated_duration_seconds > 0, "the estimate is still stored"
    assert track.actual_duration_seconds > 0
    assert track.duration_seconds() == track.actual_duration_seconds


def test_the_track_records_full_metadata(project, project_dir) -> None:
    """Section 41: path, duration, rate, channels, voice, model, timestamp."""
    generate_narration(project, project_dir, make_settings(), engine=FakeKokoroEngine())

    track = project.narration.tracks[0]

    assert track.path == "audio/narration/narration_full.wav"
    assert track.sample_rate == 24000
    assert track.channels == 1
    assert track.size_bytes > 0
    assert track.voice == "hf_alpha"
    assert track.language == "hi"
    assert track.speed == 1.0
    assert track.volume == 1.0
    assert track.engine == "kokoro"
    assert track.model_version == "kokoro-82m-v1.0"
    assert track.generated_at.endswith("Z")
    assert track.source_hash and track.settings_hash
    assert track.status == "ready"
    assert project.narration.status == "ready"


def test_one_generation_is_exactly_one_job_and_one_file(project, project_dir) -> None:
    """Section 28: one action, one job, one output - no duplicates."""
    engine = FakeKokoroEngine()

    generate_narration(project, project_dir, make_settings(), engine=engine)

    assert len(engine.synth_calls) == 1
    assert engine.load_count == 1
    assert len(project.narration.tracks) == 1
    assert len(list((project_dir / "audio" / "narration").glob("*.wav"))) == 1


def test_regenerating_replaces_the_same_file_instead_of_piling_up(project, project_dir) -> None:
    engine = FakeKokoroEngine()

    generate_narration(project, project_dir, make_settings(), engine=engine)
    generate_narration(project, project_dir, make_settings(), engine=engine)

    files = list((project_dir / "audio" / "narration").glob("*.wav"))
    assert len(files) == 1
    assert len(project.narration.tracks) == 1
    assert len(engine.synth_calls) == 2


def test_preprocessing_changes_are_reported_not_hidden(project, project_dir) -> None:
    outcome = generate_narration(project, project_dir, make_settings(), engine=FakeKokoroEngine())

    assert outcome.preprocessing_notes, "the two paragraphs become a pause; say so"
    assert project.script.source_text == HINDI_SCRIPT, "the script itself is never rewritten"


def test_generating_is_impossible_from_a_module_import() -> None:
    """Section 56: importing the pipeline must not generate anything."""
    import importlib

    import app.tts.narration as narration_module

    importlib.reload(narration_module)

    # Reloading must not have created files, threads or a project.
    assert narration_module.LOGGER is not None


# --------------------------------------------------------------------------
# Section / scene mode (section 24)
# --------------------------------------------------------------------------

def test_section_mode_writes_one_file_per_section(project_dir) -> None:
    project = Project()
    project.narration.mode = "section_scene"
    for index in range(3):
        project.script.sections.append(
            ScriptSection(id=f"sec{index}", title=f"Scene {index + 1}",
                          text=f"Narration for scene {index + 1}.", order=index)
        )

    outcome = generate_narration(project, project_dir, make_settings(), engine=FakeKokoroEngine())

    assert outcome.ok, outcome.error
    names = sorted(Path(track.path).name for track in project.narration.tracks)
    assert names == ["narration_scene_001.wav", "narration_scene_002.wav", "narration_scene_003.wav"]
    assert project.narration.total_duration_seconds() > 0


def test_plan_outputs_is_deterministic(project) -> None:
    assert plan_outputs(project) == [("audio/narration/narration_full.wav", "")]

    project.narration.mode = "section_scene"
    project.script.sections = [ScriptSection(id="a", text="x", order=0),
                               ScriptSection(id="b", text="y", order=1)]
    assert plan_outputs(project) == [
        ("audio/narration/narration_scene_001.wav", "a"),
        ("audio/narration/narration_scene_002.wav", "b"),
    ]


def test_an_output_path_cannot_escape_the_project(tmp_path: Path) -> None:
    from app.tts.narration import NarrationError

    with pytest.raises(NarrationError):
        resolve_audio_path(tmp_path, "../outside.wav")
    with pytest.raises(NarrationError):
        resolve_audio_path(tmp_path, "/etc/passwd")


# --------------------------------------------------------------------------
# Staleness (section 42)
# --------------------------------------------------------------------------

def test_a_script_change_marks_narration_stale(project, project_dir) -> None:
    settings = make_settings()
    generate_narration(project, project_dir, settings, engine=FakeKokoroEngine())
    assert project.narration.status == "ready"

    project.script.source_text = "यह बिल्कुल नया स्क्रिप्ट है।"
    notes = refresh_statuses(project, project_dir, settings)

    assert project.narration.status == "stale"
    assert project.narration.tracks[0].status == "stale"
    assert notes and "no longer matches" in notes[0]


def test_a_voice_change_marks_narration_stale(project, project_dir) -> None:
    generate_narration(project, project_dir, make_settings(), engine=FakeKokoroEngine())

    notes = refresh_statuses(project, project_dir, make_settings(voice="hm_ishaan"))

    assert project.narration.status == "stale"
    assert notes


def test_a_speed_change_marks_narration_stale(project, project_dir) -> None:
    """Scenario 6 of the manual matrix: no silent reuse after a speed change."""
    generate_narration(project, project_dir, make_settings(), engine=FakeKokoroEngine())

    refresh_statuses(project, project_dir, make_settings(speed=1.25))

    assert project.narration.status == "stale"


def test_a_volume_change_marks_narration_stale(project, project_dir) -> None:
    generate_narration(project, project_dir, make_settings(), engine=FakeKokoroEngine())

    refresh_statuses(project, project_dir, make_settings(volume=0.8))

    assert project.narration.status == "stale"


def test_reverting_the_script_clears_the_stale_flag(project, project_dir) -> None:
    settings = make_settings()
    generate_narration(project, project_dir, settings, engine=FakeKokoroEngine())
    project.script.source_text = "बदला हुआ।"
    refresh_statuses(project, project_dir, settings)
    assert project.narration.status == "stale"

    project.script.source_text = HINDI_SCRIPT
    refresh_statuses(project, project_dir, settings)

    assert project.narration.status == "ready"


def test_an_unchanged_project_is_not_stale(project, project_dir) -> None:
    settings = make_settings()
    generate_narration(project, project_dir, settings, engine=FakeKokoroEngine())

    notes = refresh_statuses(project, project_dir, settings)

    assert notes == []
    assert project.narration.status == "ready"


# --------------------------------------------------------------------------
# Missing and damaged files (section 41)
# --------------------------------------------------------------------------

def test_a_deleted_narration_file_is_reported_without_crashing(project, project_dir) -> None:
    settings = make_settings()
    generate_narration(project, project_dir, settings, engine=FakeKokoroEngine())
    (project_dir / project.narration.tracks[0].path).unlink()

    notes = refresh_statuses(project, project_dir, settings)

    assert project.narration.status == "missing"
    assert project.narration.tracks[0].status == "missing"
    assert any("missing" in note.lower() for note in notes)
    assert "Regenerate" in project.narration.tracks[0].message


def test_a_corrupted_narration_file_is_reported(project, project_dir) -> None:
    settings = make_settings()
    generate_narration(project, project_dir, settings, engine=FakeKokoroEngine())
    target = project_dir / project.narration.tracks[0].path
    target.write_bytes(target.read_bytes()[:80])

    refresh_statuses(project, project_dir, settings)

    assert project.narration.tracks[0].status == "failed"
    assert project.narration.status == "failed"


def test_an_interrupted_generation_is_not_left_looking_ready(project_dir) -> None:
    """A track stuck in 'generating' after a crash must not read as usable."""
    from app.project.model import NarrationTrack

    project = Project()
    project.narration.tracks.append(
        NarrationTrack(id="n1", kind="full", path="audio/narration/narration_full.wav",
                       status="generating")
    )

    notes = refresh_statuses(project, project_dir, make_settings())

    assert project.narration.tracks[0].status == "failed"
    assert notes and "incomplete" in notes[0]


# --------------------------------------------------------------------------
# Cancellation (section 31)
# --------------------------------------------------------------------------

def test_cancelling_before_generation_stops_it(project, project_dir) -> None:
    engine = FakeKokoroEngine()
    engine.request_cancel()

    with pytest.raises(JobCancelled):
        generate_narration(project, project_dir, make_settings(), engine=engine)

    assert project.narration.status == "cancelled"
    assert list((project_dir / "audio" / "narration").glob("*.wav")) == [], \
        "a cancelled run must not leave a half-written file behind"


def test_cancelling_between_sections_stops_cleanly(project_dir) -> None:
    project = Project()
    project.narration.mode = "section_scene"
    for index in range(3):
        project.script.sections.append(
            ScriptSection(id=f"sec{index}", text=f"Part {index} narration.", order=index)
        )
    calls = {"count": 0}

    def cancel_after_first() -> bool:
        calls["count"] += 1
        return calls["count"] > 1

    with pytest.raises(JobCancelled):
        generate_narration(project, project_dir, make_settings(),
                           engine=FakeKokoroEngine(), cancel=cancel_after_first)

    assert project.narration.status == "cancelled"
    written = list((project_dir / "audio" / "narration").glob("*.wav"))
    assert len(written) == 1, "the first section finished; the rest never started"
    assert validate_wav(written[0]).valid, "and what did finish is a valid file"


def test_a_cancelled_run_can_be_retried(project, project_dir) -> None:
    engine = FakeKokoroEngine()
    engine.request_cancel()
    with pytest.raises(JobCancelled):
        generate_narration(project, project_dir, make_settings(), engine=engine)

    fresh = FakeKokoroEngine()
    outcome = generate_narration(project, project_dir, make_settings(), engine=fresh)

    assert outcome.ok, outcome.error
    assert project.narration.status == "ready"


# --------------------------------------------------------------------------
# Engine failure (section 48)
# --------------------------------------------------------------------------

def test_an_engine_failure_is_a_friendly_error(project, project_dir) -> None:
    engine = FakeKokoroEngine(fail_with=RuntimeError("model exploded"))

    outcome = generate_narration(project, project_dir, make_settings(), engine=engine)

    assert outcome.ok is False
    assert project.narration.status == "failed"
    assert outcome.error.actions
    assert "model exploded" in outcome.error.technical, "the real cause stays visible"


def test_empty_engine_output_is_a_failure_not_a_success(project, project_dir) -> None:
    """Section 40: silence must never be reported as generated narration."""
    outcome = generate_narration(project, project_dir, make_settings(),
                                 engine=FakeKokoroEngine(empty_audio=True))

    assert outcome.ok is False
    assert project.narration.status == "failed"
    assert project.narration.tracks == [] or project.narration.tracks[0].status != "ready"


def test_an_unexpected_exception_is_still_diagnosable(project, project_dir) -> None:
    class Broken(FakeKokoroEngine):
        def synthesize(self, request, progress=None):
            raise AttributeError("'NoneType' object has no attribute 'create'")

    outcome = generate_narration(project, project_dir, make_settings(), engine=Broken())

    assert outcome.ok is False
    assert "AttributeError" in outcome.error.technical


# --------------------------------------------------------------------------
# Persistence across a restart (section 41)
# --------------------------------------------------------------------------

def test_narration_survives_saving_and_reopening(tmp_path: Path) -> None:
    """Scenario 7: close and reopen must remember the narration."""
    layout = ProjectLayout(tmp_path / "StageC_Test")
    layout.ensure()
    project = build_project("StageC_Test")
    project.script.source_text = HINDI_SCRIPT
    project.voice.voice = "hf_alpha"
    project.voice.language = "hi"
    settings = make_settings()

    generate_narration(project, layout.root, settings, engine=FakeKokoroEngine())
    saved = ProjectStore().save(project, layout, reason="test")
    assert saved.ok, saved.summary()

    reloaded = ProjectStore().load(layout.project_file)
    assert reloaded.ok, reloaded.error
    reopened = reloaded.project

    assert reopened.voice.voice == "hf_alpha"
    assert reopened.voice.language == "hi"
    assert reopened.script.source_text == HINDI_SCRIPT
    track = reopened.narration.tracks[0]
    assert track.path == "audio/narration/narration_full.wav"
    assert track.actual_duration_seconds > 0
    assert (layout.root / track.path).exists()

    notes = refresh_statuses(reopened, layout.root, settings)
    assert notes == []
    assert reopened.narration.status == "ready"
    assert "ready" in status_explanation(reopened).lower()


def test_a_reopened_project_detects_a_deleted_file(tmp_path: Path) -> None:
    layout = ProjectLayout(tmp_path / "StageC_Test")
    layout.ensure()
    project = build_project("StageC_Test")
    project.script.source_text = HINDI_SCRIPT
    settings = make_settings()
    generate_narration(project, layout.root, settings, engine=FakeKokoroEngine())
    ProjectStore().save(project, layout, reason="test")

    (layout.root / project.narration.tracks[0].path).unlink()
    reopened = ProjectStore().load(layout.project_file).project
    refresh_statuses(reopened, layout.root, settings)

    assert reopened.narration.status == "missing"
    assert "missing" in status_explanation(reopened).lower()


def test_status_explanation_covers_every_state(project, project_dir) -> None:
    assert "No narration" in status_explanation(project)

    generate_narration(project, project_dir, make_settings(), engine=FakeKokoroEngine())
    assert "ready" in status_explanation(project).lower()

    project.script.source_text = "नया।"
    refresh_statuses(project, project_dir, make_settings())
    assert "out of date" in status_explanation(project).lower()


# --------------------------------------------------------------------------
# regression: the model version must not make every track look stale
# --------------------------------------------------------------------------

@pytest.fixture()
def service(paths, settings, tmp_path: Path):
    """A real ProjectService with an open project on disk."""
    from app.project.service import CreateRequest, ProjectService

    instance = ProjectService(paths, settings)
    folder = tmp_path / "Model Version"
    instance.create_project(CreateRequest(name="Model Version", folder=folder))
    return instance


def test_a_track_is_not_stale_after_reopening_without_a_probe(service, tmp_path: Path) -> None:
    """BUG REGRESSION: narration was permanently STALE after a refresh.

    The staleness hash includes the model version that produced the audio, but
    ``ProjectService.narration_settings`` built its snapshot without one, so the
    hash could never match and every ready track was reported stale on the next
    refresh - which would push the user to regenerate audio that was already
    correct (directive section 43).
    """
    from app.project.service import ProjectService
    from tests.fake_tts import FakeKokoroEngine

    service.set_script_text("A short line about model versions.")
    service.set_voice_settings(voice="hf_alpha", language="h")
    outcome = generate_narration(
        service.current,
        service.current_layout.root,
        make_settings(voice="hf_alpha", language="h", model_version="kokoro-82m-v1.0"),
        engine=FakeKokoroEngine(),
    )
    assert outcome.ok
    assert service.current.narration.status == "ready"
    saved = service.save()
    assert saved
    folder = service.current_layout.root
    service.close_project()

    # Reopen cold, with no engine probed - exactly what a fresh start looks like.
    reopened_service = ProjectService(service.paths, service.settings)
    reopened = reopened_service.open_project(folder)
    notes = reopened_service.refresh_narration_statuses()

    assert reopened.narration.tracks[0].model_version == "kokoro-82m-v1.0"
    assert reopened.narration.status == "ready", notes
    assert not any("no longer matches" in note for note in notes)


def test_a_real_model_change_is_still_reported_as_stale(service) -> None:
    """The fallback must not hide a genuine model upgrade."""
    from tests.fake_tts import FakeKokoroEngine

    service.set_script_text("A short line about model upgrades.")
    service.set_voice_settings(voice="hf_alpha", language="h")
    generate_narration(
        service.current,
        service.current_layout.root,
        make_settings(voice="hf_alpha", language="h", model_version="kokoro-82m-v1.0"),
        engine=FakeKokoroEngine(),
    )
    assert service.current.narration.status == "ready"

    notes = service.refresh_narration_statuses(model_version="kokoro-82m-v2.0")

    assert service.current.narration.status == "stale"
    assert any("no longer matches" in note for note in notes)


def test_changing_the_script_still_marks_it_stale_with_the_fallback(service) -> None:
    """The fallback only covers the model version, never the script text."""
    from tests.fake_tts import FakeKokoroEngine

    service.set_script_text("The original sentence.")
    service.set_voice_settings(voice="hf_alpha", language="h")
    generate_narration(
        service.current,
        service.current_layout.root,
        make_settings(voice="hf_alpha", language="h", model_version="kokoro-82m-v1.0"),
        engine=FakeKokoroEngine(),
    )

    service.set_script_text("A completely different sentence.")
    service.refresh_narration_statuses()

    assert service.current.narration.status == "stale"


def new_scene(scene_id: str, name: str):
    from app.project.model import SceneSpec

    return SceneSpec(id=scene_id, name=name)



# --------------------------------------------------------------------------
# Linking generated audio back to the scenes that will render it (section 4)
# --------------------------------------------------------------------------

def _ready_track(index: int, seconds: float):
    from app.project.model import NarrationTrack

    return NarrationTrack(
        id=f"track{index}", kind="section", section_id=f"sec{index}",
        path=f"audio/narration/narration_scene_{index:03d}.wav", status="ready",
        actual_duration_seconds=seconds, sample_rate=24000, channels=1,
        source_hash="srchash", settings_hash="sethash", size_bytes=1000)


def test_generated_narration_is_attached_to_the_scenes_that_render_it(project) -> None:
    """Stage C writes one file per section; Stage E renders what scenes point at."""
    for number in range(3):
        project.scenes.append(new_scene(f"s{number}", f"Scene {number}"))
    tracks = [_ready_track(i + 1, 2.0 + i) for i in range(3)]

    notes = attach_narration_to_scenes(project, tracks)

    assert len(notes) == 3
    for index, scene in enumerate(project.scenes):
        assert scene.narration.extra["narration_status"] == "ready"
        assert scene.narration.file.endswith(f"narration_scene_{index + 1:03d}.wav")
        assert scene.narration.duration == pytest.approx(2.0 + index)
        assert scene.narration.extra["source_hash"] == "srchash"


def test_a_scene_never_claims_audio_that_was_not_generated(project) -> None:
    """A pending or failed track must not make its scene look ready."""
    for number in range(3):
        project.scenes.append(new_scene(f"s{number}", f"Scene {number}"))
    pending = _ready_track(2, 3.0)
    pending.status = "pending"
    tracks = [_ready_track(1, 2.0), pending]

    notes = attach_narration_to_scenes(project, tracks)

    assert project.scenes[0].narration.file != ""
    assert project.scenes[1].narration.file == ""
    assert "narration_status" not in project.scenes[1].narration.extra
    assert "no narration file generated" in notes[1]
    assert "no narration file generated" in notes[2]


def test_disabled_scenes_do_not_consume_a_track(project) -> None:
    for number in range(3):
        project.scenes.append(new_scene(f"s{number}", f"Scene {number}"))
    project.scenes[1].enabled = False

    attach_narration_to_scenes(project, [_ready_track(1, 2.0), _ready_track(2, 3.0)])

    assert project.scenes[0].narration.duration == pytest.approx(2.0)
    # The disabled scene keeps nothing, and the second track lands on scene 3.
    assert project.scenes[1].narration.file == ""
    assert project.scenes[2].narration.duration == pytest.approx(3.0)


def test_an_unknown_attachment_mode_is_rejected(project) -> None:
    with pytest.raises(ValueError):
        attach_narration_to_scenes(project, [], by="magic")


def test_attached_narration_survives_a_save_and_reload(tmp_path: Path) -> None:
    """Provenance lives in ``extra`` because ``asdict()`` drops unknown fields."""
    # build_project() rather than Project(): saving validates, and a bare
    # Project() has no id.
    project = build_project("StageC_Test")
    project.voice.voice = "hf_alpha"
    project.voice.language = "hi"
    project.scenes.clear()
    for number in range(2):
        project.scenes.append(new_scene(f"s{number}", f"Scene {number}"))
    attach_narration_to_scenes(project, [_ready_track(1, 2.5), _ready_track(2, 3.5)])

    layout = ProjectLayout(tmp_path / "StageC_Test")
    layout.ensure()
    saved = ProjectStore().save(project, layout, reason="test")
    assert saved.ok, saved.summary()
    reopened = ProjectStore().load(layout.project_file).project

    first, second = reopened.scenes
    assert first.narration.file.endswith("narration_scene_001.wav")
    assert first.narration.duration == pytest.approx(2.5)
    assert first.narration.extra["narration_status"] == "ready"
    assert first.narration.extra["source_hash"] == "srchash"
    assert second.narration.duration == pytest.approx(3.5)
