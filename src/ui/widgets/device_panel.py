"""DAC selection, exclusive-mode negotiation and live capability probing."""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...config import get_logger, humanise_rate
from ...core.devices import AudioBackendUnavailable, DeviceCapabilities, DeviceInfo, DeviceManager
from .base import Card, Chip, InfoRow, SectionLabel

LOGGER = get_logger("ui.devices")

HOST_FILTERS = (
    ("all", "All drivers"),
    ("asio", "ASIO"),
    ("wasapi", "WASAPI"),
    ("wdm-ks", "WDM-KS"),
    ("directsound", "DirectSound"),
    ("mme", "MME"),
)


class CapabilityProbe(QThread):
    """Runs Pa_IsFormatSupported outside the UI thread."""

    probed = Signal(int, object, bool)
    failed = Signal(int, str)

    def __init__(self, manager: DeviceManager, device: DeviceInfo, exclusive: bool, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._manager = manager
        self._device = device
        self._exclusive = exclusive

    def run(self) -> None:  # type: ignore[override]
        try:
            capabilities = self._manager.capabilities(self._device, exclusive=self._exclusive, reuse_cache=False)
        except AudioBackendUnavailable as exc:
            self.failed.emit(self._device.index, str(exc))
            return
        except Exception as exc:  # pragma: no cover - driver level failure
            self.failed.emit(self._device.index, f"{type(exc).__name__}: {exc}")
            return
        self.probed.emit(self._device.index, capabilities, self._exclusive)


class DevicePanel(QWidget):
    """Panel with device selection, exclusive toggle and probed clocks."""

    deviceChanged = Signal(int, bool)
    exclusiveToggled = Signal(bool)
    reprobeRequested = Signal()

    def __init__(self, manager: DeviceManager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._manager = manager
        self._devices: List[DeviceInfo] = []
        self._current: Optional[DeviceInfo] = None
        self._capabilities: Optional[DeviceCapabilities] = None
        self._probe_thread: Optional[CapabilityProbe] = None
        self._rate_chips: List[Chip] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        selector = Card(spacing=10)
        selector.body.setContentsMargins(16, 14, 16, 14)
        selector.body.addWidget(SectionLabel("Output device"))

        filter_row = QHBoxLayout()
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.setSpacing(8)
        self.host_filter = QComboBox()
        for key, label in HOST_FILTERS:
            self.host_filter.addItem(label, key)
        self.host_filter.currentIndexChanged.connect(self._rebuild_device_list)
        filter_row.addWidget(self.host_filter, 1)
        self.rescan_button = QPushButton("Rescan")
        self.rescan_button.clicked.connect(self.refresh_devices)
        filter_row.addWidget(self.rescan_button)
        selector.body.addLayout(filter_row)

        self.device_combo = QComboBox()
        self.device_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.device_combo.setMinimumContentsLength(26)
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        selector.body.addWidget(self.device_combo)

        mode_row = QHBoxLayout()
        mode_row.setContentsMargins(0, 0, 0, 0)
        self.exclusive_check = QCheckBox("Exclusive mode (bypass the OS mixer)")
        self.exclusive_check.setChecked(True)
        self.exclusive_check.toggled.connect(self._on_exclusive_toggled)
        mode_row.addWidget(self.exclusive_check)
        mode_row.addStretch(1)
        selector.body.addLayout(mode_row)

        self.driver_label = QLabel("")
        self.driver_label.setObjectName("Hint")
        self.driver_label.setWordWrap(True)
        selector.body.addWidget(self.driver_label)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        self.apply_button = QPushButton("Use this DAC")
        self.apply_button.setObjectName("Primary")
        self.apply_button.clicked.connect(self._emit_selection)
        actions.addWidget(self.apply_button)
        self.probe_button = QPushButton("Probe clocks")
        self.probe_button.clicked.connect(self.probe_now)
        actions.addWidget(self.probe_button)
        selector.body.addLayout(actions)
        layout.addWidget(selector)

        clock_card = Card(spacing=8)
        clock_card.body.setContentsMargins(16, 14, 16, 14)
        clock_card.body.addWidget(SectionLabel("Native clocks reported by the driver"))
        self.clock_summary = QLabel("Probe the DAC to enumerate its clocks.")
        self.clock_summary.setObjectName("Hint")
        self.clock_summary.setWordWrap(True)
        clock_card.body.addWidget(self.clock_summary)
        self.rate_container = QWidget()
        self.rate_layout = QGridLayout(self.rate_container)
        self.rate_layout.setContentsMargins(0, 2, 0, 0)
        self.rate_layout.setHorizontalSpacing(6)
        self.rate_layout.setVerticalSpacing(6)
        clock_card.body.addWidget(self.rate_container)
        layout.addWidget(clock_card)

        facts = Card(spacing=4)
        facts.body.setContentsMargins(16, 14, 16, 14)
        facts.body.addWidget(SectionLabel("Negotiation facts"))
        self.rows = {
            "driver": InfoRow("Driver", "—"),
            "channels": InfoRow("Output channels", "—"),
            "default_rate": InfoRow("Default clock", "—"),
            "max_rate": InfoRow("Probed maximum", "—"),
            "max_depth": InfoRow("Probed depth", "—"),
            "dop": InfoRow("DoP capable rates", "—"),
            "backend": InfoRow("PortAudio", "—"),
            "errors": InfoRow("Driver notes", "—"),
        }
        for row in self.rows.values():
            facts.body.addWidget(row)
        layout.addWidget(facts)
        layout.addStretch(1)

        versions = manager.backend_versions()
        self.rows["backend"].set_value(f"{versions.get('portaudio', '?')} / sd {versions.get('sounddevice', '?')}")
        self.refresh_devices()

    # -------------------------------------------------------------------- data
    def refresh_devices(self) -> None:
        try:
            self._devices = self._manager.refresh(force=True)
        except AudioBackendUnavailable as exc:
            self._devices = []
            self.device_combo.clear()
            self.device_combo.addItem("No audio backend available", None)
            self.driver_label.setText(str(exc))
            return
        self._rebuild_device_list()

    def _rebuild_device_list(self) -> None:
        selected_index = self._current.index if self._current else None
        host_key = self.host_filter.currentData() or "all"
        filtered = [device for device in self._devices if host_key == "all" or device.host_kind == host_key]
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for device in filtered:
            label = f"{device.name}   ·   {device.driver_label}"
            self.device_combo.addItem(label, device.index)
        self.device_combo.blockSignals(False)
        if not filtered:
            self.device_combo.addItem("No output device for this driver", None)
            self.driver_label.setText("Connect a DAC or pick another driver family.")
            return
        target_position = 0
        for position, device in enumerate(filtered):
            if selected_index is not None and device.index == selected_index:
                target_position = position
                break
            if device.is_default_output and selected_index is None:
                target_position = position
        self.device_combo.setCurrentIndex(target_position)
        self._on_device_changed(target_position)

    def _on_device_changed(self, position: int) -> None:
        index = self.device_combo.itemData(position)
        self._current = next((device for device in self._devices if device.index == index), None)
        self._capabilities = None
        if self._current is None:
            return
        device = self._current
        exclusive_possible = device.exclusive_capable
        self.exclusive_check.setEnabled(exclusive_possible)
        if not exclusive_possible:
            self.exclusive_check.setChecked(False)
        self.rows["driver"].set_value(device.driver_label)
        self.rows["channels"].set_value(f"{device.max_output_channels} ch")
        self.rows["default_rate"].set_value(humanise_rate(int(device.default_samplerate)) if device.default_samplerate else "—")
        self.rows["max_rate"].set_value("—")
        self.rows["max_depth"].set_value("—")
        self.rows["dop"].set_value("—")
        self.rows["errors"].set_value("—")
        self.clock_summary.setText("Probe the DAC to enumerate its clocks.")
        self._clear_rate_chips()
        if device.is_asio:
            self.driver_label.setText(
                "ASIO bypasses the Windows audio engine completely. Lowest latency and no OS volume stage."
            )
        elif device.is_wasapi:
            self.driver_label.setText(
                "WASAPI Exclusive locks the DAC clock; the Windows mixer is bypassed entirely."
            )
        elif exclusive_possible:
            self.driver_label.setText("Kernel streaming bypasses the system mixer (WDM-KS).")
        else:
            self.driver_label.setText(
                "This driver family shares the Windows mixer: it can resample the stream and is never bit-perfect."
            )

    def _on_exclusive_toggled(self, checked: bool) -> None:
        if self._current is not None and not checked and self._current.exclusive_capable:
            self.driver_label.setText("Shared mode selected: the OS mixer may resample the signal.")
        self._clear_rate_chips()
        self.clock_summary.setText("Probe the DAC to enumerate its clocks.")
        self.exclusiveToggled.emit(bool(checked))

    # ----------------------------------------------------------------- probing
    def probe_now(self) -> None:
        if self._current is None:
            return
        if self._probe_thread is not None and self._probe_thread.isRunning():
            return
        self.probe_button.setEnabled(False)
        self.probe_button.setText("Probing…")
        self.rows["errors"].set_value("querying the driver")
        thread = CapabilityProbe(self._manager, self._current, self.exclusive_check.isChecked(), self)
        thread.probed.connect(self._on_probed)
        thread.failed.connect(self._on_probe_failed)
        thread.finished.connect(self._on_probe_finished)
        self._probe_thread = thread
        thread.start()

    def _on_probe_finished(self) -> None:
        self.probe_button.setEnabled(True)
        self.probe_button.setText("Probe clocks")

    def _on_probe_failed(self, device_index: int, message: str) -> None:
        self.rows["errors"].set_value(message[:64])
        self.clock_summary.setText("The driver rejected the capability query.")
        LOGGER.warning("capability probe failed", extra={"device_index": device_index, "error": message})

    def _on_probed(self, device_index: int, capabilities: DeviceCapabilities, exclusive: bool) -> None:
        if self._current is None or self._current.index != device_index:
            return
        self._capabilities = capabilities
        if not capabilities.rates:
            self.clock_summary.setText("The driver reported no usable clocks for this mode.")
            return
        best_rate = capabilities.max_rate
        summary = (
            f"{len(capabilities.rates)} clocks accepted · maximum {humanise_rate(best_rate)} · "
            f"up to {capabilities.max_bits} bit"
        )
        self.clock_summary.setText(summary)
        self.rows["max_rate"].set_value(humanise_rate(best_rate))
        self.rows["max_depth"].set_value(f"{capabilities.max_bits} bit")
        (self.rows["errors"].set_value(" · ".join(capabilities.errors[:2]) if capabilities.errors else "clean"))
        dop = capabilities.dop_capable_rates()
        self.rows["dop"].set_value(", ".join(humanise_rate(rate) for rate in dop) if dop else "none")
        self._render_rate_chips(capabilities)

    def _clear_rate_chips(self) -> None:
        for chip in self._rate_chips:
            chip.setParent(None)
            chip.deleteLater()
        self._rate_chips.clear()

    def _render_rate_chips(self, capabilities: DeviceCapabilities) -> None:
        self._clear_rate_chips()
        rates = sorted(capabilities.rates)
        for position, rate in enumerate(rates):
            formats = capabilities.rates[rate]
            integer_depths = [int(name.replace("int", "")) for name in formats if name.startswith("int")]
            if integer_depths:
                depth = str(max(integer_depths))
            else:
                depth = "32f" if "float32" in formats else "64f"
            chip = Chip(f"{rate / 1000:g}k · {depth}", "accent" if rate == capabilities.max_rate else "chip")
            chip.setToolTip(" · ".join(formats))
            self.rate_layout.addWidget(chip, position // 4, position % 4)
            self._rate_chips.append(chip)

    # ------------------------------------------------------------------ actions
    def _emit_selection(self) -> None:
        if self._current is None:
            return
        self.deviceChanged.emit(self._current.index, self.exclusive_check.isChecked())

    def current_device(self) -> Optional[DeviceInfo]:
        return self._current

    def select_device(self, name: str, host_api: str = "") -> None:
        device = self._manager.find_by_name(name, host_api)
        if device is None:
            return
        for position in range(self.device_combo.count()):
            if self.device_combo.itemData(position) == device.index:
                self.device_combo.setCurrentIndex(position)
                return
        self.host_filter.setCurrentIndex(0)
        for position in range(self.device_combo.count()):
            if self.device_combo.itemData(position) == device.index:
                self.device_combo.setCurrentIndex(position)
                return

    def set_exclusive(self, exclusive: bool) -> None:
        self.exclusive_check.setChecked(bool(exclusive))

    def shutdown(self, timeout_ms: int = 3000) -> None:
        """Wait for a running capability probe so Qt never destroys a live thread."""

        thread = self._probe_thread
        if thread is not None and thread.isRunning():
            thread.wait(timeout_ms)
