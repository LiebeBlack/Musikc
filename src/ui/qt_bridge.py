"""Thread-safe bridge from the engine event bus to Qt signals.

Every engine thread publishes on :class:`EventBus`.  This bridge converts those
events into Qt signals; because the emitting object lives in the GUI thread and
the emission happens from a worker thread, Qt automatically queues the calls, so
widget code runs exclusively on the UI thread.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, Signal

from ..core.events import (
    ENGINE_DEVICE_LOST,
    ENGINE_DEVICE_RESTORED,
    ENGINE_ERROR,
    ENGINE_FINISHED,
    ENGINE_INTEGRITY,
    ENGINE_STATE,
    ENGINE_STREAM_OPENED,
    ENGINE_TRACK_CHANGED,
    ENGINE_XRUN,
    Event,
    EventBus,
    LIBRARY_SCANNED,
    PLAYLIST_CHANGED,
    PLAYLIST_TRACK,
    SETTINGS_CHANGED,
)


class QtBridge(QObject):
    stateChanged = Signal(str, dict)
    errorRaised = Signal(str, str)
    trackChanged = Signal(dict)
    streamOpened = Signal(dict)
    underrun = Signal(int, int)
    deviceLost = Signal(str, float)
    deviceRestored = Signal(str, float)
    integrityUpdated = Signal(dict)
    playlistChanged = Signal(dict)
    playlistTrack = Signal(dict)
    trackFinished = Signal(str)
    libraryScanned = Signal(dict)
    settingsChanged = Signal(dict)

    def __init__(self, bus: EventBus, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._bus = bus
        bus.subscribe(ENGINE_STATE, self._on_state)
        bus.subscribe(ENGINE_ERROR, self._on_error)
        bus.subscribe(ENGINE_TRACK_CHANGED, self._on_track)
        bus.subscribe(ENGINE_STREAM_OPENED, self._on_stream_opened)
        bus.subscribe(ENGINE_XRUN, self._on_xrun)
        bus.subscribe(ENGINE_DEVICE_LOST, self._on_device_lost)
        bus.subscribe(ENGINE_DEVICE_RESTORED, self._on_device_restored)
        bus.subscribe(ENGINE_INTEGRITY, self._on_integrity)
        bus.subscribe(PLAYLIST_CHANGED, self._on_playlist)
        bus.subscribe(PLAYLIST_TRACK, self._on_playlist_track)
        bus.subscribe(ENGINE_FINISHED, self._on_finished)
        bus.subscribe(LIBRARY_SCANNED, self._on_library)
        bus.subscribe(SETTINGS_CHANGED, self._on_settings)

    # ------------------------------------------------------------------ relays
    def _on_state(self, event: Event) -> None:
        self.stateChanged.emit(str(event.get("state") or ""), dict(event.payload))

    def _on_error(self, event: Event) -> None:
        self.errorRaised.emit(str(event.get("message") or ""), str(event.get("previous") or ""))

    def _on_track(self, event: Event) -> None:
        self.trackChanged.emit(dict(event.payload))

    def _on_stream_opened(self, event: Event) -> None:
        self.streamOpened.emit(dict(event.payload))

    def _on_xrun(self, event: Event) -> None:
        self.underrun.emit(int(event.get("count") or 0), int(event.get("missing_frames") or 0))

    def _on_device_lost(self, event: Event) -> None:
        self.deviceLost.emit(str(event.get("device") or ""), float(event.get("position") or 0.0))

    def _on_device_restored(self, event: Event) -> None:
        self.deviceRestored.emit(str(event.get("device") or ""), float(event.get("position") or 0.0))

    def _on_integrity(self, event: Event) -> None:
        self.integrityUpdated.emit(dict(event.payload))

    def _on_playlist(self, event: Event) -> None:
        self.playlistChanged.emit(dict(event.payload))

    def _on_playlist_track(self, event: Event) -> None:
        self.playlistTrack.emit(dict(event.payload))

    def _on_finished(self, event: Event) -> None:
        self.trackFinished.emit(str(event.get("path") or ""))

    def _on_library(self, event: Event) -> None:
        self.libraryScanned.emit(dict(event.payload))

    def _on_settings(self, event: Event) -> None:
        self.settingsChanged.emit(dict(event.payload))
