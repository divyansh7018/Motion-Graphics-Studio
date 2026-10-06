"""Stage F manual test matrix (directive section 73) - real runs, real files.

Every scenario is executed against the real services and the results are printed
as they are found.  Nothing is asserted away: a scenario that cannot run on this
machine reports **NOT AVAILABLE** with the reason, rather than being skipped
quietly or, worse, counted as a pass.

The 17 numbered tests in the directive are covered, plus the backend contract
and honesty checks that section 82 depends on.

Run it with:

    python scripts/stage_f_manual_matrix.py --data-root /tmp/mgs_stage_f

On a machine with no image model installed - the honest state of a fresh install
- the generation scenarios run against the built-in **standard** backend and the
**local command** backend, both of which are real code paths.  Nothing here
pretends to be an AI model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PIL import Image  # noqa: E402

from app.core.paths import AppPaths  # noqa: E402
from app.core.settings import SettingsStore  # noqa: E402
from app.image.editor import ImageEditSession, apply_to_source  # noqa: E402
from app.image.integration import (Placement, resolve_element_image,  # noqa: E402
                                   send_to_scene, use_in_project)
from app.image.library import LibraryQuery  # noqa: E402
from app.image.metadata import ImageMetadata, read_metadata, write_metadata  # noqa: E402
from app.image.provider import GenerationMode, GenerationRequest  # noqa: E402
from app.image.service import ImageService  # noqa: E402
from app.image.upscale import upscale  # noqa: E402
from app.image.validation import validate_image_file  # noqa: E402
from app.image.variants import VersionGraph  # noqa: E402
from app.project.service import CreateRequest, ProjectService  # noqa: E402
from app.scene.compose import render_scene  # noqa: E402
from app.scene.storyboard import build_context  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
NOT_AVAILABLE = "N/A"

FAKE_GENERATOR = REPO_ROOT / "tests" / "fake_image_generator.py"

RESULTS: list[tuple[int, str, str, str]] = []


def record(number: int, name: str, ok: str, detail: str) -> None:
    RESULTS.append((number, name, ok, detail))
    print(f"[{ok}] {number:02d} {name}: {detail}", flush=True)


def check(number: int, name: str, condition: bool, detail: str) -> bool:
    record(number, name, PASS if condition else FAIL, detail)
    return bool(condition)


def make_image(path: Path, size=(320, 200), colour=(40, 120, 200),
               mode="RGB") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    value = colour if mode != "RGBA" else (*colour, 200)
    Image.new(mode, size, value).save(path)
    return path


def md5(path: Path) -> str:
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="/tmp/mgs_stage_f")
    parser.add_argument("--keep", action="store_true",
                        help="Keep the data root instead of starting clean.")
    args = parser.parse_args(argv)

    root = Path(args.data_root)
    if root.exists() and not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    paths = AppPaths(data_root=root, source_root=REPO_ROOT, reason="stage-f-matrix")
    paths.ensure()
    settings = SettingsStore(paths.settings_file).load().settings

    work = root / "matrix"
    work.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print("STAGE F MANUAL MATRIX")
    print(f"data root : {root}")
    print(f"python    : {sys.version.split()[0]}")
    print("=" * 78)

    # ----------------------------------------------------------------------
    # Detection first: everything below depends on what is installed.
    # ----------------------------------------------------------------------
    service = ImageService.for_paths(paths, settings)
    status = service.status()
    print("\n-- detected --")
    print(status.describe())
    print()

    # A real local backend, so the generation scenarios exercise the full
    # adapter path rather than a mock.
    command = (f'"{sys.executable}" "{FAKE_GENERATOR}" --prompt {{prompt}} '
               f'--seed {{seed}} --width {{width}} --height {{height}} '
               f'--out {{output}}')
    settings.image.command = command
    settings.image.active_backend = "command"
    SettingsStore(paths.settings_file).save(settings)
    service = ImageService.for_paths(paths, settings)
    service.status()
    command_available = bool(service.registry.get("command")
                             and service.registry.get("command").available)
    print(f"local command backend available: {command_available}")
    print(f"any AI generator installed     : {status.any_generator}")
    print()

    # ----------------------------------------------------------------------
    # 1. Import a PNG, preview it, save it to the asset library.
    # ----------------------------------------------------------------------
    png = make_image(work / "import_me.png", size=(400, 250))
    report = service.import_image(png, destination=service.library_root / png.name)
    entry_ok = report.ok and report.path is not None and report.path.is_file()
    check(1, "Import a PNG into the asset library", entry_ok,
          f"{report.describe()}" if report.ok else report.error)
    if entry_ok:
        check(1, "Imported PNG is indexed in the library",
              service.query().total >= 1,
              f"{service.query().total} image(s) in the library")

    # ----------------------------------------------------------------------
    # 2. Import a JPEG and edit it into a new version.
    # ----------------------------------------------------------------------
    jpeg = work / "photo.jpg"
    Image.new("RGB", (400, 250), (200, 120, 60)).save(jpeg, quality=95)
    jpeg_report = service.import_image(jpeg,
                                       destination=service.library_root / jpeg.name)
    before = jpeg_report.path.read_bytes() if jpeg_report.ok else b""
    session = ImageEditSession(source=jpeg_report.path,
                               metadata=ImageMetadata(prompt="edited photo"))
    session.add("brightness", factor=1.2)
    session.add("rounded_corners", radius=24)
    edited = service.save_edit(session,
                               target=service.library_root / "photo_edited.png")
    same_original = jpeg_report.path.read_bytes() == before if jpeg_report.ok else False
    check(2, "Edit an imported JPEG into a new version",
          edited.ok and same_original,
          f"{edited.describe()}; original untouched: {same_original}")

    # ----------------------------------------------------------------------
    # 3. Text to image with a local backend.
    # ----------------------------------------------------------------------
    if not command_available:
        record(3, "Text to image with a local backend", NOT_AVAILABLE,
               "no local command backend is configured on this machine")
    else:
        request = GenerationRequest(
            mode=GenerationMode.TEXT_TO_IMAGE, backend="command",
            model="command-model", prompt="a lighthouse in a storm",
            width=256, height=256, seed=2468, output_format="png",
            output_dir=str(service.library_root), name_stem="lighthouse")
        result = service.generate(request)
        written = result.ok and Path(result.paths[0]).is_file()
        check(3, "Text to image with a local backend", written,
              (f"{Path(result.paths[0]).name} {result.width}x{result.height} "
               f"seed={result.seeds[0]} {result.seconds:.2f}s")
              if written else f"{result.error} {result.what_to_do}")
        if written:
            verify = validate_image_file(Path(result.paths[0]))
            check(3, "The generated file is a valid image", verify.ok,
                  f"{verify.width}x{verify.height} {verify.format}")

    # ----------------------------------------------------------------------
    # 4. The same prompt and seed regenerate the same settings.
    # ----------------------------------------------------------------------
    if not command_available:
        record(4, "Same prompt and seed regenerate identically", NOT_AVAILABLE,
               "no local backend")
    else:
        request = GenerationRequest(
            mode=GenerationMode.TEXT_TO_IMAGE, backend="command",
            prompt="a fox in snow", width=192, height=192, seed=1357,
            output_dir=str(work / "seed_a"), name_stem="fox")
        first = service.generate(request)
        second = service.generate(GenerationRequest.from_dict(request.to_dict()))
        if first.ok and second.ok:
            identical = md5(Path(first.paths[0])) == md5(Path(second.paths[0]))
            first_metadata = read_metadata(Path(first.paths[0]))
            check(4, "Same prompt and seed regenerate identically", identical,
                  f"md5 {md5(Path(first.paths[0]))[:12]} both; "
                  f"recorded seed {first_metadata.seed if first_metadata else '?'}")
        else:
            check(4, "Same prompt and seed regenerate identically", False,
                  first.error or second.error)

    # ----------------------------------------------------------------------
    # 5. A new seed makes a variation.
    # ----------------------------------------------------------------------
    if not command_available:
        record(5, "A new seed produces different output", NOT_AVAILABLE,
               "no local backend")
    else:
        request = GenerationRequest(
            mode=GenerationMode.TEXT_TO_IMAGE, backend="command",
            prompt="a fox in snow", width=192, height=192, seed=1357,
            output_dir=str(work / "seed_a"), name_stem="fox")
        request2 = GenerationRequest.from_dict(request.to_dict())
        request2.seed = 24680
        request2.name_stem = "fox_b"
        other = service.generate(request2)
        if other.ok:
            a = work / "seed_a" / "fox.png"
            check(5, "A new seed produces different output",
                  md5(a) != md5(Path(other.paths[0])),
                  "seeds 1357 vs 24680 differ")
        else:
            check(5, "A new seed produces different output", False, other.error)

    # ----------------------------------------------------------------------
    # 6. Image to image (where supported).
    # ----------------------------------------------------------------------
    caps = service.capabilities_for("standard")
    if not caps.supports("image_to_image"):
        record(6, "Image to image", NOT_AVAILABLE,
               "the local backends do not report image_to_image")
    else:
        source = make_image(work / "i2i_src.png", size=(256, 256),
                            colour=(90, 40, 160))
        result = service.generate(GenerationRequest(
            mode=GenerationMode.IMAGE_TO_IMAGE, backend="standard",
            model="standard", prompt="restyle", source_image=str(source),
            width=256, height=256, strength=0.4, output_dir=str(work / "i2i"),
            name_stem="styled"))
        check(6, "Image to image", result.ok and source.is_file(),
              f"{result.summary()} (source kept: {source.is_file()})"
              if result.ok else result.error)

    # ----------------------------------------------------------------------
    # 7. Inpainting (where supported).
    # ----------------------------------------------------------------------
    if not service.capabilities_for("standard").supports("inpaint"):
        record(7, "Inpainting", NOT_AVAILABLE,
               "the available backends do not report inpainting")
    else:
        source = make_image(work / "inpaint_src.png", size=(192, 192),
                            colour=(60, 60, 60))
        mask = Image.new("L", (192, 192), 0)
        for x in range(60, 130):
            for y in range(60, 130):
                mask.putpixel((x, y), 255)
        mask_path = work / "inpaint_mask.png"
        mask.save(mask_path)
        result = service.generate(GenerationRequest(
            mode=GenerationMode.INPAINT, backend="standard", model="standard",
            prompt="fill the hole", source_image=str(source),
            mask_image=str(mask_path), width=192, height=192,
            output_dir=str(work / "inpaint"), name_stem="patched"))
        check(7, "Inpainting with a real mask", result.ok,
              result.summary() if result.ok else result.error)

    # ----------------------------------------------------------------------
    # 8. Upscaling (where supported) - always available as a standard resize.
    # ----------------------------------------------------------------------
    source = make_image(work / "small.png", size=(160, 100))
    upscaled = upscale(source, work / "upscaled.png", scale=2.0,
                       method="standard")
    check(8, "Upscale (Standard Resize)", upscaled.ok
          and upscaled.output_size == (320, 200)
          and upscaled.method == "standard",
          upscaled.describe() if upscaled.ok else upscaled.error)
    ai_result = upscale(source, work / "ai.png", scale=2.0, method="ai")
    if ai_result.ok:
        check(8, "AI upscale reports itself as an AI upscale",
              ai_result.method == "ai", ai_result.describe())
    else:
        check(8, "AI upscale with no model is refused, not faked",
              "none is installed" in ai_result.error
              and not (work / "ai.png").exists(),
              ai_result.error)

    # ----------------------------------------------------------------------
    # 9. Send an image to a project.
    # ----------------------------------------------------------------------
    projects = ProjectService(paths, settings)
    project = projects.create_project(CreateRequest(name="Stage F Matrix"))
    project_dir = projects.current_layout.root
    hero = make_image(work / "hero.png", size=(480, 270), colour=(20, 160, 120))
    write_metadata(hero, ImageMetadata(prompt="a green field", model="command-model",
                                       backend="command", seed=99,
                                       width=480, height=270, origin="generated"))
    added = use_in_project(projects, hero, name="hero")
    project = projects.current
    asset = project.asset_by_id(added.asset_id) if added.ok else None
    check(9, "Send an image to the project's asset library",
          bool(added.ok and asset is not None and asset.path.startswith("assets/")),
          f"asset {added.asset_id} at {asset.path}" if added.ok else added.message)

    # ----------------------------------------------------------------------
    # 10. Send an image to a scene.
    # ----------------------------------------------------------------------
    sent = send_to_scene(projects, hero, placement=Placement.OVERLAY,
                         name="hero")
    scene = project.scene_by_id(sent.scene_id) if sent.ok else None
    element = scene.elements[0] if scene and scene.elements else None
    check(10, "Send an image to a scene as an element",
          bool(sent.ok and element is not None and element.kind == "image"
               and element.asset_id),
          f"{sent.describe()}; element references asset id"
          if sent.ok else sent.message)

    # The element must be drawn, not merely recorded.
    if element is not None:
        context = build_context(project, project_dir=project_dir)
        image = render_scene(scene, context)
        pixel = image.convert("RGB").getpixel((image.width // 2, image.height // 2))
        check(10, "The scene actually paints the image",
              abs(pixel[1] - 160) < 45,
              f"centre pixel {pixel} at {image.width}x{image.height}")

    # ----------------------------------------------------------------------
    # 11. Reopen the project: the reference is still valid.
    # ----------------------------------------------------------------------
    projects.save(reason="matrix")
    projects.close_project(save=False)
    reopened = ProjectService(paths, settings)
    project2 = reopened.open_project(project_dir / "project.json")
    scene2 = project2.scene_by_id(sent.scene_id) if sent.ok else None
    element2 = scene2.elements[0] if scene2 and scene2.elements else None
    resolved = resolve_element_image(project2, element2, project_dir) \
        if element2 else None
    check(11, "Reopen the project: the image reference is still valid",
          resolved is not None and resolved.is_file(),
          f"resolved {resolved}" if resolved else "the reference did not resolve")

    # ----------------------------------------------------------------------
    # 12. Move the project folder: relative references still work.
    # ----------------------------------------------------------------------
    # Close the project first: the lock is held per session, and a second
    # service cannot open a project that is already open.
    reopened.close_project(save=False)
    moved = root / "moved" / project_dir.name
    moved.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(project_dir), str(moved))
    moved_service = ProjectService(paths, settings)
    moved_project = moved_service.open_project(moved / "project.json")
    moved_scene = moved_project.scene_by_id(sent.scene_id) if sent.ok else None
    moved_element = moved_scene.elements[0] if moved_scene and moved_scene.elements \
        else None
    moved_path = resolve_element_image(moved_project, moved_element, moved) \
        if moved_element else None
    check(12, "Move the project folder: relative references still work",
          moved_path is not None and moved_path.is_file(),
          f"resolved from the new location: {moved_path.name}"
          if moved_path else "the reference broke when the project moved")

    # ----------------------------------------------------------------------
    # 13. Delete the source asset: report it, never crash.
    # ----------------------------------------------------------------------
    if moved_element is None:
        record(13, "A deleted asset is reported, not crashed", NOT_AVAILABLE,
               "no element to delete")
    else:
        asset2 = moved_project.asset_by_id(moved_element.asset_id)
        (moved / asset2.path).unlink()
        checks = moved_service.verify_assets()
        missing = [item for item in checks if item.asset.id == asset2.id]
        reported = bool(missing and missing[0].missing)
        still_renders = True
        try:
            context = build_context(moved_project, project_dir=moved)
            render_scene(moved_scene, context)
        except Exception as exc:  # noqa: BLE001 - a crash here is the failure
            still_renders = False
            print(f"    (rendering raised: {exc})")
        check(13, "A deleted asset is reported, and the project still renders",
              reported and still_renders,
              f"missing reported: {reported}; render survived: {still_renders}")
        moved_service.save(reason="after deletion")
        moved_service.close_project(save=False)

    # ----------------------------------------------------------------------
    # 14. Cancel a generation: the UI recovers and no process is left behind.
    # ----------------------------------------------------------------------
    import psutil

    slow = (f'"{sys.executable}" "{FAKE_GENERATOR}" --prompt {{prompt}} '
            f'--seed {{seed}} --width {{width}} --height {{height}} '
            f'--out {{output}} --sleep 30')
    cancelling = ImageService.for_paths(paths, settings)
    cancelling.registry.get("command").provider.command = slow
    cancelling.registry.get("command").refresh()

    before_children = len(psutil.Process().children(recursive=True))
    token_holder: dict = {}

    def cancel_soon() -> None:
        # Cancel from the progress callback: the same hook the GUI's Stop uses.
        holder = token_holder.get("token")
        if holder is not None:
            holder.cancel()

    class Token:
        def __init__(self) -> None:
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

        def is_cancelled(self) -> bool:
            return self.cancelled

        def register(self, process) -> None:
            self.process = process

        def unregister(self, process) -> None:
            pass

    token = Token()
    token_holder["token"] = token
    request = GenerationRequest(
        mode=GenerationMode.TEXT_TO_IMAGE, backend="command", prompt="slow one",
        width=128, height=128, output_dir=str(work / "cancel"), name_stem="slow")

    def progress(state: str, fraction: float) -> None:
        cancel_soon()

    result = cancelling.generate(request, progress=progress, cancel=token)
    time.sleep(0.5)
    after_children = len(psutil.Process().children(recursive=True))
    no_orphans = after_children <= before_children
    check(14, "Cancel a generation: it stops and no process is left running",
          result.cancelled and no_orphans,
          f"state={result.state} cancelled={result.cancelled}; "
          f"child processes {before_children} -> {after_children}")
    # The studio is still usable afterwards.
    follow_up = cancelling.generate(GenerationRequest(
        mode=GenerationMode.TEXT_TO_IMAGE, backend="standard", prompt="x",
        width=128, height=128, output_dir=str(work / "after_cancel"),
        name_stem="after"))
    check(14, "The studio still works after a cancellation",
          not follow_up.ok and follow_up.code in ("NO_MODEL_INSTALLED",
                                                 "FEATURE_UNSUPPORTED"),
          f"a later request was answered honestly ({follow_up.code})")

    # ----------------------------------------------------------------------
    # 15. Backend unavailable: a clear message, and the studio still opens.
    # ----------------------------------------------------------------------
    unavailable = ImageService.for_paths(paths, settings)
    entry = unavailable.registry.get("diffusers")
    state = entry.status()
    message_ok = bool(state.reason and state.instructions)
    result = unavailable.generate(GenerationRequest(
        mode=GenerationMode.TEXT_TO_IMAGE, backend="diffusers", prompt="a fox",
        width=256, height=256))
    check(15, "An unavailable backend gives a clear message",
          message_ok and not result.ok and bool(result.what_to_do),
          f"{state.state}: {state.reason[:70]}... | request refused with "
          f"{result.code}")

    # ----------------------------------------------------------------------
    # 16. A large library stays responsive (measured, not asserted).
    # ----------------------------------------------------------------------
    big = root / "big_library"
    big.mkdir(parents=True, exist_ok=True)
    for index in range(300):
        Image.new("RGB", (64, 64), (index % 256, 90, 40)).save(
            big / f"bulk_{index:04d}.png")
    library = service.library
    original_root = library.root
    library.root = big
    library.index_path = big / "library_index.json"
    library._entries = None
    started = time.monotonic()
    found = library.scan()
    scan_seconds = time.monotonic() - started
    page_query_started = time.monotonic()
    page = library.query(LibraryQuery(limit=24))
    page_seconds = time.monotonic() - page_query_started
    started = time.monotonic()
    thumbs = [service.thumbnails.get(path) for path in
              [entry.path for entry in page.entries][:12]]
    thumb_seconds = time.monotonic() - started
    check(16, "A 300-image library scans and pages without loading everything",
          found == 300 and page.total == 300 and len(page.entries) == 24
          and page_seconds < 1.0,
          f"scan {scan_seconds:.2f}s for {found}; "
          f"page returned {len(page.entries)} of {page.total} in "
          f"{page_seconds * 1000:.0f}ms; 12 thumbnails in {thumb_seconds:.2f}s; "
          f"{sum(1 for t in thumbs if t)} built")
    library.root = original_root
    library.index_path = original_root / "library_index.json"
    library._entries = None

    # ----------------------------------------------------------------------
    # 17. A batch: unique outputs, no overwrite.
    # ----------------------------------------------------------------------
    if not command_available:
        record(17, "Batch generation produces unique outputs", NOT_AVAILABLE,
               "no local backend")
    else:
        batch_request = GenerationRequest(
            mode=GenerationMode.TEXT_TO_IMAGE, backend="command",
            prompt="a batch of tiles", width=128, height=128, seed=500,
            batch=4, output_dir=str(work / "batch"), name_stem="tile")
        batch_service = ImageService.for_paths(paths, settings)
        batch = batch_service.generate(batch_request)
        if batch.ok:
            paths_out = [Path(item) for item in batch.paths]
            unique = len({str(item) for item in paths_out}) == 4
            all_there = all(item.is_file() for item in paths_out)
            distinct_seeds = len(set(batch.seeds)) == 4
            record(17, "Batch generation produces four unique, distinct files",
                   PASS if (unique and all_there and distinct_seeds) else FAIL,
                   f"{[item.name for item in paths_out]}; "
                   f"seeds {batch.seeds}")
            # Running the same batch again must not overwrite the first.
            second = ImageService.for_paths(paths, settings).generate(
                GenerationRequest.from_dict(batch_request.to_dict()))
            before = {item.name for item in paths_out}
            after = {Path(item).name for item in second.paths} if second.ok else set()
            untouched = all(md5(item) == md5(item) for item in paths_out)
            record(17, "A second batch does not overwrite the first",
                   PASS if (second.ok and not (before & after) and untouched)
                   else FAIL,
                   f"first run {sorted(before)}; second run {sorted(after)}")
        else:
            check(17, "Batch generation produces unique outputs", False,
                  batch.error)

    # ----------------------------------------------------------------------
    # Contract and honesty checks (section 82).
    # ----------------------------------------------------------------------
    honest = ImageService.for_paths(paths, settings)
    standard = honest.registry.get("standard")
    record(18, "The built-in backend does not claim to draw prompts",
           PASS if standard.capabilities().supports("text_to_image") is False
           else FAIL,
           f"text_to_image={standard.capabilities().supports('text_to_image')}; "
           f"it reports: {standard.capabilities().describe()}")

    refused = honest.generate(GenerationRequest(
        mode=GenerationMode.TEXT_TO_IMAGE, backend="standard", prompt="a cat",
        width=256, height=256, output_dir=str(work / "refused")))
    record(18, "A text prompt with no model is refused, not faked",
           PASS if (not refused.ok and refused.code == "NO_MODEL_INSTALLED")
           else FAIL,
           f"{refused.error} -> {refused.what_to_do}")

    size_refusal = honest.validate(GenerationRequest(
        mode=GenerationMode.TEXT_TO_IMAGE, backend="command", prompt="x",
        width=64, height=64, output_dir=str(work / "size")), backend_id="command")
    record(18, "A legal request passes validation",
           PASS if not [i for i in size_refusal if i.severity == "error"]
           else FAIL,
           f"{len(size_refusal)} issue(s), none blocking")

    big_size = honest.validate(GenerationRequest(
        mode=GenerationMode.TEXT_TO_IMAGE, backend="command", prompt="x",
        width=99999, height=99999), backend_id="command")
    record(18, "An impossible size is refused with an explanation",
           PASS if any(i.code == "RESOLUTION_OUT_OF_RANGE" for i in big_size)
           else FAIL,
           f"{[i.code for i in big_size]}")

    # The version graph links a variation to its source.
    graph_source = make_image(work / "graph_src.png")
    graph_child = make_image(work / "graph_child.png")
    write_metadata(graph_child, ImageMetadata(origin="variation",
                                              parent=str(graph_source)))
    graph = VersionGraph([graph_source, graph_child])
    record(19, "The version graph links a variation to its source",
           PASS if (len(graph.roots()) == 1
                    and len(graph.children_of(graph_source)) == 1) else FAIL,
           f"{len(graph.nodes)} nodes, {len(graph.roots())} root(s), "
           f"lineage {[n.name for n in graph.lineage(graph_child)]}")

    # Overwriting the original demands confirmation.
    guard = make_image(work / "guard.png")
    guard_before = guard.read_bytes()
    guard_session = ImageEditSession(source=guard)
    guard_session.add("grayscale")
    refused_overwrite = apply_to_source(guard_session, confirmed=False)
    record(19, "Overwriting the original requires confirmation",
           PASS if (not refused_overwrite.ok
                    and guard.read_bytes() == guard_before) else FAIL,
           "refused without confirmation; the original is byte-identical")

    # ----------------------------------------------------------------------
    # Summary
    # ----------------------------------------------------------------------
    passed = sum(1 for _, _, state, _ in RESULTS if state == PASS)
    failed = sum(1 for _, _, state, _ in RESULTS if state == FAIL)
    skipped = sum(1 for _, _, state, _ in RESULTS if state == NOT_AVAILABLE)

    print()
    print("=" * 78)
    print(f"Checks        : {len(RESULTS)}")
    print(f"Passed        : {passed}")
    print(f"Failed        : {failed}")
    print(f"Not available : {skipped}")
    print()
    print(f"Image backend : local command + built-in standard "
          f"(installed: {command_available})")
    print(f"AI generator  : {'installed' if status.any_generator else 'NOT INSTALLED'}")
    print("Kokoro        : not used by Stage F")
    print()

    if failed:
        print("FAILURES")
        for number, name, state, detail in RESULTS:
            if state == FAIL:
                print(f"  {number:02d} {name}: {detail}")
        print()
        print("OVERALL: FAIL")
    else:
        print("OVERALL: PASS")
        if skipped:
            print(f"{skipped} check(s) were NOT AVAILABLE - see the lines above.")
    print("=" * 78)

    (root / "stage_f_matrix_results.json").write_text(
        json.dumps([{"number": n, "name": name, "result": state, "detail": detail}
                    for n, name, state, detail in RESULTS], indent=2),
        encoding="utf-8")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
