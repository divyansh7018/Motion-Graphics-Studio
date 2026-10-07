"""The unified AI backend hierarchy (sections 2, 4, 6, 61).

One class answers for every engine the studio can drive - a local Python model,
Diffusers, ComfyUI, a local HTTP server, a local command, ONNX, or any engine
added later - and one set of questions is asked of all of them:

    id, name, type, version, capabilities, status, device requirements,
    model information, settings schema, logs

This module holds the parts that are the same for all of them:

* :class:`AIBackend` - the abstract contract, with the default answers written
  down once instead of copied into every backend;
* :class:`ImageBackendAdapter` - Stage F's image providers wrapped so they are
  AI backends too, without a single line of Stage F changing.  That is how the
  existing Image Studio keeps working while the AI Studio gains a common face.

Backend types are named after the *transport*, because that is what determines
what the user has to install: ``python``, ``diffusers``, ``comfyui``, ``http``,
``command``, ``onnx``, ``builtin``.  A cloud adapter is described (section 4)
but not implemented: it reports NOT SUPPORTED rather than pretending.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from ..core.logging_setup import log_event
from .capabilities import AICapabilities
from .provider import ProviderStatus, SettingField
from .types import BackendKind, DeviceRequirement, ProviderState, is_reachable

__all__ = [
    "AIBackend", "ImageBackendAdapter", "CloudBackendNotSupported",
    "BACKEND_TYPES", "TRANSPORT_LABELS", "backend_type_label",
    "normalise_state", "STATE_ALIASES",
    "describe_provider", "describe_models", "log_lines_for",
]

#: Every backend type the architecture allows (section 4).
BACKEND_TYPES: tuple[str, ...] = (
    "builtin", "python", "diffusers", "comfyui", "http", "command", "onnx",
    "cloud")

TRANSPORT_LABELS: dict[str, str] = {
    "builtin": "Built in",
    "python": "Local Python model",
    "diffusers": "Diffusers",
    "comfyui": "ComfyUI",
    "http": "Local HTTP endpoint",
    "command": "Local command",
    "onnx": "ONNX Runtime",
    "cloud": "Cloud service (not supported in this phase)",
}


def backend_type_label(transport: str) -> str:
    return TRANSPORT_LABELS.get(str(transport or ""), str(transport or ""))


class AIBackend:
    """Everything the studio needs to know about one way of generating.

    Subclasses fill in the class attributes and override the methods they can
    answer better.  The defaults are deliberately conservative: unknown
    capabilities are ``False``, an unknown device requirement is ``UNKNOWN``,
    and an unimplemented operation raises rather than returning something that
    looks like a result.
    """

    #: Stable identifier used in settings, jobs and project files.
    id = ""
    #: What the user sees.
    name = ""
    #: Image, video, upscale, background_removal or llm.
    kind = BackendKind.IMAGE
    #: How it runs: builtin, python, diffusers, comfyui, http, command, onnx.
    transport = "python"
    #: The backend's own version, when it has one.
    version = ""
    #: Whether it needs real hardware.
    device_requirement = DeviceRequirement.UNKNOWN
    #: Whether this backend runs an AI model.  False for the test fixture.
    is_model = True
    #: Licence and source, recorded even when the backend is not installed.
    licence = ""
    homepage = ""

    def __init__(self) -> None:
        self.timeout = 3600.0
        self.enabled = True

    # -- description -------------------------------------------------------

    def describe(self) -> str:
        """One paragraph: what this backend is and what it needs."""
        return f"{self.name} ({backend_type_label(self.transport)})."

    def location(self) -> str:
        """Where it lives: a path, an endpoint, or 'built in'."""
        return ""

    def install_hint(self) -> str:
        """How to make it available, in the user's own terms."""
        return ""

    def uses_network(self) -> bool:
        """Whether it may talk to the network at all.

        Answering ``True`` that cannot be narrowed is safer than answering
        ``False`` and being wrong: the interface warns rather than reassures.
        """
        return False

    def settings_schema(self) -> list:
        """The settings this backend accepts, as :class:`SettingField` objects."""
        return []

    # -- state -------------------------------------------------------------

    def status(self) -> ProviderStatus:
        """The backend's state right now, with instructions when it is not usable."""
        return ProviderStatus(state=ProviderState.NOT_VERIFIED,
                              reason="This backend has not been checked yet.",
                              instructions=["Run its test in the AI Studio."])

    def available(self) -> bool:
        try:
            return bool(self.status().available)
        except Exception:  # noqa: BLE001 - a broken backend is not available
            return False

    def state(self) -> str:
        try:
            return str(self.status().state)
        except Exception as exc:  # noqa: BLE001
            return ProviderState.NOT_VERIFIED if not exc else ProviderState.NOT_VERIFIED

    # -- capabilities and models -------------------------------------------

    def capabilities(self) -> AICapabilities:
        return AICapabilities(notes="This backend has not described its abilities.")

    def supports(self, feature: str) -> bool:
        return bool(self.capabilities().supports(feature))

    def models(self) -> list:
        """Models this backend can run.  Never loads anything to answer."""
        return []

    def model_info(self) -> dict:
        """A summary of the models, for the manager and for reports."""
        return describe_models(self)

    # -- logging -----------------------------------------------------------

    def log_lines(self, limit: int = 200) -> list[str]:
        """The tail of this backend's log, from the application's own log."""
        return log_lines_for(self, limit=limit)

    # -- generation --------------------------------------------------------

    def load(self, model: Any = None) -> None:
        """Bring the model into memory.  Backends without models do nothing."""
        raise NotImplementedError(
            f"{self.name} does not load models, so there is nothing to load.")

    def unload(self) -> None:
        """Release whatever is in memory.  Doing nothing is acceptable."""
        return None

    def loaded_model(self) -> str:
        return ""

    def generate(self, request: Any, *, progress: Any = None,
                 cancel: Any = None) -> Any:
        raise NotImplementedError(
            f"{self.name} cannot generate yet, so nothing was produced.")

    def check(self, *, deep: bool = False, service: Any = None) -> Any:
        """Run the light or deep health check (section 7)."""
        from .health import check_provider

        return check_provider(self, level="deep" if deep else "light",
                              service=service)

    def supports_operation(self, operation: str) -> bool:
        """Whether this backend can do an operation, by the operation's kind."""
        from .types import OPERATION_KIND

        kind = OPERATION_KIND.get(str(operation), "")
        feature = {
            "text_to_image": "text_to_image", "image_to_image": "image_to_image",
            "inpaint": "inpaint", "outpaint": "outpaint",
            "variation": "image_to_image", "upscale": "upscale",
            "background_removal": "background_removal",
            "text_to_video": "text_to_video", "image_to_video": "image_to_video",
            "video_to_video": "video_to_video", "video_extend": "video_extend",
            "storyboard_to_video": "storyboard_to_video",
            "model_check": "", "backend_test": "",
        }.get(str(operation), "")
        if kind == "video" and operation == "variation":
            feature = "text_to_video"
        if not feature:
            return True
        return self.supports(feature)

    def to_dict(self, *, deep: bool = False) -> dict:
        """Everything the manager shows about this backend."""
        return describe_provider(self, deep=deep)


class CloudBackendNotSupported(AIBackend):
    """A cloud adapter, described but not implemented (sections 3, 4).

    Stage G is local-first and offline-capable.  The architecture leaves room
    for a cloud engine, and this class is that room: it answers every question
    honestly - NOT SUPPORTED, and why - without sending anything anywhere.
    """

    id = "cloud"
    name = "Cloud service (not supported)"
    transport = "cloud"
    device_requirement = DeviceRequirement.UNKNOWN
    is_model = True
    licence = "n/a"

    def describe(self) -> str:
        return ("Cloud and paid generation services are not part of this phase. "
                "This entry exists so the architecture is clear: nothing is sent "
                "anywhere, and no account or key is used.")

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            state=ProviderState.NOT_SUPPORTED,
            reason=("This build is local-first and offline-capable, so cloud "
                    "generation is not supported."),
            instructions=["Install a local backend instead; the AI Studio "
                          "lists the ones this machine can run."])

    def uses_network(self) -> bool:
        return False

    def capabilities(self) -> AICapabilities:
        return AICapabilities(notes="Not supported in this phase.")

    def models(self) -> list:
        return []


class ImageBackendAdapter(AIBackend):
    """Stage F's image providers, seen as AI backends (section 61).

    The adapter is deliberately thin.  It does not reinterpret Stage F, fix its
    answers, or add capabilities it did not claim: it renames what is already
    there into the words the AI Studio uses.  Stage F's own tests keep passing
    unchanged because nothing about Stage F changes.
    """

    def __init__(self, provider: Any) -> None:
        super().__init__()
        self.provider = provider
        self.id = str(getattr(provider, "id", "") or "")
        self.name = str(getattr(provider, "label", "") or
                        getattr(provider, "name", "") or self.id)
        self.kind = BackendKind.IMAGE
        self.transport = _transport_for(provider)
        #: Whether this really runs a model.  Stage F's deterministic editor
        #: does not, and must not be counted as one (section 4).
        self.is_model = _claims_model(provider, self.transport)
        self.version = ""
        self.licence = str(getattr(provider, "licence", "") or "")
        self.homepage = str(getattr(provider, "homepage", "") or "")

    # -- description -------------------------------------------------------

    def describe(self) -> str:
        return str(getattr(self.provider, "describe", lambda: "")() or
                   f"{self.name} ({backend_type_label(self.transport)}).")

    def location(self) -> str:
        for attribute in ("endpoint", "command", "model_path", "path"):
            value = getattr(self.provider, attribute, "")
            if value:
                return str(value)
        return "Built in" if self.transport == "builtin" else ""

    def install_hint(self) -> str:
        return str(getattr(self.provider, "install_hint", lambda: "")() or "")

    def uses_network(self) -> bool:
        network = getattr(self.provider, "uses_network", None)
        if callable(network):
            return bool(network())
        return self.transport in ("http", "comfyui")

    def settings_schema(self) -> list:
        fields = getattr(self.provider, "settings_schema", None)
        if callable(fields):
            try:
                return list(fields())
            except Exception:  # noqa: BLE001
                return []
        return []

    def log_lines(self, limit: int = 200) -> list[str]:
        lines = getattr(self.provider, "log_lines", None)
        if callable(lines):
            try:
                return list(lines(limit=limit))
            except TypeError:
                return list(lines())
        return super().log_lines(limit)

    # -- state -------------------------------------------------------------

    def status(self) -> ProviderStatus:
        try:
            provider_status = self.provider.status()
        except Exception as exc:  # noqa: BLE001
            return ProviderStatus(
                state=ProviderState.NOT_VERIFIED,
                reason=f"The Stage F backend could not report its state: {exc}",
                instructions=["Check the log, or use another backend."])
        return ProviderStatus(
            state=normalise_state(str(getattr(provider_status, "state",
                                             ProviderState.NOT_VERIFIED))),
            reason=str(getattr(provider_status, "reason", "") or ""),
            instructions=list(getattr(provider_status, "instructions", []) or []),
            version=str(getattr(provider_status, "version", "") or ""),
            device=str(getattr(provider_status, "device", "") or "cpu"))

    def capabilities(self) -> AICapabilities:
        try:
            return AICapabilities.from_image(self.provider.capabilities())
        except Exception as exc:  # noqa: BLE001
            return AICapabilities(notes=f"Capabilities unavailable: {exc}")

    def models(self) -> list:
        try:
            return list(self.provider.models())
        except Exception:  # noqa: BLE001
            return []

    # -- model handling ----------------------------------------------------

    def load(self, model: Any = None) -> None:
        loader = getattr(self.provider, "load", None)
        if callable(loader):
            loader(model)
            return
        raise NotImplementedError(
            f"{self.name} loads its model itself when it generates, so there is "
            f"nothing to preload.")

    def unload(self) -> None:
        unloader = getattr(self.provider, "unload", None)
        if callable(unloader):
            unloader()

    def loaded_model(self) -> str:
        loaded = getattr(self.provider, "loaded_model", None)
        if callable(loaded):
            try:
                return str(loaded() or "")
            except Exception:  # noqa: BLE001
                return ""
        return ""

    def generate(self, request: Any, *, progress: Any = None,
                 cancel: Any = None) -> Any:
        return self.provider.generate(request, progress=progress, cancel=cancel)

    def to_dict(self, *, deep: bool = False) -> dict:
        data = super().to_dict(deep=deep)
        data["stage"] = "F"
        data["wrapped_provider"] = type(self.provider).__name__
        return data


#: Stage F writes its states in lower case (``not_installed``); the AI Studio
#: uses the directive's exact words (``NOT_INSTALLED``).  Translating here keeps
#: one vocabulary in the interface without changing a line of Stage F.
STATE_ALIASES: dict[str, str] = {
    "verified": ProviderState.VERIFIED, "available": ProviderState.AVAILABLE,
    "not_installed": ProviderState.NOT_INSTALLED,
    "not_verified": ProviderState.NOT_VERIFIED,
    "not_supported": ProviderState.NOT_SUPPORTED,
    "check not available": ProviderState.CHECK_NOT_AVAILABLE,
    "check_not_available": ProviderState.CHECK_NOT_AVAILABLE,
    "limited": ProviderState.LIMITED,
}


def normalise_state(state: str) -> str:
    """Translate a lower-case state into the studio's words, or pass it through."""
    text = str(state or "").strip()
    return STATE_ALIASES.get(text.lower(), text or ProviderState.NOT_VERIFIED)


def describe_models(provider: Any) -> dict:
    """Every model a provider offers, described the same way for every kind.

    A provider that cannot list its models is reported as listing none: the
    alternative - guessing - would put a model in the list that nothing proved
    exists (section 93).
    """
    models: list[dict] = []
    try:
        found = list(provider.models() or [])
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        log_event("AI_MODEL_LIST_FAILED",
                  f"'{getattr(provider, 'id', '')}' could not list its models",
                  error=f"{type(exc).__name__}: {exc}")
        found = []
    for model in found:
        models.append({
            "id": str(getattr(model, "id", "")),
            "name": str(getattr(model, "name", "") or getattr(model, "id", "")),
            "path": str(getattr(model, "path", "") or ""),
            "size_bytes": int(getattr(model, "size_bytes", 0) or 0),
            "status": str(getattr(model, "status", "") or ""),
            "requirement": str(getattr(model, "requirement", "") or ""),
        })
    return {"count": len(models), "models": models}


def log_lines_for(provider: Any, limit: int = 200) -> list[str]:
    """The tail of one backend's log, from the application's own log file."""
    try:
        from ..core.paths import (AppPaths, default_source_root,
                                  resolve_data_root)

        source_root = default_source_root()
        data_root, _reason = resolve_data_root(source_root)
        log_file = AppPaths(data_root, source_root).log_file
    except Exception:  # noqa: BLE001 - no log file is not an error
        return []
    if not log_file or not log_file.is_file():
        return []
    try:
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    needle = f"backend={getattr(provider, 'id', '')}"
    matching = [line for line in lines if needle in line]
    chosen = matching or lines
    return chosen[-max(1, int(limit)):]


def describe_provider(provider: Any, *, deep: bool = False) -> dict:
    """Everything the managers and reports show about one provider (section 6).

    Both provider shapes in this package - the image-shaped :class:`AIBackend`
    and the video :class:`~app.ai.video.VideoProvider` - are described here, so
    every column the interface shows exists for every kind of backend, and a
    field added once appears everywhere.
    """
    status = _safe_status(provider)
    try:
        capabilities = provider.capabilities()
    except Exception as exc:  # noqa: BLE001 - an unusable answer is data
        capabilities = AICapabilities(
            notes=f"This backend could not describe its abilities: {exc}")
    try:
        settings = [field.to_dict() for field in provider.settings_schema()]
    except Exception:  # noqa: BLE001
        settings = []
    try:
        uses_network = bool(provider.uses_network())
    except Exception:  # noqa: BLE001 - an unclear answer is the safe one
        uses_network = True
    transport = str(getattr(provider, "transport", "") or "")
    return {
        "id": str(getattr(provider, "id", "")),
        "name": str(getattr(provider, "name", "") or getattr(provider, "id", "")),
        "kind": str(getattr(provider, "kind", BackendKind.IMAGE)),
        "transport": transport,
        "type_label": backend_type_label(transport),
        "version": str(getattr(provider, "version", "") or status.version),
        "state": str(status.state), "available": bool(status.available),
        "reason": str(status.reason),
        "instructions": list(status.instructions),
        "location": str(_safe_call(provider, "location") or ""),
        "describe": str(_safe_call(provider, "describe") or ""),
        "is_model": bool(getattr(provider, "is_model", True)),
        "device_requirement": str(getattr(provider, "device_requirement", "")),
        "device": str(status.device),
        "uses_network": uses_network,
        "licence": str(getattr(provider, "licence", "") or ""),
        "homepage": str(getattr(provider, "homepage", "") or ""),
        "features": capabilities.features(),
        "capabilities": capabilities.to_dict(),
        "models": describe_models(provider),
        "settings": settings,
        "enabled": bool(getattr(provider, "enabled", True)),
        "deep": bool(deep),
    }


def _safe_status(provider: Any) -> ProviderStatus:
    """A provider's state, or NOT VERIFIED with the reason it could not say."""
    try:
        return provider.status()
    except Exception as exc:  # noqa: BLE001 - a broken backend is reported
        return ProviderStatus(
            state=ProviderState.NOT_VERIFIED,
            reason=f"The backend could not report its state: {exc}",
            instructions=["Check the log, or use another backend."])


def _safe_call(provider: Any, name: str) -> Any:
    method = getattr(provider, name, None)
    if not callable(method):
        return ""
    try:
        return method()
    except Exception:  # noqa: BLE001 - a description is never worth a crash
        return ""


def _transport_for(provider: Any) -> str:
    """Which backend type a Stage F provider is, from what it really is."""
    kind = str(getattr(provider, "kind", "") or "").lower()
    if kind in BACKEND_TYPES:
        return kind
    ident = str(getattr(provider, "id", "") or "").lower()
    for candidate in BACKEND_TYPES:
        if candidate != "cloud" and candidate in ident:
            return candidate
    return "python"


def _claims_model(provider: Any, transport: str) -> bool:
    """Whether a Stage F provider really runs a model.

    A provider that declares it is believed.  Otherwise a built-in tool (Stage
    F's deterministic image editor) plainly is not a model, while a backend the
    user had to install or configure might be - so it is counted as one, and
    only a deep check moves it past NOT VERIFIED.
    """
    declared = getattr(provider, "is_model", None)
    if declared is None:
        declared = getattr(provider, "uses_model", None)
    if callable(declared):
        try:
            declared = declared()
        except Exception:  # noqa: BLE001 - an unusable answer is no answer
            declared = None
    if declared is not None:
        return bool(declared)
    return transport != "builtin"


def summarise_backends(backends: Iterable[AIBackend]) -> dict:
    """Counts across backends, for the manager's header line."""
    counts: dict[str, int] = {}
    usable = 0
    for backend in backends:
        status = backend.status()
        state = str(getattr(status, "state", ""))
        counts[state] = counts.get(state, 0) + 1
        if is_reachable(state):
            usable += 1
    return {"total": len(counts) and sum(counts.values()) or 0,
            "usable": usable, "by_state": counts}


def setting_field(name: str, label: str, **kwargs: Any) -> SettingField:
    """A tiny helper so schemas read the same everywhere."""
    return SettingField(name=name, label=label, **kwargs)


def optional_timeout(default: float = 3600.0) -> SettingField:
    return SettingField(name="timeout", label="Timeout (seconds)", kind="float",
                        default=float(default),
                        help="The run is stopped after this long; nothing is "
                             "left running.")


def _unused(*args: Any) -> Optional[None]:  # pragma: no cover
    return None
