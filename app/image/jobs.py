"""Background job bodies for Image Studio (Stage F, sections 34, 35, 69).

Plain functions with the :class:`app.jobs.spec.JobContext` signature, so the
same code runs in the GUI's thread pool, from the CLI and in tests.  Nothing
here touches Qt.

Cancellation is wired down to the backend: the job's cancel token is handed to
the provider, which kills its subprocess or stops its loop, so a cancelled
generation leaves no process behind and keeps the images it had already written.
"""

from __future__ import annotations

from typing import Any

from ..core.logging_setup import get_logger
from ..jobs.keys import JobKeys
from ..jobs.spec import JobContext, JobSpec
from .provider import GenerationRequest

LOGGER = get_logger("image.jobs")

__all__ = [
    "image_service_for",
    "generate_body",
    "generate_spec",
    "batch_body",
    "batch_spec",
    "detect_body",
    "detect_spec",
    "library_scan_body",
    "library_scan_spec",
    "thumbnails_body",
    "thumbnails_spec",
    "upscale_body",
    "upscale_spec",
    "import_body",
    "import_spec",
]


def image_service_for(context: JobContext) -> Any:
    """The Image Studio service, taken from the context or built on demand."""
    service = context.get("service")
    if service is not None:
        return service
    from .service import ImageService

    paths = context.paths
    data_root = getattr(paths, "data_root", None)
    return ImageService(context.settings, data_root=data_root)


def _progress_bridge(context: JobContext):
    """Turn generation states into job progress."""
    def report(state: str, fraction: float) -> None:
        reporter = context.progress
        value = max(0.0, min(1.0, float(fraction)))
        if getattr(reporter.progress, "total", 0) != 1.0:
            reporter.start(total=1.0, message=str(state), unit="image")
        reporter.update(current=value, message=str(state))
    return report


def _request_from(context: JobContext) -> GenerationRequest:
    raw = context.get("request")
    if isinstance(raw, GenerationRequest):
        return raw
    if isinstance(raw, dict):
        return GenerationRequest.from_dict(raw)
    raise ValueError("The image job was not given a generation request.")


# --------------------------------------------------------------------------
# Generate
# --------------------------------------------------------------------------

def generate_body(context: JobContext) -> dict:
    """Generate one image (or one batch) with the chosen backend.

    A failure is returned rather than raised so the job result carries what
    happened, why and what to do - and so the UI can offer Retry, Choose another
    model or Cancel instead of switching backends on its own (section 36).
    """
    context.raise_if_cancelled()
    service = image_service_for(context)
    request = _request_from(context)

    result = service.generate(request, progress=_progress_bridge(context),
                              cancel=context.cancel)
    context.raise_if_cancelled()
    if not result.ok:
        return {
            "ok": False,
            "cancelled": bool(result.cancelled),
            "state": result.state,
            "message": result.error or "The generation did not finish.",
            "why": result.why,
            "what_to_do": result.what_to_do,
            "code": result.code,
            "backend": result.backend,
            "paths": [],
            "seeds": [],
            "quality": {},
            "metadata": {},
            # What the user can do next, without the UI inventing options.
            "options": ["retry", "choose_model", "cancel"],
        }
    return {
        "ok": True,
        "cancelled": False,
        "state": result.state,
        "message": f"{len(result.paths)} image(s) created.",
        "paths": list(result.paths),
        "seeds": list(result.seeds),
        "model": result.model,
        "backend": result.backend,
        "width": result.width,
        "height": result.height,
        "output_format": result.output_format,
        "seconds": round(result.seconds, 2),
        "quality": dict(result.quality),
        "metadata": dict(result.metadata),
        "options": ["use", "variation", "upscale", "send_to_scene"],
    }


def generate_spec(context_payload: dict, **options: Any) -> JobSpec:
    request = context_payload.get("request")
    batch = int(getattr(request, "batch", 1) or 1) if request is not None else 1
    return JobSpec(
        key=JobKeys.IMAGE_BATCH if batch > 1 else JobKeys.IMAGE_GENERATE,
        title="Generate image" if batch <= 1 else f"Generate {batch} images",
        body=generate_body,
        description="Generating with the local image backend.",
        allow_parallel=False,
        payload=dict(context_payload, **options),
    )


def batch_body(context: JobContext) -> dict:
    """Generate a batch, reporting each image as it lands.

    One job for the batch, but each image is a distinct entry with its own seed
    and its own path, and the images already written survive a cancellation.
    """
    context.raise_if_cancelled()
    service = image_service_for(context)
    request = _request_from(context)
    batch = max(1, int(request.batch or 1))
    reporter = context.progress
    if getattr(reporter.progress, "total", 0) != float(batch):
        reporter.start(total=float(batch), message="Generating images", unit="image")

    collected: list[dict] = []
    for index in range(batch):
        context.raise_if_cancelled()
        single = GenerationRequest.from_dict(request.to_dict())
        single.batch = 1
        if index:
            single.seed = (int(request.seed or 0) + index) if request.seed else 0
        single.name_stem = f"{request.name_stem or 'image'}_{index + 1:02d}"
        result = service.generate(single, cancel=context.cancel,
                                  progress=_progress_bridge(context))
        collected.append({
            "index": index + 1,
            "ok": bool(result.ok),
            "cancelled": bool(result.cancelled),
            "paths": list(result.paths),
            "seeds": list(result.seeds),
            "message": result.error if not result.ok else "Created.",
            "why": result.why,
            "what_to_do": result.what_to_do,
        })
        reporter.update(current=float(index + 1),
                        message=f"{index + 1} of {batch} image(s)")

    created = sum(len(item["paths"]) for item in collected if item["ok"])
    failed = [item for item in collected if not item["ok"] and not item["cancelled"]]
    cancelled = any(item["cancelled"] for item in collected)
    return {
        "ok": created > 0 and not failed,
        "cancelled": bool(cancelled),
        "created": created,
        "requested": batch,
        "items": collected,
        "message": (f"{created} of {batch} image(s) created."
                    + (" Some were cancelled." if cancelled else "")
                    + (f" {len(failed)} failed." if failed else "")),
        "options": ["use", "cancel"] if created else ["retry", "choose_model"],
    }


def batch_spec(context_payload: dict, **options: Any) -> JobSpec:
    return JobSpec(
        key=JobKeys.IMAGE_BATCH,
        title="Generate image batch",
        body=batch_body,
        description="Generating a batch of images.",
        allow_parallel=False,
        payload=dict(context_payload, **options),
    )


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

def detect_body(context: JobContext) -> dict:
    """Detect backends and models.  Never loads one (section 41)."""
    context.raise_if_cancelled()
    service = image_service_for(context)
    reporter = context.progress
    if getattr(reporter.progress, "total", 0) != 1.0:
        reporter.start(total=1.0, message="Looking for image backends",
                       unit="backend")
    status = service.status(refresh=True)
    reporter.update(current=1.0,
                    message=f"{status.model_count} model(s) found")
    return status.to_dict()


def detect_spec(context_payload: dict, **options: Any) -> JobSpec:
    return JobSpec(
        key=JobKeys.IMAGE_DETECT,
        title="Detect image backends",
        body=detect_body,
        description="Looking for local image backends and models.",
        allow_parallel=False,
        payload=dict(context_payload, **options),
    )


# --------------------------------------------------------------------------
# Library
# --------------------------------------------------------------------------

def library_scan_body(context: JobContext) -> dict:
    """Scan the library folder in the background so the grid never freezes."""
    context.raise_if_cancelled()
    service = image_service_for(context)
    if service.library is None:
        return {"ok": False, "count": 0, "entries": [],
                "message": "No library folder is configured."}
    reporter = context.progress
    if getattr(reporter.progress, "total", 0) != 1.0:
        reporter.start(total=1.0, message="Scanning the image library",
                       unit="image")
    page = service.query()
    reporter.update(current=1.0, message=f"{page.total} image(s) in the library")
    return {
        "ok": True,
        "count": page.total,
        "entries": [entry.to_dict() for entry in page.entries],
        "duplicates": {key: [item.path for item in items]
                       for key, items in service.duplicates().items()},
        "message": f"{page.total} image(s) in the library.",
    }


def library_scan_spec(context_payload: dict, **options: Any) -> JobSpec:
    return JobSpec(
        key=JobKeys.IMAGE_LIBRARY_SCAN,
        title="Scan image library",
        body=library_scan_body,
        description="Scanning the image library.",
        allow_parallel=False,
        payload=dict(context_payload, **options),
    )


def thumbnails_body(context: JobContext) -> dict:
    """Build thumbnails for a page of entries, on a background thread."""
    context.raise_if_cancelled()
    service = image_service_for(context)
    raw_paths = context.get("paths") or []
    reporter = context.progress
    total = max(1, len(raw_paths))
    if getattr(reporter.progress, "total", 0) != float(total):
        reporter.start(total=float(total), message="Building thumbnails",
                       unit="thumbnail")
    made: list[str] = []
    for index, raw in enumerate(raw_paths):
        context.raise_if_cancelled()
        thumb = service.thumbnail(raw)
        if thumb is not None:
            made.append(str(thumb))
        reporter.update(current=float(index + 1),
                        message=f"{index + 1} of {total} thumbnail(s)")
    return {"ok": True, "count": len(made), "paths": made,
            "message": f"{len(made)} thumbnail(s) ready."}


def thumbnails_spec(context_payload: dict, **options: Any) -> JobSpec:
    return JobSpec(
        key=JobKeys.IMAGE_THUMBNAILS,
        title="Build thumbnails",
        body=thumbnails_body,
        description="Building image thumbnails.",
        allow_parallel=False,
        payload=dict(context_payload, **options),
    )


# --------------------------------------------------------------------------
# Editing and importing
# --------------------------------------------------------------------------

def upscale_body(context: JobContext) -> dict:
    """Upscale an image, always labelling which method really ran."""
    context.raise_if_cancelled()
    service = image_service_for(context)
    source = context.get("source")
    if not source:
        raise ValueError("The upscale job needs a source image.")
    reporter = context.progress
    if getattr(reporter.progress, "total", 0) != 1.0:
        reporter.start(total=1.0, message="Upscaling the image", unit="image")
    result = service.upscale_image(
        source, scale=float(context.get("scale", 2.0) or 2.0),
        method=str(context.get("method", "auto") or "auto"),
        output_dir=context.get("output_dir") or None)
    reporter.update(current=1.0, message=result.describe())
    return {
        "ok": bool(result.ok),
        # `label` is the *method that really ran* (Standard Resize or AI
        # Upscale). It is never inferred from what was requested.
        "method": result.method,
        "label": result.method_label,
        "output_path": str(result.path) if result.path else "",
        "source_size": list(result.source_size),
        "output_size": list(result.output_size),
        "scale": result.scale,
        "downgraded": bool(result.downgraded),
        "message": result.describe(),
        "error": result.error,
        "what_to_do": result.what_to_do,
        "edit_history": list(result.edit_history),
    }


def upscale_spec(context_payload: dict, **options: Any) -> JobSpec:
    return JobSpec(
        key=JobKeys.IMAGE_UPSCALE,
        title="Upscale image",
        body=upscale_body,
        description="Upscaling the image.",
        allow_parallel=False,
        payload=dict(context_payload, **options),
    )


def import_body(context: JobContext) -> dict:
    """Import an image into a project or the library."""
    context.raise_if_cancelled()
    service = image_service_for(context)
    source = context.get("source")
    destination = context.get("destination")
    if not source or not destination:
        raise ValueError("The import job needs a source file and a destination.")
    reporter = context.progress
    if getattr(reporter.progress, "total", 0) != 1.0:
        reporter.start(total=1.0, message="Importing the image", unit="image")
    report = service.import_image(
        source, destination=destination,
        requested_format=str(context.get("requested_format", "") or ""),
        overwrite=bool(context.get("overwrite", False)))
    reporter.update(current=1.0, message=report.describe())
    return {
        "ok": bool(report.ok),
        "path": str(report.path) if report.path else "",
        "source": str(source),
        "size_bytes": report.size_bytes,
        "message": report.describe(),
        "error": report.error,
        "what_to_do": report.what_to_do,
        # Reported, never silent: a format that could not hold the image.
        "format_changed": report.format_changed,
        "verified": bool(report.verified),
    }


def import_spec(context_payload: dict, **options: Any) -> JobSpec:
    return JobSpec(
        key=JobKeys.IMAGE_IMPORT,
        title="Import image",
        body=import_body,
        description="Importing the image.",
        allow_parallel=False,
        payload=dict(context_payload, **options),
    )
