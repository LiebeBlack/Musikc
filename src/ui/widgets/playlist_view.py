"""Queue view: reorderable, keyboard friendly, with a hand painted row delegate."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, List, Optional

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QMimeData,
    QPoint,
    QRect,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ...config import format_duration, get_logger
from ...core.playlist import Playlist, Track
from ..theme import PALETTE

LOGGER = get_logger("ui.playlist")

MIME_TYPE = "application/x-truehq-track-row"
ROW_HEIGHT = 42


class PlaylistTableModel(QAbstractTableModel):
    COLUMNS = ("#", "TITLE", "ALBUM", "FORMAT", "TIME")

    def __init__(self, playlist: Optional[Playlist] = None) -> None:
        super().__init__()
        self._playlist = playlist or Playlist()
        self._current = -1

    # ------------------------------------------------------------------ qt api
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        return 0 if parent.isValid() else len(self._playlist)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # type: ignore[override]
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # type: ignore[override]
        if not index.isValid():
            return None
        try:
            track = self._playlist[index.row()]
        except IndexError:
            return None
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if column == 0:
                return str(index.row() + 1)
            if column == 1:
                return track.display_title
            if column == 2:
                return track.album or "—"
            if column == 3:
                return track.resolution or track.container
            if column == 4:
                return format_duration(track.duration)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if column in (0, 4):
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ForegroundRole:
            if index.row() == self._current:
                return QColor(PALETTE.accent)
            if column in (1,):
                return QColor(PALETTE.text)
            return QColor(PALETTE.text_dim)
        if role == Qt.ItemDataRole.FontRole and column == 0:
            font = QFont()
            font.setPointSizeF(8.5)
            return font
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{track.path}\n{track.resolution} · {track.container} · {track.codec}"
        if role == Qt.ItemDataRole.UserRole:
            return track
        if role == Qt.ItemDataRole.UserRole + 1:
            return index.row() == self._current
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:  # type: ignore[override]
        base = super().flags(index)
        if index.isValid():
            return base | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled
        return base | Qt.ItemFlag.ItemIsDropEnabled

    # ---------------------------------------------------------------- drag/drop
    def supportedDropActions(self) -> Qt.DropAction:  # type: ignore[override]
        return Qt.DropAction.MoveAction

    def mimeTypes(self) -> List[str]:  # type: ignore[override]
        return [MIME_TYPE]

    def mimeData(self, indexes) -> QMimeData:  # type: ignore[override]
        payload = QMimeData()
        rows = sorted({index.row() for index in indexes if index.isValid()})
        payload.setData(MIME_TYPE, ",".join(str(row) for row in rows).encode("utf-8"))
        return payload

    def dropMimeData(self, data: QMimeData, action: Qt.DropAction, row: int, column: int, parent: QModelIndex) -> bool:  # type: ignore[override]
        if not data.hasFormat(MIME_TYPE):
            return False
        try:
            source_rows = [int(value) for value in bytes(data.data(MIME_TYPE)).decode("utf-8").split(",") if value != ""]
        except ValueError:
            return False
        if not source_rows:
            return False
        target = row if row >= 0 else self.rowCount()
        model = self
        source = source_rows[0]
        if source < target:
            target -= 1
        if source == target:
            return False
        model.beginResetModel()
        self._playlist.move(source, target)
        model.endResetModel()
        return True

    # ------------------------------------------------------------------- model
    def set_playlist(self, playlist: Playlist) -> None:
        self.beginResetModel()
        self._playlist = playlist
        self._current = playlist.current_index
        self.endResetModel()

    def set_current(self, index: int) -> None:
        previous = self._current
        self._current = index
        if previous >= 0 and previous < self.rowCount():
            self.dataChanged.emit(self.index(previous, 0), self.index(previous, self.columnCount() - 1))
        if 0 <= index < self.rowCount():
            self.dataChanged.emit(self.index(index, 0), self.index(index, self.columnCount() - 1))

    def refresh(self) -> None:
        self.beginResetModel()
        self._current = self._playlist.current_index
        self.endResetModel()

    def track_at(self, row: int) -> Optional[Track]:
        try:
            return self._playlist[row]
        except IndexError:
            return None


class TrackDelegate(QStyledItemDelegate):
    """Draws a two line title cell and a subtle 'now playing' marker."""

    def sizeHint(self, option, index) -> QSize:  # type: ignore[override]
        return QSize(option.rect.width(), ROW_HEIGHT)

    def paint(self, painter: QPainter, option, index) -> None:  # type: ignore[override]
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = option.rect.adjusted(6, 0, -6, 0)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        is_current = bool(index.data(Qt.ItemDataRole.UserRole + 1))
        if selected:
            background = QColor(PALETTE.accent_soft)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(background)
            painter.drawRoundedRect(option.rect.adjusted(2, 2, -2, -2), 8, 8)
        elif hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(PALETTE.surface_alt))
            painter.drawRoundedRect(option.rect.adjusted(2, 2, -2, -2), 8, 8)
        if index.column() == 1:
            track = index.data(Qt.ItemDataRole.UserRole)
            title_font = QFont(option.font)
            title_font.setPointSizeF(10.0)
            title_font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(title_font)
            painter.setPen(QColor(PALETTE.accent) if is_current else QColor(PALETTE.text))
            metrics = QFontMetrics(title_font)
            title_rect = QRect(rect.left() + 4, rect.top() + 6, rect.width() - 8, 18)
            painter.drawText(
                title_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                metrics.elidedText(str(index.data() or ""), Qt.TextElideMode.ElideRight, title_rect.width()),
            )
            sub_font = QFont(option.font)
            sub_font.setPointSizeF(8.5)
            painter.setFont(sub_font)
            painter.setPen(QColor(PALETTE.text_dim))
            sub_text = f"{track.display_artist} · {track.container}" if track is not None else ""
            painter.drawText(
                QRect(rect.left() + 4, rect.top() + 24, rect.width() - 8, 16),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                QFontMetrics(sub_font).elidedText(sub_text, Qt.TextElideMode.ElideRight, rect.width() - 8),
            )
            if is_current:
                marker = QRect(rect.left() - 2, rect.center().y() - 7, 3, 14)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(PALETTE.accent))
                painter.drawRoundedRect(marker, 1.5, 1.5)
        elif index.column() == 0:
            font = QFont(option.font)
            font.setPointSizeF(8.5)
            painter.setFont(font)
            painter.setPen(QColor(PALETTE.accent) if is_current else QColor(PALETTE.text_dim))
            painter.drawText(rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(index.data() or ""))
        elif index.column() == 3:
            text = str(index.data() or "")
            font = QFont(option.font)
            font.setPointSizeF(8.3)
            painter.setFont(font)
            metrics = QFontMetrics(font)
            width = metrics.horizontalAdvance(text) + 14
            chip_rect = QRect(rect.left(), rect.center().y() - 9, min(width, rect.width()), 18)
            painter.setPen(QPen(QColor(PALETTE.stroke_strong), 1.0))
            painter.setBrush(QColor(PALETTE.surface_alt))
            painter.drawRoundedRect(chip_rect, 6, 6)
            painter.setPen(QColor(PALETTE.text_muted))
            painter.drawText(chip_rect, Qt.AlignmentFlag.AlignCenter, metrics.elidedText(text, Qt.TextElideMode.ElideRight, chip_rect.width() - 8))
        else:
            painter.setFont(option.font)
            painter.setPen(QColor(PALETTE.text_dim) if index.column() != 4 else QColor(PALETTE.text_muted))
            painter.drawText(
                rect,
                (Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if index.column() == 4
                else (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                str(index.data() or ""),
            )
        painter.restore()


class PlaylistView(QWidget):
    """Table view plus toolbar for the playback queue."""

    playRequested = Signal(int)
    removeRequested = Signal(int)
    moveRequested = Signal(int, int)
    clearRequested = Signal()
    addFilesRequested = Signal()

    def __init__(
        self,
        playlist: Optional[Playlist] = None,
        parent: Optional[QWidget] = None,
        toolbar: bool = True,
    ) -> None:
        super().__init__(parent)
        self._playlist = playlist or Playlist()
        self.model = PlaylistTableModel(self._playlist)
        self.count_label: Optional[QLabel] = None
        self.add_button: Optional[QPushButton] = None
        self.clear_button: Optional[QPushButton] = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        if toolbar:
            toolbar_row = QHBoxLayout()
            toolbar_row.setContentsMargins(0, 0, 0, 0)
            toolbar_row.setSpacing(8)
            self.count_label = QLabel("0 tracks")
            self.count_label.setObjectName("Hint")
            toolbar_row.addWidget(self.count_label)
            toolbar_row.addStretch(1)
            self.add_button = QPushButton("Add files")
            self.add_button.clicked.connect(self.addFilesRequested.emit)
            toolbar_row.addWidget(self.add_button)
            self.clear_button = QPushButton("Clear")
            self.clear_button.clicked.connect(self.clearRequested.emit)
            toolbar_row.addWidget(self.clear_button)
            layout.addLayout(toolbar_row)

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setItemDelegate(TrackDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.table.setMouseTracking(True)
        self.table.setDragEnabled(True)
        self.table.setAcceptDrops(True)
        self.table.setDropIndicatorShown(True)
        self.table.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.doubleClicked.connect(self._on_double_click)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().resizeSection(0, 36)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().resizeSection(2, 150)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().resizeSection(3, 118)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().resizeSection(4, 58)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.table, 1)

        self.empty_label = QLabel("Queue is empty — add files or drag a folder")
        self.empty_label.setObjectName("Hint")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.empty_label.setMinimumHeight(54)
        layout.addWidget(self.empty_label)
        self._refresh_state()

    # -------------------------------------------------------------------- api
    def set_playlist(self, playlist: Playlist) -> None:
        self._playlist = playlist
        self.model.set_playlist(playlist)
        self._refresh_state()

    def refresh(self) -> None:
        self.model.refresh()
        self._refresh_state()

    def set_current(self, index: int) -> None:
        self.model.set_current(index)
        if 0 <= index < self.model.rowCount():
            self.table.scrollTo(self.model.index(index, 0), QAbstractItemView.ScrollHint.EnsureVisible)

    def _refresh_state(self) -> None:
        count = len(self._playlist)
        duration = self._playlist.total_duration
        if self.count_label is not None:
            self.count_label.setText(f"{count} track{'s' if count != 1 else ''} · {format_duration(duration)}")
        self.empty_label.setVisible(count == 0)
        self.table.setVisible(count > 0)
        self.table.setEnabled(count > 0)

    # ----------------------------------------------------------------- events
    def _on_double_click(self, index: QModelIndex) -> None:
        if index.isValid():
            self.playRequested.emit(index.row())

    def _show_context_menu(self, position: QPoint) -> None:
        index = self.table.indexAt(position)
        menu = QMenu(self)
        play_action = menu.addAction("Play now")
        remove_action = menu.addAction("Remove from queue")
        menu.addSeparator()
        reveal_action = menu.addAction("Show in Explorer")
        copy_action = menu.addAction("Copy file path")
        play_action.setEnabled(index.isValid())
        remove_action.setEnabled(index.isValid())
        reveal_action.setEnabled(index.isValid())
        copy_action.setEnabled(index.isValid())
        chosen = menu.exec(self.table.viewport().mapToGlobal(position))
        if chosen is None or not index.isValid():
            return
        track = self.model.track_at(index.row())
        if track is None:
            return
        if chosen == play_action:
            self.playRequested.emit(index.row())
        elif chosen == remove_action:
            self.removeRequested.emit(index.row())
        elif chosen == reveal_action:
            _reveal_in_explorer(Path(track.path))
        elif chosen == copy_action:
            from PySide6.QtWidgets import QApplication

            QApplication.clipboard().setText(track.path)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        width = self.width()
        self.table.setColumnHidden(2, width < 620)
        self.table.setColumnHidden(3, width < 500)
        self.table.verticalHeader().setDefaultSectionSize(38 if self.height() < 230 else ROW_HEIGHT)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            index = self.table.currentIndex()
            if index.isValid():
                self.removeRequested.emit(index.row())
                event.accept()
                return
        if event.key() == Qt.Key.Key_Return:
            index = self.table.currentIndex()
            if index.isValid():
                self.playRequested.emit(index.row())
                event.accept()
                return
        super().keyPressEvent(event)


def _reveal_in_explorer(path: Path) -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        subprocess.Popen(["explorer", "/select,", str(path)])
    except OSError as exc:
        LOGGER.warning("cannot open explorer", extra={"error": str(exc)})
