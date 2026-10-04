"""``motion-studio project ...`` commands.

Every command goes through :class:`ProjectService` - the same object the GUI
uses (directive section 32).  There is no second implementation of create,
save, validate or recovery here.

Exit codes follow the rest of the CLI: ``0`` fine, ``1`` problems found,
``2`` blocked, ``3`` unexpected error.
"""

from __future__ import annotations

from pathlib import Path

from ..core.errors import AppError
from ..core.paths import AppPaths
from ..core.settings import Settings
from ..project.layout import find_project_file
from ..project.presets import (
    FPS_OPTIONS,
    PROJECT_TEMPLATES,
    QUALITY_PRESETS,
    preview_filename,
    resolve_quality,
)
from ..project.service import CreateRequest, ProjectService

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_BLOCKED = 2


def make_service(paths: AppPaths, settings: Settings) -> ProjectService:
    return ProjectService(paths, settings)


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------

def command_create(args, paths: AppPaths, settings: Settings) -> int:
    service = make_service(paths, settings)
    template = args.template or "blank"

    request = CreateRequest(
        name=args.name,
        description=args.description or "",
        template=template,
        width=args.width or 0,
        height=args.height or 0,
        fps=args.fps or 0,
        quality=resolve_quality(args.quality) if args.quality else "",
        channel_name=args.channel or "",
        folder=Path(args.folder) if args.folder else None,
        language=args.language or "en-us",
        voice=args.voice or "",
    )
    try:
        project = service.create_project(request, open_after=not args.no_open)
    except AppError as exc:
        print(exc.friendly().to_message())
        return EXIT_PROBLEMS

    layout = service.current_layout
    print(f"Created '{project.project.name}'")
    print(f"  Folder      : {layout.root}")
    print(f"  Project file: {layout.project_file}")
    print(f"  Format      : {project.format.label()}")
    print(f"  Quality     : {project.format.quality_label()}")
    print(f"  Voice       : {project.voice.label()}")
    print(f"  Output name : {preview_filename(project.export.filename_template, project.project.name)}")
    if args.script:
        text = Path(args.script).read_text(encoding="utf-8")
        service.set_script_text(text)
        service.save(reason="script from --script")
        print(f"  Script      : {len(text.split())} words from {args.script}")
    print()
    print("Open it in the application:  python run_studio.py")
    # The CLI is not holding the project open, so its lock must not outlive the
    # command - otherwise the next open would be told the project is in use.
    service.close_project()
    return EXIT_OK


# --------------------------------------------------------------------------
# open / info / validate
# --------------------------------------------------------------------------

def command_open(args, paths: AppPaths, settings: Settings) -> int:
    service = make_service(paths, settings)
    try:
        project = service.open_project(Path(args.path))
    except AppError as exc:
        print(exc.friendly().to_message())
        return EXIT_PROBLEMS

    print(project.to_text())
    notes = service.session.notes if service.session else []
    if notes:
        print()
        print("Notes:")
        for note in notes:
            print(f"  - {note}")
    report = service.validate(check_files=True)
    print()
    print(report.to_text())
    service.close_project()
    return EXIT_OK if report.ok else EXIT_PROBLEMS


def command_info(args, paths: AppPaths, settings: Settings) -> int:
    service = make_service(paths, settings)
    target = find_project_file(Path(args.path))
    if target is None:
        print(f"'{args.path}' is not a project (no project.json found).")
        return EXIT_PROBLEMS

    loaded = service.store.load(target, check_files=True)
    if not loaded.ok or loaded.project is None:
        print(loaded.friendly.to_message() if loaded.friendly else f"Could not open: {loaded.error}")
        return EXIT_PROBLEMS

    project = loaded.project
    print(project.to_text())
    if loaded.migrated and loaded.migration is not None:
        print()
        print(loaded.migration.summary())
    print()
    print("Scenes:")
    if not project.scenes:
        print("  (none yet)")
    for row in project.timeline():
        print(f"  {row['index'] + 1:>2}. {row['name']:<24} {row['start']:>7.2f}s - {row['end']:>7.2f}s  ({row['timed_by']})")
    print()
    print("Assets:")
    if not project.assets:
        print("  (none)")
    for asset in project.assets:
        mark = "missing" if asset.missing else ("portable" if asset.is_portable else "absolute path")
        print(f"  [{mark:<13}] {asset.label():<28} {asset.kind:<6} {asset.path or asset.absolute_path}")
    print()
    print("Folders:")
    for name, directory in service_paths_layout(target).named_directories().items():
        print(f"  [{'ok ' if directory.is_dir() else '-- '}] {name:<10} {directory}")
    return EXIT_OK


def service_paths_layout(project_file: Path):
    from ..project.layout import ProjectLayout

    return ProjectLayout.from_project_file(project_file)


def command_validate(args, paths: AppPaths, settings: Settings) -> int:
    service = make_service(paths, settings)
    target = find_project_file(Path(args.path))
    if target is None:
        print(f"'{args.path}' is not a project (no project.json found).")
        return EXIT_PROBLEMS

    loaded = service.store.load(target, check_files=True)
    if not loaded.ok or loaded.project is None:
        print(loaded.friendly.to_message() if loaded.friendly else f"Could not open: {loaded.error}")
        return EXIT_BLOCKED

    from ..project.validation import validate_for_render, validate_project

    layout = service_paths_layout(target)
    report = validate_for_render(loaded.project, layout.root) if args.for_render else validate_project(
        loaded.project, project_dir=layout.root, check_files=True
    )
    print(report.to_text())
    print()
    print(report.headline())
    return EXIT_OK if report.ok else EXIT_PROBLEMS


# --------------------------------------------------------------------------
# list / duplicate / rename / recovery
# --------------------------------------------------------------------------

def command_list(args, paths: AppPaths, settings: Settings) -> int:
    service = make_service(paths, settings)
    entries = service.list_recent(include_archived=args.all)
    if not entries:
        print("No projects yet.")
        print(f"Projects folder: {paths.projects_dir}")
        print("Create one with:  motion-studio project create --name \"My first video\"")
        return EXIT_OK

    print(f"{len(entries)} project(s)")
    print()
    print(f"{'NAME':<32} {'FORMAT':<14} {'SCENES':>6}  {'STATUS':<15} FOLDER")
    for entry in entries:
        star = "*" if entry.favorite else " "
        print(
            f"{star}{entry.name[:31]:<31} {entry.resolution_label():<14} {entry.scenes:>6}  "
            f"{entry.status_label():<15} {Path(entry.path).name}"
        )
    print()
    print("* = favourite.  Open one with:  motion-studio project open <folder>")
    return EXIT_OK


def command_duplicate(args, paths: AppPaths, settings: Settings) -> int:
    service = make_service(paths, settings)
    try:
        duplicate = service.duplicate_project(
            Path(args.path), name=args.name or "", copy_assets=not args.reference_assets
        )
    except AppError as exc:
        print(exc.friendly().to_message())
        return EXIT_PROBLEMS

    print(f"Duplicated as '{duplicate.project.name}'")
    print(f"  Folder: {service.current_layout.root}")
    print(f"  Assets: {'copied into the new project' if not args.reference_assets else 'referenced from the original'}")
    service.close_project()
    return EXIT_OK


def command_rename(args, paths: AppPaths, settings: Settings) -> int:
    service = make_service(paths, settings)
    try:
        service.open_project(Path(args.path))
        if args.folder:
            target = service.rename_project_folder(args.folder)
            service.save(reason="after renaming the folder")
            print(f"Folder renamed to: {target}")
        else:
            service.rename_project(args.name)
            result = service.save(reason="rename")
            if not result.ok:
                print(result.summary())
                return EXIT_PROBLEMS
            print(f"Project renamed to: {service.current.project.name}")
    except AppError as exc:
        print(exc.friendly().to_message())
        return EXIT_PROBLEMS
    finally:
        service.close_project()
    return EXIT_OK


def command_recovery(args, paths: AppPaths, settings: Settings) -> int:
    service = make_service(paths, settings)
    candidates = service.scan_recovery(Path(args.path) if args.path else None)
    if not candidates:
        print("No recoverable projects found.")
        return EXIT_OK

    print(f"{len(candidates)} project(s) have recovery data:")
    for index, candidate in enumerate(candidates, start=1):
        print(f"  {index}. {candidate.project_name or '(unnamed)'} - {candidate.kind}")
        print(f"     {candidate.path}")
        print(f"     {candidate.reason}")
    print()

    if not args.restore and not args.ignore:
        print("Restore one with:  motion-studio project recovery --path <project folder> --restore 1")
        print("Dismiss one with:  motion-studio project recovery --path <project folder> --ignore 1")
        return EXIT_PROBLEMS

    index = int(args.restore or args.ignore) - 1
    if not 0 <= index < len(candidates):
        print(f"There is no recovery entry {index + 1}.")
        return EXIT_PROBLEMS
    candidate = candidates[index]

    if args.ignore:
        service.ignore_recovery(candidate)
        print("Recovery data set aside. The project file was not changed.")
        return EXIT_OK

    try:
        project = service.restore_recovery(candidate)
    except AppError as exc:
        print(exc.friendly().to_message())
        return EXIT_PROBLEMS
    print(f"Restored '{project.project.name}' from {candidate.path.name}.")
    print("The previous project.json was kept in the project's backups folder.")
    service.close_project()
    return EXIT_OK


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

def build_project_parser(subparsers) -> None:
    """Add the ``project`` command group to the CLI parser."""
    parser = subparsers.add_parser("project", help="Create, open, validate and manage projects.")
    project_sub = parser.add_subparsers(dest="project_command")

    create = project_sub.add_parser("create", help="Create a new project folder and project.json.")
    create.add_argument("--name", required=True, help="Project name.")
    create.add_argument("--description", help="Short description.")
    create.add_argument("--template", choices=[template.key for template in PROJECT_TEMPLATES], help="Starting template.")
    create.add_argument("--width", type=int, help="Frame width in pixels.")
    create.add_argument("--height", type=int, help="Frame height in pixels.")
    create.add_argument("--fps", type=int, choices=list(FPS_OPTIONS), help="Frames per second.")
    create.add_argument("--quality", choices=[preset.key for preset in QUALITY_PRESETS], help="Quality preset.")
    create.add_argument("--channel", help="Channel/profile name.")
    create.add_argument("--language", help="Narration language, e.g. en-us.")
    create.add_argument("--voice", help="Kokoro voice id (leave empty for the first available).")
    create.add_argument("--folder", help="Create the project in this folder instead of the projects folder.")
    create.add_argument("--script", help="Import this text file as the script.")
    create.add_argument("--no-open", action="store_true", help="Create without opening the project.")
    create.set_defaults(func=command_create)

    open_parser = project_sub.add_parser("open", help="Open a project and report its state.")
    open_parser.add_argument("path", help="Project folder or project.json.")
    open_parser.set_defaults(func=command_open)

    info = project_sub.add_parser("info", help="Print everything stored in a project.")
    info.add_argument("path")
    info.set_defaults(func=command_info)

    validate = project_sub.add_parser("validate", help="Check a project and list every problem.")
    validate.add_argument("path")
    validate.add_argument("--for-render", action="store_true", help="Use the stricter pre-render rules.")
    validate.set_defaults(func=command_validate)

    list_parser = project_sub.add_parser("list", help="List recent projects.")
    list_parser.add_argument("--all", action="store_true", help="Include archived projects.")
    list_parser.set_defaults(func=command_list)

    duplicate = project_sub.add_parser("duplicate", help="Copy a project into a new independent project.")
    duplicate.add_argument("path")
    duplicate.add_argument("--name", help="Name for the copy.")
    duplicate.add_argument("--reference-assets", action="store_true", help="Point at the original assets instead of copying them.")
    duplicate.set_defaults(func=command_duplicate)

    rename = project_sub.add_parser("rename", help="Rename a project (or its folder with --folder).")
    rename.add_argument("path")
    rename.add_argument("--name", help="New display name.")
    rename.add_argument("--folder", help="New folder name inside the projects folder.")
    rename.set_defaults(func=command_rename)

    recovery = project_sub.add_parser("recovery", help="Find, restore or dismiss recovery data.")
    recovery.add_argument("--path", help="One project folder; without it every known project is scanned.")
    recovery.add_argument("--restore", help="Restore this entry number.")
    recovery.add_argument("--ignore", help="Set this entry number aside.")
    recovery.set_defaults(func=command_recovery)

    parser.set_defaults(func=lambda args, paths, settings: _project_help(parser))


def _project_help(parser) -> int:
    parser.print_help()
    return EXIT_OK


def run_project_command(args, paths: AppPaths, settings: Settings) -> int:
    """Dispatch a parsed ``project`` command."""
    handler = getattr(args, "func", None)
    if handler is None:
        return EXIT_OK
    return int(handler(args, paths, settings))


__all__ = ["build_project_parser", "make_service", "run_project_command"]
