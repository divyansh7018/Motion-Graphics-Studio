"""The AI Backend Manager (sections 5, 6, 7, 8, 61, 92).

One place that knows every backend the studio can use, what state each is in,
what models each offers, and which one is currently loaded.  The interface, the
CLI and the job orchestrator all ask this object rather than building backends
of their own, so a backend is configured once and behaves the same everywhere.

What it deliberately does *not* do:

* it never loads a model while listing (section 11) - listing a model is a
  ``stat``, not an import;
* it never presents the Stage F test fixture as a real AI model (section 79);
* it never hides a backend that failed to build - a broken adapter appears with
  its error and its instructions rather than vanishing (section 62).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from ..core.logging_setup import log_event
from ..core.settings import Settings
from .backend import (AIBackend, CloudBackendNotSupported, ImageBackendAdapter,
                      backend_type_label)
from .capabilities import AICapabilities
from .models import ModelCache, ModelStore, human_bytes
from .provider import ProviderStatus, SettingField
from .types import (BackendKind, KIND_LABELS, ProviderState, is_reachable)
from .video_backends import VIDEO_BACKEND_CLASSES, VIDEO_BACKEND_ORDER

__all__ = ["BackendEntry", "DiscoveryReport", "AIBackendManager",
           "manager_for", "STATE_ORDER"]

#: How the manager sorts backends: the ones that work come first.
STATE_ORDER: dict[str, int] = {
    ProviderState.VERIFIED: 0,
    ProviderState.AVAILABLE: 1,
    ProviderState.LIMITED: 2,
    ProviderState.NOT_VERIFIED: 3,
    ProviderState.CHECK_NOT_AVAILABLE: 4,
    ProviderState.NOT_INSTALLED: 5,
    ProviderState.NOT_SUPPORTED: 6,
}


@dataclass
class BackendEntry:
    """One backend, its last status, and why it failed if it did."""

    backend: Optional[AIBackend] = None
    backend_id: str = ""
    import_error: str = ""
    _status: Optional[ProviderStatus] = field(default=None, repr=False)

    @property
    def id(self) -> str:
        return self.backend_id or str(getattr(self.backend, "id", ""))

    @property
    def name(self) -> str:
        return str(getattr(self.backend, "name", "") or self.id)

    @property
    def kind(self) -> str:
        return str(getattr(self.backend, "kind", BackendKind.IMAGE))

    def status(self, *, refresh: bool = False) -> ProviderStatus:
        if self._status is not None and not refresh:
            return self._status
        if self.backend is None:
            self._status = ProviderStatus(
                state=ProviderState.NOT_INSTALLED,
                reason=(f"This backend could not be loaded: {self.import_error}"
                        if self.import_error else
                        "This backend is not available."),
                instructions=["Reinstall the application, or use another backend."])
            return self._status
        try:
            self._status = self.backend.status()
        except Exception as exc:  # noqa: BLE001 - a broken backend is reported
            self._status = ProviderStatus(
                state=ProviderState.NOT_VERIFIED,
                reason=f"This backend could not be checked: {exc}",
                instructions=["Check the log, or use another backend."])
        return self._status

    def capabilities(self) -> AICapabilities:
        if self.backend is None:
            return AICapabilities(notes="This backend is not available.")
        try:
            return self.backend.capabilities()
        except Exception as exc:  # noqa: BLE001
            return AICapabilities(notes=f"Capabilities unavailable: {exc}")

    def models(self) -> list:
        if self.backend is None:
            return []
        try:
            status = self.status()
        except Exception:  # noqa: BLE001
            return []
        if not is_reachable(str(getattr(status, "state", ""))):
            # A backend that is not installed has no models to offer; reporting
            # its weights would look like a working model (section 93).
            return []
        try:
            return list(self.backend.models())
        except Exception as exc:  # noqa: BLE001
            log_event("AI_MODEL_LIST_FAILED",
                      f"'{self.id}' could not list its models", error=str(exc))
            return []

    def available(self) -> bool:
        return is_reachable(str(self.status().state))

    def to_dict(self, *, deep: bool = False) -> dict:
        if self.backend is None:
            status = self.status()
            return {"id": self.id, "name": self.id, "kind": "",
                    "transport": "", "type_label": "", "version": "",
                    "state": str(status.state), "available": False,
                    "reason": str(status.reason),
                    "instructions": list(status.instructions),
                    "location": "", "describe": "", "is_model": False,
                    "device_requirement": "", "device": "", "uses_network": False,
                    "licence": "", "homepage": "", "features": [],
                    "capabilities": {}, "models": {"count": 0, "models": []},
                    "settings": [], "enabled": True}
        return self.backend.to_dict(deep=deep)


class DiscoveryReport:
    """What a detection pass found, ready to be shown or logged."""

    def __init__(self) -> None:
        self.entries: list[BackendEntry] = []
        self.errors: list[str] = []
        self.seconds: float = 0.0
        self.device: dict = {}

    @property
    def usable(self) -> list[BackendEntry]:
        return [entry for entry in self.entries if entry.available()]

    @property
    def by_kind(self) -> dict[str, list[BackendEntry]]:
        grouped: dict[str, list[BackendEntry]] = {}
        for entry in self.entries:
            grouped.setdefault(entry.kind, []).append(entry)
        return grouped

    def generator_note(self) -> str:
        """One line about what this machine can actually generate with."""
        usable = self.usable
        real = [entry for entry in usable
                if bool(getattr(entry.backend, "is_model", True))]
        if real:
            return ("Ready to generate with: " +
                    ", ".join(entry.name for entry in real[:3]) + ".")
        if usable:
            return ("No AI model is installed. Only test backends are available; "
                    "their output is not AI generation.")
        return ("No AI model is installed, and no test backend is available "
                "either. Install a backend to generate.")

    def describe(self) -> str:
        lines = [self.generator_note()]
        for entry in sorted(self.entries, key=lambda item: STATE_ORDER.get(
                str(item.status().state), 9)):
            status = entry.status()
            lines.append(f"- {entry.name}: {status.state} - {status.reason}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "seconds": round(self.seconds, 3),
            "device": dict(self.device),
            "count": len(self.entries),
            "usable_count": len(self.usable),
            "usable": [entry.id for entry in self.usable],
            "note": self.generator_note(),
            "errors": list(self.errors),
            "backends": [entry.to_dict() for entry in self.entries],
        }


class AIBackendManager:
    """Builds, describes and configures every AI backend on this machine."""

    def __init__(self, settings: Optional[Settings] = None, *,
                 tools: Any = None, data_root: Any = None,
                 device: Any = None,
                 extra_backends: Iterable[AIBackend] = ()) -> None:
        self.settings = settings
        self.tools = tools
        self.data_root = Path(data_root) if data_root else None
        self.device = device
        self.extra_backends = list(extra_backends)
        self.entries: dict[str, BackendEntry] = {}
        self.cache = ModelCache(keep_loaded=self._ai_values().get("keep_models_loaded", True))
        self.store = ModelStore(self.model_roots())
        self._report: Optional[DiscoveryReport] = None
        self._built = False

    # -- settings ----------------------------------------------------------

    def _ai_values(self) -> dict:
        """The ``ai`` section of the settings as a plain dict."""
        if self.settings is None:
            return {}
        try:
            section = self.settings.get("ai", {}) \
                if hasattr(self.settings, "get") else getattr(self.settings, "ai", None)
        except Exception:  # noqa: BLE001 - settings are never worth crashing for
            return {}
        if section is None:
            return {}
        if isinstance(section, dict):
            return dict(section)
        try:
            from dataclasses import asdict, is_dataclass

            if is_dataclass(section):
                return dict(asdict(section))
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _image_values(self) -> dict:
        """The Stage F ``image`` section, so one endpoint is not configured twice."""
        if self.settings is None:
            return {}
        try:
            section = self.settings.get("image", {}) \
                if hasattr(self.settings, "get") else getattr(self.settings, "image", None)
        except Exception:  # noqa: BLE001
            return {}
        if section is None:
            return {}
        if isinstance(section, dict):
            return dict(section)
        try:
            from dataclasses import asdict, is_dataclass

            if is_dataclass(section):
                return dict(asdict(section))
        except Exception:  # noqa: BLE001
            pass
        return {}

    def model_roots(self) -> list[Path]:
        """Where models are looked for: the app's folder plus the user's choices."""
        return [Path(item) for item in self._ai_values().get("model_folders", [])
                if str(item).strip()] + ModelStore.default_roots()

    def is_enabled(self, backend_id: str) -> bool:
        disabled = [str(item) for item in self._ai_values().get("disabled_backends", [])]
        return str(backend_id) not in disabled

    def backend_settings(self, backend_id: str) -> dict:
        values = self._ai_values().get("backend_settings") or {}
        section = values.get(str(backend_id)) or {}
        return dict(section) if isinstance(section, dict) else {}

    def configured_value(self, backend_id: str, name: str, default: Any = "") -> Any:
        """A setting for one backend: what the user typed, or the default."""
        values = self.backend_settings(backend_id)
        if name in values and values[name] not in (None, ""):
            return values[name]
        for field_spec in self._schema_for(backend_id):
            if field_spec.name == name and field_spec.default not in (None, ""):
                return field_spec.default
        return default

    def _schema_for(self, backend_id: str) -> list:
        entry = self.entries.get(backend_id)
        if entry is None or entry.backend is None:
            return []
        try:
            return list(entry.backend.settings_schema())
        except Exception:  # noqa: BLE001
            return []

    # -- construction ------------------------------------------------------

    def build(self) -> dict[str, BackendEntry]:
        """Construct every backend.  Cheap: nothing is loaded or imported."""
        if self._built:
            return self.entries
        self._built = True
        ai = self._ai_values()
        image = self._image_values()

        model_paths: list[Path] = []
        for root in self.model_roots():
            if Path(root).is_dir():
                model_paths.extend(item.path for item
                                   in self.store.weight_files(Path(root)))
        image_factories = {
            "standard": lambda: _image_provider("standard"),
            "command": lambda: _image_provider("command", image.get("command", "")),
            "http": lambda: _image_provider("http", image.get("endpoint", "")),
            "comfyui": lambda: _image_provider(
                "comfyui", image.get("comfyui_endpoint", ""),
                image.get("comfyui_workflow", "")),
            "diffusers": lambda: _image_provider("diffusers", model_paths),
            "onnx": lambda: _image_provider("onnx", model_paths),
        }
        for backend_id, factory in image_factories.items():
            try:
                provider = factory()
            except Exception as exc:  # noqa: BLE001 - one bad adapter is not fatal
                self.entries[backend_id] = BackendEntry(backend_id=backend_id,
                                                        import_error=str(exc))
                log_event("AI_BACKEND_BUILD_FAILED",
                          f"'{backend_id}' could not be constructed",
                          backend=backend_id, error=str(exc))
                continue
            self.entries[backend_id] = BackendEntry(
                backend=ImageBackendAdapter(provider), backend_id=backend_id)

        values = {
            "standard_video": {},
            "command_video": {"command": ai.get("video_command", "")},
            "http_video": {"endpoint": ai.get("video_endpoint", "")},
            "comfyui_video": {"endpoint": image.get("comfyui_endpoint", "") or
                              ai.get("video_endpoint", ""),
                              "workflow": ai.get("comfyui_video_workflow", "")},
            "diffusers_video": {"model_path": ai.get("diffusers_video_model", "")},
            "python_video": {"module_path": ai.get("python_video_module", "")},
        }
        for backend_id in VIDEO_BACKEND_ORDER:
            factory = VIDEO_BACKEND_CLASSES.get(backend_id)
            if factory is None:
                continue
            try:
                backend = factory(tools=self.tools, **values.get(backend_id, {}))
            except Exception as exc:  # noqa: BLE001
                self.entries[backend_id] = BackendEntry(backend_id=backend_id,
                                                        import_error=str(exc))
                log_event("AI_BACKEND_BUILD_FAILED",
                          f"'{backend_id}' could not be constructed",
                          backend=backend_id, error=str(exc))
                continue
            self.entries[backend_id] = BackendEntry(backend=backend,
                                                    backend_id=backend_id)

        for backend in self.extra_backends:
            self.entries[backend.id] = BackendEntry(backend=backend,
                                                    backend_id=backend.id)
        self.entries["cloud"] = BackendEntry(backend=CloudBackendNotSupported(),
                                             backend_id="cloud")
        return self.entries

    def refresh(self, *, rebuild: bool = False) -> None:
        """Clear cached statuses, and optionally rebuild the backends."""
        if rebuild:
            self._built = False
            self.entries = {}
            self.build()
            return
        for entry in self.entries.values():
            entry.status(refresh=True)

    # -- discovery ---------------------------------------------------------

    def detect(self, *, refresh: bool = True) -> DiscoveryReport:
        import time

        self.build()
        started = time.monotonic()
        report = DiscoveryReport()
        if refresh:
            self.refresh()
        for entry in self.entries.values():
            report.entries.append(entry)
            status = entry.status()
            if not is_reachable(str(status.state)) and entry.import_error:
                report.errors.append(f"{entry.id}: {entry.import_error}")
        report.entries.sort(key=lambda item: (STATE_ORDER.get(
            str(item.status().state), 9), item.id))
        if self.device is not None:
            to_dict = getattr(self.device, "to_dict", None)
            report.device = dict(to_dict() if callable(to_dict) else {})
        report.seconds = time.monotonic() - started
        self._report = report
        log_event("AI_BACKENDS_DETECTED",
                  f"{len(report.usable)} of {len(report.entries)} backends usable",
                  seconds=round(report.seconds, 3))
        return report

    def report(self) -> Optional[DiscoveryReport]:
        return self._report

    # -- access -----------------------------------------------------------

    def get(self, backend_id: str) -> Optional[BackendEntry]:
        self.build()
        return self.entries.get(str(backend_id))

    def backend(self, backend_id: str) -> Optional[AIBackend]:
        entry = self.get(backend_id)
        return None if entry is None else entry.backend

    def enabled_entries(self, *, kind: str = "") -> list[BackendEntry]:
        self.build()
        return [entry for entry in self.entries.values()
                if self.is_enabled(entry.id)
                and (not kind or entry.kind == kind)]

    def usable_backends(self, *, kind: str = "") -> list[BackendEntry]:
        """Backends that could be asked to work, in a stable order."""
        return sorted((entry for entry in self.enabled_entries(kind=kind)
                       if entry.available()),
                      key=lambda item: (STATE_ORDER.get(
                          str(item.status().state), 9), item.id))

    def real_backends(self, *, kind: str = "") -> list[BackendEntry]:
        """Backends that run a real model - the test fixture is excluded.

        This is what a "do I have an AI model installed?" question must use, so
        the answer cannot be rounded up by a fixture (section 79).
        """
        return [entry for entry in self.usable_backends(kind=kind)
                if bool(getattr(entry.backend, "is_model", True))]

    def default_backend(self, kind: str = BackendKind.IMAGE) -> Optional[str]:
        """The backend a new generation should start on."""
        ai = self._ai_values()
        preferred = str(ai.get("video_backend") if kind == BackendKind.VIDEO
                        else self._image_values().get("active_backend", "") or "")
        if preferred:
            entry = self.get(preferred)
            if entry is not None and entry.available() and \
                    (not kind or entry.kind == kind):
                return preferred
        usable = self.usable_backends(kind=kind)
        real = [entry for entry in usable
                if bool(getattr(entry.backend, "is_model", True))]
        if real:
            return real[0].id
        return usable[0].id if usable else ""

    def default_model(self, backend_id: str) -> str:
        """The model a backend should start on: the user's choice, else the first."""
        ai = self._ai_values()
        preferred = str(ai.get("video_model") or
                        self._image_values().get("active_model") or "")
        models = self.models_for(backend_id)
        if preferred:
            for model in models:
                if str(getattr(model, "id", "")) == preferred:
                    return preferred
        return str(getattr(models[0], "id", "")) if models else ""

    # -- models -----------------------------------------------------------

    def models_for(self, backend_id: str) -> list:
        entry = self.get(backend_id)
        return [] if entry is None else entry.models()

    def find_model(self, backend_id: str, model_id: str) -> Any:
        for model in self.models_for(backend_id):
            if str(getattr(model, "id", "")) == str(model_id):
                return model
        return None

    def all_models(self, *, kind: str = "") -> list[dict]:
        """Every model every usable backend offers, described for the manager."""
        described: list[dict] = []
        for entry in self.usable_backends(kind=kind):
            for model in entry.models():
                described.append({
                    "id": str(getattr(model, "id", "")),
                    "name": str(getattr(model, "name", "") or
                                getattr(model, "id", "")),
                    "backend": entry.id, "backend_name": entry.name,
                    "kind": entry.kind,
                    "size_bytes": int(getattr(model, "size_bytes", 0) or 0),
                    "size": human_bytes(int(getattr(model, "size_bytes", 0) or 0)),
                    "path": str(getattr(model, "path", "") or ""),
                    "status": str(getattr(model, "status", "") or ""),
                    "requirement": str(getattr(model, "requirement", "") or ""),
                    "is_ai_model": bool(getattr(entry.backend, "is_model", True)),
                    "notes": str(getattr(model, "notes", "") or ""),
                })
        return described

    def discovered_models(self, *, kind: str = "") -> list[dict]:
        """Models found on disk in the configured folders (nothing loaded)."""
        self.store = ModelStore(self.model_roots())
        return self.store.discover(kind=kind)

    def estimate(self, backend_id: str, model_id: str, *,
                 dtype: str = "float32") -> Any:
        model = self.find_model(backend_id, model_id)
        if model is None:
            return None
        from ..image.device import detect_device

        device = self.device or detect_device()
        return self.store.estimate_memory(
            model, dtype=dtype,
            available_bytes=int(float(getattr(device, "ram_gb", 0.0)) * 1024 ** 3))

    def ensure_loaded(self, backend_id: str, model_id: str = "") -> Any:
        """Load the model this backend needs, reusing it when it is the same."""
        entry = self.get(backend_id)
        if entry is None or entry.backend is None:
            raise LookupError(f"No backend called '{backend_id}' is available.")
        model = self.find_model(backend_id, model_id) if model_id else None
        if model is None and entry.models():
            model = entry.models()[0]
        return self.cache.ensure(entry.backend, model or _ModelRef(model_id or "default"))

    def unload(self, *, backend_id: str = "") -> None:
        if backend_id:
            self.cache.release_for(backend_id)
            entry = self.get(backend_id)
            if entry is not None and entry.backend is not None:
                try:
                    entry.backend.unload()
                except Exception as exc:  # noqa: BLE001
                    log_event("AI_MODEL_UNLOAD_FAILED",
                              f"'{backend_id}' could not unload cleanly",
                              level="WARNING", error=str(exc))
            return
        self.cache.unload(reason="requested")
        for entry in self.entries.values():
            if entry.backend is None:
                continue
            try:
                entry.backend.unload()
            except Exception as exc:  # noqa: BLE001
                log_event("AI_MODEL_UNLOAD_FAILED", f"'{entry.id}' could not "
                                                    f"unload cleanly",
                          level="WARNING", error=str(exc))

    def loaded(self) -> dict:
        return self.cache.describe()

    # -- configuration ----------------------------------------------------

    def set_enabled(self, backend_id: str, enabled: bool) -> list[str]:
        """Turn a backend on or off, returning the new disabled list."""
        disabled = [str(item) for item in self._ai_values().get("disabled_backends", [])]
        changed = False
        if enabled and backend_id in disabled:
            disabled = [item for item in disabled if item != backend_id]
            changed = True
        if not enabled and backend_id not in disabled:
            disabled.append(backend_id)
            changed = True
        if changed:
            self._write_section_key("disabled_backends", disabled)
            if not enabled:
                self.unload(backend_id=backend_id)
            log_event("AI_BACKEND_ENABLED" if enabled else "AI_BACKEND_DISABLED",
                      f"Backend '{backend_id}' was "
                      f"{'enabled' if enabled else 'disabled'}",
                      backend=backend_id)
        return disabled

    def configure(self, backend_id: str, values: dict) -> dict:
        """Save settings for one backend, validating against its schema.

        A value that does not fit the schema is refused with a reason rather
        than being stored and failing later (section 6).
        """
        stored = self.backend_settings(backend_id)
        schema = {field_spec.name: field_spec for field_spec in
                  self._schema_for(backend_id)}
        problems: list[str] = []
        for name, value in dict(values or {}).items():
            spec = schema.get(str(name))
            if spec is None and schema:
                # A name the backend never declared is refused rather than
                # stored: a typo that quietly saved would never take effect and
                # would never be reported either (section 6).
                problems.append(f"'{name}' is not a setting of this backend, so "
                                f"it was not saved.")
                continue
            if spec is not None:
                value = _coerce_setting(spec, value)
                ok, problem = spec.validate(value)
                if not ok:
                    problems.append(problem)
                    continue
            stored[str(name)] = value
        if problems:
            return {"ok": False, "problems": problems, "settings": stored}
        sections = dict(self._ai_values().get("backend_settings") or {})
        sections[str(backend_id)] = stored
        self._write_section_key("backend_settings", sections)
        log_event("AI_BACKEND_CONFIGURED",
                  f"Backend '{backend_id}' settings were saved",
                  backend=backend_id, keys=",".join(sorted(stored)))
        return {"ok": True, "problems": [], "settings": stored}

    def settings_values(self, backend_id: str) -> dict:
        """The settings one backend would be configured with, defaults included.

        The configuration form starts from this, so it shows the value that is
        really in force rather than an empty box (section 6).
        """
        values = {}
        for field_spec in self._schema_for(backend_id):
            values[field_spec.name] = self.configured_value(
                backend_id, field_spec.name, field_spec.default)
        return values

    def _write_section_key(self, name: str, value: Any) -> None:
        """Write one key back into the ``ai`` settings section.

        A manager with no writable settings (a test, or the CLI in read-only
        mode) still reports the change rather than silently dropping it.
        """
        if self.settings is None:
            return
        section = getattr(self.settings, "ai", None)
        if section is None:
            return
        try:
            setattr(section, name, value)
        except Exception as exc:  # noqa: BLE001
            log_event("AI_SETTINGS_WRITE_FAILED",
                      f"'{name}' could not be saved in the settings",
                      level="WARNING", error=str(exc))

    # -- description -------------------------------------------------------

    def summary(self) -> dict:
        """Counts and the headline, for the manager's header."""
        self.build()
        counts: dict[str, int] = {}
        usable_real = 0
        for entry in self.entries.values():
            status = entry.status()
            state = str(status.state)
            counts[state] = counts.get(state, 0) + 1
            if is_reachable(state) and bool(getattr(entry.backend, "is_model", True)):
                usable_real += 1
        return {"total": len(self.entries), "by_state": counts,
                "usable_real_models": usable_real,
                "test_backends": sum(1 for entry in self.entries.values()
                                     if not bool(getattr(entry.backend, "is_model", True))),
                "note": (self._report.generator_note() if self._report else ""),
                "loaded": self.cache.describe()}

    def describe(self) -> str:
        lines = [f"AI backends ({len(self.entries)})"]
        for entry in sorted(self.entries.values(),
                            key=lambda item: (STATE_ORDER.get(
                                str(item.status().state), 9), item.id)):
            status = entry.status()
            kind = KIND_LABELS.get(entry.kind, entry.kind)
            lines.append(f"- {entry.name} [{kind}, "
                         f"{backend_type_label(str(getattr(entry.backend, 'transport', '')))}]: "
                         f"{status.state} - {status.reason}")
        loaded = self.cache.describe()
        if loaded.get("loaded"):
            lines.append(f"Loaded: {loaded['loaded']} "
                         f"({loaded['seconds_loaded']:.0f}s)")
        return "\n".join(lines)

    def settings_examples(self) -> list:
        """Every setting field across every backend, for the settings page."""
        seen: list[SettingField] = []
        for entry in self.entries.values():
            if entry.backend is None:
                continue
            for field_spec in self._schema_for(entry.id):
                seen.append(SettingField(
                    name=f"{entry.id}.{field_spec.name}",
                    label=f"{entry.name}: {field_spec.label}",
                    kind=field_spec.kind, default=field_spec.default,
                    help=field_spec.help, required=field_spec.required,
                    choices=list(field_spec.choices), advanced=field_spec.advanced))
        return seen


class _ModelRef:
    """A stand-in model for a backend that has no model list of its own."""

    def __init__(self, model_id: str) -> None:
        self.id = model_id
        self.name = model_id
        self.path = ""
        self.size_bytes = 0
        self.requirement = "UNKNOWN"
        self.status = "available"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ModelRef {self.id}>"


def _coerce_setting(spec: Any, value: Any) -> Any:
    """Turn a typed-in value into the type the schema asked for.

    A form gives text; a backend wants an int, a float or a flag.  A value that
    cannot be converted is left alone so the schema's own validation reports it,
    rather than being quietly replaced with a default.
    """
    kind = str(getattr(spec, "kind", "text"))
    try:
        if kind == "int" and not isinstance(value, bool):
            return int(str(value).strip())
        if kind == "float":
            return float(str(value).strip())
        if kind == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
    except (TypeError, ValueError):
        return value
    return value


def _image_provider(backend_id: str, *args: Any) -> Any:
    """Build a Stage F image provider by id, without importing them all."""
    from ..image.backends import BACKEND_CLASSES

    factory = BACKEND_CLASSES.get(backend_id)
    if factory is None:
        raise LookupError(f"No Stage F backend called '{backend_id}' exists.")
    return factory(*[item for item in args if item != ""])


def manager_for(context: Any = None, *, settings: Any = None,
                tools: Any = None, data_root: Any = None) -> AIBackendManager:
    """A manager built from whatever the caller has to hand.

    The GUI passes its application context; the CLI passes the settings and
    tools it loaded.  Both end up with backends configured exactly the same
    way, which is the point of having one manager (section 61).
    """
    settings = settings if settings is not None else getattr(context, "settings", None)
    tools = tools if tools is not None else getattr(context, "tools", None)
    data_root = data_root if data_root is not None else getattr(context, "data_root", None)
    device = getattr(context, "device", None)
    return AIBackendManager(settings, tools=tools, data_root=data_root,
                            device=device)
