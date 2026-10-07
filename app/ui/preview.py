"""Playing a clip, when this machine can, and saying so when it cannot.

Qt's multimedia module is built on a system media stack (on Windows that is
Media Foundation).  It is not always present - a stripped build, a missing system
library, or a test environment - so playback is treated as something that may
simply not exist here.  When it does not, the application says so and offers the
two things that always work: open the file in the system's own player, or open
the folder it is in.

Nothing here decodes video itself: this application renders video, and a second
decoder that disagrees with FFmpeg would be one more thing to be wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..core.logging_setup import log_event

__all__ = ["PreviewResult", "preview_video", "preview_supported",
           "open_in_system_player", "open_containing_folder"]


@dataclass
class PreviewResult:
    """What happened when a preview was asked for."""

    opened: bool = False
    message: str = ""
    what_to_do: str = ""

    def describe(self) -> str:
        return " ".join(part for part in (self.message, self.what_to_do) if part)


def preview_supported() -> bool:
    """Whether in-app playback is available on this machine."""
    try:
        from PySide6.QtMultimedia import QMediaPlayer  # noqa: F401
    except Exception:  # noqa: BLE001 - missing module or missing system library
        return False
    return True


def _player_for(path: Path, parent: Any) -> Optional[Any]:
    """A player window, or None when playback is not available here."""
    try:
        from PySide6.QtCore import QUrl
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        from PySide6.QtMultimediaWidgets import QVideoWidget
        from PySide6.QtWidgets import QDialog, QVBoxLayout
    except Exception:  # noqa: BLE001
        return None

    dialog = QDialog(parent)
    dialog.setWindowTitle(f"Preview - {path.name}")
    dialog.resize(720, 440)
    layout = QVBoxLayout(dialog)
    widget = QVideoWidget(dialog)
    layout.addWidget(widget)
    player = QMediaPlayer(dialog)
    try:
        audio = QAudioOutput(dialog)
        player.setAudioOutput(audio)
        dialog._audio = audio  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - silent playback is still playback
        pass
    player.setVideoOutput(widget)
    player.setSource(QUrl.fromLocalFile(str(path)))
    dialog.finished.connect(lambda *_: player.stop())
    player.play()
    return dialog


def preview_video(path: Any, *, parent: Any = None) -> PreviewResult:
    """Play a clip if this machine can, otherwise offer what does work."""
    target = Path(str(path))
    if not target.is_file():
        return PreviewResult(
            message=f"{target.name or 'That clip'} is not there any more.",
            what_to_do="Find the file again, or remove the entry from the library.")
    dialog = _player_for(target, parent)
    if dialog is None:
        return PreviewResult(
            message=("This machine cannot play video inside the application "
                     "(the media component is not available)."),
            what_to_do=("The file itself is fine: open it in your usual player, "
                        "or open the folder and play it from there."))
    try:
        dialog.show()
    except Exception as exc:  # noqa: BLE001 - a preview is never worth a crash
        log_event("VIDEO_PREVIEW_FAILED", f"The preview could not open: {exc}",
                  level="WARNING")
        return PreviewResult(
            message=f"The preview could not open: {exc}",
            what_to_do="Use the system player instead.")
    return PreviewResult(opened=True, message=f"Playing {target.name}.")


def open_in_system_player(path: Any) -> PreviewResult:
    """Hand the file to the system's own player, which always exists."""
    target = Path(str(path))
    if not target.is_file():
        return PreviewResult(message=f"{target.name or 'That clip'} is not there.",
                             what_to_do="Find the file again.")
    try:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        if QDesktopServices.openUrl(QUrl.fromLocalFile(str(target))):
            return PreviewResult(opened=True,
                                 message=f"{target.name} was opened.")
    except Exception:  # noqa: BLE001
        pass
    try:
        from .notifications import open_path_in_explorer

        if open_path_in_explorer(target):
            return PreviewResult(
                opened=True,
                message=(f"{target.name} could not be opened in a player, so its "
                         f"folder was opened instead."))
    except Exception as exc:  # noqa: BLE001
        log_event("VIDEO_OPEN_FAILED", f"The file could not be opened: {exc}",
                  level="WARNING")
    return PreviewResult(
        message=f"{target.name} could not be opened.",
        what_to_do=f"The file is at {target} - open it from there.")


def open_containing_folder(path: Any) -> PreviewResult:
    """Show the clip in the system's file browser."""
    target = Path(str(path))
    if not target.exists():
        return PreviewResult(message=f"{target} is not there.",
                             what_to_do="Find the file again.")
    try:
        from .notifications import open_path_in_explorer

        if open_path_in_explorer(target):
            return PreviewResult(opened=True, message="The folder was opened.")
    except Exception as exc:  # noqa: BLE001
        log_event("VIDEO_FOLDER_OPEN_FAILED", f"The folder could not be opened: {exc}",
                  level="WARNING")
    return PreviewResult(message="The folder could not be opened.",
                         what_to_do=f"The file is at {target}.")
