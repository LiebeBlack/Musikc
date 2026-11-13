"""In-process event bus used to decouple the engine from the user interface.

Publishing never blocks: a misbehaving subscriber is logged and skipped.  The
Qt bridge subscribes once and re-emits every event as a queued signal, so the
UI thread is the only place where widgets are touched.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

from ..config import get_logger

LOGGER = get_logger("events")

ENGINE_READY = "engine.ready"
ENGINE_STATE = "engine.state"
ENGINE_ERROR = "engine.error"
ENGINE_TRACK_CHANGED = "engine.track"
ENGINE_POSITION = "engine.position"
ENGINE_XRUN = "engine.xrun"
ENGINE_DEVICE_LOST = "engine.device_lost"
ENGINE_DEVICE_RESTORED = "engine.device_restored"
ENGINE_STREAM_OPENED = "engine.stream_opened"
ENGINE_INTEGRITY = "engine.integrity"
ENGINE_FINISHED = "engine.finished"
PLAYLIST_CHANGED = "playlist.changed"
PLAYLIST_TRACK = "playlist.track"
LIBRARY_SCANNED = "library.scanned"
SETTINGS_CHANGED = "settings.changed"
APP_CLOSING = "app.closing"

ALL_KINDS = (
    ENGINE_READY,
    ENGINE_STATE,
    ENGINE_ERROR,
    ENGINE_TRACK_CHANGED,
    ENGINE_POSITION,
    ENGINE_XRUN,
    ENGINE_DEVICE_LOST,
    ENGINE_DEVICE_RESTORED,
    ENGINE_STREAM_OPENED,
    ENGINE_INTEGRITY,
    ENGINE_FINISHED,
    PLAYLIST_CHANGED,
    PLAYLIST_TRACK,
    LIBRARY_SCANNED,
    SETTINGS_CHANGED,
    APP_CLOSING,
)


@dataclass(frozen=True, slots=True)
class Event:
    """Immutable event envelope."""

    kind: str
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.monotonic)

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


Subscriber = Callable[[Event], None]


class EventBus:
    """Tiny thread-safe publish/subscribe hub."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: Dict[str, List[Subscriber]] = {}
        self._wildcard: List[Subscriber] = []
        self._publishing = 0

    def subscribe(self, kind: str, callback: Subscriber) -> int:
        with self._lock:
            if kind == "*":
                self._wildcard.append(callback)
                return id(callback)
            self._subscribers.setdefault(kind, []).append(callback)
            return id(callback)

    def unsubscribe(self, token: int) -> None:
        with self._lock:
            self._wildcard = [cb for cb in self._wildcard if id(cb) != token]
            for kind, callbacks in list(self._subscribers.items()):
                self._subscribers[kind] = [cb for cb in callbacks if id(cb) != token]

    def publish(self, kind: str, **payload: Any) -> None:
        event = Event(kind, payload)
        with self._lock:
            targets = list(self._subscribers.get(kind, ())) + list(self._wildcard)
        for callback in targets:
            try:
                callback(event)
            except Exception:  # pragma: no cover - subscriber isolation
                LOGGER.exception("event subscriber failed", extra={"kind": kind})

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._wildcard) + sum(len(items) for items in self._subscribers.values())

    def clear(self) -> None:
        with self._lock:
            self._subscribers.clear()
            self._wildcard.clear()

    def kinds(self) -> List[str]:
        with self._lock:
            return sorted(self._subscribers)
