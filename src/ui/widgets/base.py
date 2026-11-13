"""Primitive widgets: animated icon buttons, chips, cards and rows."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import (
    Property,
    QAbstractAnimation,
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import QAbstractButton, QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ..icons import paint_icon
from ..theme import PALETTE, mix


class IconButton(QAbstractButton):
    """A borderless vector button with a soft, animated hover halo."""

    def __init__(
        self,
        icon_name: str,
        tooltip: str = "",
        size: int = 38,
        icon_size: int = 17,
        parent: Optional[QWidget] = None,
        accent: bool = False,
        checkable: bool = False,
    ) -> None:
        super().__init__(parent)
        self._icon_name = icon_name
        self._icon_size = icon_size
        self._hover = 0.0
        self._accent = accent
        self._checked_visual = False
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        if tooltip:
            self.setToolTip(tooltip)
        self.setCheckable(checkable)
        self._animation = QPropertyAnimation(self, b"hoverProgress", self)
        self._animation.setDuration(160)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._on_toggled)

    # ------------------------------------------------------------------ anim
    def get_hover_progress(self) -> float:
        return self._hover

    def set_hover_progress(self, value: float) -> None:
        self._hover = float(value)
        self.update()

    hoverProgress = Property(float, get_hover_progress, set_hover_progress)

    def _animate_to(self, target: float) -> None:
        self._animation.stop()
        self._animation.setStartValue(self._hover)
        self._animation.setEndValue(target)
        self._animation.start()

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self._animate_to(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._animate_to(0.0)
        super().leaveEvent(event)

    def _on_toggled(self, checked: bool) -> None:
        self._checked_visual = checked
        self.update()

    # ------------------------------------------------------------------- api
    def set_icon(self, name: str) -> None:
        if name != self._icon_name:
            self._icon_name = name
            self.update()

    def set_accent(self, accent: bool) -> None:
        if accent != self._accent:
            self._accent = accent
            self.update()

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(self.width(), self.height())

    # ----------------------------------------------------------------- paint
    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)

        if self._accent or self._checked_visual:
            glow = QColor(PALETTE.accent)
            glow.setAlphaF(0.10 + 0.16 * self._hover)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(rect)
            if self._checked_visual:
                border = QColor(PALETTE.accent)
                border.setAlphaF(0.35 + 0.35 * self._hover)
                pen = painter.pen()
                pen.setColor(border)
                pen.setWidthF(1.0)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(rect)
            color = QColor(PALETTE.accent)
        else:
            if self._hover > 0.001:
                halo = QColor(PALETTE.surface_raised)
                halo.setAlphaF(0.85 * self._hover)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(halo)
                painter.drawEllipse(rect)
            color = mix(QColor(PALETTE.text_muted), QColor(PALETTE.text), self._hover)

        if not self.isEnabled():
            color.setAlphaF(0.35)
        if self.isDown():
            color = mix(color, QColor(PALETTE.base), 0.25)

        icon_rect = QRectF(
            rect.center().x() - self._icon_size / 2.0,
            rect.center().y() - self._icon_size / 2.0,
            self._icon_size,
            self._icon_size,
        )
        thickness = 1.9 if self._icon_name in ("stop", "play", "pause") else 1.6
        paint_icon(painter, self._icon_name, icon_rect, color, thickness=thickness)
        if self.hasFocus():
            focus = QColor(PALETTE.accent)
            focus.setAlphaF(0.55)
            pen = painter.pen()
            pen.setColor(focus)
            pen.setWidthF(1.0)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(rect.adjusted(0.5, 0.5, -0.5, -0.5))
        painter.end()


class ElidedLabel(QLabel):
    """QLabel that elides its text instead of forcing the layout to grow."""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self._full_text = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)

    def setText(self, text: str) -> None:  # type: ignore[override]
        self._full_text = text or ""
        self.setToolTip(self._full_text)
        self._apply_elide()

    def text(self) -> str:  # type: ignore[override]
        """The logical text: the elided fragment is only what gets painted."""

        return self._full_text

    def full_text(self) -> str:
        return self._full_text

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        if self.width() <= 1:
            return  # not laid out yet: never flash a bare ellipsis
        metrics = QFontMetrics(self.font())
        elided = metrics.elidedText(self._full_text, Qt.TextElideMode.ElideRight, max(10, self.width()))
        if elided != super().text():
            super().setText(elided)


class Card(QFrame):
    """Rounded panel with the standard studio surface treatment."""

    def __init__(self, parent: Optional[QWidget] = None, raised: bool = False, spacing: int = 14) -> None:
        super().__init__(parent)
        self.setObjectName("CardRaised" if raised else "Card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(18, 16, 18, 16)
        self._layout.setSpacing(spacing)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    @property
    def body(self) -> QVBoxLayout:
        return self._layout


class Chip(QLabel):
    """Small uppercase badge used for formats and states."""

    def __init__(self, text: str = "", variant: str = "chip", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self._variant = variant
        self.setObjectName(self._object_name_for(variant))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        font = QFont(self.font())
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1.2))
        font.setWeight(QFont.Weight.DemiBold)
        self.setFont(font)

    @staticmethod
    def _object_name_for(variant: str) -> str:
        return {
            "accent": "ChipAccent",
            "warning": "ChipWarning",
            "chip": "Chip",
        }.get(variant, "Chip")

    def set_variant(self, variant: str) -> None:
        if variant == self._variant and self.objectName() == self._object_name_for(variant):
            return
        self._variant = variant
        self.setObjectName(self._object_name_for(variant))
        style = self.style()
        if style is not None:
            style.unpolish(self)
            style.polish(self)
        self.update()

    def set_text(self, text: str) -> None:
        self.setText(text)
        self.setVisible(bool(text))


class SectionLabel(QLabel):
    def __init__(self, text: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(text.upper(), parent)
        self.setObjectName("SectionTitle")


class MonoLabel(QLabel):
    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("MonoValue")
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)


class Separator(QFrame):
    def __init__(self, parent: Optional[QWidget] = None, vertical: bool = False) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.VLine if vertical else QFrame.Shape.HLine)
        self.setStyleSheet(f"color: {PALETTE.stroke}; background-color: {PALETTE.stroke};")
        if vertical:
            self.setFixedWidth(1)
        else:
            self.setFixedHeight(1)


class InfoRow(QWidget):
    """A label/value pair used across the hardware panel."""

    def __init__(self, label: str, value: str = "—", parent: Optional[QWidget] = None, mono: bool = False) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(10)
        self._label = QLabel(label.upper())
        self._label.setObjectName("TileTitle")
        self._value_text = value
        if mono:
            self._value: QLabel = MonoLabel(value)
            # Long digests and paths must not widen the whole card.
            self._value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            self._value.setToolTip(value)
        else:
            self._value = ElidedLabel(value)
            self._value.setObjectName("TileSub")
        self._value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._color_name = ""
        layout.addWidget(self._label)
        layout.addStretch(1)
        layout.addWidget(self._value)

    def set_value(self, value: str) -> None:
        if value == self._value_text:
            return
        self._value_text = value
        self._value.setText(value)
        self._value.setToolTip(value)

    def set_value_color(self, color: QColor) -> None:
        name = color.name()
        if name == self._color_name:
            return
        self._color_name = name
        self._value.setStyleSheet(f"color: {name};")


class DotIndicator(QWidget):
    """A tiny status dot with an optional breathing animation."""

    def __init__(self, parent: Optional[QWidget] = None, color: QColor = QColor(PALETTE.accent)) -> None:
        super().__init__(parent)
        self._color = QColor(color)
        self._pulse = 0.0
        self.setFixedSize(10, 10)
        self._animation = QPropertyAnimation(self, b"pulse", self)
        self._animation.setDuration(2200)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._animation.setLoopCount(-1)

    def get_pulse(self) -> float:
        return self._pulse

    def set_pulse(self, value: float) -> None:
        self._pulse = float(value)
        self.update()

    pulse = Property(float, get_pulse, set_pulse)

    def start(self) -> None:
        if self._animation.state() != QAbstractAnimation.State.Running:
            self._animation.start()

    def stop(self) -> None:
        self._animation.stop()
        self._pulse = 0.0
        self.update()

    def set_color(self, color: QColor) -> None:
        self._color = QColor(color)
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        center = QRectF(self.rect()).center()
        halo = QColor(self._color)
        halo.setAlphaF(0.16 + 0.14 * (1.0 - abs(self._pulse * 2 - 1)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(halo)
        painter.drawEllipse(center, 5.0, 5.0)
        painter.setBrush(self._color)
        painter.drawEllipse(center, 2.6, 2.6)
        painter.end()
