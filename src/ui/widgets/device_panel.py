"""DAC clock probing and negotiated transport parameters.

The output device is chosen once, from the window header, so this panel only
covers what cannot be seen anywhere else: the clocks the driver really accepts,
the DSD-over-PCM rates and the negotiated transport facts.
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ...config import get_logger, humanise_rate
from ...core.devices import AudioBackendUnavailable, DeviceCapabilities, DeviceInfo, DeviceManager
from .base import Card, Chip, InfoRow, SectionLabel

LOGGER = get_logger("ui.devices")

PROBE_LABEL = "Detectar"
PROBING_LABEL = "Detectando…"


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
    """Probed clock matrix and negotiated parameters for the selected DAC."""

    def __init__(self, manager: DeviceManager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._manager = manager
        self._device: Optional[DeviceInfo] = None
        self._exclusive = True
        self._capabilities: Optional[DeviceCapabilities] = None
        self._probe_thread: Optional[CapabilityProbe] = None
        self._rate_chips: List[Chip] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        clock_card = Card(spacing=8)
        clock_card.body.setContentsMargins(16, 14, 16, 14)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        header.addWidget(SectionLabel("Frecuencias nativas del DAC"))
        header.addStretch(1)
        self.probe_button = QPushButton(PROBE_LABEL)
        self.probe_button.setObjectName("Primary")
        self.probe_button.setToolTip("Detectar las frecuencias que acepta el DAC seleccionado")
        self.probe_button.clicked.connect(self.probe_now)
        header.addWidget(self.probe_button)
        clock_card.body.addLayout(header)

        self.clock_summary = QLabel("Elige la salida en la cabecera y detecta aquí las frecuencias que acepta.")
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
        facts.body.addWidget(SectionLabel("Parámetros negociados"))
        self.rows = {
            "driver": InfoRow("Controlador", "—"),
            "path": InfoRow("Camino de salida", "—"),
            "channels": InfoRow("Canales de salida", "—"),
            "default_rate": InfoRow("Frecuencia predeterminada", "—"),
            "max_rate": InfoRow("Frecuencia máxima", "—"),
            "max_depth": InfoRow("Profundidad máxima", "—"),
            "dop": InfoRow("Frecuencias DoP", "—"),
            "backend": InfoRow("PortAudio", "—"),
            "notes": InfoRow("Notas del controlador", "—"),
        }
        for row in self.rows.values():
            facts.body.addWidget(row)
        layout.addWidget(facts)
        layout.addStretch(1)

        versions = manager.backend_versions()
        self.rows["backend"].set_value(f"{versions.get('portaudio', '?')} / sd {versions.get('sounddevice', '?')}")

    # -------------------------------------------------------------------- data
    def set_device(self, device: Optional[DeviceInfo]) -> None:
        """Point the panel at the DAC selected in the header."""

        if device is None or (self._device is not None and device.index == self._device.index and device.name == self._device.name):
            if device is None:
                self._reset_facts()
            return
        self._device = device
        self._capabilities = None
        self._clear_rate_chips()
        self.probe_button.setEnabled(True)
        self.probe_button.setText(PROBE_LABEL)
        self.rows["driver"].set_value(device.driver_label)
        self.rows["path"].set_value(self._path_text(device))
        self.rows["channels"].set_value(f"{device.max_output_channels} ch")
        self.rows["default_rate"].set_value(
            humanise_rate(int(device.default_samplerate)) if device.default_samplerate else "—"
        )
        self.rows["max_rate"].set_value("—")
        self.rows["max_depth"].set_value("—")
        self.rows["dop"].set_value("—")
        self.rows["notes"].set_value("sin analizar")
        self.clock_summary.setText("Detecta las frecuencias para ver la matriz completa del controlador.")

    def set_exclusive(self, exclusive: bool) -> None:
        exclusive = bool(exclusive)
        if exclusive == self._exclusive:
            return
        self._exclusive = exclusive
        self._capabilities = None
        self._clear_rate_chips()
        if self._device is not None:
            self.rows["max_rate"].set_value("—")
            self.rows["max_depth"].set_value("—")
            self.rows["dop"].set_value("—")
            self.rows["notes"].set_value("sin analizar")
            self.clock_summary.setText("Detecta las frecuencias para ver la matriz completa del controlador.")

    def _reset_facts(self) -> None:
        self._device = None
        self._capabilities = None
        self._clear_rate_chips()
        self.probe_button.setEnabled(False)
        for row in self.rows.values():
            row.set_value("—")
        self.clock_summary.setText("No hay ningún DAC disponible para analizar.")
        versions = self._manager.backend_versions()
        self.rows["backend"].set_value(f"{versions.get('portaudio', '?')} / sd {versions.get('sounddevice', '?')}")

    @staticmethod
    def _path_text(device: DeviceInfo) -> str:
        if device.is_asio:
            return "ASIO: omite el motor de audio de Windows"
        if device.is_wasapi:
            return "WASAPI exclusivo: reloj del DAC, sin mezclador"
        if device.host_kind == "wdm-ks":
            return "WDM-KS: transmisión directa al núcleo"
        return "Mezclador compartido de Windows: puede remuestrear"

    # ----------------------------------------------------------------- probing
    def probe_now(self) -> None:
        if self._device is None:
            return
        if self._probe_thread is not None and self._probe_thread.isRunning():
            return
        self.probe_button.setEnabled(False)
        self.probe_button.setText(PROBING_LABEL)
        self.rows["notes"].set_value("consultando el controlador")
        thread = CapabilityProbe(self._manager, self._device, self._exclusive, self)
        thread.probed.connect(self._on_probed)
        thread.failed.connect(self._on_probe_failed)
        thread.finished.connect(self._on_probe_finished)
        self._probe_thread = thread
        thread.start()

    def _on_probe_finished(self) -> None:
        self.probe_button.setEnabled(True)
        self.probe_button.setText(PROBE_LABEL)

    def _on_probe_failed(self, device_index: int, message: str) -> None:
        self.rows["notes"].set_value(message[:64])
        self.clock_summary.setText("El controlador rechazó la consulta de capacidades.")
        LOGGER.warning("capability probe failed", extra={"device_index": device_index, "error": message})

    def _on_probed(self, device_index: int, capabilities: DeviceCapabilities, exclusive: bool) -> None:
        if self._device is None or self._device.index != device_index:
            return
        self._capabilities = capabilities
        if not capabilities.rates:
            self.clock_summary.setText("El controlador no informó de frecuencias utilizables en este modo.")
            return
        best_rate = capabilities.max_rate
        self.clock_summary.setText(
            f"{len(capabilities.rates)} frecuencias aceptadas · máximo {humanise_rate(best_rate)} · "
            f"hasta {capabilities.max_bits} bits"
        )
        self.rows["max_rate"].set_value(humanise_rate(best_rate))
        self.rows["max_depth"].set_value(f"{capabilities.max_bits} bit")
        self.rows["notes"].set_value(" · ".join(capabilities.errors[:2]) if capabilities.errors else "sin errores")
        dop = capabilities.dop_capable_rates()
        self.rows["dop"].set_value(", ".join(humanise_rate(rate) for rate in dop) if dop else "ninguna")
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
    def current_device(self) -> Optional[DeviceInfo]:
        return self._device

    def shutdown(self, timeout_ms: int = 3000) -> None:
        """Wait for a running capability probe so Qt never destroys a live thread."""

        thread = self._probe_thread
        if thread is not None and thread.isRunning():
            thread.wait(timeout_ms)
