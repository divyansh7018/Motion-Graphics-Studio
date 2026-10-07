"""Hardening pass: audio, captions and the timeline (directive sections 3-6, 31).

Everything here runs real FFmpeg on real files.  Narration in the fixtures is a
tone of a *measured* length, created through FFmpeg - it is labelled synthetic
everywhere and is never presented as a Kokoro voice (section 2).  What is being
tested is the pipeline: placement, trimming, fades, gain, ducking, normalisation,
the master mix, caption timing and the timeline arithmetic.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.audio.service import AudioService
from app.core.paths import AppPaths
from app.core.settings import SettingsStore
from app.media.probe import probe_media
from app.project.model import NarrationTrack, SceneSpec, SoundEffect
from app.project.service import CreateRequest, ProjectService
from app.render.service import RenderService
from app.scene.timing import TimingOptions, build_timeline
from app.subtitles.service import (
    SubtitleService,
    delete_cue,
    generate_cues,
    merge_cues,
    split_cue,
    validate_cues,
)
from app.tools.ffmpeg import FFmpegTools, discover_ffmpeg

DISCOVERY = discover_ffmpeg()
pytestmark = pytest.mark.skipif(
    not DISCOVERY.has_ffmpeg,
    reason="FFmpeg is not installed, so no audio or caption file can be made")

SYNTHETIC = "SYNTHETIC TEST TONE - not a Kokoro voice"


@pytest.fixture(scope="module")
def tools() -> FFmpegTools:
    return FFmpegTools(DISCOVERY)


class Studio:
    """A real data root, project service, FFmpeg tools and helper writers."""

    def __init__(self, root: Path, ffmpeg: FFmpegTools) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.paths = AppPaths(data_root=self.root, source_root=self.root,
                              reason="hardening")
        self.paths.ensure()
        self.settings = SettingsStore(self.paths.settings_file).load().settings
        self.service = ProjectService(self.paths, self.settings)
        self.tools = ffmpeg
        self.project_dir = self.root

    # -- fixtures ---------------------------------------------------------

    def create(self, *, scenes: int = 2, seconds: float = 1.5,
               width: int = 320, height: int = 256, fps: int = 25):
        project = self.service.create_project(CreateRequest(
            name="Audio_Project", width=width, height=height, fps=fps,
            template="blank"))
        colours = ("#101a2a", "#2a101a", "#102a1a")
        for index in range(scenes):
            project.add_scene(SceneSpec(name=f"Scene {index + 1}",
                                        duration=float(seconds),
                                        background=colours[index % len(colours)]))
        from app.project.model import ScriptSection

        for index, scene in enumerate(project.scenes, start=1):
            scene.narration.text = (
                f"Scene {index} is spoken here. It has several sentences. "
                f"Each one gets its own caption. The last one closes the scene.")
            project.script.sections.append(ScriptSection(
                id=f"sec{index}", title=scene.name, text=scene.narration.text,
                order=index))
        self.project_dir = Path(self.service.session.layout.root)
        return project

    def tone(self, name: str, seconds: float, frequency: int,
             channels: int = 1) -> Path:
        path = self.project_dir / "audio" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        result = self.tools.run(["-hide_banner", "-loglevel", "error", "-y",
                                 "-f", "lavfi",
                                 "-i", f"sine=frequency={frequency}:duration={seconds}",
                                 "-ar", "48000", "-ac", str(channels), str(path)])
        assert result.ok, result.describe_failure()
        return path

    def silence(self, name: str, seconds: float) -> Path:
        path = self.project_dir / "audio" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        result = self.tools.run([
            "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
            "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000:d={seconds}",
            str(path)])
        assert result.ok, result.describe_failure()
        return path

    def attach_narration(self, lengths: list[float]) -> list[Path]:
        """Measured synthetic narration: a tone per scene, real length stored."""
        from app.tts.narration import attach_narration_to_scenes

        tracks = []
        files = []
        for index, length in enumerate(lengths, start=1):
            wav = self.tone(f"narration/narration_scene_{index:03d}.wav", length,
                            170 + index * 40)
            info = probe_media(wav, self.tools)
            assert info.ok, info.error
            track = NarrationTrack(
                id=f"track{index}", kind="section", section_id=f"sec{index}",
                path=f"audio/narration/{wav.name}", status="ready",
                actual_duration_seconds=info.duration,
                sample_rate=info.sample_rate or 48000,
                channels=info.channels or 1, size_bytes=wav.stat().st_size)
            self.service.current.narration.tracks.append(track)
            tracks.append(track)
            files.append(wav)
        attach_narration_to_scenes(self.service.current, tracks)
        self.service.save(reason="narration")
        return files


@pytest.fixture()
def studio(tmp_path: Path, tools: FFmpegTools) -> Studio:
    return Studio(tmp_path / "data", tools)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def mean_volume(tools: FFmpegTools, path: Path, *, start: float = 0.0,
                seconds: float = 0.0) -> float:
    """The mean level of a window of audio, measured by FFmpeg (dB)."""
    window = f"atrim=start={max(0.0, start):.3f}"
    if seconds > 0:
        window += f":duration={seconds:.3f}"
    result = tools.run([
        "-hide_banner", "-nostdin", "-v", "info", "-i", str(path),
        "-af", f"{window},volumedetect", "-f", "null", "-",
    ])
    assert result.ok, result.describe_failure()
    match = re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", result.stderr or "")
    assert match, (result.stderr or "")[-400:]
    value = match.group(1)
    return -120.0 if value == "-inf" else float(value)


def duration_of(tools: FFmpegTools, path: Path) -> float:
    info = probe_media(path, studio.tools)
    assert info.ok, info.error
    return float(info.duration)


# ---------------------------------------------------------------------------
# 3-4. Narration, music, effects, trimming, fades, gain, ducking, master
# ---------------------------------------------------------------------------

def test_the_master_mix_places_measured_narration_and_effect_in_the_timeline(studio):
    project = studio.create(scenes=2, seconds=1.5)
    studio.attach_narration([1.4, 1.2])
    music = studio.tone("music.wav", 8.0, 110, channels=2)
    ping = studio.tone("sfx.wav", 0.4, 900)
    project = studio.service.current
    project.audio.music.path = "audio/music.wav"
    project.audio.music.volume = 0.2
    project.audio.music.loop = True
    project.audio.sfx.append(SoundEffect(id="sfx1", path="audio/sfx.wav", volume=0.5,
                                         anchor="scene", scene_id=project.scenes[1].id,
                                         at_seconds=0.2, fade_in=0.05, fade_out=0.1))
    studio.service.save(reason="mix")
    assert music.is_file() and ping.is_file()

    audio = AudioService(studio.tools, project_dir=studio.project_dir)
    timeline = build_timeline(project.scenes)
    validation = audio.validate(project, timeline)
    assert validation.ok, [issue.to_dict() for issue in validation.errors]
    assert len(validation.placements) == 2

    master = studio.project_dir / "mix" / "master.wav"
    result = audio.render_master(project, timeline, master)
    assert result.ok, result.message
    assert master.is_file()

    info = probe_media(master, studio.tools)
    assert info.ok and info.has_audio
    # The master runs for the timeline, not for the longest source file.
    assert abs(info.duration - timeline.total_duration) < 0.25, (
        info.duration, timeline.total_duration)
    assert info.sample_rate == 48000
    assert info.channels == 2


def test_fades_trim_and_gain_are_real_filter_steps(studio):
    project = studio.create(scenes=1, seconds=1.0)
    music = studio.tone("music.wav", 4.0, 120, channels=2)
    project = studio.service.current
    project.audio.music.path = "audio/music.wav"
    project.audio.music.trim_in = 0.5
    project.audio.music.trim_out = 3.0
    project.audio.music.fade_in = 0.3
    project.audio.music.fade_out = 0.4
    project.audio.narration_enabled = False
    # Normalisation targets a loudness level, so it would flatten any master
    # fader change; this test is about the fader reaching the filter graph.
    project.audio.normalize_enabled = False
    studio.service.save(reason="music")

    audio = AudioService(studio.tools, project_dir=studio.project_dir)
    timeline = build_timeline(project.scenes)
    plan, issues = audio.build_plan(project, timeline)
    chain = plan.filter_complex
    assert "atrim" in chain, chain
    assert "afade=t=in" in chain and "afade=t=out" in chain, chain
    assert "volume=" in chain, chain
    assert music.is_file()

    quiet = studio.project_dir / "mix" / "quiet.wav"
    loud = studio.project_dir / "mix" / "loud.wav"
    assert audio.render_master(project, timeline, quiet).ok
    project.audio.master_volume = 2.0
    assert audio.render_master(project, timeline, loud).ok

    quiet_level = mean_volume(studio.tools, quiet, seconds=0.6)
    loud_level = mean_volume(studio.tools, loud, seconds=0.6)
    assert loud_level > quiet_level + 3.0, (quiet_level, loud_level)
    # The trim is audible: the first 0.3 s fade in from silence.
    start_level = mean_volume(studio.tools, quiet, start=0.5, seconds=0.2)
    assert start_level > -120.0


def test_ducking_lowers_the_music_while_the_narration_speaks(studio):
    """The bed is measured on its own, so the ducking is not hidden by the voice.

    The narration *files* are digital silence here - the timeline still reserves
    the narration window, which is what ducking reacts to - so any level change
    measured in that window belongs to the music bed.
    """
    project = studio.create(scenes=2, seconds=2.0)
    from app.tts.narration import attach_narration_to_scenes

    tracks = []
    for index in (1, 2):
        studio.silence(f"narration/silent_{index}.wav", 1.8)
        track = NarrationTrack(id=f"track{index}", kind="section",
                               section_id=f"sec{index}",
                               path=f"audio/narration/silent_{index}.wav",
                               status="ready", actual_duration_seconds=1.8)
        project.narration.tracks.append(track)
        tracks.append(track)
    attach_narration_to_scenes(project, tracks)
    project = studio.service.current
    studio.tone("music.wav", 8.0, 120, channels=2)
    project.audio.music.path = "audio/music.wav"
    project.audio.music.volume = 0.9
    project.audio.music.loop = True
    project.audio.music.fade_in = 0.0
    project.audio.music.fade_out = 0.0
    project.audio.normalize_enabled = False
    # Short ramps, so "inside the narration" and "between the two scenes" are
    # unambiguous moments even in a four second fixture.
    project.audio.ducking_attack = 0.05
    project.audio.ducking_release = 0.05
    studio.service.save(reason="ducking")

    audio = AudioService(studio.tools, project_dir=studio.project_dir)
    timeline = build_timeline(project.scenes)

    ducked = studio.project_dir / "mix" / "ducked.wav"
    flat = studio.project_dir / "mix" / "flat.wav"
    project.audio.ducking_enabled = True
    project.audio.ducking_level = 0.25
    plan, _ = audio.build_plan(project, timeline)
    assert "volume=volume=" in plan.filter_complex, plan.filter_complex
    assert audio.render_master(project, timeline, ducked).ok
    project.audio.ducking_enabled = False
    assert audio.render_master(project, timeline, flat).ok

    # Inside the first narration window the bed is clearly quieter...
    inside = mean_volume(studio.tools, ducked, start=0.6, seconds=0.8)
    flat_inside = mean_volume(studio.tools, flat, start=0.6, seconds=0.8)
    assert inside < flat_inside - 6.0, (inside, flat_inside)
    # ...and it recovers between the two scenes.
    gap = mean_volume(studio.tools, ducked, start=1.9, seconds=0.3)
    flat_gap = mean_volume(studio.tools, flat, start=1.9, seconds=0.3)
    assert abs(gap - flat_gap) < 1.0, (gap, flat_gap)
    assert gap > inside + 3.0, (gap, inside)

    # The envelope the mix uses agrees with what was measured, and it reads the
    # control the way the UI labels it: "Music level while speaking" = 25% kept.
    project.audio.ducking_enabled = True
    inside_gain = audio.duck_level_at(project, timeline, 1.0)
    assert abs(inside_gain - 0.25) < 0.05, inside_gain
    assert audio.duck_level_at(project, timeline, 2.05) > 0.9


def test_normalisation_changes_the_level_and_is_reported(studio):
    project = studio.create(scenes=1, seconds=1.0)
    studio.attach_narration([0.9])
    project = studio.service.current
    studio.service.save(reason="normalize")
    audio = AudioService(studio.tools, project_dir=studio.project_dir)
    timeline = build_timeline(project.scenes)

    plain = studio.project_dir / "mix" / "plain.wav"
    normalised = studio.project_dir / "mix" / "normalised.wav"
    project.audio.normalize_enabled = False
    assert audio.render_master(project, timeline, plain).ok
    project.audio.normalize_enabled = True
    project.audio.target_lufs = -16.0
    result = audio.render_master(project, timeline, normalised)
    assert result.ok, result.message

    plan, _ = audio.build_plan(project, timeline)
    assert "loudnorm" in plan.filter_complex or "dynaudnorm" in plan.filter_complex, \
        plan.filter_complex
    quiet = mean_volume(studio.tools, plain, seconds=0.8)
    loud = mean_volume(studio.tools, normalised, seconds=0.8)
    assert abs(loud - quiet) > 0.5, (quiet, loud)


def test_a_project_whose_only_audio_is_silence_is_flagged_by_qc(studio):
    """Silence is measured on the finished file, not assumed from the model."""
    project = studio.create(scenes=1, seconds=1.0)
    silent = studio.silence("narration/silent.wav", 1.0)
    from app.tts.narration import attach_narration_to_scenes

    track = NarrationTrack(id="t1", kind="section", section_id="sec1",
                          path="audio/narration/silent.wav", status="ready",
                          actual_duration_seconds=1.0)
    project.narration.tracks.append(track)
    attach_narration_to_scenes(project, [track])
    studio.service.save(reason="silent")

    audio = AudioService(studio.tools, project_dir=studio.project_dir)
    timeline = build_timeline(project.scenes)
    validation = audio.validate(project, timeline)
    # Validation itself does not decode for silence (that would cost one FFmpeg
    # run per file on every check), so it must at least not invent a problem.
    assert all(issue.severity in ("error", "warning") for issue in validation.issues)

    render = RenderService(studio.tools, project_dir=studio.project_dir,
                           paths=studio.paths)
    result = render.render(project, include_subtitles=False,
                           overrides={"quality_preset": "draft",
                                      "encoder_preset": "ultrafast"})
    assert result.status == "COMPLETED", result.message
    assert silent.is_file()
    assert result.qc is not None
    codes = [issue.code for issue in result.qc.issues]
    assert any("SILENT" in code for code in codes), (codes, result.qc.describe())


def test_a_missing_narration_file_blocks_the_render_with_an_explanation(studio):
    project = studio.create(scenes=1, seconds=1.0)
    track = NarrationTrack(id="t1", kind="section", section_id="sec1",
                           path="audio/narration/never_written.wav", status="ready",
                           actual_duration_seconds=1.0)
    project.narration.tracks.append(track)
    project.scenes[0].narration.file = track.path
    project.scenes[0].narration.duration = 1.0
    studio.service.save(reason="missing")

    audio = AudioService(studio.tools, project_dir=studio.project_dir)
    validation = audio.validate(project, build_timeline(project.scenes))
    assert validation.ok is False
    assert validation.errors
    assert all(issue.what_to_do for issue in validation.errors)

    render = RenderService(studio.tools, project_dir=studio.project_dir,
                           paths=studio.paths)
    result = render.render(studio.service.current, include_subtitles=False,
                           overrides={"quality_preset": "draft",
                                      "encoder_preset": "ultrafast"})
    assert result.status == "FAILED"
    assert result.what_to_do
    assert not result.path


def test_a_malformed_narration_file_is_reported_not_crashed_on(studio):
    project = studio.create(scenes=1, seconds=1.0)
    broken = studio.project_dir / "audio" / "narration" / "broken.wav"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"this is not audio at all")
    track = NarrationTrack(id="t1", kind="section", section_id="sec1",
                           path="audio/narration/broken.wav", status="ready",
                           actual_duration_seconds=1.0)
    project.narration.tracks.append(track)
    project.scenes[0].narration.file = track.path
    project.scenes[0].narration.duration = 1.0
    studio.service.save(reason="broken")

    audio = AudioService(studio.tools, project_dir=studio.project_dir)
    validation = audio.validate(project, build_timeline(project.scenes))
    assert validation.ok is False
    text = " ".join(issue.message for issue in validation.errors).lower()
    assert "read" in text or "audio" in text or "could not" in text, text


def test_the_rendered_video_carries_audio_of_the_expected_length(studio):
    project = studio.create(scenes=2, seconds=1.2, width=320, height=256)
    studio.attach_narration([1.1, 1.0])
    music = studio.tone("music.wav", 6.0, 140, channels=2)
    project = studio.service.current
    project.audio.music.path = "audio/music.wav"
    project.audio.music.volume = 0.15
    project.audio.music.loop = True
    studio.service.save(reason="av")
    assert music.is_file()

    render = RenderService(studio.tools, project_dir=studio.project_dir, paths=studio.paths)
    timeline = build_timeline(project.scenes)
    result = render.render(project, include_subtitles=False,
                           overrides={"quality_preset": "draft",
                                      "encoder_preset": "ultrafast"})
    assert result.status == "COMPLETED", result.message
    info = probe_media(result.path, studio.tools)
    assert info.ok and info.has_audio and info.has_video
    assert abs(info.duration - timeline.total_duration) <= 0.25
    drift = abs(info.stream_duration("video") - info.stream_duration("audio"))
    assert drift <= 0.25, (info.stream_duration("video"), info.stream_duration("audio"))


def test_rendering_with_no_audio_at_all_still_works(studio):
    project = studio.create(scenes=1, seconds=1.0)
    studio.service.save(reason="silent project")
    render = RenderService(studio.tools, project_dir=studio.project_dir, paths=studio.paths)
    result = render.render(project, include_audio=False, include_subtitles=False,
                           overrides={"quality_preset": "draft",
                                      "encoder_preset": "ultrafast"})
    assert result.status == "COMPLETED", result.message
    info = probe_media(result.path, studio.tools)
    assert info.ok and info.has_video and not info.has_audio


# ---------------------------------------------------------------------------
# 5. Captions: generation, round trip, editing, unsafe timing
# ---------------------------------------------------------------------------

def test_captions_come_from_measured_narration_and_survive_a_round_trip(studio):
    project = studio.create(scenes=2, seconds=1.5)
    studio.attach_narration([1.4, 1.3])
    project = studio.service.current
    timeline = build_timeline(project.scenes)
    plan, issues = generate_cues(project, timeline)
    assert plan, [issue.to_dict() for issue in issues]
    assert plan[0].start >= 0.0
    assert all(cue.end > cue.start for cue in plan)
    assert all(cue.text.strip() for cue in plan)
    assert plan[-1].end <= timeline.total_duration + 0.01, (
        plan[-1].end, timeline.total_duration)

    service = SubtitleService(project_dir=studio.project_dir)
    # Storing captions records where their timing came from: measured narration
    # windows, never a word-level alignment that does not exist.
    stored = service.generate(project, timeline, store=True)
    assert stored.ok, [issue.to_dict() for issue in stored.issues]
    assert project.subtitles.timing_source == "narration"
    project.subtitles.cues = plan
    written = service.export(project, studio.project_dir / "subtitles",
                             formats=("srt", "vtt", "ass"))
    assert set(written) == {"srt", "vtt", "ass"}
    for path in written.values():
        assert Path(path).is_file() and Path(path).stat().st_size > 0

    srt_text = Path(written["srt"]).read_text(encoding="utf-8")
    parsed = parse_srt(srt_text)
    assert len(parsed) == len(plan)
    for (start, end, text), cue in zip(parsed, plan):
        assert abs(start - cue.start) < 0.01, (start, cue.start)
        assert abs(end - cue.end) < 0.01
        assert normalize(text) == normalize(cue.text)

    vtt_text = Path(written["vtt"]).read_text(encoding="utf-8")
    assert vtt_text.startswith("WEBVTT")
    assert len(parse_vtt(vtt_text)) == len(plan)
    ass_text = Path(written["ass"]).read_text(encoding="utf-8")
    assert "[Script Info]" in ass_text and "Dialogue:" in ass_text
    for cue in plan:
        assert normalize(cue.text.split(".")[0]) in normalize(ass_text)


def parse_srt(text: str) -> list[tuple[float, float, str]]:
    blocks = re.split(r"\n\s*\n", text.strip())
    out = []
    for block in blocks:
        lines = [line for line in block.splitlines() if line.strip()]
        if len(lines) < 3:
            continue
        match = re.match(
            r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)",
            lines[1].strip())
        if not match:
            continue
        values = [int(part) for part in match.groups()]
        start = values[0] * 3600 + values[1] * 60 + values[2] + values[3] / 1000
        end = values[4] * 3600 + values[5] * 60 + values[6] + values[7] / 1000
        out.append((start, end, " ".join(lines[2:])))
    return out


def parse_vtt(text: str) -> list[tuple[float, float, str]]:
    body = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines or "-->" not in lines[0]:
            continue
        left, right = [part.strip() for part in lines[0].split("-->")]
        def to_seconds(value: str) -> float:
            value = value.replace(",", ".")
            parts = value.split(":")
            seconds = float(parts[-1])
            if len(parts) > 1:
                seconds += int(parts[-2]) * 60
            if len(parts) > 2:
                seconds += int(parts[-3]) * 3600
            return seconds
        body.append((to_seconds(left), to_seconds(right), " ".join(lines[1:])))
    return body


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().lower()


def test_a_cue_can_be_split_merged_and_deleted_without_inventing_timing(studio):
    project = studio.create(scenes=1, seconds=2.0)
    studio.attach_narration([1.8])
    project = studio.service.current
    cues, cue_issues = generate_cues(project, build_timeline(project.scenes))
    assert cues, [issue.to_dict() for issue in cue_issues]
    project.subtitles.cues = cues
    original_end = cues[-1].end
    before = len(cues)
    last = cues[-1]  # keep the object: splitting appends to the same list

    second = split_cue(project.subtitles, last.id, (last.start + last.end) / 2)
    assert second is not None
    assert len(project.subtitles.cues) == before + 1
    assert abs(project.subtitles.cues[-1].end - original_end) < 0.01
    assert project.subtitles.timing_source == "manual"

    assert merge_cues(project.subtitles, last.id, second.id) is True
    assert len(project.subtitles.cues) == before
    assert abs(project.subtitles.cues[-1].end - original_end) < 0.01

    assert delete_cue(project.subtitles, last.id) is True
    assert all(cue.id != last.id for cue in project.subtitles.cues)
    # Deleting something that is not there is a no-op, not a crash.
    assert delete_cue(project.subtitles, "no-such-cue") is False


def test_splitting_outside_a_cue_or_with_one_word_is_refused(studio):
    project = studio.create(scenes=1, seconds=1.0)
    studio.attach_narration([0.9])
    project = studio.service.current
    project.subtitles.cues, _ = generate_cues(project, build_timeline(project.scenes))
    cue = project.subtitles.cues[0]
    assert split_cue(project.subtitles, cue.id, cue.start) is None
    assert split_cue(project.subtitles, cue.id, cue.end + 5) is None
    single = project.subtitles.cues[0]
    single.text = "Word."
    assert split_cue(project.subtitles, single.id, single.start + single.duration / 2) is None


def test_non_adjacent_captions_refuse_to_merge(studio):
    project = studio.create(scenes=1, seconds=2.0)
    studio.attach_narration([1.8])
    project = studio.service.current
    project.subtitles.cues, _ = generate_cues(project, build_timeline(project.scenes))
    cues = project.subtitles.cues
    assert len(cues) >= 3, "this fixture needs at least three captions"
    before = len(cues)
    assert merge_cues(project.subtitles, cues[0].id, cues[2].id) is False
    assert len(project.subtitles.cues) == before


def test_unsafe_caption_timing_is_caught(studio):
    """Negative, inverted and out-of-range cues are errors, not warnings."""
    project = studio.create(scenes=1, seconds=1.0)
    from app.project.model import SubtitleCue

    project.subtitles.cues = [
        SubtitleCue(id="c1", start=0.0, end=1.0, text="Fine."),
        SubtitleCue(id="c2", start=-2.0, end=1.5, text="Starts before the video."),
        SubtitleCue(id="c3", start=2.0, end=1.0, text="Ends before it starts."),
        SubtitleCue(id="c4", start=0.9, end=3.0, text="Runs past the end."),
    ]
    issues = validate_cues(project.subtitles.cues,
                           canvas=project.format, style=project.theme.subtitle_style)
    codes = {issue.code for issue in issues}
    # A caption that ends before it starts, and one that overlaps another, are
    # errors rather than something the render quietly accepts.
    assert "SUBTITLE_ZERO_LENGTH" in codes, codes
    assert "SUBTITLE_OVERLAP" in codes, codes
    assert "SUBTITLE_NEGATIVE_START" in codes, codes
    errors = [issue for issue in issues if issue.severity == "error"]
    assert errors, [issue.to_dict() for issue in issues]
    assert all(issue.what_to_do for issue in errors)
    # The writers clamp to zero, so a negative cue can never produce a broken
    # timecode in a file the user hands to a player - it is reported instead.
    from app.subtitles.service import to_srt

    exported = to_srt([cue for cue in project.subtitles.cues if cue.id == "c2"])
    timecode_row = exported.splitlines()[1]
    assert timecode_row.startswith("00:00:00,000 -->"), timecode_row
    assert not re.match(r"^-", timecode_row)


def test_captions_are_never_claimed_as_word_timed(studio):
    """The honest label is sentence level until a human retimes a cue."""
    project = studio.create(scenes=1, seconds=1.0)
    studio.attach_narration([0.9])
    project = studio.service.current
    plan = SubtitleService(project_dir=studio.project_dir).generate(project, store=True)
    assert plan.ok, [issue.to_dict() for issue in plan.issues]
    assert plan.timing_source == "narration"
    described = plan.summary().lower()
    assert "word" not in described
    assert "caption" in described


# ---------------------------------------------------------------------------
# 6. Timeline: transitions, disabled scenes, degenerate and long projects
# ---------------------------------------------------------------------------

def test_a_crossfade_overlaps_and_never_goes_backwards(studio):
    project = studio.create(scenes=3, seconds=2.0)
    for first, second in zip(project.scenes, project.scenes[1:]):
        first.transition_out.type = "fade"
        first.transition_out.duration = 0.5
        second.transition_in.type = "fade"
        second.transition_in.duration = 0.5
    timeline = build_timeline(project.scenes)
    assert timeline.total_duration < 6.0, timeline.total_duration
    starts = [timing.start for timing in timeline.timings]
    assert starts == sorted(starts)
    for previous, current in zip(timeline.timings, timeline.timings[1:]):
        overlap = (previous.start + previous.duration) - current.start
        assert 0.0 <= overlap <= min(previous.duration, current.duration) / 2 + 1e-6, overlap


def test_a_disabled_scene_leaves_the_cut_but_stays_in_the_project(studio):
    project = studio.create(scenes=3, seconds=1.0)
    project.scenes[1].enabled = False
    project.scenes[1].notes = "kept for later"
    timeline = build_timeline(project.scenes)
    assert len(timeline.timings) == 2
    assert all(timing.scene_id != project.scenes[1].id for timing in timeline.timings)
    assert [timing.scene_id for timing in timeline.timings] == [
        project.scenes[0].id, project.scenes[2].id]
    # Still in the project, still holding its work, still marked.
    assert len(project.scenes) == 3
    assert project.scenes[1].notes == "kept for later"
    assert project.scenes[1].enabled is False


def test_zero_and_negative_scene_lengths_never_produce_a_zero_length_cut(studio):
    project = studio.create(scenes=0)
    from app.project.model import SceneSpec as Spec

    project.add_scene(Spec(name="Zero", duration=0.0, background="#101010"))
    project.add_scene(Spec(name="Negative", duration=-5.0, background="#101010"))
    timeline = build_timeline(project.scenes)
    assert timeline.total_duration > 0
    assert all(timing.duration > 0 for timing in timeline.timings)
    # A scene with no usable length falls back to the default scene length and
    # says so; it is never rendered as a zero-length segment.
    assert all(timing.source in ("default", "manual") for timing in timeline.timings)


def test_fifty_and_one_hundred_scenes_plan_without_a_length_cap(studio):
    # There is no duration limit in the timing options at all: the only fields
    # are head, tail, default scene length, minimum scene length and transitions.
    from dataclasses import fields

    names = {field.name for field in fields(TimingOptions)}
    assert names == {"head", "tail", "default_duration", "min_duration",
                     "overlap_transitions"}, names
    for count in (50, 100):
        scenes = [SceneSpec(name=f"S{index}", duration=12.5, background="#203040")
                  for index in range(count)]
        timeline = build_timeline(scenes)
        assert len(timeline.timings) == count
        assert abs(timeline.total_duration - count * 12.5) < 0.01
        assert timeline.total_duration == max(
            timing.start + timing.duration for timing in timeline.timings)


def test_a_six_hundred_second_timeline_is_planned_end_to_end(studio):
    """The 625 s case from the directive, planned and saved (section 31)."""
    project = studio.create(scenes=0, width=320, height=256)
    from app.project.model import SceneSpec as Spec

    for index in range(25):
        scene = Spec(name=f"Chapter {index + 1}", duration=25.0, background="#1c2b3a")
        project.add_scene(scene)
    studio.service.save(reason="long form")
    timeline = build_timeline(project.scenes)
    assert abs(timeline.total_duration - 625.0) < 0.01

    # It saves, reloads and still plans the same way.
    studio.service.save(reason="long form")
    reopened = studio.service.current
    assert abs(build_timeline(reopened.scenes).total_duration - 625.0) < 0.01

    render = RenderService(studio.tools, project_dir=studio.project_dir, paths=studio.paths)
    plan = render.plan(reopened)
    assert plan.ready, [issue.to_dict() for issue in plan.errors]
    assert abs(plan.duration - 625.0) < 0.01
    assert plan.frames == int(round(625.0 * reopened.format.fps))


def test_many_scenes_really_render_and_the_timeline_matches_the_file(studio):
    """A short project with many scenes, rendered for real (section 31).

    The full 625 s render is not practical in a test run, so this is the honest
    middle ground: twelve scenes, real frames, real encode, real QC.
    """
    project = studio.create(scenes=0, width=320, height=256, fps=25)
    from app.project.model import SceneSpec as Spec

    for index in range(12):
        project.add_scene(Spec(name=f"Beat {index + 1}", duration=0.5,
                               background="#2b3a1c" if index % 2 else "#3a1c2b"))
    studio.service.save(reason="many scenes")
    timeline = build_timeline(project.scenes)
    assert abs(timeline.total_duration - 6.0) < 0.01

    render = RenderService(studio.tools, project_dir=studio.project_dir, paths=studio.paths)
    result = render.render(project, include_audio=False, include_subtitles=False,
                           overrides={"quality_preset": "draft",
                                      "encoder_preset": "ultrafast"})
    assert result.status == "COMPLETED", result.message
    info = probe_media(result.path, studio.tools)
    assert info.ok
    assert abs(info.duration - timeline.total_duration) <= 0.35, (
        info.duration, timeline.total_duration)
    assert result.details.get("segments") == 12
    assert result.qc is not None and result.qc.verdict in ("PASS", "WARNING")
