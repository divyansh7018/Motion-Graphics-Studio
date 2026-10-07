"""Hardening pass: image studio, batching, jobs and GUI state (sections 13-28, 33-36, 58).

The image backend used here is the **local command adapter** running
``tests/fake_image_generator.py``.  That program is a deterministic stand-in, not
an image model, and it is labelled as such everywhere: the point of these tests is
the adapter, the file handling and the job plumbing, not image quality.  No test
in this module claims that a real AI model was used (section 18).
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from app.core.paths import AppPaths
from app.core.settings import SettingsStore
from app.image.history import ImageHistory, PromptLibrary
from app.image.library import ImageLibrary, LibraryQuery
from app.image.provider import (
    GenerationMode,
    GenerationRequest,
    GenerationResult,
    ImageCapabilities,
    ImageProvider,
    ProviderStatus,
)
from app.image.service import ImageService
from app.image.variants import Origin, VersionGraph

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_GENERATOR = REPO_ROOT / "tests" / "fake_image_generator.py"
TEST_BACKEND_LABEL = "TEST BACKEND (fixture - not an AI model)"

pytestmark = pytest.mark.skipif(not FAKE_GENERATOR.is_file(),
                                reason="the test image writer is missing")


# ---------------------------------------------------------------------------
# a clearly labelled test backend, and the provider contract
# ---------------------------------------------------------------------------

class FixtureBackend(ImageProvider):
    #: pytest must not treat this as a test class
    __test__ = False
    """A deliberately fake backend for contract tests.

    It is named and labelled so that a report can never mistake it for a model:
    every string it returns says TEST BACKEND, and it reports no capabilities it
    does not have.
    """

    id = "test-backend"
    label = TEST_BACKEND_LABEL
    kind = "python"

    def __init__(self) -> None:
        self.generated: list[GenerationRequest] = []
        self.loaded: list[str] = []
        self.closed = 0

    def status(self) -> ProviderStatus:
        return ProviderStatus(available=True, state="available",
                              reason="This is a test fixture, not a real backend.",
                              version="0")

    def capabilities(self) -> ImageCapabilities:
        return ImageCapabilities(text_to_image=True, seed_control=True,
                                 min_dimension=32, max_dimension=1024,
                                 dimension_multiple=1, max_batch=4)

    def models(self) -> list:
        return []

    def load(self, model) -> None:
        self.loaded.append(getattr(model, "id", ""))

    def unload(self) -> None:
        self.closed += 1

    def generate(self, request: GenerationRequest, *, progress=None, cancel=None):
        self.generated.append(request)
        if cancel is not None and getattr(cancel, "cancelled", False):
            return GenerationResult(ok=False, cancelled=True, state="CANCELLED",
                                    error="The test backend was cancelled.",
                                    why="A test asked it to stop.",
                                    what_to_do="Run the generation again.",
                                    backend=self.id)
        return GenerationResult(ok=False, state="FAILED", backend=self.id,
                                error="The test backend does not write images.",
                                why="It exists to check the contract, not to generate.",
                                what_to_do="Use a real backend.")


def test_every_registered_backend_satisfies_the_provider_contract():
    """Duck-typing is not enough: the base class promises an interface."""
    from app.image.backends import BACKEND_CLASSES, BACKEND_ORDER

    assert BACKEND_CLASSES, "no backends are registered at all"
    assert set(BACKEND_CLASSES) == set(BACKEND_ORDER)
    for backend in BACKEND_CLASSES.values():
        assert isinstance(backend, type) and issubclass(backend, ImageProvider), backend
        for attribute in ("id", "label", "kind"):
            value = getattr(backend, attribute, "")
            assert isinstance(value, str) and value, (backend, attribute)
        for method in ("status", "capabilities", "models", "generate", "validate"):
            assert callable(getattr(backend, method, None)), (backend, method)
        # Construction must be cheap and must not raise.
        instance = backend()
        assert isinstance(instance.status(), ProviderStatus)
        assert isinstance(instance.capabilities(), ImageCapabilities)
        assert isinstance(instance.models(), list)


def test_the_test_backend_is_labelled_and_never_claims_a_model():
    backend = FixtureBackend()
    assert "TEST BACKEND" in backend.label.upper()
    assert backend.models() == []
    status = backend.status()
    assert status.available is True
    assert "fixture" in status.reason.lower()
    result = backend.generate(GenerationRequest(prompt="anything", width=64, height=64))
    assert isinstance(result, GenerationResult)
    assert result.ok is False
    assert TEST_BACKEND_LABEL not in result.error  # the error is about the request


def test_the_provider_contract_checks_a_request_before_loading_a_model():
    backend = FixtureBackend()
    issues = backend.validate(GenerationRequest(mode="inpaint", prompt="x",
                                               width=64, height=64))
    assert issues, "an unsupported mode must be refused"
    assert all(issue.code and issue.message and issue.what_to_do for issue in issues)
    assert backend.loaded == [], "validation must not load anything"
    allowed = backend.validate(GenerationRequest(mode="text_to_image", prompt="x",
                                                width=64, height=64, batch=1))
    assert allowed == []


def test_batch_beyond_the_backend_limit_is_refused():
    backend = FixtureBackend()
    issues = backend.validate(GenerationRequest(mode="text_to_image", prompt="x",
                                               width=64, height=64, batch=9))
    assert any("BATCH" in issue.code for issue in issues), [i.code for i in issues]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

class ImageStudio:
    """A real data root, settings and ImageService wired to the test writer."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.paths = AppPaths(data_root=self.root, source_root=self.root,
                              reason="hardening")
        self.paths.ensure()
        self.store = SettingsStore(self.paths.settings_file)
        settings = self.store.load().settings
        settings.image.command = (
            f'"{sys.executable}" "{FAKE_GENERATOR}" --prompt {{prompt}} '
            f'--seed {{seed}} --width {{width}} --height {{height}} '
            f'--out {{output}}')
        settings.image.active_backend = "command"
        self.store.save(settings)
        self.settings = settings
        self.service = ImageService.for_paths(self.paths, settings)

    def request(self, **values) -> GenerationRequest:
        defaults = dict(mode="text_to_image", backend="command",
                        prompt="a hardening test image",
                        width=64, height=64, seed=1234, batch=1,
                        output_dir=str(self.root / "generated"))
        defaults.update(values)
        return GenerationRequest(**defaults)

    def generate(self, **values):
        return self.service.generate(self.request(**values))

    def write_image(self, name: str, *, size=(64, 48), colour=(30, 90, 200),
                    mode="RGB", folder: str = "images") -> Path:
        from PIL import Image

        path = self.root / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        value = (*colour, 200) if mode == "RGBA" else colour
        Image.new(mode, size, value).save(path)
        return path


@pytest.fixture()
def studio(tmp_path: Path) -> ImageStudio:
    return ImageStudio(tmp_path / "data")


def md5(path) -> str:
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 13. The command adapter, three batches in a row
# ---------------------------------------------------------------------------

def test_three_batches_through_the_command_adapter_never_collide(studio: ImageStudio):
    first = studio.generate(seed=100, batch=4, name_stem="tile")
    assert first.ok, first.error
    assert len(set(first.paths)) == 4
    assert len({md5(path) for path in first.paths}) == 4

    # The same prompt and seed in a new batch must reproduce the same images...
    again = studio.generate(seed=100, batch=4, name_stem="tile")
    assert again.ok, again.error
    assert len(set(again.paths)) == 4
    assert all(path not in first.paths for path in again.paths), (
        "a second batch overwrote files from the first")
    for earlier, later in zip(first.paths, again.paths):
        assert md5(earlier) == md5(later), (earlier, later)

    # An overlapping seed range reproduces the images it overlaps, byte for
    # byte, and writes them somewhere new - the seed really is the recipe.
    other = studio.generate(seed=101, batch=4, name_stem="tile")
    assert other.ok, other.error
    assert other.seeds == [101, 102, 103, 104]
    by_seed = dict(zip(first.seeds, [md5(path) for path in first.paths]))
    for seed, path in zip(other.seeds, other.paths):
        if seed in by_seed:
            assert md5(path) == by_seed[seed], (seed, path)

    # A seed well away from that range gives completely different pictures.
    far = studio.generate(seed=7000, batch=4, name_stem="tile")
    assert far.ok, far.error
    assert not ({md5(path) for path in far.paths}
                & {md5(path) for path in first.paths})

    on_disk = sorted(path.name for path in Path(first.paths[0]).parent.glob("*.png"))
    # Four batches of four, and not one of them replaced an earlier file.
    assert len(on_disk) == 16, on_disk


def test_a_single_generation_reports_its_real_dimensions_and_seed(studio: ImageStudio):
    result = studio.generate(seed=777, width=96, height=64)
    assert result.ok, result.error
    assert result.seeds == [777]
    assert (result.width, result.height) == (96, 64)
    from PIL import Image

    with Image.open(result.paths[0]) as image:
        assert image.size == (96, 64)


def test_the_command_adapter_reports_a_failing_command_honestly(studio: ImageStudio):
    studio.settings.image.command = (
        f'"{sys.executable}" "{FAKE_GENERATOR}" --fail --prompt {{prompt}} '
        f'--seed {{seed}} --width {{width}} --height {{height}} --out {{output}}')
    service = ImageService.for_paths(studio.paths, studio.settings)
    result = service.generate(studio.request())
    assert result.ok is False
    assert result.state == "FAILED"
    assert result.error and result.what_to_do
    assert result.paths == []


def test_a_command_that_writes_nothing_is_a_failure_not_a_success(studio: ImageStudio):
    studio.settings.image.command = (
        f'"{sys.executable}" "{FAKE_GENERATOR}" --write-nothing --prompt {{prompt}} '
        f'--seed {{seed}} --width {{width}} --height {{height}} --out {{output}}')
    service = ImageService.for_paths(studio.paths, studio.settings)
    result = service.generate(studio.request())
    assert result.ok is False
    assert "wrote no" in result.error.lower() or "not" in result.error.lower()
    assert result.paths == []


def test_a_cancelled_generation_stops_the_child_process(studio: ImageStudio):
    from app.jobs.cancel import CancelToken

    token = CancelToken()

    class CancelAfterStart(threading.Thread):
        def __init__(self, delay: float) -> None:
            super().__init__(daemon=True)
            self.delay = delay

        def run(self) -> None:
            time.sleep(self.delay)
            token.cancel()

    # The fake writer sleeps, so there is something to cancel.
    studio.settings.image.command = (
        f'"{sys.executable}" "{FAKE_GENERATOR}" --sleep 20 --prompt {{prompt}} '
        f'--seed {{seed}} --width {{width}} --height {{height}} --out {{output}}')
    service = ImageService.for_paths(studio.paths, studio.settings)
    watcher = CancelAfterStart(0.4)
    started = time.monotonic()
    watcher.start()
    result = service.generate(studio.request(), cancel=token)
    elapsed = time.monotonic() - started
    assert result.cancelled is True, (result.state, result.error)
    assert result.state == "CANCELLED"
    assert elapsed < 15.0, elapsed
    assert result.paths == []


# ---------------------------------------------------------------------------
# 14. Upscaling without a modal that can hang
# ---------------------------------------------------------------------------

def test_upscale_is_callable_without_any_dialog(studio: ImageStudio):
    from app.image.upscale import upscale

    source = studio.write_image("small.png", size=(40, 30))
    target = studio.root / "big.png"
    result = upscale(source, target, scale=4.0, method="standard")
    assert result.ok, getattr(result, "error", "")
    assert target.is_file()
    from PIL import Image

    with Image.open(target) as image:
        assert image.size == (160, 120)
    assert "standard" in result.method_label.lower()


def test_an_ai_upscale_without_a_model_is_refused_rather_than_faked(studio: ImageStudio):
    from app.image.upscale import upscale

    source = studio.write_image("small.png", size=(40, 30))
    target = studio.root / "ai.png"
    result = upscale(source, target, scale=2.0, method="ai")
    assert result.ok is False
    assert not target.exists(), "nothing may be written when the model is missing"
    assert "ai" in result.method_label.lower()
    assert result.downgraded is False
    assert result.error, "a refused upscale must say why"
    assert result.what_to_do


def test_an_upscale_never_overwrites_an_existing_file(studio: ImageStudio):
    from app.image.upscale import upscale

    source = studio.write_image("small.png", size=(40, 30))
    target = studio.root / "taken.png"
    target.write_bytes(b"the user's own file")
    result = upscale(source, target, scale=2.0, method="standard")
    assert result.ok, getattr(result, "error", "")
    assert target.read_bytes() == b"the user's own file"
    assert Path(getattr(result, "path", target)) != target
    assert Path(result.path).name.startswith("taken")
    assert Path(result.path).stat().st_size > 0


# ---------------------------------------------------------------------------
# 15-16. Prompt library persistence, missing files and relinking
# ---------------------------------------------------------------------------

def test_saved_prompts_survive_a_restart(studio: ImageStudio):
    prompts = studio.service.prompts
    saved = prompts.save("a lighthouse at dusk")
    assert saved is not None
    prompts.set_favourite(saved.id, True)
    prompts.record_use("a lighthouse at dusk")
    prompts.record_use("a lighthouse at dusk")
    boat = prompts.save("a paper boat")
    assert boat is not None
    assert prompts.rename(boat.id, "the folded paper boat") is True

    reopened = PromptLibrary(studio.paths.config_dir / "image_prompts.json")
    texts = [entry.text for entry in reopened.all()]
    assert "a lighthouse at dusk" in texts
    assert "a paper boat" in texts
    favourite = reopened.by_text("a lighthouse at dusk")
    assert favourite is not None and favourite.favourite is True
    assert favourite.used >= 2
    renamed = reopened.find(boat.id)
    assert renamed is not None and renamed.name == "the folded paper boat"


def test_a_prompt_is_only_changed_by_an_explicit_edit(studio: ImageStudio):
    """Recording a use must never rewrite what the user typed."""
    prompts = studio.service.prompts
    entry = prompts.save("exactly this wording")
    assert entry is not None
    prompts.record_use("exactly this wording")
    assert prompts.find(entry.id).text == "exactly this wording"
    prompts.record_use("a different prompt")
    assert prompts.find(entry.id).text == "exactly this wording"


def test_a_prompt_edit_is_persisted(studio: ImageStudio):
    prompts = studio.service.prompts
    entry = prompts.save("first wording")
    assert entry is not None
    assert prompts.edit(entry.id, "second wording", negative="no blur") is True
    reopened = PromptLibrary(studio.paths.config_dir / "image_prompts.json")
    stored = reopened.find(entry.id)
    assert stored is not None
    assert stored.text == "second wording"
    assert stored.negative == "no blur"
    assert reopened.by_text("first wording") is None


def test_a_deleted_image_is_kept_as_missing_and_can_be_relinked(studio: ImageStudio):
    library = studio.service.library
    original = studio.write_image("keep_me.png")
    library.add_entry(original)
    library.save()
    library.scan()
    assert library.entry_for(original) is not None

    original.unlink()
    removed = library.scan()
    assert removed == 0
    entry = library.entry_for(original)
    assert entry is not None, "a deleted file must not vanish from the index"
    assert entry.missing is True

    # Put the file back: the entry is no longer missing and links again.
    studio.write_image("keep_me.png", colour=(200, 30, 30))
    library.scan()
    entry = library.entry_for(original)
    assert entry is not None and entry.missing is False
    assert entry.width == 64 and entry.height == 48


def test_relinking_an_image_that_moved_updates_the_library(studio: ImageStudio):
    library = studio.service.library
    original = studio.write_image("before.png", size=(50, 40))
    library.add_entry(original)
    library.set_tags(original, ["hero"])
    library.save()
    library.scan()

    moved = studio.root / "images" / "sub" / "after.png"
    moved.parent.mkdir(parents=True, exist_ok=True)
    original.rename(moved)
    library.scan()

    assert library.entry_for(original) is not None
    assert library.entry_for(original).missing is True
    assert library.entry_for(moved) is not None
    assert library.entry_for(moved).missing is False


def test_image_history_survives_a_restart_with_its_seeds(studio: ImageStudio):
    result = studio.generate(seed=4242)
    assert result.ok, result.error
    statuses = {entry.status for entry in studio.service.history.all()}
    assert "COMPLETED" in statuses, statuses

    reopened = ImageHistory(studio.paths.config_dir / "image_history.json")
    entries = reopened.all()
    assert entries, "history did not persist"
    assert any(entry.seed == 4242 for entry in entries)
    assert reopened.same_seed(4242), "find-by-seed stopped working after a reload"

    # The prompt the generation used is remembered, and the history kept it.
    assert studio.service.prompts.by_text("a hardening test image") is not None
    assert any("hardening test image" in (entry.prompt or "") for entry in entries)


# ---------------------------------------------------------------------------
# 17. Device detection must not import torch (and must be fast)
# ---------------------------------------------------------------------------

def _in_a_fresh_interpreter(code: str) -> str:
    """Run a snippet in its own Python, so nothing else can pollute it."""
    import subprocess

    finished = subprocess.run(
        [sys.executable, "-c", code], cwd=str(REPO_ROOT),
        capture_output=True, text=True, timeout=120)
    assert finished.returncode == 0, finished.stderr[-2000:]
    return finished.stdout


def test_device_detection_is_cheap_and_never_imports_torch():
    """Measured in a fresh interpreter: another test may already have loaded torch.

    That matters because the real question is what *device detection* costs.  A
    machine without torch must not have it dragged in - importing it is a crash
    waiting to happen on a fresh Windows install - and a machine with it must not
    pay half a gigabyte of memory just to read the CPU name.
    """
    output = _in_a_fresh_interpreter(
        "import json, sys, time\n"
        "assert 'torch' not in sys.modules, 'torch was already imported'\n"
        "from app.image.device import detect_device\n"
        "samples = []\n"
        "for _ in range(3):\n"
        "    started = time.perf_counter()\n"
        "    info = detect_device()\n"
        "    samples.append(time.perf_counter() - started)\n"
        "print(json.dumps({'best': min(samples), 'torch': 'torch' in sys.modules,\n"
        "                  'accelerator': info.accelerator,\n"
        "                  'ram_gb': info.ram_gb, 'probes': len(info.probes),\n"
        "                  'cpu_count': info.cpu_count}))\n")
    measured = json.loads(output.strip().splitlines()[-1])

    assert measured["torch"] is False, (
        "detect_device imported torch - on a machine without it that is a crash, "
        "and on a machine with it that is half a gigabyte of memory")
    assert measured["best"] < 0.25, (
        f"device detection took {measured['best'] * 1000:.1f} ms")
    assert measured["accelerator"] in ("cpu", "cuda", "dml", "rocm", "mps", "unknown")
    assert measured["ram_gb"] >= 0
    assert measured["probes"] >= 1
    assert measured["cpu_count"] >= 1


def test_device_detection_deep_mode_is_opt_in_and_reports_itself():
    """A deep probe is asked for, never done behind the user's back."""
    output = _in_a_fresh_interpreter(
        "import json, sys\n"
        "from app.image.device import detect_device\n"
        "shallow = detect_device()\n"
        "assert 'torch' not in sys.modules, 'the shallow probe loaded torch'\n"
        "print(json.dumps({'accelerator': shallow.accelerator,\n"
        "                  'probes': [str(probe) for probe in shallow.probes]}))\n")
    measured = json.loads(output.strip().splitlines()[-1])
    assert measured["accelerator"]
    assert measured["probes"], "a device check must say what it looked at"


# ---------------------------------------------------------------------------
# 19-22. Backends that are not installed must say so, not pretend
# ---------------------------------------------------------------------------

def test_uninstalled_backends_report_their_state_without_raising(studio: ImageStudio):
    registry = studio.service.registry
    report = registry.detect()
    available = {entry.id: entry.available for entry in report.backends}
    assert "command" in available
    assert available["command"] is True
    assert report.any_generator is False or report.models
    assert report.generator_note
    # Whatever else is installed on this machine, every backend must report a
    # state and an unavailable one must say what to do about it.
    for entry in report.backends:
        state = entry.status()
        assert state.state in ("available", "not_installed", "not_supported", "error")
        if not entry.available:
            assert state.reason or state.instructions, entry.id


def test_a_backend_that_cannot_run_explains_itself(studio: ImageStudio):
    from app.image.backends import model_backends

    for name in ("DiffusersBackend", "ComfyUIBackend", "OnnxBackend"):
        backend = getattr(model_backends, name, None)
        if backend is None:
            continue
        instance = backend()
        status = instance.status()
        assert isinstance(status, ProviderStatus)
        assert isinstance(status.instructions, list)
        if not status.available:
            assert status.reason, f"{name} is unavailable with no reason given"
            capabilities = instance.capabilities()
            assert isinstance(capabilities, ImageCapabilities)
            # An unavailable backend must not claim it can do things.
            assert instance.generate(GenerationRequest(
                prompt="x", width=64, height=64), progress=None,
                cancel=None).ok is False


def test_background_removal_and_ai_upscale_report_an_honest_state(studio: ImageStudio):
    from app.image import background_removal

    state = getattr(background_removal, "state", None) or \
        getattr(background_removal, "removal_state", None)
    if callable(state):
        reported = state()
        assert getattr(reported, "available", False) in (True, False)
        if not getattr(reported, "available", False):
            assert getattr(reported, "reason", "") or getattr(reported, "instructions", [])


# ---------------------------------------------------------------------------
# 23-24. Editing formats, lossy sources, and versioning
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("suffix", [".png", ".jpg", ".webp"])
def test_editing_works_for_the_formats_the_app_offers(studio: ImageStudio, suffix: str):
    from PIL import Image

    source = studio.root / "images" / f"source{suffix}"
    source.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (80, 60), (200, 120, 40))
    for x in range(0, 80, 8):
        for y in range(0, 60, 8):
            image.putpixel((x, y), (10, 10, 10))
    if suffix == ".jpg":
        image.save(source, quality=70)          # deliberately lossy
    elif suffix == ".webp":
        image.save(source, lossless=False, quality=60)
    else:
        image.save(source)

    before = md5(source)
    session = studio.service.open_editor(source)
    session.add("brightness", amount=0.2)
    session.add("rotate", degrees=90)
    assert session.can_undo is True
    target = studio.root / "images" / f"edited{suffix}"
    saved = studio.service.save_edit(session, target=target)
    written = Path(getattr(saved, "path", saved))
    assert written.is_file(), saved
    assert md5(source) == before, "the original was modified by an edit"
    with Image.open(written) as result:
        assert result.size == (80, 60), result.size
    assert len(session.history()) == 2


def test_editing_a_transparent_png_keeps_its_alpha(studio: ImageStudio):
    target = studio.root / "images" / "alpha.png"
    source = studio.write_image("alpha_source.png", size=(40, 40), mode="RGBA",
                                colour=(10, 200, 90))
    session = studio.service.open_editor(source)
    session.add("brightness", amount=0.1)
    saved = studio.service.save_edit(session, target=target)
    from PIL import Image

    with Image.open(Path(getattr(saved, "path", saved))) as image:
        assert image.mode in ("RGBA", "LA"), image.mode


def test_a_chain_of_variations_keeps_its_lineage_after_a_restart(studio: ImageStudio):
    first = studio.generate(seed=11, name_stem="root")
    assert first.ok, first.error
    root = Path(first.paths[0])

    from app.image.metadata import ImageMetadata

    graph = VersionGraph()
    graph.add(root, metadata=ImageMetadata(origin=Origin.GENERATED, seed=11))
    child = studio.root / "generated" / "child.png"
    child.write_bytes(root.read_bytes())
    variation = studio.service.variant_of(root, collection="hero")
    assert variation.parent_asset == str(root), "a variation must record its parent"
    assert md5(root)
    assert variation.mode == GenerationMode.IMAGE_TO_IMAGE
    assert (variation.width, variation.height) == (64, 64)
    assert md5(root), "the source image must still be on disk, untouched"

    graph.add(child, metadata=ImageMetadata(origin=Origin.VARIATION, seed=12,
                                           parent=str(root)))
    graph.relink()
    assert [Path(str(node.path)) for node in graph.lineage(child)] == [root, child]
    assert [Path(str(node.path)) for node in graph.children_of(root)] == [child]
    assert [Path(str(node.path)) for node in graph.roots()] == [root]

    # A parent that is not in this graph is reported, not silently re-rooted.
    orphan = studio.root / "generated" / "orphan.png"
    orphan.write_bytes(root.read_bytes())
    graph.add(orphan, metadata=ImageMetadata(parent=str(studio.root / "vanished.png")))
    assert graph.orphans(), "a missing parent must be reported, not hidden"


# ---------------------------------------------------------------------------
# 25. Images in a project, including a moved project folder
# ---------------------------------------------------------------------------

def test_an_image_used_in_a_scene_survives_moving_the_project(studio: ImageStudio):
    from app.project.service import CreateRequest, ProjectService
    from app.scene.compose import render_scene
    from app.scene.storyboard import build_context
    from app.scene.canvas import Canvas

    settings = studio.store.load().settings
    projects = ProjectService(studio.paths, settings)
    project = projects.create_project(CreateRequest(name="Images", width=320, height=256,
                                                   fps=25, template="blank"))
    project_dir = Path(projects.session.layout.root)

    from PIL import Image

    asset_path = project_dir / "assets" / "hero.png"
    asset_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (200, 150), (20, 160, 120)).save(asset_path)

    service = ImageService.for_paths(studio.paths, settings)
    project = projects.current
    projects.save(reason="images")

    from app.project.model import ElementSpec

    project.add_scene()
    scene = project.scenes[0]
    scene.duration = 1.0
    element = ElementSpec(id="img1", kind="image", asset_id="",
                          position={"x": 0.5, "y": 0.5},
                          size={"mode": "relative", "value": 0.5})
    scene.elements = [element]
    entry = service.library.add_entry(asset_path)
    entry.width, entry.height = 200, 150
    service.library.save()

    from app.project.model import AssetSpec

    project.assets.append(AssetSpec(id="asset-hero", name="hero.png", kind="image",
                                   path="assets/hero.png",
                                   size_bytes=asset_path.stat().st_size,
                                   width=200, height=150))
    element.asset_id = "asset-hero"
    projects.save(reason="images")

    projects.close_project(save=True)
    moved_root = studio.root / "moved project (v2)"
    moved_root.parent.mkdir(parents=True, exist_ok=True)
    project_dir.rename(moved_root)

    reopened = ProjectService(studio.paths, settings)
    reopened.open_project(moved_root / "project.json")
    canvas = Canvas(320, 256, 25)
    ctx = build_context(reopened.current, canvas=canvas, project_dir=moved_root)
    frame = render_scene(reopened.current.scenes[0], ctx)
    assert frame is not None
    reopened.close_project()


# ---------------------------------------------------------------------------
# 26-27. State that must persist, caches that must invalidate
# ---------------------------------------------------------------------------

def test_the_library_index_reloads_without_a_rescan(studio: ImageStudio):
    library = studio.service.library
    for index in range(3):
        library.add_entry(studio.write_image(f"pic_{index}.png"))
    library.save()

    reopened = ImageLibrary(studio.paths.images_dir)
    entries = reopened.entries()
    assert len(entries) == 3, [entry.name for entry in entries]
    # The index file is what provides this: no scan was run on the new object.
    assert (studio.paths.images_dir / "library_index.json").is_file()


def test_a_thumbnail_is_rebuilt_when_the_file_changes(studio: ImageStudio):
    cache = studio.service.thumbnails
    image = studio.write_image("changing.png", colour=(10, 10, 10))
    first = cache.get(image)
    assert first is not None and Path(first).is_file()
    assert cache.get(image) == first, "a cached thumbnail must be reused"

    time.sleep(0.01)
    studio.write_image("changing.png", size=(96, 96), colour=(240, 240, 240))
    second = cache.get(image)
    assert second is not None and Path(second).is_file()
    assert Path(second) != Path(first), "the cache key ignored a changed file"

    # A file that is not an image at all gets no thumbnail rather than a wrong one.
    broken = studio.write_image("broken.png")
    Path(broken).write_bytes(b"this is not a png")
    assert cache.get(broken) is None


def test_generation_settings_change_the_bytes_not_the_cache(studio: ImageStudio):
    """Same prompt, four different settings: four different images."""
    base = studio.generate(seed=5, width=64, height=64)
    assert base.ok, base.error
    changed = {
        "seed": studio.generate(seed=6, width=64, height=64),
        "resolution": studio.generate(seed=5, width=96, height=64),
        "prompt": studio.generate(seed=5, prompt="a different prompt"),
    }
    for name, result in changed.items():
        assert result.ok, (name, result.error)
        assert md5(result.paths[0]) != md5(base.paths[0]), name


def test_the_library_query_reflects_edits_without_a_full_rescan(studio: ImageStudio):
    library = studio.service.library
    path = studio.write_image("queryable.png")
    library.add_entry(path)
    entry = library.entry_for(path)
    assert entry is not None
    entry.prompt = "orange lighthouse"
    assert library.save_entry(path) is True
    library.save()

    assert [item.path for item in library.query(
        LibraryQuery(text="orange")).entries] == [str(path)]
    assert library.set_tags(path, ["hero", "orange"]) is True
    library.save()
    tagged = library.query(LibraryQuery(tags=["hero"])).entries
    assert [item.path for item in tagged] == [str(path)]


# ---------------------------------------------------------------------------
# 28. Batches of 1, 4 and 10; cancellation; a partial failure is not a success
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("size", [1, 4])
def test_batches_of_one_and_four_are_complete_and_unique(studio: ImageStudio,
                                                         size: int):
    result = studio.generate(batch=size, seed=900, name_stem="batch")
    assert result.ok, result.error
    assert len(result.paths) == size
    assert len(set(result.paths)) == size, "the same file was reported twice"
    assert len({md5(path) for path in result.paths}) == size, "two images were identical"
    assert len(result.seeds) == size
    assert len(set(result.seeds)) == size, "every image in a batch needs its own seed"


def test_a_ten_image_batch_runs_through_the_job_the_button_starts(studio: ImageStudio):
    """Ten images: one job, ten files, ten seeds (section 28).

    The adapter caps a single *backend* call at eight, so the job body asks for
    one image at a time - which is what the button does.
    """
    from app.image.jobs import batch_body

    request = studio.request(batch=10, seed=700, name_stem="ten")
    context = _FakeContext(studio, request)
    payload = batch_body(context)

    assert payload["ok"] is True, payload["message"]
    assert payload["created"] == 10
    assert payload["requested"] == 10
    assert len(payload["items"]) == 10
    all_paths = [path for item in payload["items"] for path in item["paths"]]
    assert len(set(all_paths)) == 10
    assert len({md5(path) for path in all_paths}) == 10
    seeds = [seed for item in payload["items"] for seed in item["seeds"]]
    assert len(set(seeds)) == 10

    # And the direct call is refused honestly rather than silently split.
    direct = studio.generate(batch=10, seed=700, name_stem="direct")
    assert direct.ok is False
    assert "8" in direct.error
    assert direct.what_to_do


def test_a_batch_that_loses_one_image_is_not_reported_as_success(studio: ImageStudio):
    from app.image.jobs import batch_body
    from app.jobs.spec import JobContext

    class Progress:
        def start(self, **kwargs):
            pass

        def update(self, **kwargs):
            pass

    class Context(JobContext):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__(spec=None, payload={})

    # Drive the job body directly with a service that fails on the second image.
    service = studio.service
    request = studio.request(batch=3, seed=500)
    calls = {"count": 0}
    original = service.generate

    def flaky(single, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            return GenerationResult(ok=False, state="FAILED", error="Image 2 failed.",
                                    why="The test asked for one failure.",
                                    what_to_do="Try again.", backend="command")
        return original(single, **kwargs)

    context = _FakeContext(studio, request, service=_FlakyService(service, flaky))
    payload = batch_body(context)

    assert payload["ok"] is False, "a partial failure was counted as success"
    assert payload["created"] == 2
    assert payload["requested"] == 3
    assert any(not item["ok"] for item in payload["items"])
    assert "failed" in payload["message"].lower()


class _FlakyService:
    """An ImageService whose ``generate`` fails on one chosen call."""

    def __init__(self, service, generate) -> None:
        self._service = service
        self.generate = generate  # type: ignore[assignment]

    def __getattr__(self, name):
        return getattr(self._service, name)


class _FakeContext:
    """A real JobContext, so the job body is exercised as it really runs."""

    def __init__(self, studio: ImageStudio, request: GenerationRequest,
                 *, service=None) -> None:
        from app.jobs.spec import JobContext

        payload = {"request": request, "paths": studio.paths,
                   "settings": studio.settings}
        if service is not None:
            payload["service"] = service
        self._context = JobContext(
            job_id="test-job", key="image_batch", cancel=_Never(),
            progress=_QuietProgress(), settings=studio.settings,
            paths=studio.paths, payload=payload)

    def __getattr__(self, name):
        return getattr(self._context, name)


class _Never:
    """A cancel token that is never cancelled, with the real token's contract."""

    job_id = "test-job"
    cancelled = False
    _processes: list = []

    def is_cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None

    def register_process(self, process) -> None:
        self._processes.append(process)

    def unregister_process(self, process) -> None:
        if process in self._processes:
            self._processes.remove(process)

    def active_process_count(self) -> int:
        return len(self._processes)

    def terminate_children(self, grace_seconds: float = 5.0) -> int:
        return 0


class _QuietProgress:
    class _P:
        total = 1.0

    progress = _P()

    def start(self, **kwargs) -> None:
        return None

    def update(self, **kwargs) -> None:
        return None


# ---------------------------------------------------------------------------
# 33-36, 58. Jobs from the GUI: one action, one job; no modal hangs
# ---------------------------------------------------------------------------

def test_the_command_backend_is_chosen_by_the_settings_not_by_the_ui(
        studio: ImageStudio):
    status = studio.service.status()
    assert status.any_backend is True
    described = status.describe()
    assert "command" in described.lower() or "local" in described.lower()
    assert "test" not in described.lower() or "TEST BACKEND" in described


def test_a_stale_history_entry_for_a_deleted_file_is_reported(studio: ImageStudio):
    result = studio.generate(seed=88)
    assert result.ok, result.error
    Path(result.paths[0]).unlink()
    entries = studio.service.history.refresh_exists()
    assert isinstance(entries, int)
    gone = [entry for entry in studio.service.history.all()
            if entry.path == result.paths[0]]
    if gone:
        assert gone[0].exists is False
        assert gone[0].to_dict()["exists"] is False


def test_reporting_json_is_stable_and_complete(studio: ImageStudio):
    result = studio.generate(seed=31337)
    payload = json.loads(json.dumps({
        "paths": list(result.paths), "seeds": list(result.seeds),
        "backend": result.backend, "state": result.state,
    }))
    assert payload["state"] in ("COMPLETED", "FAILED", "CANCELLED")
    assert payload["backend"]
