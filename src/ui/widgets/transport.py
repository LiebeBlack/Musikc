"""Transport controls: previous / play-pause / next / stop, repeat and shuffle."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QWidget

from .base import IconButton


class TransportBar(QWidget):
    """Axis of playback controls with a prominent, accent play button."""

    playPauseRequested = Signal()
    previousRequested = Signal()
    nextRequested = Signal()
    stopRequested = Signal()
    repeatRequested = Signal()
    shuffleRequested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

        self.stop_button = IconButton("stop", "Detener (Ctrl+.)", size=34, icon_size=16)
        self.previous_button = IconButton("previous", "Pista anterior (Ctrl+←)", size=40, icon_size=17)
        self.play_button = IconButton("play", "Reproducir / Pausar (Espacio)", size=56, icon_size=22, accent=True)
        self.next_button = IconButton("next", "Pista siguiente (Ctrl+→)", size=40, icon_size=17)
        self.repeat_button = IconButton("repeat", "Modo de repetición", size=36, icon_size=16, checkable=True)
        self.shuffle_button = IconButton("shuffle", "Reproducción aleatoria", size=36, icon_size=16, checkable=True)
        self._has_track = False
        self._update_enabled_state()

        layout.addWidget(self.stop_button)
        layout.addSpacing(2)
        layout.addWidget(self.previous_button)
        layout.addWidget(self.play_button)
        layout.addWidget(self.next_button)
        layout.addSpacing(10)
        layout.addWidget(self.repeat_button)
        layout.addWidget(self.shuffle_button)

        self.stop_button.setToolTip("Detener")
        self.play_button.setAccessibleName("Reproducir o pausar")
        self.previous_button.setAccessibleName("Pista anterior")
        self.next_button.setAccessibleName("Pista siguiente")
        self.stop_button.setAccessibleName("Detener reproducción")
        self.repeat_button.setAccessibleName("Modo de repetición")
        self.shuffle_button.setAccessibleName("Reproducción aleatoria de la cola")
        self._playing = False

        self.stop_button.clicked.connect(self.stopRequested.emit)
        self.previous_button.clicked.connect(self.previousRequested.emit)
        self.play_button.clicked.connect(self.playPauseRequested.emit)
        self.next_button.clicked.connect(self.nextRequested.emit)
        self.repeat_button.clicked.connect(self.repeatRequested.emit)
        self.shuffle_button.clicked.connect(self.shuffleRequested.emit)

    # -------------------------------------------------------------------- api
    def set_playing(self, playing: bool) -> None:
        if playing != self._playing:
            self._playing = playing
            self.play_button.set_icon("pause" if playing else "play")
            self.play_button.setToolTip("Pausar (Espacio)" if playing else "Reproducir (Espacio)")

    def set_transport_enabled(self, enabled: bool) -> None:
        self._has_track = bool(enabled)
        self._update_enabled_state()

    def set_enabled_transport(self, enabled: bool) -> None:
        self.set_transport_enabled(enabled)

    def _update_enabled_state(self) -> None:
        for button in (self.play_button, self.previous_button, self.next_button, self.stop_button):
            button.setEnabled(self._has_track)
        self.repeat_button.setEnabled(self._has_track)
        self.shuffle_button.setEnabled(self._has_track)

    def set_repeat_mode(self, mode: str) -> None:
        icon = "repeat_one" if mode == "one" else "repeat"
        self.repeat_button.set_icon(icon)
        self.repeat_button.setChecked(mode != "off")
        self.repeat_button.setToolTip(
            {"off": "Repetición: desactivada", "all": "Repetir cola", "one": "Repetir pista"}.get(mode, "Repetición")
        )
        self.repeat_button.set_accent(mode != "off")

    def set_shuffle(self, enabled: bool) -> None:
        self.shuffle_button.setChecked(enabled)
        self.shuffle_button.set_accent(enabled)
        self.shuffle_button.setToolTip("Aleatorio: activado" if enabled else "Aleatorio: desactivado")

    def flash_play(self) -> None:
        self.play_button.setFocus(Qt.FocusReason.OtherFocusReason)
