"""Stage E memory / performance profiling (directive section 6).

The Stage E claim used to be "memory is controlled by design".  That is a design
statement, not a measurement, so this script measures it.  Each phase reports
the resident set of *this* process at the end of the phase, plus the peak it
reached, so a regression in any one phase shows up as a number rather than as an
argument.

What is measured:

* ``import``        - the application's own modules, before any Qt.
* ``gui``           - a real offscreen QApplication and the main window.
* ``project``       - creating a project with scenes, elements and assets.
* ``load``          - reading that project back from disk.
* ``preview``       - drawing real frames at the export resolution.
* ``plan``          - planning a long-form render (many scenes, no cap).
* ``render``        - a real render of a short project through the real encoder.

Nothing here is estimated.  If a phase cannot run (no FFmpeg, no display
support) it says so and reports nothing for that phase.

Usage::

    python scripts/stage_e_profile.py --data-root /tmp/mgs_profile
    python scripts/stage_e_profile.py --scenes 120 --render-scenes 4
"""

from __future__ import annotations

import argparse
import gc
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MB = 1024.0 * 1024.0


def _rss() -> float:
    """Resident set of this process, in MiB."""
    import psutil

    return psutil.Process(os.getpid()).memory_info().rss / MB


def _peak() -> float:
    import psutil

    process = psutil.Process(os.getpid())
    try:
        return process.memory_info().peak_wset / MB  # Windows
    except AttributeError:
        pass
    try:
        import resource

        # ru_maxrss is KiB on Linux, bytes on macOS.
        raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return raw / 1024.0 if sys.platform != "darwin" else raw / MB
    except Exception:  # noqa: BLE001 - a missing metric is reported, not faked
        return 0.0


class Profiler:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.previous = _rss()

    def mark(self, phase: str, note: str = "") -> float:
        gc.collect()
        now = _rss()
        row = {
            "phase": phase,
            "rss_mib": round(now, 1),
            "delta_mib": round(now - self.previous, 1),
            "peak_mib": round(_peak(), 1),
            "note": note,
        }
        self.rows.append(row)
        self.previous = now
        print(f"{phase:<12} {row['rss_mib']:>9.1f} MiB   "
              f"(+{row['delta_mib']:.1f})   peak {row['peak_mib']:.1f} MiB"
              + (f"   {note}" if note else ""), flush=True)
        return now

    def table(self) -> str:
        lines = [f"{'Phase':<12} {'RSS (MiB)':>10} {'Change':>9} {'Peak':>9}   Notes"]
        for row in self.rows:
            lines.append(f"{row['phase']:<12} {row['rss_mib']:>10.1f} "
                         f"{row['delta_mib']:>+9.1f} {row['peak_mib']:>9.1f}   {row['note']}")
        return "\n".join(lines)


def _make_scene(project, index: int, *, seconds: float = 12.5) -> None:
    from app.project.model import ElementSpec, SceneSpec

    scene = SceneSpec(
        id=f"scene_{index:04d}", name=f"Scene {index + 1}", type="title",
        duration=seconds,
        script=f"Long form scene {index + 1}.",
        elements=[ElementSpec(
            id=f"title_{index:04d}", kind="text", text=f"Scene {index + 1}",
            position={"x": 0.5, "y": 0.5},
            size={"mode": "relative", "value": 0.09})],
    )
    project.add_scene(scene)


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage E memory profiling")
    parser.add_argument("--data-root", default="/tmp/mgs_profile")
    parser.add_argument("--scenes", type=int, default=50,
                        help="Scenes in the long-form planning test")
    parser.add_argument("--render-scenes", type=int, default=4,
                        help="Scenes in the real render test")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--no-gui", action="store_true")
    args = parser.parse_args()

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    root = Path(args.data_root)
    if root.exists():
        import shutil

        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    print("Stage E memory profile")
    print(f"  data root : {root}")
    print(f"  python    : {sys.version.split()[0]}  platform {sys.platform}")
    print()
    print(f"{'Phase':<12} {'RSS (MiB)':>10} {'Change':>9} {'Peak':>9}")

    profile = Profiler()
    profile.mark("start", "interpreter only")

    from app.core.paths import AppPaths
    from app.core.settings import Settings
    from app.project.service import CreateRequest, ProjectService
    from app.tools.ffmpeg import FFmpegTools, discover_ffmpeg

    profile.mark("import", "app modules, no Qt yet")

    discovery = discover_ffmpeg()
    tools = FFmpegTools(discovery)
    print(f"  ffmpeg    : {discovery.ffmpeg.version if discovery.ffmpeg else 'MISSING'}")
    print(f"  ffprobe   : {discovery.ffprobe.version if discovery.ffprobe else 'MISSING'}")

    paths = AppPaths(data_root=root, source_root=Path(__file__).resolve().parents[1],
                     reason="stage-e-profile")
    paths.ensure()
    settings = Settings()
    profile.mark("tools", "FFmpeg probed, paths ready")

    # -- GUI -------------------------------------------------------------
    if not args.no_gui:
        jobs = None
        try:
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance() or QApplication([])
            profile.mark("qt", "QApplication constructed")

            from app.jobs.manager import JobManager
            from app.ui.context import StartupInfo, create_context
            from app.ui.main_window import MainWindow

            jobs = JobManager()
            context = create_context(
                paths, jobs,
                startup=StartupInfo(data_root_reason="profile", directories_created=0,
                                    stale_temp_removed=0))
            window = MainWindow(context, jobs)
            window.show()
            app.processEvents()
            profile.mark("gui", "MainWindow built and shown")
            window.close()
            jobs.shutdown(timeout_ms=4000)
        except Exception as exc:  # noqa: BLE001
            print(f"  GUI phase skipped: {type(exc).__name__}: {exc}")
            if jobs is not None:
                jobs.shutdown(timeout_ms=4000)

    # -- project ---------------------------------------------------------
    service = ProjectService(paths, settings)
    project = service.create_project(CreateRequest(
        name="Profile", description="Memory profile project",
        width=args.width, height=args.height, fps=args.fps, template="blank"))
    project_dir = Path(service.session.layout.root)
    profile.mark("project", "empty project created")

    for index in range(args.render_scenes):
        _make_scene(project, index, seconds=3.0)
    service.save(reason="profile")
    profile.mark("scenes", f"{args.render_scenes} scenes added and saved")

    service.close_project()
    del project
    gc.collect()
    reopened = ProjectService(paths, Settings()).open_project(project_dir / "project.json")
    profile.mark("load", f"reopened with {len(reopened.scenes)} scenes")
    project = reopened

    # -- preview ---------------------------------------------------------
    try:
        from app.render.frames import FrameSource
        from app.scene.canvas import Canvas
        from app.scene.segments_probe import plan_segments  # type: ignore  # noqa: F401
    except ImportError:
        from app.render.segments import plan_segments  # noqa: F401
    try:
        from app.render.frames import FrameSource
        from app.scene.canvas import Canvas
        from app.scene.storyboard import build_context
        from app.scene.timing import build_timeline as _timeline

        canvas = Canvas(args.width, args.height, args.fps)
        preview_ctx = build_context(project, canvas=canvas, project_dir=project_dir)
        source = FrameSource(project, canvas=canvas, ctx=preview_ctx, fps=args.fps)
        timeline = _timeline(project.scenes)
        plan = plan_segments(timeline, fps=args.fps)
        drawn = 0
        for segment in plan.segments[:2]:
            for chunk in source.stream(segment, width=args.width, height=args.height):
                drawn += 1
        profile.mark("preview", f"{drawn} raw frames produced at "
                               f"{args.width}x{args.height}")
        del canvas, preview_ctx, source
    except Exception as exc:  # noqa: BLE001
        print(f"  preview phase skipped: {type(exc).__name__}: {exc}")

    # -- long form planning ----------------------------------------------
    from app.render.service import RenderService
    from app.scene.timing import build_timeline

    service.close_project()
    big = service.create_project(CreateRequest(
        name="LongForm", description="Long form planning", width=args.width,
        height=args.height, fps=args.fps, template="blank"))
    for index in range(args.scenes):
        _make_scene(big, index, seconds=12.5)
    service.save(reason="profile")
    timeline = build_timeline(big.scenes)
    long_dir = Path(service.session.layout.root)
    renders = RenderService(tools, project_dir=long_dir, paths=paths)
    started = time.monotonic()
    plan = renders.plan(big)
    planned = time.monotonic() - started
    profile.mark("plan", f"{args.scenes} scenes / {plan.duration:.0f}s / "
                         f"{plan.frames} frames in {planned:.2f}s")
    print(f"  long-form : {plan.duration:.1f}s = "
          f"{int(plan.duration // 60)}m {plan.duration % 60:.1f}s, "
          f"{plan.segments} segments, no duration cap applied")

    # -- a real render ----------------------------------------------------
    if discovery.has_ffmpeg:
        from app.render import RenderRequest
        from app.render.engine import RenderEngine

        # ``project`` is already in memory from the load phase; reopening it
        # would just trip the single-window lock.
        engine = RenderEngine(tools, project_dir=project_dir, paths=paths)
        started = time.monotonic()
        result = engine.render(RenderRequest(project=project))
        rendered = time.monotonic() - started
        note = (f"{result.status} in {rendered:.1f}s -> "
                f"{Path(result.path).name if result.path else 'no file'}")
        if result.qc is not None:
            note += f", QC {result.qc.verdict}"
        profile.mark("render", note)
    else:
        print("  render phase skipped: FFmpeg is not installed")

    profile.mark("done", "end of profile")

    print()
    print(profile.table())
    peak = max((row["peak_mib"] for row in profile.rows), default=0.0)
    print()
    print(f"Highest resident set seen: {peak:.1f} MiB")
    print("Measured on this machine at this moment; it is not a guarantee for "
          "another machine.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
