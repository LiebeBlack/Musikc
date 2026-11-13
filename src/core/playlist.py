"""Playlist model and playback controller.

The controller is the only component that knows the queue order; the engine
knows only "current track" and "preloaded next track".  This separation keeps
gapless transitions deterministic: the controller resolves the successor once,
preloads it, and lets the engine splice it into the running stream.
"""

from __future__ import annotations

import json
import random
import threading
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..config import SUPPORTED_EXTENSIONS, get_logger, playlists_directory
from .decoders.base import SourceInfo
from .events import (
    ENGINE_FINISHED,
    ENGINE_TRACK_CHANGED,
    Event,
    EventBus,
    PLAYLIST_CHANGED,
    PLAYLIST_TRACK,
)

LOGGER = get_logger("playlist")


class RepeatMode(str, Enum):
    OFF = "off"
    ONE = "one"
    ALL = "all"

    @classmethod
    def parse(cls, value: str) -> "RepeatMode":
        try:
            return cls((value or "off").lower())
        except ValueError:
            return cls.OFF


@dataclass
class Track:
    """A playlist entry with every field cached from the decoder header."""

    path: str
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float = 0.0
    resolution: str = ""
    container: str = ""
    codec: str = ""
    size_bytes: int = 0
    is_dsd: bool = False
    sample_rate: int = 0
    bit_depth: int = 0
    channels: int = 0
    lossless: bool = True
    notes: List[str] = field(default_factory=list)

    @property
    def filename(self) -> str:
        return Path(self.path).name

    @property
    def display_title(self) -> str:
        return self.title or Path(self.path).stem

    @property
    def display_artist(self) -> str:
        return self.artist or "Unknown artist"

    @classmethod
    def from_source(cls, info: SourceInfo) -> "Track":
        return cls(
            path=str(info.path),
            title=info.title,
            artist=info.artist,
            album=info.album,
            duration=info.duration,
            resolution=info.resolution_text,
            container=info.container,
            codec=info.codec,
            size_bytes=info.file_size,
            is_dsd=info.is_dsd,
            sample_rate=info.sample_rate,
            bit_depth=info.audio_format.valid_bits,
            channels=info.channels,
            lossless=info.lossless,
            notes=list(info.notes),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Track":
        allowed = {name for name in cls.__dataclass_fields__}
        cleaned = {key: value for key, value in data.items() if key in allowed}
        cleaned.setdefault("path", "")
        if not cleaned["path"]:
            raise ValueError("track without path")
        cleaned["notes"] = list(cleaned.get("notes") or [])
        return cls(**cleaned)


class Playlist:
    """Ordered collection of tracks with repeat and shuffle semantics."""

    def __init__(self, name: str = "Session", tracks: Optional[Sequence[Track]] = None) -> None:
        self.name = name
        self._tracks: List[Track] = list(tracks or [])
        self._current = -1
        self._repeat = RepeatMode.OFF
        self._shuffle = False
        self._random = random.Random()
        self._order: List[int] = []
        self._rebuild_order()

    # ------------------------------------------------------------- properties
    @property
    def tracks(self) -> List[Track]:
        return list(self._tracks)

    @property
    def current_index(self) -> int:
        return self._current

    @property
    def current_track(self) -> Optional[Track]:
        if 0 <= self._current < len(self._tracks):
            return self._tracks[self._current]
        return None

    @property
    def repeat_mode(self) -> RepeatMode:
        return self._repeat

    @property
    def shuffle(self) -> bool:
        return self._shuffle

    @property
    def play_order(self) -> List[int]:
        """The resolved queue order (identical to the track order when not shuffled)."""

        return list(self._order)

    def __len__(self) -> int:
        return len(self._tracks)

    def __iter__(self):
        return iter(self._tracks)

    def __getitem__(self, index: int) -> Track:
        return self._tracks[index]

    @property
    def total_duration(self) -> float:
        return sum(track.duration for track in self._tracks)

    # ---------------------------------------------------------------- mutation
    def _rebuild_order(self) -> None:
        self._order = list(range(len(self._tracks)))
        if self._shuffle:
            self._random.shuffle(self._order)

    def add(self, track: Track, index: Optional[int] = None) -> int:
        if index is None or index >= len(self._tracks):
            self._tracks.append(track)
            position = len(self._tracks) - 1
        else:
            position = max(0, index)
            self._tracks.insert(position, track)
        if not self._shuffle:
            self._rebuild_order()
        else:
            self._order.append(len(self._tracks) - 1)
        if self._current < 0:
            self._current = 0 if self._tracks else -1
        return position

    def add_paths(self, paths: Iterable[Path], probe) -> int:
        added = 0
        for path in paths:
            if not path.exists() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            try:
                info = probe(path)
            except Exception as exc:  # noqa: BLE001 - a broken file must not abort the import
                LOGGER.warning("skipping unreadable file", extra={"path": str(path), "error": str(exc)})
                continue
            self.add(Track.from_source(info))
            added += 1
        return added

    def remove(self, index: int) -> Optional[Track]:
        if not 0 <= index < len(self._tracks):
            return None
        removed = self._tracks.pop(index)
        if self._current == index:
            self._current = min(index, len(self._tracks) - 1)
        elif self._current > index:
            self._current -= 1
        self._rebuild_order()
        return removed

    def move(self, source: int, destination: int) -> None:
        if not (0 <= source < len(self._tracks)) or not (0 <= destination < len(self._tracks)):
            return
        track = self._tracks.pop(source)
        self._tracks.insert(destination, track)
        if self._current == source:
            self._current = destination
        elif source < self._current <= destination:
            self._current -= 1
        elif destination <= self._current < source:
            self._current += 1
        self._rebuild_order()

    def clear(self) -> None:
        self._tracks.clear()
        self._current = -1
        self._order = []

    def set_current(self, index: int) -> Optional[Track]:
        if 0 <= index < len(self._tracks):
            self._current = index
            return self._tracks[index]
        return None

    def set_repeat(self, mode: RepeatMode) -> None:
        self._repeat = mode

    def cycle_repeat(self) -> RepeatMode:
        order = [RepeatMode.OFF, RepeatMode.ALL, RepeatMode.ONE]
        self._repeat = order[(order.index(self._repeat) + 1) % len(order)]
        return self._repeat

    def set_shuffle(self, enabled: bool) -> None:
        self._shuffle = bool(enabled)
        self._rebuild_order()

    def toggle_shuffle(self) -> bool:
        self.set_shuffle(not self._shuffle)
        return self._shuffle

    # ------------------------------------------------------------- navigation
    def _position_in_order(self) -> int:
        if not self._order:
            return -1
        try:
            return self._order.index(self._current)
        except ValueError:
            self._rebuild_order()
            try:
                return self._order.index(self._current)
            except ValueError:
                return -1

    def next_index(self, manual: bool = True) -> Optional[int]:
        if not self._tracks:
            return None
        if self._repeat is RepeatMode.ONE and not manual:
            return self._current
        position = self._position_in_order()
        if position < 0:
            return 0
        if position + 1 < len(self._order):
            return self._order[position + 1]
        if self._repeat is RepeatMode.ALL:
            self._rebuild_order()
            return self._order[0] if self._order else None
        return None

    def previous_index(self) -> Optional[int]:
        if not self._tracks:
            return None
        position = self._position_in_order()
        if position < 0:
            return 0
        if position - 1 >= 0:
            return self._order[position - 1]
        if self._repeat is RepeatMode.ALL:
            return self._order[-1]
        return None

    # ------------------------------------------------------------ persistence
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "repeat": self._repeat.value,
            "shuffle": self._shuffle,
            "current": self._current,
            "tracks": [track.to_dict() for track in self._tracks],
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Playlist":
        tracks: List[Track] = []
        for raw in payload.get("tracks") or []:
            try:
                tracks.append(Track.from_dict(raw))
            except (TypeError, ValueError):
                continue
        playlist = cls(str(payload.get("name") or "Session"), tracks)
        playlist.set_repeat(RepeatMode.parse(str(payload.get("repeat") or "off")))
        playlist.set_shuffle(bool(payload.get("shuffle")))
        current = int(payload.get("current", -1) or -1)
        playlist._current = current if 0 <= current < len(tracks) else (0 if tracks else -1)
        return playlist

    def save(self, path: Optional[Path] = None) -> Path:
        destination = path or (playlists_directory() / f"{self.name or 'session'}.json".replace(" ", "_"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(destination)
        return destination

    @classmethod
    def load(cls, path: Path) -> "Playlist":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(payload)


class PlaybackController:
    """Glue between the playlist model and the audio engine."""

    def __init__(self, engine, bus: EventBus, playlist: Optional[Playlist] = None) -> None:
        self._engine = engine
        self._bus = bus
        self._playlist = playlist or Playlist()
        self._lock = threading.RLock()
        bus.subscribe(ENGINE_FINISHED, self._on_engine_finished)
        bus.subscribe(ENGINE_TRACK_CHANGED, self._on_engine_track_changed)

    @property
    def playlist(self) -> Playlist:
        return self._playlist

    def set_playlist(self, playlist: Playlist) -> None:
        with self._lock:
            self._playlist = playlist
        self._notify_changed()

    # ----------------------------------------------------------------- control
    def play_index(self, index: int) -> None:
        with self._lock:
            track = self._playlist.set_current(index)
        if track is None:
            return
        self._bus.publish(PLAYLIST_TRACK, index=index, path=track.path)
        # Preload first: the engine must know the successor before a very short
        # track can reach its end, otherwise the queue would stop.
        self._preload_successor()
        self._engine.load(track.path, autoplay=True)

    def play_path(self, path: str | Path) -> None:
        target = str(path)
        for index, track in enumerate(self._playlist.tracks):
            if track.path == target:
                self.play_index(index)
                return
        self._playlist.add(Track(path=target, title=Path(target).stem))
        self.play_index(len(self._playlist) - 1)

    def toggle(self) -> None:
        track = self._playlist.current_track
        if track is None and len(self._playlist):
            self.play_index(0)
            return
        self._engine.toggle()

    def next(self) -> None:
        index = self._playlist.next_index(manual=True)
        if index is None:
            self._engine.stop()
            return
        self.play_index(index)

    def previous(self) -> None:
        index = self._playlist.previous_index()
        if index is None:
            self._engine.seek(0.0)
            return
        self.play_index(index)

    def stop(self) -> None:
        self._engine.stop()

    def seek(self, seconds: float) -> None:
        self._engine.seek(seconds)

    def set_repeat(self, mode: RepeatMode) -> None:
        self._playlist.set_repeat(mode)
        self._notify_changed()
        self._preload_successor()

    def cycle_repeat(self) -> RepeatMode:
        mode = self._playlist.cycle_repeat()
        self._notify_changed()
        self._preload_successor()
        return mode

    def toggle_shuffle(self) -> bool:
        enabled = self._playlist.toggle_shuffle()
        self._notify_changed()
        self._preload_successor()
        return enabled

    # ------------------------------------------------------------------- queue
    def add_paths(self, paths: Iterable[Path], probe) -> int:
        added = self._playlist.add_paths(paths, probe)
        if added:
            self._notify_changed()
        return added

    def remove_index(self, index: int) -> None:
        self._playlist.remove(index)
        self._notify_changed()

    def clear(self) -> None:
        self._playlist.clear()
        self._notify_changed()

    def move(self, source: int, destination: int) -> None:
        self._playlist.move(source, destination)
        self._notify_changed()

    # ---------------------------------------------------------------- internals
    def _preload_successor(self) -> None:
        with self._lock:
            index = self._playlist.next_index(manual=False)
            if index is None:
                self._engine.set_next_track("")
                return
            track = self._playlist.tracks[index]
        # Repeat-one preloads the same file so the engine can loop it without
        # closing the DAC stream.
        self._engine.set_next_track(track.path)

    def _on_engine_finished(self, event: Event) -> None:
        with self._lock:
            index = self._playlist.next_index(manual=False)
            if index is None:
                LOGGER.debug("queue finished")
                return
            track = self._playlist.set_current(index)
        if track is None:
            return
        self._bus.publish(PLAYLIST_TRACK, index=index, path=track.path)
        self._preload_successor()
        self._engine.load(track.path, autoplay=True)

    def _on_engine_track_changed(self, event: Event) -> None:
        """Keep the queue index in sync with whatever the engine is playing.

        This covers both gapless splices and the automatic reopen performed when
        the next track needs a different hardware format.
        """

        path = str(event.get("path") or "")
        gapless = bool(event.get("gapless"))
        for index, track in enumerate(self._playlist.tracks):
            if track.path == path:
                with self._lock:
                    changed = index != self._playlist.current_index
                    self._playlist.set_current(index)
                self._bus.publish(PLAYLIST_TRACK, index=index, path=path, gapless=gapless)
                if changed or gapless:
                    self._preload_successor()
                return

    def _notify_changed(self) -> None:
        self._bus.publish(
            PLAYLIST_CHANGED,
            count=len(self._playlist),
            current=self._playlist.current_index,
            repeat=self._playlist.repeat_mode.value,
            shuffle=self._playlist.shuffle,
            duration=self._playlist.total_duration,
        )
