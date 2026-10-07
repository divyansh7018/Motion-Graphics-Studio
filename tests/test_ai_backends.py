"""Stage G tests: the backend contract, the manager and the model store.

Sections 1-8, 11, 61, 78-81, 92-93, 97-98.  These run with no AI model
installed, which is the point: the studio must describe every backend honestly,
offer the ones that work, and say NOT INSTALLED - in those words - for the rest.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

from app.ai.backend import (BACKEND_TYPES, AIBackend, ImageBackendAdapter,
                            CloudBackendNotSupported, backend_type_label,
                            describe_provider, normalise_state)
from app.ai.capabilities import AICapabilities
from app.ai.provider import describe_capabilities
from app.ai.models import ModelStore, human_bytes
from app.ai.registry import AIBackendManager
from app.ai.types import (BackendKind, DeviceRequirement, ProviderState,
                          is_reachable, state_label)

from tests.ai_fakes import FakeImageProvider, FakeModel, FakeVideoBackend, write_weights


# --------------------------------------------------------------------------
# The vocabulary (sections 1, 4, 93)
# --------------------------------------------------------------------------

def test_the_only_states_are_the_ones_the_rules_allow() -> None:
    """PASS is deliberately absent: nothing is ever "passing"."""
    from app.ai.types import PROVIDER_STATES, STATE_MEANINGS

    assert "PASS" not in PROVIDER_STATES
    assert set(PROVIDER_STATES) == set(STATE_MEANINGS)
    for word in (ProviderState.NOT_INSTALLED, ProviderState.NOT_VERIFIED,
                 ProviderState.NOT_SUPPORTED, ProviderState.CHECK_NOT_AVAILABLE,
                 ProviderState.LIMITED, ProviderState.VERIFIED):
        assert word in PROVIDER_STATES


def test_states_are_readable_without_changing_what_they_say() -> None:
    assert state_label("NOT_INSTALLED") == "NOT INSTALLED"
    assert state_label("CHECK NOT AVAILABLE") == "CHECK NOT AVAILABLE"
    assert state_label("VERIFIED") == "VERIFIED"


def test_only_reachable_states_count_as_usable() -> None:
    assert is_reachable(ProviderState.AVAILABLE)
    assert is_reachable(ProviderState.VERIFIED)
    assert is_reachable(ProviderState.LIMITED)
    for state in (ProviderState.NOT_INSTALLED, ProviderState.NOT_VERIFIED,
                  ProviderState.NOT_SUPPORTED, ProviderState.CHECK_NOT_AVAILABLE):
        assert not is_reachable(state)


def test_every_backend_type_in_the_architecture_is_named() -> None:
    for expected in ("python", "diffusers", "comfyui", "http", "command", "onnx",
                     "cloud", "builtin"):
        assert expected in BACKEND_TYPES
        assert backend_type_label(expected)


def test_capabilities_default_to_no_and_say_why_a_control_is_off() -> None:
    empty = AICapabilities()
    assert empty.features() == []
    assert not empty.supports("text_to_video")
    assert "not available" in empty.unsupported_note("text_to_video")
    full = AICapabilities(text_to_video=True, image_to_video=True)
    assert full.supports("text_to_video")
    assert "Text to video" in describe_capabilities(full)


# --------------------------------------------------------------------------
# The abstract contract (sections 2, 6, 61)
# --------------------------------------------------------------------------

def test_the_contract_answers_everything_the_manager_asks() -> None:
    """id, name, type, version, capabilities, status, device, models, settings."""
    backend = FakeVideoBackend()
    payload = describe_provider(backend)
    for key in ("id", "name", "kind", "transport", "type_label", "version",
                "state", "available", "reason", "instructions", "location",
                "describe", "is_model", "device_requirement", "device",
                "uses_network", "licence", "homepage", "features",
                "capabilities", "models", "settings", "enabled"):
        assert key in payload, key
    assert payload["kind"] == BackendKind.VIDEO
    assert payload["is_model"] is True


def test_a_video_backend_and_an_image_backend_describe_themselves_alike() -> None:
    """Both provider shapes produce the same payload: one shape to show."""
    video = describe_provider(FakeVideoBackend())
    image = describe_provider(ImageBackendAdapter(FakeImageProvider()))
    assert set(video) == set(image)
    assert image["kind"] == BackendKind.IMAGE
    assert image["id"] == "fake_image"


def test_a_broken_backend_is_reported_not_raised() -> None:
    class Exploding(AIBackend):
        id = "exploding"
        name = "Exploding backend"

        def status(self):  # pragma: no cover - the point is that it raises
            raise RuntimeError("no status for you")

        def capabilities(self):
            raise RuntimeError("no capabilities either")

    payload = describe_provider(Exploding())
    assert payload["state"] == ProviderState.NOT_VERIFIED
    assert "no status for you" in payload["reason"]
    assert payload["features"] == []


def test_a_backend_that_has_not_implemented_generation_refuses() -> None:
    class Nothing(AIBackend):
        id = "nothing"
        name = "Nothing"

    with pytest.raises(NotImplementedError):
        Nothing().generate(object())


def test_the_cloud_adapter_is_described_but_not_supported() -> None:
    cloud = CloudBackendNotSupported()
    status = cloud.status()
    assert status.state == ProviderState.NOT_SUPPORTED
    assert not cloud.available()
    assert "local" in status.reason.lower()


def test_an_unknown_state_is_never_upgraded() -> None:
    assert normalise_state("something odd") in (ProviderState.NOT_VERIFIED,
                                                "something odd")
    assert not is_reachable(normalise_state("something odd"))


# --------------------------------------------------------------------------
# The Stage F adapter (section 61)
# --------------------------------------------------------------------------

def test_a_provider_that_claims_to_be_a_model_is_believed() -> None:
    adapter = ImageBackendAdapter(FakeImageProvider())
    assert adapter.is_model is True
    assert adapter.to_dict()["is_model"] is True


def test_a_builtin_provider_that_never_claims_is_not_a_model() -> None:
    """Stage F's deterministic editor writes a real image and is not AI."""

    class NoClaim:
        id = "standard"
        label = "Standard (no AI model)"
        kind = "builtin"

    adapter = ImageBackendAdapter(NoClaim())
    assert adapter.transport == "builtin"
    assert adapter.is_model is False
    assert adapter.to_dict()["is_model"] is False


def test_a_python_provider_that_never_claims_is_not_called_unverified_ai() -> None:
    class NoClaim:
        id = "my-python-backend"
        label = "My backend"
        kind = "python"

    adapter = ImageBackendAdapter(NoClaim())
    assert adapter.transport == "python"
    assert adapter.is_model is True
    assert adapter.status().state == ProviderState.NOT_VERIFIED


def test_the_adapter_renames_rather_than_reinterprets() -> None:
    provider = FakeImageProvider(state=ProviderState.NOT_INSTALLED,
                                 reason="not installed on this machine")
    adapter = ImageBackendAdapter(provider)
    status = adapter.status()
    assert status.state == ProviderState.NOT_INSTALLED
    assert "not installed" in status.reason
    assert adapter.name == provider.label


# --------------------------------------------------------------------------
# The manager (sections 5, 6, 11, 92)
# --------------------------------------------------------------------------

@pytest.fixture()
def manager(settings, tmp_path, paths):
    return AIBackendManager(settings, data_root=tmp_path,
                            extra_backends=[FakeVideoBackend()])


def test_detection_finds_every_backend_without_loading_one(manager) -> None:
    report = manager.detect()
    assert report.entries
    assert "fake_video" in [entry.id for entry in report.entries]
    assert report.seconds >= 0.0
    assert report.to_dict()["count"] == len(report.entries)


def test_detection_never_imports_a_model_library(manager) -> None:
    """A detection pass must not pay for torch or diffusers."""

    before = set(sys.modules)
    manager.detect()
    for name in ("torch", "diffusers", "onnxruntime", "transformers"):
        if name not in before:
            assert name not in sys.modules, f"detection imported {name}"


def test_the_shape_of_the_payload_the_gui_shows(manager) -> None:
    report = manager.detect()
    described = report.to_dict()
    assert "backends" in described
    for item in described["backends"]:
        assert {"id", "name", "state", "available", "reason"} <= set(item)


def test_a_fake_backend_is_usable_and_a_missing_one_is_not(manager) -> None:
    manager.detect()
    usable = [entry.id for entry in manager.usable_backends()]
    assert "fake_video" in usable
    missing = AIBackendManager(None, extra_backends=[
        FakeVideoBackend("no_model", state=ProviderState.NOT_INSTALLED)])
    missing.detect()
    assert "no_model" not in [entry.id for entry in missing.usable_backends()]
    assert missing.get("no_model").status().state == ProviderState.NOT_INSTALLED
    assert not missing.get("no_model").available()


def test_models_are_only_offered_by_backends_that_can_run_them(manager) -> None:
    manager.detect()
    model = FakeModel("weights-a", size_bytes=2048)
    manager.get("fake_video").backend._models = [model]  # noqa: SLF001
    listed = {item["id"]: item for item in manager.all_models()}
    assert "weights-a" in listed
    assert listed["weights-a"]["is_ai_model"] is True
    not_installed = AIBackendManager(None, extra_backends=[
        FakeVideoBackend("absent", state=ProviderState.NOT_INSTALLED,
                         models=[model])])
    not_installed.detect()
    assert "weights-a" not in {item["id"] for item in not_installed.all_models()}


def test_a_model_is_never_loaded_to_answer_a_question(manager) -> None:
    manager.detect()
    backend = manager.get("fake_video").backend
    backend.loaded_with = None
    assert manager.find_model("fake_video", "whatever") is None
    assert backend.loaded_with is None
    assert manager.loaded()["loaded"] == ""


def test_disabling_a_backend_hides_it_without_deleting_it(manager) -> None:
    manager.detect()
    manager.set_enabled("fake_video", False)
    assert "fake_video" not in [entry.id for entry in manager.enabled_entries()]
    assert manager.get("fake_video") is not None
    assert "fake_video" not in [entry.id for entry in manager.usable_backends()]
    manager.set_enabled("fake_video", True)
    assert "fake_video" in [entry.id for entry in manager.usable_backends()]


def test_the_real_backends_exclude_the_test_fixture(paths) -> None:
    """A fixture backend and a model backend are counted apart (section 93)."""
    manager = AIBackendManager(None, extra_backends=[
        FakeVideoBackend("test_fixture", is_model=False,
                         name="TEST BACKEND (fixture - not an AI model)")])
    manager.detect()
    assert "test_fixture" not in [entry.id for entry in manager.real_backends()]
    summary = manager.summary()
    assert summary["usable_real_models"] == 0
    assert summary["test_backends"] >= 1
    assert "not AI generation" in manager.detect().generator_note()


def test_a_real_model_is_counted_as_one_when_it_really_answers(manager) -> None:
    """A backend that runs a model, verified here, is reported as one."""
    manager.detect()
    manager.get("fake_video").backend._state = ProviderState.VERIFIED  # noqa: SLF001
    assert manager.summary()["usable_real_models"] >= 1
    assert "Fake video backend" in manager.detect().generator_note()


def test_the_note_says_no_model_is_installed_when_none_is(settings) -> None:
    """With only fixtures installed, the sentence is the honest one."""
    manager = AIBackendManager(settings, extra_backends=[
        FakeVideoBackend("test_fixture", is_model=False,
                         name="TEST BACKEND (fixture - not an AI model)")])
    note = manager.detect().generator_note()
    assert "No AI model is installed" in note
    assert "not AI generation" in note


def test_configure_stores_a_value_and_rejects_an_unknown_one(manager) -> None:
    from app.ai.provider import SettingField

    backend = manager.get("fake_video").backend
    backend._settings = [  # noqa: SLF001
        SettingField(name="endpoint", label="Endpoint", kind="text",
                     default="http://127.0.0.1:8188"),
        SettingField(name="timeout", label="Timeout", kind="int", default=600)]
    manager.detect()
    assert manager.configure("fake_video", {"endpoint": "http://127.0.0.1:9000"})
    assert manager.configured_value("fake_video", "endpoint") == \
        "http://127.0.0.1:9000"
    outcome = manager.configure("fake_video", {"nosuch": "x"})
    assert outcome["ok"] is False
    assert outcome["problems"]


def test_a_backend_that_is_not_there_reports_not_installed(manager) -> None:
    entry = manager.get("does-not-exist")
    assert entry is None
    assert manager.backend("does-not-exist") is None


# --------------------------------------------------------------------------
# Models on disk (sections 11, 42, 86, 92)
# --------------------------------------------------------------------------

def test_model_folders_are_read_without_loading_anything(tmp_path) -> None:
    root = tmp_path / "models"
    write_weights(root / "some-video-model", size_bytes=4096,
                  descriptor={"_class_name": "VideoPipeline"})
    store = ModelStore([root])
    found = store.discover()
    assert [item["id"] for item in found] == ["some-video-model"]
    assert found[0]["size_bytes"] == 4096
    assert found[0]["kind"] == "video"
    assert found[0]["requirement"] in tuple(DeviceRequirement.__dict__.values())


def test_a_descriptor_alone_does_not_make_a_model(tmp_path) -> None:
    """A folder with a config but no weights is not offered as a model."""
    folder = tmp_path / "models" / "empty-folder"
    folder.mkdir(parents=True)
    (folder / "model_index.json").write_text("{}", encoding="utf-8")
    store = ModelStore([tmp_path / "models"])
    assert store.discover() == []


def test_memory_is_estimated_from_real_sizes_and_refused_when_unknown(tmp_path) -> None:
    root = tmp_path / "models"
    write_weights(root / "small", size_bytes=10 * 1024 * 1024)
    store = ModelStore([root])
    entry = store.discover()[0]
    estimate = store.estimate_memory(entry, dtype="float16", available_bytes=8 << 30)
    assert estimate.known
    # float16 is half of float32, so the weights cost half of the file size.
    assert estimate.weights_bytes == 5 * 1024 * 1024
    assert estimate.peak_bytes > estimate.weights_bytes
    assert "GB" in estimate.describe() or "MB" in estimate.describe()
    unknown = store.estimate_memory({"size_bytes": 0, "files": []}, dtype="float32")
    assert not unknown.known
    assert unknown.weights_bytes == 0
    # It says it cannot say, rather than inventing a number.
    assert "not enough information" in unknown.describe().lower()


def test_human_bytes_is_readable() -> None:
    assert human_bytes(0) == ""
    assert "KB" in human_bytes(2048)
    assert "GB" in human_bytes(3 * 1024 ** 3)


# --------------------------------------------------------------------------
# Windows-first (section 64, 65)
# --------------------------------------------------------------------------

def test_the_ai_package_imports_no_model_library_at_import_time() -> None:
    """Importing the studio must not import torch, diffusers or onnxruntime."""
    source = inspect.getsource(__import__("app.ai", fromlist=["x"]))
    assert "import torch" not in source


def test_nothing_in_the_ai_layer_forks() -> None:
    """No os.fork / multiprocessing: the studio has to work on Windows."""
    import app.ai as package

    folder = Path(package.__file__).parent
    offenders = []
    for path in folder.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "os.fork" in text or "multiprocessing" in text or "fork(" in text:
            offenders.append(path.name)
    assert offenders == []


def test_every_ai_module_uses_pathlib_not_string_paths() -> None:
    import app.ai as package

    folder = Path(package.__file__).parent
    for path in folder.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "os.path.join" not in text, f"{path.name} builds paths by string"
        assert "shell=True" not in text, f"{path.name} uses a shell"
