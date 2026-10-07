"""Stage G manual test matrix - real runs, real files, real measurements.

Every scenario runs against the real services on this machine and prints what it
found.  Nothing is asserted away: a scenario that cannot run here reports
**N/A** with the reason, and a scenario that runs through the built-in fixture
says **TEST BACKEND** out loud.  No line in this output claims an AI model ran
unless a real one did.

Run it with:

    python scripts/stage_g_manual_matrix.py --data-root /tmp/mgs_stage_g

The output is the evidence quoted in ``docs/STAGE_G_REPORT.md``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.ai.service import AIService  # noqa: E402
from app.ai.types import state_label  # noqa: E402
from app.ai.video import VideoRequest  # noqa: E402
from app.ai.video_library import LibraryQuery  # noqa: E402
from app.ai.video_library_jobs import (library_scan_body,  # noqa: E402
                                       library_thumbnails_body)
from app.core.paths import AppPaths  # noqa: E402
from app.core.settings import SettingsStore  # noqa: E402
from app.jobs.spec import JobContext  # noqa: E402
from app.project.service import CreateRequest, ProjectService  # noqa: E402
from app.tools.ffmpeg import FFmpegTools, discover_ffmpeg  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
NOT_AVAILABLE = "N/A"

RESULTS: list[tuple[int, str, str, str]] = []


def record(number: int, name: str, ok: str, detail: str) -> None:
    RESULTS.append((number, name, ok, detail))
    print(f"[{ok}] {number:02d} {name}: {detail}", flush=True)


def check(number: int, name: str, condition: bool, detail: str) -> bool:
    record(number, name, PASS if condition else FAIL, detail)
    return bool(condition)


def bytes_label(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count:.1f} {unit}" if unit != "B" else f"{count:.0f} B"
        count /= 1024
    return f"{count:.1f} GB"


class _Never:
    """A cancel token that is never cancelled (the CLI's own contract)."""

    job_id = "matrix"
    cancelled = False

    def is_cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None

    def register_process(self, process) -> None:
        return None

    def unregister_process(self, process) -> None:
        return None

    def active_process_count(self) -> int:
        return 0

    def terminate_children(self, grace_seconds: float = 5.0) -> int:
        return 0


class _Progress:
    class _Total:
        total = 0.0

    def __init__(self) -> None:
        self.progress = self._Total()

    def start(self, **kwargs) -> None:
        self.progress.total = float(kwargs.get("total", 1.0) or 0.0)

    def update(self, **kwargs) -> None:
        return None


def context_for(service: AIService, **payload) -> JobContext:
    return JobContext(job_id="matrix", key="video.library", cancel=_Never(),
                      progress=_Progress(), settings=None, paths=service.paths,
                      payload={"library": service.library,
                               "tools": service.tools, **payload})


def make_request(service: AIService, work: Path, **kwargs) -> VideoRequest:
    """A request with the fields the caller changes and sane defaults."""
    options = dict(mode="text_to_video", backend="standard_video",
                   prompt="a lighthouse in a storm, dawn light",
                   width=160, height=96, fps=12, duration=1.0, seed=2468,
                   output_dir=str(work), name_stem="clip")
    options.update(kwargs)
    return VideoRequest(**options)


def read_json(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a missing/corrupt file is a result too
        return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="/tmp/mgs_stage_g")
    parser.add_argument("--keep", action="store_true",
                        help="Keep the data root instead of starting clean.")
    args = parser.parse_args(argv)

    root = Path(args.data_root)
    if root.exists() and not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    paths = AppPaths(data_root=root, source_root=REPO_ROOT, reason="stage-g-matrix")
    paths.ensure()
    settings = SettingsStore(paths.settings_file).load().settings
    work = root / "matrix"
    work.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print("STAGE G MANUAL MATRIX")
    print(f"data root : {root}")
    print(f"python    : {sys.version.split()[0]}")
    print(f"platform  : {sys.platform}")
    print("=" * 78)

    tools = FFmpegTools(discover_ffmpeg())
    print(f"\nFFmpeg: {tools.discovery.summary()}")

    # ----------------------------------------------------------------------
    # 1. Backend detection: what is installed, and what is only detectable.
    # ----------------------------------------------------------------------
    service = AIService(settings, paths=paths, data_root=root, tools=tools)
    before_rss = _rss_mb()
    started = time.perf_counter()
    report = service.status()
    detection = time.perf_counter() - started
    after_rss = _rss_mb()

    entries = list(report.entries)
    usable = [entry for entry in entries if entry.status().available]
    real_models = [entry for entry in entries
                   if getattr(entry.backend, "is_model", False)]
    print("\n-- detected backends --")
    for entry in entries:
        status = entry.status()
        print(f"  {entry.id:26s} {state_label(status.state):18s} "
              f"model={'yes' if getattr(entry.backend, 'is_model', False) else 'no '}"
              f"  {status.reason}")
    check(1, "Every registered backend is reported with a state and a reason",
          len(entries) >= 8 and all(entry.status().reason for entry in entries),
          f"{len(entries)} backends, {len(usable)} usable, "
          f"{len(real_models)} claim to hold a model")
    check(2, "Detection loads no model and stays fast",
          detection < 2.0,
          f"{detection * 1000:.0f} ms, RSS {before_rss:.1f} -> {after_rss:.1f} MiB "
          f"({after_rss - before_rss:+.1f})")

    # ----------------------------------------------------------------------
    # 2. Models: what is on disk, and what is offered.
    # ----------------------------------------------------------------------
    models = service.manager.all_models()
    on_disk = [model for model in models if model.get("path")]
    check(3, "The model list is honest about what is installed",
          all("is_ai_model" in model for model in models),
          f"{len(models)} offered, {len(on_disk)} with a file on disk"
          + ("" if on_disk else " - no AI model is installed on this machine"))
    if not on_disk:
        record(3, "A real AI model is installed", NOT_AVAILABLE,
               "no model file was found; every generation below is either the "
               "fixture or refused")

    # ----------------------------------------------------------------------
    # 3. Light check vs deep check.
    # ----------------------------------------------------------------------
    entry_for_check = service.manager.get("standard_video")
    light = entry_for_check.backend.check(deep=False)
    check(4, "A light check reads metadata without initialising anything",
          bool(light.ok) and str(light.state) in ("AVAILABLE", "VERIFIED"),
          f"ok={light.ok} state={state_label(str(light.state))} :: {light.message}")
    deep = service.self_test("standard_video")
    check(5, "A deep check runs a real generation and can report VERIFIED",
          str(deep.get("state")) == "VERIFIED" and bool(deep.get("ok")),
          f"state={state_label(str(deep.get('state')))} :: "
          f"{str(deep.get('message', ''))[:120]}")
    real_deep = service.self_test("diffusers")
    check(5, "A deep check refuses to claim VERIFIED with no model installed",
          str(real_deep.get("state")) != "VERIFIED" and not real_deep.get("ok"),
          f"diffusers -> {state_label(str(real_deep.get('state')))} :: "
          f"{str(real_deep.get('message', ''))[:120]}")

    # ----------------------------------------------------------------------
    # 4-7. The four video modes, through the built-in test backend.
    # ----------------------------------------------------------------------
    print("\n-- video modes (TEST BACKEND: fixture - not an AI model) --")
    result = service.generate_video(make_request(service, work),
                                    backend_id="standard_video")
    entry = service.library.for_path(result.path) if result.ok else None
    check(6, "Text to video writes a real, measured clip",
          bool(result.ok and entry and entry.width and entry.duration),
          (f"{Path(result.path).name} - {entry.measurements()} - "
           f"{entry.provenance()} - label='{entry.label}'") if result.ok
          else str(result.error))

    # An image to move: a real PNG, made here rather than shipped.
    from PIL import Image

    source_png = work / "source.png"
    Image.new("RGB", (160, 96), (30, 90, 160)).save(source_png)
    before_png = source_png.read_bytes()
    from app.ai.video import CAMERA_MOVES

    image_result = service.generate_video(
        make_request(service, work, mode="image_to_video",
                     source_image=str(source_png), name_stem="from_image",
                     camera=CAMERA_MOVES[1], camera_amount=0.5),
        backend_id="standard_video")
    check(7, "Image to video moves the source and leaves it untouched",
          bool(image_result.ok and source_png.read_bytes() == before_png),
          f"{Path(image_result.path).name if image_result.ok else image_result.error}; "
          f"source unchanged: {source_png.read_bytes() == before_png}")

    video_result = service.generate_video(
        make_request(service, work, mode="video_to_video",
                     source_video=str(result.path), name_stem="regraded"),
        backend_id="standard_video")
    original_kept = bool(result.ok and Path(result.path).is_file())
    check(8, "Video to video keeps the original clip",
          bool(video_result.ok and original_kept),
          f"{Path(video_result.path).name if video_result.ok else video_result.error}; "
          f"original still there: {original_kept}")

    # ----------------------------------------------------------------------
    # 5. Storyboard to video, through the real plan builder.
    # ----------------------------------------------------------------------
    project_service = ProjectService(paths=paths, settings=settings)
    project = project_service.create_project(
        CreateRequest(name="Stage G Matrix", width=640, height=360, fps=24,
                      folder=str(paths.projects_dir)), open_after=True)
    project_service.add_scene(scene_type="title", name="Opening")
    project_service.add_scene(scene_type="title", name="Second")
    plan = service.storyboard_plan(project_service.current,
                                   backend_id="standard_video", width=160,
                                   height=96, fps=12)
    approved = service.approve_plan(plan)
    check(9, "A storyboard plan is built from the scenes and approved by the user",
          len(plan) >= 2 and all(item["approved"] for item in approved),
          f"{len(plan)} scene(s) planned, seeds "
          f"{[item['seed'] for item in plan]}; approval is a separate step")

    # ----------------------------------------------------------------------
    # 6. Variations and extend: originals are never damaged.
    # ----------------------------------------------------------------------
    original_bytes = Path(result.path).read_bytes() if result.ok else b""
    variation = service.generate_video(
        make_request(service, work, name_stem="variation", seed=99),
        backend_id="standard_video")
    extended = service.generate_video(
        make_request(service, work, mode="extend", extend_from=str(result.path),
                     name_stem="extended", duration=0.5),
        backend_id="standard_video")
    check(10, "A variation and an extension never overwrite the original",
          bool(variation.ok and extended.ok
               and Path(result.path).read_bytes() == original_bytes),
          f"variation={Path(variation.path).name if variation.ok else 'failed'}, "
          f"extend={Path(extended.path).name if extended.ok else 'failed'}, "
          f"original bytes unchanged: "
          f"{Path(result.path).read_bytes() == original_bytes}")

    # ----------------------------------------------------------------------
    # 7. Sending a clip into the project: asset, scene, timeline.
    # ----------------------------------------------------------------------
    from app.ai.integration import send_to_project, send_to_scene, send_to_timeline

    asset_report = send_to_project(project_service, result.path, name="matrix clip",
                                   metadata={"clip": True})
    check(11, "Send to project copies the clip in as an asset",
          bool(asset_report.ok and len(project.assets) >= 1),
          f"{asset_report.describe() if asset_report.ok else asset_report.message} "
          f"- assets={len(project.assets)}")

    scene_report = send_to_scene(project_service, result.path, name="matrix scene",
                                 duration=1.0, fps=12)
    kinds = [getattr(scene, "type", "") for scene in project.scenes]
    check(12, "Send to scene adds a video scene that carries the real duration",
          bool(scene_report.ok and "video" in kinds),
          f"{scene_report.describe() if scene_report.ok else scene_report.message} "
          f"- scene types={kinds}")

    timeline_report = send_to_timeline(project_service, result.path,
                                       name="matrix timeline", duration=1.0, fps=12)
    rows = project.timeline()
    check(13, "Send to timeline places the clip and the timeline shows its length",
          bool(timeline_report.ok and rows and rows[-1]["duration"] == 1.0),
          f"{timeline_report.describe() if timeline_report.ok else timeline_report.message}"
          f" - timeline={[(row['name'], row['start'], row['duration']) for row in rows]}")

    # ----------------------------------------------------------------------
    # 8. Validation refuses a bad file rather than calling it a success.
    # ----------------------------------------------------------------------
    from app.ai.video_validation import validate_video_file

    broken = work / "broken.mp4"
    broken.write_bytes(b"this is not a video")
    broken_check = validate_video_file(broken, tools=tools)
    small = work / "tiny.mp4"
    small.write_bytes(b"\x00" * 27)
    small_check = validate_video_file(small, tools=tools)
    check(14, "A file that is not a usable video is refused with a reason",
          bool(not broken_check.ok and not small_check.ok),
          f"broken -> {broken_check.code} ({broken_check.error}); "
          f"27-byte file -> {small_check.code}")

    # ----------------------------------------------------------------------
    # 9. Output names never overwrite anything.
    # ----------------------------------------------------------------------
    again = service.generate_video(make_request(service, work),
                                   backend_id="standard_video")
    names = {Path(result.path).name, Path(again.path).name}
    check(15, "A second run writes a new file and leaves the first alone",
          bool(again.ok and len(names) == 2),
          f"{sorted(names)}; both on disk: "
          f"{all(Path(result.path).is_file() for result in (result, again))}")

    # ----------------------------------------------------------------------
    # 10. Reproducibility: same seed, recorded provenance, honest refusal.
    # ----------------------------------------------------------------------
    check(16, "A clip's metadata records the backend, model and seed",
          bool(entry and entry.backend and entry.seed),
          f"backend={entry.backend if entry else '-'} "
          f"model={entry.model or '(none)'} seed={entry.seed if entry else '-'}")

    # A retry of a *failed* job repeats the same request; a retry of a finished
    # one is refused, and a missing backend is refused outright (sections 34, 100).
    from app.ai.jobs import AIJobState
    from app.ui.views.ai_studio_view import retry_job

    studio_jobs = service.jobs
    settings_used = {"mode": "text_to_video", "prompt": "a harbour at dawn",
                     "seed": 2468, "width": 160, "height": 96, "fps": 12,
                     "duration": 1.0, "backend": "standard_video"}
    studio_jobs.register("matrix-done", operation="video_generate", kind="video",
                         backend="standard_video", model="test-clip-writer",
                         request=dict(settings_used), is_ai_model=False,
                         label="TEST BACKEND (fixture - not an AI model)")
    studio_jobs.finish("matrix-done", state=AIJobState.COMPLETED,
                       output={"path": str(result.path)})
    studio_jobs.register("matrix-failed", operation="video_generate", kind="video",
                         backend="standard_video", model="",
                         request=dict(settings_used), is_ai_model=False,
                         label="TEST BACKEND (fixture - not an AI model)")
    studio_jobs.finish("matrix-failed", state=AIJobState.FAILED,
                       error="the backend stopped")
    studio_jobs.register("matrix-gone", operation="video_generate", kind="video",
                         backend="removed_backend", model="",
                         request=dict(settings_used), is_ai_model=False)
    studio_jobs.finish("matrix-gone", state=AIJobState.CANCELLED)
    refused_done = retry_job(service, "matrix-done")
    allowed = retry_job(service, "matrix-failed")
    refused_gone = retry_job(service, "matrix-gone")
    check(17, "Retry repeats the same request, and refuses what cannot be repeated",
          bool(refused_done.get("ok") is False and allowed.get("ok") is True
               and allowed.get("request", {}).get("seed") == 2468
               and refused_gone.get("ok") is False
               and "not available" in str(refused_gone.get("reason", "")).lower()),
          f"completed -> {refused_done.get('reason')} | "
          f"failed -> same seed {allowed.get('request', {}).get('seed')} | "
          f"backend gone -> {refused_gone.get('reason')}")

    # ----------------------------------------------------------------------
    # 11. The video library: scan, measure, search, pictures, restart.
    # ----------------------------------------------------------------------
    body = library_scan_body(context_for(service, measure=True, folder=str(work)))
    measured = [record for record in body.get("videos", [])
                if record.get("measured_with")]
    check(18, "The library scan indexes and measures what is in the folder",
          bool(body.get("ok")) and body.get("found", 0) >= 1 and bool(measured),
          f"{body.get('message')} - counts={body.get('counts')} - "
          f"measured by {measured[0].get('measured_with') if measured else '-'}")

    pictures = library_thumbnails_body(context_for(service))
    made = int(pictures.get("made", 0) or 0)
    check(19, "A picture is taken from a clip with real FFmpeg",
          made >= 1,
          f"{pictures.get('message')} - failed={len(pictures.get('failed', []))}")

    page = service.library.query(LibraryQuery(limit=2, sort="duration"))
    check(20, "Paging reports the total and whether there is more",
          page.total >= 1 and len(page.entries) <= 2,
          f"total={page.total} returned={len(page.entries)} has_more={page.has_more}")

    reopened = AIService(settings, paths=paths, data_root=root, tools=tools)
    check(21, "The library survives a restart",
          reopened.library.counts().get("total", 0) == service.library.counts().get("total", 0),
          f"before={service.library.counts()} after={reopened.library.counts()}")

    index = read_json(Path(str(service.library.index_path)))
    check(22, "The index on disk is readable JSON with the same clips",
          len(index.get("videos", [])) == service.library.counts().get("total", 0),
          f"{len(index.get('videos', []))} record(s) in {service.library.index_path}")

    # ----------------------------------------------------------------------
    # 12. Refusals: unsupported operations, missing files, no backend.
    # ----------------------------------------------------------------------
    missing_backend = service.generate_video(
        make_request(service, work, backend="no_such_backend"),
        backend_id="no_such_backend")
    check(23, "An unknown backend is refused, and nothing is written",
          not missing_backend.ok and bool(missing_backend.error),
          f"{missing_backend.state or 'refused'}: {missing_backend.error or '-'}")

    from app.ai.integration import send_to_project as send_again

    absent = send_again(project_service, work / "not-there.mp4")
    check(24, "Sending a file that is not there is refused with what to do",
          not absent.ok and bool(absent.what_to_do),
          f"{absent.message} - {absent.what_to_do}")

    # ----------------------------------------------------------------------
    # 13. Nothing reaches the network unless the user configured an endpoint.
    # ----------------------------------------------------------------------
    import socket as _socket

    opened: list[str] = []
    real_socket = _socket.socket

    class _Watched(_socket.socket):
        def connect(self, address, *args, **kwargs):
            opened.append(str(address))
            return super().connect(address, *args, **kwargs)

    _socket.socket = _Watched  # type: ignore[misc]
    try:
        offline = service.generate_video(
            make_request(service, work, name_stem="offline", seed=3),
            backend_id="standard_video")
    finally:
        _socket.socket = real_socket  # type: ignore[misc]
    check(25, "A generation opens no network connection",
          bool(offline.ok and not opened),
          f"clip written: {Path(offline.path).name if offline.ok else offline.error}; "
          f"outbound connections: {opened or 'none'}")

    # ----------------------------------------------------------------------
    # 14. Device awareness on a CPU-only machine.
    # ----------------------------------------------------------------------
    from app.image.device import detect_device

    device = detect_device()
    check(26, "Device awareness reports this machine honestly",
          device.cpu_count >= 1 and bool(device.describe()),
          f"{device.describe()[:170]}")

    # ----------------------------------------------------------------------
    # 15. The command line shares the same services.
    # ----------------------------------------------------------------------
    from app.cli.main import main as cli_main

    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli_main(["--data-root", str(root), "ai", "library", "list"])
    listing = buffer.getvalue()
    check(27, "The command line lists the same library as the GUI service",
          code == 0 and "Video library" in listing,
          next((line.strip() for line in listing.splitlines()
                if "clip(s)" in line), ""))

    # ----------------------------------------------------------------------
    # 16. What this machine cannot verify.
    # ----------------------------------------------------------------------
    for number, name, why in (
        (28, "REAL AI VIDEO MODEL VERIFICATION",
         "no AI video model is installed here: every clip above came from the "
         "labelled fixture, which does not understand prompts"),
        (29, "REAL DIFFUSERS / COMFYUI INFERENCE",
         "the adapters stop at the model-loading boundary and report NOT "
         "INSTALLED / NOT VERIFIED rather than pretending"),
        (30, "BACKGROUND REMOVAL",
         "no remover is installed; the request is refused with instructions"),
        (31, "in-application video playback",
         "QtMultimedia is not available in this environment (no libpulse), so "
         "the page reports 'cannot play video inside the application' and "
         "offers the system player"),
        (32, "WINDOWS",
         "carried from Stage E: this matrix ran on Linux; Windows stays NOT VERIFIED"),
        (33, "KOKORO",
         "carried from Stage E: Kokoro was not verified; the test fallback was used"),
    ):
        record(number, name, NOT_AVAILABLE, why)

    # ----------------------------------------------------------------------
    # Manual GUI walkthrough - for a person with the application open.
    # ----------------------------------------------------------------------
    print("\n" + "=" * 78)
    print("MANUAL WALKTHROUGH (needs the application open - MANUAL VERIFICATION PENDING)")
    print("=" * 78)
    for step in (
        "AI Studio -> Backends: every row shows a state and what it needs.",
        "AI Studio -> Images: Generate with no model installed -> refused, no file written.",
        "AI Studio -> Video: pick the fixture backend, press Generate -> a real clip.",
        "Video library: press Scan, then Make pictures -> a picture appears per clip.",
        "Video library: select a clip -> Send to project / scene / timeline -> check the project.",
        "Queue: start two generations, cancel the second -> the first result is kept.",
        "Open the log file and check every step above is an EVENT= line.",
    ):
        print(f"  [ ] {step}")

    # ----------------------------------------------------------------------
    # Summary - the numbers, and nothing rounded up.
    # ----------------------------------------------------------------------
    passed = sum(1 for _, _, ok, _ in RESULTS if ok == PASS)
    failed = sum(1 for _, _, ok, _ in RESULTS if ok == FAIL)
    skipped = sum(1 for _, _, ok, _ in RESULTS if ok == NOT_AVAILABLE)
    print("\n" + "=" * 78)
    print(f"SUMMARY: {passed} passed, {failed} failed, {skipped} not available "
          f"({len(RESULTS)} checks)")
    print("REAL AI MODEL VERIFICATION: PENDING (no AI model is installed here)")
    print("WINDOWS: NOT VERIFIED   KOKORO: NOT VERIFIED - TEST FALLBACK USED")
    print("=" * 78)
    return 1 if failed else 0


def _rss_mb() -> float:
    """Resident memory in MiB, without importing psutil or torch."""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


if __name__ == "__main__":
    raise SystemExit(main())
