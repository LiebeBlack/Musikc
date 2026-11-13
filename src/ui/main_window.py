"""Main window: header, now playing, queue, library, hardware and telemetry."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QRectF, Qt, QTimer, QUrl
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
from .widgets.base import Card, Chip, ElidedLabel, IconButton, InfoRow, MonoLabel, SectionLabel
from .widgets.badge import BitPerfectBadge, StatusPill
from .widgets.device_panel import DevicePanel
from .widgets.library_view import LibraryView
from .widgets.now_playing import NowPlayingCard
from .widgets.playlist_view import PlaylistView
from .widgets.telemetry_panel import TelemetryPanel

LOGGER = get_logger("ui.main")

AUDIO_FILTER = (
    "Audio files ("
    + " ".join(f"*{extension}" for extension in SUPPORTED_EXTENSIONS)
    + ");;All files (*.*)"
)

CONFIG_DIR = Path.home()


def build_app_icon(size: int = 256) -> QIcon:
    """Render the application icon at runtime (no binary assets needed)."""

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
    return QIcon(pixmap)


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
    """Slim telemetry strip pinned to the bottom of the window."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("FooterSurface")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 8, 18, 8)
        layout.setSpacing(16)

        self.state_label = QLabel("ENGINE OFFLINE")
        self.state_label.setObjectName("TileTitle")
        layout.addWidget(self.state_label)

        self.clock_label = self._make_pair("CLOCK", "—")
        self.depth_label = self._make_pair("DEPTH", "—")
        self.buffer_label = self._make_pair("BUFFER", "—")
        self.latency_label = self._make_pair("LATENCY", "—")
        self.xrun_label = self._make_pair("XRUNS", "0")
        layout.addWidget(self.clock_label[0])
        layout.addWidget(self.depth_label[0])
        layout.addWidget(self.buffer_label[0])
        layout.addWidget(self.latency_label[0])
        layout.addWidget(self.xrun_label[0])
        layout.addStretch(1)

        self.message_label = ElidedLabel("")
        self.message_label.setObjectName("Hint")
        self.message_label.setMinimumWidth(160)
        layout.addWidget(self.message_label, 1)
        self._message_timer = QTimer(self)
        self._message_timer.setSingleShot(True)
        self._message_timer.timeout.connect(lambda: self.message_label.setText(""))

        self.digest_label = MonoLabel("")
        self.digest_label.setObjectName("Hint")
        layout.addWidget(self.digest_label)
        self.dsp_chip = Chip("ZERO DSP", "accent")
        layout.addWidget(self.dsp_chip)
        self.priority_label = QLabel("prioridad estándar")
        self.priority_label.setObjectName("Hint")
        layout.addWidget(self.priority_label)

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
        self.state_label.setText(snapshot.state.upper())
        clock = snapshot.hardware_clock or snapshot.source_rate
        self.clock_label[1].setText(humanise_rate(clock) if clock else "—")
        depth = snapshot.hardware_bits or snapshot.source_bits
        self.depth_label[1].setText(f"{depth}-bit" if depth else "—")
        self.buffer_label[1].setText(f"{snapshot.buffer_ms:.0f} ms")
        self.latency_label[1].setText(f"{snapshot.latency_ms:.1f} ms")
        xruns = snapshot.xruns
        self.xrun_label[1].setText(str(xruns))
        self.xrun_label[1].setStyleSheet(
            f"color: {PALETTE.danger};" if xruns else f"color: {PALETTE.success};"
        )
        digest = snapshot.integrity_digest[:16]
        self.digest_label.setText(f"BLAKE2b {digest}…" if digest else "")
        self.priority_label.setText("MMCSS Pro Audio" if snapshot.priority_realtime else "prioridad estándar")
        self.dsp_chip.setText("ZERO DSP" if not snapshot.dsp_chain else f"{len(snapshot.dsp_chain)} DSP")


class SessionPanel(QWidget):
    """Technical metadata and integrity report for the current stream."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        card = Card(spacing=4)
        card.body.setContentsMargins(16, 14, 16, 14)
        card.body.addWidget(SectionLabel("Source metadata"))
        self.rows: Dict[str, InfoRow] = {
            "path": InfoRow("File", "—"),
            "container": InfoRow("Container", "—"),
            "codec": InfoRow("Codec", "—"),
            "source": InfoRow("Source format", "—"),
            "transport": InfoRow("DAC transport", "—"),
            "clock": InfoRow("Hardware clock", "—"),
            "depth": InfoRow("Hardware depth", "—"),
            "driver": InfoRow("Driver", "—"),
            "device": InfoRow("Device", "—"),
            "priority": InfoRow("Scheduling", "—"),
            "bytes": InfoRow("Bytes to DAC", "—"),
            "xruns": InfoRow("Underruns", "0"),
            "digest": InfoRow("Output digest", "—"),
            "verdict": InfoRow("Integrity verdict", "—"),
        }
        for row in self.rows.values():
            card.body.addWidget(row)
        layout.addWidget(card)

        policy = Card(spacing=6)
        policy.body.setContentsMargins(16, 14, 16, 14)
        policy.body.addWidget(SectionLabel("Processing policy"))
        text = QLabel(
            "True HQ Audio applies no gain, no normalisation, no limiter, no equaliser, "
            "no dither and no resampling. The only transformation allowed is a lossless "
            "container repack (bit shift into a wider sample word), and it is logged here."
        )
        text.setWordWrap(True)
        text.setObjectName("Hint")
        policy.body.addWidget(text)
        chips = QHBoxLayout()
        chips.setContentsMargins(0, 0, 0, 0)
        chips.setSpacing(6)
        for label in ("NO GAIN", "NO LIMITER", "NO EQ", "NO RESAMPLING", "NO DITHER"):
            chips.addWidget(Chip(label, "accent"))
        chips.addStretch(1)
        policy.body.addLayout(chips)
        layout.addWidget(policy)
        layout.addStretch(1)

    def update_from(self, snapshot: TelemetrySnapshot, engine) -> None:
        report = engine.integrity_report()
        self.rows["path"].set_value(Path(snapshot.track_path).name if snapshot.track_path else "—")
        self.rows["container"].set_value(snapshot.track_container or "—")
        self.rows["codec"].set_value(snapshot.track_codec or "—")
        self.rows["source"].set_value(snapshot.source_format or "—")
        transport = snapshot.hardware_format or "—"
        self.rows["transport"].set_value(transport)
        self.rows["clock"].set_value(humanise_rate(snapshot.hardware_clock) if snapshot.hardware_clock else "—")
        self.rows["depth"].set_value(f"{snapshot.hardware_bits}-bit" if snapshot.hardware_bits else "—")
        self.rows["driver"].set_value(snapshot.driver_label or "—")
        self.rows["device"].set_value(snapshot.device_name or "—")
        self.rows["priority"].set_value(snapshot.priority or "—")
        self.rows["bytes"].set_value(format_bytes(snapshot.integrity_bytes_sent))
        self.rows["xruns"].set_value(str(snapshot.xruns))
        self.rows["digest"].set_value(report.output_digest or "—")
        verdict_color = QColor(PALETTE.accent) if report.is_bit_perfect else QColor(PALETTE.warning)
        self.rows["verdict"].set_value(report.verdict.value)
        self.rows["verdict"].set_value_color(verdict_color)


class PreferencesDialog(QDialog):
    """Engine buffer configuration (never touches the signal)."""

    def __init__(self, settings: SettingsStore, engine_config, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Engine preferences")
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
        form.addRow("Driver buffer request", self.device_buffer)
        self.ring_buffer = QSpinBox()
        self.ring_buffer.setRange(200, 4000)
        self.ring_buffer.setSingleStep(100)
        self.ring_buffer.setSuffix(" ms")
        self.ring_buffer.setValue(int(getattr(engine_config, "ring_ms", 1200)))
        form.addRow("Ring buffer capacity", self.ring_buffer)
        self.period = QSpinBox()
        self.period.setRange(0, 8192)
        self.period.setSingleStep(64)
        self.period.setSuffix(" frames")
        self.period.setSpecialValueText("driver default")
        self.period.setValue(int(getattr(engine_config, "period_frames", 0)))
        form.addRow("Period size", self.period)
        self.allow_degraded = QCheckBox("Allow lossy conversion when the DAC cannot accept the source depth")
        self.allow_degraded.setChecked(bool(getattr(engine_config, "allow_degraded", False)))
        form.addRow("", self.allow_degraded)
        layout.addLayout(form)
        note = QLabel(
            "These settings only affect buffering and negotiation. They never change the samples: "
            "the engine refuses lossy conversions unless explicitly allowed above."
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
        self._last_state = "idle"
        self._drag_active = False
        self._xrun_override_until = 0.0

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setWindowIcon(build_app_icon())
        self.setMinimumSize(920, 620)
        self.setAcceptDrops(True)
        self._build_ui()
        self._connect_bridge()
        self._install_shortcuts()
        self._start_polling()
        self._apply_saved_modes()
        self._apply_snapshot(engine.snapshot())
        self.device_combo.blockSignals(True)
        if self.device_combo.count() == 0:
            self.device_combo.addItem("Detecting output devices…", -1)
        self.device_combo.blockSignals(False)

    # ------------------------------------------------------------------- build
    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("RootSurface")
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        root_layout.addWidget(self._build_header())

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
        header.addWidget(SectionLabel("Session queue"))
        self.queue_chip = Chip("0 TRACKS", "chip")
        header.addWidget(self.queue_chip)
        header.addStretch(1)
        self.queue_add_button = QPushButton("Add files")
        self.queue_add_button.clicked.connect(self._open_files)
        header.addWidget(self.queue_add_button)
        self.queue_clear_button = QPushButton("Clear")
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
        self.tabs.addTab(self.library_view, "LIBRARY")

        hardware = QWidget()
        hardware_layout = QVBoxLayout(hardware)
        hardware_layout.setContentsMargins(0, 8, 0, 0)
        hardware_layout.setSpacing(12)
        self.device_panel = DevicePanel(self._engine.devices)
        self.device_panel.deviceChanged.connect(self._on_device_panel_changed)
        self.device_panel.exclusiveToggled.connect(self._on_panel_exclusive_toggled)
        self.telemetry_panel = TelemetryPanel()
        hardware_layout.addWidget(self.device_panel)
        hardware_layout.addWidget(self.telemetry_panel)
        hardware_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(hardware)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tabs.addTab(scroll, "HARDWARE")

        self.session_panel = SessionPanel()
        session_scroll = QScrollArea()
        session_scroll.setWidgetResizable(True)
        session_scroll.setWidget(self.session_panel)
        session_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tabs.addTab(session_scroll, "SESSION")

        right_layout.addWidget(self.tabs, 1)
        left.setMinimumWidth(390)
        right.setMinimumWidth(280)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 58)
        splitter.setStretchFactor(1, 42)
        splitter.setSizes([820, 520])
        content_layout.addWidget(splitter, 1)
        root_layout.addWidget(content, 1)

        self.footer = FooterStrip()
        root_layout.addWidget(self.footer)

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
        name = QLabel("TRUE HQ AUDIO")
        name.setObjectName("AppTitle")
        subtitle = QLabel("BIT-PERFECT REFERENCE PLAYER")
        subtitle.setObjectName("AppSubtitle")
        titles.addWidget(name)
        titles.addWidget(subtitle)
        top.addLayout(titles)
        top.addStretch(1)
        self.badge = BitPerfectBadge()
        top.addWidget(self.badge)
        top.addSpacing(8)
        self.open_button = QPushButton("Open files")
        self.open_button.setObjectName("Primary")
        self.open_button.clicked.connect(self._open_files)
        top.addWidget(self.open_button)
        self.menu_button = IconButton("settings", "Application menu", size=34, icon_size=16)
        self.menu_button.clicked.connect(self._show_menu)
        top.addWidget(self.menu_button)
        outer.addLayout(top)

        second = QHBoxLayout()
        second.setContentsMargins(0, 0, 0, 0)
        second.setSpacing(10)
        device_label = QLabel("OUTPUT")
        device_label.setObjectName("TileTitle")
        second.addWidget(device_label)
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(0)
        self.device_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.device_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.device_combo.setMinimumContentsLength(24)
        self.device_combo.currentIndexChanged.connect(self._on_device_combo_changed)
        second.addWidget(self.device_combo, 1)
        self.exclusive_check = QCheckBox("Exclusive")
        self.exclusive_check.setChecked(True)
        self.exclusive_check.setToolTip("WASAPI/WDM-KS exclusive mode: the OS mixer is bypassed")
        self.exclusive_check.toggled.connect(self._on_exclusive_toggled)
        second.addWidget(self.exclusive_check)
        self.driver_pill = StatusPill("—", accent=True)
        second.addWidget(self.driver_pill)
        self.clock_pill = StatusPill("—")
        second.addWidget(self.clock_pill)
        self.refresh_button = IconButton("refresh", "Rescan audio devices (F5)", size=34, icon_size=16)
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
        self._bridge.integrityUpdated.connect(self._on_integrity)

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
        interval = max(60, int(self._settings.settings.telemetry_poll_ms))
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(interval)
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
            self.device_combo.addItem("No output device detected", -1)
            self._syncing = False
            self.badge.set_state("error", "no output device")
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
                f"{device.driver_label} cannot run exclusively: shared mode is not bit-perfect"
            )
        self.device_panel.select_device(device.name, device.host_api_name)
        self.device_panel.set_exclusive(self.exclusive_check.isChecked())
        if self._syncing:
            return
        self._engine.set_device(device.index, self.exclusive_check.isChecked())
        self._settings.update(
            output_device_name=device.name,
            output_host_api=device.host_api_name,
            exclusive_mode=self.exclusive_check.isChecked(),
        )
        if announce:
            self._show_status(f"Output: {device.name} · {device.driver_label}")

    def _on_device_panel_changed(self, device_index: int, exclusive: bool) -> None:
        self.exclusive_check.blockSignals(True)
        self.exclusive_check.setChecked(exclusive)
        self.exclusive_check.blockSignals(False)
        device = next((device for device in self._devices if device.index == device_index), None)
        if device is None:
            return
        for position, candidate in enumerate(self._devices):
            if candidate.index == device_index:
                self.device_combo.blockSignals(True)
                self.device_combo.setCurrentIndex(position)
                self.device_combo.blockSignals(False)
                break
        self._engine.set_device(device_index, exclusive)
        self._settings.update(output_device_name=device.name, output_host_api=device.host_api_name, exclusive_mode=exclusive)

    def _on_exclusive_toggled(self, checked: bool) -> None:
        device = self._current_device()
        if device is None:
            return
        self._settings.update(exclusive_mode=checked)
        self.device_panel.set_exclusive(checked)
        self._engine.set_device(device.index, checked)
        self._show_status("Exclusive mode enabled" if checked else "Shared mode: the OS mixer may resample")

    def _on_panel_exclusive_toggled(self, checked: bool) -> None:
        if self.exclusive_check.isChecked() == checked:
            return
        self.exclusive_check.blockSignals(True)
        self.exclusive_check.setChecked(checked)
        self.exclusive_check.blockSignals(False)
        self._on_exclusive_toggled(checked)

    # -------------------------------------------------------------- transport
    def _on_seek(self, seconds: float) -> None:
        self._engine.seek(seconds)

    def _on_repeat(self) -> None:
        mode = self._controller.cycle_repeat()
        self.now_playing.set_modes(mode.value, self._controller.playlist.shuffle)
        self._settings.update(repeat_mode=mode.value)
        self._show_status(f"Repeat: {mode.value}")

    def _on_shuffle(self) -> None:
        enabled = self._controller.toggle_shuffle()
        self.now_playing.set_modes(self._controller.playlist.repeat_mode.value, enabled)
        self._settings.update(shuffle=enabled)
        self._show_status("Shuffle enabled" if enabled else "Shuffle disabled")

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
        files, _filter = QFileDialog.getOpenFileNames(self, "Open audio files", start, AUDIO_FILTER)
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
        folder = QFileDialog.getExistingDirectory(self, "Open folder", start)
        if not folder:
            return
        self._settings.update(last_directory=folder)
        paths = discover_files([Path(folder)])
        if not paths:
            self._show_status("No supported audio files in that folder")
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
        self._show_status(f"{added} track(s) added to the queue")

    def _on_folders_changed(self, folders: List[str]) -> None:
        self._settings.update(library_folders=list(folders))

    # ------------------------------------------------------------------- menu
    def _show_menu(self) -> None:
        menu = QMenu(self)
        open_files = menu.addAction("Open files…")
        open_files.setShortcut(QKeySequence("Ctrl+O"))
        open_folder = menu.addAction("Open folder…")
        open_folder.setShortcut(QKeySequence("Ctrl+Shift+O"))
        add_library = menu.addAction("Add music folder to library…")
        menu.addSeparator()
        save_playlist = menu.addAction("Save queue…")
        load_playlist = menu.addAction("Load queue…")
        menu.addSeparator()
        preferences = menu.addAction("Engine preferences…")
        open_logs = menu.addAction("Open log folder")
        about = menu.addAction("About True HQ Audio")
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
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
        target, _filter = QFileDialog.getSaveFileName(self, "Save queue", start, "True HQ queue (*.json)")
        if not target:
            return
        try:
            self._controller.playlist.save(Path(target))
            self._show_status(f"Queue saved to {target}")
        except OSError as exc:
            self._show_status(f"Could not save the queue: {exc}")

    def _load_playlist(self) -> None:
        start = str(Path(self._settings.settings.last_directory))
        target, _filter = QFileDialog.getOpenFileName(self, "Load queue", start, "True HQ queue (*.json)")
        if not target:
            return
        try:
            playlist = Playlist.load(Path(target))
        except (OSError, ValueError) as exc:
            self._show_status(f"Could not load the queue: {exc}")
            return
        self._controller.set_playlist(playlist)
        self.playlist_view.set_playlist(playlist)
        self.playlist_view.refresh()
        self.now_playing.set_modes(playlist.repeat_mode.value, playlist.shuffle)
        self._show_status(f"Queue loaded ({len(playlist)} tracks)")

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
            self._show_status("Engine preferences applied")

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<b>{APP_NAME}</b> {APP_VERSION}<br><br>"
            "Bit-perfect reference player for Windows.<br>"
            "Zero DSP. Zero resampling. Direct hardware clock.<br><br>"
            "Signal path: decoder → lossless container mapper → lock-free ring → exclusive driver → DAC.<br>"
            "No volume normalisation, no limiter, no equaliser, no dither.<br><br>"
            f"Python {sys.version.split()[0]} · PySide6",
        )

    # ------------------------------------------------------------------ bridge
    def _on_engine_state(self, state: str, payload: Dict[str, Any]) -> None:
        self._last_state = state
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
            self.badge.set_state("off", "stream starting")
        self._settings.update(last_track=str(payload.get("path") or ""))
        self.footer.update_from(self._engine.snapshot())
        self.session_panel.update_from(self._engine.snapshot(), self._engine)

    def _on_stream_opened(self, payload: Dict[str, Any]) -> None:
        driver = payload.get("driver") or "driver"
        rate = int(payload.get("rate") or 0)
        bits = int(payload.get("bits") or 0)
        latency = float(payload.get("latency_ms") or 0.0)
        self._show_status(f"{driver} locked · {bits}-bit {humanise_rate(rate)} · {latency:.1f} ms latency")

    def _on_underrun(self, count: int, missing_frames: int) -> None:
        self._show_status(f"Buffer underrun #{count} ({missing_frames} frames) — badge downgraded")
        self._xrun_override_until = time.monotonic() + 1.5
        self.badge.set_state("degraded", f"{count} buffer underrun(s) inserted digital silence")

    def _on_device_lost(self, device: str, position: float) -> None:
        self.badge.set_state("error", f"DAC disconnected: {device}")
        self._show_status(f"DAC disconnected: {device}. Waiting for it to return…")

    def _on_device_restored(self, device: str, position: float) -> None:
        self._show_status(f"DAC reconnected: {device} — resuming from {position:.1f}s")
        self._refresh_devices()

    def _on_playlist_changed(self, payload: Dict[str, Any]) -> None:
        count = int(payload.get("count") or 0)
        self.queue_chip.setText(f"{count} TRACK{'S' if count != 1 else ''}")
        self.queue_clear_button.setEnabled(count > 0)
        self.playlist_view.refresh()
        current = int(payload.get("current", -1))
        if current >= 0:
            self.playlist_view.set_current(current)

    def _on_playlist_track(self, payload: Dict[str, Any]) -> None:
        index = int(payload.get("index", -1))
        if index >= 0:
            self.playlist_view.set_current(index)

    def _on_integrity(self, payload: Dict[str, Any]) -> None:
        digest = str(payload.get("digest") or "")
        if digest:
            self.footer.digest_label.setText(f"BLAKE2b {digest[:16]}…")

    # -------------------------------------------------------------------- poll
    def _on_poll(self) -> None:
        self._apply_snapshot(self._engine.snapshot())

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
            self.session_panel.update_from(snapshot, self._engine)
        if snapshot.device_name and snapshot.driver_label:
            self.driver_pill.set_text(snapshot.driver_label.upper(), accent=snapshot.exclusive)
        if snapshot.hardware_clock:
            self.clock_pill.set_text(humanise_rate(snapshot.hardware_clock), accent=snapshot.bit_perfect)

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
                    self._drag_active = True
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
            self._show_status(f"{added} track(s) added")
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
