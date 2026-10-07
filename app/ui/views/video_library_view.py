"""The Video Library page: every clip the studio has, with what is really in it.

Three kinds of clip live here and the page keeps them apart (section 30):

* **Generated** - what an AI backend produced, with its prompt, seed and model;
* **Rendered** - a finished render the user chose to keep;
* **Imported** - a file they brought in from elsewhere.

Nothing on this page invents a fact.  The length, size, frame rate and codec in
the table were measured with FFprobe and cached against the file; a clip whose
file has gone says so instead of quietly disappearing; and where a real AI
backend is missing the page says that plainly rather than offering a button that
cannot work.

Listing never opens a clip: the table is served from the index, pictures are made
one at a time in a background job, and every long operation (scanning a folder,
extracting frames, re-measuring, removing) goes through the application's job
manager so the window stays responsive (section 41).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from app.ai.video_library import (ENTRY_READY, LIBRARY_SORTS, LIBRARY_SOURCES,
                                  SOURCE_LABELS, LibraryQuery, VideoLibrary)
from app.ai.video_library_jobs import (library_import_spec, library_recheck_spec,
                                       library_remove_spec, library_scan_spec,
                                       library_thumbnails_spec)
from app.ai.integration import send_to_project, send_to_scene, send_to_timeline

from ..theme import mark_primary
from ..widgets.common import ButtonRow, Card, HintLabel, Page, Row


class VideoLibraryPage(Page):
    """The clip library: measured facts, real provenance, no invented entries."""

    project_changed = Signal()

    def __init__(self, context: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Video library",
            "Every clip this studio has made or imported: what it is, what is "
            "really in it, and where it came from.",
            parent,
        )
        self.context = context
        self.service: Optional[VideoLibrary] = None
        self._entries: list = []
        self._scan_job: Optional[str] = None
        self._thumb_job: Optional[str] = None
        self._selected: Optional[str] = None

        self._build_toolbar()
        self._build_splitter()
        self._build_footer()

        manager = getattr(self.context, "jobs", None)
        if manager is not None:
            try:
                manager.job_finished.connect(self._on_job_finished)
            except Exception:  # noqa: BLE001 - the page still works without it
                pass
        self.refresh()

    # -- services ----------------------------------------------------------

    def library(self) -> Optional[VideoLibrary]:
        """One library per page, pointed at the application's own folder."""
        if self.service is None:
            from app.ai.video_library_jobs import video_library_service

            try:
                self.service = video_library_service(
                    self.context, paths=getattr(self.context, "paths", None),
                    tools=self._tools())
            except Exception as exc:  # noqa: BLE001 - reported, not fatal
                self.status_label.setText(
                    f"The video library could not be opened: {exc}")
                return None
        return self.service

    def _tools(self) -> Any:
        try:
            from app.tools.ffmpeg import FFmpegTools, discover_ffmpeg

            return FFmpegTools(discover_ffmpeg())
        except Exception:  # noqa: BLE001 - no FFmpeg is a normal machine state
            return None

    @property
    def ai_service(self) -> Any:
        """The AI Studio's service, when the window has one (for provenance)."""
        page = getattr(self.context, "ai_studio_page", None)
        service = getattr(page, "service", None) if page is not None else None
        if service is None:
            window = self.window()
            page = getattr(window, "ai_studio_page", None)
            service = getattr(page, "service", None) if page is not None else None
        return service

    @property
    def controller(self) -> Any:
        return getattr(self.context, "projects", None)

    # -- construction ------------------------------------------------------

    def _build_toolbar(self) -> None:
        card = Card("Find a clip")
        self.status_label = QLabel("Nothing has been scanned yet.")
        self.status_label.setObjectName("Hint")
        self.status_label.setWordWrap(True)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search names, prompts, models, tags")
        self.search_edit.textChanged.connect(self.refresh_list)

        self.source_combo = QComboBox()
        self.source_combo.addItem("Everything", "")
        for source in LIBRARY_SOURCES:
            self.source_combo.addItem(SOURCE_LABELS[source], source)
        self.source_combo.currentIndexChanged.connect(self.refresh_list)

        self.sort_combo = QComboBox()
        for key, label in LIBRARY_SORTS.items():
            self.sort_combo.addItem(label, key)
        self.sort_combo.currentIndexChanged.connect(self.refresh_list)

        self.collection_combo = QComboBox()
        self.collection_combo.addItem("All collections", "")
        self.collection_combo.currentIndexChanged.connect(self.refresh_list)

        self.favourites_only = QCheckBox("Favourites only")
        self.favourites_only.toggled.connect(self.refresh_list)
        self.missing_only = QCheckBox("Missing files only")
        self.missing_only.toggled.connect(self.refresh_list)

        self.scan_button = QPushButton("Scan the folder")
        self.scan_button.clicked.connect(self.scan_folder)
        self.import_button = QPushButton("Add a file")
        self.import_button.clicked.connect(self.import_file)
        self.thumbnails_button = QPushButton("Make pictures")
        self.thumbnails_button.clicked.connect(self.make_thumbnails)

        card.add(Row("Search", self.search_edit))
        row = QHBoxLayout()
        row.addWidget(self.source_combo)
        row.addWidget(self.sort_combo)
        row.addWidget(self.collection_combo)
        row.addWidget(self.favourites_only)
        row.addWidget(self.missing_only)
        holder = QWidget()
        holder.setLayout(row)
        card.add(Row("Filter and sort", holder))
        buttons = ButtonRow([self.scan_button, self.import_button,
                             self.thumbnails_button])
        card.add(buttons)
        card.add(self.status_label)
        self.add(card)

    def _build_splitter(self) -> None:
        splitter = QSplitter(Qt.Horizontal)
        self._splitter = splitter

        table_card = Card("Clips")
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Name", "Source", "Length", "Size", "Frame rate", "Codec", "State"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        self.table.setSortingEnabled(False)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.doubleClicked.connect(lambda *_: self.preview_selected())
        table_card.add(self.table)
        table_card.add(HintLabel(
            "The length, size, frame rate and codec were measured from each "
            "file. A clip whose file has gone is marked MISSING, never removed "
            "behind your back."))

        detail = Card("The clip you chose")
        self.thumbnail = QLabel("No clip chosen.")
        self.thumbnail.setMinimumSize(240, 135)
        self.thumbnail.setAlignment(Qt.AlignCenter)
        self.thumbnail.setObjectName("Preview")
        self.detail_label = QLabel("Choose a clip to see what it is.")
        self.detail_label.setWordWrap(True)
        self.detail_label.setObjectName("Hint")
        self.provenance_label = QLabel("")
        self.provenance_label.setWordWrap(True)
        self.provenance_label.setObjectName("Hint")
        self.tags_list = QListWidget()
        self.tags_list.setMaximumHeight(90)

        self.tag_edit = QLineEdit()
        self.tag_edit.setPlaceholderText("Add a tag")
        self.tag_button = QPushButton("Add tag")
        self.tag_button.clicked.connect(self.add_tag)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Library name")
        self.rename_button = QPushButton("Rename")
        self.rename_button.clicked.connect(self.rename_selected)
        self.collection_edit = QLineEdit()
        self.collection_edit.setPlaceholderText("Collection")
        self.collection_button = QPushButton("Set collection")
        self.collection_button.clicked.connect(self.set_collection)
        self.favourite_button = QPushButton("Favourite")
        self.favourite_button.clicked.connect(self.toggle_favourite)
        self.recheck_button = QPushButton("Measure again")
        self.recheck_button.clicked.connect(self.recheck_selected)
        self.preview_button = QPushButton("Preview")
        self.preview_button.clicked.connect(self.preview_selected)

        detail.add(self.thumbnail)
        detail.add(self.detail_label)
        detail.add(self.provenance_label)
        detail.add(self.tags_list)
        detail.add(Row("Tag", self.tag_edit))
        detail.add(Row("Name", self.name_edit))
        detail.add(Row("Collection", self.collection_edit))
        detail.add(ButtonRow([self.tag_button, self.rename_button,
                              self.collection_button, self.favourite_button]))
        detail.add(ButtonRow([self.recheck_button, self.preview_button]))

        splitter.addWidget(table_card)
        splitter.addWidget(detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self.add(splitter)

        self.preview_label = QLabel("")
        self.preview_label.setObjectName("Hint")
        self.preview_label.setWordWrap(True)
        detail.add(self.preview_label)

    def _build_footer(self) -> None:
        card = Card("Use this clip")
        self.send_project_button = QPushButton("Send to project")
        self.send_project_button.clicked.connect(
            lambda: self.send_selected("project"))
        self.send_scene_button = QPushButton("Send to scene")
        self.send_scene_button.clicked.connect(lambda: self.send_selected("scene"))
        self.send_timeline_button = QPushButton("Send to timeline")
        self.send_timeline_button.clicked.connect(
            lambda: self.send_selected("timeline"))
        self.remove_button = QPushButton("Remove from library")
        self.remove_button.clicked.connect(lambda: self.remove_selected(False))
        self.delete_button = QPushButton("Delete the file too")
        self.delete_button.clicked.connect(lambda: self.remove_selected(True))
        mark_primary(self.send_timeline_button)

        self.send_note = QLabel(
            "Generated clips are ordinary assets: send one to the project, place "
            "it in a scene, or drop it on the timeline - exactly like any other "
            "clip.")
        self.send_note.setWordWrap(True)
        self.send_note.setObjectName("Hint")

        card.add(ButtonRow([self.send_project_button, self.send_scene_button,
                            self.send_timeline_button]))
        card.add(ButtonRow([self.remove_button, self.delete_button]))
        card.add(self.send_note)
        self.add(card)

    # -- listing -----------------------------------------------------------

    def refresh(self) -> None:
        library = self.library()
        if library is None:
            return
        library.refresh_exists()
        self._fill_collections()
        self.refresh_list()
        self._update_enabled()

    def refresh_list(self) -> None:
        library = self.library()
        if library is None:
            return
        query = LibraryQuery(
            text=self.search_edit.text(),
            source=str(self.source_combo.currentData() or ""),
            collection=str(self.collection_combo.currentData() or ""),
            favourites_only=self.favourites_only.isChecked(),
            missing_only=self.missing_only.isChecked(),
            sort=str(self.sort_combo.currentData() or "recent"), limit=200)
        page = library.query(query)
        self._entries = list(page.entries)
        self.table.setRowCount(0)
        for entry in page.entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            cells = [entry.filename, entry.source_label(),
                     entry.duration_label() or "-", entry.size_label() or "-",
                     f"{entry.fps:g}" if entry.fps else "-",
                     entry.codec.upper() if entry.codec else "-",
                     entry.status if entry.status == ENTRY_READY else entry.status]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if column == 0:
                    item.setData(Qt.UserRole, entry.id)
                self.table.setItem(row, column, item)
        if page.total:
            self.status_label.setText(
                f"{len(page.entries)} of {page.total} clip(s) shown. "
                f"{library.describe()}")
        else:
            self.status_label.setText(
                library.describe() if library.counts() else
                "The library is empty. Scan the folder, or add a file.")
        self._sync_selection()

    def _fill_collections(self) -> None:
        library = self.library()
        if library is None:
            return
        current = str(self.collection_combo.currentData() or "")
        self.collection_combo.blockSignals(True)
        self.collection_combo.clear()
        self.collection_combo.addItem("All collections", "")
        for name in library.collections():
            self.collection_combo.addItem(name, name)
        index = self.collection_combo.findData(current)
        self.collection_combo.setCurrentIndex(max(0, index))
        self.collection_combo.blockSignals(False)

    def _sync_selection(self) -> None:
        if not self._entries:
            self._selected = None
            self._show_entry(None)
            return
        wanted = self._selected
        row = next((index for index, entry in enumerate(self._entries)
                    if entry.id == wanted), 0)
        self.table.selectRow(row)

    def _on_selection_changed(self) -> None:
        entry = self.selected_entry()
        self._selected = entry.id if entry is not None else None
        self._show_entry(entry)

    def selected_entry(self) -> Any:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        if not rows or rows[0] >= len(self._entries):
            return None
        return self._entries[rows[0]]

    def _show_entry(self, entry: Any) -> None:
        if entry is None:
            self.thumbnail.setText("No clip chosen.")
            self.thumbnail.setPixmap(QPixmap())
            self.detail_label.setText("Choose a clip to see what it is.")
            self.provenance_label.setText("")
            self.tags_list.clear()
            self._update_enabled()
            return
        self.name_edit.setText(entry.name)
        self.collection_edit.setText(entry.collection)
        self._show_thumbnail(entry)
        self.detail_label.setText(
            f"{entry.filename}\n{entry.measurements() or 'Not measured yet.'}\n"
            f"{entry.status}" + (f"\n{entry.notes}" if entry.notes else ""))
        lines = [entry.provenance()]
        if entry.prompt:
            lines.append(f"Prompt: {entry.prompt}")
        if entry.seed:
            lines.append(f"Seed: {entry.seed}")
        if entry.project:
            lines.append(f"Project: {Path(entry.project).name}")
        if entry.measured_with:
            lines.append(f"Measured with: {entry.measured_with}")
        if entry.status != ENTRY_READY:
            lines.append("The file is not there any more. It is still listed so "
                         "you can find it again or remove it deliberately.")
        self.provenance_label.setText("\n".join(lines))
        self.tags_list.clear()
        for tag in entry.tags or []:
            self.tags_list.addItem(str(tag))
        self._update_enabled()

    def _show_thumbnail(self, entry: Any) -> None:
        library = self.library()
        path = Path(str(entry.thumbnail)) if entry.thumbnail else None
        if path is not None and path.is_file() and library is not None:
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                self.thumbnail.setPixmap(pixmap.scaled(
                    320, 180, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                return
        self.thumbnail.setPixmap(QPixmap())
        self.thumbnail.setText(
            "No picture yet.\nPress Make pictures to take one from each clip."
            if entry.status == ENTRY_READY else "The file is not there.")

    def _update_enabled(self) -> None:
        entry = self.selected_entry()
        has = entry is not None
        for widget in (self.tag_button, self.rename_button, self.collection_button,
                       self.favourite_button, self.recheck_button,
                       self.preview_button, self.remove_button, self.delete_button,
                       self.send_project_button, self.send_scene_button,
                       self.send_timeline_button):
            widget.setEnabled(has)
        if has:
            self.favourite_button.setText(
                "Unfavourite" if entry.favourite else "Favourite")
        self.delete_button.setToolTip(
            "Removes the entry and deletes the file from disk. Only press this "
            "if you really want the clip gone.")
        self.send_scene_button.setToolTip(
            "Adds the clip as a scene that plays it, at the end of the timeline.")

    # -- jobs --------------------------------------------------------------

    def _submit(self, spec: Any) -> Optional[str]:
        manager = getattr(self.context, "jobs", None)
        if manager is None:
            return None
        paths = getattr(self.context, "paths", None)
        if paths is not None:
            spec.paths = paths
        job = manager.submit(spec)
        return getattr(job, "id", None) if job is not None else None

    def _payload(self, **extra: Any) -> dict:
        return {"library": self.library(), "tools": self._tools(), **extra}

    def scan_folder(self) -> None:
        if self.library() is None:
            return
        self.status_label.setText("Scanning the clip folder…")
        self._scan_job = self._submit(library_scan_spec(self._payload()))

    def import_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Add a clip to the library", "",
            "Videos (*.mp4 *.mov *.mkv *.webm *.avi *.m4v)")
        if path:
            self.import_clip(path)

    def import_clip(self, path: str, *, source: str = "",
                    metadata: Optional[dict] = None) -> Optional[str]:
        """Add one file.  Used by the button and by the render page."""
        return self._submit(library_import_spec(
            self._payload(path=str(path), source=source, metadata=metadata or {})))

    def make_thumbnails(self) -> None:
        if self.library() is None:
            return
        self.status_label.setText("Making clip pictures…")
        self._thumb_job = self._submit(library_thumbnails_spec(self._payload()))

    def recheck_selected(self) -> None:
        entry = self.selected_entry()
        if entry is None:
            return
        self.status_label.setText(f"Measuring {entry.filename} again…")
        self._submit(library_recheck_spec(self._payload(id=entry.id)))

    def remove_selected(self, delete_file: bool) -> None:
        entry = self.selected_entry()
        if entry is None:
            return
        self._submit(library_remove_spec(
            self._payload(ids=[entry.id], delete_file=bool(delete_file))))
        self.status_label.setText(
            "Removing the clip from the library"
            + (" and deleting the file…" if delete_file else "…"))

    def _on_job_finished(self, result: Any) -> None:
        """Say what the job did, after the list has been refreshed.

        The order matters: refreshing an empty library writes its own hint, so a
        failure message written first would be wiped by the refresh and the user
        would never learn that the import failed.
        """
        key = str(getattr(result, "key", ""))
        if not key.startswith("video.library"):
            return
        message = ""
        value = getattr(result, "value", None)
        if isinstance(value, dict):
            message = str(value.get("message", "") or "")
            if value.get("ok") is False and value.get("what_to_do"):
                # A body that failed on purpose: what happened, and what to do.
                message = f"{message} {value['what_to_do']}".strip()
        elif getattr(result, "cancelled", False):
            message = ("The clip work was stopped. Nothing that was already "
                       "saved was changed.")
        elif getattr(result, "error", None) is not None:
            # A crashed job must never leave the user reading stale text.
            error = result.error
            what = str(getattr(error, "what_happened", "") or
                       getattr(error, "title", "") or "The job did not finish.")
            why = str(getattr(error, "why", "") or "")
            actions = list(getattr(error, "actions", ()) or ())
            message = what
            if why:
                message += f" Why: {why}"
            if actions:
                message += f" What to do: {actions[0]}"
        self._scan_job = self._thumb_job = None
        self.refresh()
        if message:
            self.status_label.setText(message)

    # -- library metadata --------------------------------------------------

    def add_tag(self) -> None:
        entry = self.selected_entry()
        library = self.library()
        tag = self.tag_edit.text().strip()
        if entry is None or library is None or not tag:
            return
        library.add_tag(entry.id, tag)
        self.tag_edit.clear()
        self.refresh()

    def rename_selected(self) -> None:
        entry = self.selected_entry()
        library = self.library()
        name = self.name_edit.text().strip()
        if entry is None or library is None or not name:
            self.status_label.setText("Give the clip a name first.")
            return
        library.rename(entry.id, name)
        self.status_label.setText(
            f"'{name}' is what this clip is called in the library. The file on "
            f"disk keeps its own name.")
        self.refresh()

    def set_collection(self) -> None:
        entry = self.selected_entry()
        library = self.library()
        if entry is None or library is None:
            return
        library.set_collection(entry.id, self.collection_edit.text())
        self.refresh()

    def toggle_favourite(self) -> None:
        entry = self.selected_entry()
        library = self.library()
        if entry is None or library is None:
            return
        library.set_favourite(entry.id, not entry.favourite)
        self.refresh()

    def preview_selected(self) -> None:
        """Play a clip when this machine can, and say so when it cannot."""
        entry = self.selected_entry()
        if entry is None:
            return
        from ..preview import preview_video

        result = preview_video(Path(entry.path), parent=self)
        # The message and what to do instead are shown together: a preview that
        # cannot play must still leave the user knowing their next step.
        self.preview_label.setText(result.describe())
        if result.opened:
            return
        self.status_label.setText(result.message)

    # -- sending -----------------------------------------------------------

    def send_selected(self, placement: str) -> None:
        entry = self.selected_entry()
        controller = self.controller
        if entry is None:
            return
        service = getattr(controller, "service", None)
        if service is None or not getattr(service, "is_open", False):
            self.send_note.setText(
                "Open or create a project first: a clip needs somewhere to go.")
            return
        metadata = entry.to_metadata()
        duration = float(entry.duration or 0.0)
        fps = float(entry.fps or 0.0)
        if placement == "project":
            report = send_to_project(service, entry.path, name=entry.name,
                                     metadata=metadata)
        elif placement == "scene":
            report = send_to_scene(service, entry.path, name=entry.name,
                                   metadata=metadata, duration=duration, fps=fps)
        else:
            report = send_to_timeline(service, entry.path, name=entry.name,
                                      metadata=metadata, duration=duration,
                                      fps=fps)
        self.send_note.setText(report.describe())
        if report.ok:
            self.project_changed.emit()

    # -- housekeeping ------------------------------------------------------

    def status_text(self) -> str:
        return self.status_label.text()

    def stop(self) -> None:
        """Nothing to stop: every long operation here runs as a job."""
        return None
