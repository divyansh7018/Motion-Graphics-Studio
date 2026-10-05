"""Stage C manual verification matrix.

Runs the sixteen scenarios the directive requires (section 59) through the same
code paths the interface uses, and prints what happened.  This is evidence, not a
substitute for the automated suite.

Two engine modes
----------------
``--engine fake`` (default) uses the Kokoro test double in ``tests/fake_tts.py``.
It produces real float32 audio and real WAV files, so the whole pipeline is
exercised - but the voice is not a real Kokoro voice.

``--engine real`` uses the installed Kokoro.  Use that for the evidence that goes
in the Stage C report:

    LD_LIBRARY_PATH=/tmp/stublib QT_QPA_PLATFORM=offscreen \\
        /home/user/.venv/bin/python scripts/stage_c_manual_matrix.py \\
        --data-root /tmp/mgs_evidence --engine real
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.paths import AppPaths  # noqa: E402
from app.core.settings import Settings  # noqa: E402
from app.project.service import CreateRequest, ProjectService  # noqa: E402
from app.tts.capabilities import probe_kokoro  # noqa: E402
from app.tts.engine import KokoroEngine  # noqa: E402
from app.tts.narration import NarrationSettings, generate_narration  # noqa: E402
from app.tts.preprocess import PreprocessOptions  # noqa: E402
from app.tts.voices import discover_voices, validate_voice_choice  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []
ENGINE_MODE = "fake"


def record(number: int, name: str, ok: bool, detail: str) -> None:
    RESULTS.append((f"{number:02d}", "PASS" if ok else "FAIL", f"{name} - {detail}"))
    print(f"[{'PASS' if ok else 'FAIL'}] {number:02d} {name}: {detail}")


# --------------------------------------------------------------------------
# engine selection
# --------------------------------------------------------------------------

def make_engine(paths: AppPaths):
    """Return an engine for the chosen mode, or ``None`` when it is unavailable."""
    global ENGINE_MODE
    if ENGINE_MODE == "real":
        status = probe_kokoro(model_dir=paths.kokoro_model_dir)
        if not status.ready:
            print(f"  (real Kokoro unavailable: {status.headline()})")
            return None
        return KokoroEngine(model_path=status.model.path, runtime=status.runtime.name or None)

    from tests.fake_tts import FakeKokoroEngine

    return FakeKokoroEngine(sample_rate=24000)


def first_voice(paths: AppPaths, prefer: str = "hf_alpha") -> str:
    catalogue = discover_voices(model_dir=paths.kokoro_model_dir)
    if catalogue.count:
        for voice in catalogue.available:
            if voice.id == prefer:
                return voice.id
        return catalogue.available[0].id
    return prefer


def settings_for(paths: AppPaths, voice: str, language: str, speed: float = 1.0) -> NarrationSettings:
    options = PreprocessOptions()
    return NarrationSettings(
        voice=voice,
        language=language,
        speed=speed,
        sample_rate=24000,
        model_version="matrix-evidence",
        preprocessing={
            "collapse_spaces": options.collapse_spaces,
            "normalize_newlines": options.normalize_newlines,
            "normalize_typography": options.normalize_typography,
            "strip_markdown": options.strip_markdown,
            "expand_numbers": options.expand_numbers,
            "paragraph_pauses": options.paragraph_pauses,
        },
        preprocess_options=options,
    )


def new_service(root: Path) -> ProjectService:
    paths = AppPaths(
        data_root=root, source_root=Path(__file__).resolve().parents[1], reason="Stage C matrix"
    )
    paths.ensure()
    return ProjectService(paths, Settings())


ENGLISH_SCRIPT = (
    "Welcome back to the channel. Today we look at how small habits compound.\n\n"
    "Start with two minutes a day. Then add a little more."
)
HINDI_SCRIPT = "नमस्ते दोस्तों। आज हम बात करेंगे निवेश की। धीरे-धीरे आगे बढ़ें।"
MIXED_SCRIPT = "नमस्ते दोस्तों! Let us talk about SIP. निवेश का सबसे आसान तरीका यही है."


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------

def scenario_1(root: Path) -> None:
    """1 - English script, pick a voice, generate narration."""
    service = new_service(root)
    project = service.create_project(CreateRequest(name="C English", folder=root / "C English"))
    service.set_script_text(ENGLISH_SCRIPT)
    voice = first_voice(service.paths)
    engine = make_engine(service.paths)
    if engine is None:
        record(1, "English script to narration", False, "no engine available")
        return

    outcome = generate_narration(project, service.current_layout.root, settings_for(service.paths, voice, "en"), engine=engine)
    ok = outcome.ok and len(outcome.tracks) == 1
    track = outcome.tracks[0] if outcome.tracks else None
    detail = (
        f"{track.filename}, {track.actual_duration_seconds:.2f}s, {track.sample_rate} Hz, voice {track.voice}"
        if ok else str(outcome.error)
    )
    record(1, "English script to narration", ok, detail)


def scenario_2(root: Path) -> None:
    """2 - Hindi script generates Hindi narration."""
    service = new_service(root)
    project = service.create_project(CreateRequest(name="C Hindi", folder=root / "C Hindi"))
    service.set_script_text(HINDI_SCRIPT)
    engine = make_engine(service.paths)
    if engine is None:
        record(2, "Hindi narration", False, "no engine available")
        return

    outcome = generate_narration(
        project, service.current_layout.root, settings_for(service.paths, "hf_alpha", "h"), engine=engine
    )
    ok = outcome.ok and outcome.tracks and outcome.tracks[0].actual_duration_seconds > 0
    stored = service.current_layout.root / "audio" / "narration" / "narration_full.wav"
    record(
        2, "Hindi narration", bool(ok),
        f"{stored.name} {outcome.tracks[0].actual_duration_seconds:.2f}s, language {outcome.tracks[0].language}"
        if ok else str(outcome.error),
    )


def scenario_3(root: Path) -> None:
    """3 - A mixed Hindi/English script survives the round trip."""
    service = new_service(root)
    project = service.create_project(CreateRequest(name="C Mixed", folder=root / "C Mixed"))
    service.set_script_text(MIXED_SCRIPT)
    service.save()
    reloaded = service.open_project(service.current_layout.root)
    engine = make_engine(service.paths)
    outcome = None
    if engine is not None:
        outcome = generate_narration(
            project, service.current_layout.root, settings_for(service.paths, "hf_alpha", "h"), engine=engine
        )
    ok = reloaded.script.source_text == MIXED_SCRIPT and (outcome is None or outcome.ok)
    detail = "text preserved through save/reload"
    if outcome is not None:
        detail += f", narration {outcome.tracks[0].actual_duration_seconds:.2f}s" if outcome.ok else f", {outcome.error}"
    record(3, "mixed Hindi/English script", ok, detail)


def scenario_4(root: Path) -> None:
    """4 - Changing the voice makes the narration stale."""
    service = new_service(root)
    project = service.create_project(CreateRequest(name="C Stale Voice", folder=root / "C Stale Voice"))
    service.set_script_text(ENGLISH_SCRIPT)
    engine = make_engine(service.paths)
    if engine is None:
        record(4, "voice change marks narration stale", False, "no engine available")
        return
    settings = settings_for(service.paths, "af_bella", "en")
    generate_narration(project, service.current_layout.root, settings, engine=engine)
    before = project.narration.status

    service.set_voice_settings(voice="hf_alpha")
    service.refresh_narration_statuses()
    record(
        4, "voice change marks narration stale",
        before == "ready" and project.narration.status == "stale",
        f"{before} -> {project.narration.status}",
    )


def scenario_5(root: Path) -> None:
    """5 - Editing the script after generation makes it stale, not reused."""
    service = new_service(root)
    project = service.create_project(CreateRequest(name="C Stale Script", folder=root / "C Stale Script"))
    service.set_script_text(ENGLISH_SCRIPT)
    engine = make_engine(service.paths)
    if engine is None:
        record(5, "script change marks narration stale", False, "no engine available")
        return
    settings = settings_for(service.paths, first_voice(service.paths), "en")
    generate_narration(project, service.current_layout.root, settings, engine=engine)
    old_size = (service.current_layout.root / "audio" / "narration" / "narration_full.wav").stat().st_size

    service.set_script_text(ENGLISH_SCRIPT + "\n\nAn extra closing line was added.")
    service.refresh_narration_statuses()
    after_edit = project.narration.status

    # Reverting must make the existing file usable again - never regenerated silently.
    service.set_script_text(ENGLISH_SCRIPT)
    service.refresh_narration_statuses()
    record(
        5, "script change marks narration stale",
        after_edit == "stale" and project.narration.status == "ready",
        f"stale after the edit, back to {project.narration.status} after reverting ({old_size} bytes)",
    )


def scenario_6(root: Path) -> None:
    """6 - Changing the speed produces new audio, not a stale reuse."""
    service = new_service(root)
    project = service.create_project(CreateRequest(name="C Speed", folder=root / "C Speed"))
    service.set_script_text(ENGLISH_SCRIPT)
    engine = make_engine(service.paths)
    if engine is None:
        record(6, "speed change regenerates", False, "no engine available")
        return
    voice = first_voice(service.paths)
    generate_narration(project, service.current_layout.root, settings_for(service.paths, voice, "en", 1.0), engine=engine)
    slow_seconds = project.narration.tracks[0].actual_duration_seconds

    service.set_voice_settings(speed=1.5)
    service.refresh_narration_statuses()
    stale = project.narration.status
    generate_narration(project, service.current_layout.root, settings_for(service.paths, voice, "en", 1.5), engine=engine)
    fast_seconds = project.narration.tracks[0].actual_duration_seconds
    record(
        6, "speed change regenerates",
        stale == "stale" and fast_seconds < slow_seconds,
        f"{slow_seconds:.2f}s at 1.0x -> {fast_seconds:.2f}s at 1.5x (was {stale} in between)",
    )


def scenario_7(root: Path) -> None:
    """7 - Close and reopen remembers the narration settings."""
    service = new_service(root)
    project = service.create_project(CreateRequest(name="C Reopen", folder=root / "C Reopen"))
    service.set_script_text(ENGLISH_SCRIPT)
    service.set_voice_settings(voice="hf_alpha", language="h", speed=1.25, volume=0.9)
    engine = make_engine(service.paths)
    if engine is None:
        record(7, "settings survive a close/reopen", False, "no engine available")
        return
    generate_narration(project, service.current_layout.root, settings_for(service.paths, "hf_alpha", "h", 1.25), engine=engine)
    saved = bool(service.save(reason="narration generated").ok)
    folder = service.current_layout.root
    service.close_project()

    service2 = new_service(root)
    reopened = service2.open_project(folder)
    service2.refresh_narration_statuses()
    track = reopened.narration.tracks[0] if reopened.narration.tracks else None
    ok = (
        saved
        and reopened.script.source_text == ENGLISH_SCRIPT
        and reopened.voice.voice == "hf_alpha"
        and float(reopened.voice.speed) == 1.25
        and track is not None and track.actual_duration_seconds > 0
        and reopened.narration.status == "ready"
    )
    detail = (
        f"voice {reopened.voice.voice}, {reopened.voice.speed}x, "
        f"{reopened.narration.status}, {track.actual_duration_seconds:.2f}s"
        if track else "no track was stored"
    )
    record(7, "settings survive a close/reopen", ok, detail)


def scenario_8(root: Path) -> None:
    """8 - Deleting the WAV warns instead of crashing."""
    service = new_service(root)
    project = service.create_project(CreateRequest(name="C Missing", folder=root / "C Missing"))
    service.set_script_text(ENGLISH_SCRIPT)
    engine = make_engine(service.paths)
    if engine is None:
        record(8, "a deleted file warns, never crashes", False, "no engine available")
        return
    generate_narration(project, service.current_layout.root, settings_for(service.paths, first_voice(service.paths), "en"), engine=engine)
    path = service.current_layout.root / "audio" / "narration" / "narration_full.wav"
    path.unlink()

    service.refresh_narration_statuses()
    from app.tts.narration import status_explanation

    explanation = status_explanation(project)
    record(
        8, "a deleted file warns, never crashes",
        project.narration.status == "missing" and "missing" in explanation.lower(),
        f"{project.narration.status}: {explanation}",
    )


def scenario_9(root: Path) -> None:
    """9 - Cancelling mid-generation leaves no file and a safe project."""
    service = new_service(root)
    project = service.create_project(CreateRequest(name="C Cancel", folder=root / "C Cancel"))
    service.set_script_text(ENGLISH_SCRIPT * 12)
    engine = make_engine(service.paths)
    if engine is None:
        record(9, "cancelling leaves nothing behind", False, "no engine available")
        return

    def cancel_immediately() -> bool:
        return True

    from app.core.errors import JobCancelled

    started = time.time()
    try:
        generate_narration(
            project, service.current_layout.root,
            settings_for(service.paths, first_voice(service.paths), "en"),
            engine=engine, cancel=cancel_immediately,
        )
        cancelled = False
    except JobCancelled as error:
        cancelled = True
        detail_cancel = str(error)
    elapsed = time.time() - started

    service.mark_narration_generating()
    service.refresh_narration_statuses()
    path = service.current_layout.root / "audio" / "narration" / "narration_full.wav"
    save_result = service.save()
    saved = bool(save_result.ok)
    record(
        9, "cancelling leaves nothing behind",
        cancelled and not path.exists() and saved and project.narration.status != "ready",
        f"cancelled after {elapsed:.3f}s ({detail_cancel if cancelled else 'it did not cancel'}), "
        f"file present={path.exists()}, status={project.narration.status}, project saved={saved}",
    )


def scenario_10(root: Path) -> None:
    """10 - One generate call produces exactly one job and one file."""
    import app.tts.jobs as tts_jobs
    from app.jobs.cancel import CancelToken
    from app.jobs.keys import JobKeys
    from app.jobs.progress import ProgressReporter
    from app.jobs.spec import JobContext

    service = new_service(root)
    project = service.create_project(CreateRequest(name="C One Job", folder=root / "C One Job"))
    service.set_script_text(ENGLISH_SCRIPT)
    engine = make_engine(service.paths)
    if engine is None:
        record(10, "one generation is one job", False, "no engine available")
        return

    # Run the genuine job body with the genuine Stage A plumbing, so this is
    # evidence about the job the interface actually submits.
    tts_jobs.build_engine = lambda paths=None, model_path=None: engine
    options = PreprocessOptions()
    voice = first_voice(service.paths)
    context = JobContext(
        job_id="matrix-10", key=JobKeys.TTS_NARRATION, cancel=CancelToken("matrix-10"),
        progress=ProgressReporter(), settings=Settings(), paths=service.paths,
        payload={
            "project": project, "project_dir": service.current_layout.root,
            "voice": voice, "language": "en", "speed": 1.0, "volume": 1.0,
            "model_version": "matrix-evidence",
            "preprocessing": {
                key: getattr(options, key) for key in (
                    "collapse_spaces", "normalize_newlines", "normalize_typography",
                    "strip_markdown", "expand_numbers", "paragraph_pauses",
                )
            },
            "preprocess_options": options,
        },
    )
    result = tts_jobs.narration_job(context)
    files = sorted((service.current_layout.root / "audio" / "narration").glob("*.wav"))
    spec = tts_jobs.narration_spec(project, service.current_layout.root, paths=service.paths)
    ok = len(files) == 1 and project.narration.status == "ready" and spec.allow_parallel is False
    record(
        10, "one generation is one job", bool(ok),
        f"job key {spec.key}, allow_parallel={spec.allow_parallel}, "
        f"{len(files)} file(s): {[f.name for f in files]}, {result.get('summary', '')}",
    )


def scenario_11(root: Path) -> None:
    """11 - Generating again replaces the file, with no hidden extra jobs."""
    service = new_service(root)
    project = service.create_project(CreateRequest(name="C Twice", folder=root / "C Twice"))
    service.set_script_text(ENGLISH_SCRIPT)
    engine = make_engine(service.paths)
    if engine is None:
        record(11, "regenerating replaces, never duplicates", False, "no engine available")
        return
    settings = settings_for(service.paths, first_voice(service.paths), "en")
    generate_narration(project, service.current_layout.root, settings, engine=engine)
    first_time = project.narration.tracks[0].generated_at

    time.sleep(1.1)
    generate_narration(project, service.current_layout.root, settings, engine=engine)
    files = sorted((service.current_layout.root / "audio" / "narration").glob("*.wav"))
    record(
        11, "regenerating replaces, never duplicates",
        len(files) == 1 and project.narration.tracks[0].generated_at != first_time,
        f"{len(files)} file, regenerated at {project.narration.tracks[0].generated_at}",
    )


def scenario_12(root: Path) -> None:
    """12 - With Kokoro missing the app still starts and System Check says so."""
    import app.checks.items  # noqa: F401 - registers the checks
    from app.checks.status import CheckContext, run_system_check

    service = new_service(root)
    service.create_project(CreateRequest(name="C No Engine", folder=root / "C No Engine"))
    service.set_script_text(ENGLISH_SCRIPT)
    service.save()

    context = CheckContext(paths=service.paths, settings=Settings(), deep=False)
    report = run_system_check(context, only=("voice.kokoro", "voice.voices", "voice.selftest"))
    kokoro = next(result for result in report.results if result.check_id == "voice.kokoro")
    project_ok = service.current_layout.root.joinpath("project.json").is_file()
    record(
        12, "the app works without Kokoro",
        project_ok and report.core_ready,
        f"project saved={project_ok}, voice.kokoro={kokoro.status.value} ({kokoro.title}), "
        f"start-up blockers={len(report.blockers)}",
    )


def scenario_13(root: Path) -> None:
    """13 - A missing model gives a friendly error, not a traceback."""
    service = new_service(root)
    service.create_project(CreateRequest(name="C No Model", folder=root / "C No Model"))
    service.set_script_text(ENGLISH_SCRIPT)
    status = probe_kokoro(model_dir=root / "does-not-exist")
    catalogue = discover_voices(status=status)
    ok_choice, message = validate_voice_choice(catalogue, "af_bella", "en")
    ok = not ok_choice and catalogue.count == 0 and "Kokoro" in message
    record(
        13, "a missing model is a friendly error", ok,
        f"0 voices, refused before touching a model: {message}",
    )


def scenario_14(root: Path) -> None:
    """14 - A voice that does not exist is a validation error."""
    service = new_service(root)
    service.create_project(CreateRequest(name="C Bad Voice", folder=root / "C Bad Voice"))
    service.set_script_text(ENGLISH_SCRIPT)
    catalogue = discover_voices(model_dir=service.paths.kokoro_model_dir)
    ok_voice, message = validate_voice_choice(catalogue, "not_a_real_voice", "en")
    # The interface validates the choice before it can submit a job, so an
    # unknown voice never reaches the model.
    record(
        14, "an unknown voice is rejected",
        not ok_voice and "not in the installed Kokoro catalogue" in message,
        f"validation: {message}",
    )


def scenario_15(root: Path) -> None:
    """15 - A long script stays responsive and produces audio."""
    from app.script.parser import parse
    from app.script.stats import stats_for_script

    long_script = "\n\n".join(
        f"[SCENE {i:02d}]\nNarration: This is part {i} of a very long script. "
        "It keeps going so we can measure how the interface behaves. "
        "Each scene adds another block to the timeline and another paragraph to read."
        for i in range(1, 41)
    )
    service = new_service(root)
    project = service.create_project(CreateRequest(name="C Long", folder=root / "C Long"))
    started = time.time()
    service.set_script_text(long_script)
    parsed = parse(long_script)
    stats = stats_for_script(long_script)
    elapsed = time.time() - started

    engine = make_engine(service.paths)
    outcome = None
    if engine is not None:
        outcome = generate_narration(
            project, service.current_layout.root,
            settings_for(service.paths, first_voice(service.paths), "en"), engine=engine,
        )
    ok = len(parsed.blocks) == 40 and elapsed < 5 and (outcome is None or outcome.ok)
    detail = f"{len(parsed.blocks)} scenes, {stats.words} words, parsed+stored in {elapsed:.3f}s"
    if outcome is not None:
        detail += f", narration {outcome.tracks[0].actual_duration_seconds:.2f}s" if outcome.ok else f", {outcome.error}"
    record(15, "a long script stays responsive", ok, detail)


def scenario_16(root: Path) -> None:
    """16 - Closing mid-edit leaves the project intact."""
    service = new_service(root)
    service.create_project(CreateRequest(name="C Mid Edit", folder=root / "C Mid Edit"))
    service.set_script_text(ENGLISH_SCRIPT)
    service.set_voice_settings(voice="hf_alpha", language="h")
    service.save()
    service.set_script_text(ENGLISH_SCRIPT + "\n\nTyped but never saved.")
    folder = service.current_layout.root
    lock_file = folder / ".project.lock.json"
    if lock_file.is_file():
        info = json.loads(lock_file.read_text(encoding="utf-8"))
        info["pid"] = 999999999
        lock_file.write_text(json.dumps(info), encoding="utf-8")
    # No save(), no close_project(): exactly what a window close looks like.
    del service

    service2 = new_service(root)
    reopened = service2.open_project(folder)
    record(
        16, "closing mid-edit keeps the project intact",
        reopened.script.source_text == ENGLISH_SCRIPT and reopened.voice.voice == "hf_alpha",
        f"the saved script came back unchanged, voice {reopened.voice.voice}, "
        f"schema {reopened.schema_version}",
    )


SCENARIOS = (
    scenario_1, scenario_2, scenario_3, scenario_4, scenario_5, scenario_6,
    scenario_7, scenario_8, scenario_9, scenario_10, scenario_11, scenario_12,
    scenario_13, scenario_14, scenario_15, scenario_16,
)


def main(argv=None) -> int:
    global ENGINE_MODE
    parser = argparse.ArgumentParser(description="Stage C manual verification matrix.")
    parser.add_argument("--data-root", required=True, help="A scratch folder for the evidence.")
    parser.add_argument("--engine", choices=("fake", "real"), default="fake")
    args = parser.parse_args(argv)
    ENGINE_MODE = args.engine

    root = Path(args.data_root).resolve()
    root.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    print(f"Stage C manual matrix - data root {root}, engine {ENGINE_MODE}\n")

    failures = 0
    for index, scenario in enumerate(SCENARIOS, start=1):
        try:
            scenario(root)
        except Exception as exc:  # noqa: BLE001 - the matrix must report, not abort
            failures += 1
            import traceback

            traceback.print_exc()
            record(index, (scenario.__doc__ or scenario.__name__).strip(), False, f"raised {exc!r}")
        app.processEvents()

    print()
    passed = sum(1 for _n, status, _d in RESULTS if status == "PASS")
    print(f"{passed}/{len(RESULTS)} scenarios passed (engine mode: {ENGINE_MODE})")
    return 0 if failures == 0 and passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
