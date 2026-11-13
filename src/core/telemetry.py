"""Structured telemetry shared between the realtime engine and the UI.

The snapshot is an immutable dataclass; the engine publishes a brand new
instance with :meth:`TelemetryHub.set`.  Readers (the Qt timer, the log
exporter) therefore perform one atomic reference read and never see a partially
updated measurement.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, replace
from typing import Any, Deque, Dict, List, Optional, Tuple

from ..config import get_logger, utc_timestamp
from .formats import AudioFormat

LOGGER = get_logger("telemetry")

HISTORY_LENGTH = 480


class HealthState:
    OPTIMAL = "optimal"
    DEGRADED = "degraded"
    FAILING = "failing"
    IDLE = "idle"


@dataclass(frozen=True)
class TelemetrySnapshot:
    """Complete hardware and stream state published at most 10 times/second."""

    state: str = "idle"
    engine_thread: str = "stopped"
    uptime_seconds: float = 0.0

    track_path: str = ""
    track_title: str = ""
    track_artist: str = ""
    track_album: str = ""
    track_container: str = ""
    track_codec: str = ""
    track_notes: Tuple[str, ...] = ()

    source_format: str = ""
    source_bits: int = 0
    source_rate: int = 0
    source_channels: int = 0
    source_float: bool = False
    is_dsd: bool = False

    device_name: str = ""
    device_host_api: str = ""
    device_index: int = -1
    exclusive: bool = False
    driver_label: str = ""
    hardware_clock: int = 0
    hardware_bits: int = 0
    hardware_format: str = ""
    hardware_channels: int = 0
    stream_dtype: str = ""
    latency_ms: float = 0.0
    buffer_ms: float = 0.0
    buffer_fill: float = 0.0
    buffer_capacity_ms: float = 0.0
    period_frames: int = 0

    frames_played: int = 0
    frames_total: int = 0
    position_seconds: float = 0.0
    duration_seconds: float = 0.0

    bit_perfect: bool = False
    bit_perfect_reason: str = "engine idle"
    conversion_chain: str = ""
    dsp_chain: Tuple[str, ...] = ()
    integrity_digest: str = ""
    integrity_bytes_sent: int = 0
    integrity_verified: bool = False

    xruns: int = 0
    underrun_bytes: int = 0
    overruns: int = 0
    ring_discarded: int = 0
    silence_frames: int = 0
    stream_restarts: int = 0
    clock_switches: int = 0

    priority: str = ""
    priority_realtime: bool = False
    cpu_affinity: str = ""
    last_error: str = ""
    device_present: bool = True
    peak_fill: float = 0.0

    def health(self) -> str:
        if self.state in ("idle", "stopped"):
            return HealthState.IDLE
        if self.last_error:
            return HealthState.FAILING
        if self.xruns > 0 or not self.device_present:
            return HealthState.DEGRADED
        if self.bit_perfect:
            return HealthState.OPTIMAL
        return HealthState.DEGRADED

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["health"] = self.health()
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @property
    def progress(self) -> float:
        if self.duration_seconds <= 0:
            return 0.0
        return min(1.0, max(0.0, self.position_seconds / self.duration_seconds))

    @property
    def buffer_health(self) -> str:
        if self.buffer_fill < 0.05:
            return HealthState.FAILING
        if self.buffer_fill < 0.20:
            return HealthState.DEGRADED
        return HealthState.OPTIMAL


EMPTY = TelemetrySnapshot()


class TelemetryHub:
    """Holds the latest snapshot plus a bounded history for the sparkline."""

    def __init__(self, history_length: int = HISTORY_LENGTH) -> None:
        self._lock = threading.Lock()
        self._snapshot: TelemetrySnapshot = EMPTY
        self._fill_history: Deque[float] = deque(maxlen=history_length)
        self._xrun_history: Deque[int] = deque(maxlen=history_length)
        self._latency_history: Deque[float] = deque(maxlen=history_length)
        self._started = time.monotonic()
        self._updates = 0
        self._last_persist = 0.0

    # ------------------------------------------------------------------ write
    def set(self, **changes: Any) -> TelemetrySnapshot:
        with self._lock:
            self._snapshot = replace(self._snapshot, **changes)
            self._updates += 1
            return self._snapshot

    def update(self, snapshot: TelemetrySnapshot) -> None:
        with self._lock:
            self._snapshot = snapshot
            self._updates += 1

    def observe(self, snapshot: TelemetrySnapshot) -> None:
        """Store a snapshot and append history samples."""

        with self._lock:
            previous_peak = self._snapshot.peak_fill
            self._updates += 1
            self._fill_history.append(snapshot.buffer_fill)
            self._xrun_history.append(snapshot.xruns)
            self._latency_history.append(snapshot.latency_ms)
            self._snapshot = replace(
                snapshot,
                uptime_seconds=time.monotonic() - self._started,
                peak_fill=max(previous_peak, snapshot.buffer_fill),
            )

    # ------------------------------------------------------------------- read
    def snapshot(self) -> TelemetrySnapshot:
        return self._snapshot

    def history(self) -> List[float]:
        with self._lock:
            return list(self._fill_history)

    def xrun_history(self) -> List[int]:
        with self._lock:
            return list(self._xrun_history)

    def latency_history(self) -> List[float]:
        with self._lock:
            return list(self._latency_history)

    def updates(self) -> int:
        return self._updates

    def clear_history(self) -> None:
        with self._lock:
            self._fill_history.clear()
            self._xrun_history.clear()
            self._latency_history.clear()

    def export(self) -> str:
        return self.snapshot().to_json()

    def maybe_log(self, force: bool = False, interval: float = 5.0) -> None:
        """Emit a structured telemetry line at most once per interval."""

        now = time.monotonic()
        if not force and now - self._last_persist < interval:
            return
        self._last_persist = now
        payload = self.snapshot().to_dict()
        payload["ts"] = utc_timestamp()
        LOGGER.info("telemetry", extra={"telemetry": payload})


def format_transport_badge(format_: Optional[AudioFormat]) -> str:
    if format_ is None:
        return "No stream"
    return f"{format_.valid_bits}-bit · {format_.sample_rate} Hz"


def describe_dsp_chain() -> Tuple[str, ...]:
    """The complete list of DSP stages in the signal path (deliberately empty)."""

    return ()
