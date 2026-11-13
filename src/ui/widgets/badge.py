"""The BIT-PERFECT indicator and secondary status pills.

The badge is the single most important piece of feedback in the application, so
it is painted by hand: a radial halo behind the pill communicates the state from
across the room, the inner dot breathes while the stream is genuinely exact, and
it stops breathing the instant the engine reports a deviation.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Property, QAbstractAnimation, QEasingCurve, QPointF, QPropertyAnimation, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..theme import PALETTE

STATE_COLORS = {
    "active": PALETTE.accent,
    "dop": PALETTE.accent,
    "degraded": PALETTE.warning,
    "off": PALETTE.text_dim,
    "error": PALETTE.danger,
}

STATE_LABELS = {
    "active": "BIT-PERFECT",
    "dop": "BIT-PERFECT · DoP",
    "degraded": "NOT BIT-PERFECT",
    "off": "ENGINE IDLE",
    "error": "STREAM ERROR",
}


class BitPerfectBadge(QWidget):
    """Glowing pill that reports the integrity verdict of the live stream."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._state = "off"
        self._reason = "engine idle"
        self._pulse = 0.0
        self._glow = 0.0
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(34)
        self.setMinimumWidth(178)
        self.setCursor(Qt.CursorShape.WhatsThisCursor)
        self.setToolTip("Bit-perfect status")

        self._pulse_animation = QPropertyAnimation(self, b"pulse", self)
        self._pulse_animation.setDuration(2400)
        self._pulse_animation.setStartValue(0.0)
        self._pulse_animation.setEndValue(1.0)
        self._pulse_animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._pulse_animation.setLoopCount(-1)

        self._glow_animation = QPropertyAnimation(self, b"glow", self)
        self._glow_animation.setDuration(420)
        self._glow_animation.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._refresh_tooltip()

    # ------------------------------------------------------------------ props
    def get_pulse(self) -> float:
        return self._pulse

    def set_pulse(self, value: float) -> None:
        self._pulse = float(value)
        self.update()

    pulse = Property(float, get_pulse, set_pulse)

    def get_glow(self) -> float:
        return self._glow

    def set_glow(self, value: float) -> None:
        self._glow = float(value)
        self.update()

    glow = Property(float, get_glow, set_glow)

    # -------------------------------------------------------------------- api
    @property
    def state(self) -> str:
        return self._state

    @property
    def reason(self) -> str:
        return self._reason

    def set_state(self, state: str, reason: str = "") -> None:
        state = state if state in STATE_COLORS else "off"
        changed = state != self._state
        self._state = state
        self._reason = reason or STATE_LABELS.get(state, "")
        self._refresh_tooltip()
        if changed:
            self._glow_animation.stop()
            self._glow_animation.setStartValue(self._glow)
            self._glow_animation.setEndValue(1.0)
            self._glow_animation.start()
        if state in ("active", "dop"):
            if self._pulse_animation.state() != QAbstractAnimation.State.Running:
                self._pulse_animation.start()
        else:
            self._pulse_animation.stop()
            self._pulse = 0.0
        self.update()

    def _refresh_tooltip(self) -> None:
        label = STATE_LABELS.get(self._state, "UNKNOWN")
        self.setToolTip(f"{label}\n{self._reason}" if self._reason else label)

    def sizeHint(self) -> QSize:  # type: ignore[override]
        metrics = QFontMetrics(self._label_font())
        width = metrics.horizontalAdvance(STATE_LABELS.get(self._state, "")) + 58
        return QSize(max(178, width), 34)

    def _label_font(self) -> QFont:
        font = QFont(self.font())
        font.setPointSizeF(8.4)
        font.setWeight(QFont.Weight.Bold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.4)
        return font

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        color = QColor(STATE_COLORS.get(self._state, PALETTE.text_dim))
        label = STATE_LABELS.get(self._state, "")
        font = self._label_font()
        metrics = QFontMetrics(font)
        text_width = metrics.horizontalAdvance(label)
        pill_width = min(self.width(), text_width + 58)
        rect = QRectF(2.0, 2.0, pill_width - 4.0, self.height() - 4.0)

        breathing = 0.5 + 0.5 * self._pulse if self._pulse_animation.state() == QAbstractAnimation.State.Running else 0.0
        halo = QColor(color)
        halo.setAlphaF(0.05 + 0.05 * breathing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(halo)
        painter.drawRoundedRect(rect.adjusted(-2, -2, 2, 2), (rect.height() + 4) / 2, (rect.height() + 4) / 2)

        fill = QColor(color)
        fill.setAlphaF(0.085 + 0.05 * breathing)
        border = QColor(color)
        border.setAlphaF(0.36 + 0.24 * breathing)
        painter.setBrush(fill)
        pen = QPen(border)
        pen.setWidthF(1.1)
        painter.setPen(pen)
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)

        dot_center = QPointF(rect.left() + 17.0, rect.center().y())
        if self._state in ("active", "dop"):
            ring = QColor(color)
            ring.setAlphaF(0.20 * (1.0 - breathing) + 0.06)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(ring)
            painter.drawEllipse(dot_center, 8.0, 8.0)
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(dot_center, 4.0, 4.0)

        painter.setFont(font)
        painter.setPen(QColor(PALETTE.text) if self._state in ("active", "dop") else color)
        text_rect = QRectF(rect.left() + 31.0, rect.top(), rect.width() - 40.0, rect.height())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)
        painter.end()


class StatusPill(QWidget):
    """Compact label/value pill used for the driver and the DAC clock."""

    def __init__(self, text: str = "—", parent: Optional[QWidget] = None, accent: bool = False) -> None:
        super().__init__(parent)
        self._text = text
        self._accent = accent
        self._dot = True
        self.setFixedHeight(28)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(96)

    def set_text(self, text: str, accent: Optional[bool] = None) -> None:
        changed = text != self._text or (accent is not None and accent != self._accent)
        self._text = text
        if accent is not None:
            self._accent = accent
        if changed:
            self.updateGeometry()
            self.update()

    def set_dot(self, visible: bool) -> None:
        self._dot = visible
        self.update()

    def sizeHint(self) -> QSize:  # type: ignore[override]
        metrics = QFontMetrics(self.font())
        return QSize(metrics.horizontalAdvance(self._text) + (38 if self._dot else 24), 28)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        color = QColor(PALETTE.accent if self._accent else PALETTE.text_muted)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        fill = QColor(PALETTE.accent_soft if self._accent else PALETTE.surface_alt)
        fill.setAlphaF(0.9)
        painter.setBrush(fill)
        border = QColor(PALETTE.accent_dim if self._accent else PALETTE.stroke)
        painter.setPen(QPen(border, 1.0))
        painter.drawRoundedRect(rect, 9, 9)
        text_left = rect.left() + 11
        if self._dot:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QPointF(rect.left() + 11.0, rect.center().y()), 3.0, 3.0)
            text_left += 13
        font = QFont(self.font())
        font.setPointSizeF(8.2)
        font.setWeight(QFont.Weight.DemiBold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.8)
        painter.setFont(font)
        painter.setPen(color)
        painter.drawText(
            QRectF(text_left, rect.top(), rect.width() - (text_left - rect.left()) - 8, rect.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            self._text,
        )
        painter.end()
