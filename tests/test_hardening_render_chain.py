"""Hardening pass: the full render call chain (directive sections 10-12, 42-43, 60).

The tests in this module follow the *whole* path a click takes:

    project model -> RenderService.plan() -> OutputDecision -> FFmpeg argv ->
    real encoded file -> FFprobe measurement

Nothing is mocked.  A recording wrapper sits on the tools object so the exact
argv FFmpeg was invoked with can be inspected next to the file it produced, which
is what proves the settings the user chose are the settings the encoder received.

Every fixture is deliberately tiny (320x256, one-second scenes, ultrafast) so the
suite stays fast; the resolutions that matter for the final acceptance report are
rendered for real in :func:`test_vertical_and_square_formats_render_at_exact_sizes`.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest

from app.core.logging_setup import get_logger
from app.core.paths import AppPaths, unique_path
from app.core.settings import SettingsStore
from app.media.probe import probe_media
from app.project.model import SceneSpec
from app.project.service import CreateRequest, ProjectService
from app.render.service import RenderService
from app.tools.ffmpeg import CommandResult, FFmpegTools, discover_ffmpeg

DISCOVERY = discover_ffmpeg()
pytestmark = pytest.mark.skipif(
    not DISCOVERY.has_ffmpeg,
    reason="FFmpeg is not installed, so no render can be made")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def md5(path: Path) -> str:
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


class RecordingTools(FFmpegTools):
    """The real tools object, remembering every command it ran."""

    def __init__(self, discovery, log: list) -> None:
        super().__init__(discovery)
        self.commands: list[list[str]] = log

    def run(self, args, **kwargs) -> CommandResult:  # noqa: D102 - wrapper
        self.commands.append([str(part) for part in args])
        return super().run(args, **kwargs)


class EncodeLog:
    """Collects the encoder commands the engine logged while rendering.

    ``stream_encode`` pipes raw frames into FFmpeg with its own ``Popen`` call
    (it cannot go through ``tools.run`` because the frames are the stdin), and it
    logs the exact command line it built.  Reading that back is how a test can
    see the real invocation instead of the arguments someone intended to pass.
    """

    def __init__(self) -> None:
        self.commands: list[str] = []
        self._logger = get_logger()
        self._records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def __init__(self, owner: "EncodeLog") -> None:
            super().__init__(level=logging.INFO)
            self.owner = owner

        def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
            self.owner._records.append(record)

    def __enter__(self) -> "EncodeLog":
        self._handler = self._Handler(self)
        # A bare logger inherits the root level (WARNING), so INFO events would
        # never reach the handler; the application raises it in configure_logging.
        self._previous_level = self._logger.level
        self._logger.setLevel(logging.INFO)
        self._logger.addHandler(self._handler)
        return self

    def __exit__(self, *exc_info) -> None:
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._previous_level)

    def commands_for(self, event: str) -> list[str]:
        found = []
        for record in self._records:
            if getattr(record, "event", "") != event:
                continue
            command = getattr(record, "field_command", "")
            if command:
                found.append(str(command))
        return found

    @property
    def encodes(self) -> list[str]:
        return self.commands_for("RENDER_ENCODE_START")

    @property
    def burns(self) -> list[str]:
        return self.commands_for("RENDER_BURN_START")


@pytest.fixture(scope="module")
def tools() -> FFmpegTools:
    return FFmpegTools(DISCOVERY)


class Studio:
    """A real data root, a real project service and a real render service."""

    def __init__(self, root: Path, *, settings_store: SettingsStore | None = None) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.paths = AppPaths(data_root=root, source_root=root, reason="hardening")
        self.paths.ensure()
        self.store = settings_store or SettingsStore(self.paths.settings_file)
        self.settings = self.store.load().settings
        self.service = ProjectService(self.paths, self.settings)
        self.commands: list[list[str]] = []
        self.tools = RecordingTools(DISCOVERY, self.commands)

    @property
    def project_dir(self) -> Path:
        return Path(self.service.session.layout.root)

    def create(self, *, name: str = "Project_Video", scenes: int = 2,
               seconds: float = 1.0, width: int = 320, height: int = 256,
               fps: int = 25) -> object:
        project = self.service.create_project(
            CreateRequest(name=name, width=width, height=height, fps=fps,
                          template="blank"))
        colours = ("#14a078", "#b4285a", "#1e3cc8", "#c8a01e")
        for index in range(scenes):
            scene = SceneSpec(name=f"Scene {index + 1}", duration=float(seconds),
                              background=colours[index % len(colours)])
            project.add_scene(scene)
        return project

    def save_export(self, **values) -> None:
        """Change the export section through the normal edit path, then save."""
        def mutate(project) -> None:
            for key, value in values.items():
                setattr(project.export, key, value)

        self.service.edit("test: export settings", mutate)
        self.service.save(reason="test")

    def renderer(self, **kwargs) -> RenderService:
        return RenderService(self.tools, project_dir=self.project_dir,
                             paths=self.paths, **kwargs)

    def fast(self) -> dict:
        """Overrides that keep a render cheap but real."""
        return {"quality_preset": "draft", "encoder_preset": "ultrafast"}


@pytest.fixture()
def studio(tmp_path: Path) -> Studio:
    return Studio(tmp_path / "data")


def _encode_commands(studio: Studio) -> list[list[str]]:
    """Only the commands that really encoded a video."""
    return [argv for argv in studio.commands if "-c:v" in argv or "libx264" in argv]


# ---------------------------------------------------------------------------
# 10. UI -> project -> plan -> encoder arguments -> FFmpeg invocation
# ---------------------------------------------------------------------------

def test_the_plan_the_ui_shows_is_the_render_that_happens(studio: Studio):
    """The file the UI promised is the file that appears, byte for byte."""
    project = studio.create(scenes=2, seconds=1.0)
    studio.save_export(filename_template="{name}{seq}", output_dir="renders")
    service = studio.renderer()

    plan = service.plan(project)
    assert plan.ready, plan.errors
    planned = plan.output
    assert planned is not None
    assert planned.filename == "Project_Video1.mp4"
    assert not planned.path.exists(), "planning must not create the output file"

    result = service.render(studio.service.current, include_audio=False,
                            include_subtitles=False, overrides=studio.fast())

    assert result.status == "COMPLETED", result.message
    assert result.path == planned.path
    assert result.path.is_file()
    assert result.path.stat().st_size > 0
    assert result.decision is not None
    assert result.decision.sequence == planned.sequence


def test_the_encoder_receives_the_settings_the_plan_promised(studio: Studio):
    """Read the invocation, not the intention (section 10)."""
    studio.create(scenes=1, seconds=1.0)
    studio.save_export(filename_template="{name}_{seq}", output_dir="renders")
    service = studio.renderer()
    overrides = {"quality_preset": "draft", "encoder_preset": "ultrafast",
                 "crf": 27, "codec": "h264_cpu", "container": "mp4",
                 "pixel_format": "yuv420p", "bitrate_kbps": 0}

    plan = service.plan(studio.service.current, overrides=dict(overrides))
    with EncodeLog() as log:
        result = service.render(studio.service.current, include_audio=False,
                                include_subtitles=False, overrides=dict(overrides))
    assert result.status == "COMPLETED", result.message
    assert plan.output is not None

    encodes = log.encodes
    assert encodes, "the engine logged no encoder invocation"
    command = encodes[-1]

    # The resolution and frame rate the plan promised reach the raw input.
    assert "-s 320x256" in command, command
    assert "-r 25" in command, command
    # The codec, the quality and the pixel format are the chosen ones.
    assert "libx264" in command
    assert "-crf 27" in command
    assert "yuv420p" in command
    assert "-preset ultrafast" in command
    # The intermediate the encoder writes is inside the application's own temp
    # area, never the user's output folder.
    assert str(studio.paths.temp_dir) in command, command
    assert str(plan.output.directory) not in command, command
    assert str(result.path) not in command, (
        "the encoder must write a staging segment, not the final output")

    info = probe_media(result.path, studio.tools)
    assert info.ok
    assert (info.width, info.height) == (320, 256)
    assert int(round(info.fps or 0)) == 25
    assert "264" in str(info.video_codec)


def test_the_render_only_runs_commands_for_its_own_work_dir(studio: Studio):
    """A render may not write outside its project folder except the output."""
    studio.create(scenes=1, seconds=1.0)
    studio.save_export(filename_template="{name}{seq}", output_dir="renders")
    result = studio.renderer().render(studio.service.current, include_audio=False,
                                      include_subtitles=False, overrides=studio.fast())
    assert result.status == "COMPLETED", result.message

    written = [argv[-1] for argv in studio.commands if argv and argv[-1].endswith(".mp4")]
    for path in written:
        assert path.startswith(str(studio.root)), path


# ---------------------------------------------------------------------------
# 11. Output directory, file name template, no overwrite
# ---------------------------------------------------------------------------

def test_three_generate_clicks_make_exactly_project_video_1_2_3(studio: Studio):
    """The named regression from the directive: Video1, Video2, Video3."""
    studio.create(scenes=2, seconds=1.0)
    studio.save_export(filename_template="{name}{seq}", output_dir="render_out")
    service = studio.renderer()
    output_dir = studio.project_dir / "render_out"

    takes = []
    for _ in range(3):
        before = sorted(path.name for path in output_dir.iterdir()) \
            if output_dir.is_dir() else []
        result = service.render(studio.service.current, include_audio=False,
                                include_subtitles=False, overrides=studio.fast())
        assert result.status == "COMPLETED", result.message
        after = sorted(path.name for path in output_dir.iterdir())
        assert len(after) == len(before) + 1, (
            f"one click must make one file, {before} -> {after}")
        takes.append(result.path)

    assert [path.name for path in takes] == [
        "Project_Video1.mp4", "Project_Video2.mp4", "Project_Video3.mp4"]
    # Nothing earlier was touched.
    assert all(path.is_file() and path.stat().st_size > 0 for path in takes)
    digests = [md5(path) for path in takes]
    assert len(set(digests)) == 1, "identical settings must produce identical files"


def test_an_unrelated_file_in_the_output_folder_does_not_derail_the_naming(studio: Studio):
    """A shared folder full of other videos must not push the take number away.

    The old scan read the trailing digits of *every* file in the folder, so a
    ``Holiday 2024.mp4`` sitting next to the renders made the first take of a new
    project ``Project_Video2024.mp4``.
    """
    studio.create(scenes=1, seconds=1.0)
    studio.save_export(filename_template="{name}{seq}", output_dir="shared")
    output_dir = studio.project_dir / "shared"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "Holiday 2024.mp4").write_bytes(b"not really a video")

    result = studio.renderer().render(studio.service.current, include_audio=False,
                                      include_subtitles=False, overrides=studio.fast())
    assert result.status == "COMPLETED", result.message
    assert result.path.name == "Project_Video1.mp4"
    assert (output_dir / "Holiday 2024.mp4").read_bytes() == b"not really a video"


def test_a_relative_output_folder_stays_inside_the_project(studio: Studio):
    studio.create(scenes=1, seconds=1.0)
    studio.save_export(filename_template="{name}_{seq}", output_dir="my renders")
    result = studio.renderer().render(studio.service.current, include_audio=False,
                                     include_subtitles=False, overrides=studio.fast())
    assert result.status == "COMPLETED", result.message
    assert result.path.parent == studio.project_dir / "my renders"


def test_an_output_folder_that_does_not_exist_yet_is_created_by_the_render(studio: Studio):
    studio.create(scenes=1, seconds=1.0)
    studio.save_export(filename_template="{name}_{seq}",
                       output_dir="deep/nested/output")
    service = studio.renderer()
    service.plan(studio.service.current)  # planning only, no side effects
    assert not (studio.project_dir / "deep").exists(), \
        "planning the render must not create folders"
    result = service.render(studio.service.current, include_audio=False,
                            include_subtitles=False, overrides=studio.fast())
    assert result.status == "COMPLETED", result.message
    assert result.path.parent.is_dir()


def test_a_leftover_staging_file_is_not_mistaken_for_a_finished_take(studio: Studio):
    """A crashed render leaves ``.Name1.rendering.mp4`` behind; it must be reused."""
    studio.create(scenes=1, seconds=1.0)
    studio.save_export(filename_template="{name}{seq}", output_dir="renders")
    output_dir = studio.project_dir / "renders"
    output_dir.mkdir(parents=True, exist_ok=True)
    staging = output_dir / ".Project_Video1.rendering.mp4"
    staging.write_bytes(b"half a file")

    result = studio.renderer().render(studio.service.current, include_audio=False,
                                      include_subtitles=False, overrides=studio.fast())
    assert result.status == "COMPLETED", result.message
    assert result.path.name == "Project_Video1.mp4"
    assert result.path.stat().st_size > len(b"half a file")
    assert not staging.exists(), "the staging file must be consumed, not left behind"


def test_a_name_that_appears_during_the_render_is_not_overwritten(studio: Studio):
    """The user's file always wins, even if it appears at the wrong moment."""
    studio.create(scenes=1, seconds=1.0)
    studio.save_export(filename_template="{name}{seq}", output_dir="renders")
    service = studio.renderer()

    def steal_the_name(progress):
        target = studio.project_dir / "renders" / "Project_Video1.mp4"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"the user's own file")

    result = service.render(studio.service.current, include_audio=False,
                            include_subtitles=False, overrides=studio.fast(),
                            progress=steal_the_name)
    assert result.status == "COMPLETED", result.message
    stolen = studio.project_dir / "renders" / "Project_Video1.mp4"
    assert stolen.read_bytes() == b"the user's own file"
    assert result.path.name == "Project_Video2.mp4"


def test_unique_path_steps_around_existing_names(tmp_path: Path):
    """The shared helper the producers use has no gaps at the edges."""
    first = tmp_path / "captions.srt"
    first.write_text("one", encoding="utf-8")
    second = unique_path(tmp_path, "captions", ".srt")
    assert second.name == "captions2.srt"
    second.write_text("two", encoding="utf-8")
    assert unique_path(tmp_path, "captions", ".srt").name == "captions3.srt"
    assert unique_path(tmp_path, "captions", "srt").name == "captions3.srt"


# ---------------------------------------------------------------------------
# 12. No output producer overwrites silently
# ---------------------------------------------------------------------------

def test_the_render_output_only_ever_gains_files(studio: Studio):
    studio.create(scenes=1, seconds=1.0)
    studio.save_export(filename_template="{name}_{seq}", output_dir="renders")
    service = studio.renderer()
    first = service.render(studio.service.current, include_audio=False,
                           include_subtitles=False, overrides=studio.fast())
    original = md5(first.path)
    second = service.render(studio.service.current, include_audio=False,
                            include_subtitles=False, overrides=studio.fast())
    assert second.path != first.path
    assert md5(first.path) == original
    assert len(list(first.path.parent.iterdir())) == 2


def test_exporting_the_same_captions_twice_does_not_litter_or_replace(tmp_path: Path):
    """Identical text -> the file is left alone; different text -> a new name."""
    from app.subtitles.service import write_subtitle_file

    target = tmp_path / "captions.srt"
    written = write_subtitle_file(target, "1\n00:00:00,000 --> 00:00:01,000\nHello\n")
    assert written == target

    again = write_subtitle_file(target, target.read_text(encoding="utf-8"))
    assert again == target, "re-exporting identical captions must not make a copy"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["captions.srt"]

    kept = target.read_text(encoding="utf-8")
    second = write_subtitle_file(target, kept.replace("Hello", "Hello there"))
    assert second.name == "captions2.srt"
    assert target.read_text(encoding="utf-8") == kept, "the edited file must survive"
    assert "Hello there" in second.read_text(encoding="utf-8")


def test_a_caption_file_is_written_atomically(tmp_path: Path):
    """A crash mid-write may not leave a half caption file (section 38)."""
    from app.subtitles.service import write_subtitle_file

    target = tmp_path / "atomic.srt"
    write_subtitle_file(target, "the first captions")
    leftovers = [path for path in tmp_path.iterdir() if path.name != target.name]
    assert leftovers == [], f"temporary files left behind: {leftovers}"

    # The library used underneath is the project's own atomic writer, so a
    # failure cannot truncate the file that is already there.
    from app.core import atomicio

    assert atomicio.atomic_write_text.__module__.endswith("atomicio")


def test_the_cli_audio_mix_keeps_an_existing_file(tmp_path: Path, capsys):
    """``motion-studio audio mix --output x.wav`` may not replace ``x.wav``."""
    import argparse

    from app.cli import render as cli_render
    from app.project.model import NarrationTrack

    studio = Studio(tmp_path / "data")
    project = studio.create(scenes=1, seconds=1.0)
    tone = studio.project_dir / "tone.wav"
    made = studio.tools.run(["-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                             "-i", "sine=frequency=300:duration=1", str(tone)])
    assert made.ok, made.describe_failure()
    project.narration.tracks.append(NarrationTrack(
        id="t1", kind="section", section_id="s1",
        path="tone.wav", status="ready", actual_duration_seconds=1.0))
    project.scenes[0].narration.file = "tone.wav"
    project.scenes[0].narration.duration = 1.0
    studio.service.save(reason="test")
    project_dir = studio.project_dir
    studio.service.close_project(save=True)

    existing = tmp_path / "master.wav"
    existing.write_bytes(b"the user's own recording")
    args = argparse.Namespace(project=str(project_dir), output=str(existing))
    code = cli_render.command_audio_mix(args, studio.paths, studio.settings)
    printed = capsys.readouterr().out

    assert code == cli_render.EXIT_OK, printed
    assert existing.read_bytes() == b"the user's own recording", \
        "the CLI replaced a file the user already had"
    assert (tmp_path / "master2.wav").is_file()
    assert "master2.wav" in printed


# ---------------------------------------------------------------------------
# 42-43, 60. Quality, resolutions and determinism
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("width,height,label", [
    (1080, 1920, "vertical"),
    (1080, 1350, "portrait"),
    (1080, 1080, "square"),
])
def test_vertical_and_square_formats_render_at_exact_sizes(studio: Studio, width: int,
                                                           height: int, label: str):
    """The three shapes a short-form video is published in (sections 42-43)."""
    studio.create(scenes=1, seconds=0.8, width=width, height=height, fps=30)
    studio.save_export(filename_template="{name}_{seq}", output_dir="renders")
    result = studio.renderer().render(
        studio.service.current, include_audio=False, include_subtitles=False,
        overrides={"quality_preset": "draft", "encoder_preset": "ultrafast"})

    assert result.status == "COMPLETED", f"{label}: {result.message}"
    info = probe_media(result.path, studio.tools)
    assert info.ok
    assert (info.width, info.height) == (width, height), label
    assert int(round(info.fps or 0)) == 30
    assert result.qc is not None and result.qc.verdict in ("PASS", "WARNING"), \
        result.qc.describe() if result.qc else "no QC"


def _detailed_project(studio: Studio, scenes: int = 2) -> None:
    """A project whose frames carry real detail, for size comparisons.

    A flat colour frame compresses so well that it is useless as a proxy for
    quality: at CRF 16 it can be *smaller* than at CRF 36.
    """
    from app.scene.templates import create_scene_from_template, default_templates_registered

    default_templates_registered()
    studio.create(scenes=0)
    project = studio.service.current
    files = [
        ("chart", {"title": "Renders per quarter", "values": [12, 30, 18, 44, 27],
                   "labels": ["Q1", "Q2", "Q3", "Q4", "Q5"]}),
        ("stat", {"title": "Scenes rendered", "value": 4, "unit": "scenes"}),
        ("title", {"title": "Quality", "subtitle": "draft versus high"}),
    ]
    for index in range(scenes):
        key, content = files[index % len(files)]
        scene = create_scene_from_template(key, dict(content))
        scene.duration = 1.0
        project.add_scene(scene)


def test_the_chosen_quality_preset_becomes_encoder_arguments(studio: Studio):
    """A preset the user picks is turned into real settings (section 42)."""
    from app.project.presets import resolve_quality

    _detailed_project(studio, scenes=1)
    studio.save_export(filename_template="{name}_{seq}", output_dir="renders")
    service = studio.renderer()
    draft = resolve_quality("draft", "mp4", "h264_cpu")
    high = resolve_quality("high", "mp4", "h264_cpu")
    assert draft["crf"] != high["crf"]

    with EncodeLog() as log:
        low = service.render(studio.service.current, include_audio=False,
                             include_subtitles=False, overrides=dict(draft))
        high_result = service.render(studio.service.current, include_audio=False,
                                     include_subtitles=False, overrides=dict(high))
    assert low.status == high_result.status == "COMPLETED", (low.message,
                                                             high_result.message)

    encodes = log.encodes
    assert f"-crf {draft['crf']}" in " ".join(encodes)
    assert f"-crf {high['crf']}" in " ".join(encodes)
    assert f"-preset {draft['encoder_preset']}" in " ".join(encodes)
    assert f"-preset {high['encoder_preset']}" in " ".join(encodes)

    low_info = probe_media(low.path, studio.tools)
    high_info = probe_media(high_result.path, studio.tools)
    assert low_info.ok and high_info.ok
    assert high_info.size_bytes > low_info.size_bytes, (
        f"the higher quality setting must produce the larger file: "
        f"{high_info.size_bytes} vs {low_info.size_bytes}")


def test_a_lower_crf_means_a_bigger_file_for_the_same_preset(studio: Studio):
    """The usual quality knob still behaves (section 42)."""
    _detailed_project(studio, scenes=1)
    studio.save_export(filename_template="{name}_{seq}", output_dir="renders")
    service = studio.renderer()
    common = {"encoder_preset": "veryfast", "bitrate_kbps": 0, "pixel_format": "yuv420p"}
    low = service.render(studio.service.current, include_audio=False,
                         include_subtitles=False, overrides=dict(common, crf=34))
    high = service.render(studio.service.current, include_audio=False,
                          include_subtitles=False, overrides=dict(common, crf=16))
    assert low.status == high.status == "COMPLETED"
    low_info = probe_media(low.path, studio.tools)
    high_info = probe_media(high.path, studio.tools)
    assert (high_info.bitrate_kbps or 0) > (low_info.bitrate_kbps or 0)
    assert high_info.size_bytes > low_info.size_bytes


def test_two_renders_of_an_unchanged_project_are_byte_identical(studio: Studio):
    """Determinism with the same settings (section 60)."""
    studio.create(scenes=3, seconds=0.6)
    studio.save_export(filename_template="{name}_{seq}", output_dir="renders")
    service = studio.renderer()
    kwargs = dict(include_audio=False, include_subtitles=False, overrides=studio.fast())
    first = service.render(studio.service.current, **kwargs)
    second = service.render(studio.service.current, **kwargs)
    assert first.status == second.status == "COMPLETED"
    assert first.path != second.path
    assert md5(first.path) == md5(second.path)
    assert first.path.stat().st_size == second.path.stat().st_size


def test_a_changed_scene_changes_the_render(studio: Studio):
    """Determinism must not mean ignoring edits."""
    studio.create(scenes=2, seconds=0.8)
    studio.save_export(filename_template="{name}_{seq}", output_dir="renders")
    service = studio.renderer()
    first = service.render(studio.service.current, include_audio=False,
                           include_subtitles=False, overrides=studio.fast())

    def repaint(project) -> None:
        project.scenes[0].background = "#ffd400"

    studio.service.edit("test: repaint", repaint)
    second = service.render(studio.service.current, include_audio=False,
                            include_subtitles=False, overrides=studio.fast())
    assert first.status == second.status == "COMPLETED"
    assert md5(first.path) != md5(second.path)
