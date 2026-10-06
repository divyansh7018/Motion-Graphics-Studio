"""``mgs image`` - Image Studio from the command line (Stage F).

Every command goes through the same :class:`~app.image.service.ImageService` the
GUI uses, so the command line and the application can never disagree about what a
backend supports or why a generation failed.

Nothing here loads a model at import time, and the status commands are explicit
about what is *not* installed rather than guessing (section 82).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Sequence

from ..core.paths import AppPaths
from ..core.settings import Settings

__all__ = ["build_image_parser", "run_image_command"]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_BLOCKED = 2


def _service(paths: AppPaths, settings: Settings) -> Any:
    """The same service the GUI uses, pointed at the same folders."""
    from ..image.service import ImageService

    return ImageService.for_paths(paths, settings)


def _print_heading(title: str) -> None:
    print(f"\n{title}")


def _print_key_values(pairs) -> None:
    width = max((len(str(key)) for key, _ in pairs), default=0)
    for key, value in pairs:
        print(f"  {str(key):<{width}} : {value}")


def _print_failure(message: str, *, why: str = "", what_to_do: str = "") -> None:
    """What happened, why, and what to do - never just 'failed'."""
    print(f"\n  X  {message}")
    if why:
        print(f"     Why: {why}")
    if what_to_do:
        print(f"     What to do: {what_to_do}")


def _print_backend_table(report: Any) -> None:
    _print_heading("Backends")
    for entry in report.backends:
        state = entry.status()
        marker = "yes" if state.available else "no "
        print(f"  [{marker}] {entry.label} ({entry.id}) - {state.state}")
        print(f"         {state.reason}")
        for line in state.instructions:
            print(f"         -> {line}")


def _print_model_table(models: Sequence[Any]) -> None:
    _print_heading("Models")
    if not models:
        print("  No local image model was found.")
        print("  Image Studio still works: import, edit, upscale and organise")
        print("  images need no model.")
        return
    for model in models:
        print(f"  {model.name}")
        print(f"    backend: {model.backend}  kind: {model.kind}  "
              f"device: {model.device}")
        if model.path:
            size = f"{model.size_bytes / (1024 * 1024):.1f} MB" if model.size_bytes \
                else "size unknown"
            print(f"    path: {model.path} ({size})")
        print(f"    can: {model.capabilities.describe() or 'nothing reported'}")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def command_backends(args: Any, paths: AppPaths, settings: Settings) -> int:
    """List the backends and models this machine actually has."""
    service = _service(paths, settings)
    if getattr(args, "deep", False):
        # An explicit device report may import torch; the normal path must not,
        # because that costs about 500 MB of memory.
        from ..image.device import detect_device

        service._device = detect_device(deep=True)
    status = service.status()
    _print_backend_table(status.report)
    _print_model_table(status.report.models)

    _print_heading("Device")
    print(f"  {status.device.describe()}")
    for probe in status.device.probes:
        print(f"    - {probe}")

    _print_heading("Verdict")
    print(f"  {status.generator_note}")
    if args.json:
        print(json.dumps(status.to_dict(), indent=2))
    return 0


def command_generate(args: Any, paths: AppPaths, settings: Settings) -> int:
    """Generate one image, or a batch, with the chosen backend."""
    from ..image.provider import GenerationRequest

    service = _service(paths, settings)
    output_dir = Path(args.output_dir) if args.output_dir else \
        paths.data_root / "images"
    output_dir.mkdir(parents=True, exist_ok=True)

    request = GenerationRequest(
        mode=args.mode,
        backend=args.backend or "",
        prompt=args.prompt or "",
        negative_prompt=args.negative or "",
        model=args.model or "",
        width=int(args.width or 0),
        height=int(args.height or 0),
        seed=int(args.seed or 0),
        steps=int(args.steps or 0),
        guidance=float(args.guidance or 0.0),
        sampler=args.sampler or "",
        batch=int(args.batch or 1),
        strength=float(args.strength or 0.0),
        source_image=args.source or "",
        mask_image=args.mask or "",
        scale=float(args.scale or 0.0),
        output_format=args.format or "png",
        output_dir=str(output_dir),
        name_stem=args.name or "image",
    )

    issues = service.validate(request, backend_id=request.backend)
    blocking = [issue for issue in issues if issue.severity == "error"]
    if blocking:
        for issue in blocking:
            _print_failure(issue.message, what_to_do=issue.what_to_do)
        return EXIT_BLOCKED
    for issue in issues:
        print(f"  Note: {issue.message}")

    def progress(state: str, fraction: float) -> None:
        print(f"  {state} {fraction * 100:.0f}%", file=sys.stderr)

    result = service.generate(request, progress=progress)
    if not result.ok:
        _print_failure(result.error or "The generation did not finish.",
                       why=result.why, what_to_do=result.what_to_do)
        return EXIT_PROBLEMS

    print(f"\n  OK  {result.summary()}")
    _print_key_values([
        ("Backend", result.backend),
        ("Model", result.model),
        ("Resolution", f"{result.width}x{result.height}"),
        ("Seed", ", ".join(str(seed) for seed in result.seeds)),
        ("Format", result.output_format),
        ("Seconds", f"{result.seconds:.2f}"),
    ])
    _print_heading("Files")
    for raw in result.paths:
        path = Path(raw)
        size = path.stat().st_size if path.is_file() else 0
        print(f"  {path} ({size:,} bytes)")
    if args.json:
        print(json.dumps({"paths": result.paths, "seeds": result.seeds,
                          "quality": result.quality}, indent=2))
    return 0


def command_info(args: Any, paths: AppPaths, settings: Settings) -> int:
    """Print the facts recorded for one image."""
    from ..image.metadata import read_metadata
    from ..image.validation import validate_image_file

    target = Path(args.path)
    check = validate_image_file(target, required=True, label="The image")
    if not check.ok:
        _print_failure(check.error, what_to_do=check.what_to_do)
        return EXIT_PROBLEMS

    _print_key_values([
        ("File", str(check.path)),
        ("Format", check.format or "unknown"),
        ("Mode", check.mode),
        ("Resolution", f"{check.width}x{check.height}"),
        ("Size", f"{check.size_bytes:,} bytes"),
        ("Transparency", "yes" if check.has_alpha else "no"),
        ("Animated", "yes" if check.animated else "no"),
    ])

    metadata = read_metadata(target)
    _print_heading("Recorded metadata")
    if metadata is None:
        print("  None. This file carries no generation record.")
        return 0
    data = metadata.to_dict()
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    if not data:
        print("  None.")
        return 0
    for key in sorted(data):
        if key == "version":
            continue
        value = data[key]
        if isinstance(value, list):
            print(f"  {key}:")
            for item in value:
                print(f"    - {item}")
        else:
            print(f"  {key}: {value}")
    return 0


def command_library(args: Any, paths: AppPaths, settings: Settings) -> int:
    """List the image library, or scan a folder into it."""
    from ..image.library import LibraryQuery

    service = _service(paths, settings)
    if service.library is None:
        _print_failure("No image library folder is configured.",
                       what_to_do="Run 'motion-studio setup' to create the folders.")
        return EXIT_PROBLEMS

    if args.scan:
        service.library.scan(checksums=bool(args.checksums))
        service.library.save()
        print(f"  Scanned {service.library.root}")

    page = service.query(LibraryQuery(
        text=args.search or "", collection=args.collection or "",
        limit=int(args.limit or 50)))
    _print_heading(f"Library ({page.total} image(s))")
    if not page.entries:
        print("  Nothing here yet. Generate or import an image.")
        return 0
    for entry in page.entries:
        tags = f" [{', '.join(entry.tags)}]" if entry.tags else ""
        state = "  (file missing)" if entry.missing else ""
        print(f"  {entry.name}  {entry.resolution}  {entry.format}{tags}{state}")
        if entry.prompt:
            prompt = entry.prompt if len(entry.prompt) <= 70 else \
                entry.prompt[:67] + "..."
            print(f"      prompt: {prompt}")
        if entry.seed is not None:
            print(f"      seed: {entry.seed}  model: {entry.model or '-'}")
    return 0


def command_upscale(args: Any, paths: AppPaths, settings: Settings) -> int:
    """Upscale an image, saying which method really ran."""
    service = _service(paths, settings)
    result = service.upscale_image(
        Path(args.source), scale=float(args.scale or 2.0),
        method=args.method or "standard",
        output_dir=Path(args.output_dir) if args.output_dir else None,
        allow_fallback=bool(args.allow_fallback))
    if not result.ok:
        _print_failure(result.error or "The image could not be upscaled.",
                       what_to_do=result.what_to_do)
        return EXIT_PROBLEMS
    print(f"\n  OK  {result.describe()}")
    _print_key_values([
        ("Method", result.method_label),
        ("Output", str(result.path)),
        ("Size", f"{result.width}x{result.height}"),
        ("Scale", f"{result.scale:g}x"),
    ])
    if result.downgraded:
        # Never let a standard resize be mistaken for an AI upscale.
        print(f"  Note: {result.downgraded}")
    return 0


def command_import(args: Any, paths: AppPaths, settings: Settings) -> int:
    """Copy an image into a project or the library, validating it first."""
    service = _service(paths, settings)
    destination = Path(args.destination)
    report = service.import_image(Path(args.source), destination=destination,
                                  requested_format=args.format or "",
                                  overwrite=bool(args.overwrite))
    if not report.ok:
        _print_failure(report.error, what_to_do=report.what_to_do)
        return EXIT_PROBLEMS
    print(f"\n  OK  {report.describe()}")
    for note in report.notes or []:
        print(f"  Note: {note}")
    return 0


def command_edit(args: Any, paths: AppPaths, settings: Settings) -> int:
    """Apply edits to an image and save a new version."""
    service = _service(paths, settings)
    session = service.open_editor(Path(args.source))
    for operation in args.operations or []:
        name, _, parameters = str(operation).partition("=")
        values: dict[str, Any] = {}
        for pair in parameters.split(",") if parameters else []:
            key, _, value = pair.partition(":")
            if not key:
                continue
            try:
                values[key.strip()] = float(value) if "." in value \
                    else int(value)
            except ValueError:
                values[key.strip()] = value.strip()
        session.add(name.strip(), **values)

    target = Path(args.output) if args.output else \
        service.unique_output(Path(args.source).parent,
                              f"{Path(args.source).stem}_edited",
                              Path(args.source).suffix or ".png")
    report = service.save_edit(session, target=target,
                               requested_format=args.format or "",
                               overwrite=bool(args.overwrite))
    if not report.ok:
        _print_failure(report.error, what_to_do=report.what_to_do)
        return EXIT_PROBLEMS
    print(f"\n  OK  {report.describe()}")
    print(f"  Operations applied: {len(session.operations)}")
    if report.format_changed:
        print(f"  Note: {report.format_changed}")
    return 0


def command_history(args: Any, paths: AppPaths, settings: Settings) -> int:
    """Show recent generations, including the ones that failed."""
    service = _service(paths, settings)
    entries = service.history.recent(limit=int(args.limit or 20))
    _print_heading(f"History ({len(entries)} recent)")
    if not entries:
        print("  Nothing generated yet.")
        return 0
    for entry in entries:
        print(f"  [{entry.status}] {entry.at}  {Path(entry.path).name if entry.path else '-'}")
        if entry.prompt:
            prompt = entry.prompt if len(entry.prompt) <= 60 else \
                entry.prompt[:57] + "..."
            print(f"      {prompt}")
        print(f"      model: {entry.model or '-'}  seed: {entry.seed}  "
              f"{entry.width}x{entry.height}")
        if entry.error:
            print(f"      error: {entry.error}")
    return 0


def command_send(args: Any, paths: AppPaths, settings: Settings) -> int:
    """Send an image into the open project's scene."""
    from ..image.integration import send_to_scene
    from ..project.service import ProjectService

    projects = ProjectService(paths, settings)
    if args.project:
        projects.open_project(Path(args.project))
    if not projects.is_open():
        _print_failure(
            "No project is open, so the image cannot be sent to a scene.",
            why="A scene belongs to a project.",
            what_to_do="Pass --project <folder>, or create a project first.")
        return EXIT_BLOCKED

    result = send_to_scene(projects, Path(args.source),
                           scene_id=args.scene or "",
                           placement=args.placement or "overlay",
                           name=args.name or "")
    if not result.ok:
        _print_failure(result.message, why=result.why,
                       what_to_do=result.what_to_do)
        return EXIT_PROBLEMS
    print(f"\n  OK  {result.describe()}")
    _print_key_values([
        ("Asset", result.asset_id),
        ("Scene", result.scene_name),
        ("Placement", result.placement),
    ])
    for note in result.notes:
        print(f"  {note}")
    projects.save(reason="Image Studio: sent an image to a scene")
    projects.close_project(save=False)
    return 0


def command_background(args: Any, paths: AppPaths, settings: Settings) -> int:
    """Report whether background removal is really available here."""
    from ..image.background_removal import detect_background_removal

    state = detect_background_removal()
    _print_key_values([
        ("State", state.state),
        ("Reason", state.reason),
    ])
    for line in state.instructions:
        print(f"  -> {line}")
    if not state.available:
        print("  No fake result is produced. Use the manual mask instead:")
        print("    mgs image edit <file> --op mask --output <new file>")
        return 0
    return 0


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

def build_image_parser(subparsers: Any) -> None:
    """Register ``mgs image ...``."""
    image = subparsers.add_parser(
        "image", help="Image Studio: detect, generate, edit and organise images.")
    image.set_defaults(func=_dispatch)
    image_sub = image.add_subparsers(dest="image_command")

    backends = image_sub.add_parser(
        "backends", help="List the local image backends and models.")
    backends.add_argument("--json", action="store_true",
                          help="Print the detection result as JSON.")
    backends.add_argument("--deep", action="store_true",
                          help="Also probe torch for a GPU. This imports torch, "
                               "which costs about 500 MB of memory, so it is off "
                               "by default.")

    generate = image_sub.add_parser("generate", help="Generate an image.")
    generate.add_argument("--mode", default="text_to_image",
                          choices=("text_to_image", "image_to_image", "inpaint",
                                   "outpaint", "variation", "upscale"))
    generate.add_argument("--backend", default="",
                          help="Adapter id, for example standard, command or diffusers.")
    generate.add_argument("--model", default="")
    generate.add_argument("--prompt", default="")
    generate.add_argument("--negative", default="")
    generate.add_argument("--width", type=int, default=512)
    generate.add_argument("--height", type=int, default=512)
    generate.add_argument("--seed", type=int, default=0,
                          help="0 picks a random seed, which is then reported.")
    generate.add_argument("--steps", type=int, default=0)
    generate.add_argument("--guidance", type=float, default=0.0)
    generate.add_argument("--sampler", default="")
    generate.add_argument("--batch", type=int, default=1)
    generate.add_argument("--strength", type=float, default=0.0)
    generate.add_argument("--scale", type=float, default=0.0,
                          help="Upscale factor for --mode upscale.")
    generate.add_argument("--source", default="", help="Source image file.")
    generate.add_argument("--mask", default="", help="Mask image for inpaint.")
    generate.add_argument("--format", default="png",
                          choices=("png", "jpg", "jpeg", "webp", "bmp", "tiff"))
    generate.add_argument("--output-dir", default="")
    generate.add_argument("--name", default="image", help="Output file stem.")
    generate.add_argument("--json", action="store_true")

    info = image_sub.add_parser("info", help="Print one image's facts and metadata.")
    info.add_argument("path")
    info.add_argument("--json", action="store_true")

    library = image_sub.add_parser("library", help="List or scan the image library.")
    library.add_argument("--scan", action="store_true",
                         help="Rescan the library folder first.")
    library.add_argument("--checksums", action="store_true",
                         help="Hash files while scanning, to find duplicates.")
    library.add_argument("--search", default="")
    library.add_argument("--collection", default="")
    library.add_argument("--limit", type=int, default=50)

    upscale = image_sub.add_parser("upscale", help="Upscale an image.")
    upscale.add_argument("source")
    upscale.add_argument("--scale", type=float, default=2.0)
    upscale.add_argument("--method", default="standard",
                         choices=("standard", "ai", "auto"))
    upscale.add_argument("--output-dir", default="")
    upscale.add_argument("--allow-fallback", action="store_true",
                         help="Fall back to a standard resize if no AI upscaler "
                              "is installed. The result always says which ran.")

    import_p = image_sub.add_parser("import", help="Import an image to a new path.")
    import_p.add_argument("source")
    import_p.add_argument("destination")
    import_p.add_argument("--format", default="")
    import_p.add_argument("--overwrite", action="store_true",
                          help="Replace the destination. Off by default so an "
                               "existing file is never destroyed.")

    edit = image_sub.add_parser("edit", help="Edit an image into a new version.")
    edit.add_argument("source")
    edit.add_argument("--op", dest="operations", action="append",
                      metavar="NAME=key:value",
                      help="Repeatable, for example --op brightness=amount:1.2")
    edit.add_argument("--output", default="")
    edit.add_argument("--format", default="")
    edit.add_argument("--overwrite", action="store_true")

    history = image_sub.add_parser("history", help="Show recent generations.")
    history.add_argument("--limit", type=int, default=20)

    send = image_sub.add_parser("send", help="Send an image into a project scene.")
    send.add_argument("source")
    send.add_argument("--project", default="", help="Project folder to open.")
    send.add_argument("--scene", default="", help="Scene id; default is the first.")
    send.add_argument("--placement", default="overlay",
                      choices=("overlay", "background", "character", "reference"))
    send.add_argument("--name", default="")

    image_sub.add_parser(
        "background", help="Report whether background removal is installed.")


def _dispatch(args: Any, paths: AppPaths, settings: Settings) -> int:
    command = getattr(args, "image_command", None)
    handlers = {
        "backends": command_backends,
        "generate": command_generate,
        "info": command_info,
        "library": command_library,
        "upscale": command_upscale,
        "import": command_import,
        "edit": command_edit,
        "history": command_history,
        "send": command_send,
        "background": command_background,
    }
    handler = handlers.get(command or "")
    if handler is None:
        print("Usage: mgs image <backends|generate|info|library|upscale|"
              "import|edit|history|send|background>")
        return 2
    return handler(args, paths, settings)


def run_image_command(args: Any, paths: AppPaths, settings: Settings) -> int:
    """Entry point used by ``main.py``."""
    return _dispatch(args, paths, settings)
