"""``mgs ai`` - the AI Studio from the command line (sections 62, 63).

Every command goes through the **same** services the GUI uses:

    ai backends     the AI Backend Manager's detection pass
    ai models       the model list, plus what was found on disk
    ai check        a light or deep check (a deep check runs a real generation)
    ai selftest     a deep check on every usable backend, with what it produced
    ai job status   the studio's job records
    ai library      the video library: list, inspect, thumbnail, scan, add, remove
    ai video        generate a clip
    ai image        generate a still through Stage F's engine
    ai storyboard   build, approve and run a storyboard plan

The command line is deliberately explicit about states: it prints NOT INSTALLED,
NOT VERIFIED, CHECK NOT AVAILABLE and TEST BACKEND in the same words the dossier
uses, because a command line that rounds those up would undo the honesty the
rest of the stage keeps (sections 79, 93).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..ai.types import state_label
from ..core.paths import AppPaths
from ..core.settings import Settings

__all__ = ["build_ai_parser", "run_ai_command"]

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_BLOCKED = 2


def _service(paths: AppPaths, settings: Settings) -> Any:
    """The same service the GUI uses, pointed at the same folders."""
    from ..ai.service import AIService

    return AIService.for_paths(paths, settings)


def _print_heading(title: str) -> None:
    print(f"\n{title}")


def _print_failure(message: str, *, why: str = "", what_to_do: str = "") -> None:
    print(f"\n  X  {message}")
    if why:
        print(f"     Why: {why}")
    if what_to_do:
        print(f"     What to do: {what_to_do}")


def _print_report(payload: dict) -> None:
    """Print the JSON a script can read, or a readable summary."""
    print(json.dumps(payload, indent=2, default=str))


def _print_backends(report: Any) -> None:
    _print_heading("AI backends")
    for entry in report.entries:
        status = entry.status()
        marker = "yes" if status.available else "no "
        kind = str(getattr(entry, "kind", "") or "")
        print(f"  [{marker}] {entry.name} ({entry.id}, {kind}) - "
              f"{state_label(status.state)}")
        print(f"         {status.reason}")
        if status.instructions:
            print(f"         What to do: {' '.join(status.instructions)}")
    _print_heading("Summary")
    print(f"  {report.generator_note()}")


def _print_models(service: Any) -> None:
    _print_heading("Models offered by usable backends")
    models = service.manager.all_models()
    if not models:
        print("  None. No AI model is installed; nothing will be generated.")
    for model in models:
        # An honest label: the fixture and Stage F's editor both write real
        # files, and neither is an AI model.
        label = "AI model" if model["is_ai_model"] else "not an AI model"
        print(f"  {model['name']} - {model['backend']} [{label}]")
        if model["size"]:
            print(f"    size: {model['size']}  path: {model['path']}")
        if model["requirement"]:
            print(f"    requirement: {model['requirement']}")
    _print_heading("Models found on disk")
    found = service.manager.discovered_models()
    if not found:
        print("  Nothing found in the model folders.")
    for item in found:
        print(f"  {item['name']} - {item['kind']}, {item['size'] or 'size unknown'}")
        print(f"    {item['path']}")


def _print_check(result: dict) -> None:
    state = str(result.get("state", ""))
    evidence = result.get("evidence") or {}
    is_model = bool(evidence.get("is_ai_model", True))
    print(f"\n  State: {state_label(state)}")
    print(f"  {result.get('message', '')}")
    if result.get("why"):
        print(f"  Why: {result['why']}")
    if result.get("what_to_do"):
        print(f"  What to do: {result['what_to_do']}")
    if evidence:
        _print_heading("Evidence (measured, not assumed)")
        for key, value in evidence.items():
            print(f"  {key}: {value}")
    if state == "VERIFIED" and is_model:
        print("\n  MODEL VERIFIED: this backend ran on this machine and "
              "produced a real file.")
    elif state == "VERIFIED":
        print("\n  VERIFIED: it ran on this machine and wrote a real file. It "
              "is not an AI model, so this is not AI generation.")
    elif state == "CHECK NOT AVAILABLE":
        print("\n  CHECK NOT AVAILABLE: the check could not run, so nothing is "
              "claimed about this backend.")


def _print_jobs(service: Any) -> None:
    _print_heading("AI jobs")
    records = service.jobs.all()
    if not records:
        print("  No AI jobs have been started in this session.")
    for record in records:
        print(f"  {record.id[:8]}  {record.status:<12} {record.title()}")
        print(f"    {record.progress_label()}  {record.elapsed_label()}  "
              f"ETA {record.eta_label()}")
        if record.output:
            path = record.output.get("path") or record.output.get("paths")
            print(f"    result: {path}")
        if record.error:
            print(f"    {record.error}")


def _print_clip(result: Any) -> None:
    print(f"\n  {result.summary()}")
    if result.mismatch:
        print("  The file differs from the request:")
        for item in result.mismatch:
            print(f"    - {item}")
    if result.path:
        print(f"  File: {result.path}")
    if not result.ok:
        _print_failure(result.error, why=result.why, what_to_do=result.what_to_do)
    if result.options:
        print(f"  Options: {', '.join(result.options)}")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def _command_backends(service: Any, args: Any) -> int:
    report = service.status(refresh=True)
    if getattr(args, "json", False):
        _print_report(report.to_dict())
        return EXIT_OK
    _print_backends(report)
    return EXIT_OK


def _command_models(service: Any, args: Any) -> int:
    if getattr(args, "json", False):
        _print_report({"models": service.manager.all_models(),
                       "on_disk": service.manager.discovered_models()})
        return EXIT_OK
    _print_models(service)
    return EXIT_OK


def _command_check(service: Any, args: Any) -> int:
    backend_id = str(getattr(args, "backend", "") or "")
    if not backend_id:
        backend_id = service.manager.default_backend("video") or \
            service.manager.default_backend("image")
    entry = service.manager.get(backend_id)
    if entry is None or entry.backend is None:
        _print_failure(f"No backend called '{backend_id}' was found.",
                       what_to_do="Run 'mgs ai backends' to see the list.")
        return EXIT_PROBLEMS
    if getattr(args, "deep", False):
        result = service.self_test(backend_id)
    else:
        report = entry.backend.check(deep=False)
        result = {"ok": bool(report.ok), "state": str(report.state),
                  "message": report.message, "why": report.why,
                  "what_to_do": report.what_to_do,
                  "evidence": dict(report.evidence)}
    if getattr(args, "json", False):
        _print_report(result)
    else:
        _print_check(result)
    return EXIT_OK if result.get("ok") else EXIT_PROBLEMS


def _command_selftest(service: Any, args: Any) -> int:
    """Deep-check every usable backend, and say what each really produced."""
    kind = str(getattr(args, "kind", "") or "")
    entries = service.manager.usable_backends(kind=kind) \
        if kind else service.manager.usable_backends()
    if not entries:
        _print_failure(
            "No usable backend was found, so nothing could be tested.",
            why="Nothing is installed or configured that can generate.",
            what_to_do="Run 'mgs ai backends' to see what each one needs.")
        return EXIT_PROBLEMS
    results: list[dict] = []
    failures = 0
    for entry in entries:
        result = service.self_test(entry.id)
        result["backend"] = entry.id
        result["backend_name"] = entry.name
        results.append(result)
        if not result.get("ok"):
            failures += 1
        if getattr(args, "json", False):
            continue
        _print_heading(f"Test: {entry.name} ({entry.id})")
        _print_check(result)
        if not bool(getattr(entry.backend, "is_model", True)):
            print("  Note: this backend does not run an AI model. The file it "
                  "wrote is real; the generation was not AI.")
    if getattr(args, "json", False):
        _print_report({"results": results,
                       "tested": len(results), "failed": failures})
    else:
        _print_heading("Result")
        print(f"  {len(results) - failures} of {len(results)} backend(s) "
              f"produced a real file.")
        if not service.has_ai_model():
            print("  REAL AI MODEL VERIFICATION: PENDING - no AI model is "
                  "installed on this machine.")
    return EXIT_OK if failures == 0 else EXIT_PROBLEMS


def _command_job_status(service: Any, args: Any) -> int:
    job_id = str(getattr(args, "job_id", "") or "")
    if job_id:
        record = service.jobs.get(job_id)
        if record is None:
            _print_failure(f"No job called '{job_id}' was found.",
                           what_to_do="Run 'mgs ai job status' to list the jobs.")
            return EXIT_PROBLEMS
        if getattr(args, "json", False):
            _print_report(record.to_dict())
        else:
            _print_heading(f"Job {record.id}")
            for key, value in record.to_dict().items():
                if key in ("request", "output") and value:
                    print(f"  {key}: {value}")
                elif key not in ("request", "output"):
                    print(f"  {key}: {value}")
        return EXIT_OK
    if getattr(args, "json", False):
        _print_report({"jobs": [record.to_dict() for record in service.jobs.all()]})
        return EXIT_OK
    _print_jobs(service)
    return EXIT_OK


def _command_video(service: Any, args: Any) -> int:
    from ..ai.video import VideoRequest

    output_dir = Path(args.output_dir) if getattr(args, "output_dir", "") else \
        Path(service.data_root or Path.cwd()) / "output" / "ai"
    request = VideoRequest(
        mode=str(args.mode), backend=str(args.backend or ""),
        model=str(args.model or ""), prompt=str(args.prompt or ""),
        negative_prompt=str(getattr(args, "negative", "") or ""),
        duration=float(args.duration or 0.0), fps=int(args.fps or 0),
        width=int(args.width or 0), height=int(args.height or 0),
        seed=int(args.seed or 0), strength=float(getattr(args, "strength", 0.0) or 0.0),
        source_image=str(getattr(args, "source", "") or ""),
        source_video=str(getattr(args, "source_video", "") or ""),
        extend_from=str(getattr(args, "extend_from", "") or ""),
        camera=str(getattr(args, "camera", "") or ""),
        camera_amount=float(getattr(args, "camera_amount", 0.0) or 0.0),
        output_dir=str(output_dir), name_stem=str(args.name or "clip"),
        quality=str(getattr(args, "quality", "") or ""),
        batch=int(getattr(args, "batch", 1) or 1))
    if not service.manager.get(request.backend or service.manager.default_backend("video")):
        _print_failure(
            "No backend is available to make a clip.",
            why="Nothing is installed or configured that can generate video.",
            what_to_do="Run 'mgs ai backends' to see what each one needs.")
        return EXIT_PROBLEMS

    def progress(state: str, fraction: float) -> None:
        if not getattr(args, "quiet", False):
            print(f"  {state}: {fraction * 100:.0f}%", end="\r", flush=True)

    result = service.generate_video(request, backend_id=request.backend,
                                    progress=progress)
    if not getattr(args, "quiet", False):
        print(" " * 40, end="\r")
    if getattr(args, "json", False):
        _print_report(result.to_dict())
    else:
        _print_clip(result)
    return EXIT_OK if result.ok else EXIT_PROBLEMS


def _command_image(service: Any, args: Any) -> int:
    from ..image.provider import GenerationRequest

    output_dir = Path(args.output_dir) if getattr(args, "output_dir", "") else \
        Path(service.data_root or Path.cwd()) / "output" / "ai"
    backend_id = str(getattr(args, "backend", "") or "")
    request = GenerationRequest(
        prompt=str(args.prompt or ""), model=str(args.model or ""),
        width=int(args.width or 0), height=int(args.height or 0),
        seed=int(args.seed or 0), batch=int(getattr(args, "batch", 1) or 1),
        output_dir=str(output_dir), name_stem=str(args.name or "image"))
    result = service.generate_image(request, backend_id=backend_id)
    if getattr(args, "json", False):
        _print_report({"ok": bool(result.ok), "paths": list(result.paths),
                       "seeds": list(result.seeds), "backend": result.backend,
                       "model": result.model, "error": result.error,
                       "why": result.why, "what_to_do": result.what_to_do})
        return EXIT_OK if result.ok else EXIT_PROBLEMS
    if result.ok:
        _print_heading("Image generated")
        for path in result.paths:
            print(f"  {path}")
        print(f"  seed(s): {', '.join(str(seed) for seed in result.seeds)}")
    else:
        _print_failure(result.error, why=result.why, what_to_do=result.what_to_do)
    return EXIT_OK if result.ok else EXIT_PROBLEMS


def _command_storyboard(service: Any, args: Any) -> int:
    """Build a plan from a project's storyboard, and optionally run it."""
    from ..ai.integration import send_to_timeline
    from ..project.service import ProjectService

    project_path = Path(args.project).expanduser()
    project_file = project_path / "project.json" if project_path.is_dir() else project_path
    if not project_file.is_file():
        _print_failure(f"No project was found at {project_path}",
                       what_to_do="Give the project folder or its project.json.")
        return EXIT_PROBLEMS
    service_ = ProjectService(service.paths, service.settings)
    service_.open_project(project_file)
    project = service_.current
    plan = service.storyboard_plan(project, backend_id=str(args.backend or ""),
                                   width=int(args.width or 0),
                                   height=int(args.height or 0),
                                   fps=int(args.fps or 0),
                                   project_dir=project_file.parent)
    _print_heading("Storyboard plan")
    for entry in plan:
        print(f"  {entry['index']}. {entry['name']} ({entry['duration']:.1f}s, "
              f"seed {entry['seed']})")
        print(f"     prompt: {entry['prompt']}")
        if entry["source_image"]:
            print(f"     from image: {entry['source_image']}")
    from ..ai.service import plan_summary

    print(f"\n  {plan_summary(plan)}")
    if not args.yes:
        print("  Nothing was generated. Re-run with --yes to approve these "
              "prompts and generate the clips.")
        return EXIT_OK
    service.approve_plan(plan)
    requests = service.requests_from_plan(
        plan, backend_id=str(args.backend or ""),
        output_dir=str(args.output_dir or ""), project=str(project_file.parent))
    _ = send_to_timeline  # used below when --send-to-timeline is given
    failures = 0
    sent = 0
    for request in requests:
        result = service.generate_video(request, backend_id=str(args.backend or ""))
        if getattr(args, "json", False):
            continue
        _print_clip(result)
        if not result.ok:
            failures += 1
            continue
        if getattr(args, "send_to_timeline", False) and result.path:
            report = send_to_timeline(service_, result.path,
                                      placement="end", duration=result.duration,
                                      fps=result.fps)
            if report.ok:
                sent += 1
            print(f"  {report.describe()}")
    if getattr(args, "json", False):
        _print_report({"planned": len(plan), "generated": len(requests) - failures,
                       "failures": failures, "sent_to_timeline": sent})
    return EXIT_OK if failures == 0 else EXIT_PROBLEMS


def _dispatch(args: Any) -> int:
    from .main import _bootstrap, _print_header

    paths, load_result = _bootstrap(args)
    if getattr(args, "ai_command", None):
        _print_header(paths)
    return run_ai_command(args, paths, load_result.settings)


def run_ai_command(args: Any, paths: AppPaths, settings: Settings) -> int:
    """Run one ``ai`` subcommand.  Returns a process exit code."""
    command = str(getattr(args, "ai_command", "") or "")
    service = _service(paths, settings)
    handlers = {
        "backends": _command_backends, "models": _command_models,
        "check": _command_check, "selftest": _command_selftest,
        "job": lambda svc, a: _command_job_status(svc, a),
        "video": _command_video, "image": _command_image,
        "storyboard": _command_storyboard,
        "library": _command_library,
    }
    handler = handlers.get(command)
    if handler is None:
        print("Choose a command: backends, models, check, selftest, job, "
              "library, video, image or storyboard.")
        return EXIT_BLOCKED
    if command == "job" and str(getattr(args, "job_command", "") or "") == "status":
        return _command_job_status(service, args)
    return handler(service, args)


def _find_clip(library: Any, wanted: str) -> Any:
    """A clip by id, by path, or by the name a user sees in the list."""
    wanted = str(wanted or "").strip()
    if not wanted:
        return None
    entry = library.find(wanted)
    if entry is None:
        entry = library.for_path(wanted)
    if entry is None:
        name = Path(wanted).name.lower()
        entry = next((item for item in library.all()
                      if item.filename.lower() == name
                      or item.name.lower() == Path(wanted).stem.lower()), None)
    return entry


def _library(service: Any) -> Any:
    """The service's library, which is the same store the GUI page shows."""
    library = getattr(service, "library", None)
    if library is None:
        from ..ai.video_library_jobs import video_library_service

        library = service.library = video_library_service(
            paths=getattr(service, "paths", None), tools=service.tools)
    return library


def _library_row(entry: Any) -> None:
    print(f"  {entry.id}  {entry.display_name()}")
    facts = entry.measurements() or "not measured"
    print(f"      {facts} | {entry.source_label()} | {entry.status}")
    who = entry.provenance()
    if who:
        print(f"      {who}")


def _command_library(service: Any, args: Any) -> int:
    """``ai library`` - the video library from the command line (section 62)."""
    from ..ai.video_library import LibraryQuery
    from ..ai.video_library_jobs import (library_import_body,
                                         library_remove_body,
                                         library_scan_body,
                                         library_recheck_body,
                                         library_thumbnails_body,
                                         video_library_service)

    library = getattr(service, "library", None) or video_library_service(
        paths=getattr(service, "paths", None), tools=service.tools)
    what = str(getattr(args, "library_command", "") or "")

    if what == "list":
        query = LibraryQuery(
            text=str(getattr(args, "search", "") or ""),
            source=str(getattr(args, "source", "") or ""),
            sort=str(getattr(args, "sort", "") or "recent"),
            favourites_only=bool(getattr(args, "favourites", False)),
            missing_only=bool(getattr(args, "missing", False)),
            tag=str(getattr(args, "tag", "") or ""),
            collection=str(getattr(args, "collection", "") or ""),
            limit=int(getattr(args, "limit", 0) or 0),
            offset=int(getattr(args, "offset", 0) or 0))
        page = library.query(query)
        if getattr(args, "json", False):
            _print_report({"total": page.total, "returned": len(page.entries),
                           "has_more": page.has_more,
                           "clips": [entry.to_dict() for entry in page.entries]})
            return EXIT_OK
        _print_heading("Video library")
        print(f"  {library.describe()}")
        if not page.entries:
            print("  Nothing matches. Press 'Scan' in the application, or run "
                  "'mgs ai library scan'.")
            return EXIT_OK
        for entry in page.entries:
            _library_row(entry)
        if page.has_more:
            print(f"  ... {page.total - len(page.entries)} more (use --limit "
                  f"and --offset)")
        return EXIT_OK

    if what == "inspect":
        wanted = str(getattr(args, "clip", "") or "")
        entry = _find_clip(library, wanted)
        if entry is None:
            _print_failure(
                f"No clip in the library matches '{wanted}'.",
                why="The library indexes the clips in its own folder plus any "
                    "clip that was generated or imported here.",
                what_to_do="Run 'mgs ai library list' to see what is indexed, or "
                           "'mgs ai library add <file>' to index one.")
            return EXIT_PROBLEMS
        if getattr(args, "json", False):
            _print_report(entry.to_dict())
            return EXIT_OK
        _print_heading(entry.display_name())
        print(f"  id:      {entry.id}")
        print(f"  file:    {entry.path}")
        print(f"  status:  {entry.status}")
        print(f"  source:  {entry.source_label()}")
        facts = entry.measurements() or "not measured here"
        print(f"  facts:   {facts}")
        print(f"  made:    {entry.provenance() or 'no provenance recorded'}")
        if entry.prompt:
            print(f"  prompt:  {entry.prompt}")
        if entry.seed:
            print(f"  seed:    {entry.seed}")
        if entry.collection or entry.tags:
            print(f"  folder:  {entry.collection or '-'}"
                  f"  tags: {', '.join(entry.tags) or '-'}")
        if entry.thumbnail:
            print(f"  picture: {entry.thumbnail}")
        if not entry.measured_with:
            print("  note:    no FFprobe is available, so the numbers above are "
                  "not measurements")
        return EXIT_OK

    if what == "thumbnail":
        wanted = str(getattr(args, "clip", "") or "")
        entry = _find_clip(library, wanted)
        if entry is None:
            _print_failure(
                f"No clip in the library matches '{wanted}'.",
                why="A picture is only taken from a clip the library knows.",
                what_to_do="Use 'mgs ai library list' to find the clip's id.")
            return EXIT_PROBLEMS
        picture = library.thumbnail_for(entry.id,
                                       regenerate=bool(getattr(args, "again", False)))
        if picture is None:
            _print_failure(
                f"No picture could be taken from {entry.filename}.",
                why="FFmpeg is missing, or the file could not be read as video.",
                what_to_do="Run 'mgs check' to see whether FFmpeg was found.")
            return EXIT_PROBLEMS
        print(f"  {picture}")
        return EXIT_OK

    if what == "scan":
        folder = str(getattr(args, "folder", "") or "")
        if folder:
            library.ensure_root()
        body = library_scan_body(_job_context(
            library, service, folder=folder,
            measure=not bool(getattr(args, "no_measure", False))))
        if getattr(args, "json", False):
            _print_report(body)
            return EXIT_OK if body.get("ok") else EXIT_PROBLEMS
        print(f"  {body.get('message', '')}")
        for record in body.get("videos", [])[:20]:
            print(f"    {record['name']} - {record.get('measurements') or 'not measured'}")
        return EXIT_OK if body.get("ok") else EXIT_PROBLEMS

    if what == "add":
        path = str(getattr(args, "path", "") or "")
        body = library_import_body(_job_context(
            library, service, path=path,
            source=str(getattr(args, "source", "") or ""),
            name=str(getattr(args, "name", "") or "")))
        if getattr(args, "json", False):
            _print_report(body)
        elif body.get("ok"):
            print(f"  {body.get('message', '')}")
        else:
            _print_failure(body.get("message", ""), why=body.get("why", ""),
                           what_to_do=body.get("what_to_do", ""))
        return EXIT_OK if body.get("ok") else EXIT_PROBLEMS

    if what == "recheck":
        wanted = str(getattr(args, "clip", "") or "")
        entry = _find_clip(library, wanted)
        if entry is None:
            _print_failure(f"No clip in the library matches '{wanted}'.",
                           why="Only an indexed clip can be measured again.",
                           what_to_do="Use 'mgs ai library list' to find its id.")
            return EXIT_PROBLEMS
        body = library_recheck_body(_job_context(library, service, id=entry.id))
        print(f"  {body.get('message', '')}")
        return EXIT_OK if body.get("ok") else EXIT_PROBLEMS

    if what == "remove":
        wanted = str(getattr(args, "clip", "") or "")
        entry = _find_clip(library, wanted)
        if entry is None:
            _print_failure(f"No clip in the library matches '{wanted}'.",
                           why="Nothing with that id or path is indexed.",
                           what_to_do="Use 'mgs ai library list' to find its id.")
            return EXIT_PROBLEMS
        body = library_remove_body(_job_context(
            library, service, ids=[entry.id],
            delete_file=bool(getattr(args, "delete_file", False))))
        print(f"  {body.get('message', '')}")
        return EXIT_OK

    if what == "thumbnails":
        body = library_thumbnails_body(_job_context(library, service))
        print(f"  {body.get('message', '')}")
        for item in body.get("failed", []):
            print(f"    {item['name']}: {item['reason']}")
        return EXIT_OK

    print("Choose a library command: list, inspect, thumbnail, scan, add, "
          "recheck, remove or thumbnails.")
    return EXIT_BLOCKED


def _job_context(library: Any, service: Any, **payload: Any) -> Any:
    """A job context for one library command, as the jobs would build it.

    The GUI runs these same bodies inside a job; the command line runs them
    directly, so it passes the same payload and a cancel token that is never
    cancelled (the user's Ctrl+C ends the process, and the file work is small).
    """
    from ..jobs.spec import JobContext

    class _NoCancel:
        job_id = "cli"
        cancelled = False

        def is_cancelled(self) -> bool:
            return False

        def raise_if_cancelled(self) -> None:
            return None

        def register_process(self, process: Any) -> None:
            return None

        def unregister_process(self, process: Any) -> None:
            return None

        def active_process_count(self) -> int:
            return 0

        def terminate_children(self, grace_seconds: float = 5.0) -> int:
            return 0

    class _NoProgress:
        """The shape a progress reporter has: the bodies read ``.progress.total``."""

        class _Total:
            total = 0.0

        def __init__(self) -> None:
            self.progress = self._Total()

        def start(self, **kwargs: Any) -> None:
            self.progress.total = float(kwargs.get("total", 1.0) or 0.0)

        def update(self, **kwargs: Any) -> None:
            return None

    return JobContext(
        job_id="cli", key="video.library", cancel=_NoCancel(),
        progress=_NoProgress(), settings=None,
        paths=getattr(service, "paths", None),
        payload={"library": library, "tools": getattr(service, "tools", None),
                 **payload})


def build_ai_parser(subparsers: Any) -> None:
    """Register ``mgs ai ...``."""
    ai = subparsers.add_parser(
        "ai", help="AI Studio: local backends, models, checks and generation.")
    ai.set_defaults(func=_dispatch)
    ai_sub = ai.add_subparsers(dest="ai_command")

    backends = ai_sub.add_parser(
        "backends", help="List the local AI backends and their states.")
    backends.add_argument("--json", action="store_true")

    models = ai_sub.add_parser("models", help="List models, and what is on disk.")
    models.add_argument("--json", action="store_true")

    check = ai_sub.add_parser(
        "check", help="Check one backend. --deep runs a real generation.")
    check.add_argument("--backend", default="")
    check.add_argument("--deep", action="store_true",
                       help="Initialise the backend and produce a real file. "
                            "Only this can report VERIFIED.")
    check.add_argument("--json", action="store_true")

    selftest = ai_sub.add_parser(
        "selftest", help="Deep-check every usable backend, one at a time.")
    selftest.add_argument("--kind", default="",
                          choices=("", "image", "video", "upscale",
                                   "background_removal"))
    selftest.add_argument("--json", action="store_true")

    job = ai_sub.add_parser("job", help="Look at the AI job records.")
    job_sub = job.add_subparsers(dest="job_command")
    status = job_sub.add_parser("status", help="List jobs, or show one.")
    status.add_argument("job_id", nargs="?", default="")
    status.add_argument("--json", action="store_true")

    video = ai_sub.add_parser("video", help="Generate a clip.")
    video.add_argument("--mode", default="text_to_video",
                       choices=("text_to_video", "image_to_video",
                                "video_to_video", "extend",
                                "storyboard_to_video"))
    video.add_argument("--backend", default="")
    video.add_argument("--model", default="")
    video.add_argument("--prompt", default="")
    video.add_argument("--negative", default="")
    video.add_argument("--duration", type=float, default=0.0)
    video.add_argument("--fps", type=int, default=0)
    video.add_argument("--width", type=int, default=0)
    video.add_argument("--height", type=int, default=0)
    video.add_argument("--seed", type=int, default=0)
    video.add_argument("--batch", type=int, default=1,
                       help="Explicitly generate several clips, one after another.")
    video.add_argument("--strength", type=float, default=0.0)
    video.add_argument("--camera", default="")
    video.add_argument("--camera-amount", dest="camera_amount", type=float,
                       default=0.0)
    video.add_argument("--source", default="", help="Source image (image to video).")
    video.add_argument("--source-video", dest="source_video", default="")
    video.add_argument("--extend-from", dest="extend_from", default="")
    video.add_argument("--quality", default="", choices=("", "draft", "medium",
                                                         "high", "ultra"))
    video.add_argument("--output-dir", dest="output_dir", default="")
    video.add_argument("--name", default="clip")
    video.add_argument("--quiet", action="store_true")
    video.add_argument("--json", action="store_true")

    library = ai_sub.add_parser(
        "library", help="The video library: list, inspect, thumbnail, scan, add.")
    library_sub = library.add_subparsers(dest="library_command")
    listing = library_sub.add_parser("list", help="List the clips in the library.")
    listing.add_argument("--search", default="")
    listing.add_argument("--source", default="", choices=("", "generated", "render",
                                                          "imported"))
    listing.add_argument("--sort", default="recent",
                         choices=("recent", "oldest", "name", "duration", "size",
                                  "resolution"))
    listing.add_argument("--tag", default="")
    listing.add_argument("--collection", default="")
    listing.add_argument("--favourites", action="store_true")
    listing.add_argument("--missing", action="store_true",
                         help="Only the clips whose file has gone.")
    listing.add_argument("--limit", type=int, default=0)
    listing.add_argument("--offset", type=int, default=0)
    listing.add_argument("--json", action="store_true")

    inspect = library_sub.add_parser("inspect", help="Everything known about one clip.")
    inspect.add_argument("clip", help="The clip's id, or its file path.")
    inspect.add_argument("--json", action="store_true")

    thumbnail = library_sub.add_parser("thumbnail", help="Take a picture from a clip.")
    thumbnail.add_argument("clip")
    thumbnail.add_argument("--again", action="store_true",
                           help="Make the picture again even if one is cached.")

    scan = library_sub.add_parser(
        "scan", help="Index the library folder, or a folder you name.")
    scan.add_argument("folder", nargs="?", default="")
    scan.add_argument("--no-measure", dest="no_measure", action="store_true",
                      help="Index the files without measuring them.")
    scan.add_argument("--json", action="store_true")

    add = library_sub.add_parser("add", help="Index one file that already exists.")
    add.add_argument("path")
    add.add_argument("--source", default="", choices=("", "generated", "render",
                                                      "imported"))
    add.add_argument("--name", default="")
    add.add_argument("--json", action="store_true")

    recheck = library_sub.add_parser("recheck", help="Measure one clip again.")
    recheck.add_argument("clip")

    remove = library_sub.add_parser("remove", help="Forget a clip.")
    remove.add_argument("clip")
    remove.add_argument("--delete-file", dest="delete_file", action="store_true",
                        help="Also delete the file itself. Without this the file "
                             "is left where it is.")

    library_sub.add_parser("thumbnails",
                           help="Make the pictures for every clip that has none.")

    image = ai_sub.add_parser("image", help="Generate a still image.")
    image.add_argument("--backend", default="")
    image.add_argument("--model", default="")
    image.add_argument("--prompt", default="")
    image.add_argument("--width", type=int, default=512)
    image.add_argument("--height", type=int, default=512)
    image.add_argument("--seed", type=int, default=0)
    image.add_argument("--batch", type=int, default=1)
    image.add_argument("--output-dir", dest="output_dir", default="")
    image.add_argument("--name", default="image")
    image.add_argument("--json", action="store_true")

    storyboard = ai_sub.add_parser(
        "storyboard", help="Build a plan from a project's storyboard, and run it.")
    storyboard.add_argument("project", help="The project folder or project.json.")
    storyboard.add_argument("--backend", default="")
    storyboard.add_argument("--width", type=int, default=0)
    storyboard.add_argument("--height", type=int, default=0)
    storyboard.add_argument("--fps", type=int, default=0)
    storyboard.add_argument("--output-dir", dest="output_dir", default="")
    storyboard.add_argument("--yes", action="store_true",
                            help="Approve the plan's prompts and generate the "
                                 "clips. Without this, the plan is only printed.")
    storyboard.add_argument("--send-to-timeline", dest="send_to_timeline",
                            action="store_true",
                            help="Also add each finished clip to the project's "
                                 "timeline.")
    storyboard.add_argument("--json", action="store_true")
