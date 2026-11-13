"""High precision transport seek bar.

A hand painted widget instead of QSlider so the handle, the hover ghost and the
millisecond read-out can share one visual language.  Scrubbing is optimistic:
the UI follows the pointer immediately and only commits the seek on release, so
the audio clock is never interrupted while dragging.
"""

from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..theme import PALETTE

TRACK_HEIGHT = 6.0
HANDLE_RADIUS = 7.0
SIDE_PADDING = 12.0


class SeekBar(QWidget):
    """Draggable progress bar with millisecond precision."""

    seekRequested = Signal(float)
    scrubStarted = Signal()
    scrubMoved = Signal(float)
    scrubEnded = Signal(float)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._duration = 0.0
        self._position = 0.0
        self._preview: Optional[float] = None
        self._step_seconds = 5.0
        self._hovering = False
        self._hover_x = -1.0
        self._dragging = False
        self._enabled = False
        self.setMinimumHeight(34)
        self.setMaximumHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Seek — drag or click; arrows ±5 s, Page Up/Down ±30 s, Home/End")

    # -------------------------------------------------------------------- api
    @property
    def duration(self) -> float:
        return self._duration

    @property
    def position(self) -> float:
        return self._preview if self._preview is not None else self._position

    def set_duration(self, seconds: float) -> None:
        try:
            duration = float(seconds)
        except (TypeError, ValueError, OverflowError):
            duration = 0.0
        if not math.isfinite(duration):
            duration = 0.0
        duration = max(0.0, duration)
        if abs(duration - self._duration) > 1e-9:
            self._duration = duration
            self._position = min(self._position, duration) if duration else 0.0
            self._enabled = duration > 0
            self._update_accessible_description()
            self.update()

    def set_position(self, seconds: float) -> None:
        if self._dragging:
            return
        try:
            position = float(seconds)
        except (TypeError, ValueError, OverflowError):
            position = 0.0
        if not math.isfinite(position):
            position = 0.0
        position = max(0.0, position)
        if self._duration > 0:
            position = min(position, self._duration)
        if abs(position - self._position) > 0.0005:
            self._position = position
            self._update_accessible_description()
            self.update()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled and self._duration > 0)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if self._enabled else Qt.CursorShape.ArrowCursor
        )
        self.setAccessibleName("Playback position")
        self._update_accessible_description()
        self.update()

    def _update_accessible_description(self) -> None:
        if not self._enabled:
            description = "No seekable track loaded."
        else:
            description = (
                f"{_format_time(self._position)} of {_format_time(self._duration)}. "
                "Use left and right arrows to seek 5 seconds; Home and End go to the beginning and end."
            )
        self.setAccessibleDescription(description)

    # ------------------------------------------------------------------ helpers
    def _track_rect(self) -> QRectF:
        return QRectF(
            SIDE_PADDING,
            self.height() / 2.0 - TRACK_HEIGHT / 2.0,
            max(1.0, self.width() - 2 * SIDE_PADDING),
            TRACK_HEIGHT,
        )

    def _value_to_x(self, value: float) -> float:
        track = self._track_rect()
        if self._duration <= 0:
            return track.left()
        ratio = max(0.0, min(1.0, value / self._duration))
        return track.left() + track.width() * ratio

    def _x_to_value(self, x: float) -> float:
        track = self._track_rect()
        if track.width() <= 0 or self._duration <= 0:
            return 0.0
        ratio = (x - track.left()) / track.width()
        return max(0.0, min(1.0, ratio)) * self._duration

    # ------------------------------------------------------------------- events
    def enterEvent(self, event) -> None:  # type: ignore[override]
        self._hovering = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._hovering = False
        self._hover_x = -1.0
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if not self._enabled or event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._dragging = True
        value = self._x_to_value(event.position().x())
        self._preview = value
        self.scrubStarted.emit()
        self.scrubMoved.emit(value)
        self.update()

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        self._hover_x = event.position().x()
        if self._dragging:
            value = self._x_to_value(self._hover_x)
            self._preview = value
            self.scrubMoved.emit(value)
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if not self._dragging or event.button() != Qt.MouseButton.LeftButton:
            return
        value = self._x_to_value(event.position().x())
        self._dragging = False
        self._preview = None
        self._position = value
        self._update_accessible_description()
        self.scrubEnded.emit(value)
        self.seekRequested.emit(value)
        self.update()

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        if not self._enabled:
            return
        steps = event.angleDelta().y() / 120.0
        if steps == 0:
            return
        delta = self._step_seconds * steps
        target = max(0.0, min(self._duration, self.position + delta))
        self._position = target
        self._update_accessible_description()
        self.seekRequested.emit(target)
        self.update()
        event.accept()

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if not self._enabled:
            super().keyPressEvent(event)
            return
        key = event.key()
        target: Optional[float] = None
        if key == Qt.Key.Key_Space:
            self._enabled = False
            self.clearFocus()
            event.ignore()
            return
        if key in (Qt.Key.Key_Right, Qt.Key.Key_Up):
            target = min(self._duration, self.position + self._step_seconds)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Down):
            target = max(0.0, self.position - self._step_seconds)
        elif key == Qt.Key.Key_Home:
            target = 0.0
        elif key == Qt.Key.Key_End:
            target = self._duration
        elif key == Qt.Key.Key_PageUp:
            target = min(self._duration, self.position + 30.0)
        elif key == Qt.Key.Key_PageDown:
            target = max(0.0, self.position - 30.0)
        if target is not None:
            self._position = target
            self._update_accessible_description()
            self.seekRequested.emit(target)
            self.update()
            event.accept()
            return
        super().keyPressEvent(event)

    # -------------------------------------------------------------------- paint
    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        track = self._track_rect()
        radius = track.height() / 2.0

        base = QColor(PALETTE.stroke)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(base)
        painter.drawRoundedRect(track, radius, radius)

        for fraction in (0.25, 0.5, 0.75):
            tick_x = track.left() + track.width() * fraction
            painter.setBrush(QColor(PALETTE.stroke_strong))
            painter.drawRect(QRectF(tick_x - 0.5, track.top() + 1.5, 1.0, track.height() - 3.0))

        value = self.position
        played_width = self._value_to_x(value) - track.left()
        if played_width > 0.5:
            gradient = QLinearGradient(track.left(), 0, track.left() + played_width, 0)
            gradient.setColorAt(0.0, QColor(PALETTE.accent_dim))
            gradient.setColorAt(0.65, QColor(PALETTE.accent))
            gradient.setColorAt(1.0, QColor(PALETTE.accent_alt))
            played = QRectF(track.left(), track.top(), played_width, track.height())
            painter.setBrush(gradient)
            painter.drawRoundedRect(played, radius, radius)

        if self._hovering and not self._dragging and self._enabled:
            hover_value = self._x_to_value(self._hover_x)
            hover_x = self._value_to_x(hover_value)
            ghost = QColor(PALETTE.text)
            ghost.setAlphaF(0.12)
            painter.setBrush(ghost)
            painter.drawRoundedRect(
                QRectF(track.left(), track.top(), max(0.0, hover_x - track.left()), track.height()),
                radius,
                radius,
            )

        handle_x = self._value_to_x(value)
        handle_center = QPointF(handle_x, track.center().y())
        active = self._dragging or self._hovering or self.hasFocus()
        if active:
            halo = QColor(PALETTE.accent)
            halo.setAlphaF(0.16 if self._dragging else 0.10)
            painter.setBrush(halo)
            painter.drawEllipse(handle_center, HANDLE_RADIUS + 6.0, HANDLE_RADIUS + 6.0)
        if self._enabled:
            painter.setBrush(QColor(PALETTE.text))
            painter.setPen(QPen(QColor(PALETTE.base), 2.0))
            painter.drawEllipse(handle_center, HANDLE_RADIUS if active else HANDLE_RADIUS - 1.0, HANDLE_RADIUS if active else HANDLE_RADIUS - 1.0)
        else:
            painter.setBrush(QColor(PALETTE.stroke_strong))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(handle_center, HANDLE_RADIUS - 2.0, HANDLE_RADIUS - 2.0)

        if active and self._duration > 0:
            label = _format_time(value)
            font = QFont(self.font())
            font.setPointSizeF(8.2)
            font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(font)
            metrics = painter.fontMetrics()
            text_width = metrics.horizontalAdvance(label) + 14
            text_rect = QRectF(handle_x - text_width / 2.0, track.top() - 24.0, text_width, 18.0)
            text_rect.moveLeft(max(2.0, min(text_rect.left(), self.width() - text_width - 2.0)))
            bubble = QColor(PALETTE.surface_raised)
            bubble.setAlphaF(0.96)
            painter.setBrush(bubble)
            painter.setPen(QPen(QColor(PALETTE.stroke_strong), 1.0))
            painter.drawRoundedRect(text_rect, 6.0, 6.0)
            painter.setPen(QColor(PALETTE.text))
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, label)
        painter.end()


def _format_time(seconds: float) -> str:
    total_ms = int(round(max(0.0, seconds) * 1000))
    minutes, remainder = divmod(total_ms, 60000)
    secs, millis = divmod(remainder, 1000)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}.{millis:03d}"
    return f"{minutes:02d}:{secs:02d}.{millis:03d}"
