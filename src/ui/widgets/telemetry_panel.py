"""Buffer health graph and signal path.

The numeric readouts live in the footer strip (always visible) and in the session
report, so this panel keeps only what those two cannot show: the buffer history,
its occupancy meter and the attested signal path.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ...config import humanise_rate
from ...core.telemetry import TelemetrySnapshot
from ..theme import PALETTE, alpha
from .base import Card, Chip, InfoRow, SectionLabel


class Sparkline(QWidget):
    """Buffer-fill history rendered as a filled area chart."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._history: List[float] = []
        self.setMinimumHeight(64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip("Ocupación del búfer circular en las últimas 480 muestras")

    def set_history(self, history: Iterable[float]) -> None:
        self._history = [max(0.0, min(1.0, value)) for value in history]
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(1, 4, -1, -4)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(PALETTE.surface_alt))
        painter.drawRoundedRect(rect, 8, 8)

        for fraction in (0.25, 0.5, 0.75):
            y = rect.bottom() - rect.height() * fraction
            pen = QPen(QColor(PALETTE.stroke))
            pen.setStyle(Qt.PenStyle.DashLine)
            pen.setWidthF(1.0)
            painter.setPen(pen)
            painter.drawLine(QPointF(rect.left() + 6, y), QPointF(rect.right() - 6, y))

        history = self._history
        if len(history) >= 2:
            path = QPainterPath()
            step = rect.width() / max(1, len(history) - 1)
            path.moveTo(rect.left(), rect.bottom() - rect.height() * history[0])
            for index, value in enumerate(history[1:], start=1):
                x = rect.left() + step * index
                y = rect.bottom() - rect.height() * value
                path.lineTo(x, y)
            area = QPainterPath(path)
            area.lineTo(rect.right(), rect.bottom())
            area.lineTo(rect.left(), rect.bottom())
            area.closeSubpath()
            gradient = QLinearGradient(0, rect.top(), 0, rect.bottom())
            gradient.setColorAt(0.0, alpha(QColor(PALETTE.accent), 0.34))
            gradient.setColorAt(1.0, alpha(QColor(PALETTE.accent), 0.02))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(gradient)
            painter.drawPath(area)
            pen = QPen(QColor(PALETTE.accent), 1.6)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
        else:
            painter.setPen(QColor(PALETTE.text_dim))
            font = QFont(self.font())
            font.setPointSizeF(8.5)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "esperando señal de audio")
        painter.end()


class MeterBar(QWidget):
    """Thin fill meter with a gradient and a target marker."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._value = 0.0
        self._target = 0.75
        self.setFixedHeight(10)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, value: float) -> None:
        value = max(0.0, min(1.0, float(value)))
        if abs(value - self._value) > 0.002:
            self._value = value
            self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0, 2, 0, -2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(PALETTE.surface_alt))
        painter.drawRoundedRect(rect, 3, 3)
        if self._value > 0:
            fill = QRectF(rect.left(), rect.top(), max(2.0, rect.width() * self._value), rect.height())
            gradient = QLinearGradient(fill.left(), 0, fill.right(), 0)
            danger = self._value < 0.08
            gradient.setColorAt(0.0, QColor(PALETTE.danger if danger else PALETTE.accent_dim))
            gradient.setColorAt(1.0, QColor(PALETTE.danger if danger else PALETTE.accent))
            painter.setBrush(gradient)
            painter.drawRoundedRect(fill, 3, 3)
        marker_x = rect.left() + rect.width() * self._target
        pen = QPen(alpha(QColor(PALETTE.text), 0.35), 1.0)
        painter.setPen(pen)
        painter.drawLine(QPointF(marker_x, rect.top()), QPointF(marker_x, rect.bottom()))
        painter.end()


class TelemetryPanel(QWidget):
    """Buffer health plus the attested signal path."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        buffer_card = Card(spacing=8)
        buffer_card.body.setContentsMargins(14, 12, 14, 12)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(SectionLabel("Estado del búfer"))
        header.addStretch(1)
        self.buffer_state = Chip("EN ESPERA", "chip")
        header.addWidget(self.buffer_state)
        buffer_card.body.addLayout(header)
        self.sparkline = Sparkline()
        buffer_card.body.addWidget(self.sparkline)
        self.meter = MeterBar()
        buffer_card.body.addWidget(self.meter)
        self.fill_row = InfoRow("Ocupación", "—")
        self.capacity_row = InfoRow("Capacidad", "—")
        self.latency_row = InfoRow("Latencia", "—")
        self.xrun_row = InfoRow("Cortes", "0")
        for row in (self.fill_row, self.capacity_row, self.latency_row, self.xrun_row):
            buffer_card.body.addWidget(row)
        layout.addWidget(buffer_card)

        path_card = Card(spacing=6)
        path_card.body.setContentsMargins(14, 12, 14, 14)
        path_card.body.addWidget(SectionLabel("Ruta de señal"))
        self.conversion_row = InfoRow("Conversión del contenedor", "—")
        self.driver_row = InfoRow("Controlador", "—")
        self.priority_row = InfoRow("Prioridad del hilo", "—")
        for row in (self.conversion_row, self.driver_row, self.priority_row):
            path_card.body.addWidget(row)
        self.notes = QLabel("")
        self.notes.setObjectName("Hint")
        self.notes.setWordWrap(True)
        path_card.body.addWidget(self.notes)
        layout.addWidget(path_card)
        layout.addStretch(1)

    # -------------------------------------------------------------------- api
    def update_from(self, snapshot: TelemetrySnapshot, history: Optional[Iterable[float]] = None) -> None:
        self.meter.set_value(snapshot.buffer_fill)
        if history is not None:
            self.sparkline.set_history(history)
        health = snapshot.buffer_health
        self.buffer_state.set_text(
            {"optimal": "ÓPTIMO", "degraded": "BAJO", "failing": "CRÍTICO"}.get(health, "EN ESPERA")
        )
        self.buffer_state.set_variant(
            "accent" if health == "optimal" else ("warning" if health == "degraded" else "chip")
        )

        clock = snapshot.hardware_clock or snapshot.source_rate
        clock_text = humanise_rate(clock) if clock else "—"
        self.fill_row.set_value(f"{snapshot.buffer_ms:.1f} ms · {snapshot.buffer_fill * 100:.0f} %")
        self.capacity_row.set_value(f"{snapshot.buffer_capacity_ms:.0f} ms a {clock_text}")
        self.latency_row.set_value(f"{snapshot.latency_ms:.1f} ms")
        xrun_color = QColor(PALETTE.danger) if snapshot.xruns else QColor(PALETTE.success)
        self.xrun_row.set_value(f"{snapshot.xruns} · {snapshot.silence_frames} tramas de silencio")
        self.xrun_row.set_value_color(xrun_color)

        self.conversion_row.set_value(snapshot.conversion_chain or "—")
        self.driver_row.set_value(snapshot.driver_label or "—")
        self.priority_row.set_value("TIEMPO REAL" if snapshot.priority_realtime else "estándar")
        notes = list(snapshot.track_notes)
        if snapshot.last_error:
            notes.insert(0, snapshot.last_error)
        self.notes.setText(" · ".join(notes[:3]) if notes else "Ninguna etapa de procesamiento en la ruta de señal.")
