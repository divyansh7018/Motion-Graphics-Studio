"""``motion-studio scene ...`` commands.

These share :class:`app.project.scene_service.SceneService` with the GUI, so the
command line reports exactly what the storyboard sees - the same scene engine,
the same validation, the same timing (directive section 45).  There is no second
implementation of listing or validation here.

Sub-commands
------------
``list``     every scene with its timing, narration state and issue counts
``validate`` validate every scene; exit 1 when anything is an error
``info``     full detail for one scene (elements, transitions, issues)
"""

from __future__ import annotations

from ..core.paths import AppPaths
from ..core.settings import Settings
from ..project.layout import find_project_file
from ..project.scene_service import SceneService
from ..project.service import ProjectService

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_BLOCKED = 2


def _open_service(args, paths: AppPaths, settings: Settings) -> ProjectService:
    """Open the requested project (or the most recent) and return its service."""
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


def _fmt_duration(seconds: float) -> str:
    return f"{seconds:.2f}s"


def command_scene_list(args, paths: AppPaths, settings: Settings) -> int:
    service = _open_service(args, paths, settings)
    scenes = SceneService(service)
    rows = scenes.list_scenes()
    if not rows:
        print("This project has no scenes yet.")
        return EXIT_OK
    print(f"{'#':>3}  {'Name':<24} {'Type':<10} {'Dur':>8} {'By':<10} {'Narration':<14} {'El':>3}  Flags")
    for row in rows:
        flags = "".join([
            "" if row.enabled else "disabled ",
            "locked " if row.locked else "",
            f"!{row.errors} " if row.errors else "",
            f"~{row.warnings}" if row.warnings else "",
        ]).strip() or "-"
        name = (row.name or "")[:24]
        print(f"{row.index + 1:>3}  {name:<24} {row.type:<10} {_fmt_duration(row.duration):>8} "
              f"{row.duration_source:<10} {row.narration_status:<14} {row.element_count:>3}  {flags}")
    total = sum(r.duration for r in rows)
    print()
    print(f"{len(rows)} scene(s), {_fmt_duration(total)} total on the timeline.")
    return EXIT_OK


def command_scene_validate(args, paths: AppPaths, settings: Settings) -> int:
    service = _open_service(args, paths, settings)
    scenes = SceneService(service)
    validation = scenes.validate()
    errors = [i for i in validation.issues if i.severity == "error"]
    warnings = [i for i in validation.issues if i.severity != "error"]
    if not validation.issues:
        print("All scenes are valid - no problems found.")
        return EXIT_OK
    for issue in validation.issues:
        where = f"element {issue.element_id}" if issue.element_id else "scene"
        print(f"[{issue.severity.upper():<7}] {where}: {issue.message}")
        if issue.what_to_do:
            print(f"           -> {issue.what_to_do}")
    print()
    print(f"{len(errors)} error(s), {len(warnings)} warning(s).")
    return EXIT_PROBLEMS if errors else EXIT_OK


def command_scene_info(args, paths: AppPaths, settings: Settings) -> int:
    service = _open_service(args, paths, settings)
    scenes = SceneService(service)
    detail = scenes.scene_info(args.scene)
    if detail is None:
        print(f"No scene with id '{args.scene}'.")
        print("Run 'motion-studio scene list' to see the scene ids.")
        return EXIT_PROBLEMS
    s = detail.summary
    print(f"Scene {s.index + 1}: {s.name}")
    print(f"  id:          {s.scene_id}")
    print(f"  type:        {s.type}")
    print(f"  duration:    {_fmt_duration(s.duration)} ({s.duration_source})")
    print(f"  narration:   {s.narration_status}")
    print(f"  enabled:     {s.enabled}    locked: {s.locked}")
    print(f"  transition:  in={detail.transition_in}  out={detail.transition_out}")
    print(f"  elements:    {len(detail.elements)}")
    for el in detail.elements:
        lock = " [locked]" if el["locked"] else ""
        text = f' "{el["text"][:40]}"' if el["text"] else ""
        print(f"    - {el['kind']:<9} {el['id']}{text}{lock}")
    if detail.issues:
        print(f"  issues:      {len(detail.issues)}")
        for issue in detail.issues:
            print(f"    [{issue['severity']}] {issue['code']}: {issue['message']}")
    else:
        print("  issues:      none")
    return EXIT_OK


def build_scene_parser(subparsers) -> None:
    parser = subparsers.add_parser("scene", help="List, validate and inspect scenes.")
    scene_sub = parser.add_subparsers(dest="scene_command")

    common = lambda p: p.add_argument("--project", help="Path to a project.json (default: most recent).")  # noqa: E731

    list_parser = scene_sub.add_parser("list", help="List every scene with its timing.")
    common(list_parser)
    list_parser.set_defaults(func=command_scene_list)

    validate_parser = scene_sub.add_parser("validate", help="Validate every scene.")
    common(validate_parser)
    validate_parser.set_defaults(func=command_scene_validate)

    info_parser = scene_sub.add_parser("info", help="Show full detail for one scene.")
    common(info_parser)
    info_parser.add_argument("scene", help="The scene id to inspect.")
    info_parser.set_defaults(func=command_scene_info)


def run_scene_command(args, paths: AppPaths, settings: Settings) -> int:
    command = getattr(args, "scene_command", None)
    if not command:
        print("Choose a sub-command: list, validate, info.")
        return EXIT_OK
    try:
        return int(args.func(args, paths, settings))
    except ValueError as exc:
        print(str(exc))
        return EXIT_BLOCKED
