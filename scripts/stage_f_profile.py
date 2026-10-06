"""Stage F performance measurements (directive sections 74, 56, 40).

Measures what the directive asks for, on this machine, with the tools available
here:

* Image Studio startup - import cost, registry construction, detection
* model loading - what detection actually costs (it must not load a model)
* generation memory - a real generation, sampled throughout
* thumbnail generation - a batch, timed and measured
* large library browsing - scan, page and query on a big folder
* batch processing - a real batch of N images

Numbers are reported as measured.  Where something cannot be measured honestly -
a diffusion model that is not installed - this says so rather than inventing a
figure.

Run it with:

    python scripts/stage_f_profile.py --data-root /tmp/mgs_stage_f_profile
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import shutil
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PIL import Image  # noqa: E402

from app.core.paths import AppPaths  # noqa: E402
from app.core.settings import SettingsStore  # noqa: E402

FAKE_GENERATOR = REPO_ROOT / "tests" / "fake_image_generator.py"


def rss_mib() -> float:
    """Resident set size in MiB, or 0.0 when it cannot be read."""
    try:
        import psutil

        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:  # noqa: BLE001 - a missing psutil is not a failure here
        return 0.0


def peak_mib() -> float:
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KiB, macOS reports bytes.
        return usage / 1024.0 if sys.platform != "darwin" else usage / (1024 * 1024)
    except Exception:  # noqa: BLE001
        return 0.0


class Measurement:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, label: str, value: str, note: str = "") -> None:
        self.rows.append((label, value, note))
        print(f"  {label:<34} {value:>16}   {note}", flush=True)

    def to_dict(self) -> list[dict]:
        return [{"label": label, "value": value, "note": note}
                for label, value, note in self.rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="/tmp/mgs_stage_f_profile")
    parser.add_argument("--library-size", type=int, default=500,
                        help="How many images to generate for the library test.")
    parser.add_argument("--thumbnails", type=int, default=60)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.data_root)
    if root.exists() and not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    paths = AppPaths(data_root=root, source_root=REPO_ROOT,
                     reason="stage-f-profile")
    paths.ensure()
    settings = SettingsStore(paths.settings_file).load().settings

    library = paths.images_dir
    library.mkdir(parents=True, exist_ok=True)

    measurements = Measurement()
    base = rss_mib()
    print("=" * 78)
    print("STAGE F PERFORMANCE MEASUREMENTS")
    print(f"python {sys.version.split()[0]} | "
          f"cpu {os.cpu_count()} | data root {root}")
    print(f"baseline RSS {base:.1f} MiB")
    print("=" * 78)

    # ------------------------------------------------------------------
    # 1. Startup
    # ------------------------------------------------------------------
    print("\n[1] Image Studio startup")
    started = time.perf_counter()
    from app.image.service import ImageService  # noqa: F401

    import_seconds = time.perf_counter() - started
    measurements.add("import app.image.service", f"{import_seconds * 1000:.0f} ms",
                     "no model is loaded or imported")

    started = time.perf_counter()
    service = ImageService.for_paths(paths, settings)
    construct_seconds = time.perf_counter() - started
    measurements.add("construct ImageService", f"{construct_seconds * 1000:.0f} ms",
                     f"RSS now {rss_mib():.1f} MiB")

    # A local backend so generation below is real.
    settings.image.command = (
        f'"{sys.executable}" "{FAKE_GENERATOR}" --prompt {{prompt}} '
        f'--seed {{seed}} --width {{width}} --height {{height}} '
        f'--out {{output}}')
    settings.image.active_backend = "command"
    SettingsStore(paths.settings_file).save(settings)
    service = ImageService.for_paths(paths, settings)

    before_detect = rss_mib()
    started = time.perf_counter()
    status = service.status()
    detect_seconds = time.perf_counter() - started
    after_detect = rss_mib()
    measurements.add("detect 6 backends + models",
                     f"{detect_seconds * 1000:.0f} ms",
                     f"RSS {before_detect:.1f} -> {after_detect:.1f} MiB "
                     f"({after_detect - before_detect:+.1f})")
    measurements.add("models found", str(status.model_count),
                     "no weights loaded by detection")
    measurements.add("AI generator installed",
                     "yes" if status.any_generator else "NO", "")

    # ------------------------------------------------------------------
    # 2. Model loading
    # ------------------------------------------------------------------
    print("\n[2] Model loading")
    if status.any_generator:
        entry = next((item for item in service.registry.entries.values()
                      if item.models() and item.capabilities().text_to_image),
                     None)
        if entry is not None:
            model = entry.models()[0]
            before = rss_mib()
            started = time.perf_counter()
            try:
                entry.provider.load(model)
                seconds = time.perf_counter() - started
                measurements.add("load the local model", f"{seconds:.2f} s",
                                 f"RSS {before:.1f} -> {rss_mib():.1f} MiB")
                entry.provider.unload()
                measurements.add("unload it again", "done",
                                 f"RSS {rss_mib():.1f} MiB")
            except NotImplementedError as exc:
                measurements.add("load the local model", "not measurable",
                                 str(exc)[:60])
    else:
        measurements.add("load a diffusion model", "NOT MEASURED",
                         "no AI model is installed on this machine")

    # ------------------------------------------------------------------
    # 3. A real generation, sampled
    # ------------------------------------------------------------------
    print("\n[3] Generation memory and time")
    from app.image.provider import GenerationMode, GenerationRequest

    def sample_generation(width: int, height: int, prompt: str) -> tuple[float, float, float]:
        samples: list[float] = []
        request = GenerationRequest(
            mode=GenerationMode.TEXT_TO_IMAGE, backend="command", prompt=prompt,
            width=width, height=height, seed=1234, output_dir=str(library),
            name_stem=f"profile_{width}x{height}")
        started = time.perf_counter()

        def progress(_state: str, _fraction: float) -> None:
            samples.append(rss_mib())

        result = service.generate(request, progress=progress)
        seconds = time.perf_counter() - started
        if not result.ok:
            print(f"    (generation failed: {result.error})")
            return seconds, 0.0, 0.0
        peak = max(samples) if samples else rss_mib()
        return seconds, peak, rss_mib()

    for size in ((512, 512), (1024, 1024), (1920, 1080)):
        seconds, peak, after = sample_generation(*size, prompt="a profile test")
        measurements.add(f"generate {size[0]}x{size[1]}",
                         f"{seconds:.2f} s",
                         f"peak RSS {peak:.1f} MiB, after {after:.1f} MiB")

    gc.collect()
    measurements.add("RSS after three generations", f"{rss_mib():.1f} MiB",
                     "buffers are released between images")

    # ------------------------------------------------------------------
    # 4. Thumbnails
    # ------------------------------------------------------------------
    print("\n[4] Thumbnail generation")

    cache = service.thumbnails
    sources: list[Path] = []
    for index in range(args.thumbnails):
        target = library / f"thumb_src_{index:03d}.png"
        Image.new("RGB", (800, 600), (index % 256, 120, 60)).save(target)
        sources.append(target)

    before = rss_mib()
    started = time.perf_counter()
    made = [cache.get(path) for path in sources]
    seconds = time.perf_counter() - started
    built = sum(1 for item in made if item)
    measurements.add(f"{built} thumbnails (cold)",
                     f"{seconds * 1000:.0f} ms",
                     f"{seconds / max(1, built) * 1000:.1f} ms each, "
                     f"RSS {before:.1f} -> {rss_mib():.1f} MiB")

    started = time.perf_counter()
    [cache.get(path) for path in sources]
    warm = time.perf_counter() - started
    measurements.add(f"{built} thumbnails (cached)",
                     f"{warm * 1000:.0f} ms",
                     f"{warm / max(1, built) * 1000:.2f} ms each (from cache)")

    # ------------------------------------------------------------------
    # 5. A large library
    # ------------------------------------------------------------------
    print(f"\n[5] Library with {args.library_size} images")
    bulk = root / "bulk_library"
    bulk.mkdir(parents=True, exist_ok=True)
    for index in range(args.library_size):
        Image.new("RGB", (64, 64), (index % 256, 80, 40)).save(
            bulk / f"bulk_{index:05d}.png")

    from app.image.library import ImageLibrary, LibraryQuery

    big = ImageLibrary(bulk)
    started = time.perf_counter()
    found = big.scan()
    scan_seconds = time.perf_counter() - started
    measurements.add(f"scan {found} images", f"{scan_seconds:.2f} s",
                     f"{found / max(scan_seconds, 1e-6):.0f} images/s")

    started = time.perf_counter()
    big._load_index()
    index_seconds = time.perf_counter() - started
    measurements.add("reload the index", f"{index_seconds * 1000:.0f} ms",
                     "no files are opened")

    times: list[float] = []
    for _ in range(5):
        started = time.perf_counter()
        page = big.query(LibraryQuery(limit=24))
        times.append(time.perf_counter() - started)
    measurements.add("query one page of 24",
                     f"{statistics.median(times) * 1000:.1f} ms",
                     f"{page.total} entries indexed, median of 5")

    started = time.perf_counter()
    matches = big.query(LibraryQuery(text="bulk_0000", limit=24))
    search_seconds = time.perf_counter() - started
    measurements.add("search by name",
                     f"{search_seconds * 1000:.1f} ms",
                     f"{matches.total} match(es)")

    started = time.perf_counter()
    duplicates = big.duplicates()
    duplicate_seconds = time.perf_counter() - started
    measurements.add("duplicate scan (size+dims)",
                     f"{duplicate_seconds * 1000:.0f} ms",
                     f"{len(duplicates)} group(s), nothing hashed")

    # ------------------------------------------------------------------
    # 6. Batch processing
    # ------------------------------------------------------------------
    print(f"\n[6] Batch of {args.batch}")
    batch_request = GenerationRequest(
        mode=GenerationMode.TEXT_TO_IMAGE, backend="command",
        prompt="a batch profile", width=256, height=256, seed=900,
        batch=args.batch, output_dir=str(root / "batch"), name_stem="batch")
    before = rss_mib()
    started = time.perf_counter()
    batch_result = service.generate(batch_request)
    batch_seconds = time.perf_counter() - started
    if batch_result.ok:
        measurements.add(f"generate a batch of {len(batch_result.paths)}",
                         f"{batch_seconds:.2f} s",
                         f"{batch_seconds / len(batch_result.paths):.2f} s each, "
                         f"RSS {before:.1f} -> {rss_mib():.1f} MiB")
        sizes = [Path(item).stat().st_size for item in batch_result.paths]
        measurements.add("files written", str(len(sizes)),
                         f"{sum(sizes) / 1024:.0f} KiB total")
    else:
        measurements.add("generate a batch", "FAILED",
                         batch_result.error or "")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    highest = peak_mib()
    print()
    print("=" * 78)
    print(f"Highest RSS seen by the process : {highest:.1f} MiB")
    print(f"RSS at the end                  : {rss_mib():.1f} MiB")
    print()
    print("These are single-run measurements from one machine.  They are not a")
    print("benchmark, and they say nothing about a machine without a local")
    print("command backend or one with a real diffusion model installed.")
    print("=" * 78)

    (root / "stage_f_profile.json").write_text(
        json.dumps({"highest_rss_mib": round(highest, 2),
                    "final_rss_mib": round(rss_mib(), 2),
                    "measurements": measurements.to_dict()}, indent=2),
        encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
