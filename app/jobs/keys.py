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

    # -- Stage G (AI Studio) ----------------------------------------------
    AI_DETECT = "ai.detect"
    AI_MODEL_CHECK = "ai.model_check"
    AI_BACKEND_TEST = "ai.backend_test"
    AI_VIDEO_GENERATE = "ai.video.generate"
    AI_VIDEO_BATCH = "ai.video.batch"
    AI_VIDEO_EXTEND = "ai.video.extend"
    AI_VIDEO_UPSCALE = "ai.video.upscale"
    AI_IMAGE_GENERATE = "ai.image.generate"
    AI_HISTORY_SCAN = "ai.history_scan"
    AI_REFERENCES_IMPORT = "ai.references.import"
    AI_SEND_TO_PROJECT = "ai.send_to_project"
    AI_SEND_TO_SCENE = "ai.send_to_scene"
    AI_SEND_TO_TIMELINE = "ai.send_to_timeline"

    # -- Stage G (Video Library) ------------------------------------------
    VIDEO_LIBRARY_SCAN = "video.library.scan"
    VIDEO_LIBRARY_IMPORT = "video.library.import"
    VIDEO_LIBRARY_THUMBNAILS = "video.library.thumbnails"
    VIDEO_LIBRARY_REMOVE = "video.library.remove"
    VIDEO_LIBRARY_RECHECK = "video.library.recheck"


#: Keys that are allowed to run in parallel with themselves (used only where
#: duplication is impossible, e.g. read-only scans).
PARALLEL_SAFE_KEYS: frozenset[str] = frozenset(
    {
        JobKeys.STORAGE_REPORT,
        JobKeys.ENVIRONMENT_PROBE,
    }
)
