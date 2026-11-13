"""Real-time hardware telemetry: clocks, buffer health, xruns and integrity."""

from __future__ import annotations

from typing import Iterable, List, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...config import humanise_rate
from ...core.telemetry import TelemetrySnapshot
from ..theme import PALETTE, alpha
from .base import Card, Chip, InfoRow, SectionLabel


class Sparkline(QWidget):
    """Buffer-fill history rendered as a filled area chart."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._history: List[float] = []
        self._minimum = 0.0
        self._maximum = 1.0
        self.setMinimumHeight(64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip("Ring buffer occupancy over the last 480 samples")

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

        for fraction, label in ((0.25, ""), (0.5, ""), (0.75, "")):
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
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "waiting for stream")
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


class TelemetryTile(Card):
    """Compact metric card."""

    def __init__(self, title: str, value: str = "—", sub: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, spacing=2)
        self.body.setContentsMargins(14, 11, 14, 11)
        self._title = QLabel(title.upper())
        self._title.setObjectName("TileTitle")
        self._value = QLabel(value)
        self._value.setObjectName("TileValue")
        self._sub = QLabel(sub)
        self._sub.setObjectName("TileSub")
        self.body.addWidget(self._title)
        self.body.addWidget(self._value)
        self.body.addWidget(self._sub)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, value: str, sub: Optional[str] = None, color: Optional[QColor] = None) -> None:
        if self._value.text() != value:
            self._value.setText(value)
        if sub is not None and self._sub.text() != sub:
            self._sub.setText(sub)
        if color is not None:
            self._value.setStyleSheet(f"color: {color.name()};")
        else:
            self._value.setStyleSheet("")

    def set_title(self, title: str) -> None:
        self._title.setText(title.upper())


class TelemetryPanel(QWidget):
    """Grid of metrics plus buffer history and signal path."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        self.clock_tile = TelemetryTile("DAC clock", "—", "hardware rate")
        self.depth_tile = TelemetryTile("Depth", "—", "valid bits")
        self.buffer_tile = TelemetryTile("Buffer", "—", "ring occupancy")
        self.latency_tile = TelemetryTile("Latency", "—", "driver + ring")
        self.xrun_tile = TelemetryTile("Underruns", "0", "since stream start")
        self.digest_tile = TelemetryTile("Stream hash", "—", "blake2b of DAC bytes")
        tiles = [
            self.clock_tile,
            self.depth_tile,
            self.buffer_tile,
            self.latency_tile,
            self.xrun_tile,
            self.digest_tile,
        ]
        for index, tile in enumerate(tiles):
            grid.addWidget(tile, index // 3, index % 3)
        layout.addLayout(grid)

        buffer_card = Card(spacing=8)
        buffer_card.body.setContentsMargins(14, 12, 14, 12)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(SectionLabel("Buffer health"))
        header.addStretch(1)
        self.buffer_state = Chip("IDLE", "chip")
        header.addWidget(self.buffer_state)
        buffer_card.body.addLayout(header)
        self.sparkline = Sparkline()
        buffer_card.body.addWidget(self.sparkline)
        self.meter = MeterBar()
        buffer_card.body.addWidget(self.meter)
        layout.addWidget(buffer_card)

        path_card = Card(spacing=6)
        path_card.body.setContentsMargins(14, 12, 14, 14)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.addWidget(SectionLabel("Signal path"))
        title_row.addStretch(1)
        self.dsp_chip = Chip("ZERO DSP", "accent")
        title_row.addWidget(self.dsp_chip)
        path_card.body.addLayout(title_row)
        self.conversion_row = InfoRow("Container mapping", "—")
        self.driver_row = InfoRow("Driver", "—")
        self.priority_row = InfoRow("Thread priority", "—")
        self.device_row = InfoRow("Device", "—")
        for row in (self.conversion_row, self.driver_row, self.priority_row, self.device_row):
            path_card.body.addWidget(row)
        self.notes = QLabel("")
        self.notes.setObjectName("Hint")
        self.notes.setWordWrap(True)
        path_card.body.addWidget(self.notes)
        layout.addWidget(path_card)
        layout.addStretch(1)

    # -------------------------------------------------------------------- api
    def update_from(self, snapshot: TelemetrySnapshot, history: Optional[Iterable[float]] = None) -> None:
        clock = snapshot.hardware_clock or snapshot.source_rate
        clock_text = humanise_rate(clock) if clock else "—"
        source_text = (
            f"source {humanise_rate(snapshot.source_rate)}" if snapshot.source_rate else "no source"
        )
        self.clock_tile.set_value(clock_text, source_text, QColor(PALETTE.accent) if snapshot.bit_perfect else None)

        depth = snapshot.hardware_bits or snapshot.source_bits
        depth_text = f"{depth}-bit" if depth else "—"
        depth_sub = f"source {snapshot.source_bits}-bit" if snapshot.source_bits else "no source"
        if snapshot.is_dsd:
            depth_text = "DSD · DoP"
            depth_sub = f"{snapshot.source_bits}-bit container"
        self.depth_tile.set_value(depth_text, depth_sub)

        buffer_text = f"{snapshot.buffer_ms:.1f} ms"
        buffer_sub = f"of {snapshot.buffer_capacity_ms:.0f} ms · {snapshot.buffer_fill * 100:.0f}%"
        self.buffer_tile.set_value(buffer_text, buffer_sub)
        self.latency_tile.set_value(f"{snapshot.latency_ms:.1f} ms", f"clock {clock_text}")
        xrun_color = QColor(PALETTE.danger) if snapshot.xruns else QColor(PALETTE.success)
        self.xrun_tile.set_value(str(snapshot.xruns), f"{snapshot.silence_frames} silence frames", xrun_color)
        digest = snapshot.integrity_digest[:12] if snapshot.integrity_digest else "—"
        self.digest_tile.set_value(digest, f"{snapshot.integrity_bytes_sent / 1048576:.1f} MiB sent")

        self.meter.set_value(snapshot.buffer_fill)
        if history is not None:
            self.sparkline.set_history(history)
        health = snapshot.buffer_health
        self.buffer_state.set_text({"optimal": "HEALTHY", "degraded": "LOW", "failing": "CRITICAL"}.get(health, "IDLE"))
        self.buffer_state.set_variant("accent" if health == "optimal" else ("warning" if health == "degraded" else "chip"))

        self.conversion_row.set_value(snapshot.conversion_chain or "—")
        self.driver_row.set_value(snapshot.driver_label or "—")
        priority_text = "REALTIME" if snapshot.priority_realtime else "standard"
        self.priority_row.set_value(priority_text)
        self.device_row.set_value(snapshot.device_name or "—")
        notes = list(snapshot.track_notes)
        if snapshot.last_error:
            notes.insert(0, snapshot.last_error)
        self.notes.setText(" · ".join(notes[:4]) if notes else "No processing stages in the signal path.")
        self.dsp_chip.set_text("ZERO DSP" if not snapshot.dsp_chain else f"{len(snapshot.dsp_chain)} STAGES")
        self.dsp_chip.set_variant("accent" if not snapshot.dsp_chain else "warning")
