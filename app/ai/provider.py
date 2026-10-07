"""The one contract every AI backend keeps (sections 1, 7, 36, 86).

A backend - a diffusion model, a video model, an upscaler, a background
remover, a local HTTP server, a command line tool - describes itself with this
interface and nothing else.  The GUI, the CLI and the job queue only ever talk
to it, so no part of the application is wired to one particular provider.

Two rules are built into the shapes here rather than left to callers:

* **discovery is free.**  ``status()``, ``capabilities()``, ``models()`` and
  ``settings_schema()`` must not load a model, import torch, or contact
  anything slow.  A backend that costs a gigabyte to *describe* itself is a
  backend that makes the studio feel broken when it opens (sections 7, 42, 87).
* **a health check says which check it ran.**  A light check looks at paths and
  metadata; a deep check initialises the model and produces something.  Only a
  deep check that really ran may report ``VERIFIED`` - the word is never earned
  by checking that a folder exists (section 7).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from .capabilities import AICapabilities
from .types import (BackendKind, DeviceRequirement, ProviderState, is_reachable)

__all__ = [
    "SettingField",
    "ProviderInfo",
    "HealthCheck",
    "HealthReport",
    "AIProvider",
    "HEALTH_LIGHT",
    "HEALTH_DEEP",
]

HEALTH_LIGHT = "light"
HEALTH_DEEP = "deep"


@dataclass
class SettingField:
    """One configurable value of a backend (section 1's settings schema).

    The UI builds its configuration form from these, which is why a new backend
    needs no new dialog: it declares its fields and the same form renders them.
    """

    name: str = ""
    label: str = ""
    kind: str = "text"          # text | path | url | int | float | bool | choice
    default: Any = ""
    help: str = ""
    choices: list = field(default_factory=list)
    required: bool = False
    #: Shown when the value is secret-ish (an endpoint token for a future
    #: remote provider).  Nothing in this stage stores one.
    advanced: bool = False

    def validate(self, value: Any) -> tuple[bool, str]:
        """Whether a value is usable, and why not if it is not."""
        if self.kind == "bool":
            return True, ""
        text = "" if value is None else str(value).strip()
        if not text:
            if self.required:
                return False, f"{self.label or self.name} cannot be empty."
            return True, ""
        if self.kind == "int":
            try:
                int(text)
            except ValueError:
                return False, f"{self.label or self.name} must be a whole number."
        elif self.kind == "float":
            try:
                float(text)
            except ValueError:
                return False, f"{self.label or self.name} must be a number."
        elif self.kind == "choice" and self.choices and text not in self.choices:
            return False, (f"{self.label or self.name} must be one of: "
                           + ", ".join(str(item) for item in self.choices))
        return True, ""

    def to_dict(self) -> dict:
        return {
            "name": self.name, "label": self.label, "kind": self.kind,
            "default": self.default, "help": self.help,
            "choices": [str(item) for item in self.choices],
            "required": bool(self.required), "advanced": bool(self.advanced),
        }


@dataclass
class ProviderInfo:
    """Everything a manager screen shows about a backend (section 4)."""

    id: str = ""
    name: str = ""
    kind: str = BackendKind.IMAGE
    version: str = ""
    description: str = ""
    #: Where it lives: a folder for a local backend, a URL for a server.
    location: str = ""
    #: What the user would have to install, in their words.
    install_hint: str = ""
    requirement: str = DeviceRequirement.UNKNOWN
    #: "python" | "diffusers" | "comfyui" | "http" | "command" | "onnx" | ...
    transport: str = ""
    #: True when this backend needs the network, even if only loopback.
    network: bool = False
    licence: str = ""
    homepage: str = ""

    @property
    def requirement_label(self) -> str:
        from .types import REQUIREMENT_LABELS

        return REQUIREMENT_LABELS.get(self.requirement, self.requirement)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "kind": self.kind,
            "version": self.version, "description": self.description,
            "location": self.location, "install_hint": self.install_hint,
            "requirement": self.requirement,
            "requirement_label": self.requirement_label,
            "transport": self.transport, "network": bool(self.network),
            "licence": self.licence, "homepage": self.homepage,
        }


@dataclass
class HealthCheck:
    """One thing a health pass looked at."""

    name: str = ""
    ok: bool = False
    detail: str = ""
    state: str = ProviderState.NOT_VERIFIED

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": bool(self.ok), "detail": self.detail,
                "state": self.state}


@dataclass
class HealthReport:
    """The result of a light or deep check (section 7)."""

    provider: str = ""
    model: str = ""
    level: str = HEALTH_LIGHT
    state: str = ProviderState.NOT_VERIFIED
    ok: bool = False
    message: str = ""
    why: str = ""
    what_to_do: str = ""
    seconds: float = 0.0
    checks: list = field(default_factory=list)
    #: For a deep check: what it produced, so the claim can be checked.
    evidence: dict = field(default_factory=dict)

    @property
    def deep(self) -> bool:
        return self.level == HEALTH_DEEP

    @property
    def verified(self) -> bool:
        """Only a deep check that ran and worked may say this."""
        return bool(self.deep and self.ok and self.state == ProviderState.VERIFIED)

    def describe(self) -> str:
        head = f"{self.provider or 'backend'}"
        if self.model:
            head += f" / {self.model}"
        lines = [f"{head}: {self.state} ({self.level} check, {self.seconds:.2f}s)"]
        if self.message:
            lines.append(f"  {self.message}")
        for check in self.checks:
            mark = "ok" if check.ok else "no"
            lines.append(f"  [{mark}] {check.name}: {check.detail}")
        if self.what_to_do:
            lines.append(f"  what to do: {self.what_to_do}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider, "model": self.model, "level": self.level,
            "state": self.state, "ok": bool(self.ok), "verified": self.verified,
            "message": self.message, "why": self.why,
            "what_to_do": self.what_to_do,
            "seconds": round(float(self.seconds or 0.0), 3),
            "checks": [check.to_dict() for check in self.checks],
            "evidence": dict(self.evidence),
        }


@dataclass
class ProviderStatus:
    """Whether a backend can be used right now, and why not if it cannot.

    ``state`` is one of the seven words in :class:`app.ai.types.ProviderState`;
    ``available`` is derived from it so the two can never disagree.
    """

    state: str = ProviderState.NOT_INSTALLED
    reason: str = ""
    instructions: list = field(default_factory=list)
    version: str = ""
    device: str = "cpu"

    @property
    def available(self) -> bool:
        return is_reachable(self.state)

    def to_dict(self) -> dict:
        return {
            "available": self.available, "state": self.state,
            "reason": self.reason,
            "instructions": [str(item) for item in self.instructions],
            "version": self.version, "device": self.device,
        }


class AIProvider(ABC):
    """One AI backend.

    Implementations must be cheap to construct and must not load a model in
    ``__init__``: discovery happens when the AI Studio opens, and weights are
    loaded only when a generation is asked for (sections 42, 87).
    """

    #: Stable id, used in settings and logs.
    id: str = ""
    #: Human name.
    name: str = ""
    #: What it produces - one of :class:`app.ai.types.BackendKind`.
    kind: str = BackendKind.IMAGE
    #: How it runs: "python" | "diffusers" | "comfyui" | "http" | "command"
    #: | "onnx" | "builtin".
    transport: str = ""
    version: str = ""

    # -- description (must not load anything) -------------------------------

    def info(self) -> ProviderInfo:
        """Metadata for the manager screen.  Cheap: no model, no network."""
        requirement = DeviceRequirement.UNKNOWN
        if str(self.kind) == BackendKind.VIDEO:
            requirement = getattr(self, "device_requirement", requirement)
        return ProviderInfo(
            id=self.id, name=self.name or self.id, kind=self.kind,
            version=self.version, description=self.describe(),
            location=self.location(), install_hint=self.install_hint(),
            requirement=requirement, transport=self.transport,
            network=bool(getattr(self, "uses_network", False)),
            licence=str(getattr(self, "licence", "") or ""),
            homepage=str(getattr(self, "homepage", "") or ""),
        )

    def describe(self) -> str:
        return ""

    def location(self) -> str:
        """Where this backend lives - a path or an endpoint."""
        return ""

    def install_hint(self) -> str:
        """What the user would have to install, in their words."""
        return ""

    def uses_network(self) -> bool:
        """Whether it talks to something over a socket (loopback included)."""
        return False

    def settings_schema(self) -> list[SettingField]:
        """The values this backend can be configured with (section 1)."""
        return []

    # -- discovery ---------------------------------------------------------

    @abstractmethod
    def status(self) -> ProviderStatus:
        """Is this backend usable here?  Must not load a model."""

    @abstractmethod
    def capabilities(self) -> AICapabilities:
        """What this backend can do.  Report False, never guess True."""

    @abstractmethod
    def models(self) -> list:
        """The models this backend can see.  Must not load a model."""

    # -- health ------------------------------------------------------------

    def health(self, *, level: str = HEALTH_LIGHT,
               model: Any = None) -> HealthReport:
        """Check this backend (section 7).

        The default light check asks the backend for its status and its models
        and records what came back.  The default deep check runs one small,
        real generation - that is the only honest way to earn ``VERIFIED``, and
        a backend whose deep check cannot run reports ``CHECK NOT AVAILABLE``
        rather than guessing.  Backends override this with something cheaper or
        more specific when they can.
        """
        started = time.monotonic()
        report = HealthReport(provider=self.id, level=level,
                              model=str(getattr(model, "id", "") or ""))
        if level == HEALTH_LIGHT:
            status = self.status()
            report.state = status.state
            report.ok = status.available
            report.message = status.reason or "The backend is present."
            report.what_to_do = " ".join(status.instructions)
            report.checks.append(HealthCheck(
                name="status", ok=status.available, state=status.state,
                detail=status.reason or status.state))
            try:
                models = list(self.models())
            except Exception as exc:  # noqa: BLE001 - a broken listing is data
                models = []
                report.checks.append(HealthCheck(
                    name="models", ok=False, state=ProviderState.NOT_VERIFIED,
                    detail=f"Listing models failed: {exc}"))
            else:
                report.checks.append(HealthCheck(
                    name="models", ok=True, state=ProviderState.AVAILABLE,
                    detail=f"{len(models)} model(s) found"))
            report.seconds = time.monotonic() - started
            return report

        report.state = ProviderState.CHECK_NOT_AVAILABLE
        report.message = (f"'{self.name or self.id}' does not provide a deep "
                          f"check, so nothing was verified.")
        report.what_to_do = ("Use the backend's own test command, or run a "
                             "small generation from the AI Studio.")
        report.checks.append(HealthCheck(
            name="deep", ok=False, state=ProviderState.CHECK_NOT_AVAILABLE,
            detail="No deep check is implemented for this backend."))
        report.seconds = time.monotonic() - started
        return report

    # -- model lifetime ----------------------------------------------------

    def load(self, model: Any = None) -> None:
        """Prepare a model for use.  Default: nothing to do."""

    def unload(self) -> None:
        """Release a loaded model.  Default: nothing held."""

    def loaded_model(self) -> str:
        """Which model is currently loaded, for the memory manager."""
        return ""

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _started() -> float:
        return time.monotonic()

    def state_for(self, *, ok: bool, verified: bool = False) -> str:
        return (ProviderState.VERIFIED if (ok and verified) else
                ProviderState.AVAILABLE if ok else ProviderState.NOT_INSTALLED)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} id={self.id!r} kind={self.kind!r}>"


def optional_path(value: Any) -> Optional[str]:
    """A configurable path as a string, or None when it is not set."""
    text = "" if value is None else str(value).strip()
    return text or None


def describe_capabilities(capabilities: Any) -> str:
    """A sentence listing what a backend can do, in plain words.

    Used by the interface so a beginner reads "Text to video, image to video"
    rather than a row of ticks, and by the reports so a claim and its evidence
    are never far apart (sections 6, 93).
    """
    try:
        names = list(capabilities.features())
    except Exception:  # noqa: BLE001 - a broken capabilities object is data
        return "This backend did not describe its abilities."
    if not names:
        return "This backend describes no capabilities."
    from .capabilities import FEATURE_LABELS

    labels = [FEATURE_LABELS.get(name, name.replace("_", " ")) for name in names]
    return ", ".join(labels) + "."
