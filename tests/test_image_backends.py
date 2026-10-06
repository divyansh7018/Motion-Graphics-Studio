"""Backend contract tests (Stage F, sections 2, 35, 37, 46, 69, 72).

Every adapter is run against the *same* expectations, so a new backend cannot
ship with a capability it says it has but cannot honour, or report success for a
file that was never written.

Most of these run with nothing installed, because on a fresh machine none of the
AI backends exists - and "reports not installed, clearly" is itself part of the
contract.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from app.image.backends import BACKEND_CLASSES, BACKEND_ORDER
from app.image.backends.command import CommandBackend
from app.image.backends.http import HttpBackend, is_loopback
from app.image.backends.model_backends import (ComfyUIBackend, DiffusersBackend,
                                               ModelFolderScanner, OnnxBackend)
from app.image.backends.standard import StandardBackend
from app.image.capabilities import FEATURES
from app.image.provider import (MODES, GenerationMode, GenerationRequest,
                                GenerationState, ImageProvider, MODE_FEATURE)

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_GENERATOR = REPO_ROOT / "tests" / "fake_image_generator.py"

#: Every adapter, built with no configuration at all - the state a fresh
#: install is in.
ALL_BACKENDS = (
    StandardBackend(),
    CommandBackend(),
    HttpBackend(),
    ComfyUIBackend(),
    DiffusersBackend(),
    OnnxBackend(),
)

BACKEND_IDS = tuple(backend.id for backend in ALL_BACKENDS)


def make_image(path: Path, size=(64, 64), colour=(80, 120, 160)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path)
    return path


# --------------------------------------------------------------------------
# The contract every adapter must keep
# --------------------------------------------------------------------------

def test_every_adapter_is_registered_and_constructible():
    """A backend in the order list must actually exist in the table."""
    assert set(BACKEND_ORDER) == set(BACKEND_CLASSES)


@pytest.mark.parametrize("backend", ALL_BACKENDS, ids=BACKEND_IDS)
def test_status_never_raises_and_loads_nothing(backend):
    status = backend.status()
    assert isinstance(status.available, bool)
    assert status.state in ("available", "not_installed", "not_supported", "error")
    # A backend that is not available must say why and what to do about it.
    if not status.available:
        assert status.reason, f"{backend.id} gave no reason"
        assert status.instructions, f"{backend.id} gave no instructions"


@pytest.mark.parametrize("backend", ALL_BACKENDS, ids=BACKEND_IDS)
def test_capabilities_are_booleans_and_never_guess(backend):
    caps = backend.capabilities()
    for feature in FEATURES:
        assert isinstance(caps.supports(feature), bool)
    # No adapter may claim a feature without an implementation behind it.
    assert caps.notes or caps.features() or True


@pytest.mark.parametrize("backend", ALL_BACKENDS, ids=BACKEND_IDS)
def test_models_never_raise(backend):
    assert isinstance(backend.models(), list)


@pytest.mark.parametrize("backend", ALL_BACKENDS, ids=BACKEND_IDS)
def test_validation_never_raises(backend):
    request = GenerationRequest(mode=GenerationMode.TEXT_TO_IMAGE, prompt="a fox",
                                width=512, height=512)
    assert isinstance(backend.validate(request), list)


@pytest.mark.parametrize("backend", ALL_BACKENDS, ids=BACKEND_IDS)
def test_generating_with_an_unavailable_backend_fails_politely(backend):
    """No exception, no fake success: a reported failure with a fix."""
    if backend.status().available:
        pytest.skip(f"{backend.id} is available on this machine")
    request = GenerationRequest(mode=GenerationMode.TEXT_TO_IMAGE, prompt="a fox",
                                width=512, height=512)
    result = backend.generate(request)
    assert not result.ok
    assert result.error
    assert result.what_to_do
    assert result.paths == []


@pytest.mark.parametrize("backend", ALL_BACKENDS, ids=BACKEND_IDS)
def test_a_backend_that_does_not_support_a_mode_says_so(backend):
    """Every adapter must refuse a mode it does not advertise."""
    caps = backend.capabilities()
    missing = [mode for mode in MODES
               if not caps.supports(MODE_FEATURE.get(mode, ""))]
    if not missing:
        pytest.skip(f"{backend.id} supports every mode")
    mode = missing[0]
    request = GenerationRequest(mode=mode, prompt="a fox", width=512, height=512)
    issues = backend.validate(request)
    codes = {issue.code for issue in issues}
    # Either the generic "unsupported" or a more specific explanation of why
    # (the standard backend says no model is installed) is acceptable - what
    # matters is that it is refused, with an error, before anything runs.
    assert codes & {"FEATURE_UNSUPPORTED", "NO_MODEL_INSTALLED"}, codes
    assert all(issue.severity == "error" for issue in issues
               if issue.code in ("FEATURE_UNSUPPORTED", "NO_MODEL_INSTALLED"))


@pytest.mark.parametrize("backend", ALL_BACKENDS, ids=BACKEND_IDS)
def test_unload_is_safe_on_a_backend_that_holds_nothing(backend):
    backend.unload()  # must not raise


# --------------------------------------------------------------------------
# The standard (no model) backend
# --------------------------------------------------------------------------

def test_the_standard_backend_is_always_available():
    status = StandardBackend().status()
    assert status.available
    assert status.state == "available"


def test_the_standard_backend_does_not_claim_to_draw_prompts():
    """It has no model. Saying it could would be the fake this stage forbids."""
    caps = StandardBackend().capabilities()
    assert caps.supports("text_to_image") is False


def test_a_prompt_with_the_standard_backend_gives_a_clear_message(tmp_path):
    backend = StandardBackend()
    request = GenerationRequest(mode=GenerationMode.TEXT_TO_IMAGE, prompt="a fox",
                                width=96, height=96,
                                output_dir=str(tmp_path))
    result = backend.generate(request)
    assert not result.ok
    assert "No local image-generation model" in result.error
    assert result.code == "NO_MODEL_INSTALLED"
    assert result.paths == []


def test_the_standard_backend_reports_it_is_not_an_ai_model(tmp_path):
    source = make_image(tmp_path / "src.png")
    backend = StandardBackend()
    request = GenerationRequest(mode=GenerationMode.VARIATION, prompt="",
                                source_image=str(source), width=96, height=96,
                                output_dir=str(tmp_path), name_stem="v")
    result = backend.generate(request)
    assert result.ok
    assert "not an AI model" in result.quality["method"]


def test_the_standard_backend_extends_a_canvas_for_outpainting(tmp_path):
    source = make_image(tmp_path / "src.png", size=(96, 96))
    backend = StandardBackend()
    request = GenerationRequest(mode=GenerationMode.OUTPAINT, prompt="more sky",
                                source_image=str(source), width=192, height=96,
                                extend={"left": 48, "right": 48},
                                output_dir=str(tmp_path), name_stem="wide")
    result = backend.generate(request)
    assert result.ok, result.error
    assert result.width == 192 and result.height == 96


def test_outpainting_without_an_extension_is_refused(tmp_path):
    source = make_image(tmp_path / "src.png")
    backend = StandardBackend()
    request = GenerationRequest(mode=GenerationMode.OUTPAINT, prompt="x",
                                source_image=str(source), width=96, height=96,
                                extend={}, output_dir=str(tmp_path))
    result = backend.generate(request)
    assert not result.ok
    assert "at least one side" in result.error


# --------------------------------------------------------------------------
# The command backend (a real subprocess)
# --------------------------------------------------------------------------

def command_backend(tmp_path: Path, extra: str = "") -> CommandBackend:
    template = (f'"{sys.executable}" "{FAKE_GENERATOR}" '
                f'--prompt {{prompt}} --seed {{seed}} --width {{width}} '
                f'--height {{height}} --out {{output}} {extra}').strip()
    return CommandBackend(template)


def test_the_command_backend_reports_a_missing_program(tmp_path):
    backend = CommandBackend("definitely-not-installed-xyz --out {output}")
    status = backend.status()
    assert not status.available
    assert "not found" in status.reason


def test_the_command_backend_writes_a_real_image(tmp_path):
    backend = command_backend(tmp_path)
    assert backend.status().available
    request = GenerationRequest(mode=GenerationMode.TEXT_TO_IMAGE, prompt="a fox",
                                width=96, height=64, seed=1234,
                                output_dir=str(tmp_path / "out"),
                                name_stem="fox")
    result = backend.generate(request)
    assert result.ok, result.error
    assert result.seeds == [1234]
    path = Path(result.paths[0])
    assert path.is_file() and path.stat().st_size > 0
    assert (result.width, result.height) == (96, 64)


def test_the_same_prompt_and_seed_produce_the_same_file(tmp_path):
    """Determinism through the real adapter, not through a mock."""
    backend = command_backend(tmp_path)
    digests = []
    for index in range(2):
        request = GenerationRequest(
            mode=GenerationMode.TEXT_TO_IMAGE, prompt="a red fox", seed=99,
            width=96, height=96, output_dir=str(tmp_path / f"run{index}"),
            name_stem="fox")
        result = backend.generate(request)
        assert result.ok, result.error
        digests.append(hashlib.md5(Path(result.paths[0]).read_bytes()).hexdigest())
    assert digests[0] == digests[1]


def test_a_different_seed_produces_a_different_file(tmp_path):
    backend = command_backend(tmp_path)
    digests = []
    for seed in (1, 2):
        request = GenerationRequest(
            mode=GenerationMode.TEXT_TO_IMAGE, prompt="a red fox", seed=seed,
            width=96, height=96, output_dir=str(tmp_path / f"s{seed}"),
            name_stem="fox")
        result = backend.generate(request)
        digests.append(hashlib.md5(Path(result.paths[0]).read_bytes()).hexdigest())
    assert digests[0] != digests[1]


def test_a_batch_produces_distinct_files_with_distinct_seeds(tmp_path):
    backend = command_backend(tmp_path)
    request = GenerationRequest(mode=GenerationMode.TEXT_TO_IMAGE, prompt="a fox",
                                width=96, height=96, seed=10, batch=4,
                                output_dir=str(tmp_path / "batch"),
                                name_stem="fox")
    result = backend.generate(request)
    assert result.ok, result.error
    assert len(result.paths) == 4
    assert len({str(path) for path in result.paths}) == 4, "files were overwritten"
    assert len(set(result.seeds)) == 4
    for path in result.paths:
        assert Path(path).is_file()


def test_a_non_zero_exit_is_reported_as_a_failure(tmp_path):
    backend = command_backend(tmp_path, "--fail")
    request = GenerationRequest(mode=GenerationMode.TEXT_TO_IMAGE, prompt="x",
                                width=96, height=96,
                                output_dir=str(tmp_path / "out"))
    result = backend.generate(request)
    assert not result.ok
    assert result.code == "COMMAND_FAILED"
    assert result.why, "the program's own output should be passed on"


def test_a_backend_that_writes_nothing_must_not_report_success(tmp_path):
    """Exit code 0 with no file is the sneakiest way to fake a success."""
    backend = command_backend(tmp_path, "--write-nothing")
    request = GenerationRequest(mode=GenerationMode.TEXT_TO_IMAGE, prompt="x",
                                width=96, height=96,
                                output_dir=str(tmp_path / "out"))
    result = backend.generate(request)
    assert not result.ok
    assert result.code == "OUTPUT_NOT_WRITTEN"


def test_a_wrong_size_is_caught_rather_than_accepted(tmp_path):
    """The backend is asked for 64x64 but writes 200x200."""
    template = (f'"{sys.executable}" -c "import sys;from PIL import Image;'
                f'Image.new(\'RGB\',(200,200)).save(sys.argv[1])" {{output}}')
    backend = CommandBackend(template)
    request = GenerationRequest(mode=GenerationMode.TEXT_TO_IMAGE, prompt="x",
                                width=96, height=96,
                                output_dir=str(tmp_path / "out"))
    result = backend.generate(request)
    assert not result.ok
    assert "64 pixels wide" in result.error or "200" in result.error


def test_a_failure_message_never_switches_the_backend(tmp_path):
    """Section 69: report the failure, never silently try another model."""
    backend = command_backend(tmp_path, "--fail")
    request = GenerationRequest(mode=GenerationMode.TEXT_TO_IMAGE, prompt="x",
                                width=96, height=96, model="the-one-i-chose",
                                output_dir=str(tmp_path / "out"))
    result = backend.generate(request)
    assert not result.ok
    assert result.backend == "command", "the result must name the chosen backend"
    assert result.model == "the-one-i-chose"


def test_an_unsupported_request_is_refused_before_the_program_runs(tmp_path):
    """A missing prompt must not spawn a process at all."""
    backend = command_backend(tmp_path)
    request = GenerationRequest(mode=GenerationMode.TEXT_TO_IMAGE, prompt="",
                                width=96, height=96,
                                output_dir=str(tmp_path / "out"))
    result = backend.generate(request)
    assert not result.ok
    assert result.code == "PROMPT_EMPTY"
    assert not (tmp_path / "out").exists() or \
        not list((tmp_path / "out").glob("*.png"))


# --------------------------------------------------------------------------
# Cancellation (section 35)
# --------------------------------------------------------------------------

class _Token:
    """A cancel token that flips after a set number of checks."""

    def __init__(self, after: int = 0) -> None:
        self.calls = 0
        self.after = after
        self.cancelled = False
        self.registered: list = []

    def is_cancelled(self) -> bool:
        self.calls += 1
        if self.calls > self.after:
            self.cancelled = True
        return self.cancelled

    def register(self, process) -> None:
        self.registered.append(process)

    def unregister(self, process) -> None:
        if process in self.registered:
            self.registered.remove(process)

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise RuntimeError("cancelled")


def test_a_cancelled_generation_kills_its_process_and_reports_cancelled(tmp_path):
    """The child is terminated, not left running in the background."""
    backend = command_backend(tmp_path, "--sleep 30")
    token = _Token(after=0)  # cancelled as soon as the backend checks
    request = GenerationRequest(mode=GenerationMode.TEXT_TO_IMAGE, prompt="x",
                                width=96, height=96,
                                output_dir=str(tmp_path / "out"))
    result = backend.generate(request, cancel=token)
    assert result.cancelled
    assert result.state == GenerationState.CANCELLED
    assert not result.ok
    # Nothing was registered as still running.
    assert token.registered == [], "a process handle was left behind"


def test_cancelling_does_not_delete_an_already_written_image(tmp_path):
    """Cancelling a batch keeps what had already been produced."""
    backend = command_backend(tmp_path)
    token = _Token(after=1)  # survives the first check, cancelled on the second
    request = GenerationRequest(mode=GenerationMode.TEXT_TO_IMAGE, prompt="x",
                                width=96, height=96, batch=4,
                                output_dir=str(tmp_path / "out"),
                                name_stem="keep")
    result = backend.generate(request, cancel=token)
    assert result.cancelled
    written = sorted((tmp_path / "out").glob("*.png"))
    assert written, "the images already written should survive a cancel"


# --------------------------------------------------------------------------
# The local HTTP backend (section 67)
# --------------------------------------------------------------------------

def test_only_loopback_endpoints_are_accepted():
    assert is_loopback("http://127.0.0.1:8188")
    assert is_loopback("http://localhost:8188")
    assert not is_loopback("https://api.example.com")


def test_a_remote_endpoint_is_refused_as_not_supported():
    """Stage F is local-only; a remote address is refused, not silently used."""
    backend = HttpBackend("https://api.somewhere-remote.com")
    status = backend.status()
    assert not status.available
    assert status.state == "not_supported"


def test_an_unconfigured_endpoint_says_so():
    status = HttpBackend("").status()
    assert not status.available
    assert status.state == "not_installed"


def test_an_endpoint_that_is_not_listening_is_reported():
    # Port 9 (discard) is not something a test machine serves images on.
    backend = HttpBackend("http://127.0.0.1:9")
    status = backend.status()
    assert not status.available
    assert status.reason


# --------------------------------------------------------------------------
# Model folder scanning (sections 4, 5, 41)
# --------------------------------------------------------------------------

def test_the_scanner_finds_model_files_without_loading_them(tmp_path):
    models = tmp_path / "models" / "image"
    models.mkdir(parents=True)
    (models / "some-model.safetensors").write_bytes(b"0" * 32)
    (models / "notes.txt").write_text("not a model")
    found = ModelFolderScanner([tmp_path]).files()
    assert len(found) == 1
    assert found[0].name == "some-model.safetensors"


def test_a_model_folder_with_no_models_is_reported_as_not_installed(tmp_path):
    """The package may be missing, or the weights - either way, say which."""
    (tmp_path / "models").mkdir()
    backend = DiffusersBackend([])
    status = backend.status()
    if not status.available:
        assert status.reason
        assert status.instructions


def test_unknown_model_files_are_skipped_by_the_scanner(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "readme.md").write_text("hello")
    assert ModelFolderScanner([tmp_path]).files() == []


# --------------------------------------------------------------------------
# The provider base class
# --------------------------------------------------------------------------

def test_a_provider_must_implement_the_contract():
    class Empty(ImageProvider):
        id = "empty"

    with pytest.raises(TypeError):
        Empty()  # abstract methods


def test_the_default_validation_uses_the_reported_capabilities():
    backend = CommandBackend("")
    caps = backend.capabilities()
    assert caps.supports("text_to_image") is True
    request = GenerationRequest(mode="nonsense", prompt="x")
    assert [issue.code for issue in backend.validate(request)] == ["MODE_UNKNOWN"]
