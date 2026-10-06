"""Media inspection utilities (Stage E).

Importing this package does nothing but expose names - no subprocess is started,
no file is read and nothing is rendered at import time (directive section 84).
"""

from __future__ import annotations

from .probe import MediaInfo, StreamInfo, format_timecode, parse_ffmpeg_info, probe_media

__all__ = [
    "MediaInfo",
    "StreamInfo",
    "format_timecode",
    "parse_ffmpeg_info",
    "probe_media",
]
