"""User-facing notifications: friendly errors, confirmations, results.

Everything the user sees about a failure goes through :func:`show_error`, so
the three questions from directive section 40 are always answered:

* **What happened** (headline),
* **Why** it happened,
* **What you can do** about it,

with the technical detail and the log file available behind "Show details".
No dialog ever says only "Something went wrong".
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QMessageBox, QWidget

from ..core.errors import FriendlyError
from ..core.logging_setup import active_log_file, get_logger

LOGGER = get_logger("ui")


def _base_box(parent: Optional[QWidget], icon: QMessageBox.Icon, title: str, friendly: FriendlyError) -> QMessageBox:
    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setText(friendly.what_happened.strip() or friendly.title)

    extra_lines: list[str] = []
    if friendly.why:
        extra_lines.append(friendly.why.strip())
    if friendly.actions:
        extra_lines.append("What you can do:")
        extra_lines.extend(f"  {index}. {action}" for index, action in enumerate(friendly.actions, start=1))
    box.setInformativeText("\n".join(extra_lines))

    detail_parts = [f"Log file: {active_log_file() or 'not available'}"]
    if friendly.error_code:
        detail_parts.append(f"Code: {friendly.error_code}")
    if friendly.technical:
        detail_parts.append("")
        detail_parts.append(friendly.technical.strip())
    box.setDetailedText("\n".join(detail_parts))
    return box


def show_error(parent: Optional[QWidget], friendly: FriendlyError, title: str = "Problem") -> None:
    """Show a failure with a full explanation and a copy-details button."""
    box = _base_box(parent, QMessageBox.Critical, title or friendly.title, friendly)
    copy_button = box.addButton("Copy details", QMessageBox.ActionRole)
    log_button = box.addButton("Open log folder", QMessageBox.ActionRole)
    box.addButton(QMessageBox.Close)
    box.exec()

    clicked = box.clickedButton()
    if clicked is copy_button:
        copy_to_clipboard(box_to_text(friendly))
    elif clicked is log_button:
        open_log_folder(parent)


def show_warning(parent: Optional[QWidget], friendly: FriendlyError, title: str = "Warning") -> None:
    box = _base_box(parent, QMessageBox.Warning, title or friendly.title, friendly)
    box.addButton(QMessageBox.Ok)
    box.exec()


def show_info(parent: Optional[QWidget], text: str, informative: str = "", title: str = "Motion Graphics Studio") -> None:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Information)
    box.setWindowTitle(title)
    box.setText(text)
    if informative:
        box.setInformativeText(informative)
    box.addButton(QMessageBox.Ok)
    box.exec()


def ask_confirm(
    parent: Optional[QWidget],
    text: str,
    informative: str = "",
    confirm_label: str = "Continue",
    dangerous: bool = False,
) -> bool:
    """Ask a yes/no question.  Default button is *Cancel* for destructive acts."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning if dangerous else QMessageBox.Question)
    box.setWindowTitle("Please confirm")
    box.setText(text)
    if informative:
        box.setInformativeText(informative)
    confirm_button = box.addButton(confirm_label, QMessageBox.AcceptRole)
    cancel_button = box.addButton(QMessageBox.Cancel)
    box.setDefaultButton(cancel_button)
    box.exec()
    return box.clickedButton() is confirm_button


def box_to_text(friendly: FriendlyError) -> str:
    """Plain text version of an error, used by "Copy details"."""
    lines = [f"[{friendly.severity.value.upper()}] {friendly.title}", "", friendly.what_happened, ""]
    if friendly.why:
        lines.extend([f"Why: {friendly.why}", ""])
    for index, action in enumerate(friendly.actions, start=1):
        lines.append(f"{index}. {action}")
    lines.extend(["", f"Log: {active_log_file() or 'not available'}"])
    if friendly.error_code:
        lines.append(f"Code: {friendly.error_code}")
    if friendly.technical:
        lines.extend(["", "Technical details:", friendly.technical])
    return "\n".join(lines)


def copy_to_clipboard(text: str) -> None:
    clipboard = QGuiApplication.clipboard()
    if clipboard is not None:
        clipboard.setText(text)


def open_path_in_explorer(path) -> bool:
    """Open a folder (or file location) in the system file manager."""
    from pathlib import Path

    target = Path(path)
    if not target.exists():
        return False
    try:
        if target.is_dir():
            return QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        return QDesktopServices.openUrl(QUrl.fromLocalFile(str(target.parent)))
    except Exception:  # pragma: no cover - platform specific
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(target))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
            return True
        except Exception:
            return False


def open_log_folder(parent: Optional[QWidget] = None) -> None:
    path = active_log_file()
    if path is None:
        show_info(parent, "Logging is not available.", "The log folder could not be created - check that the data folder is writable.")
        return
    if not open_path_in_explorer(path):
        show_info(parent, "The log folder could not be opened.", f"Open it manually:\n{path.parent}")


def open_folder(parent: Optional[QWidget], path) -> None:
    if not open_path_in_explorer(path):
        show_info(parent, "This folder could not be opened.", f"It may not exist yet:\n{path}")


def show_job_result(parent: Optional[QWidget], result, success_message: str = "") -> None:
    """Report the outcome of a background job in a consistent way."""
    from ..jobs.states import JobState

    if result.state is JobState.SUCCEEDED:
        if success_message:
            show_info(parent, success_message)
        return
    if result.state is JobState.CANCELLED:
        show_info(parent, f"{result.title} was cancelled.", "Nothing was changed. You can start it again at any time.")
        return
    if result.error is not None:
        show_error(parent, result.error, title=result.error.title)
        return
    show_info(parent, f"{result.title} did not finish.", "Please try again.")


def status_message_for_job(result) -> str:
    """Short message for the status bar after a job finished."""
    from ..jobs.states import JobState

    if result.state is JobState.SUCCEEDED:
        from ..jobs.progress import format_duration

        return f"{result.title} finished in {format_duration(result.duration_seconds)}"
    if result.state is JobState.CANCELLED:
        return f"{result.title} cancelled"
    return f"{result.title} failed"
