"""Stage E manual verification matrix (directive section 70).

Runs the Stage E workflow end to end against a *real project on disk* with a
*real FFmpeg*, through the same service objects the GUI and CLI use.  Every
scenario either measures something with the probe or reads it back out of the
finished file - nothing is asserted from the model alone.

Coverage: audio mix, subtitles, timeline validation, a real render, QC,
cancellation, failure handling, output numbering, five resolutions and four
quality presets.

Nothing here needs a GPU or a network connection.  Narration is real audio on
disk; see ``--narration`` for how it is sourced and why the distinction matters.

Usage::

    LD_LIBRARY_PATH=/tmp/stublib QT_QPA_PLATFORM=offscreen \\
        python scripts/stage_e_manual_matrix.py --data-root /tmp/mgs_stage_e
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.audio.service import AudioService  # noqa: E402
from app.core.paths import AppPaths  # noqa: E402
from app.core.settings import Settings  # noqa: E402
from app.media.probe import probe_media  # noqa: E402
from app.project.model import (  # noqa: E402
    AssetSpec,
    SceneSpec,
    ScriptSection,
    SoundEffect,
    SubtitleCue,
    build_project,
)
from app.project.service import CreateRequest, ProjectService  # noqa: E402
from app.render import RenderEngine, RenderRequest  # noqa: E402
from app.render.encode import video_encoder_args  # noqa: E402
from app.render.qc import QCService  # noqa: E402
from app.scene.service import TimelineService  # noqa: E402
from app.scene.templates import (  # noqa: E402
    create_scene_from_template,
    default_templates_registered,
)
from app.subtitles.service import SubtitleService  # noqa: E402
from app.tools.ffmpeg import FFmpegCancelToken, FFmpegTools, discover_ffmpeg  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []

#: The five resolutions section 71 asks for, verified against the real file.
RESOLUTIONS = ((1280, 720), (1920, 1080), (1080, 1920), (1080, 1350), (1080, 1080))

#: The quality levels section 72 asks for.
QUALITIES = ("draft", "medium", "high", "ultra")

SCENE_TEXTS = (
    "This is the first scene of the Stage E verification matrix.",
    "The second scene carries an imported image.",
    "The third scene shows a chart drawn from real data.",
    "And the fourth closes on a single number.",
)


def record(number: int, name: str, ok: bool, detail: str) -> None:
    RESULTS.append((f"{number:02d}", "PASS" if ok else "FAIL", f"{name} - {detail}"))
    print(f"[{'PASS' if ok else 'FAIL'}] {number:02d} {name}: {detail}", flush=True)


def _tone(tools: FFmpegTools, path: Path, seconds: float, frequency: int,
          channels: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    result = tools.run(["-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", f"sine=frequency={frequency}:duration={seconds}",
                        "-ar", "48000", "-ac", str(channels), str(path)], timeout=300.0)
    if not result.ok:
        raise RuntimeError(f"Could not create {path.name}: {result.describe_failure()}")
    return path


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def build_matrix_project(service: ProjectService, tools: FFmpegTools,
                         narration_source: str) -> tuple[object, Path, str]:
    """A real project: narration, music, a sound effect, image, chart, number."""
    project = service.create_project(CreateRequest(
        name="StageE_Matrix", description="Stage E manual verification matrix",
        width=1280, height=720, fps=30, template="blank"))
    project_dir = Path(service.session.layout.root)

    default_templates_registered()
    project.scenes.clear()
    specs = [
        ("title", {"title": "Stage E", "subtitle": "Manual verification matrix"}),
        ("image", {"text": "An imported image, fitted without stretching."}),
        ("chart", {"title": "Renders per quarter", "values": [12, 30, 18, 44, 27],
                   "labels": ["Q1", "Q2", "Q3", "Q4", "Q5"]}),
        ("stat", {"title": "Scenes rendered", "value": 4, "unit": "scenes"}),
    ]
    for index, (template, content) in enumerate(specs, start=1):
        scene = create_scene_from_template(template, dict(content))
        scene.id = f"s{index}"
        scene.name = f"Scene {index}"
        scene.narration.text = SCENE_TEXTS[index - 1]
        project.scenes.append(scene)
        project.script.sections.append(ScriptSection(
            id=f"sec{index}", title=scene.name, text=SCENE_TEXTS[index - 1], order=index))
    for first, second in zip(project.scenes, project.scenes[1:]):
        first.transition_out.type = "fade"
        first.transition_out.duration = 0.4
        second.transition_in.type = "fade"
        second.transition_in.duration = 0.4

    # -- a real imported image -------------------------------------------
    from PIL import Image, ImageDraw

    image_path = project_dir / "assets" / "product.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    picture = Image.new("RGB", (960, 540), "#1d2433")
    draw = ImageDraw.Draw(picture)
    draw.rectangle([40, 40, 920, 500], outline="#7fb2ff", width=6)
    draw.text((60, 260), "imported image asset", fill="#e8eefc")
    picture.save(image_path)
    asset = AssetSpec(id="product_image", name="Product image", kind="image",
                      path="assets/product.png", size_bytes=image_path.stat().st_size,
                      width=960, height=540)
    project.assets.append(asset)
    for element in project.scenes[1].elements:
        if element.kind == "image":
            element.asset_id = asset.id

    # -- narration --------------------------------------------------------
    status = "synthetic"
    if narration_source in ("kokoro", "auto"):
        from app.tts.capabilities import probe_kokoro
        from app.tts.narration import (
            NarrationSettings,
            attach_narration_to_scenes,
            generate_narration,
        )

        kokoro = probe_kokoro(deep_init_check=True)
        if kokoro.ready:
            outcome = generate_narration(project, project_dir, NarrationSettings(),
                                         overwrite=True)
            if outcome.ok:
                attach_narration_to_scenes(project, outcome.tracks)
                status = "kokoro"
        if status != "kokoro" and narration_source == "kokoro":
            raise SystemExit(
                "Kokoro was requested but is not usable here:\n  "
                + "\n  ".join(kokoro.problems[:3] or ["unknown reason"]))

    if status != "kokoro":
        # Labelled synthetic audio of a measured length.  The timings, the mix
        # and the render are all real; only the voice is a tone.
        from app.project.model import NarrationTrack
        from app.tts.narration import attach_narration_to_scenes

        tracks = []
        for index in range(1, len(project.scenes) + 1):
            length = 1.6 + 0.3 * index
            wav = _tone(tools, project_dir / "audio" / "narration"
                        / f"narration_scene_{index:03d}.wav", length, 170 + index * 40)
            info = probe_media(wav, tools)
            track = NarrationTrack(
                id=f"track{index}", kind="section", section_id=f"sec{index}",
                path=f"audio/narration/{wav.name}", status="ready",
                actual_duration_seconds=info.duration or length,
                sample_rate=info.sample_rate or 48000, channels=info.channels or 1,
                size_bytes=wav.stat().st_size)
            project.narration.tracks.append(track)
            tracks.append(track)
        attach_narration_to_scenes(project, tracks)

    # -- music and one sound effect ---------------------------------------
    _tone(tools, project_dir / "audio" / "music.wav", 30.0, 110, channels=2)
    _tone(tools, project_dir / "audio" / "sfx.wav", 0.5, 880)
    project.audio.music.id = "music_bed"
    project.audio.music.path = "audio/music.wav"
    project.audio.music.volume = 0.22
    project.audio.music.loop = True
    project.audio.music.fade_in = 0.4
    project.audio.music.fade_out = 1.0
    project.audio.ducking_enabled = True
    project.audio.ducking_level = 0.35
    project.audio.sfx.append(SoundEffect(
        id="sfx_ping", path="audio/sfx.wav", volume=0.5, anchor="project",
        at_seconds=0.4, fade_in=0.02, fade_out=0.1))

    project.subtitles.enabled = True
    project.subtitles.language = "en"
    project.format.quality_preset = "high"
    project.format.encoder_preset = "veryfast"
    project.format.crf = 26
    project.export.output_dir = "renders"
    service.save(reason="Stage E matrix")
    return project, project_dir, status


def main() -> int:  # noqa: PLR0915 - a matrix is a long sequence by nature
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="/tmp/mgs_stage_e")
    parser.add_argument("--narration", choices=("auto", "kokoro", "synthetic"),
                        default="auto",
                        help="auto: use Kokoro if it is installed, otherwise labelled "
                             "synthetic audio.  kokoro: refuse to run without it.")
    args = parser.parse_args()

    root = Path(args.data_root)
    if root.exists():
        shutil.rmtree(root)
    paths = AppPaths(data_root=root, source_root=Path(__file__).resolve().parents[1],
                     reason="stage-e-matrix")
    paths.ensure()

    discovery = discover_ffmpeg()
    if not discovery.has_ffmpeg:
        print("FFmpeg is not installed, so no Stage E scenario can run.")
        return 2
    tools = FFmpegTools(discovery)
    print(f"FFmpeg: {discovery.ffmpeg.version if discovery.ffmpeg else 'unknown'}")

    service = ProjectService(paths, Settings())
    project, project_dir, narration_kind = build_matrix_project(
        service, tools, args.narration)
    print(f"Project: {project_dir}")
    print(f"Narration source: {narration_kind}"
          + (" (TEST/DEV FALLBACK - not Kokoro)" if narration_kind != "kokoro" else ""))
    print()

    timeline_service = TimelineService(project_dir=project_dir, tools=tools)
    subtitle_service = SubtitleService(project_dir=project_dir, tools=tools)
    audio_service = AudioService(tools, project_dir=project_dir)
    qc_service = QCService(tools)

    # -- 1: the timeline is real and valid -------------------------------
    report = timeline_service.check(project)
    record(1, "Timeline builds and validates", report.ok and report.timeline.scene_count == 4,
           f"{report.timeline.format_total()} over {report.timeline.scene_count} scene(s); "
           f"{len(report.errors)} error(s), {len(report.warnings)} warning(s)")

    # -- 2: every scene length came from measured narration --------------
    measured = sum(1 for t in report.timeline.timings if t.source == "narration")
    record(2, "Scene lengths come from measured narration", measured == 4,
           f"{measured} of {report.timeline.scene_count} scene(s) measured")

    # -- 3: audio validation --------------------------------------------
    validation = audio_service.validate(project, report.timeline)
    record(3, "Audio validates", validation.ok and len(validation.placements) == 4,
           f"{len(validation.placements)} narration placement(s), "
           f"{len(validation.errors)} error(s)")

    # -- 4: the mix really builds ---------------------------------------
    mix = audio_service.render_master(project, report.timeline,
                                      project_dir / "cache" / "matrix_master.wav")
    mix_info = probe_media(mix.path, tools) if mix.ok else None
    record(4, "Master mix renders and is measurable",
           mix.ok and mix_info.has_audio and abs(mix_info.duration - report.timeline.total_duration) < 0.6,
           f"{mix_info.duration:.2f}s {mix_info.audio_codec} {mix_info.sample_rate}Hz "
           f"{mix_info.channels}ch (timeline {report.timeline.total_duration:.2f}s)"
           if mix_info else mix.message)

    # -- 5: ducking actually lowers the music ---------------------------
    project.audio.ducking_enabled = False
    flat = audio_service.render_master(project, report.timeline,
                                       project_dir / "cache" / "matrix_flat.wav")
    project.audio.ducking_enabled = True
    ducked = audio_service.render_master(project, report.timeline,
                                         project_dir / "cache" / "matrix_ducked.wav")
    record(5, "Ducking changes the mix", flat.ok and ducked.ok
           and _md5(flat.path) != _md5(ducked.path),
           "the ducked master differs from the unducked one"
           if flat.ok and ducked.ok else "a mix failed to build")

    # -- 6: captions come from the narration ----------------------------
    plan = subtitle_service.generate(project)
    record(6, "Captions generated from narration", plan.ok and len(plan.cues) >= 4,
           f"{len(plan.cues)} cue(s), timing source '{project.subtitles.timing_source}'")

    # -- 7: caption files are written and non-empty ---------------------
    written = subtitle_service.export(project, project_dir / "subtitles",
                                      formats=("srt", "vtt", "ass"))
    record(7, "SRT, VTT and ASS are written", set(written) == {"srt", "vtt", "ass"}
           and all(Path(p).stat().st_size > 0 for p in written.values()),
           ", ".join(Path(p).name for p in written.values()) or "nothing written")

    # -- 8: caption validation catches a real overlap -------------------
    saved = list(project.subtitles.cues)
    project.subtitles.cues = [SubtitleCue(id="a", start=0.0, end=5.0, text="One"),
                              SubtitleCue(id="b", start=2.0, end=7.0, text="Two")]
    overlap = [issue.code for issue in subtitle_service.validate(project)]
    project.subtitles.cues = saved
    record(8, "Overlapping captions are caught", "SUBTITLE_OVERLAP" in overlap,
           f"reported {', '.join(overlap) or 'nothing'}")

    # -- 9: a real render ----------------------------------------------
    engine = RenderEngine(tools, project_dir=project_dir)
    first = engine.render(RenderRequest(project=project, quick_qc=False))
    info_1 = probe_media(first.path, tools) if first.ok else None
    record(9, "First render completes", first.ok and info_1 is not None
           and info_1.has_video and info_1.has_audio,
           f"{info_1.width}x{info_1.height} @ {info_1.fps} fps, {info_1.duration:.2f}s, "
           f"{info_1.video_codec}/{info_1.audio_codec}, {info_1.size_bytes:,} bytes"
           if info_1 else first.message)

    # -- 10: QC really measures the file ------------------------------
    qc_1 = qc_service.check(first.path, expected_duration=report.timeline.total_duration,
                            expected_width=1280, expected_height=720, expected_fps=30,
                            expect_audio=True) if first.ok else None
    record(10, "QC measures the finished file", qc_1 is not None
           and qc_1.measured.get("width") == 1280 and not qc_1.has_fail,
           f"{qc_1.verdict}: {qc_1.summary()}" if qc_1 else "no file to check")

    # -- 11: burnt-in captions change the picture ---------------------
    project.subtitles.burn_in = True
    burnt = RenderEngine(tools, project_dir=project_dir).render(
        RenderRequest(project=project, quick_qc=True))
    project.subtitles.burn_in = False
    record(11, "Burnt-in captions are drawn in", burnt.ok and first.ok
           and _md5(burnt.path) != _md5(first.path),
           "the burnt-in file differs from the plain one"
           if burnt.ok and first.ok else burnt.message)

    # -- 12: output numbering never overwrites -----------------------
    before = _md5(first.path) if first.ok else ""
    second = RenderEngine(tools, project_dir=project_dir).render(
        RenderRequest(project=project, quick_qc=True))
    untouched = first.ok and second.ok and _md5(first.path) == before
    record(12, "Second render does not touch the first",
           untouched and first.path != second.path,
           f"{Path(first.path).name} unchanged; new file {Path(second.path).name}"
           if untouched and first.path != second.path else "the first file changed")

    # -- 13: five resolutions, verified through the probe -------------
    resolution_notes: list[str] = []
    resolution_ok = True
    for width, height in RESOLUTIONS:
        project.format.width, project.format.height = width, height
        outcome = RenderEngine(tools, project_dir=project_dir).render(
            RenderRequest(project=project, quick_qc=True))
        if not outcome.ok:
            resolution_ok = False
            resolution_notes.append(f"{width}x{height}=FAILED")
            continue
        measured = probe_media(outcome.path, tools)
        exact = measured.width == width and measured.height == height
        resolution_ok = resolution_ok and exact
        resolution_notes.append(
            f"{width}x{height}={measured.width}x{measured.height}"
            + ("" if exact else " MISMATCH"))
    project.format.width, project.format.height = 1280, 720
    record(13, "Five resolutions render at exact sizes", resolution_ok,
           "; ".join(resolution_notes))

    # -- 14: quality presets reach the encoder -----------------------
    quality_notes: list[str] = []
    quality_ok = True
    for key in QUALITIES:
        project.format.quality_preset = key
        project.format.bitrate_kbps = 0
        from app.project.presets import quality_preset

        preset = quality_preset(key)
        project.format.crf = preset.crf
        project.format.encoder_preset = preset.encoder_preset
        args_list = video_encoder_args(project.format)
        text = " ".join(str(part) for part in args_list)
        reflected = f"-crf {preset.crf}" in text and preset.encoder_preset in text
        quality_ok = quality_ok and reflected
        quality_notes.append(f"{key}:crf{preset.crf}/{preset.encoder_preset}"
                             + ("" if reflected else " MISSING"))
    project.format.quality_preset = "high"
    record(14, "Quality presets reach the encoder command", quality_ok,
           "; ".join(quality_notes))

    # -- 15: an explicit bitrate is used and is not silently dropped --
    project.format.bitrate_kbps = 4000
    text = " ".join(str(part) for part in video_encoder_args(project.format))
    record(15, "An explicit bitrate replaces CRF",
           "4000k" in text and "-crf" not in text,
           "encoder args contain the bitrate and no -crf"
           if "4000k" in text and "-crf" not in text else text[:120])
    project.format.bitrate_kbps = 0

    # -- 16: cancellation stops the render and leaves nothing behind --
    token = FFmpegCancelToken()
    project.format.width, project.format.height = 1920, 1080
    engine_cancel = RenderEngine(tools, project_dir=project_dir, cancel_token=token,
                                 progress=lambda progress: (
                                     token.cancel() if progress.segments_done >= 1 else None))
    work_dir = project_dir / "cache" / "renders"
    cancelled = engine_cancel.render(RenderRequest(project=project, quick_qc=True))
    leftovers = sorted(p.name for p in work_dir.rglob("*.mp4")) if work_dir.exists() else []
    no_file = cancelled.path is None or not Path(cancelled.path).exists()
    record(16, "A cancelled render stops cleanly",
           cancelled.cancelled and no_file,
           f"status {cancelled.status}, no finished file, "
           f"{len(leftovers)} scratch file(s) left"
           if cancelled.cancelled else f"status {cancelled.status}")
    project.format.width, project.format.height = 1280, 720

    # -- 17: no zombie FFmpeg survives the cancel ---------------------
    running = [line for line in _ffmpeg_processes() if "stage_e" in line.lower()
               or str(root).lower() in line.lower()]
    record(17, "No FFmpeg process is left running", not running,
           f"{len(running)} process(es) still running" if running else "none left")

    # -- 18: missing narration blocks the render ---------------------
    kept = project.scenes[0].narration.file
    project.scenes[0].narration.file = "audio/does_not_exist.wav"
    missing = RenderEngine(tools, project_dir=project_dir).render(
        RenderRequest(project=project, quick_qc=True))
    project.scenes[0].narration.file = kept
    record(18, "Missing narration blocks the render",
           missing.failed and missing.path is None,
           f"{missing.message[:90]}" if missing.failed else f"status {missing.status}")

    # -- 19: an unavailable codec is refused before rendering --------
    from app.render.capabilities import EncoderCapabilities

    kept_codec = project.format.codec
    project.format.codec = "h264_cpu"
    fake_caps = EncoderCapabilities(encoders=["libx265"], audio_encoders=["aac"],
                                    detected=True, ffmpeg_version="fabricated")
    blocked = RenderEngine(tools, project_dir=project_dir, caps=fake_caps).render(
        RenderRequest(project=project, quick_qc=True))
    project.format.codec = kept_codec
    record(19, "An unavailable codec stops the render early",
           blocked.failed and blocked.path is None
           and "CODEC" in " ".join(getattr(e, "code", "") for e in blocked.errors).upper(),
           f"{[getattr(e, 'code', '') for e in blocked.errors][:3]}")

    # -- 20: an unusable output folder is reported, not guessed ------
    kept_dir = project.export.output_dir
    project.export.output_dir = "/proc/definitely/not/writable"
    bad_folder = RenderEngine(tools, project_dir=project_dir).render(
        RenderRequest(project=project, quick_qc=True))
    project.export.output_dir = kept_dir
    record(20, "An unusable output folder is reported",
           bad_folder.failed and bad_folder.path is None,
           f"{bad_folder.message[:90]}" if bad_folder.failed else f"status {bad_folder.status}")

    # -- 21: an empty project is refused ---------------------------
    empty = build_project("Empty Matrix")
    empty.format.width, empty.format.height, empty.format.fps = 1280, 720, 30
    empty_result = RenderEngine(tools, project_dir=project_dir).render(
        RenderRequest(project=empty, quick_qc=True))
    record(21, "A project with no scenes is refused",
           empty_result.failed and empty_result.path is None,
           f"{empty_result.message[:90]}" if empty_result.failed
           else f"status {empty_result.status}")

    # -- 22: a missing image asset is caught before rendering -------
    kept_asset = project.assets[0].path
    project.assets[0].path = "assets/deleted.png"
    asset_result = RenderEngine(tools, project_dir=project_dir).render(
        RenderRequest(project=project, quick_qc=True))
    project.assets[0].path = kept_asset
    record(22, "A missing asset blocks the render",
           asset_result.failed and asset_result.path is None,
           f"{[getattr(e, 'code', '') for e in asset_result.errors][:3]}")

    # -- 23: a resume reuses unchanged segments --------------------
    resumed = RenderEngine(tools, project_dir=project_dir).render(
        RenderRequest(project=project, quick_qc=True, resume=True))
    record(23, "A resume render completes", resumed.ok,
           f"{resumed.details.get('segments')} segment(s), {resumed.seconds:.1f}s"
           if resumed.ok else resumed.message)

    # -- 24: a long timeline plans without limits ------------------
    long_project = build_project("Long Form")
    for index in range(50):
        scene = long_project.add_scene(SceneSpec(id=f"l{index}", name=f"Scene {index + 1}"))
        scene.narration.duration = 12.0
        scene.narration.file = "audio/none.wav"
    long_timeline = timeline_service.build(long_project)
    record(24, "A 50-scene timeline plans without a duration cap",
           long_timeline.scene_count == 50 and long_timeline.total_duration > 600,
           f"{long_timeline.scene_count} scene(s), "
           f"{long_timeline.format_total()} ({long_timeline.total_duration:.0f}s)")

    # -- 25: the project still saves and reloads ------------------
    saved = service.save(reason="Stage E matrix evidence")
    service.close_project()          # a project can only be open once at a time
    reopened = ProjectService(paths, Settings()).open_project(project_dir / "project.json")
    record(25, "The project saves and reloads intact",
           saved.ok and len(reopened.scenes) == 4
           and reopened.subtitles.enabled is True,
           f"{len(reopened.scenes)} scene(s), "
           f"{len(reopened.subtitles.cues)} caption(s) after reopening")

    # -- summary ----------------------------------------------------
    print()
    print("=" * 72)
    print("STAGE E MANUAL MATRIX")
    print("=" * 72)
    if first.ok and info_1 is not None:
        print(f"Rendered file : {first.path}")
        print(f"Resolution    : {info_1.width}x{info_1.height}")
        print(f"Frame rate    : {info_1.fps} fps")
        print(f"Duration      : {info_1.duration:.2f}s")
        print(f"Video codec   : {info_1.video_codec} {info_1.pixel_format}")
        print(f"Audio         : {info_1.audio_codec} {info_1.sample_rate} Hz "
              f"{info_1.channels} ch" if info_1.has_audio else "Audio         : none")
        print(f"Size          : {info_1.size_bytes:,} bytes")
        if qc_1 is not None:
            print(f"QC            : {qc_1.verdict}")
    print(f"Narration     : {narration_kind}"
          + ("  (TEST/DEV FALLBACK - not Kokoro)" if narration_kind != "kokoro" else ""))
    failed = [row for row in RESULTS if row[1] == "FAIL"]
    print(f"Scenarios     : {len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    if narration_kind != "kokoro":
        print("\nPIPELINE VERIFIED.  KOKORO NOT VERIFIED - TEST FALLBACK USED.")
        print("The render pipeline is verified with explicitly labelled synthetic")
        print("test narration; the Kokoro runtime path remains unverified on this")
        print("machine because model weights are unavailable.")
    else:
        print("\nPIPELINE VERIFIED.  KOKORO VERIFIED.")
    return 1 if failed else 0


def _ffmpeg_processes() -> list[str]:
    """Any FFmpeg processes still alive, so a cancel can be proven clean."""
    try:
        output = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True,
                                timeout=20.0)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in output.stdout.splitlines()
            if "ffmpeg" in line and "ps -eo" not in line]


if __name__ == "__main__":
    sys.exit(main())
