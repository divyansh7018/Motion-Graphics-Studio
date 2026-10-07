"""Stage G tests: the Video Library - indexing, search, metadata, thumbnails.

Sections 14, 15, 25, 29, 30, 31, 42, 98.  Everything here works on real (tiny)
MP4 files written with FFmpeg, so the numbers in an entry are the numbers
FFprobe read back - not a fixture's opinion.  No test in this module claims an AI
model ran: the clips that come from a backend come from a labelled test one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ai.service import AIService
from app.ai.video_library import (ENTRY_MISSING, ENTRY_READY, LIBRARY_SORTS,
                                  LIBRARY_SOURCES, MediaProbeCache,
                                  VideoLibrary, VideoThumbnailCache,
                                  probe_video_file)
from app.ai.video_library_jobs import (library_import_body, library_recheck_body,
                                       library_remove_body, library_scan_body,
                                       library_thumbnails_body,
                                       video_library_service)
from app.jobs.spec import JobContext

from tests.ai_fakes import FakeVideoBackend, _tiny_mp4, ffmpeg_tools, write_png


TOOLS = ffmpeg_tools()


def make_clip(folder: Path, name: str = "clip.mp4", *, width: int = 160,
              height: int = 96, fps: int = 12, seconds: float = 2.0) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    return _tiny_mp4(folder / name, width=width, height=height, fps=fps,
                     seconds=seconds)


@pytest.fixture()
def no_ffmpeg(monkeypatch):
    """A machine with no FFmpeg anywhere, not even on the PATH."""
    from app.tools.ffmpeg import FFmpegDiscovery

    class NoTools:
        ffmpeg = None
        ffprobe = None
        discovery = FFmpegDiscovery()

    monkeypatch.setattr("app.tools.ffmpeg.discover_ffmpeg",
                        lambda *a, **k: FFmpegDiscovery())
    return NoTools()


@pytest.fixture()
def library(tmp_path) -> VideoLibrary:
    found = VideoLibrary(tmp_path / "videos", tools=TOOLS)
    found.ensure_root()
    return found


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


class _Progress:
    class _P:
        total = 0.0

    def __init__(self) -> None:
        self.progress = self._P()
        self.updates = 0

    def start(self, **kwargs) -> None:
        self.progress.total = float(kwargs.get("total", 1.0))

    def update(self, **kwargs) -> None:
        self.updates += 1


def context_for(library: VideoLibrary, /, **payload) -> JobContext:
    return JobContext(
        job_id="test-job", key="video.library.scan", cancel=_Never(),
        progress=_Progress(), settings=None, paths=None,
        payload={"library": library, "tools": TOOLS, **payload})


# ---------------------------------------------------------------------------
# scanning and measuring (sections 14, 15)
# ---------------------------------------------------------------------------

def test_a_scanned_clip_carries_measured_numbers(library, tmp_path) -> None:
    make_clip(tmp_path / "videos", "measured.mp4", width=160, height=96,
              fps=12, seconds=2.0)
    assert library.scan() == 1
    entry = library.all()[0]
    assert entry.filename == "measured.mp4"
    assert (entry.width, entry.height) == (160, 96)
    assert entry.fps == pytest.approx(12.0, abs=0.5)
    assert entry.duration == pytest.approx(2.0, abs=0.2)
    assert entry.codec == "h264"
    assert entry.size_bytes > 0
    assert entry.measured_with, "the report says what measured it"
    assert entry.status == ENTRY_READY
    assert "160x96" in entry.measurements()


def test_scanning_twice_does_not_double_the_index(library, tmp_path) -> None:
    make_clip(tmp_path / "videos", "once.mp4")
    library.scan()
    library.scan()
    assert library.counts()["total"] == 1
    on_disk = json.loads(Path(library.index_path).read_text(encoding="utf-8"))
    assert len(on_disk["videos"]) == 1


def test_an_index_written_before_the_first_read_does_not_double(library,
                                                               tmp_path) -> None:
    """The regression the history had: writers must read before they write."""
    make_clip(tmp_path / "videos", "one.mp4")
    library.scan()
    second = VideoLibrary(tmp_path / "videos", tools=TOOLS)
    make_clip(tmp_path / "videos", "two.mp4")
    second.scan()          # never read the file first
    assert third_count(tmp_path) == 2


def third_count(tmp_path: Path) -> int:
    reopened = VideoLibrary(tmp_path / "videos", tools=TOOLS)
    return len(reopened.all())


def test_a_measurement_is_not_repeated_while_the_file_is_unchanged(library,
                                                                   tmp_path) -> None:
    path = make_clip(tmp_path / "videos", "cached.mp4")
    library.scan()
    first = library.all()[0]
    assert library.probes.cached(path), "the measurement was cached"

    # Measuring again is the same answer, and the file was not touched.
    before = path.stat().st_mtime_ns
    library.recheck(first.id)
    assert path.stat().st_mtime_ns == before
    assert library.all()[0].duration == pytest.approx(first.duration, abs=0.01)


def test_replacing_a_file_invalidates_its_measurement(library, tmp_path) -> None:
    path = make_clip(tmp_path / "videos", "swapped.mp4", seconds=1.0)
    library.scan()
    assert library.all()[0].duration == pytest.approx(1.0, abs=0.2)
    # A different clip at the same path: same name, new content.
    path.unlink()
    make_clip(tmp_path / "videos", "swapped.mp4", seconds=3.0, fps=8)
    assert library.probes.cached(path) is None, "a changed file is not cached"
    entry = library.recheck(library.all()[0].id)
    assert entry.duration == pytest.approx(3.0, abs=0.2)
    assert entry.fps == pytest.approx(8.0, abs=0.5)


def test_a_file_that_cannot_be_measured_is_still_listed(library, tmp_path) -> None:
    """A file that is not a video is listed as unreadable, not hidden."""
    from app.ai.video_library import ENTRY_UNREADABLE, probe_state

    broken = tmp_path / "videos" / "broken.mp4"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"this is not a video")
    assert probe_state(broken, TOOLS)[0] == "unreadable"
    library.scan()
    entry = library.all()[0]
    assert entry.status == ENTRY_UNREADABLE
    assert entry.measured_with == ""
    assert entry.size_bytes == len(b"this is not a video")


def test_without_ffprobe_a_clip_is_not_called_broken(library, tmp_path,
                                                     no_ffmpeg) -> None:
    """No FFprobe means "not measured here", never "your clip is bad"."""
    from app.ai.video_library import ENTRY_READY, probe_state

    path = make_clip(tmp_path / "videos", "fine.mp4")
    assert probe_state(path, no_ffmpeg)[0] == "check_not_available"
    library.tools = no_ffmpeg
    library.scan()
    entry = library.all()[0]
    assert entry.status == ENTRY_READY
    assert entry.measured_with == ""
    assert entry.resolution() == "" and entry.duration_label() == ""
    assert entry.fps == 0.0 and entry.codec == "", "no numbers are invented"
    assert entry.size_bytes > 0, "but the size comes from the file itself"


def test_a_library_on_a_machine_without_ffprobe_still_lists_and_searches(
        library, tmp_path, no_ffmpeg) -> None:
    """Everything except the measured numbers keeps working with no FFmpeg."""
    library.tools = no_ffmpeg
    library.add(make_clip(tmp_path / "videos", "clip.mp4"), source="generated",
                prompt="a harbour")
    from app.ai.video_library import LibraryQuery

    assert library.counts()["total"] == 1
    assert library.describe().startswith("1 clip(s)")
    assert len(library.query(LibraryQuery(text="harbour")).entries) == 1
    assert library.thumbnail_for(library.all()[0].id) is None, \
        "no FFmpeg means no picture, and the entry is unaffected"


def test_a_clip_whose_file_has_gone_is_marked_not_dropped(library, tmp_path) -> None:
    path = make_clip(tmp_path / "videos", "vanishing.mp4")
    library.scan()
    path.unlink()
    assert library.refresh_exists() == 1
    entry = library.all()[0]
    assert entry.status == ENTRY_MISSING
    assert entry.id, "the entry is kept so the user can find it again"
    assert [item.id for item in library.missing()] == [entry.id]


def test_a_file_that_comes_back_is_marked_ready_again(library, tmp_path) -> None:
    path = make_clip(tmp_path / "videos", "back.mp4")
    library.scan()
    saved = Path(path).read_bytes()
    path.unlink()
    library.refresh_exists()
    path.write_bytes(saved)
    assert library.refresh_exists() == 1
    assert library.all()[0].status == ENTRY_READY


def test_only_videos_are_indexed(library, tmp_path) -> None:
    folder = tmp_path / "videos"
    make_clip(folder, "real.mp4")
    write_png(folder / "picture.png")
    (folder / "notes.txt").write_text("not a clip", encoding="utf-8")
    assert library.scan() == 1
    assert [entry.filename for entry in library.all()] == ["real.mp4"]


def test_probe_video_file_says_nothing_when_it_cannot_measure(tmp_path) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"nope")
    assert probe_video_file(broken, TOOLS) == {}


# ---------------------------------------------------------------------------
# kinds of clip stay distinct (section 30)
# ---------------------------------------------------------------------------

def test_the_three_kinds_of_clip_are_kept_apart(library, tmp_path) -> None:
    generated = make_clip(tmp_path / "videos", "generated.mp4")
    render = make_clip(tmp_path / "videos", "final_render.mp4")
    imported = make_clip(tmp_path / "videos", "holiday.mp4")
    library.add(generated, source="generated", backend="fake_video", model="m")
    library.add(render)
    library.add(imported)
    assert library.find(library.for_path(render).id) is not None
    assert library.for_path(render).source == "render", \
        "a finished render is not called generated"
    assert library.for_path(imported).source == "imported"
    counts = library.counts()
    assert counts["generated"] == 1 and counts["render"] == 1
    assert counts["imported"] == 1


def test_a_generated_clip_keeps_its_provenance(library, tmp_path) -> None:
    path = make_clip(tmp_path / "videos", "made.mp4")
    entry = library.add(path, source="generated", backend="fake_video",
                        model="fake-model", prompt="a harbour at dawn",
                        seed=42, label="TEST BACKEND (fixture)")
    assert entry.provenance().startswith("Generated by")
    assert "TEST BACKEND" in entry.provenance()
    from app.ai.video_library import LibraryQuery

    assert [e.id for e in library.query(LibraryQuery(text="harbour")).entries] \
        == [entry.id]


def test_the_metadata_a_project_gets_says_which_kind_it_was(library,
                                                            tmp_path) -> None:
    generated = library.add(make_clip(tmp_path / "videos", "made.mp4"),
                            source="generated", backend="fake_video",
                            prompt="x", seed=3)
    imported = library.add(make_clip(tmp_path / "videos", "mine.mp4"))
    assert generated.to_metadata()["generated"] is True
    assert generated.to_metadata()["prompt"] == "x"
    assert imported.to_metadata()["generated"] is False, \
        "an imported clip must not claim to have been generated"


# ---------------------------------------------------------------------------
# search, sort, filter, page (section 14)
# ---------------------------------------------------------------------------

def test_search_looks_at_names_prompts_models_and_tags(library, tmp_path) -> None:
    first = library.add(make_clip(tmp_path / "videos", "harbour.mp4"),
                        source="generated", prompt="a quiet harbour",
                        model="sdxl-turbo")
    second = library.add(make_clip(tmp_path / "videos", "forest.mp4"),
                         source="generated", prompt="a dark forest",
                         model="animatediff")
    library.add_tag(second.id, "nature")
    from app.ai.video_library import LibraryQuery

    assert [e.id for e in library.query(LibraryQuery(text="harbour")).entries] \
        == [first.id]
    assert [e.id for e in library.query(LibraryQuery(text="forest")).entries] \
        == [second.id]
    assert [e.id for e in library.query(LibraryQuery(text="sdxl")).entries] \
        == [first.id]
    assert [e.id for e in library.query(LibraryQuery(tag="nature")).entries] \
        == [second.id]


def test_filters_and_sorting_are_applied(library, tmp_path) -> None:
    short = library.add(make_clip(tmp_path / "videos", "short.mp4", seconds=1.0),
                        source="generated")
    long_clip = library.add(
        make_clip(tmp_path / "videos", "long.mp4", seconds=4.0), source="render")
    library.set_favourite(short.id, True)
    from app.ai.video_library import LibraryQuery

    assert [e.id for e in library.query(LibraryQuery(source="render")).entries] \
        == [long_clip.id]
    assert [e.id for e in library.query(
        LibraryQuery(favourites_only=True)).entries] == [short.id]
    assert [e.id for e in library.query(LibraryQuery(sort="duration")).entries][0] \
        == long_clip.id
    assert [e.id for e in library.query(LibraryQuery(sort="name")).entries][0] \
        == long_clip.id
    assert len(LIBRARY_SORTS) >= 5
    assert all(isinstance(label, str) and label for label in LIBRARY_SORTS.values())
    assert LIBRARY_SOURCES == ("generated", "render", "imported")


def test_paging_reports_the_total_and_does_not_load_everything(library,
                                                               tmp_path) -> None:
    for index in range(6):
        library.add(make_clip(tmp_path / "videos", f"clip_{index}.mp4",
                              seconds=1.0 + index))
    from app.ai.video_library import LibraryQuery

    page = library.query(LibraryQuery(limit=2))
    assert len(page.entries) == 2
    assert page.total == 6
    assert page.has_more is True
    second = library.query(LibraryQuery(limit=2, offset=4))
    assert len(second.entries) == 2 and second.has_more is False


def test_collections_and_tags_are_listed(library, tmp_path) -> None:
    entry = library.add(make_clip(tmp_path / "videos", "one.mp4"))
    library.set_collection(entry.id, "Shorts")
    library.set_tags(entry.id, ["blue", "evening"])
    assert library.collections() == ["Shorts"]
    assert library.tags() == ["blue", "evening"]
    assert library.describe().startswith("1 clip(s)")


def test_rename_changes_the_library_name_not_the_file(library, tmp_path) -> None:
    path = make_clip(tmp_path / "videos", "original_name.mp4")
    entry = library.add(path)
    assert library.rename(entry.id, "Harbour at dawn") is True
    assert library.find(entry.id).name == "Harbour at dawn"
    assert path.is_file(), "the file on disk keeps its own name"


# ---------------------------------------------------------------------------
# thumbnails (sections 15, 29, 42)
# ---------------------------------------------------------------------------

def test_a_thumbnail_is_made_from_the_clip_and_cached(library, tmp_path) -> None:
    path = make_clip(tmp_path / "videos", "shot.mp4")
    entry = library.add(path)
    picture = library.thumbnail_for(entry.id)
    assert picture is not None and picture.is_file()
    assert picture.stat().st_size > 0
    assert library.find(entry.id).thumbnail == str(picture)
    # The second ask is the cached file, unchanged.
    again = library.thumbnail_for(entry.id)
    assert again == picture
    assert again.stat().st_mtime_ns == picture.stat().st_mtime_ns


def test_a_thumbnail_cache_is_keyed_to_the_file(library, tmp_path) -> None:
    path = make_clip(tmp_path / "videos", "shot.mp4", seconds=1.0)
    entry = library.add(path)
    first = library.thumbnail_for(entry.id)
    path.unlink()
    make_clip(tmp_path / "videos", "shot.mp4", seconds=3.0, width=320, height=192)
    second = library.thumbnail_for(entry.id, regenerate=True)
    assert second is not None and second != first, \
        "a replaced clip must not show the old picture"


def test_a_thumbnail_of_a_broken_file_fails_quietly(tmp_path) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video at all")
    cache = VideoThumbnailCache(tmp_path / "thumbs")
    assert cache.get(broken, TOOLS) is None


def test_no_ffmpeg_means_no_thumbnail_and_no_crash(tmp_path, no_ffmpeg) -> None:
    path = make_clip(tmp_path, "clip.mp4")
    cache = VideoThumbnailCache(tmp_path / "thumbs")
    assert cache.get(path, no_ffmpeg) is None, \
        "without FFmpeg there is no picture, and that is not an error"
    assert cache.get(path, None) is None, \
        "and the library does not go looking for one by itself either"


def test_clearing_thumbnails_keeps_the_clips(library, tmp_path) -> None:
    path = make_clip(tmp_path / "videos", "kept.mp4")
    entry = library.add(path)
    library.thumbnail_for(entry.id)
    removed = library.clear_thumbnails()
    assert removed >= 1
    assert path.is_file()
    assert library.find(entry.id).thumbnail == ""
    assert library.find(entry.id).status == ENTRY_READY


# ---------------------------------------------------------------------------
# deleting (sections 31, 98)
# ---------------------------------------------------------------------------

def test_removing_an_entry_keeps_the_file_unless_asked(library, tmp_path) -> None:
    path = make_clip(tmp_path / "videos", "mine.mp4")
    entry = library.add(path)
    assert library.remove(entry.id) is True
    assert path.is_file()
    assert library.all() == []


def test_deleting_can_remove_the_file_when_it_is_asked_for(library, tmp_path) -> None:
    path = make_clip(tmp_path / "videos", "gone.mp4")
    entry = library.add(path)
    assert library.remove(entry.id, delete_file=True) is True
    assert not path.exists()


def test_the_library_never_overwrites_a_file_it_indexes(library, tmp_path) -> None:
    path = make_clip(tmp_path / "videos", "twice.mp4")
    before = path.read_bytes()
    library.add(path)
    library.add(path)                 # the same file again
    assert path.read_bytes() == before
    assert library.counts()["total"] == 1, "the same file is the same entry"


# ---------------------------------------------------------------------------
# the jobs (sections 34, 41, 42)
# ---------------------------------------------------------------------------

def test_the_scan_job_indexes_and_reports(library, tmp_path) -> None:
    make_clip(tmp_path / "videos", "one.mp4")
    make_clip(tmp_path / "videos", "two.mp4")
    outcome = library_scan_body(context_for(library))
    assert outcome["ok"] is True
    assert outcome["found"] == 2 and outcome["added"] == 2
    assert "2 clip(s)" in outcome["message"]
    assert len(outcome["videos"]) == 2
    assert outcome["videos"][0]["measurements"]


def test_the_import_job_adds_one_file(library, tmp_path) -> None:
    path = make_clip(tmp_path, "outside.mp4")
    outcome = library_import_body(context_for(library, path=str(path),
                                              source="render"))
    assert outcome["ok"] is True
    assert outcome["entry"]["source"] == "render"
    assert library.for_path(path) is not None


def test_importing_a_file_that_is_not_there_says_so(library, tmp_path) -> None:
    outcome = library_import_body(context_for(library, path=str(tmp_path / "no.mp4")))
    assert outcome["ok"] is False
    assert "not there" in outcome["message"]
    assert outcome["what_to_do"]


def test_the_thumbnail_job_makes_pictures_and_reports_failures(library,
                                                               tmp_path) -> None:
    library.add(make_clip(tmp_path / "videos", "good.mp4"))
    broken = tmp_path / "videos" / "broken.mp4"
    broken.write_bytes(b"not a video")
    library.add(broken)
    outcome = library_thumbnails_body(context_for(library))
    assert outcome["made"] == 1
    assert len(outcome["failed"]) == 1
    assert outcome["failed"][0]["reason"]
    assert "could not be read" in outcome["message"]


def test_the_thumbnail_job_only_does_what_it_was_asked_for(library,
                                                           tmp_path) -> None:
    first = library.add(make_clip(tmp_path / "videos", "one.mp4"))
    second = library.add(make_clip(tmp_path / "videos", "two.mp4"))
    outcome = library_thumbnails_body(context_for(library, ids=[second.id]))
    assert outcome["made"] == 1
    assert library.find(second.id).thumbnail
    assert library.find(first.id).thumbnail == ""


def test_the_remove_job_reports_what_it_did(library, tmp_path) -> None:
    path = make_clip(tmp_path / "videos", "removed.mp4")
    entry = library.add(path)
    outcome = library_remove_body(context_for(library, ids=[entry.id],
                                              delete_file=False))
    assert outcome["removed"] == ["removed.mp4"]
    assert path.is_file()
    assert "left where they are" in outcome["message"]


def test_the_remove_job_can_delete_files_when_told_to(library, tmp_path) -> None:
    path = make_clip(tmp_path / "videos", "gone.mp4")
    entry = library.add(path)
    outcome = library_remove_body(context_for(library, ids=[entry.id],
                                              delete_file=True))
    assert outcome["deleted_files"] is True
    assert not path.exists()


def test_the_recheck_job_measures_again(library, tmp_path) -> None:
    path = make_clip(tmp_path / "videos", "again.mp4", seconds=1.0)
    entry = library.add(path)
    path.unlink()
    make_clip(tmp_path / "videos", "again.mp4", seconds=4.0)
    outcome = library_recheck_body(context_for(library, id=entry.id))
    assert outcome["ok"] is True
    assert "4" in outcome["message"] or "4.0" in str(outcome["entry"]["duration"])


def test_scanning_one_folder_only_touches_that_folder(library, tmp_path) -> None:
    make_clip(tmp_path / "videos", "home.mp4")
    elsewhere = tmp_path / "elsewhere"
    make_clip(elsewhere, "away.mp4")
    outcome = library_scan_body(context_for(library, folder=str(elsewhere)))
    assert outcome["found"] == 1
    assert [entry.filename for entry in library.all()] == ["away.mp4"]


# ---------------------------------------------------------------------------
# the studio keeps the library up to date (sections 22, 25)
# ---------------------------------------------------------------------------

def test_a_generated_clip_appears_in_the_library_with_its_provenance(paths,
                                                                    settings,
                                                                    tmp_path) -> None:
    from app.ai.registry import AIBackendManager
    from tests.ai_fakes import make_video_request

    service = AIService(settings, paths=paths, data_root=tmp_path,
                        manager=AIBackendManager(settings, data_root=tmp_path,
                                                 extra_backends=[
                                                     FakeVideoBackend(
                                                         "fake_video")]))
    service.status()
    request = make_video_request(tmp_path, prompt="a harbour at dawn", seed=17)
    result = service.generate_video(request, backend_id="fake_video")
    assert result.ok
    entries = service.library.all()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.path == str(result.path)
    assert entry.source == "generated"
    assert entry.prompt == "a harbour at dawn"
    assert entry.seed == 17
    assert entry.backend == "fake_video"
    assert entry.duration == pytest.approx(1.0, abs=0.2)
    assert service.library_summary().startswith("1 clip(s)")


def test_a_failed_generation_adds_nothing_to_the_library(paths, settings,
                                                         tmp_path) -> None:
    from app.ai.registry import AIBackendManager
    from tests.ai_fakes import make_video_request

    service = AIService(settings, paths=paths, data_root=tmp_path,
                        manager=AIBackendManager(settings, data_root=tmp_path,
                                                 extra_backends=[
                                                     FakeVideoBackend(
                                                         "fake_empty",
                                                         behaviour="empty")]))
    service.status()
    result = service.generate_video(
        make_video_request(tmp_path, prompt="x"), backend_id="fake_empty")
    assert not result.ok
    assert service.library.counts() == {}
    assert service.history.counts().get("video", 0) == 0


def test_the_studio_history_and_the_library_agree(paths, settings,
                                                  tmp_path) -> None:
    from app.ai.registry import AIBackendManager
    from tests.ai_fakes import make_video_request

    service = AIService(settings, paths=paths, data_root=tmp_path,
                        manager=AIBackendManager(settings, data_root=tmp_path,
                                                 extra_backends=[
                                                     FakeVideoBackend(
                                                         "fake_video")]))
    service.status()
    for seed in (1, 2, 3):
        assert service.generate_video(
            make_video_request(tmp_path, seed=seed, name_stem=f"clip{seed}"),
            backend_id="fake_video").ok
    assert service.library.counts()["total"] == 3
    assert service.history.counts()["video"] == 3
    assert {entry.path for entry in service.library.all()} == \
        {entry.path for entry in service.history.all()}


def test_the_library_is_built_from_the_application_folders(paths, settings) -> None:
    from app.ui.context import StartupInfo, create_context  # noqa: F401

    found = video_library_service(paths=paths, tools=TOOLS)
    assert found.root == paths.videos_dir
    assert found.thumbnails.cache_dir == paths.video_thumbnails_dir


def test_a_library_in_the_studio_survives_a_restart(paths, settings,
                                                    tmp_path) -> None:
    from app.ai.registry import AIBackendManager
    from tests.ai_fakes import make_video_request

    first = AIService(settings, paths=paths, data_root=tmp_path,
                      manager=AIBackendManager(settings, data_root=tmp_path,
                                               extra_backends=[
                                                   FakeVideoBackend("fake_video")]))
    first.status()
    assert first.generate_video(make_video_request(tmp_path, seed=8),
                                backend_id="fake_video").ok

    second = AIService(settings, paths=paths, data_root=tmp_path)
    assert second.library.counts()["total"] == 1
    entry = second.library.all()[0]
    assert entry.source == "generated"
    assert entry.seed == 8
    assert entry.status == ENTRY_READY


def test_the_probe_cache_can_be_cleared_and_rebuilt(library, tmp_path) -> None:
    path = make_clip(tmp_path / "videos", "cached.mp4")
    library.scan()
    assert library.probes.cached(path)
    assert library.probes.clear() >= 1
    assert library.probes.cached(path) is None
    assert library.recheck(library.all()[0].id).duration > 0.0


def test_the_probe_cache_ignores_a_file_it_cannot_trust(tmp_path) -> None:
    cache = MediaProbeCache(tmp_path / "probe.json")
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"x" * 10)
    cache._records[str(path)] = {"fingerprint": "stale", "metadata": {"fps": 99}}
    assert cache.cached(path) is None


def test_a_library_with_no_folder_does_not_crash() -> None:
    empty = VideoLibrary(None)
    assert empty.all() == []
    assert empty.counts() == {}
    assert empty.scan() == 0
    assert empty.describe().startswith("The video library is empty")
    assert empty.query().total == 0


# ---------------------------------------------------------------------------
# reporting a problem never becomes the problem (regressions)
# ---------------------------------------------------------------------------

def test_log_event_accepts_a_level_name() -> None:
    """Every Stage G warning path says level="WARNING"; that must not raise."""
    import logging

    from app.core.logging_setup import log_event, resolve_level

    assert resolve_level("WARNING") == logging.WARNING
    assert resolve_level("warning") == logging.WARNING
    assert resolve_level(logging.ERROR) == logging.ERROR
    assert resolve_level(None) == logging.INFO
    assert resolve_level("not a level") == logging.INFO
    log_event("TEST_EVENT", "a warning written by name", level="WARNING")


def test_an_unreadable_index_is_reported_and_survived(tmp_path) -> None:
    """A corrupt library file must be put aside, not crash the page."""
    root = tmp_path / "videos"
    root.mkdir(parents=True, exist_ok=True)
    (root / "videos.json").write_text("{not json at all", encoding="utf-8")
    library = VideoLibrary(root, tools=TOOLS)
    assert library.all() == []
    library.add(make_clip(root, "later.mp4"))
    assert library.counts()["total"] == 1


def test_the_ffmpeg_binary_is_a_path_in_every_shape() -> None:
    """A tool object must never be handed to the operating system as a program."""
    from app.ai.video_library import _binary_path, _ffmpeg_binary
    from app.tools.ffmpeg import ToolInfo, discover_ffmpeg
    from tests.ai_fakes import ffmpeg_tools

    tools = ffmpeg_tools()
    assert Path(_ffmpeg_binary(tools)).name == "ffmpeg"
    assert Path(_binary_path(tools.ffmpeg)).is_file()
    assert Path(_binary_path(ToolInfo("ffmpeg", tools.ffmpeg))).is_file()
    assert _binary_path(None) == "" and _binary_path("") == ""
    assert Path(_ffmpeg_binary(None)).is_file(), \
        "a library with no tools still finds FFmpeg itself"
    discovery = discover_ffmpeg()
    if discovery.ffmpeg is not None:  # pragma: no branch - FFmpeg is installed here
        assert Path(_ffmpeg_binary(discovery)).is_file(), \
            "a discovery result works too"


# ---------------------------------------------------------------------------
# the command line (section 62) - same services as the GUI
# ---------------------------------------------------------------------------

def _run_cli(data_root: Path, *args: str) -> int:
    from app.cli.main import main

    return main(["--data-root", str(data_root), *args])


def _cli_path_line(output: str) -> str:
    """The path the command printed, ignoring the banner above it."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _cli_json(output: str) -> dict:
    """The JSON a command printed, ignoring the banner above it."""
    import json

    start = output.index("{")
    return json.loads(output[start:])


def test_the_cli_lists_inspects_and_pictures_a_clip(tmp_path, capsys) -> None:
    from app.cli.main import EXIT_OK

    incoming = tmp_path / "incoming"
    make_clip(incoming, "harbour_01.mp4", width=320, height=176)
    data_root = tmp_path / "cli-data"

    assert _run_cli(data_root, "ai", "library", "add",
                    str(incoming / "harbour_01.mp4")) == EXIT_OK
    capsys.readouterr()

    assert _run_cli(data_root, "ai", "library", "list") == EXIT_OK
    listing = capsys.readouterr().out
    assert "harbour_01" in listing and "320x176" in listing
    assert "not an AI model" not in listing  # nothing claims a model here

    assert _run_cli(data_root, "ai", "library", "inspect",
                    "harbour_01.mp4") == EXIT_OK
    detail = capsys.readouterr().out
    assert "Imported from a file" in detail

    assert _run_cli(data_root, "ai", "library", "thumbnail",
                    "harbour_01") == EXIT_OK
    picture = _cli_path_line(capsys.readouterr().out)
    assert picture.endswith(".jpg"), picture
    assert Path(picture).is_file(), "the command printed a real picture path"


def test_the_cli_says_what_it_could_not_find(tmp_path, capsys) -> None:
    from app.cli.main import EXIT_PROBLEMS

    data_root = tmp_path / "cli-empty"
    assert _run_cli(data_root, "ai", "library", "inspect", "nope.mp4") \
        == EXIT_PROBLEMS
    output = capsys.readouterr().out
    assert "No clip in the library matches" in output
    assert "What to do:" in output


def test_the_cli_remove_needs_the_explicit_flag_to_delete_a_file(tmp_path,
                                                                 capsys) -> None:
    from app.cli.main import EXIT_OK

    incoming = tmp_path / "incoming"
    path = make_clip(incoming, "keep.mp4")
    data_root = tmp_path / "cli-remove"
    _run_cli(data_root, "ai", "library", "add", str(path))
    capsys.readouterr()

    assert _run_cli(data_root, "ai", "library", "remove", "keep.mp4") == EXIT_OK
    assert "left where they are" in capsys.readouterr().out
    assert path.is_file(), "the command line must not delete media by accident"


def test_the_cli_json_is_machine_readable(tmp_path, capsys) -> None:

    from app.cli.main import EXIT_OK

    incoming = tmp_path / "incoming"
    make_clip(incoming, "clip.mp4")
    data_root = tmp_path / "cli-json"
    _run_cli(data_root, "ai", "library", "add", str(incoming / "clip.mp4"))
    capsys.readouterr()

    assert _run_cli(data_root, "ai", "library", "list", "--json") == EXIT_OK
    payload = _cli_json(capsys.readouterr().out)
    assert payload["total"] == 1
    assert payload["clips"][0]["filename"] == "clip.mp4"
    assert payload["clips"][0]["measurements"]


def test_the_cli_library_has_no_dead_subcommands() -> None:
    """Every documented subcommand is registered and takes its arguments."""
    from app.cli.main import build_parser

    parser = build_parser()
    needs_clip = ("inspect", "thumbnail", "recheck", "remove")
    for name in ("list", "scan", "thumbnails", "add") + needs_clip:
        argv = ["ai", "library", name]
        if name in needs_clip:
            argv.append("some-clip")
        elif name == "add":
            argv.append("some-file.mp4")
        parsed = parser.parse_args(argv)
        assert parsed.library_command == name
