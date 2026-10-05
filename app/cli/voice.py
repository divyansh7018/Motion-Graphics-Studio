"""``motion-studio script ...`` and ``motion-studio voice ...`` commands.

These exist so the Stage C workflow can be driven without the GUI - for support,
for automated tests and for the end-to-end smoke test (directive section 61).

They share :class:`ProjectService` and the :mod:`app.tts` pipeline with the
interface, so there is no second implementation of import, export, voice
discovery or narration generation.

Exit codes match the rest of the CLI: ``0`` fine, ``1`` problems found,
``2`` blocked (a required component is missing), ``3`` unexpected error.
"""

from __future__ import annotations

from pathlib import Path

from ..core.errors import AppError
from ..core.paths import AppPaths
from ..core.settings import Settings
from ..project.service import ProjectService
from ..tts.audio import format_seconds

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_BLOCKED = 2
EXIT_ERROR = 3


def make_service(paths: AppPaths, settings: Settings) -> ProjectService:
    return ProjectService(paths, settings)


def _open(service: ProjectService, project_path: Path):
    """Open a project by path, printing a friendly error when it fails.

    ``ProjectService.open_project`` returns the project and raises
    :class:`ProjectError` when it cannot, so the failure is turned into a plain
    message here rather than a traceback (section 48).
    """
    from ..project.layout import find_project_file

    resolved = Path(project_path)
    target = resolved if resolved.suffix == ".json" else find_project_file(resolved)
    if target is None:
        print(f"✗ No project.json was found at '{resolved}'.")
        print("  Choose the project folder (the one that contains project.json).")
        return None
    try:
        return service.open_project(target)
    except AppError as error:
        friendly = error.friendly()
        print(f"✗ {friendly.title}")
        print(f"  {friendly.what_happened}")
        for action in friendly.actions:
            print(f"  → {action}")
        return None


# --------------------------------------------------------------------------
# script
# --------------------------------------------------------------------------

def command_script_show(args, paths: AppPaths, settings: Settings) -> int:
    service = make_service(paths, settings)
    project = _open(service, Path(args.project))
    if project is None:
        return EXIT_PROBLEMS
    from ..script.stats import stats_for_text

    stats = stats_for_text(project.script.source_text)
    print(project.script.source_text)
    print()
    print(
        f"{stats.words} words · {stats.characters} characters · "
        f"{stats.sentences} sentences · {stats.paragraphs} paragraphs · "
        f"{stats.duration_label()}"
    )
    return EXIT_OK


def command_script_import(args, paths: AppPaths, settings: Settings) -> int:
    service = make_service(paths, settings)
    project = _open(service, Path(args.project))
    if project is None:
        return EXIT_PROBLEMS

    result = service.import_script_file(Path(args.file))
    if not result.ok:
        print(f"✗ {result.error}")
        return EXIT_PROBLEMS
    if not args.no_save:
        save = service.save(reason="script import")
        if not save.ok:
            print(f"✗ The script was imported but could not be saved: {save.error}")
            return EXIT_PROBLEMS
    for note in result.notes:
        print(f"· {note}")
    print(f"✓ Imported '{args.file}' ({result.encoding}). The script is stored exactly as written.")
    return EXIT_OK


def command_script_export(args, paths: AppPaths, settings: Settings) -> int:
    service = make_service(paths, settings)
    if _open(service, Path(args.project)) is None:
        return EXIT_PROBLEMS

    target = Path(args.file)
    if target.exists() and not args.force:
        print(f"✗ '{target.name}' already exists. Use --force to replace it.")
        return EXIT_PROBLEMS
    if target.exists():
        target.unlink()

    result = service.export_script_file(target, target=args.format, title=args.title or "")
    if not result.ok:
        print(f"✗ {result.error}")
        return EXIT_PROBLEMS
    print(f"✓ Exported {result.bytes_written} bytes to {result.path}.")
    return EXIT_OK


def command_script_convert(args, paths: AppPaths, settings: Settings) -> int:
    """Print the script in another format without changing the project."""
    service = make_service(paths, settings)
    project = _open(service, Path(args.project))
    if project is None:
        return EXIT_PROBLEMS
    from ..script.io import convert

    print(convert(project.script.source_text, args.to, markdown_input=args.markdown_input))
    return EXIT_OK


# --------------------------------------------------------------------------
# voice
# --------------------------------------------------------------------------

def command_voice_list(args, paths: AppPaths, settings: Settings) -> int:
    """List the voices discovered from the installed Kokoro model."""
    from ..tts.capabilities import probe_kokoro
    from ..tts.voices import discover_voices, filter_voices, language_label

    model_dir = Path(args.model_dir) if args.model_dir else paths.kokoro_model_dir
    status = probe_kokoro(model_dir=model_dir, deep_init_check=args.deep)
    catalogue = discover_voices(status=status)

    print(f"Engine   : Kokoro 82M ({'installed' if status.installed else 'not installed'})")
    print(f"Runtime  : {status.runtime.describe}")
    print(f"Model    : {status.model.describe()}")
    print(f"Languages: {len(status.languages)} (from {status.language_source})")
    print(f"Voices   : {catalogue.count}")
    if catalogue.blocker():
        print(f"Blocking : {catalogue.blocker()}")
    print()

    voices = filter_voices(
        catalogue,
        language=args.language or "",
        gender=args.gender or "",
        search=args.search or "",
    )
    if not voices:
        print("No voices match those filters.")
        for instruction in status.instructions:
            print(f"  → {instruction}")
        return EXIT_BLOCKED if catalogue.count == 0 else EXIT_OK

    for voice in voices:
        state = "available" if voice.available else f"UNAVAILABLE ({voice.note})"
        print(f"{voice.id:<16} {language_label(voice.language):<18} {voice.gender:<8} {state}")
    return EXIT_OK


def command_voice_check(args, paths: AppPaths, settings: Settings) -> int:
    """Report what is installed and whether the engine can really initialise."""
    from ..tts.capabilities import probe_kokoro

    model_dir = Path(args.model_dir) if args.model_dir else paths.kokoro_model_dir
    status = probe_kokoro(model_dir=model_dir, deep_init_check=True)

    print(f"Kokoro      : {status.headline()}")
    print(f"Package     : {'installed ' + status.package_version if status.installed else 'not installed'}")
    print(f"Runtime     : {status.runtime.describe}")
    print(f"Model       : {status.model.describe()}")
    print(f"Voices      : {len(status.voices)}")
    print(f"Languages   : {', '.join(status.languages) if status.languages else 'none'}")
    print(f"Phonemiser  : {', '.join(status.phonemizer) if status.phonemizer else 'none found'}")
    print(f"Initialised : {'yes (verified)' if status.verified else 'no'}")
    if status.problems:
        print("\nProblems:")
        for problem in status.problems:
            print(f"  ✗ {problem}")
        print("\nWhat to do:")
        for instruction in status.instructions:
            print(f"  → {instruction}")
        return EXIT_BLOCKED
    return EXIT_OK


def command_voice_preview(args, paths: AppPaths, settings: Settings) -> int:
    """Generate a short preview file (never touches a project)."""
    from ..tts.audio import write_wav
    from ..tts.capabilities import probe_kokoro
    from ..tts.engine import GenerationRequest, KokoroEngine
    from ..tts.preprocess import preprocess

    model_dir = Path(args.model_dir) if args.model_dir else paths.kokoro_model_dir
    status = probe_kokoro(model_dir=model_dir)
    if not status.ready:
        print(f"✗ {status.headline()}")
        for instruction in status.instructions:
            print(f"  → {instruction}")
        return EXIT_BLOCKED
    if args.voice not in status.voices:
        print(f"✗ The voice '{args.voice}' is not in the installed catalogue.")
        print(f"  {len(status.voices)} voices are available; list them with: voice list")
        return EXIT_PROBLEMS

    target = Path(args.output) if args.output else paths.previews_dir / "voice_preview.wav"
    engine = KokoroEngine(model_path=status.model.path, runtime=status.runtime.name or None)
    try:
        cleaned = preprocess(args.text)
        result = engine.synthesize(GenerationRequest(
            text=cleaned.text, voice=args.voice, speed=args.speed, volume=args.volume,
        ))
        info = write_wav(target, result.samples, result.sample_rate)
    except AppError as error:
        print(f"✗ {error.friendly().title}: {error.friendly().what_happened}")
        return EXIT_PROBLEMS
    except Exception as error:  # noqa: BLE001
        print(f"✗ Preview failed: {error}")
        return EXIT_PROBLEMS
    finally:
        engine.unload()

    if not info.valid:
        print(f"✗ The preview audio is not valid: {'; '.join(info.problems)}")
        return EXIT_PROBLEMS
    print(f"✓ Wrote {format_seconds(info.duration_seconds)} of audio to {info.path}")
    if cleaned.changes:
        print(f"  preprocessing: {'; '.join(cleaned.changes)}")
    return EXIT_OK


def command_narration_generate(args, paths: AppPaths, settings: Settings) -> int:
    """Generate narration for a project - one command, one generation."""
    from ..tts.capabilities import probe_kokoro
    from ..tts.engine import KokoroEngine
    from ..tts.narration import NarrationSettings, generate_narration
    from ..tts.preprocess import PreprocessOptions

    service = make_service(paths, settings)
    project = _open(service, Path(args.project))
    if project is None:
        return EXIT_PROBLEMS

    voice = args.voice or project.voice.voice
    language = args.language or project.voice.language
    speed = args.speed if args.speed is not None else float(project.voice.speed or 1.0)
    volume = args.volume if args.volume is not None else float(project.voice.volume or 1.0)

    model_dir = Path(args.model_dir) if args.model_dir else paths.kokoro_model_dir
    status = probe_kokoro(model_dir=model_dir)
    if not status.ready:
        print(f"✗ {status.headline()}")
        for instruction in status.instructions:
            print(f"  → {instruction}")
        return EXIT_BLOCKED
    if voice and voice not in status.voices:
        print(f"✗ The voice '{voice}' is not in the installed catalogue.")
        return EXIT_PROBLEMS

    options = PreprocessOptions(expand_numbers=bool(args.expand_numbers))
    preprocessing = {
        "collapse_spaces": options.collapse_spaces,
        "normalize_newlines": options.normalize_newlines,
        "normalize_typography": options.normalize_typography,
        "strip_markdown": options.strip_markdown,
        "expand_numbers": options.expand_numbers,
        "paragraph_pauses": options.paragraph_pauses,
    }
    narration_settings = NarrationSettings(
        voice=voice, language=language, speed=speed, volume=volume,
        sample_rate=int(project.voice.sample_rate or 24000),
        model_version=status.model.path.name if status.model.path else "",
        preprocessing=preprocessing, preprocess_options=options,
    )

    if args.mode:
        service.set_narration_mode(args.mode)

    engine = KokoroEngine(model_path=status.model.path, runtime=status.runtime.name or None)
    try:
        outcome = generate_narration(project, service.current_layout.root, narration_settings, engine=engine)
    except AppError as error:
        print(f"✗ {error.friendly().title}: {error.friendly().what_happened}")
        return EXIT_PROBLEMS
    finally:
        engine.unload()

    if not outcome.ok:
        friendly = outcome.error
        print(f"✗ {friendly.title if friendly else 'Narration was not generated.'}")
        if friendly:
            print(f"  {friendly.what_happened}")
            for action in friendly.actions:
                print(f"  → {action}")
        return EXIT_PROBLEMS

    for track in outcome.tracks:
        print(f"✓ {track.path} · {format_seconds(track.actual_duration_seconds)} · "
              f"{track.sample_rate} Hz · voice {track.voice}")
    if outcome.preprocessing_notes:
        print(f"  preprocessing: {'; '.join(outcome.preprocessing_notes)}")

    save = service.save(reason="narration generated")
    if not save.ok:
        print(f"! The audio was written but the project could not be saved: {save.error}")
        return EXIT_PROBLEMS
    print(f"✓ {outcome.summary()}")
    return EXIT_OK


def command_narration_status(args, paths: AppPaths, settings: Settings) -> int:
    """Report narration state, including stale and missing files."""
    from ..tts.narration import status_explanation

    service = make_service(paths, settings)
    project = _open(service, Path(args.project))
    if project is None:
        return EXIT_PROBLEMS

    # Built by the service, not here: it resolves the model version from the
    # recorded track, without which the settings hash cannot match and every
    # track would be reported stale even seconds after generating it.
    notes = service.refresh_narration_statuses()

    print(f"Status  : {project.narration.status}")
    print(f"Mode    : {project.narration.mode_label()}")
    print(f"Voice   : {project.voice.voice or 'not selected'} ({project.voice.language})")
    print(f"Summary : {status_explanation(project)}")
    for track in project.narration.tracks:
        print(f"  · {track.describe()}")
        if track.message:
            print(f"      {track.message}")
    for note in notes:
        print(f"! {note}")
    return EXIT_OK if project.narration.status in ("ready", "not_generated") else EXIT_PROBLEMS


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def build_voice_parsers(subparsers) -> None:
    """Register the ``script`` and ``voice`` command groups."""
    script = subparsers.add_parser("script", help="Show, import, export and convert a project script.")
    script_sub = script.add_subparsers(dest="script_command")

    show = script_sub.add_parser("show", help="Print the script with its counts.")
    show.add_argument("project", help="Path to the project folder or project.json.")
    show.set_defaults(func=command_script_show)

    import_p = script_sub.add_parser("import", help="Import a script file into the project.")
    import_p.add_argument("project")
    import_p.add_argument("file", help="A .txt or .md file.")
    import_p.add_argument("--no-save", action="store_true", help="Do not save the project afterwards.")
    import_p.set_defaults(func=command_script_import)

    export_p = script_sub.add_parser("export", help="Export the script to a file.")
    export_p.add_argument("project")
    export_p.add_argument("file")
    export_p.add_argument("--format", default="txt", choices=("txt", "md", "script"))
    export_p.add_argument("--title", default="")
    export_p.add_argument("--force", action="store_true", help="Replace an existing file.")
    export_p.set_defaults(func=command_script_export)

    convert_p = script_sub.add_parser("convert", help="Print the script in another format.")
    convert_p.add_argument("project")
    convert_p.add_argument("--to", default="structured", choices=("plain", "structured", "markdown"))
    convert_p.add_argument("--markdown-input", action="store_true")
    convert_p.set_defaults(func=command_script_convert)

    voice = subparsers.add_parser("voice", help="Inspect the local Kokoro voice engine.")
    voice_sub = voice.add_subparsers(dest="voice_command")

    list_p = voice_sub.add_parser("list", help="List voices discovered from the installed model.")
    list_p.add_argument("--language", default="", help="Filter by language code, e.g. h or a.")
    list_p.add_argument("--gender", default="", choices=("", "female", "male", "unknown"))
    list_p.add_argument("--search", default="")
    list_p.add_argument("--model-dir", default="")
    list_p.add_argument("--deep", action="store_true", help="Also load the model to verify it.")
    list_p.set_defaults(func=command_voice_list)

    check_p = voice_sub.add_parser("check", help="Report the engine, model, voices and languages.")
    check_p.add_argument("--model-dir", default="")
    check_p.set_defaults(func=command_voice_check)

    preview_p = voice_sub.add_parser("preview", help="Speak a short sentence to a WAV file.")
    preview_p.add_argument("--voice", required=True)
    preview_p.add_argument("--text", default="Hello! This is a preview of the selected voice.")
    preview_p.add_argument("--speed", type=float, default=1.0)
    preview_p.add_argument("--volume", type=float, default=1.0)
    preview_p.add_argument("--output", default="")
    preview_p.add_argument("--model-dir", default="")
    preview_p.set_defaults(func=command_voice_preview)

    narration = subparsers.add_parser("narration", help="Generate or inspect project narration.")
    narration_sub = narration.add_subparsers(dest="narration_command")

    gen = narration_sub.add_parser("generate", help="Generate the narration audio for a project.")
    gen.add_argument("project")
    gen.add_argument("--voice", default="")
    gen.add_argument("--language", default="")
    gen.add_argument("--speed", type=float, default=None)
    gen.add_argument("--volume", type=float, default=None)
    gen.add_argument("--mode", default="", choices=("", "full_script", "section_scene"))
    gen.add_argument("--expand-numbers", action="store_true",
                     help="Write numbers and abbreviations out as words.")
    gen.add_argument("--model-dir", default="")
    gen.set_defaults(func=command_narration_generate)

    status_p = narration_sub.add_parser("status", help="Report narration state, staleness and files.")
    status_p.add_argument("project")
    status_p.set_defaults(func=command_narration_status)


def run_script_command(args, paths: AppPaths, settings: Settings) -> int:
    handler = getattr(args, "func", None)
    if handler is None:
        return EXIT_OK
    return int(handler(args, paths, settings))


__all__ = [
    "build_voice_parsers",
    "make_service",
    "run_script_command",
]
