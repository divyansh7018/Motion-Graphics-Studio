"""Light and deep health checks for any provider (sections 7, 93, 117).

The rule this module exists to enforce:

    Only a deep check that actually ran may report ``VERIFIED``.

A light check reads paths and metadata, so it can say a backend is installed.
It cannot say a backend works.  The deep check asks the backend to initialise
and produce a real artefact, and the evidence for the claim - what was
produced, how big it was - is recorded next to the verdict so it can be
checked rather than believed.

A deep check that could not run (no FFmpeg to measure a clip, no weights, no
GPU) reports ``CHECK NOT AVAILABLE``, never ``NOT VERIFIED`` and never
``VERIFIED``: the difference between "we looked and it is broken" and "we could
not look" matters to whoever has to fix it.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .capabilities import FEATURE_LABELS
from .provider import HEALTH_DEEP, HEALTH_LIGHT, HealthCheck, HealthReport
from .types import BackendKind, ProviderState, is_reachable

__all__ = ["check_provider", "check_model", "deep_check_for", "DEEP_CHECK_OPERATION"]

#: Which operation a deep check runs, per backend kind.
DEEP_CHECK_OPERATION: dict[str, str] = {
    BackendKind.IMAGE: "text_to_image",
    BackendKind.UPSCALE: "upscale",
    BackendKind.BACKGROUND_REMOVAL: "background_removal",
    BackendKind.VIDEO: "text_to_video",
}


def check_provider(provider: Any, *, level: str = HEALTH_LIGHT,
                   model: Any = None, service: Any = None) -> HealthReport:
    """Run the light or deep check for one provider.

    ``service`` is the AI service to run the deep check through (so the check
    uses exactly the same code path a user's generation does, which is the only
    way the result means anything).  Without one, a deep check reports
    ``CHECK NOT AVAILABLE`` rather than borrowing a different route.
    """
    started = time.monotonic()
    kind = str(getattr(provider, "kind", "") or "")
    report = HealthReport(provider=str(getattr(provider, "id", "") or ""),
                          model=str(getattr(model, "id", "") or ""), level=level)

    if level != HEALTH_DEEP:
        return _light(provider, report, started)
    return _deep(provider, report, started, model=model, service=service,
                 kind=kind)


def _light(provider: Any, report: HealthReport, started: float) -> HealthReport:
    try:
        status = provider.status()
    except Exception as exc:  # noqa: BLE001 - a broken backend is data, not a crash
        report.state = ProviderState.NOT_VERIFIED
        report.ok = False
        report.message = f"The backend could not report its state: {exc}"
        report.what_to_do = "Reinstall the package this backend needs, or use another one."
        report.checks.append(HealthCheck(name="status", ok=False,
                                         state=ProviderState.NOT_VERIFIED,
                                         detail=str(exc)))
        report.seconds = time.monotonic() - started
        return report

    state = str(getattr(status, "state", ProviderState.NOT_VERIFIED))
    report.state = state
    report.ok = bool(getattr(status, "available", False))
    report.message = str(getattr(status, "reason", "") or state)
    report.what_to_do = " ".join(str(item) for item in
                                 (getattr(status, "instructions", None) or []))
    report.checks.append(HealthCheck(name="status", ok=report.ok, state=state,
                                     detail=report.message))
    try:
        models = list(provider.models())
    except Exception as exc:  # noqa: BLE001
        report.checks.append(HealthCheck(
            name="models", ok=False, state=ProviderState.NOT_VERIFIED,
            detail=f"Listing models failed: {exc}"))
        report.state = ProviderState.LIMITED
    else:
        report.checks.append(HealthCheck(
            name="models", ok=True, state=ProviderState.AVAILABLE,
            detail=f"{len(models)} model(s) found"))
        report.evidence["model_count"] = len(models)
    report.seconds = time.monotonic() - started
    return report


def _deep(provider: Any, report: HealthReport, started: float, *,
          model: Any, service: Any, kind: str) -> HealthReport:
    status = provider.status()
    if not is_reachable(str(getattr(status, "state", ""))):
        report.state = str(getattr(status, "state", ProviderState.NOT_INSTALLED))
        report.message = (f"Nothing to check: this backend is "
                          f"{report.state}.")
        report.what_to_do = " ".join(str(item) for item in
                                     (getattr(status, "instructions", None) or []))
        report.checks.append(HealthCheck(name="deep", ok=False, state=report.state,
                                         detail=report.message))
        report.seconds = time.monotonic() - started
        return report

    if service is None:
        report.state = ProviderState.CHECK_NOT_AVAILABLE
        report.message = ("A deep check needs to run a real generation, and no "
                          "service was supplied to run it through.")
        report.what_to_do = ("Run the backend's test from the AI Studio, which "
                             "uses the same path a generation does.")
        report.checks.append(HealthCheck(
            name="deep", ok=False, state=ProviderState.CHECK_NOT_AVAILABLE,
            detail=report.message))
        report.seconds = time.monotonic() - started
        return report

    operation = DEEP_CHECK_OPERATION.get(kind, "text_to_image")
    report.checks.append(HealthCheck(
        name="operation", ok=True, state=ProviderState.AVAILABLE,
        detail=f"running one {operation.replace('_', ' ')}"))
    result = service.self_test(provider_id=str(getattr(provider, "id", "")),
                               model=model, operation=operation)
    report.seconds = time.monotonic() - started
    if result.get("ok"):
        report.state = ProviderState.VERIFIED
        report.ok = True
        report.message = str(result.get("message") or
                             "The backend initialised and produced a real file.")
        report.evidence.update(dict(result.get("evidence") or {}))
        report.checks.append(HealthCheck(
            name="result", ok=True, state=ProviderState.VERIFIED,
            detail=report.message))
        return report
    report.state = str(result.get("state") or ProviderState.NOT_VERIFIED)
    report.ok = False
    report.message = str(result.get("message") or "The check did not succeed.")
    report.why = str(result.get("why") or "")
    report.what_to_do = str(result.get("what_to_do") or "")
    report.checks.append(HealthCheck(name="result", ok=False, state=report.state,
                                     detail=report.message))
    return report


def check_model(provider: Any, model: Any, *, level: str = HEALTH_LIGHT,
                service: Any = None) -> HealthReport:
    """Check one model rather than a whole backend (sections 6, 7)."""
    report = check_provider(provider, level=level, model=model, service=service)
    report.model = str(getattr(model, "id", "") or "")
    return report


def deep_check_for(kind: str) -> str:
    """Which capability a deep check proves, for the report's wording."""
    return FEATURE_LABELS.get(DEEP_CHECK_OPERATION.get(kind, ""), "")


def summarise(reports: list) -> dict:
    """A count of states across several health reports (for the manager UI)."""
    counts: dict[str, int] = {}
    for report in reports:
        state = str(getattr(report, "state", "") or "UNKNOWN")
        counts[state] = counts.get(state, 0) + 1
    return counts


def state_for_paths(paths: list, *, missing_reason: str) -> tuple[str, str]:
    """The state a path-based backend is in, given the paths it needs."""
    existing = [Path(item) for item in paths if str(item or "").strip()]
    found = [item for item in existing if item.exists()]
    if not existing:
        return ProviderState.NOT_INSTALLED, "No path is configured."
    if not found:
        return ProviderState.NOT_INSTALLED, missing_reason
    if len(found) < len(existing):
        return ProviderState.LIMITED, (f"{len(found)} of {len(existing)} configured "
                                       f"paths exist.")
    return ProviderState.AVAILABLE, f"{len(found)} path(s) found."
