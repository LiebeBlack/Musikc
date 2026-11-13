"""Studio dark design system: palette, typography, QSS and micro-animations.

Everything is generated from a single :class:`Palette` so the whole application
(including hand-painted widgets) stays visually coherent.  The stylesheet avoids
image assets entirely: gradients, borders and radii are enough for a mastering
grade look and keep the installer small.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QWidget

FONT_STACK = (
    "Segoe UI Variable Display",
    "Segoe UI Variable Text",
    "Segoe UI",
    "Inter",
    "Helvetica Neue",
    "Arial",
)

MONO_STACK = ("Cascadia Mono", "Cascadia Code", "Consolas", "JetBrains Mono", "Courier New")


@dataclass(frozen=True)
class Palette:
    """Semantic colour tokens for the studio dark theme."""

    base: str = "#080A0F"
    surface: str = "#0E1118"
    surface_alt: str = "#131823"
    surface_raised: str = "#182030"
    overlay: str = "#1D2635"
    stroke: str = "#222B3B"
    stroke_strong: str = "#2E3A4E"
    text: str = "#E9EEF7"
    text_muted: str = "#9AA5B8"
    text_dim: str = "#64708A"
    accent: str = "#4FE3C1"
    accent_dim: str = "#2A8C7A"
    accent_soft: str = "#173B36"
    accent_alt: str = "#7C9CFF"
    warning: str = "#F5B44B"
    warning_soft: str = "#3A2C14"
    danger: str = "#F87171"
    danger_soft: str = "#3A1A1D"
    success: str = "#5BE49B"
    purple: str = "#B794F6"

    def q(self, name: str) -> QColor:
        return QColor(getattr(self, name))

    def as_dict(self) -> Dict[str, str]:
        return {
            field: getattr(self, field)
            for field in self.__dataclass_fields__  # type: ignore[attr-defined]
        }


PALETTE = Palette()
RADIUS = 14
RADIUS_SMALL = 9
SPACING = 12


def _installed_families() -> Optional[set]:
    """Return installed font families, or ``None`` when no GUI app exists yet.

    This keeps :func:`build_stylesheet` usable from headless tools (the QSS
    export script and the CI checks) without instantiating Qt widgets.
    """

    if QGuiApplication.instance() is None:
        return None
    try:
        return set(QFontDatabase.families())
    except Exception:
        return None


def resolve_font_family(candidates: Iterable[str] = FONT_STACK) -> str:
    available = _installed_families()
    if available is None:
        return candidates[0] if isinstance(candidates, tuple) else "Segoe UI"
    for family in candidates:
        if family in available:
            return family
    return "Sans Serif"


def resolve_mono_family() -> str:
    available = _installed_families()
    if available is None:
        return MONO_STACK[0]
    for family in MONO_STACK:
        if family in available:
            return family
    return "Courier New"


def build_stylesheet(palette: Palette = PALETTE) -> str:
    return f"""
    * {{
        outline: none;
    }}

    QWidget {{
        color: {palette.text};
        background-color: transparent;
        selection-background-color: {palette.accent_soft};
        selection-color: {palette.text};
    }}

    QMainWindow, #RootSurface {{
        background-color: {palette.base};
    }}

    #ContentSurface {{
        background-color: {palette.base};
    }}

    #Card, QFrame#Card {{
        background-color: {palette.surface};
        border: 1px solid {palette.stroke};
        border-radius: {RADIUS}px;
    }}

    #CardRaised, QFrame#CardRaised {{
        background-color: {palette.surface_alt};
        border: 1px solid {palette.stroke};
        border-radius: {RADIUS}px;
    }}

    #HeaderSurface {{
        background-color: {palette.surface};
        border-bottom: 1px solid {palette.stroke};
    }}

    #FooterSurface {{
        background-color: {palette.surface};
        border-top: 1px solid {palette.stroke};
    }}

    QLabel {{
        background: transparent;
    }}

    QLabel#AppTitle {{
        font-size: 15px;
        font-weight: 700;
        letter-spacing: 3px;
        color: {palette.text};
    }}

    QLabel#AppSubtitle {{
        font-size: 10px;
        letter-spacing: 2px;
        color: {palette.text_dim};
    }}

    QLabel#SectionTitle {{
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 2px;
        color: {palette.text_dim};
    }}

    QLabel#TrackTitle {{
        font-size: 30px;
        font-weight: 700;
        color: {palette.text};
    }}

    QLabel#TrackArtist {{
        font-size: 14px;
        color: {palette.text_muted};
    }}

    QLabel#TrackAlbum {{
        font-size: 12px;
        color: {palette.text_dim};
    }}

    QLabel#TimeLabel {{
        font-size: 12px;
        color: {palette.text_muted};
    }}

    QLabel#MonoValue {{
        font-family: "{resolve_mono_family()}";
        font-size: 12px;
        color: {palette.text};
    }}

    QLabel#Hint {{
        color: {palette.text_dim};
        font-size: 11px;
    }}

    QLabel#Warning {{
        color: {palette.warning};
        font-size: 11px;
    }}

    QLabel#Error {{
        color: {palette.danger};
        font-size: 11px;
    }}

    QToolTip {{
        background-color: {palette.surface_raised};
        color: {palette.text};
        border: 1px solid {palette.stroke_strong};
        padding: 6px 9px;
        border-radius: 6px;
    }}

    QComboBox {{
        background-color: {palette.surface_alt};
        border: 1px solid {palette.stroke};
        border-radius: {RADIUS_SMALL}px;
        padding: 6px 12px;
        min-height: 22px;
        color: {palette.text};
        selection-background-color: {palette.accent_soft};
    }}

    QComboBox:hover {{
        border-color: {palette.stroke_strong};
        background-color: {palette.surface_raised};
    }}

    QComboBox:focus {{
        border-color: {palette.accent_dim};
    }}

    QComboBox::drop-down {{
        border: none;
        width: 22px;
    }}

    QComboBox::down-arrow {{
        image: none;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 5px solid {palette.text_muted};
        margin-right: 9px;
    }}

    QComboBox QAbstractItemView {{
        background-color: {palette.surface_raised};
        border: 1px solid {palette.stroke_strong};
        border-radius: 8px;
        padding: 4px;
        color: {palette.text};
        selection-background-color: {palette.accent_soft};
        selection-color: {palette.accent};
        outline: none;
    }}

    QPushButton {{
        background-color: {palette.surface_alt};
        border: 1px solid {palette.stroke};
        border-radius: {RADIUS_SMALL}px;
        padding: 6px 12px;
        min-height: 26px;
        color: {palette.text};
    }}

    QPushButton:hover {{
        background-color: {palette.surface_raised};
        border-color: {palette.stroke_strong};
    }}

    QPushButton:pressed {{
        background-color: {palette.overlay};
    }}

    QPushButton:disabled {{
        color: {palette.text_dim};
        border-color: {palette.stroke};
    }}

    QPushButton:focus {{
        border: 1px solid {palette.accent_dim};
    }}

    QPushButton#Primary {{
        background-color: {palette.accent_soft};
        border: 1px solid {palette.accent_dim};
        color: {palette.accent};
        font-weight: 600;
    }}

    QPushButton#Primary:hover {{
        background-color: {palette.accent_dim};
        color: #05100D;
    }}

    QPushButton#Ghost {{
        background: transparent;
        border: 1px solid transparent;
        color: {palette.text_muted};
    }}

    QPushButton#Ghost:hover {{
        background-color: {palette.surface_raised};
        color: {palette.text};
    }}

    QCheckBox {{
        spacing: 8px;
        color: {palette.text_muted};
    }}

    QCheckBox::indicator {{
        width: 16px;
        height: 16px;
        border-radius: 5px;
        border: 1px solid {palette.stroke_strong};
        background-color: {palette.surface_alt};
    }}

    QCheckBox::indicator:hover {{
        border-color: {palette.accent_dim};
    }}

    QCheckBox::indicator:checked {{
        background-color: {palette.accent};
        border-color: {palette.accent};
    }}

    QLineEdit {{
        background-color: {palette.surface_alt};
        border: 1px solid {palette.stroke};
        border-radius: {RADIUS_SMALL}px;
        padding: 7px 12px;
        color: {palette.text};
        selection-background-color: {palette.accent_soft};
    }}

    QLineEdit:focus {{
        border-color: {palette.accent_dim};
    }}

    QTabWidget::pane {{
        border: none;
        background: transparent;
    }}

    QTabBar {{
        background: transparent;
    }}

    QTabBar::tab {{
        background: transparent;
        color: {palette.text_dim};
        padding: 7px 15px;
        margin-right: 4px;
        border-radius: 8px;
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 1px;
    }}

    QTabBar::tab:hover {{
        color: {palette.text_muted};
        background-color: {palette.surface_alt};
    }}

    QTabBar::tab:selected {{
        color: {palette.accent};
        background-color: {palette.accent_soft};
    }}

    QTableView {{
        background-color: transparent;
        border: none;
        gridline-color: transparent;
        color: {palette.text_muted};
        selection-background-color: {palette.accent_soft};
        selection-color: {palette.text};
        alternate-background-color: {palette.surface};
    }}

    QTableView::item {{
        border: none;
        padding: 2px 4px;
    }}

    QTableView::item:selected {{
        background-color: {palette.accent_soft};
        color: {palette.text};
    }}

    QHeaderView {{
        background-color: transparent;
        border: none;
    }}

    QHeaderView::section {{
        background-color: transparent;
        color: {palette.text_dim};
        border: none;
        border-bottom: 1px solid {palette.stroke};
        padding: 8px 6px;
        font-size: 10px;
        font-weight: 600;
        letter-spacing: 1px;
    }}

    QScrollBar:vertical {{
        background: transparent;
        width: 10px;
        margin: 4px 2px 4px 2px;
    }}

    QScrollBar::handle:vertical {{
        background: {palette.stroke_strong};
        border-radius: 4px;
        min-height: 28px;
    }}

    QScrollBar::handle:vertical:hover {{
        background: {palette.text_dim};
    }}

    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
        background: transparent;
    }}

    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        background: transparent;
    }}

    QScrollBar:horizontal {{
        background: transparent;
        height: 10px;
        margin: 2px 4px;
    }}

    QScrollBar::handle:horizontal {{
        background: {palette.stroke_strong};
        border-radius: 4px;
        min-width: 28px;
    }}

    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0px;
        background: transparent;
    }}

    QProgressBar {{
        background-color: {palette.surface_alt};
        border: 1px solid {palette.stroke};
        border-radius: 5px;
        text-align: center;
        color: {palette.text_muted};
        height: 8px;
    }}

    QProgressBar::chunk {{
        background-color: {palette.accent};
        border-radius: 4px;
    }}

    QMenu {{
        background-color: {palette.surface_raised};
        border: 1px solid {palette.stroke_strong};
        border-radius: 10px;
        padding: 6px;
        color: {palette.text};
    }}

    QMenu::item {{
        padding: 7px 22px 7px 14px;
        border-radius: 6px;
    }}

    QMenu::item:selected {{
        background-color: {palette.accent_soft};
        color: {palette.accent};
    }}

    QMenu::separator {{
        height: 1px;
        background-color: {palette.stroke};
        margin: 5px 8px;
    }}

    QSplitter::handle {{
        background-color: transparent;
        width: 14px;
    }}

    QSplitter::handle:hover {{
        background-color: {palette.stroke};
    }}

    QScrollArea {{
        border: none;
        background: transparent;
    }}

    QStatusBar {{
        background-color: {palette.surface};
        color: {palette.text_dim};
        border-top: 1px solid {palette.stroke};
    }}

    #Chip {{
        background-color: {palette.surface_alt};
        border: 1px solid {palette.stroke};
        border-radius: 8px;
        padding: 3px 9px;
        color: {palette.text_muted};
        font-size: 10px;
        font-weight: 600;
        letter-spacing: 1px;
    }}

    #ChipAccent {{
        background-color: {palette.accent_soft};
        border: 1px solid {palette.accent_dim};
        border-radius: 8px;
        padding: 3px 9px;
        color: {palette.accent};
        font-size: 10px;
        font-weight: 600;
        letter-spacing: 1px;
    }}

    #ChipWarning {{
        background-color: {palette.warning_soft};
        border: 1px solid {palette.warning};
        border-radius: 8px;
        padding: 3px 9px;
        color: {palette.warning};
        font-size: 10px;
        font-weight: 600;
        letter-spacing: 1px;
    }}

    #TileTitle {{
        color: {palette.text_dim};
        font-size: 9px;
        font-weight: 700;
        letter-spacing: 1px;
    }}

    #TileValue {{
        color: {palette.text};
        font-size: 16px;
        font-weight: 600;
    }}

    #TileSub {{
        color: {palette.text_dim};
        font-size: 10px;
    }}
    """


def build_palette(palette: Palette = PALETTE) -> QPalette:
    qt_palette = QPalette()
    qt_palette.setColor(QPalette.ColorRole.Window, QColor(palette.base))
    qt_palette.setColor(QPalette.ColorRole.WindowText, QColor(palette.text))
    qt_palette.setColor(QPalette.ColorRole.Base, QColor(palette.surface))
    qt_palette.setColor(QPalette.ColorRole.AlternateBase, QColor(palette.surface_alt))
    qt_palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(palette.surface_raised))
    qt_palette.setColor(QPalette.ColorRole.ToolTipText, QColor(palette.text))
    qt_palette.setColor(QPalette.ColorRole.Text, QColor(palette.text))
    qt_palette.setColor(QPalette.ColorRole.Button, QColor(palette.surface_alt))
    qt_palette.setColor(QPalette.ColorRole.ButtonText, QColor(palette.text))
    qt_palette.setColor(QPalette.ColorRole.BrightText, QColor(palette.accent))
    qt_palette.setColor(QPalette.ColorRole.Highlight, QColor(palette.accent_soft))
    qt_palette.setColor(QPalette.ColorRole.HighlightedText, QColor(palette.text))
    qt_palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(palette.text_dim))
    qt_palette.setColor(QPalette.ColorRole.Link, QColor(palette.accent))
    qt_palette.setColor(QPalette.ColorRole.LinkVisited, QColor(palette.accent_alt))
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        qt_palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(palette.text_dim))
    return qt_palette


def app_font(size: int = 10) -> QFont:
    font = QFont(resolve_font_family())
    font.setPointSize(size)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return font


def apply_theme(app: QApplication) -> None:
    """Install the design system on the running QApplication."""

    app.setStyle("Fusion")
    app.setPalette(build_palette())
    app.setFont(app_font())
    app.setStyleSheet(build_stylesheet())
    app.setEffectEnabled(Qt.UIEffect.UI_AnimateCombo, True)
    app.setEffectEnabled(Qt.UIEffect.UI_AnimateTooltip, True)


def attach_fade(widget: QWidget, start: float = 0.0, end: float = 1.0, duration: int = 260) -> QPropertyAnimation:
    """Fade a widget in (used for panels and cards on first show)."""

    from PySide6.QtWidgets import QGraphicsOpacityEffect

    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    animation = QPropertyAnimation(effect, b"opacity", widget)
    animation.setDuration(duration)
    animation.setStartValue(start)
    animation.setEndValue(end)
    animation.setEasingCurve(QEasingCurve.Type.OutCubic)
    animation.finished.connect(lambda: widget.setGraphicsEffect(None))
    animation.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
    return animation


def repolish(widget: QWidget) -> None:
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    widget.update()


def set_state_property(widget: QWidget, name: str, value: str) -> None:
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    repolish(widget)


def mix(color_a: QColor, color_b: QColor, ratio: float) -> QColor:
    ratio = max(0.0, min(1.0, ratio))
    return QColor(
        int(color_a.red() + (color_b.red() - color_a.red()) * ratio),
        int(color_a.green() + (color_b.green() - color_a.green()) * ratio),
        int(color_a.blue() + (color_b.blue() - color_a.blue()) * ratio),
        int(color_a.alpha() + (color_b.alpha() - color_a.alpha()) * ratio),
    )


def alpha(color: QColor, opacity: float) -> QColor:
    result = QColor(color)
    result.setAlphaF(max(0.0, min(1.0, opacity)))
    return result
