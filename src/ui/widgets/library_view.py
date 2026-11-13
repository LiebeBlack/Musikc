"""Library browser: instant search over a header-only index."""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ...config import format_duration, get_logger
from ...core.library import Library, LibraryEntry, ScanReport
from ...core.playlist import Track
from ..theme import PALETTE

LOGGER = get_logger("ui.library")

ROW_HEIGHT = 44


class LibraryTableModel(QAbstractTableModel):
    COLUMNS = ("TITLE", "ARTIST", "ALBUM", "FORMAT", "TIME")

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._entries: List[LibraryEntry] = []

    def set_entries(self, entries: List[LibraryEntry]) -> None:
        self.beginResetModel()
        self._entries = entries
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        return 0 if parent.isValid() else len(self._entries)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # type: ignore[override]
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # type: ignore[override]
        if not index.isValid() or index.row() >= len(self._entries):
            return None
        entry = self._entries[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return {
                0: entry.display_title,
                1: entry.artist or "—",
                2: entry.album or "—",
                3: entry.resolution or entry.container,
                4: format_duration(entry.duration),
            }.get(column, "")
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(
                (Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if column == 4
                else (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            )
        if role == Qt.ItemDataRole.ForegroundRole:
            if column == 0:
                return QColor(PALETTE.text)
            if column == 1:
                return QColor(PALETTE.text_muted)
            return QColor(PALETTE.text_dim)
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{entry.path}\n{entry.resolution} · {entry.container} · {entry.codec}"
        if role == Qt.ItemDataRole.UserRole:
            return entry
        return None

    def entry_at(self, row: int) -> Optional[LibraryEntry]:
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None


class LibraryDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index) -> Any:  # type: ignore[override]
        from PySide6.QtCore import QSize

        return QSize(option.rect.width(), ROW_HEIGHT)

    def paint(self, painter, option, index) -> None:  # type: ignore[override]
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(PALETTE.accent_soft))
            painter.drawRoundedRect(option.rect.adjusted(2, 2, -2, -2), 8, 8)
        elif hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(PALETTE.surface_alt))
            painter.drawRoundedRect(option.rect.adjusted(2, 2, -2, -2), 8, 8)
        rect = option.rect.adjusted(8, 0, -8, 0)
        if index.column() == 0:
            entry = index.data(Qt.ItemDataRole.UserRole)
            font = QFont(option.font)
            font.setPointSizeF(9.6)
            font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(font)
            painter.setPen(QColor(PALETTE.text))
            painter.drawText(
                rect.adjusted(0, 5, 0, -14),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                str(index.data() or ""),
            )
            sub_font = QFont(option.font)
            sub_font.setPointSizeF(8.2)
            painter.setFont(sub_font)
            painter.setPen(QColor(PALETTE.text_dim))
            subtitle = entry.filename if entry is not None else ""
            painter.drawText(
                rect.adjusted(0, 20, 0, 0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                subtitle,
            )
        else:
            painter.setFont(option.font)
            painter.setPen(index.data(Qt.ItemDataRole.ForegroundRole) or QColor(PALETTE.text_dim))
            alignment = (
                Qt.AlignmentFlag.AlignRight if index.column() == 4 else Qt.AlignmentFlag.AlignLeft
            ) | Qt.AlignmentFlag.AlignVCenter
            painter.drawText(rect, alignment, str(index.data() or ""))
        painter.restore()


class LibraryScanThread(QThread):
    progress = Signal(int, int)
    finished_scan = Signal(object)
    failed = Signal(str)

    def __init__(self, library: Library, folders: List[str], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._library = library
        self._folders = folders
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:  # type: ignore[override]
        try:
            report = self._library.scan(
                folders=self._folders,
                progress=lambda done, total, path: self.progress.emit(done, total),
                should_cancel=lambda: self._cancelled,
            )
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.exception("library scan crashed")
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished_scan.emit(report)


class LibraryView(QWidget):
    playRequested = Signal(str)
    queueRequested = Signal(list)
    foldersChanged = Signal(list)
    statusMessage = Signal(str)

    def __init__(self, library: Library, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._library = library
        self._scan_thread: Optional[LibraryScanThread] = None
        self._last_scan_report: Optional[ScanReport] = None
        self._last_scan_error = ""
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(140)
        self._filter_timer.timeout.connect(self.apply_filter)
        self._empty_overlay_message = ""
        self._empty_overlay_kind = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search title, artist, album, format…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _text: self._filter_timer.start())
        search_row.addWidget(self.search, 1)
        self.folder_button = QPushButton("Add folder")
        self.folder_button.clicked.connect(self._choose_folder)
        search_row.addWidget(self.folder_button)
        self.scan_button = QPushButton("Rescan")
        self.scan_button.clicked.connect(lambda: self.start_scan())
        search_row.addWidget(self.scan_button)
        layout.addLayout(search_row)

        self.table = QTableView()
        self.model = LibraryTableModel(self.table)
        self.table.setModel(self.model)
        self.table.setItemDelegate(LibraryDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.table.setMouseTracking(True)
        self.table.doubleClicked.connect(self._on_double_click)
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in ((1, 150), (2, 150), (3, 120), (4, 62)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            header.resizeSection(column, width)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.table, 1)

        self.empty_label = QLabel("No tracks indexed yet.\nAdd a music folder to build the library.")
        self.empty_label.setObjectName("Hint")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setMinimumHeight(80)
        layout.addWidget(self.empty_label)
        self._empty_overlay_message = "No tracks indexed yet.\nAdd a music folder to build the library."
        self._empty_overlay_kind = "empty"

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        self.summary = QLabel("")
        self.summary.setObjectName("Hint")
        action_row.addWidget(self.summary, 1)
        self.queue_button = QPushButton("Add to queue")
        self.queue_button.setEnabled(False)
        self.queue_button.clicked.connect(self._queue_selection)
        action_row.addWidget(self.queue_button)
        self.play_button = QPushButton("Play now")
        self.play_button.setObjectName("Primary")
        self.play_button.setEnabled(False)
        self.play_button.clicked.connect(self._play_selection)
        action_row.addWidget(self.play_button)
        layout.addLayout(action_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        self.progress.setFixedHeight(6)
        layout.addWidget(self.progress)

        self.refresh_summary()

    # -------------------------------------------------------------------- data
    @property
    def library(self) -> Library:
        return self._library

    def set_library(self, library: Library) -> None:
        self._library = library
        self.apply_filter()

    def apply_filter(self) -> None:
        entries = self._library.search(self.search.text())
        self.model.set_entries(entries)
        self._on_selection_changed()
        self.table.setVisible(bool(entries))
        self.empty_label.setVisible(not entries)
        self._update_empty_state(entries)
        self.refresh_summary()

    def _update_empty_state(self, entries: List[LibraryEntry]) -> None:
        if entries:
            self.empty_label.clear()
            self._empty_overlay_message = ""
            self._empty_overlay_kind = "hidden"
            self.empty_label.hide()
            return
        if self._last_scan_error:
            message = (
                "Library scan failed.\nCheck the folder permissions and try Rescan.\n"
                + self._last_scan_error[:180]
            )
            kind = "error"
            object_name = "Error"
        elif self.search.text().strip():
            message = "No matching tracks.\nClear the search to see the full library."
            kind = "search"
            object_name = "Hint"
        elif not self._library.folders:
            message = "No music folders yet.\nSelect Add folder to choose your library."
            kind = "empty"
            object_name = "Hint"
        elif self._last_scan_report is not None and self._last_scan_report.failed:
            message = (
                f"No readable tracks found.\n{self._last_scan_report.failed} file(s) could not be decoded.\n"
                "Check the supported formats and permissions."
            )
            kind = "failed_files"
            object_name = "Warning"
        else:
            message = "No tracks indexed in these folders.\nSelect Rescan to check again."
            kind = "empty_results"
            object_name = "Hint"
        self._empty_overlay_message = message
        self._empty_overlay_kind = kind
        self.empty_label.setText(message)
        self.empty_label.setObjectName(object_name)
        self.empty_label.show()
        style = self.empty_label.style()
        if style is not None:
            style.unpolish(self.empty_label)
            style.polish(self.empty_label)
        self.empty_label.update()

    def refresh_summary(self) -> None:
        stats = self._library.stats()
        tracks = stats["tracks"]
        formats = stats.get("formats") or {}
        suffix = " · ".join(f"{name} {count}" for name, count in sorted(formats.items()))
        self.summary.setText(f"{tracks} tracks indexed" + (f" · {suffix}" if suffix else ""))

    # -------------------------------------------------------------------- scan
    def start_scan(self, folders: Optional[List[str]] = None) -> None:
        if self._scan_thread is not None and self._scan_thread.isRunning():
            return
        targets = folders or self._library.folders
        if not targets:
            self._choose_folder()
            return
        self._last_scan_error = ""
        self._empty_overlay_kind = "scanning"
        self.empty_label.setText(f"Scanning {len(targets)} folder(s)…\nThe interface remains responsive.")
        self.empty_label.setObjectName("Hint")
        self.table.hide()
        self.empty_label.show()
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.scan_button.setEnabled(False)
        self.scan_button.setText("Scanning…")
        self._scan_thread = LibraryScanThread(self._library, targets, self)
        self._scan_thread.progress.connect(self._on_scan_progress)
        self._scan_thread.finished_scan.connect(self._on_scan_finished)
        self._scan_thread.failed.connect(self._on_scan_failed)
        self._scan_thread.start()

    def _on_scan_progress(self, done: int, total: int) -> None:
        if total <= 0:
            return
        self.progress.setRange(0, total)
        self.progress.setValue(done)

    def _on_scan_finished(self, report: ScanReport) -> None:
        self._last_scan_report = report
        self._last_scan_error = ""
        self.progress.setVisible(False)
        self.scan_button.setEnabled(True)
        self.scan_button.setText("Rescan")
        self.apply_filter()
        self.statusMessage.emit(report.summary)

    def _on_scan_failed(self, message: str) -> None:
        self._last_scan_error = str(message)
        self.progress.setVisible(False)
        self.scan_button.setEnabled(True)
        self.scan_button.setText("Rescan")
        self.apply_filter()
        self.statusMessage.emit(f"Library scan failed: {message}")

    def shutdown(self, timeout_ms: int = 3000) -> None:
        """Cancel a running scan and wait for the worker before the window dies."""

        thread = self._scan_thread
        if thread is not None and thread.isRunning():
            thread.cancel()
            thread.wait(timeout_ms)

    def _choose_folder(self) -> None:
        start = self._library.folders[-1] if self._library.folders else str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Add music folder", start)
        if not folder:
            return
        folders = list(self._library.folders)
        if folder not in folders:
            folders.append(folder)
            self._library.set_folders(folders)
            self.foldersChanged.emit(folders)
        self.start_scan(folders)

    # ----------------------------------------------------------------- actions
    def _on_selection_changed(self, *_args) -> None:
        has_selection = bool(self.table.selectionModel().selectedRows())
        self.play_button.setEnabled(has_selection)
        self.queue_button.setEnabled(has_selection)

    def selected_entries(self) -> List[LibraryEntry]:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        return [entry for entry in (self.model.entry_at(row) for row in rows) if entry is not None]

    def _play_selection(self) -> None:
        entries = self.selected_entries()
        if not entries:
            return
        self.playRequested.emit(entries[0].path)

    def _queue_selection(self) -> None:
        entries = self.selected_entries()
        if not entries:
            return
        self.queueRequested.emit([entry.path for entry in entries])

    def _on_double_click(self, index: QModelIndex) -> None:
        entry = self.model.entry_at(index.row())
        if entry is not None:
            self.playRequested.emit(entry.path)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        width = self.width()
        self.table.setColumnHidden(2, width < 660)
        self.table.setColumnHidden(1, width < 520)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() == Qt.Key.Key_Return:
            self._play_selection()
            event.accept()
            return
        super().keyPressEvent(event)

    @staticmethod
    def track_from_entry(entry: LibraryEntry) -> Track:
        return Track(
            path=entry.path,
            title=entry.title,
            artist=entry.artist,
            album=entry.album,
            duration=entry.duration,
            resolution=entry.resolution,
            container=entry.container,
            codec=entry.codec,
            size_bytes=entry.size_bytes,
            is_dsd=entry.is_dsd,
            sample_rate=entry.sample_rate,
            bit_depth=entry.bit_depth,
            channels=entry.channels,
        )
