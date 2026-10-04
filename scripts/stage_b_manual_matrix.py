"""Stage B manual verification matrix.

Runs the ten scenarios the directive requires, through the same code paths the
interface uses (ProjectController + ProjectService), and prints what happened.
This is evidence, not a substitute for the automated suite.

    LD_LIBRARY_PATH=/tmp/stublib QT_QPA_PLATFORM=offscreen \
        /home/user/.venv/bin/python scripts/stage_b_manual_matrix.py --data-root /tmp/mgs_evidence
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.paths import AppPaths  # noqa: E402
from app.core.settings import Settings  # noqa: E402
from app.jobs.manager import JobManager  # noqa: E402
from app.project.layout import ProjectLayout  # noqa: E402
from app.project.model import SceneSpec  # noqa: E402
from app.project.service import CreateRequest, ProjectService  # noqa: E402
from app.project.store import ProjectStore  # noqa: E402
from app.project.validation import validate_for_render  # noqa: E402
from app.ui.context import StartupInfo, create_context  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def record(number: int, name: str, ok: bool, detail: str) -> None:
    RESULTS.append((f"{number:2d}", "PASS" if ok else "FAIL", f"{name} - {detail}"))
    print(f"[{'PASS' if ok else 'FAIL'}] {number:02d} {name}: {detail}")


def new_window(root: Path) -> MainWindow:
    paths = AppPaths(data_root=root, source_root=Path(__file__).resolve().parents[1], reason="manual matrix")
    paths.ensure()
    jobs = JobManager()
    context = create_context(
        paths, jobs, startup=StartupInfo(data_root_reason=paths.reason, directories_created=0, stale_temp_removed=0)
    )
    return MainWindow(context, jobs)


def scenario_1(root: Path) -> None:
    """Create, save, close and reopen."""
    window = new_window(root)
    controller = window.context.projects
    request = CreateRequest(name="Matrix One", channel_name="YouTube", template="youtube", quality="high")
    project = controller.service.create_project(request)
    controller._after_open(project)
    window.project_page.script_edit.setPlainText("Opening line.\n\nSecond paragraph.")
    window.project_page._apply_script()
    saved = controller.save(window)
    folder = controller.layout.root
    controller.close(window)

    window2 = new_window(root)
    reopened = window2.context.projects.service.open_project(folder)
    record(
        1,
        "create / save / close / reopen",
        saved and reopened.script.source_text == "Opening line.\n\nSecond paragraph.",
        f"reopened '{reopened.project.name}' with the script intact, version {reopened.project.project_version}",
    )
    window2.close()


def scenario_2(root: Path) -> None:
    """Autosave, force-close (no clean shutdown), recover."""
    paths = AppPaths(data_root=root, source_root=Path(__file__).resolve().parents[1], reason="manual matrix")
    service = ProjectService(paths, Settings())
    service.create_project(CreateRequest(name="Matrix Crash"))
    service.save()
    service.set_script_text("Work typed just before the crash.")
    autosave_path = service.autosave()
    layout = service.current_layout
    # Force-close: no close_project(), so the autosave stays behind.  The lock
    # is left with a pid that no longer exists, exactly as a crash would.
    lock_file = layout.root / ".project.lock.json"
    if lock_file.is_file():
        import json as _json

        info = _json.loads(lock_file.read_text(encoding="utf-8"))
        info["pid"] = 999999999
        lock_file.write_text(_json.dumps(info), encoding="utf-8")
    del service

    window = new_window(root)
    candidates = window.context.projects.service.scan_recovery()
    found = any(candidate.path.parent == layout.autosave_dir for candidate in candidates)
    restored = None
    if found:
        restored = window.context.projects.service.restore_recovery(candidates[0])
    record(
        2,
        "autosave + force-close + recovery",
        bool(autosave_path) and found and restored is not None
        and restored.script.source_text == "Work typed just before the crash.",
        f"autosave written, {len(candidates)} candidate(s) offered, restored text matches",
    )
    window.close()


def scenario_3(root: Path) -> None:
    """Save As produces a fully independent project."""
    window = new_window(root)
    controller = window.context.projects
    controller.service.create_project(CreateRequest(name="Matrix Original"))
    controller._after_open(controller.service.current)
    folder = controller.layout.root
    controller.service.set_script_text("Only in the original.")
    controller.save(window)
    original_id = controller.service.current.project.id

    # Save As switches the open project to the copy.
    copy = controller.service.save_as("Matrix Copy", root / "projects" / "Matrix Copy")
    controller.service.set_script_text("Only in the copy.")
    controller.save(window)

    original_text = json.loads((folder / "project.json").read_text(encoding="utf-8"))["script"]["source_text"]
    copy_text = json.loads((root / "projects" / "Matrix Copy" / "project.json").read_text(encoding="utf-8"))["script"]["source_text"]
    record(
        3,
        "Save As is independent",
        original_text == "Only in the original."
        and copy_text == "Only in the copy."
        and copy.project.id != original_id
        and controller.layout.root != folder,
        f"original kept '{original_text}', copy has '{copy_text}', different ids and folders",
    )
    window.close()


def scenario_4(root: Path) -> None:
    """Duplicate copies assets and is independent."""
    window = new_window(root)
    controller = window.context.projects
    controller.service.create_project(CreateRequest(name="Matrix Duplicate Source"))
    controller._after_open(controller.service.current)
    source = root / "logo-matrix.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 200)
    controller.service.import_asset(source)
    controller.save(window)
    source_id = controller.service.current.project.id

    duplicate = controller.service.duplicate_project(controller.layout.root, name="Matrix Duplicate", copy_assets=True)
    dup_assets = duplicate.assets
    copied = root / "projects" / "Matrix Duplicate" / dup_assets[0].path
    record(
        4,
        "duplicate is independent",
        duplicate.project.id != source_id
        and len(dup_assets) == 1
        and copied.is_file()
        and not any(str(asset.absolute_path) for asset in dup_assets),
        f"{len(dup_assets)} asset copied into the duplicate, relative path kept",
    )
    window.close()


def scenario_5(root: Path) -> None:
    """Move the project folder: relative assets keep working."""
    window = new_window(root)
    controller = window.context.projects
    controller.service.create_project(CreateRequest(name="Matrix Portable"))
    controller._after_open(controller.service.current)
    source = root / "portable.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 200)
    controller.service.import_asset(source)
    controller.save(window)
    old_folder = controller.layout.root
    controller.close(window)

    new_folder = root / "moved" / "Matrix Portable"
    new_folder.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old_folder), str(new_folder))

    window2 = new_window(root)
    project = window2.context.projects.service.open_project(new_folder)
    checks = window2.context.projects.service.verify_assets()
    absolute = [asset for asset in project.assets if asset.absolute_path]
    record(
        5,
        "folder moved + relative assets",
        all(check.exists for check in checks) and not absolute,
        f"{len(checks)} asset(s) still found after the move, {len(absolute)} absolute path(s)",
    )
    window2.close()


def scenario_6(root: Path) -> None:
    """A damaged project.json is quarantined and recoverable."""
    window = new_window(root)
    controller = window.context.projects
    controller.service.create_project(CreateRequest(name="Matrix Damaged"))
    controller._after_open(controller.service.current)
    window.project_page.script_edit.setPlainText("Text worth keeping.")
    window.project_page._apply_script()
    controller.save(window)
    folder = controller.layout.root
    controller.close(window)

    (folder / "project.json").write_text("{ this is not json", encoding="utf-8")

    window2 = new_window(root)
    result = ProjectStore().load(folder / "project.json")
    quarantined = list((folder / "backups").glob("*.json"))
    record(
        6,
        "corrupt project.json recovery",
        not result.ok and result.friendly is not None and bool(quarantined),
        f"reported '{result.friendly.title}' and kept {len(quarantined)} quarantined copy(ies) in backups/",
    )
    window2.close()


def scenario_7(root: Path) -> None:
    """A missing asset is reported and can be relinked."""
    window = new_window(root)
    controller = window.context.projects
    controller.service.create_project(CreateRequest(name="Matrix Missing"))
    controller._after_open(controller.service.current)
    source = root / "will-move.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 200)
    controller.service.import_asset(source)
    controller.save(window)
    asset = controller.service.current.assets[0]
    (controller.layout.root / asset.path).unlink()

    checks = controller.service.verify_assets()
    reported = [check for check in checks if not check.exists]
    moved = root / "renamed-elsewhere.png"
    moved.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 200)
    relinked = controller.service.relink_asset(asset.id, moved)
    after = controller.service.verify_assets()
    record(
        7,
        "missing asset report + relink",
        len(reported) == 1 and relinked and all(check.exists for check in after),
        f"reported '{reported[0].asset.name}' at {reported[0].path}, relinked to {moved.name}",
    )
    window.close()


def scenario_8(root: Path) -> None:
    """1080p / High survives a save and reopen."""
    window = new_window(root)
    controller = window.context.projects
    controller.service.create_project(
        CreateRequest(name="Matrix Quality", width=1920, height=1080, fps=30, quality="high")
    )
    controller._after_open(controller.service.current)
    controller.save(window)
    folder = controller.layout.root
    controller.close(window)

    window2 = new_window(root)
    project = window2.context.projects.service.open_project(folder)
    fmt = project.format
    record(
        8,
        "1080p / High persists",
        (fmt.width, fmt.height, fmt.fps) == (1920, 1080, 30)
        and fmt.quality_preset == "high"
        and fmt.codec == "h264_cpu"
        and fmt.crf == 20,
        f"{fmt.width}x{fmt.height}@{fmt.fps} {fmt.quality_preset} {fmt.codec} CRF {fmt.crf}",
    )
    window2.close()


def scenario_9(root: Path) -> None:
    """Impossible combinations are caught before a render would start."""
    from app.project.model import build_project
    from app.project.presets import resolve_quality

    video = build_project("Matrix Invalid", width=3840, height=2160, fps=60, quality=resolve_quality("ultra"))
    video.format.codec = "vp9_cpu"          # not valid in an MP4 container
    video.format.container = "mp4"
    video.format.audio_codec = "opus"       # not valid in MP4 either
    report = validate_for_render(video, None)
    codes = {issue.code for issue in report.errors}
    record(
        9,
        "invalid render settings caught",
        not report.ok and {"CODEC_CONTAINER", "AUDIO_CODEC"} <= codes,
        f"{len(report.errors)} error(s): {sorted(codes)}",
    )


def scenario_10(root: Path) -> None:
    """A large project still opens, validates and saves quickly."""
    from app.project.model import build_project

    paths = AppPaths(data_root=root, source_root=Path(__file__).resolve().parents[1], reason="manual matrix")
    paths.ensure()
    big = build_project("Matrix Large")
    for index in range(300):
        big.scenes.append(SceneSpec(id=f"scene-{index}", name=f"Scene {index}", duration=4.0, script="text"))
    big.ensure_ids()

    store = ProjectStore()
    layout = ProjectLayout(paths.projects_dir / "Matrix Large")
    layout.ensure()
    started = time.perf_counter()
    store.save(big, layout)
    saved = time.perf_counter() - started

    started = time.perf_counter()
    loaded = store.load(layout.project_file)
    opened = time.perf_counter() - started

    started = time.perf_counter()
    report = validate_for_render(loaded.project, layout.root)
    validated = time.perf_counter() - started

    size_kb = layout.project_file.stat().st_size / 1024
    record(
        10,
        "large project stays responsive",
        loaded.ok and len(loaded.project.scenes) == 300 and saved < 2 and opened < 2,
        f"300 scenes, {size_kb:.0f} kB - save {saved*1000:.0f} ms, open {opened*1000:.0f} ms, "
        f"validate {validated*1000:.0f} ms ({len(report.errors)} error(s))",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Stage B manual verification matrix.")
    parser.add_argument("--data-root", required=True, help="Throw-away data folder for the scenarios.")
    args = parser.parse_args()

    root = Path(args.data_root).resolve()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)

    # The matrix runs headless, so the decision dialogs are answered here
    # instead of blocking on exec().  Each scenario says which choice it used.
    from app.ui import project_controller as pc

    pc.ask_unsaved_changes = lambda *a, **k: pc.UnsavedChoice.SAVE
    pc.ask_confirm = lambda *a, **k: True
    pc.ask_recovery = lambda *a, **k: pc.RecoveryChoice.IGNORE
    pc.ask_external_change = lambda *a, **k: pc.ConflictChoice.RELOAD

    app = QApplication.instance() or QApplication(sys.argv[:1])
    scenarios = (
        scenario_1,
        scenario_2,
        scenario_3,
        scenario_4,
        scenario_5,
        scenario_6,
        scenario_7,
        scenario_8,
        scenario_9,
        scenario_10,
    )
    print(f"Stage B manual matrix - data root {root}\n")
    failures = 0
    for scenario in scenarios:
        try:
            scenario(root)
        except Exception as exc:  # noqa: BLE001 - the matrix must report, not abort
            failures += 1
            record(scenarios.index(scenario) + 1, scenario.__doc__ or scenario.__name__, False, f"raised {exc!r}")
        app.processEvents()

    print()
    passed = sum(1 for _n, status, _d in RESULTS if status == "PASS")
    print(f"{passed}/{len(RESULTS)} scenarios passed")
    return 0 if failures == 0 and passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
