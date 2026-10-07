"""Background jobs for the AI Studio (sections 32, 33, 34, 35, 36).

Two things live here.

**The job bodies** are plain functions with the
:class:`app.jobs.spec.JobContext` signature, so the GUI's thread pool, the CLI
and the tests run exactly the same code.  A video generation is one job, a batch
is one job that says it is a batch, and a check is one job: nothing here starts
a job the user did not ask for (sections 9, 33).

**The AI job record** is the studio's own view of a job - id, backend, model,
operation, status, progress, elapsed, ETA, input, output, error - merged with the
job manager's live progress for the queue the interface shows.  The record is
what makes "which backend, which model, how long, and what came out" answerable
after the fact, including from the CLI.

Cancellation is wired all the way down: the job's cancel token is handed to the
backend, which stops its subprocess, its HTTP polling loop or its in-process
model, and a cancelled job keeps whatever it had already produced (section 35).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..core.logging_setup import get_logger
from ..jobs.keys import JobKeys
from ..jobs.spec import JobContext, JobSpec
from .types import (AIOperation, AIJobState, OPERATION_LABELS,
                    TERMINAL_JOB_STATES, VIDEO_OPERATIONS, ProviderState)
from .video import MODE_LABELS, VideoMode, VideoRequest

LOGGER = get_logger("ai.jobs")

__all__ = [
    "AIJob", "AIJobRegistry", "ai_service_for", "video_generate_body",
    "video_batch_body", "image_generate_body", "ai_check_body",
    "ai_detect_body", "video_generate_spec", "video_batch_spec",
    "image_generate_spec", "ai_check_spec", "ai_detect_spec",
    "submit_ai_job", "job_key_for", "finalize_from_result",
]


def ai_service_for(context: JobContext) -> Any:
    """The AI service for this job: the one it was given, or a fresh one."""
    service = context.get("service")
    if service is not None:
        return service
    from .service import AIService

    return AIService.for_paths(context.paths, context.settings)
def _progress_bridge(context: JobContext, *, unit: str = "clip") -> Callable:
    """Turn a backend's ``(state, fraction)`` calls into job progress."""
    def report(state: str, fraction: float) -> None:
        reporter = context.progress
        value = max(0.0, min(1.0, float(fraction)))
        if getattr(reporter.progress, "total", 0) != 1.0:
            reporter.start(total=1.0, message=str(state), unit=unit)
        reporter.update(current=value, message=str(state))
    return report


def _video_request(context: JobContext) -> VideoRequest:
    request = context.get("request")
    if isinstance(request, VideoRequest):
        return request
    if isinstance(request, dict):
        return VideoRequest.from_dict(request)
    raise ValueError("The clip job was not given a request.")


# --------------------------------------------------------------------------
# The job record
# --------------------------------------------------------------------------

@dataclass
class AIJob:
    """One AI job as the studio sees it (section 32)."""

    id: str = ""
    operation: str = ""
    kind: str = "video"
    backend: str = ""
    backend_name: str = ""
    model: str = ""
    mode: str = ""
    status: str = AIJobState.QUEUED
    progress: float = 0.0
    message: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    #: The request, exactly as it was submitted, so Retry means the same thing.
    request: dict = field(default_factory=dict)
    output: dict = field(default_factory=dict)
    error: str = ""
    why: str = ""
    what_to_do: str = ""
    code: str = ""
    cancelled: bool = False
    #: Set when this job is a retry of another, so the chain is visible.
    retry_of: str = ""
    #: The paths a batch produced, and how many were requested.
    batch_size: int = 1
    completed_items: int = 0
    #: The backend's own label, when it is a test backend rather than a model.
    label: str = ""
    is_ai_model: bool = True
    log_hint: str = ""

    # -- display -----------------------------------------------------------

    def title(self) -> str:
        what = OPERATION_LABELS.get(self.operation, self.operation or "AI job")
        if self.mode and self.mode in MODE_LABELS:
            what = MODE_LABELS[self.mode]
        who = self.backend_name or self.backend or "no backend"
        return f"{what} - {who}"

    @property
    def finished(self) -> bool:
        return self.status in TERMINAL_JOB_STATES

    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at)

    def elapsed_label(self) -> str:
        seconds = self.elapsed()
        if seconds < 60:
            return f"{seconds:.1f}s"
        minutes, rest = divmod(int(seconds), 60)
        return f"{minutes}m {rest:02d}s"

    def eta(self) -> Optional[float]:
        """Seconds still to go, from the progress actually reported."""
        if self.finished or self.progress <= 0.02 or not self.started_at:
            return None
        elapsed = self.elapsed()
        remaining = elapsed * (1.0 - self.progress) / max(0.02, self.progress)
        return max(0.0, remaining)

    def eta_label(self) -> str:
        eta = self.eta()
        if eta is None:
            return "-"
        if eta < 60:
            return f"~{eta:.0f}s"
        return f"~{eta / 60:.0f}m"

    def progress_label(self) -> str:
        return f"{int(round(self.progress * 100))}%"

    def verdict(self) -> str:
        if self.status == AIJobState.COMPLETED:
            return "Completed"
        if self.status == AIJobState.CANCELLED:
            return "Cancelled"
        if self.status == AIJobState.FAILED:
            return self.error or "Failed"
        return self.status.title()

    def to_dict(self) -> dict:
        data = dict(self.__dict__)
        data.update({"title": self.title(), "finished": self.finished,
                     "elapsed": round(self.elapsed(), 3),
                     "elapsed_label": self.elapsed_label(),
                     "eta": self.eta(), "eta_label": self.eta_label(),
                     "progress_label": self.progress_label(),
                     "verdict": self.verdict()})
        return data


class AIJobRegistry:
    """The studio's list of AI jobs, live and finished.

    It is deliberately *not* a second job queue: the Qt job manager still owns
    execution and cancellation.  This records the studio's view of each job so
    the queue can show the backend, the model and the result without digging
    through job payloads.
    """

    def __init__(self, *, limit: int = 200) -> None:
        self.limit = max(20, int(limit))
        self._jobs: dict[str, AIJob] = {}

    def register(self, job_id: str, *, operation: str, kind: str = "video",
                 backend: str = "", backend_name: str = "", model: str = "",
                 mode: str = "", request: Optional[dict] = None,
                 batch_size: int = 1, retry_of: str = "",
                 is_ai_model: bool = True, label: str = "") -> AIJob:
        record = AIJob(id=str(job_id), operation=str(operation), kind=str(kind),
                       backend=str(backend), backend_name=str(backend_name),
                       model=str(model), mode=str(mode),
                       request=dict(request or {}), batch_size=int(batch_size or 1),
                       retry_of=str(retry_of), is_ai_model=bool(is_ai_model),
                       label=str(label), started_at=time.time())
        self._jobs[record.id] = record
        self._trim()
        return record

    def update(self, job_id: str, **fields: Any) -> Optional[AIJob]:
        record = self._jobs.get(str(job_id))
        if record is None:
            return None
        for name, value in fields.items():
            if hasattr(record, name):
                setattr(record, name, value)
        return record

    def finish(self, job_id: str, *, state: str, output: Optional[dict] = None,
               error: str = "", why: str = "", what_to_do: str = "",
               code: str = "", completed_items: int = 0) -> Optional[AIJob]:
        record = self._jobs.get(str(job_id))
        if record is None:
            return None
        record.status = str(state)
        record.finished_at = time.time()
        record.error = str(error or "")
        record.why = str(why or "")
        record.what_to_do = str(what_to_do or "")
        record.code = str(code or "")
        record.cancelled = str(state) == AIJobState.CANCELLED
        if output is not None:
            record.output = dict(output)
        if completed_items:
            record.completed_items = int(completed_items)
        if str(state) == AIJobState.COMPLETED:
            record.progress = 1.0
        return record

    def get(self, job_id: str) -> Optional[AIJob]:
        return self._jobs.get(str(job_id))

    def all(self) -> list[AIJob]:
        return sorted(self._jobs.values(), key=lambda item: item.started_at,
                      reverse=True)

    def active(self) -> list[AIJob]:
        return [record for record in self.all() if not record.finished]

    def duplicate_of(self, *, operation: str, backend: str, model: str,
                     mode: str = "") -> Optional[AIJob]:
        """An active job doing the same thing, if there is one.

        This is the duplicate-submission guard: the same operation, backend,
        model and mode already running means the second request is refused with
        the running job named, rather than starting a twin (sections 9, 33).
        """
        for record in self.active():
            if record.operation != str(operation):
                continue
            if record.backend != str(backend):
                continue
            if record.model and model and record.model != str(model):
                continue
            if mode and record.mode and record.mode != str(mode):
                continue
            return record
        return None

    def clear_finished(self) -> int:
        finished = [record.id for record in self.all() if record.finished]
        for job_id in finished:
            self._jobs.pop(job_id, None)
        return len(finished)

    def describe(self) -> str:
        active = self.active()
        if not active:
            return "No AI jobs running."
        lines = []
        for record in active:
            lines.append(f"- [{record.id[:8]}] {record.title()}: "
                         f"{record.progress_label()} "
                         f"({record.elapsed_label()}, {record.status})")
        return "\n".join(lines)

    def _trim(self) -> None:
        if len(self._jobs) <= self.limit:
            return
        finished = [record for record in self.all() if record.finished]
        for record in finished[self.limit // 2:]:
            self._jobs.pop(record.id, None)


# --------------------------------------------------------------------------
# Submission
# --------------------------------------------------------------------------

def job_key_for(request: VideoRequest, *, batch: int = 0) -> str:
    """Which key a clip job runs under.  Batches are explicit (section 33)."""
    count = int(batch or getattr(request, "batch", 1) or 1)
    if count > 1:
        return JobKeys.AI_VIDEO_BATCH
    if str(getattr(request, "mode", "")) == VideoMode.EXTEND:
        return JobKeys.AI_VIDEO_EXTEND
    return JobKeys.AI_VIDEO_GENERATE


def submit_ai_job(job_manager: Any, spec: JobSpec, *, registry: AIJobRegistry,
                  operation: str, kind: str = "video", backend: str = "",
                  backend_name: str = "", model: str = "", mode: str = "",
                  guard_duplicates: bool = True,
                  is_ai_model: bool = True, label: str = "",
                  retry_of: str = "") -> dict:
    """Submit one AI job, refusing a duplicate of something already running.

    Returns ``{"ok": True, "job": <job>, "record": <AIJob>}`` or
    ``{"ok": False, "reason": ..., "running": <AIJob>}``.  The caller shows the
    reason; nothing is started twice (sections 9, 33).
    """
    if guard_duplicates:
        running = registry.duplicate_of(operation=operation, backend=backend,
                                       model=model, mode=mode)
        if running is not None:
            return {
                "ok": False,
                "reason": (f"That generation is already running as job "
                           f"{running.id[:8]} ({running.progress_label()}). "
                           f"Wait for it, or cancel it first."),
                "running": running,
            }
    job = job_manager.submit(spec)
    if job is None:
        return {"ok": False,
                "reason": ("A job with the same key is already running, so this "
                           "one was not started."),
                "running": None}
    record = registry.register(
        job.id, operation=operation, kind=kind, backend=backend,
        backend_name=backend_name, model=model, mode=mode,
        request=dict(spec.payload.get("request_dict") or
                     getattr(spec.payload.get("request"), "to_dict",
                             lambda: {})()),
        batch_size=int(getattr(spec.payload.get("request"), "batch", 1) or 1),
        retry_of=str(retry_of or ""),
        is_ai_model=is_ai_model, label=label)
    return {"ok": True, "job": job, "record": record}


# --------------------------------------------------------------------------
# Bodies
# --------------------------------------------------------------------------

def video_generate_body(context: JobContext) -> dict:
    """Generate one clip.  One user action, one job, one file (section 33)."""
    context.raise_if_cancelled()
    service = ai_service_for(context)
    request = _video_request(context)
    backend_id = str(context.get("backend_id") or request.backend or "")
    registry = context.get("registry")
    if registry is not None:
        registry.update(context.job_id, status=AIJobState.INITIALIZING,
                        message="Loading the backend")

    result = service.generate_video(
        request, backend_id=backend_id,
        progress=_progress_bridge(context), cancel=context.cancel)

    # The record is brought up to date *before* a cancellation is propagated:
    # a job that stops must not be left looking like it is still running
    # (sections 32, 34).
    if registry is not None:
        if result.cancelled:
            registry.finish(context.job_id, state=AIJobState.CANCELLED,
                            error="Clip generation was cancelled.")
        elif result.ok:
            registry.finish(context.job_id, state=AIJobState.COMPLETED,
                            output={"path": str(result.path or ""),
                                    "width": result.width,
                                    "height": result.height,
                                    "duration": result.duration,
                                    "fps": result.fps, "seed": result.seed,
                                    "size_bytes": (result.quality or {}).get(
                                        "size_bytes", 0)},
                            completed_items=1)
        else:
            registry.finish(context.job_id, state=AIJobState.FAILED,
                            error=result.error, why=result.why,
                            what_to_do=result.what_to_do, code=result.code)
    context.raise_if_cancelled()
    if not result.ok:
        return {
            "ok": False, "cancelled": bool(result.cancelled),
            "state": result.state, "message": result.error or
            "The clip was not generated.", "why": result.why,
            "what_to_do": result.what_to_do, "code": result.code,
            "backend": result.backend, "model": result.model,
            "paths": [], "options": list(result.options or
                                         ["retry", "choose_backend", "cancel"]),
            "state_detail": result.state_detail,
        }
    return {
        "ok": True, "cancelled": False, "state": result.state,
        "message": result.summary(), "path": str(result.path or ""),
        "paths": [str(result.path)] if result.path else [],
        "backend": result.backend, "model": result.model, "mode": result.mode,
        "width": result.width, "height": result.height, "fps": result.fps,
        "duration": result.duration, "frames": result.frames,
        "seed": result.seed, "seconds": round(result.seconds, 2),
        "mismatch": list(result.mismatch), "quality": dict(result.quality),
        "metadata": dict(result.metadata),
        "options": ["use", "variation", "extend", "send_to_timeline",
                    "send_to_scene", "upscale", "open"],
    }


def video_generate_spec(payload: dict, **options: Any) -> JobSpec:
    request = payload.get("request")
    batch = int(getattr(request, "batch", 1) or 1)
    mode = str(getattr(request, "mode", "") or "")
    title = MODE_LABELS.get(mode, "Generate clip")
    return JobSpec(
        key=job_key_for(request, batch=batch) if request is not None
        else JobKeys.AI_VIDEO_GENERATE,
        title=title,
        body=video_generate_body,
        description="Generating a clip with a local backend.",
        allow_parallel=False,
        payload=dict(payload, **options,
                     request_dict=(request.to_dict()
                                   if request is not None else {})),
    )


def video_batch_body(context: JobContext) -> dict:
    """Generate a batch of clips - only when the user asked for a batch.

    Each clip is a separate generation with its own seed and its own file; a
    cancellation keeps the clips that finished and marks the rest
    (sections 33, 35, 51).
    """
    context.raise_if_cancelled()
    service = ai_service_for(context)
    request = _video_request(context)
    batch = max(2, int(request.batch or 2))
    reporter = context.progress
    if getattr(reporter.progress, "total", 0) != float(batch):
        reporter.start(total=float(batch), message="Generating clips",
                       unit="clip")
    registry = context.get("registry")
    created: list[dict] = []
    for index in range(batch):
        if context.is_cancelled():
            break
        single = VideoRequest.from_dict(request.to_dict())
        single.batch = 1
        single.seed = (int(request.seed or 0) + index) if request.seed else 0
        single.name_stem = f"{request.name_stem or 'clip'}_{index + 1:02d}"
        result = service.generate_video(
            single, backend_id=str(context.get("backend_id") or ""),
            progress=_progress_bridge(context, unit="clip"),
            cancel=context.cancel)
        created.append({"index": index + 1, "ok": bool(result.ok),
                        "cancelled": bool(result.cancelled),
                        "path": str(result.path or ""),
                        "seed": result.seed,
                        "message": result.error if not result.ok
                        else result.summary(),
                        "why": result.why, "what_to_do": result.what_to_do,
                        "code": result.code})
        reporter.update(current=float(len(created)),
                        message=f"{len(created)} of {batch} clip(s)")
        if not result.ok and not result.cancelled:
            break
    finished = [item for item in created if item["ok"]]
    cancelled = context.is_cancelled()
    failed = [item for item in created if not item["ok"] and not item["cancelled"]]
    if registry is not None:
        state = (AIJobState.CANCELLED if cancelled
                 else AIJobState.COMPLETED if finished and not failed
                 else AIJobState.FAILED)
        registry.finish(
            context.job_id, state=state,
            output={"paths": [item["path"] for item in finished]},
            error="" if finished and not failed else (
                failed[0]["message"] if failed else "Cancelled"),
            completed_items=len(finished))
    return {
        "ok": bool(finished) and not failed,
        "cancelled": bool(cancelled),
        "created": len(finished), "requested": batch, "items": created,
        "paths": [item["path"] for item in finished],
        "message": (f"{len(finished)} of {batch} clip(s) created."
                    + (" Cancelled; the finished clips were kept."
                       if cancelled else "")
                    + (f" {len(failed)} failed." if failed else "")),
        "options": ["use", "retry", "cancel"] if finished
        else ["retry", "choose_backend"],
    }


def video_batch_spec(payload: dict, **options: Any) -> JobSpec:
    request = payload.get("request")
    batch = int(getattr(request, "batch", 1) or 1)
    return JobSpec(
        key=JobKeys.AI_VIDEO_BATCH, title=f"Generate {batch} clips",
        body=video_batch_body,
        description="Generating several clips, one after another.",
        allow_parallel=False,
        payload=dict(payload, **options,
                     request_dict=(request.to_dict()
                                   if request is not None else {})),
    )


def video_storyboard_body(context: JobContext) -> dict:
    """Turn a reviewed storyboard plan into clips, one clip per scene.

    The plan is built and approved before this runs (nothing is sent from a
    storyboard automatically), and each clip is recorded against its own scene
    so it can be sent back there (sections 30, 49, 51).
    """
    context.raise_if_cancelled()
    service = ai_service_for(context)
    plan = list(context.get("plan") or [])
    if not plan:
        return {"ok": False, "message": "There is no storyboard plan to run.",
                "what_to_do": "Build the plan first, then approve it.",
                "options": ["cancel"]}
    unapproved = [item for item in plan if not item.get("approved")]
    if unapproved:
        return {
            "ok": False,
            "message": (f"{len(unapproved)} of {len(plan)} prompt(s) have not "
                        f"been approved, so nothing was generated."),
            "what_to_do": ("Review the prompts and press Approve. The studio "
                           "does not send prompts you have not seen."),
            "options": ["review", "cancel"]}
    backend_id = str(context.get("backend_id") or "")
    output_dir = str(context.get("output_dir") or "")
    project = str(context.get("project") or "")
    requests = service.requests_from_plan(plan, backend_id=backend_id,
                                          output_dir=output_dir, project=project)
    reporter = context.progress
    if getattr(reporter.progress, "total", 0) != float(len(requests)):
        reporter.start(total=float(len(requests)), message="Generating clips",
                       unit="clip")
    produced: list[dict] = []
    for position, request in enumerate(requests, start=1):
        if context.is_cancelled():
            break
        result = service.generate_video(
            request, backend_id=str(backend_id or request.backend),
            progress=_progress_bridge(context, unit="clip"),
            cancel=context.cancel)
        produced.append({
            "index": position, "scene_id": request.scene_id,
            "name": request.name_stem, "ok": bool(result.ok),
            "cancelled": bool(result.cancelled),
            "path": str(result.path or ""), "seed": result.seed,
            "message": result.summary() if result.ok else result.error,
            "why": result.why, "what_to_do": result.what_to_do,
            "code": result.code})
        reporter.update(current=float(position),
                        message=f"{position} of {len(requests)} clip(s)")
        if not result.ok and not result.cancelled:
            # One scene refusing is usually the same problem for the rest, so
            # the run stops - and the scenes that were never tried are listed
            # rather than left looking as if they were skipped silently.
            for leftover in requests[position:]:
                produced.append({
                    "index": len(produced) + 1, "scene_id": leftover.scene_id,
                    "name": leftover.name_stem, "ok": False, "skipped": True,
                    "path": "", "seed": leftover.seed,
                    "message": "Not attempted: the run stopped at the scene "
                               "before it.",
                    "why": "A scene before this one did not generate.",
                    "what_to_do": "Fix that scene's request and run the plan "
                                  "again; scenes that already produced a clip "
                                  "are kept."})
            break
    finished = [item for item in produced if item["ok"]]
    skipped = [item for item in produced if item.get("skipped")]
    failed = [item for item in produced if not item["ok"] and not item["cancelled"]]
    registry = context.get("registry")
    if registry is not None:
        state = (AIJobState.CANCELLED if context.is_cancelled()
                 else AIJobState.COMPLETED if finished and not failed
                 else AIJobState.FAILED)
        registry.finish(context.job_id, state=state,
                        output={"paths": [item["path"] for item in finished]},
                        error="" if finished and not failed else (
                            failed[0]["message"] if failed else "Cancelled"),
                        completed_items=len(finished))
    return {
        "ok": bool(finished) and not failed,
        "cancelled": bool(context.is_cancelled()),
        "created": len(finished), "requested": len(requests),
        "items": produced,
        "paths": [item["path"] for item in finished],
        "message": (f"{len(finished)} of {len(requests)} scene clip(s) created."
                    + (" Cancelled; the finished clips were kept."
                       if context.is_cancelled() else "")
                    + (f" {len(failed)} failed." if failed else "")
                    + (f" {len(skipped)} not attempted."
                       if skipped else "")),
        "options": ["send_all_to_timeline", "open"] if finished
        else ["retry", "choose_backend", "cancel"],
    }


def video_storyboard_spec(payload: dict, **options: Any) -> JobSpec:
    plan = list(payload.get("plan") or [])
    return JobSpec(
        key=JobKeys.AI_VIDEO_BATCH,
        title=f"Generate {len(plan)} clip(s) from the storyboard",
        body=video_storyboard_body,
        description="Generating one clip per storyboard scene.",
        allow_parallel=False,
        payload=dict(payload, **options))


def image_generate_body(context: JobContext) -> dict:
    """Generate an image through the chosen backend, one job (section 20)."""
    context.raise_if_cancelled()
    service = ai_service_for(context)
    request = context.get("request")
    if request is None:
        raise ValueError("The image job was not given a request.")
    registry = context.get("registry")
    if registry is not None:
        registry.update(context.job_id, status=AIJobState.INITIALIZING,
                        message="Loading the backend")
    result = service.generate_image(
        request, backend_id=str(context.get("backend_id") or ""),
        progress=_progress_bridge(context, unit="image"), cancel=context.cancel)
    if registry is not None:
        if getattr(result, "cancelled", False):
            registry.finish(context.job_id, state=AIJobState.CANCELLED,
                            error="Image generation was cancelled.")
        elif result.ok:
            registry.finish(context.job_id, state=AIJobState.COMPLETED,
                            output={"paths": list(result.paths),
                                    "width": result.width,
                                    "height": result.height,
                                    "seeds": list(result.seeds)},
                            completed_items=len(result.paths))
        else:
            registry.finish(context.job_id, state=AIJobState.FAILED,
                            error=result.error, why=result.why,
                            what_to_do=result.what_to_do, code=result.code)
    if not result.ok:
        return {"ok": False, "cancelled": bool(getattr(result, "cancelled", False)),
                "state": result.state, "message": result.error,
                "why": result.why, "what_to_do": result.what_to_do,
                "code": result.code, "backend": result.backend,
                "paths": [], "seeds": [],
                "options": ["retry", "choose_backend", "choose_model", "cancel"]}
    return {"ok": True, "cancelled": False, "state": result.state,
            "message": f"{len(result.paths)} image(s) created.",
            "paths": list(result.paths), "seeds": list(result.seeds),
            "backend": result.backend, "model": result.model,
            "width": result.width, "height": result.height,
            "seconds": round(result.seconds, 2),
            "quality": dict(result.quality), "metadata": dict(result.metadata),
            "options": ["use", "variation", "upscale", "send_to_scene"]}


def image_generate_spec(payload: dict, **options: Any) -> JobSpec:
    request = payload.get("request")
    batch = int(getattr(request, "batch", 1) or 1)
    return JobSpec(
        key=JobKeys.AI_IMAGE_GENERATE,
        title=f"Generate {batch} image(s)" if batch > 1 else "Generate image",
        body=image_generate_body,
        description="Generating with the chosen backend.",
        allow_parallel=False,
        payload=dict(payload, **options,
                     request_dict=(request.to_dict()
                                   if request is not None else {})),
    )


def ai_check_body(context: JobContext) -> dict:
    """Run a light or deep check on one backend (section 7)."""
    context.raise_if_cancelled()
    service = ai_service_for(context)
    provider_id = str(context.get("backend_id") or "")
    deep = bool(context.get("deep", False))
    reporter = context.progress
    if getattr(reporter.progress, "total", 0) != 1.0:
        reporter.start(total=1.0, message="Checking the backend", unit="check")
    entry = service.manager.get(provider_id)
    if entry is None or entry.backend is None:
        return {"ok": False, "state": "NOT_INSTALLED",
                "message": f"No backend called '{provider_id}' was found.",
                "what_to_do": "Refresh the backend list."}
    if not deep:
        report = entry.backend.check(deep=False, service=service)
        reporter.update(current=1.0, message=report.message)
        registry = context.get("registry")
        if registry is not None:
            # The check ran to a conclusion, so the job is finished; what it
            # found (including "not verified") is in the message, not hidden in
            # the job state.
            registry.finish(
                context.job_id,
                state=(AIJobState.COMPLETED if str(report.state)
                       in (ProviderState.VERIFIED, ProviderState.AVAILABLE,
                           ProviderState.LIMITED)
                       else AIJobState.FAILED),
                error="" if report.ok else report.message,
                why=report.why, what_to_do=report.what_to_do,
                code=str(report.state))
        return {"ok": bool(report.ok), "state": str(report.state),
                "message": report.message, "why": report.why,
                "what_to_do": report.what_to_do,
                "evidence": dict(report.evidence),
                "seconds": round(report.seconds, 3),
                "deep": False}
    result = service.self_test(provider_id)
    reporter.update(current=1.0, message=str(result.get("message", "")))
    registry = context.get("registry")
    if registry is not None:
        registry.update(context.job_id,
                        status=(AIJobState.COMPLETED if result.get("ok")
                                else AIJobState.FAILED),
                        message=str(result.get("message", "")))
    payload = dict(result)
    payload["deep"] = True
    return payload


def ai_check_spec(payload: dict, **options: Any) -> JobSpec:
    return JobSpec(
        key=JobKeys.AI_BACKEND_TEST if payload.get("deep")
        else JobKeys.AI_MODEL_CHECK,
        title=("Test backend (real generation)" if payload.get("deep")
               else "Check backend"),
        body=ai_check_body,
        description="Checking whether this backend really works here.",
        allow_parallel=False,
        payload=dict(payload, **options),
    )


def ai_detect_body(context: JobContext) -> dict:
    """Detect backends and models.  Never loads one (section 11)."""
    context.raise_if_cancelled()
    service = ai_service_for(context)
    reporter = context.progress
    if getattr(reporter.progress, "total", 0) != 1.0:
        reporter.start(total=1.0, message="Looking for AI backends",
                       unit="backend")
    report = service.status(refresh=True)
    reporter.update(current=1.0, message=report.generator_note())
    payload = report.to_dict()
    payload["ok"] = True
    payload["message"] = report.generator_note()
    # A scan is also a history refresh: entries whose files have gone are
    # marked, so the history never shows a file that is not there.
    payload["history_missing"] = service.history.refresh_exists()
    return payload


def ai_detect_spec(payload: dict, **options: Any) -> JobSpec:
    return JobSpec(
        key=JobKeys.AI_DETECT, title="Detect AI backends", body=ai_detect_body,
        description="Looking for local AI backends and models.",
        allow_parallel=False, payload=dict(payload, **options))


def finalize_from_result(registry: Any, result: Any) -> Optional[AIJob]:
    """Bring a studio record in line with a finished job, whatever happened.

    A job body that raises never reaches its own ``finish`` call, so without
    this the queue would show a job that is still "running" forever.  The
    record is set to what really happened: COMPLETED, FAILED with the friendly
    explanation, or CANCELLED (sections 32, 34).
    """
    if registry is None or result is None:
        return None
    job_id = str(getattr(result, "job_id", "") or "")
    record = registry.get(job_id)
    if record is None or record.finished:
        return record
    if bool(getattr(result, "cancelled", False)):
        return registry.finish(job_id, state=AIJobState.CANCELLED,
                               error="The job was cancelled.")
    if bool(getattr(result, "succeeded", False)):
        # The body normally reported its own result; this is the safety net for
        # a body that returned without one.
        return registry.finish(job_id, state=AIJobState.COMPLETED)
    error = getattr(result, "error", None)
    actions = " ".join(str(item) for item in
                       list(getattr(error, "actions", ()) or ()))
    return registry.finish(
        job_id, state=AIJobState.FAILED,
        error=str(getattr(error, "what_happened", "")
                  or getattr(error, "title", "") or "The job failed."),
        why=str(getattr(error, "why", "") or ""),
        what_to_do=actions, code=str(getattr(error, "error_code", "") or ""))


def new_job_id(prefix: str = "aijob") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def operation_for_mode(mode: str) -> str:
    """The AI operation that a video mode is (used by the job record)."""
    return str(mode) if str(mode) in VIDEO_OPERATIONS else \
        AIOperation.TEXT_TO_VIDEO
