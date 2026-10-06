"""``motion-studio render|audio|subtitles ...`` commands (Stage E, section 67).

These call the *same* services the GUI calls - :class:`RenderService`,
:class:`AudioService`, :class:`SubtitleService` - so a render started from a
terminal is byte-for-byte the same pipeline as one started from a button.  There
is no second implementation of validation, mixing or encoding here.

Sub-commands
------------
``render run``         render the project to a finished video
``render validate``    check a project without rendering anything
``render preview``     show what a render would do (length, output, estimates)
``render status``      show the render history and encoder capabilities
``render cancel``      cancel a running render
``audio validate``     check the audio setup
``audio mix``          mix the master audio to a file you can listen to
``subtitles export``   write the captions to .srt / .vtt
"""

from __future__ import annotations

from pathlib import Path

from ..audio.service import AudioService
from ..core.paths import AppPaths
from ..core.settings import Settings
from ..project.layout import find_project_file
from ..project.service import ProjectService
from ..render import CANCELLED, COMPLETED, RenderService
from ..scene.timing import build_timeline
from ..subtitles.service import generate_cues, to_srt, to_vtt, write_subtitle_file

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_BLOCKED = 2


def _open_service(args, paths: AppPaths, settings: Settings) -> ProjectService:
    """Open the requested project (or the most recent one)."""
    service = ProjectService(paths, settings)
    project_path = getattr(args, "project", None)
    if project_path:
        service.open_project(find_project_file(project_path))
    else:
        recent = service.list_recent()
        if not recent:
            raise ValueError("No project is open. Pass --project, or create one first.")
        service.open_project(recent[0].path)
    return service


def _project_dir(service: ProjectService) -> Path:
    layout = getattr(getattr(service, "session", None), "layout", None)
    root = getattr(layout, "root", None)
    return Path(root) if root else Path.cwd()


def _render_service(args, paths: AppPaths, settings: Settings):
    """The render service for a CLI call, plus the project it belongs to."""
    from ..tools.ffmpeg import FFmpegTools, discover_ffmpeg

    service = _open_service(args, paths, settings)
    project_dir = _project_dir(service)
    return service, RenderService(FFmpegTools(discover_ffmpeg()), project_dir=project_dir,
                                  paths=paths)


def _print_issues(title: str, issues) -> None:
    if not issues:
        return
    print(f"\n{title}")
    for issue in issues:
        code = getattr(issue, "code", "")
        message = getattr(issue, "message", str(issue))
        what_to_do = getattr(issue, "what_to_do", "")
        print(f"  [{code}] {message}")
        if what_to_do:
            print(f"        -> {what_to_do}")


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

def _overrides_from_args(args) -> dict:
    """Command-line export overrides.  Only what the user actually typed."""
    overrides: dict = {}
    for name in ("width", "height", "fps", "crf", "bitrate_kbps", "quality_preset",
                 "codec", "container", "encoder_preset"):
        value = getattr(args, name, None)
        if value is not None:
            overrides[name] = value
    preset = getattr(args, "preset", None)
    if preset:
        overrides["_platform"] = preset
    return overrides


def command_render_run(args, paths: AppPaths, settings: Settings) -> int:
    service, renders = _render_service(args, paths, settings)
    project = service.current
    overrides = _overrides_from_args(args)
    platform = overrides.pop("_platform", None)
    if platform:
        changes = renders.apply_platform(project, platform)
        if changes:
            print(f"Applying the '{platform}' preset: "
                  + ", ".join(f"{key} {old} -> {new}" for key, (old, new) in changes.items()))

    plan = renders.plan(project, overrides=overrides)
    print(f"Project   : {project.project.name}")
    print(f"Timeline  : {plan.duration:.2f}s, {plan.frames} frames at {plan.fps} fps")
    print(f"Output    : {plan.output.path if plan.output else '(unknown)'}")
    print(f"Estimate  : {plan.size_estimate.get('label', '')}")
    print(f"            {plan.time_estimate.get('label', '')}")

    if plan.errors and not args.force:
        _print_issues("Cannot render until these are fixed:", plan.errors)
        _print_issues("Warnings:", plan.warnings)
        return EXIT_BLOCKED
    if plan.errors:
        print("\n--force was given, so the render will be attempted anyway.")
    _print_issues("Warnings:", plan.warnings)

    last = {"percent": -1.0}

    def show(progress):
        bucket = int(progress.percent // 5) * 5
        if bucket != last["percent"]:
            last["percent"] = bucket
            extra = ""
            if progress.frames_total:
                extra = f" ({progress.frames_done}/{progress.frames_total} frames)"
            print(f"  {progress.state:<10} {progress.percent:5.1f}%  {progress.message}{extra}")

    print("\nRendering...")
    result = renders.render(project, overrides=overrides, progress=show,
                            burn_subtitles=None if args.burn_subtitles is None
                            else bool(args.burn_subtitles),
                            quick_qc=args.quick_qc, resume=not args.fresh)

    if result.status == COMPLETED:
        print(f"\nRendered  : {result.path}")
        if result.qc is not None:
            print(f"Quality   : {result.qc.verdict}")
            if result.qc.measured:
                measured = result.qc.measured
                print(f"Measured  : {measured.get('width')}x{measured.get('height')} @ "
                      f"{measured.get('fps')} fps, {measured.get('duration')}s, "
                      f"{measured.get('video_codec')}, audio "
                      f"{measured.get('audio_codec') or 'none'}")
            _print_issues("Quality notes:", result.qc.issues)
        print(f"Took      : {result.seconds:.1f}s")
        return EXIT_OK if (result.qc is None or not result.qc.has_fail) else EXIT_PROBLEMS

    if result.status == CANCELLED:
        print("\nCancelled. Nothing was overwritten.")
        return EXIT_OK

    print(f"\nThe render did not finish: {result.message}")
    if result.what_to_do:
        print(f"What to do: {result.what_to_do}")
    _print_issues("Problems:", result.errors)
    if args.verbose and result.technical:
        print(f"\nTechnical detail:\n{result.technical}")
    return EXIT_BLOCKED


def command_render_validate(args, paths: AppPaths, settings: Settings) -> int:
    _service, renders = _render_service(args, paths, settings)
    project = _service.current
    errors, warnings = renders.validate(project)
    _print_issues("Errors (these stop a render):", errors)
    _print_issues("Warnings (a render will continue):", warnings)
    if not errors and not warnings:
        print("No problems found. This project is ready to render.")
    else:
        print(f"\n{len(errors)} error(s), {len(warnings)} warning(s).")
    return EXIT_OK if not errors else EXIT_PROBLEMS


def command_render_preview(args, paths: AppPaths, settings: Settings) -> int:
    _service, renders = _render_service(args, paths, settings)
    project = _service.current
    plan = renders.plan(project, overrides=_overrides_from_args(args))

    print(f"Project    : {project.project.name}")
    print(f"Length     : {plan.duration:.2f}s")
    print(f"Frames     : {plan.frames} at {plan.fps} fps")
    print(f"Segments   : {plan.segments} ({plan.transitions} transition(s))")
    print(f"Resolution : {plan.resolution}")
    print(f"Quality    : {plan.quality}")
    if plan.output is not None:
        print(f"Output     : {plan.output.path}")
    print(f"File size  : {plan.size_estimate.get('label', 'unknown')}")
    print(f"Render time: {plan.time_estimate.get('label', 'unknown')}")
    print(f"Ready      : {'yes' if plan.ready else 'no'}")
    _print_issues("Errors:", plan.errors)
    _print_issues("Warnings:", plan.warnings)
    return EXIT_OK if plan.ready else EXIT_PROBLEMS


def command_render_status(args, paths: AppPaths, settings: Settings) -> int:
    _service, renders = _render_service(args, paths, settings)
    caps = renders.capabilities()
    print("Encoder capabilities (read from this machine's FFmpeg)")
    print(f"  FFmpeg    : {caps.ffmpeg_version or 'not found'}")
    print(f"  Detected  : {'yes' if caps.detected else 'no - install FFmpeg to render'}")
    for codec in ("h264_cpu", "hevc_cpu", "vp9_cpu"):
        print(f"  {codec:<10}: {'available' if caps.supports_codec(codec) else 'NOT AVAILABLE'}")

    history = renders.history()
    print(f"\nRender history ({len(history)} recorded)")
    if not history:
        print("  No renders yet.")
    for entry in history[:10]:
        print(f"  {entry.at}  {Path(entry.path).name:<32} {entry.status:<9} "
              f"{entry.resolution:<10} {entry.fps} fps  QC {entry.qc or '-'}")
    return EXIT_OK


def command_render_cancel(args, paths: AppPaths, settings: Settings) -> int:
    """Cancel a render started by the GUI.

    A CLI render runs in the foreground and is stopped with Ctrl-C; this covers a
    render that is running in the application.
    """
    print("A render started from the command line runs in this terminal and can be "
          "stopped with Ctrl-C.")
    print("To cancel a render running inside the application, use the Cancel button "
          "on the Render page - it stops the encoder and cleans up the temporary "
          "files. The finished takes already in the output folder are never touched.")
    return EXIT_OK


# --------------------------------------------------------------------------
# audio
# --------------------------------------------------------------------------

def command_audio_validate(args, paths: AppPaths, settings: Settings) -> int:
    service = _open_service(args, paths, settings)
    from ..tools.ffmpeg import FFmpegTools, discover_ffmpeg

    audio = AudioService(FFmpegTools(discover_ffmpeg()), project_dir=_project_dir(service))
    project = service.current
    timeline = build_timeline(project.scenes)
    validation = audio.validate(project, timeline)

    print(f"Timeline   : {timeline.total_duration:.2f}s")
    print(f"Narration  : {len(validation.placements)} scene(s) with a narration file")
    for placement in validation.placements:
        where = "missing file" if placement.path is None else Path(str(placement.path)).name
        print(f"  {placement.scene_name or placement.scene_id:<20} "
              f"{placement.start:7.2f}s +{placement.duration:5.2f}s  {where}")
    _print_issues("Errors (these stop a render):", validation.errors)
    _print_issues("Warnings:", validation.warnings)
    if validation.ok and not validation.warnings:
        print("\nThe audio setup is fine.")
    return EXIT_OK if validation.ok else EXIT_PROBLEMS


def command_audio_mix(args, paths: AppPaths, settings: Settings) -> int:
    service = _open_service(args, paths, settings)
    from ..tools.ffmpeg import FFmpegTools, discover_ffmpeg

    tools = FFmpegTools(discover_ffmpeg())
    project_dir = _project_dir(service)
    audio = AudioService(tools, project_dir=project_dir)
    project = service.current
    timeline = build_timeline(project.scenes)
    output = Path(args.output) if args.output else project_dir / "audio_preview.wav"

    print(f"Mixing to {output} ...")
    result = audio.render_master(project, timeline, output)
    if not result.ok:
        print(f"The mix did not finish: {result.message}")
        _print_issues("Problems:", result.issues)
        return EXIT_BLOCKED
    print(f"Written   : {result.path}")
    print(f"Duration  : {result.duration:.2f}s")
    _print_issues("Notes:", result.issues)
    return EXIT_OK


# --------------------------------------------------------------------------
# subtitles
# --------------------------------------------------------------------------

def command_subtitles_export(args, paths: AppPaths, settings: Settings) -> int:
    service = _open_service(args, paths, settings)
    project = service.current
    timeline = build_timeline(project.scenes)
    cues, issues = generate_cues(project, timeline)

    print(f"Captions  : {len(cues)} from the measured narration")
    _print_issues("Warnings:", issues)
    if not cues:
        print("There is nothing to export: no scene has narration with a measured length.")
        return EXIT_PROBLEMS

    project.subtitles.cues = cues
    folder = Path(args.output) if args.output else _project_dir(service) / "subtitles"
    folder.mkdir(parents=True, exist_ok=True)
    stem = args.stem or project.project.name.replace(" ", "_") or "subtitles"

    written = []
    if args.format in ("srt", "both"):
        written.append(write_subtitle_file(folder / f"{stem}.srt", to_srt(cues)))
    if args.format in ("vtt", "both"):
        written.append(write_subtitle_file(folder / f"{stem}.vtt", to_vtt(cues)))
    for path in written:
        print(f"Written   : {path}")
    return EXIT_OK


# --------------------------------------------------------------------------
# parsers
# --------------------------------------------------------------------------

def _add_export_overrides(parser) -> None:
    parser.add_argument("--width", type=int, help="Override the output width.")
    parser.add_argument("--height", type=int, help="Override the output height.")
    parser.add_argument("--fps", type=int, help="Override the frame rate.")
    parser.add_argument("--crf", type=int, help="Override the constant-quality value.")
    parser.add_argument("--bitrate-kbps", dest="bitrate_kbps", type=int,
                        help="Override the target video bitrate.")
    parser.add_argument("--quality-preset", dest="quality_preset",
                        help="draft, low, medium, high, very_high, ultra.")
    parser.add_argument("--codec", help="h264_cpu, hevc_cpu or vp9_cpu.")
    parser.add_argument("--container", help="mp4, mkv, mov or webm.")
    parser.add_argument("--encoder-preset", dest="encoder_preset",
                        help="ultrafast ... veryslow.")
    parser.add_argument("--preset", help="Apply a platform preset (youtube_1080, shorts, ...).")


def build_render_parser(subparsers) -> None:
    parser = subparsers.add_parser("render", help="Render, validate and inspect videos.")
    render_sub = parser.add_subparsers(dest="render_command")

    run = render_sub.add_parser("run", help="Render the project to a finished video.")
    run.add_argument("--project", help="Path to a project.json (default: most recent).")
    _add_export_overrides(run)
    burn = run.add_mutually_exclusive_group()
    burn.add_argument("--burn-subtitles", dest="burn_subtitles", action="store_true",
                      default=None, help="Draw the captions into the picture.")
    burn.add_argument("--no-burn-subtitles", dest="burn_subtitles", action="store_false",
                      help="Keep the captions out of the picture.")
    run.add_argument("--quick-qc", dest="quick_qc", action="store_true",
                     help="Skip the whole-file silence and black-frame scans.")
    run.add_argument("--fresh", action="store_true",
                     help="Ignore segments from an earlier interrupted render.")
    run.add_argument("--force", action="store_true",
                     help="Render even though validation reported errors.")
    run.add_argument("--verbose", action="store_true", help="Show technical detail on failure.")
    run.set_defaults(func=command_render_run)

    validate = render_sub.add_parser("validate", help="Check a project without rendering.")
    validate.add_argument("--project", help="Path to a project.json (default: most recent).")
    validate.set_defaults(func=command_render_validate)

    preview = render_sub.add_parser("preview", help="Show what a render would do.")
    preview.add_argument("--project", help="Path to a project.json (default: most recent).")
    _add_export_overrides(preview)
    preview.set_defaults(func=command_render_preview)

    status = render_sub.add_parser("status", help="Show capabilities and render history.")
    status.add_argument("--project", help="Path to a project.json (default: most recent).")
    status.set_defaults(func=command_render_status)

    cancel = render_sub.add_parser("cancel", help="Explain how to cancel a render.")
    cancel.add_argument("job", nargs="?", help="The job id (informational).")
    cancel.set_defaults(func=command_render_cancel)


def build_audio_parser(subparsers) -> None:
    parser = subparsers.add_parser("audio", help="Check and preview the project audio.")
    audio_sub = parser.add_subparsers(dest="audio_command")

    validate = audio_sub.add_parser("validate", help="Check the audio setup.")
    validate.add_argument("--project", help="Path to a project.json (default: most recent).")
    validate.set_defaults(func=command_audio_validate)

    mix = audio_sub.add_parser("mix", help="Mix the master audio to a file.")
    mix.add_argument("--project", help="Path to a project.json (default: most recent).")
    mix.add_argument("--output", help="Where to write the mix (default: audio_preview.wav).")
    mix.set_defaults(func=command_audio_mix)


def build_subtitle_parser(subparsers) -> None:
    parser = subparsers.add_parser("subtitles", help="Export the project captions.")
    subtitle_sub = parser.add_subparsers(dest="subtitle_command")

    export = subtitle_sub.add_parser("export", help="Write the captions to .srt / .vtt.")
    export.add_argument("--project", help="Path to a project.json (default: most recent).")
    export.add_argument("--output", help="Folder to write into.")
    export.add_argument("--stem", help="File name without the extension.")
    export.add_argument("--format", choices=("srt", "vtt", "both"), default="both")
    export.set_defaults(func=command_subtitles_export)


def run_render_command(args, paths: AppPaths, settings: Settings) -> int:
    command = getattr(args, "render_command", None)
    if not command:
        print("Choose a sub-command: run, validate, preview, status, cancel.")
        return EXIT_OK
    return _dispatch(args, paths, settings)


def run_audio_command(args, paths: AppPaths, settings: Settings) -> int:
    command = getattr(args, "audio_command", None)
    if not command:
        print("Choose a sub-command: validate, mix.")
        return EXIT_OK
    return _dispatch(args, paths, settings)


def run_subtitle_command(args, paths: AppPaths, settings: Settings) -> int:
    command = getattr(args, "subtitle_command", None)
    if not command:
        print("Choose a sub-command: export.")
        return EXIT_OK
    return _dispatch(args, paths, settings)


def _dispatch(args, paths: AppPaths, settings: Settings) -> int:
    try:
        return int(args.func(args, paths, settings))
    except ValueError as exc:
        print(str(exc))
        return EXIT_PROBLEMS
