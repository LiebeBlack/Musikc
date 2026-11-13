"""Main window: header, now playing, queue, library, hardware and telemetry."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QBrush,
    QCloseEvent,
    QColor,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QIcon,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import (
    APP_NAME,
    APP_VERSION,
    SUPPORTED_EXTENSIONS,
    format_bytes,
    get_logger,
    humanise_rate,
    SettingsStore,
)
from ..core.decoders.registry import probe_file
from ..core.devices import DeviceInfo, host_rank
from ..core.library import discover_files
from ..core.playlist import PlaybackController, Playlist, RepeatMode
from ..core.telemetry import TelemetrySnapshot
from .icons import logo_path
from .qt_bridge import QtBridge
from .theme import PALETTE, alpha, attach_fade
from .widgets.base import Card, Chip, ChipRow, ElidedLabel, IconButton, InfoRow, SectionLabel
from .widgets.badge import BitPerfectBadge, StatusPill
from .widgets.device_panel import DevicePanel
from .widgets.library_view import LibraryView
from .widgets.now_playing import NowPlayingCard
from .widgets.playlist_view import PlaylistView
from .widgets.telemetry_panel import TelemetryPanel

LOGGER = get_logger("ui.main")

_APP_ICON_CACHE: Dict[int, QIcon] = {}


def build_app_icon(size: int = 256) -> QIcon:
    """Render the application brand mark at runtime (no binary assets needed)."""

    cached = _APP_ICON_CACHE.get(size)
    if cached is not None:
        return cached
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    rect = QRectF(size * 0.08, size * 0.08, size * 0.84, size * 0.84)
    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    gradient.setColorAt(0.0, QColor(PALETTE.accent))
    gradient.setColorAt(1.0, QColor(PALETTE.accent_alt))
    diamond, wave = logo_path(rect)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(PALETTE.base))
    painter.drawRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
    pen = QPen(QBrush(gradient), size * 0.035)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(diamond)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(gradient)
    painter.drawPath(wave)
    painter.end()
    icon = QIcon(pixmap)
    _APP_ICON_CACHE[size] = icon
    return icon


def clear_icon_cache() -> None:
    """Drop cached QIcons while a QGuiApplication is still alive.

    Destroying them after the application is gone crashes the interpreter at
    shutdown, which is what broke the Linux CI run.
    """

    _APP_ICON_CACHE.clear()


class LogoMark(QWidget):
    """Vector brand mark."""

    def __init__(self, parent: Optional[QWidget] = None, size: int = 40) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(2, 2, self.width() - 4, self.height() - 4)
        diamond, wave = logo_path(rect)
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0.0, QColor(PALETTE.accent))
        gradient.setColorAt(1.0, QColor(PALETTE.accent_alt))
        pen = QPen(QBrush(gradient), 1.6)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(alpha(QColor(PALETTE.accent), 0.06))
        painter.drawPath(diamond)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawPath(wave)
        painter.end()


class FooterStrip(QFrame):
    """Slim live strip pinned to the bottom of the window.

    Only the values that have no other home are shown here: the engine state
    lives in the now-playing chip, the DSP attestation in the session report.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("FooterSurface")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 6, 18, 6)
        layout.setSpacing(16)

        self.clock_label = self._make_pair("RELOJ", "—")
        self.depth_label = self._make_pair("PROFUNDIDAD", "—")
        self.buffer_label = self._make_pair("BÚFER", "—")
        self.latency_label = self._make_pair("LATENCIA", "—")
        self.xrun_label = self._make_pair("CORTES", "0")
        layout.addWidget(self.clock_label[0])
        layout.addWidget(self.depth_label[0])
        layout.addWidget(self.buffer_label[0])
        layout.addWidget(self.latency_label[0])
        layout.addWidget(self.xrun_label[0])
        layout.addStretch(1)

        self._xrun_color = ""
        self.message_label = ElidedLabel("")
        self.message_label.setObjectName("Hint")
        self.message_label.setMinimumWidth(160)
        layout.addWidget(self.message_label, 1)
        self._message_timer = QTimer(self)
        self._message_timer.setSingleShot(True)
        self._message_timer.timeout.connect(lambda: self.message_label.setText(""))

    def _make_pair(self, name: str, value: str):
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        title = QLabel(name)
        title.setObjectName("TileTitle")
        value_label = QLabel(value)
        value_label.setObjectName("TileSub")
        row.addWidget(title)
        row.addWidget(value_label)
        container.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        return container, value_label

    def show_message(self, message: str, milliseconds: int = 8000) -> None:
        self.message_label.setText(message)
        self._message_timer.start(max(1000, milliseconds))

    def update_from(self, snapshot: TelemetrySnapshot) -> None:
        clock = snapshot.hardware_clock or snapshot.source_rate
        self.clock_label[1].setText(humanise_rate(clock) if clock else "—")
        depth = snapshot.hardware_bits or snapshot.source_bits
        self.depth_label[1].setText(f"{depth}-bit" if depth else "—")
        self.buffer_label[1].setText(f"{snapshot.buffer_ms:.0f} ms")
        self.latency_label[1].setText(f"{snapshot.latency_ms:.1f} ms")
        xruns = snapshot.xruns
        self.xrun_label[1].setText(str(xruns))
        color_key = "danger" if xruns else "success"
        if color_key != self._xrun_color:
            # setStyleSheet() re-evaluates the stylesheet: apply it on transitions only.
            self._xrun_color = color_key
            self.xrun_label[1].setStyleSheet(
                f"color: {PALETTE.danger};" if xruns else f"color: {PALETTE.success};"
            )


class SessionPanel(QWidget):
    """Technical metadata and integrity verdict for the current stream.

    Clock, depth, driver and scheduling are already reported by the header and
    the hardware tab, so this panel only adds what is unique to the session:
    the source metadata, the byte count and the integrity proof.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        card = Card(spacing=4)
        card.body.setContentsMargins(16, 14, 16, 14)
        card.body.addWidget(SectionLabel("Metadatos de origen"))
        self.rows: Dict[str, InfoRow] = {
            "path": InfoRow("Archivo", "—"),
            "container": InfoRow("Contenedor", "—"),
            "codec": InfoRow("Códec", "—"),
            "source": InfoRow("Formato de origen", "—"),
            "transport": InfoRow("Transporte del DAC", "—"),
            "bytes": InfoRow("Bytes enviados al DAC", "—"),
            "digest": InfoRow("Huella de salida", "—"),
            "verdict": InfoRow("Veredicto de integridad", "—"),
        }
        for row in self.rows.values():
            card.body.addWidget(row)
        layout.addWidget(card)

        policy = Card(spacing=6)
        policy.body.setContentsMargins(16, 14, 16, 14)
        policy.body.addWidget(SectionLabel("Política de procesamiento"))
        text = QLabel(
            "True HQ Audio no aplica ganancia, normalización, limitación, ecualización, "
            "tramado ni remuestreo. La única transformación permitida es cambiar el "
            "contenedor sin pérdida (desplazando los bits a una palabra de muestra más amplia), y queda registrada aquí."
        )
        text.setWordWrap(True)
        text.setObjectName("Hint")
        policy.body.addWidget(text)
        self.chip_row = ChipRow()
        for label in ("SIN GANANCIA", "SIN LIMITADOR", "SIN EQ", "SIN REMUESTREO", "SIN TRAMADO"):
            self.chip_row.add_chip(label)
        policy.body.addWidget(self.chip_row)
        layout.addWidget(policy)
        layout.addStretch(1)

    @staticmethod
    def _verdict(snapshot: TelemetrySnapshot) -> tuple[str, str]:
        if snapshot.bit_perfect:
            return ("BIT-PERFECT · DoP" if snapshot.is_dsd else "BIT-PERFECT"), PALETTE.accent
        if snapshot.state in ("idle", "stopped", "closed", "opening", "error"):
            return "EN ESPERA", PALETTE.text_muted
        reason = snapshot.bit_perfect_reason or "DEGRADADO"
        return reason[:64], PALETTE.warning

    def update_from(self, snapshot: TelemetrySnapshot) -> None:
        self.rows["path"].set_value(Path(snapshot.track_path).name if snapshot.track_path else "—")
        self.rows["container"].set_value(snapshot.track_container or "—")
        self.rows["codec"].set_value(snapshot.track_codec or "—")
        self.rows["source"].set_value(snapshot.source_format or "—")
        self.rows["transport"].set_value(snapshot.hardware_format or "—")
        self.rows["bytes"].set_value(format_bytes(snapshot.integrity_bytes_sent))
        self.rows["digest"].set_value(f"BLAKE2b {snapshot.integrity_digest}" if snapshot.integrity_digest else "—")
        verdict, color = self._verdict(snapshot)
        self.rows["verdict"].set_value(verdict)
        self.rows["verdict"].set_value_color(QColor(color))


class PreferencesDialog(QDialog):
    """Engine buffer configuration (never touches the signal)."""

    def __init__(self, settings: SettingsStore, engine_config, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferencias del motor")
        self.setMinimumWidth(430)
        self._settings = settings
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(14)
        form = QFormLayout()
        form.setSpacing(10)
        self.device_buffer = QSpinBox()
        self.device_buffer.setRange(8, 500)
        self.device_buffer.setSuffix(" ms")
        self.device_buffer.setValue(int(getattr(engine_config, "buffer_ms", 24)))
        form.addRow("Tamaño solicitado al controlador", self.device_buffer)
        self.ring_buffer = QSpinBox()
        self.ring_buffer.setRange(200, 4000)
        self.ring_buffer.setSingleStep(100)
        self.ring_buffer.setSuffix(" ms")
        self.ring_buffer.setValue(int(getattr(engine_config, "ring_ms", 1200)))
        form.addRow("Capacidad del búfer circular", self.ring_buffer)
        self.period = QSpinBox()
        self.period.setRange(0, 8192)
        self.period.setSingleStep(64)
        self.period.setSuffix(" tramas")
        self.period.setSpecialValueText("valor predeterminado")
        self.period.setValue(int(getattr(engine_config, "period_frames", 0)))
        form.addRow("Tamaño del período", self.period)
        self.allow_degraded = QCheckBox("Permitir conversión con pérdida si el DAC no admite la profundidad de origen")
        self.allow_degraded.setChecked(bool(getattr(engine_config, "allow_degraded", False)))
        form.addRow("", self.allow_degraded)
        layout.addLayout(form)
        note = QLabel(
            "Estos ajustes solo afectan al búfer y a la negociación. No modifican las muestras: "
            "el motor rechaza las conversiones con pérdida salvo que se permitan expresamente arriba."
        )
        note.setWordWrap(True)
        note.setObjectName("Hint")
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> Dict[str, Any]:
        return {
            "buffer_ms": self.device_buffer.value(),
            "ring_ms": self.ring_buffer.value(),
            "period_frames": self.period.value(),
            "allow_degraded": self.allow_degraded.isChecked(),
        }


class MainWindow(QMainWindow):
    """Application shell."""

    def __init__(
        self,
        engine,
        controller: PlaybackController,
        library,
        settings: SettingsStore,
        bridge: Optional[QtBridge] = None,
    ) -> None:
        super().__init__()
        self._engine = engine
        self._controller = controller
        self._library = library
        self._settings = settings
        self._bridge = bridge or QtBridge(engine.bus, self)
        self._devices: List[DeviceInfo] = []
        self._syncing = False
        self._snapshot = engine.snapshot()
        self._xrun_override_until = 0.0

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setWindowIcon(build_app_icon())
        self.setMinimumSize(920, 620)
        self.setAcceptDrops(True)
        self._minimum_size_final = False
        self._screen_hooked = False
        self._build_ui()
        self._apply_minimum_size()
        self._connect_bridge()
        self._install_shortcuts()
        self._start_polling()
        self._apply_saved_modes()
        self._apply_snapshot(engine.snapshot())
        self.device_combo.blockSignals(True)
        if self.device_combo.count() == 0:
            self.device_combo.addItem("Detectando dispositivos de salida…", -1)
        self.device_combo.blockSignals(False)

    # ------------------------------------------------------------------- build
    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("RootSurface")
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.header = self._build_header()
        root_layout.addWidget(self.header)

        content = QWidget()
        content.setObjectName("ContentSurface")
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(14, 12, 14, 10)
        content_layout.setSpacing(12)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(8)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)
        self.now_playing = NowPlayingCard()
        self.now_playing.playPauseRequested.connect(self._controller.toggle)
        self.now_playing.previousRequested.connect(self._controller.previous)
        self.now_playing.nextRequested.connect(self._controller.next)
        self.now_playing.stopRequested.connect(self._controller.stop)
        self.now_playing.seekRequested.connect(self._on_seek)
        self.now_playing.repeatRequested.connect(self._on_repeat)
        self.now_playing.shuffleRequested.connect(self._on_shuffle)
        left_layout.addWidget(self.now_playing)

        queue_card = Card(spacing=6)
        queue_card.body.setContentsMargins(14, 12, 14, 12)
        self.queue_card = queue_card
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        header.addWidget(SectionLabel("Cola de reproducción"))
        self.queue_chip = Chip("0 PISTAS", "chip")
        header.addWidget(self.queue_chip)
        header.addStretch(1)
        # "Abrir archivos" lives in the header: adding to the queue is the same
        # action, so the card only offers the queue-specific control.
        self.queue_clear_button = QPushButton("Vaciar")
        self.queue_clear_button.setEnabled(len(self._controller.playlist) > 0)
        self.queue_clear_button.clicked.connect(self._on_clear_queue)
        header.addWidget(self.queue_clear_button)
        queue_card.body.addLayout(header)
        self.playlist_view = PlaylistView(self._controller.playlist, toolbar=False)
        self.playlist_view.playRequested.connect(self._controller.play_index)
        self.playlist_view.removeRequested.connect(self._on_remove_track)
        self.playlist_view.clearRequested.connect(self._on_clear_queue)
        self.playlist_view.addFilesRequested.connect(self._open_files)
        queue_card.body.addWidget(self.playlist_view, 1)
        left_layout.addWidget(queue_card, 1)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setDrawBase(False)

        self.library_view = LibraryView(self._library)
        self.library_view.playRequested.connect(self._on_library_play)
        self.library_view.queueRequested.connect(self._on_library_queue)
        self.library_view.foldersChanged.connect(self._on_folders_changed)
        self.library_view.statusMessage.connect(self._show_status)
        self.tabs.addTab(self.library_view, "BIBLIOTECA")

        hardware = QWidget()
        hardware_layout = QVBoxLayout(hardware)
        hardware_layout.setContentsMargins(0, 8, 0, 0)
        hardware_layout.setSpacing(12)
        self.device_panel = DevicePanel(self._engine.devices)
        self.telemetry_panel = TelemetryPanel()
        hardware_layout.addWidget(self.device_panel)
        hardware_layout.addWidget(self.telemetry_panel)
        hardware_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(hardware)
        # Narrow windows must still be able to reach the right edge of a row.
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.tabs.addTab(scroll, "HARDWARE")

        self.session_panel = SessionPanel()
        session_scroll = QScrollArea()
        session_scroll.setWidgetResizable(True)
        session_scroll.setWidget(self.session_panel)
        session_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.tabs.addTab(session_scroll, "SESIÓN")

        right_layout.addWidget(self.tabs, 1)
        # A splitter shrinks its children below their own layout hints, which
        # clips buttons: pin each panel to what it actually needs.
        self._panel_minimums = (
            max(400, left.minimumSizeHint().width()),
            max(330, right.minimumSizeHint().width()),
        )
        left.setMinimumWidth(self._panel_minimums[0])
        right.setMinimumWidth(self._panel_minimums[1])
        self.left_panel = left
        self.right_panel = right
        self.splitter = splitter
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 55)
        splitter.setStretchFactor(1, 45)
        splitter.setSizes([790, 560])
        content_layout.addWidget(splitter, 1)
        root_layout.addWidget(content, 1)

        self.footer = FooterStrip()
        root_layout.addWidget(self.footer)

    def _splitter_room(self) -> int:
        content = self.centralWidget().layout().itemAt(1).widget()
        margins = content.layout().contentsMargins()
        return self.splitter.handleWidth() + margins.left() + margins.right()

    def _ideal_minimum_size(self) -> QSize:
        """What the header and both panels need with every control legible."""

        left, right = self._panel_minimums
        width = max(
            920,
            left + right + self._splitter_room(),
            self.header.minimumSizeHint().width(),
        )
        return QSize(width, 620)

    def _minimum_for(self, available: QSize) -> QSize:
        """Ideal minimum, capped by the usable desktop."""

        ideal = self._ideal_minimum_size()
        return QSize(min(ideal.width(), available.width()), min(ideal.height(), available.height()))

    def _apply_minimum_size(self, available: Optional[QSize] = None) -> None:
        """Fit the layout minimum inside the desktop instead of off-screen.

        On a 150 % scaled laptop the two panels can need more width than the
        screen offers. In that case the window is capped to the desktop and the
        splitter gives space back, so nothing is pushed out of reach.
        """

        if available is None:
            screen = self.screen()
            if screen is not None:
                geometry = screen.availableGeometry()
                available = QSize(geometry.width(), geometry.height())
        ideal = self._ideal_minimum_size()
        if available is None or available.width() <= 0 or available.height() <= 0:
            self.setMinimumSize(ideal)
            return
        target = self._minimum_for(available)
        left_need, right_need = self._panel_minimums
        if target.width() < ideal.width():
            budget = max(0, target.width() - self._splitter_room())
            right = max(280, min(right_need, budget * 45 // 100))
            left = max(300, min(left_need, budget - right))
        else:
            left, right = left_need, right_need
        # Only touch the layout when something actually changes: this runs on
        # every screen and font change.
        if self.left_panel.minimumWidth() != left:
            self.left_panel.setMinimumWidth(left)
        if self.right_panel.minimumWidth() != right:
            self.right_panel.setMinimumWidth(right)
        size = QSize(max(320, target.width()), max(320, target.height()))
        if size != self.minimumSize():
            self.setMinimumSize(size)

    def _on_screen_changed(self, _screen=None) -> None:
        # Moving the window to another monitor must re-fit the minimum.
        self._apply_minimum_size()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if not self._minimum_size_final:
            # Font metrics are final only after the first polish pass.
            self._minimum_size_final = True
            self._apply_minimum_size()
            handle = self.windowHandle()
            if handle is not None and not self._screen_hooked:
                handle.screenChanged.connect(self._on_screen_changed)
                self._screen_hooked = True

    def changeEvent(self, event) -> None:  # type: ignore[override]
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange and getattr(self, "_minimum_size_final", False):
            if hasattr(self, "_panel_minimums"):
                self._apply_minimum_size()

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("HeaderSurface")
        outer = QVBoxLayout(header)
        outer.setContentsMargins(18, 12, 18, 12)
        outer.setSpacing(10)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(12)
        logo = LogoMark(size=38)
        top.addWidget(logo)
        titles = QVBoxLayout()
        titles.setContentsMargins(0, 0, 0, 0)
        titles.setSpacing(0)
        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.setSpacing(8)
        name = QLabel("TRUE HQ AUDIO")
        name.setObjectName("AppTitle")
        name_row.addWidget(name)
        self.version_label = QLabel(f"v{APP_VERSION}")
        self.version_label.setObjectName("AppVersion")
        self.version_label.setToolTip(f"Versión {APP_VERSION}")
        name_row.addWidget(self.version_label)
        name_row.addStretch(1)
        subtitle = ElidedLabel("REPRODUCCIÓN DE REFERENCIA · BIT-PERFECT")
        subtitle.setObjectName("AppSubtitle")
        subtitle.setToolTip("Reproducción de referencia con ruta de señal bit-perfect")
        titles.addLayout(name_row)
        titles.addWidget(subtitle)
        top.addLayout(titles)
        top.addStretch(1)
        self.badge = BitPerfectBadge()
        top.addWidget(self.badge)
        top.addSpacing(8)
        self.open_button = QPushButton("Abrir archivos")
        self.open_button.setObjectName("Primary")
        self.open_button.clicked.connect(self._open_files)
        top.addWidget(self.open_button)
        self.menu_button = IconButton("settings", "Menú de la aplicación", size=34, icon_size=16)
        self.menu_button.clicked.connect(self._show_menu)
        top.addWidget(self.menu_button)
        outer.addLayout(top)

        second = QHBoxLayout()
        second.setContentsMargins(0, 0, 0, 0)
        second.setSpacing(10)
        device_label = QLabel("SALIDA")
        device_label.setObjectName("TileTitle")
        second.addWidget(device_label)
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(0)
        self.device_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.device_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.device_combo.setMinimumContentsLength(24)
        self.device_combo.currentIndexChanged.connect(self._on_device_combo_changed)
        second.addWidget(self.device_combo, 1)
        self.exclusive_check = QCheckBox("Exclusivo")
        self.exclusive_check.setChecked(True)
        self.exclusive_check.setToolTip("Modo exclusivo WASAPI/WDM-KS: se omite el mezclador del sistema operativo")
        self.exclusive_check.toggled.connect(self._on_exclusive_toggled)
        second.addWidget(self.exclusive_check)
        self.driver_pill = StatusPill("—", accent=True)
        second.addWidget(self.driver_pill)
        self.clock_pill = StatusPill("—")
        second.addWidget(self.clock_pill)
        self.refresh_button = IconButton("refresh", "Volver a buscar dispositivos de audio (F5)", size=34, icon_size=16)
        self.refresh_button.clicked.connect(self._refresh_devices)
        second.addWidget(self.refresh_button)
        outer.addLayout(second)
        return header

    # ------------------------------------------------------------------ wiring
    def _connect_bridge(self) -> None:
        self._bridge.stateChanged.connect(self._on_engine_state)
        self._bridge.errorRaised.connect(self._on_engine_error)
        self._bridge.trackChanged.connect(self._on_track_changed)
        self._bridge.streamOpened.connect(self._on_stream_opened)
        self._bridge.underrun.connect(self._on_underrun)
        self._bridge.deviceLost.connect(self._on_device_lost)
        self._bridge.deviceRestored.connect(self._on_device_restored)
        self._bridge.playlistChanged.connect(self._on_playlist_changed)
        self._bridge.playlistTrack.connect(self._on_playlist_track)

    def _install_shortcuts(self) -> None:
        bindings = {
            "Space": self._controller.toggle,
            "Ctrl+O": self._open_files,
            "Ctrl+Shift+O": self._open_folder,
            "Ctrl+.": self._controller.stop,
            "Ctrl+Right": lambda: self._engine.seek_relative(5.0),
            "Ctrl+Left": lambda: self._engine.seek_relative(-5.0),
            "Alt+Right": self._controller.next,
            "Alt+Left": self._controller.previous,
            "F5": self._refresh_devices,
            "Ctrl+L": lambda: self.tabs.setCurrentWidget(self.library_view),
            "Ctrl+Q": self.close,
        }
        for sequence, callback in bindings.items():
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(callback)

    def _start_polling(self) -> None:
        # Live audio is refreshed at the engine cadence; an idle window wakes up
        # once per second instead of repeating the same verification forever.
        self._poll_active_ms = max(200, int(self._settings.settings.telemetry_poll_ms))
        self._poll_idle_ms = max(1000, self._poll_active_ms * 4)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(self._poll_idle_ms)
        self._poll_timer.timeout.connect(self._on_poll)
        self._poll_timer.start()

    def _apply_saved_modes(self) -> None:
        settings = self._settings.settings
        repeat = RepeatMode.parse(settings.repeat_mode)
        self._controller.playlist.set_repeat(repeat)
        self._controller.playlist.set_shuffle(settings.shuffle)
        self.playlist_view.refresh()
        self.now_playing.set_modes(repeat.value, settings.shuffle)
        self._library.set_folders(settings.library_folders)
        self._library.load_cache()
        self.library_view.apply_filter()

    # --------------------------------------------------------------- startup
    def restore_state(self) -> None:
        settings = self._settings.settings
        self._refresh_devices(select_name=settings.output_device_name, select_host=settings.output_host_api)
        self.exclusive_check.blockSignals(True)
        self.exclusive_check.setChecked(bool(settings.exclusive_mode))
        self.exclusive_check.blockSignals(False)
        self.device_panel.set_exclusive(settings.exclusive_mode)
        self._restore_playlist()
        self._engine.start()
        width = max(920, int(settings.window_width))
        height = max(620, int(settings.window_height))
        screen = self.screen()
        if screen is not None:
            available = screen.availableGeometry()
            width = min(width, max(920, available.width() - 32))
            height = min(height, max(620, available.height() - 48))
        self.resize(width, height)
        if settings.window_maximised:
            self.showMaximized()
        attach_fade(self.now_playing, 0.0, 1.0, 320)

    def _restore_playlist(self) -> None:
        session_file = Path(self._settings.path).parent.parent / "playlists" / "last-session.json"
        if not session_file.exists():
            return
        try:
            playlist = Playlist.load(session_file)
        except (OSError, ValueError) as exc:
            LOGGER.warning("session playlist unreadable", extra={"error": str(exc)})
            return
        if not len(playlist):
            return
        self._controller.set_playlist(playlist)
        self.playlist_view.set_playlist(playlist)
        self.playlist_view.refresh()
        self._on_playlist_changed({"count": len(playlist)})

    # ------------------------------------------------------------------ devices
    def _refresh_devices(self, select_name: str = "", select_host: str = "") -> None:
        try:
            devices = self._engine.refresh_devices()
        except Exception as exc:  # pragma: no cover - backend failure
            LOGGER.exception("device refresh failed")
            self._show_status(f"Audio backend error: {exc}")
            return
        self._devices = sorted(
            devices,
            key=lambda device: (
                host_rank(device.host_kind),
                -device.dac_score,
                0 if device.is_default_output else 1,
                device.name.lower(),
            ),
        )
        self._syncing = True
        self.device_combo.clear()
        if not self._devices:
            self.device_combo.addItem("No se detectaron dispositivos de salida", -1)
            self._syncing = False
            self.device_panel.set_device(None)
            self.badge.set_state("error", "no hay salida de audio")
            return
        for device in self._devices:
            label = f"{device.name}   ·   {device.driver_label}   ·   {device.max_output_channels} ch"
            self.device_combo.addItem(label, device.index)
        target = 0
        if select_name:
            for position, device in enumerate(self._devices):
                if device.name == select_name and (not select_host or device.host_api_name == select_host):
                    target = position
                    break
        else:
            preferred = self._engine.devices.best_device(prefer_asio=True)
            if preferred is not None:
                for position, device in enumerate(self._devices):
                    if device.index == preferred.index:
                        target = position
                        break
        self.device_combo.setCurrentIndex(target)
        self._syncing = False
        self._on_device_combo_changed(target, announce=False)

    def _current_device(self) -> Optional[DeviceInfo]:
        index = self.device_combo.currentData()
        return next((device for device in self._devices if device.index == index), None)

    def _on_device_combo_changed(self, position: int, announce: bool = True) -> None:
        device = self._current_device()
        if device is None:
            return
        self.driver_pill.set_text(device.driver_label.upper(), accent=device.exclusive_capable)
        self.clock_pill.set_text(humanise_rate(int(device.default_samplerate)) if device.default_samplerate else "—")
        self.exclusive_check.setEnabled(device.exclusive_capable)
        if not device.exclusive_capable and self.exclusive_check.isChecked():
            self.exclusive_check.blockSignals(True)
            self.exclusive_check.setChecked(False)
            self.exclusive_check.blockSignals(False)
            self._show_status(
                f"{device.driver_label} no admite el modo exclusivo: el modo compartido no es bit-perfect"
            )
        self.device_panel.set_exclusive(self.exclusive_check.isChecked())
        self.device_panel.set_device(device)
        if self._syncing:
            return
        self._engine.set_device(device.index, self.exclusive_check.isChecked())
        self._settings.update(
            output_device_name=device.name,
            output_host_api=device.host_api_name,
            exclusive_mode=self.exclusive_check.isChecked(),
        )
        if announce:
            self._show_status(f"Salida: {device.name} · {device.driver_label}")

    def _on_exclusive_toggled(self, checked: bool) -> None:
        device = self._current_device()
        if device is None:
            return
        self._settings.update(exclusive_mode=checked)
        self.device_panel.set_exclusive(checked)
        self._engine.set_device(device.index, checked)
        self._show_status("Modo exclusivo activado" if checked else "Modo compartido: el mezclador del sistema podría remuestrear el audio")

    # -------------------------------------------------------------- transport
    def _on_seek(self, seconds: float) -> None:
        self._engine.seek(seconds)

    def _on_repeat(self) -> None:
        mode = self._controller.cycle_repeat()
        self.now_playing.set_modes(mode.value, self._controller.playlist.shuffle)
        self._settings.update(repeat_mode=mode.value)
        label = {"off": "desactivada", "all": "cola completa", "one": "pista"}.get(mode.value, mode.value)
        self._show_status(f"Repetición: {label}")

    def _on_shuffle(self) -> None:
        enabled = self._controller.toggle_shuffle()
        self.now_playing.set_modes(self._controller.playlist.repeat_mode.value, enabled)
        self._settings.update(shuffle=enabled)
        self._show_status("Reproducción aleatoria activada" if enabled else "Reproducción aleatoria desactivada")

    def _on_remove_track(self, index: int) -> None:
        self._controller.remove_index(index)
        self.playlist_view.refresh()

    def _on_clear_queue(self) -> None:
        self._controller.stop()
        self._controller.clear()
        self.playlist_view.refresh()
        self.now_playing.clear_track()

    # ------------------------------------------------------------------- files
    def _open_files(self) -> None:
        start = self._settings.settings.last_directory or str(Path.home())
        files, _filter = QFileDialog.getOpenFileNames(self, "Abrir archivos de audio", start, "Archivos de audio (" + " ".join(f"*{extension}" for extension in SUPPORTED_EXTENSIONS) + ");;Todos los archivos (*.*)")
        if not files:
            return
        paths = [Path(file) for file in files]
        self._settings.update(last_directory=str(paths[0].parent))
        before = len(self._controller.playlist)
        added = self._controller.add_paths(paths, probe_file)
        self.playlist_view.refresh()
        if added:
            self._controller.play_index(before)
            self.playlist_view.refresh()

    def _open_folder(self) -> None:
        start = self._settings.settings.last_directory or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Abrir carpeta", start)
        if not folder:
            return
        self._settings.update(last_directory=folder)
        paths = discover_files([Path(folder)])
        if not paths:
            self._show_status("No hay archivos de audio compatibles en esa carpeta")
            return
        before = len(self._controller.playlist)
        added = self._controller.add_paths(paths, probe_file)
        self.playlist_view.refresh()
        if added:
            self._controller.play_index(before)
            self.playlist_view.refresh()

    def _on_library_play(self, path: str) -> None:
        self._controller.play_path(path)
        self.playlist_view.refresh()
        self.tabs.setCurrentIndex(0)

    def _on_library_queue(self, paths: List[str]) -> None:
        added = self._controller.add_paths([Path(path) for path in paths], probe_file)
        self.playlist_view.refresh()
        self._show_status(f"Se añadieron {added} pista(s) a la cola")

    def _on_folders_changed(self, folders: List[str]) -> None:
        self._settings.update(library_folders=list(folders))

    # ------------------------------------------------------------------- menu
    def _show_menu(self) -> None:
        menu = QMenu(self)
        open_files = menu.addAction("Abrir archivos…")
        open_files.setShortcut(QKeySequence("Ctrl+O"))
        open_folder = menu.addAction("Abrir carpeta…")
        open_folder.setShortcut(QKeySequence("Ctrl+Shift+O"))
        add_library = menu.addAction("Añadir carpeta a la biblioteca…")
        menu.addSeparator()
        save_playlist = menu.addAction("Guardar cola…")
        load_playlist = menu.addAction("Cargar cola…")
        menu.addSeparator()
        preferences = menu.addAction("Preferencias del motor…")
        open_logs = menu.addAction("Abrir carpeta de registros")
        about = menu.addAction("Acerca de True HQ Audio")
        menu.addSeparator()
        quit_action = menu.addAction("Salir")
        quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        chosen = menu.exec(self.menu_button.mapToGlobal(self.menu_button.rect().bottomLeft()))
        if chosen is None:
            return
        if chosen == open_files:
            self._open_files()
        elif chosen == open_folder:
            self._open_folder()
        elif chosen == add_library:
            self.tabs.setCurrentWidget(self.library_view)
            self.library_view._choose_folder()
        elif chosen == save_playlist:
            self._save_playlist()
        elif chosen == load_playlist:
            self._load_playlist()
        elif chosen == preferences:
            self._show_preferences()
        elif chosen == open_logs:
            from ..config import log_directory

            QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_directory())))
        elif chosen == about:
            self._show_about()
        elif chosen == quit_action:
            self.close()

    def _save_playlist(self) -> None:
        start = str(Path(self._settings.settings.last_directory) / "queue.json")
        target, _filter = QFileDialog.getSaveFileName(self, "Guardar cola", start, "Cola de True HQ (*.json)")
        if not target:
            return
        try:
            self._controller.playlist.save(Path(target))
            self._show_status(f"Cola guardada en {target}")
        except OSError as exc:
            self._show_status(f"No se pudo guardar la cola: {exc}")

    def _load_playlist(self) -> None:
        start = str(Path(self._settings.settings.last_directory))
        target, _filter = QFileDialog.getOpenFileName(self, "Cargar cola", start, "Cola de True HQ (*.json)")
        if not target:
            return
        try:
            playlist = Playlist.load(Path(target))
        except (OSError, ValueError) as exc:
            self._show_status(f"No se pudo cargar la cola: {exc}")
            return
        self._controller.set_playlist(playlist)
        self.playlist_view.set_playlist(playlist)
        self.playlist_view.refresh()
        self.now_playing.set_modes(playlist.repeat_mode.value, playlist.shuffle)
        self._show_status(f"Cola cargada ({len(playlist)} pistas)")

    def _show_preferences(self) -> None:
        dialog = PreferencesDialog(self._settings, self._engine.config, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = dialog.values()
            self._engine.configure(**values)
            self._settings.update(
                target_buffer_ms=values["buffer_ms"],
                period_frames=values["period_frames"],
                allow_degraded_conversion=values["allow_degraded"],
            )
            self._show_status("Preferencias del motor aplicadas")

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            f"Acerca de {APP_NAME}",
            f"<b>{APP_NAME}</b> {APP_VERSION}<br><br>"
            "Reproductor de referencia bit-perfect para Windows.<br>"
            "Sin DSP. Sin remuestreo. Reloj directo del hardware.<br><br>"
            "Ruta de señal: decodificador → mapeador de contenedor sin pérdida → anillo sin bloqueos → "
            "controlador exclusivo → DAC.<br>"
            "Sin normalización de volumen, sin limitador, sin ecualizador, sin tramado.<br><br>"
            f"Python {sys.version.split()[0]} · PySide6",
        )

    # ------------------------------------------------------------------ bridge
    def _on_engine_state(self, state: str, payload: Dict[str, Any]) -> None:
        self.now_playing.set_playback_state(state)
        self.badge.set_state(self._badge_state_for(state), self._snapshot.bit_perfect_reason)

    def _on_engine_error(self, message: str, previous: str) -> None:
        LOGGER.error("engine error surfaced", extra={"message": message})
        self.badge.set_state("error", message)
        self.now_playing.set_playback_state("error")
        self._show_status(message)
        self.footer.update_from(self._engine.snapshot())

    def _on_track_changed(self, payload: Dict[str, Any]) -> None:
        self.now_playing.set_track(payload)
        self.now_playing.set_playback_state("opening")
        if not payload.get("gapless"):
            self.badge.set_state("off", "abriendo el flujo")
        self._settings.update(last_track=str(payload.get("path") or ""))
        snapshot = self._engine.snapshot()
        self.footer.update_from(snapshot)
        self.session_panel.update_from(snapshot)

    def _on_stream_opened(self, payload: Dict[str, Any]) -> None:
        driver = payload.get("driver") or "driver"
        rate = int(payload.get("rate") or 0)
        bits = int(payload.get("bits") or 0)
        latency = float(payload.get("latency_ms") or 0.0)
        self._show_status(f"{driver} fijado · {bits} bits {humanise_rate(rate)} · {latency:.1f} ms de latencia")

    def _on_underrun(self, count: int, missing_frames: int) -> None:
        self._show_status(f"Corte de búfer #{count} ({missing_frames} tramas): el motor insertó silencio")
        self._xrun_override_until = time.monotonic() + 1.5
        self.badge.set_state("degraded", f"{count} corte(s) de búfer insertaron silencio digital")

    def _on_device_lost(self, device: str, position: float) -> None:
        self.badge.set_state("error", f"DAC desconectado: {device}")
        self._show_status(f"DAC desconectado: {device}. Esperando a que vuelva…")

    def _on_device_restored(self, device: str, position: float) -> None:
        self._show_status(f"DAC reconectado: {device} — reanudando desde {position:.1f} s")
        self._refresh_devices()

    def _on_playlist_changed(self, payload: Dict[str, Any]) -> None:
        count = int(payload.get("count") or 0)
        self.queue_chip.setText(f"{count} PISTA{'S' if count != 1 else ''}")
        self.queue_clear_button.setEnabled(count > 0)
        self.playlist_view.refresh()
        current = int(payload.get("current", -1))
        if current >= 0:
            self.playlist_view.set_current(current)

    def _on_playlist_track(self, payload: Dict[str, Any]) -> None:
        index = int(payload.get("index", -1))
        if index >= 0:
            self.playlist_view.set_current(index)

    # -------------------------------------------------------------------- poll
    def _on_poll(self) -> None:
        if not self.isVisible() or self.isMinimized():
            return
        self._apply_snapshot(self._engine.snapshot())

    def _sync_poll_interval(self, state: str) -> None:
        timer = getattr(self, "_poll_timer", None)
        if timer is None:
            return
        interval = self._poll_active_ms if state in ("playing", "paused", "opening", "draining") else self._poll_idle_ms
        if timer.interval() != interval:
            timer.setInterval(interval)

    def _apply_snapshot(self, snapshot: TelemetrySnapshot) -> None:
        self._snapshot = snapshot
        current = self._controller.playlist.current_track
        if (
            snapshot.track_path
            and not self.now_playing._has_track
            and current is not None
            and Path(current.path) == Path(snapshot.track_path)
        ):
            self.now_playing.set_track(
                {
                    "path": snapshot.track_path,
                    "title": snapshot.track_title,
                    "artist": snapshot.track_artist,
                    "album": snapshot.track_album,
                    "container": snapshot.track_container,
                    "codec": snapshot.track_codec,
                    "resolution": snapshot.source_format,
                    "duration": snapshot.duration_seconds,
                    "notes": list(snapshot.track_notes),
                }
            )
        self.now_playing.set_position(snapshot.position_seconds)
        self.now_playing.set_playback_state(snapshot.state)
        self.badge.set_state(self._badge_state_for(snapshot.state), snapshot.bit_perfect_reason)
        self.footer.update_from(snapshot)
        if self.tabs.currentIndex() == 1:
            self.telemetry_panel.update_from(snapshot, self._engine.telemetry.history())
        elif self.tabs.currentIndex() == 2:
            self.session_panel.update_from(snapshot)
        if snapshot.device_name and snapshot.driver_label:
            self.driver_pill.set_text(snapshot.driver_label.upper(), accent=snapshot.exclusive)
        if snapshot.hardware_clock:
            self.clock_pill.set_text(humanise_rate(snapshot.hardware_clock), accent=snapshot.bit_perfect)
        self._sync_poll_interval(snapshot.state)

    def _badge_state_for(self, state: str) -> str:
        snapshot = self._snapshot
        if state in ("error", "no_device"):
            return "error"
        if state in ("idle", "closed", "stopped", "opening", "draining"):
            return "off"
        if time.monotonic() < self._xrun_override_until:
            return "degraded"
        if snapshot.bit_perfect:
            return "dop" if snapshot.is_dsd else "active"
        return "degraded"

    # ------------------------------------------------------------------- misc
    def _show_status(self, message: str) -> None:
        LOGGER.debug("status message", extra={"message": message})
        self.footer.show_message(message)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    event.acceptProposedAction()
                    return
        sup = getattr(super(), "dragEnterEvent", None)
        if sup:
            sup(event)

    def dropEvent(self, event: QDropEvent) -> None:  # type: ignore[override]
        paths: List[Path] = []
        for url in event.mimeData().urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if path.is_dir():
                paths.extend(discover_files([path]))
            elif path.suffix.lower() in SUPPORTED_EXTENSIONS:
                paths.append(path)
        if not paths:
            return
        before = len(self._controller.playlist)
        added = self._controller.add_paths(paths, probe_file)
        self.playlist_view.refresh()
        if added:
            self._show_status(f"{added} pista(s) añadida(s)")
            self._controller.play_index(before)
            self.playlist_view.refresh()
        event.acceptProposedAction()

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        LOGGER.info("window closing")
        self._settings.update(
            window_width=self.width(),
            window_height=self.height(),
            window_maximised=self.isMaximized(),
            repeat_mode=self._controller.playlist.repeat_mode.value,
            shuffle=self._controller.playlist.shuffle,
            library_folders=self._library.folders,
        )
        try:
            session = Path(self._settings.path).parent.parent / "playlists" / "last-session.json"
            session.parent.mkdir(parents=True, exist_ok=True)
            self._controller.playlist.save(session)
        except OSError as exc:
            LOGGER.warning("cannot persist session playlist", extra={"error": str(exc)})
        self._poll_timer.stop()
        self.library_view.shutdown()
        self.device_panel.shutdown()
        try:
            self._engine.shutdown()
        except Exception:  # pragma: no cover - shutdown must never block the close
            LOGGER.exception("engine shutdown failed")
        event.accept()
