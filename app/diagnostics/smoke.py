"""Built-in end-to-end smoke test (directive sections 57, 58, 68).

The smoke test exercises the parts of the running system that must never
break, in the order a user touches them, and reports one line per step:

1. folders exist and are writable
2. settings survive a save/reload round trip (and a damaged file is recovered)
3. files are written atomically and backups are kept
4. output naming never overwrites an existing video
5. FFmpeg and FFprobe are discovered
6. a real one-second video is encoded and validated with FFprobe
7. the system check runs and produces a report
8. caches can be cleared safely
9. a background job runs exactly once and reports exactly one result
10. a cancelled job reports ``CANCELLED`` (never "stuck")

It runs from the GUI ("Run smoke test" in Tools), from the CLI
(``motion-studio smoke-test``) and from the test suite.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..core import atomicio, logging_setup, maintenance
from ..core.events import Event
from ..core.paths import unique_path
from ..core.settings import Settings, SettingsStore
from ..jobs.progress import ProgressReporter
from ..jobs.spec import JobSpec, execute_job
from ..jobs.states import JobState


@dataclass
class SmokeStep:
    """One checked step of the smoke test."""

    name: str
    ok: bool
    detail: str = ""
    skipped: bool = False
    seconds: float = 0.0

    @property
    def glyph(self) -> str:
        if self.skipped:
            return "-"
        return "OK  " if self.ok else "FAIL"


@dataclass
class SmokeResult:
    """Outcome of a full smoke test run."""

    steps: list[SmokeStep] = field(default_factory=list)
    duration_seconds: float = 0.0
    artifacts: list[Path] = field(default_factory=list)

    @property
    def failures(self) -> list[SmokeStep]:
        return [step for step in self.steps if not step.ok and not step.skipped]

    @property
    def passed(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        total = len([step for step in self.steps if not step.skipped])
        if self.passed:
            return f"Smoke test PASSED ({total} checks in {self.duration_seconds:.1f}s)."
        names = ", ".join(step.name for step in self.failures)
        return f"Smoke test FAILED: {len(self.failures)} of {total} checks failed ({names})."

    def to_text(self) -> str:
        lines = [f"{self.summary()}", ""]
        for step in self.steps:
            lines.append(f"[{step.glyph}] {step.name} ({step.seconds:.2f}s)")
            if step.detail:
                for line in step.detail.splitlines():
                    lines.append(f"        {line}")
        if self.artifacts:
            lines.append("")
            lines.append("Files produced:")
            lines.extend(f"  {path}" for path in self.artifacts)
        return "\n".join(lines)


class _Recorder:
    """Collects steps and times them."""

    def __init__(self, verbose: bool = False, progress: Optional[Callable[[int, int, str], None]] = None) -> None:
        self.steps: list[SmokeStep] = []
        self.verbose = verbose
        self.progress = progress
        self.total = 0
        #: Files the steps produced, so the report can point at them.
        self.artifacts: list[Path] = []

    def set_total(self, total: int) -> None:
        self.total = total

    def run(self, name: str, function: Callable[[], tuple[bool, str, list[Path]]]) -> bool:
        index = len(self.steps)
        if self.progress is not None:
            self.progress(index, self.total, name)
        started = time.perf_counter()
        try:
            ok, detail, artifacts = function()
        except Exception as exc:  # noqa: BLE001 - the smoke test reports, never raises
            ok, detail, artifacts = False, f"{type(exc).__name__}: {exc}", []
        elapsed = time.perf_counter() - started
        step = SmokeStep(name=name, ok=ok, detail=detail, seconds=elapsed)
        self.steps.append(step)
        # Only media files are reported as artifacts: the report should point at
        # things a user may want to inspect, not at scratch JSON files.
        media_suffixes = {".mp4", ".mkv", ".mov", ".wav", ".mp3", ".png", ".jpg", ".jpeg"}
        for artifact in artifacts:
            if artifact.suffix.lower() in media_suffixes and artifact not in self.artifacts:
                self.artifacts.append(artifact)
        if self.verbose:
            print(f"[{step.glyph}] {name} ({elapsed:.2f}s)")
            if detail:
                for line in detail.splitlines():
                    print(f"        {line}")
        return ok

    def skip(self, name: str, reason: str) -> None:
        self.steps.append(SmokeStep(name=name, ok=True, detail=reason, skipped=True))
        if self.verbose:
            print(f"[-   ] {name}: {reason}")


def run_smoke_test(
    paths,
    settings: Settings,
    verbose: bool = False,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> SmokeResult:
    """Run every smoke step and return the collected result."""
    started = time.time()
    recorder = _Recorder(verbose=verbose, progress=progress)
    # A dedicated scratch folder so the smoke test cannot disturb real caches.
    # It lives under 'workspace' (not 'temp') because one of the checks clears the
    # temp folder - the produced video must survive long enough to be inspected.
    scratch = Path(paths.workspace_dir) / "smoke"
    # The smoke test must be repeatable: clear yesterday's scratch folder first so
    # results never depend on what a previous run left behind.
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)

    logger = logging_setup.get_logger("smoke")
    logging_setup.log_event(Event.SYSTEM_CHECK_START, "Smoke test started", logger=logger)
    recorder.set_total(10)

    # -- 1. folders --------------------------------------------------------
    def step_folders() -> tuple[bool, str, list[Path]]:
        statuses = paths.directory_statuses()
        broken = [status.name for status in statuses if not (status.exists and status.writable)]
        detail = f"{len(statuses)} folders, {len(statuses) - len(broken)} writable"
        if broken:
            detail += f"\nnot usable: {', '.join(broken)}"
        return (not broken), detail, []

    recorder.run("Application folders", step_folders)

    # -- 2. settings round trip -------------------------------------------
    def step_settings() -> tuple[bool, str, list[Path]]:
        test_file = scratch / "settings.json"
        store = SettingsStore(test_file, scratch / "settings_backups")
        candidate = Settings()
        candidate.general.theme = "light"
        candidate.voice.speed = 1.25
        store.save(candidate)
        reloaded = store.load()
        if not reloaded.settings.general.theme == "light" or abs(reloaded.settings.voice.speed - 1.25) > 1e-6:
            return False, "saved settings did not load back unchanged", [test_file]

        # A damaged file must be recovered, not crash the loader.
        test_file.write_text("{ broken json", encoding="utf-8")
        recovered = store.load()
        if recovered.source != "recovered":
            return False, "a damaged settings file was not recovered", [test_file]
        if recovered.quarantined is None:
            return False, "damaged settings file was not preserved for inspection", [test_file]
        return True, "saved, reloaded, and recovered from a damaged file", [test_file]

    recorder.run("Settings save / load / recovery", step_settings)

    # -- 3. atomic writes + backup ----------------------------------------
    def step_atomic() -> tuple[bool, str, list[Path]]:
        target = scratch / "atomic_test.json"
        atomicio.atomic_write_json(target, {"value": 1})
        atomicio.save_with_backup(target, {"value": 2}, backup_dir=scratch / "backups", keep_backups=5)
        data = json.loads(target.read_text(encoding="utf-8"))
        backups = list((scratch / "backups").glob("*"))
        if data.get("value") != 2:
            return False, "the new value was not written", [target]
        if not backups:
            return False, "no backup was kept before overwriting", [target]
        leftovers = list(scratch.glob(".atomic_test.json.*.tmp"))
        if leftovers:
            return False, f"temporary files were left behind: {leftovers}", [target]
        return True, f"atomic replace worked, {len(backups)} backup kept, no temp files left", [target]

    recorder.run("Atomic writes and backups", step_atomic)

    # -- 4. output naming --------------------------------------------------
    def step_naming() -> tuple[bool, str, list[Path]]:
        folder = scratch / "output_naming"
        folder.mkdir(parents=True, exist_ok=True)
        first = unique_path(folder, "Video", ".mp4")
        first.write_bytes(b"x")
        second = unique_path(folder, "Video", ".mp4")
        if first.name != "Video.mp4" or second.name != "Video2.mp4":
            return False, f"unexpected names: {first.name}, {second.name}", []
        if not first.exists():
            return False, "the existing file was overwritten", [first]
        return True, f"{first.name} -> {second.name} (existing file untouched)", []

    recorder.run("New videos never overwrite old ones", step_naming)

    # -- 5. FFmpeg discovery ----------------------------------------------
    discovery = None

    def step_ffmpeg() -> tuple[bool, str, list[Path]]:
        nonlocal discovery
        from ..checks import items as check_items

        context = check_items.build_context(paths, settings, deep=False)
        discovery = context.ffmpeg_discovery
        if discovery is None:
            return False, "no discovery result", []
        if not discovery.is_complete:
            return False, discovery.summary() + "\n" + "\n".join(discovery.errors[:3]), []
        return True, f"{discovery.ffmpeg.version} at {discovery.ffmpeg.path}", []

    ffmpeg_ok = recorder.run("FFmpeg and FFprobe discovery", step_ffmpeg)

    # -- 6. real encode + validate ----------------------------------------
    encoded = scratch / "smoke_video.mp4"

    def step_encode() -> tuple[bool, str, list[Path]]:
        from ..tools import ffmpeg as ffmpeg_tools

        tools = ffmpeg_tools.FFmpegTools(discovery)
        tools.ensure_available()
        if encoded.exists():
            encoded.unlink()
        result = tools.run(
            [
                "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
                "-y", str(encoded),
            ],
            timeout=120,
        )
        if not result.ok or not encoded.exists() or encoded.stat().st_size == 0:
            return False, f"FFmpeg failed:\n{result.describe_failure()}", []

        probe, error = tools.probe_json(["-hide_banner", "-show_format", "-show_streams", str(encoded)])
        if probe is None:
            return False, f"FFprobe could not read the file: {error}", [encoded]
        streams = probe.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        duration = float(probe.get("format", {}).get("duration", 0) or 0)
        if video is None:
            return False, "the encoded file has no video stream", [encoded]
        if not (0.5 <= duration <= 2.5):
            return False, f"unexpected duration {duration:.2f}s", [encoded]
        if video.get("width") != 640 or video.get("height") != 360:
            return False, f"unexpected resolution {video.get('width')}x{video.get('height')}", [encoded]
        return (
            True,
            f"{encoded.stat().st_size} bytes, {video.get('width')}x{video.get('height')}, "
            f"{duration:.2f}s, codec {video.get('codec_name')}",
            [encoded],
        )

    if ffmpeg_ok:
        recorder.run("Encode and validate a real video", step_encode)
    else:
        recorder.skip("Encode and validate a real video", "FFmpeg is not available")

    # -- 7. system check ---------------------------------------------------
    def step_check() -> tuple[bool, str, list[Path]]:
        from ..checks import items as check_items
        from ..checks.status import run_system_check

        context = check_items.build_context(paths, settings, deep=False)
        report = run_system_check(context, order=check_items.DISPLAY_ORDER)
        if not report.results:
            return False, "the system check produced no results", []
        return report.core_ready, report.headline(), []

    recorder.run("System check", step_check)

    # -- 8. cleanup --------------------------------------------------------
    def step_cleanup() -> tuple[bool, str, list[Path]]:
        canary = Path(paths.assets_dir) / "smoke_canary.txt"
        canary.parent.mkdir(parents=True, exist_ok=True)
        canary.write_text("this must survive a cache cleanup", encoding="utf-8")
        (Path(paths.temp_dir) / "smoke_should_be_removed.tmp").write_text("x", encoding="utf-8")

        report = maintenance.run_cleanup(paths, ["temp_files"], progress=ProgressReporter())
        survived = canary.exists()
        canary.unlink(missing_ok=True)
        if not survived:
            return False, "a file in the assets folder was deleted - this must never happen", []
        return True, report.summary(), []

    recorder.run("Cache cleanup is safe", step_cleanup)

    # -- 9. one job = one result ------------------------------------------
    def step_single_job() -> tuple[bool, str, list[Path]]:
        calls: list[int] = []

        def body(context):
            calls.append(1)
            context.progress.start(total=3, message="working", unit="steps")
            for index in range(3):
                context.progress.update(current=index + 1)
            return "done"

        spec = JobSpec(key="smoke.one_job", title="Smoke: single job", body=body)
        job = _make_job(spec)
        result = execute_job(job)
        if len(calls) != 1:
            return False, f"the job body ran {len(calls)} times (must run exactly once)", []
        if not result.succeeded:
            return False, "the job did not succeed", []
        if result.value != "done":
            return False, "the job returned the wrong value", []
        return True, "the job ran once and produced exactly one result", []

    recorder.run("One action = one job", step_single_job)

    # -- 10. cancellation --------------------------------------------------
    def step_cancel() -> tuple[bool, str, list[Path]]:

        def body(context):
            for _ in range(200):
                context.raise_if_cancelled()
                time.sleep(0.001)
            return "should not finish"

        spec = JobSpec(key="smoke.cancel", title="Smoke: cancel", body=body)
        job = _make_job(spec)
        job.cancel.cancel("cancelled by the smoke test")

        # A cancelled-before-start job must still reach a terminal state.
        result = execute_job(job)
        if result.state is not JobState.CANCELLED:
            return False, f"expected CANCELLED, got {result.state.value}", []

        # A job cancelled while running must also stop.
        def body2(context):
            for _ in range(1000):
                context.raise_if_cancelled()
                time.sleep(0.001)
            return "should not finish"

        spec2 = JobSpec(key="smoke.cancel2", title="Smoke: cancel while running", body=body2)
        job2 = _make_job(spec2)

        import threading

        def cancel_soon() -> None:
            time.sleep(0.05)
            job2.cancel.cancel("cancelled while running")

        thread = threading.Thread(target=cancel_soon, daemon=True)
        thread.start()
        result2 = execute_job(job2)
        thread.join(timeout=1.0)
        if result2.state is not JobState.CANCELLED:
            return False, f"expected CANCELLED while running, got {result2.state.value}", []
        if result2.error is None or result2.error.error_code != "CANCELLED":
            return False, "a cancelled job did not report a cancellation message", []
        return True, "cancellation stops the job and reports CANCELLED", []

    recorder.run("Cancellation reaches a terminal state", step_cancel)

    # -- finish ------------------------------------------------------------
    artifacts = [path for path in recorder.artifacts if path.exists()]
    result = SmokeResult(steps=recorder.steps, duration_seconds=time.time() - started, artifacts=artifacts)
    if progress is not None:
        progress(len(recorder.steps), max(recorder.total, len(recorder.steps)), "Finished")

    logging_setup.log_event(
        Event.SYSTEM_CHECK_COMPLETE,
        result.summary(),
        logger=logger,
        passed=result.passed,
        seconds=round(result.duration_seconds, 2),
    )
    return result


def _make_job(spec: JobSpec):
    from ..jobs.spec import Job

    return Job(spec)


def cleanup_smoke_files(paths) -> int:
    """Remove smoke-test scratch data (called by the GUI after the run)."""
    scratch = Path(paths.workspace_dir) / "smoke"
    if not scratch.exists():
        return 0
    try:
        shutil.rmtree(scratch)
        return 1
    except OSError:
        return 0


def make_smoke_test_spec(paths, settings, verbose: bool = False) -> JobSpec:
    """Job spec for the GUI: runs the smoke test off the UI thread."""
    from ..jobs.keys import JobKeys

    def body(context) -> SmokeResult:
        result = run_smoke_test(
            context.paths,
            context.settings,
            verbose=verbose,
            progress=lambda done, total, name: context.progress.update(current=done, message=name),
        )
        cleanup_smoke_files(context.paths)
        context.progress.finish(result.summary())
        return result

    return JobSpec(
        key=JobKeys.SMOKE_TEST,
        title="Smoke test",
        description="Checks folders, settings, FFmpeg, encoding, jobs and cancellation end to end.",
        body=body,
        cancellable=True,
        settings=settings,
        paths=paths,
    )


__all__ = [
    "SmokeResult",
    "SmokeStep",
    "cleanup_smoke_files",
    "make_smoke_test_spec",
    "run_smoke_test",
]
