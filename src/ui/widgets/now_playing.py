"""Now-playing card: typography, technical chips, seek bar and transport."""

from __future__ import annotations

from typing import Any, Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from ...config import format_duration_precise
from .base import Card, Chip, ElidedLabel
from .seekbar import SeekBar
from .transport import TransportBar


class NowPlayingCard(Card):
    seekRequested = Signal(float)
    playPauseRequested = Signal()
    previousRequested = Signal()
    nextRequested = Signal()
    stopRequested = Signal()
    repeatRequested = Signal()
    shuffleRequested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, spacing=5)
        self.body.setContentsMargins(18, 12, 18, 12)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self.state_chip = Chip("IDLE", "chip")
        header.addWidget(self.state_chip)
        header.addStretch(1)
        self.format_chip = Chip("", "accent")
        self.format_chip.setVisible(False)
        header.addWidget(self.format_chip)
        self.container_chip = Chip("", "chip")
        self.container_chip.setVisible(False)
        header.addWidget(self.container_chip)
        self.body.addLayout(header)

        self.title_label = ElidedLabel("No track loaded")
        self.title_label.setObjectName("TrackTitle")
        self.body.addWidget(self.title_label)

        self.artist_label = ElidedLabel("Open a file or pick a track from the library")
        self.artist_label.setObjectName("TrackArtist")
        self.body.addWidget(self.artist_label)

        self.album_label = ElidedLabel("")
        self.album_label.setObjectName("TrackAlbum")
        self.body.addWidget(self.album_label)

        self.spec_label = QLabel("")
        self.spec_label.setObjectName("Hint")
        self.spec_label.setWordWrap(True)
        self.body.addWidget(self.spec_label)

        self.body.addSpacing(1)
        self.seek_bar = SeekBar()
        self.seek_bar.set_enabled(False)
        self.seek_bar.seekRequested.connect(self.seekRequested.emit)
        self.body.addWidget(self.seek_bar)

        time_row = QHBoxLayout()
        time_row.setContentsMargins(0, 0, 0, 0)
        self.position_label = QLabel("00:00.000")
        self.position_label.setObjectName("TimeLabel")
        self.duration_label = QLabel("--:--")
        self.duration_label.setObjectName("TimeLabel")
        self.duration_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        time_row.addWidget(self.position_label)
        time_row.addStretch(1)
        time_row.addWidget(self.duration_label)
        self.body.addLayout(time_row)

        transport_row = QHBoxLayout()
        transport_row.setContentsMargins(0, 6, 0, 0)
        self.transport = TransportBar()
        self.transport.playPauseRequested.connect(self.playPauseRequested.emit)
        self.transport.previousRequested.connect(self.previousRequested.emit)
        self.transport.nextRequested.connect(self.nextRequested.emit)
        self.transport.stopRequested.connect(self.stopRequested.emit)
        self.transport.repeatRequested.connect(self.repeatRequested.emit)
        self.transport.shuffleRequested.connect(self.shuffleRequested.emit)
        transport_row.addWidget(self.transport)
        transport_row.addStretch(1)
        self.mode_label = QLabel("")
        self.mode_label.setObjectName("Hint")
        self.mode_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        transport_row.addWidget(self.mode_label)
        self.body.addLayout(transport_row)

        self._duration = 0.0
        self._position = 0.0
        self._scrubbing = False
        self._has_track = False
        self._last_playback_state = "idle"
        self._title_default_point_size = self.title_label.font().pointSizeF()
        self._title_default_weight = self.title_label.font().weight()
        self.seek_bar.scrubStarted.connect(self._on_scrub_started)
        self.seek_bar.scrubEnded.connect(self._on_scrub_ended)
        self._set_placeholder(True)

    # -------------------------------------------------------------------- state
    def _set_placeholder(self, placeholder: bool) -> None:
        self._has_track = not placeholder
        self.artist_label.setVisible(True)
        for widget in (self.format_chip, self.container_chip):
            widget.setVisible(not placeholder and bool(widget.text()))
        if placeholder:
            self.spec_label.clear()
            self.spec_label.hide()
            self.album_label.clear()
            self.album_label.hide()
            self.duration_label.setText("--:--")
            self.position_label.setText("00:00.000")
            self.seek_bar.set_duration(0.0)
            self.seek_bar.set_position(0.0)
            self.seek_bar.set_enabled(False)
        self._refresh_title_scale()

    def set_track(self, payload: Dict[str, Any]) -> None:
        title = payload.get("title") or "Untitled"
        artist = payload.get("artist") or "Unknown artist"
        album = payload.get("album") or ""
        self.title_label.setText(title)
        self.artist_label.setText(artist)
        self.album_label.setText(album)
        self.album_label.setVisible(bool(album) and album.casefold() != title.casefold())
        resolution = payload.get("resolution") or ""
        container = payload.get("container") or ""
        codec = payload.get("codec") or ""
        self.format_chip.setText(resolution.upper())
        self.container_chip.setText(" · ".join(part for part in (container, codec) if part).upper())
        self._set_placeholder(False)
        notes = payload.get("notes") or []
        spec = " · ".join(str(note) for note in notes if note)
        self.spec_label.setText(spec)
        self.spec_label.setVisible(bool(spec))
        duration = float(payload.get("duration") or 0.0)
        self._duration = duration
        self.seek_bar.set_duration(duration)
        self.seek_bar.set_position(0.0)
        self.seek_bar.set_enabled(duration > 0)
        self.duration_label.setText(format_duration_precise(duration) if duration > 0 else "--:--")
        self._position = 0.0
        self.position_label.setText("00:00.000")
        self.set_playback_state("stopped")
        self._refresh_title_scale()

    def clear_track(self) -> None:
        self.title_label.setText("No track loaded")
        self.artist_label.setText("Open a file or pick a track from the library")
        self.album_label.setText("")
        self.album_label.setVisible(False)
        self.format_chip.setVisible(False)
        self.container_chip.setVisible(False)
        self._duration = 0.0
        self.seek_bar.set_duration(0.0)
        self.seek_bar.set_position(0.0)
        self.seek_bar.set_enabled(False)
        self.duration_label.setText("--:--")
        self.position_label.setText("00:00.000")
        self.spec_label.clear()
        self._set_placeholder(True)
        self.set_playback_state("stopped")

    def set_position(self, seconds: float) -> None:
        if self._scrubbing:
            return
        self._position = max(0.0, seconds)
        self.seek_bar.set_position(self._position)
        self.position_label.setText(format_duration_precise(self._position))

    def set_playback_state(self, state: str) -> None:
        labels = {
            "playing": "PLAYING",
            "paused": "PAUSED",
            "stopped": "STOPPED",
            "opening": "LOCKING CLOCK",
            "error": "ERROR",
            "no_device": "DAC LOST",
            "idle": "IDLE",
            "closed": "CLOSED",
            "draining": "DRAINING",
        }
        self.state_chip.setText(labels.get(state, state.upper()))
        self.state_chip.set_variant(
            "accent" if state == "playing" else ("warning" if state in ("paused", "opening", "no_device") else "chip")
        )
        self._last_playback_state = state
        self.transport.set_playing(state == "playing")
        self.transport.set_transport_enabled(self._has_track)
        self.seek_bar.set_enabled(self._has_track and self._duration > 0)
        self._refresh_title_scale()

    def set_modes(self, repeat: str, shuffle: bool) -> None:
        self.transport.set_repeat_mode(repeat)
        self.transport.set_shuffle(shuffle)
        parts = []
        parts.append(f"repeat {repeat}")
        parts.append("shuffle on" if shuffle else "shuffle off")
        self.mode_label.setText(" · ".join(parts))

    def _on_scrub_started(self) -> None:
        self._scrubbing = True

    def _on_scrub_ended(self, value: float) -> None:
        self._scrubbing = False
        self._position = value
        self.position_label.setText(format_duration_precise(value))

    def set_enabled_transport(self, enabled: bool) -> None:
        self._has_track = bool(enabled)
        self.transport.set_transport_enabled(enabled)
        self.seek_bar.set_enabled(enabled and self._duration > 0)

    def _refresh_title_scale(self) -> None:
        if not hasattr(self, "title_label"):
            return
        size = self._title_default_point_size
        if self.width() < 560:
            size = min(size, 24.0)
        if self.width() < 460:
            size = min(size, 20.0)
        font = self.title_label.font()
        if abs(font.pointSizeF() - size) > 0.05:
            font.setPointSizeF(size)
            font.setWeight(self._title_default_weight)
            self.title_label.setFont(font)
            self.title_label._apply_elide()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._refresh_title_scale()
