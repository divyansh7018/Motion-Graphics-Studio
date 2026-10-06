"""Stable job keys.

Every background operation submits its job under a key from this list.  The job
manager refuses a second job with the same key while one is running, which is
how the application guarantees "one user action = one job" (directive section 9)
without scattering string literals through the code base.
"""

from __future__ import annotations


class JobKeys:
    """Identifiers for background jobs (stable - they appear in logs)."""

    # -- Stage A ----------------------------------------------------------
    SYSTEM_CHECK = "system.check"
    ENVIRONMENT_PROBE = "system.environment"
    CACHE_CLEANUP = "maintenance.clear_cache"
    STORAGE_REPORT = "maintenance.storage_report"
    SMOKE_TEST = "diagnostics.smoke_test"

    # -- Stage B (project) -------------------------------------------------
    PROJECT_SAVE = "project.save"
    PROJECT_LOAD = "project.load"
    PROJECT_ANALYSE = "project.analyse"
    THUMBNAIL_BATCH = "assets.thumbnails"

    # -- Stage C (voice) ---------------------------------------------------
    KOKORO_INIT = "voice.init"
    VOICE_SCAN = "voice.scan"
    VOICE_PREVIEW = "voice.preview"
    TTS_NARRATION = "voice.narration"

    # -- Stage D/E (scenes, audio) ----------------------------------------
    SCENE_PREVIEW = "scene.preview"
    STORYBOARD_RENDER = "storyboard.render"
    AUDIO_MIX = "audio.mix"
    AUDIO_VALIDATE = "audio.validate"
    SUBTITLE_BUILD = "subtitles.build"
    SUBTITLE_EXPORT = "subtitles.export"
    TIMELINE_CHECK = "timeline.check"

    # -- Stage F/G (render, QC) -------------------------------------------
    RENDER_PREVIEW = "render.preview"
    RENDER_FINAL = "render.final"
    RENDER_CAPABILITIES = "render.capabilities"
    RENDER_PLAN = "render.plan"
    QC_RUN = "qc.run"
    OUTPUT_VALIDATE = "output.validate"

    # -- Stage F (Image Studio) -------------------------------------------
    IMAGE_IMPORT = "image.import"
    IMAGE_GENERATE = "image.generate"
    IMAGE_BATCH = "image.batch"
    IMAGE_EDIT_SAVE = "image.edit_save"
    IMAGE_UPSCALE = "image.upscale"
    IMAGE_DETECT = "image.detect"
    IMAGE_LIBRARY_SCAN = "image.library_scan"
    IMAGE_THUMBNAILS = "image.thumbnails"


#: Keys that are allowed to run in parallel with themselves (used only where
#: duplication is impossible, e.g. read-only scans).
PARALLEL_SAFE_KEYS: frozenset[str] = frozenset(
    {
        JobKeys.STORAGE_REPORT,
        JobKeys.ENVIRONMENT_PROBE,
    }
)
