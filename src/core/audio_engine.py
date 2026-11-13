"""Bit-perfect playback engine.

Thread topology
---------------

``supervisor``  Control plane: command queue, device hot-plug detection,
                telemetry publication.  Never touches the audio clock.
``feeder``      Data plane: decodes, repacks (lossless only), fills the ring
                buffer.  Runs with MMCSS "Pro Audio" + TIME_CRITICAL priority.
``PortAudio``   Driver callback: locked to the DAC clock.  It only copies bytes
                from the ring buffer into the driver buffer; it takes no lock,
                performs no allocation of consequence and never raises.

The output stream is a ``RawOutputStream`` so the exact bytes of the source
container reach the driver: no float conversion by PortAudio, no clipping, no
dithering (``paClipOff`` and ``paDitherOff`` are explicitly enabled, which in
PortAudio means "do not touch the samples").
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import get_logger
from .decoders.base import DecodeError, Decoder
from .decoders.registry import open_decoder
from .devices import (
    AudioBackendUnavailable,
    DeviceCapabilities,
    DeviceInfo,
    DeviceManager,
    require_backend,
    backend_available,
)
from .dsd import DopEncoder
from .events import (
    ENGINE_DEVICE_LOST,
    ENGINE_DEVICE_RESTORED,
    ENGINE_ERROR,
    ENGINE_FINISHED,
    ENGINE_INTEGRITY,
    ENGINE_STATE,
    ENGINE_STREAM_OPENED,
    ENGINE_TRACK_CHANGED,
    ENGINE_XRUN,
    EventBus,
)
from .formats import AudioFormat, LossyConversionError, SampleFormat, repack
from .integrity import IntegrityMonitor, IntegrityReport
from .priority import AudioThreadGuard, current_priority_state, disable_thread_priority_boost
from .ring_buffer import RingBuffer, RingBufferClosed
from .telemetry import TelemetryHub, TelemetrySnapshot, describe_dsp_chain

LOGGER = get_logger("engine")

DEFAULT_RING_MS = 1200
DEFAULT_TARGET_FILL_MS = 900
MIN_RING_BYTES = 1 << 16
MAX_RING_BYTES = 96 << 20
SUPERVISOR_TICK = 0.05
HOUSEKEEPING_INTERVAL = 0.20
DEVICE_POLL_INTERVAL = 1.20
XRUN_EVENT_THROTTLE = 1.0


class EngineState(str, Enum):
    IDLE = "idle"
    OPENING = "opening"
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"
    DRAINING = "draining"
    ERROR = "error"
    NO_DEVICE = "no_device"
    CLOSED = "closed"


class EngineError(RuntimeError):
    """Base class for engine failures surfaced to the UI."""


class ClockLockError(EngineError):
    """The DAC refused the source clock: playback must not resample."""


class DeviceLostError(EngineError):
    """The selected DAC disappeared from the bus."""


class NoDeviceError(EngineError):
    """No usable output device is present."""


@dataclass
class EngineConfig:
    device_index: int = -1
    exclusive: bool = True
    prefer_asio: bool = True
    buffer_ms: int = 24
    period_frames: int = 0
    ring_ms: int = DEFAULT_RING_MS
    target_fill_ms: int = DEFAULT_TARGET_FILL_MS
    allow_degraded: bool = False
    recover_on_reconnect: bool = True
    verify_integrity: bool = True
    feeder_chunk_ms: int = 10


@dataclass(frozen=True)
class Command:
    kind: str
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EngineStatus:
    state: EngineState
    device: Optional[DeviceInfo]
    source: Optional[str]
    format: Optional[AudioFormat]
    position_seconds: float
    duration_seconds: float
    bit_perfect: bool
    reason: str


class AudioEngine:
    """The whole playback machine behind a thread-safe, non-blocking API."""

    def __init__(
        self,
        devices: Optional[DeviceManager] = None,
        telemetry: Optional[TelemetryHub] = None,
        bus: Optional[EventBus] = None,
        config: Optional[EngineConfig] = None,
    ) -> None:
        self._devices = devices or DeviceManager()
        self._telemetry = telemetry or TelemetryHub()
        self._bus = bus or EventBus()
        self._config = config or EngineConfig()

        self._commands: "queue.Queue[Command]" = queue.Queue()
        self._lock = threading.RLock()
        self._state = EngineState.IDLE
        self._shutdown = threading.Event()
        self._supervisor: Optional[threading.Thread] = None

        self._decoder: Optional[Decoder] = None
        self._next_decoder: Optional[Decoder] = None
        self._next_lock = threading.Lock()
        self._device: Optional[DeviceInfo] = None
        self._capabilities: Optional[DeviceCapabilities] = None
        self._stream: Any = None
        self._ring: Optional[RingBuffer] = None
        self._feeder: Optional[threading.Thread] = None
        self._feeder_stop = threading.Event()
        self._interrupt = threading.Event()

        self._source_format: Optional[AudioFormat] = None
        self._target_format: Optional[AudioFormat] = None
        self._dop_encoder: Optional[DopEncoder] = None
        self._integrity = IntegrityMonitor()
        self._stream_dtype = ""
        self._effective_exclusive = bool((config or EngineConfig()).exclusive)

        self._frames_sent = 0
        self._frames_total = 0
        self._xruns = 0
        self._silence_frames = 0
        self._overruns = 0
        self._callback_errors = 0
        self._stream_restarts = 0
        self._clock_switches = 0
        self._last_xrun_event = 0.0
        self._eof = False
        self._eof_action = "drain"
        self._suppress_xrun = False
        self._callback_priority_ready = False
        self._device_lost = False
        self._lost_device_name = ""
        self._last_error = ""
        self._conversion_note = ""
        self._conversion_exact = True
        self._gapless_transition = False
        self._resume_after_recovery = False
        self._last_housekeeping = 0.0
        self._last_device_poll = 0.0
        self._signature: Tuple[str, ...] = ()
        self._started_at = time.monotonic()
        self._pending_autoplay = False
        self._report_dirty = False

    # ------------------------------------------------------------------ setup
    def start(self) -> None:
        """Start the supervisor thread (idempotent)."""

        with self._lock:
            if self._supervisor is not None and self._supervisor.is_alive():
                return
            self._shutdown.clear()
            self._supervisor = threading.Thread(
                target=self._supervisor_loop, name="truehq-supervisor", daemon=True
            )
            self._supervisor.start()
        self._publish_telemetry(force=True)
        LOGGER.info("engine started", extra={"backend": backend_available()})

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop playback, join every thread and release the DAC."""

        self._shutdown.set()
        self._interrupt.set()
        self._commands.put(Command("shutdown"))
        supervisor = self._supervisor
        if supervisor is not None and supervisor.is_alive():
            supervisor.join(timeout=timeout)
        self._teardown_playback(keep_stream=False)
        self._close_sources()
        with self._lock:
            self._state = EngineState.CLOSED
        LOGGER.info("engine shut down")

    def _close_sources(self) -> None:
        """Release every decoder handle (Windows keeps files locked otherwise)."""

        with self._next_lock:
            pending = self._next_decoder
            self._next_decoder = None
        current = self._decoder
        self._decoder = None
        for decoder in (current, pending):
            if decoder is not None:
                try:
                    decoder.close()
                except Exception:
                    LOGGER.warning("decoder close failed", extra={"decoder": repr(decoder)})

    # ------------------------------------------------------------- public API
    @property
    def bus(self) -> EventBus:
        return self._bus

    @property
    def telemetry(self) -> TelemetryHub:
        return self._telemetry

    @property
    def devices(self) -> DeviceManager:
        return self._devices

    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def config(self) -> EngineConfig:
        return self._config

    def snapshot(self) -> TelemetrySnapshot:
        return self._telemetry.snapshot()

    def integrity_report(self) -> IntegrityReport:
        return self._integrity.report()

    def status(self) -> EngineStatus:
        snapshot = self._telemetry.snapshot()
        return EngineStatus(
            state=self._state,
            device=self._device,
            source=snapshot.track_path or None,
            format=self._target_format,
            position_seconds=snapshot.position_seconds,
            duration_seconds=snapshot.duration_seconds,
            bit_perfect=snapshot.bit_perfect,
            reason=snapshot.bit_perfect_reason,
        )

    def configure(self, **changes: Any) -> None:
        self._commands.put(Command("configure", dict(changes)))

    def load(self, path: str | Path, autoplay: bool = True, gapless: bool = False) -> None:
        self._commands.put(Command("load", {"path": str(path), "autoplay": autoplay, "gapless": gapless}))

    def play(self) -> None:
        self._commands.put(Command("play"))

    def pause(self) -> None:
        self._commands.put(Command("pause"))

    def toggle(self) -> None:
        self._commands.put(Command("toggle"))

    def stop(self) -> None:
        self._commands.put(Command("stop"))

    def seek(self, seconds: float) -> None:
        self._commands.put(Command("seek", {"seconds": float(seconds)}))

    def seek_relative(self, delta_seconds: float) -> None:
        self._commands.put(Command("seek_relative", {"delta": float(delta_seconds)}))

    def set_device(self, device_index: int, exclusive: Optional[bool] = None) -> None:
        payload = {"device_index": int(device_index)}
        if exclusive is not None:
            payload["exclusive"] = bool(exclusive)
        self._commands.put(Command("set_device", payload))

    def set_next_track(self, path: Optional[str | Path]) -> None:
        self._commands.put(Command("preload_next", {"path": str(path) if path else ""}))

    def refresh_devices(self) -> List[DeviceInfo]:
        try:
            return self._devices.refresh(force=True)
        except AudioBackendUnavailable as exc:
            LOGGER.warning("device refresh failed", extra={"error": str(exc)})
            return []

    def enumerate_capabilities(self, device_index: int, exclusive: Optional[bool] = None) -> Optional[DeviceCapabilities]:
        device = self._devices.device(device_index)
        if device is None:
            return None
        try:
            return self._devices.capabilities(device, exclusive=exclusive, reuse_cache=False)
        except AudioBackendUnavailable as exc:
            LOGGER.warning("capability probe failed", extra={"error": str(exc)})
            return None

    # -------------------------------------------------------------- supervisor
    def _supervisor_loop(self) -> None:
        LOGGER.debug("supervisor thread online")
        while not self._shutdown.is_set():
            command: Optional[Command] = None
            try:
                command = self._commands.get(timeout=SUPERVISOR_TICK)
            except queue.Empty:
                command = None
            if command is not None:
                try:
                    self._dispatch(command)
                except EngineError as exc:
                    self._fail(str(exc))
                except Exception as exc:  # pragma: no cover - last line of defence
                    LOGGER.exception("unhandled engine command failure", extra={"command": command.kind})
                    self._fail(f"{type(exc).__name__}: {exc}")
            try:
                self._housekeeping()
            except Exception:  # pragma: no cover
                LOGGER.exception("housekeeping failure")
        LOGGER.debug("supervisor thread offline")

    def _dispatch(self, command: Command) -> None:
        kind = command.kind
        if kind == "shutdown":
            return
        if kind == "configure":
            self._apply_configuration(command.payload)
            return
        if kind == "load":
            self._cmd_load(command.payload["path"], command.payload.get("autoplay", True), command.payload.get("gapless", False))
            return
        if kind == "play":
            self._cmd_play()
            return
        if kind == "pause":
            self._cmd_pause()
            return
        if kind == "toggle":
            if self._state is EngineState.PLAYING:
                self._cmd_pause()
            else:
                self._cmd_play()
            return
        if kind == "stop":
            self._cmd_stop()
            return
        if kind == "seek":
            self._cmd_seek(float(command.payload.get("seconds", 0.0)))
            return
        if kind == "seek_relative":
            position = self._telemetry.snapshot().position_seconds
            self._cmd_seek(position + float(command.payload.get("delta", 0.0)))
            return
        if kind == "set_device":
            self._cmd_set_device(int(command.payload["device_index"]), command.payload.get("exclusive"))
            return
        if kind == "preload_next":
            self._cmd_preload_next(command.payload.get("path") or "")
            return
        LOGGER.warning("unknown engine command", extra={"kind": kind})

    def _apply_configuration(self, changes: Dict[str, Any]) -> None:
        restart_required = False
        for key, value in changes.items():
            if not hasattr(self._config, key):
                continue
            if key in ("device_index", "exclusive", "buffer_ms", "period_frames", "ring_ms", "allow_degraded"):
                restart_required = True
            setattr(self._config, key, value)
        LOGGER.info("engine reconfigured", extra={"changes": list(changes)})
        if restart_required and self._decoder is not None and self._state is EngineState.PLAYING:
            self._reopen_for_config_change()

    def _reopen_for_config_change(self) -> None:
        position = self._telemetry.snapshot().position_seconds
        was_playing = self._state is EngineState.PLAYING
        self._teardown_playback(keep_stream=False)
        if was_playing:
            self._cmd_play()
            self._cmd_seek(position)

    # ----------------------------------------------------------------- command
    def _cmd_load(self, path: str, autoplay: bool, gapless: bool) -> None:
        resolved = Path(path)
        LOGGER.info("loading track", extra={"path": str(resolved), "gapless": gapless})
        try:
            decoder = open_decoder(resolved)
        except DecodeError as exc:
            self._fail(f"cannot open {resolved.name}: {exc}")
            return

        info = decoder.info
        same_transport = (
            gapless
            and self._target_format is not None
            and self._stream is not None
            and self._stream.active
            and info.transport_format.sample_rate == self._target_format.sample_rate
            and info.transport_format.channels == self._target_format.channels
            and info.transport_format.sample_format is self._target_format.sample_format
            and self._ring is not None
        )
        if same_transport:
            old = self._decoder
            self._decoder = decoder
            self._decode_state_reset(info)
            if old is not None:
                old.close()
            self._gapless_transition = True
            self._suppress_xrun = True
            self._publish_track(info, gapless=True)
            self._state = EngineState.PLAYING if self._stream is not None and self._stream.active else EngineState.PAUSED
            self._publish_telemetry(force=True)
            self._bus.publish(ENGINE_STATE, state=self._state.value, gapless=True)
            return

        self._teardown_playback(keep_stream=False)
        self._decoder = decoder
        self._decode_state_reset(info)
        self._publish_track(info, gapless=False)
        # Remember the intent before negotiating: if the DAC cannot lock the
        # clock, switching to a capable device must resume automatically.
        self._pending_autoplay = autoplay

        try:
            prepared = self._prepare_stream(info)
        except EngineError as exc:
            self._fail(str(exc))
            return
        if not prepared:
            return
        if autoplay:
            self._start_transport()
        else:
            self._state = EngineState.STOPPED
            self._bus.publish(ENGINE_STATE, state=self._state.value)
        self._publish_telemetry(force=True)

    def _decode_state_reset(self, info) -> None:
        self._source_format = info.transport_format
        self._frames_total = self._frames_total_for(info)
        self._frames_sent = 0
        self._eof = False
        self._eof_action = "drain"
        self._dop_encoder = DopEncoder(info.dsd_format) if info.dsd_format else None
        self._integrity = IntegrityMonitor()
        if self._target_format is not None and self._stream is not None:
            self._integrity.begin(
                self._source_format,
                self._target_format,
                self._driver_label(),
                self._conversion_note or "container passthrough",
                self._conversion_exact,
            )

    @staticmethod
    def _frames_total_for(info) -> int:
        if info.dsd_format is not None:
            return info.total_frames // 16
        return info.total_frames

    def _take_next_decoder(self) -> Optional[Decoder]:
        """Atomically claim the preloaded decoder (feeder vs. supervisor)."""

        with self._next_lock:
            decoder = self._next_decoder
            self._next_decoder = None
            return decoder

    def _cmd_preload_next(self, path: str) -> None:
        with self._next_lock:
            previous = self._next_decoder
            self._next_decoder = None
        if previous is not None:
            previous.close()
        if not path:
            return
        try:
            decoder = open_decoder(Path(path))
        except DecodeError as exc:
            LOGGER.warning("cannot preload next track", extra={"path": path, "error": str(exc)})
            return
        with self._next_lock:
            stale = self._next_decoder
            self._next_decoder = decoder
        if stale is not None:
            stale.close()
        LOGGER.debug("next track preloaded", extra={"path": path})

    def _cmd_play(self) -> None:
        if self._decoder is None:
            LOGGER.debug("play ignored: no track loaded")
            return
        if self._state is EngineState.PLAYING:
            return
        self._last_error = ""
        if self._decoder.eof:
            # Pressing play after the last sample restarts the track instead of
            # producing an immediate end-of-stream.
            try:
                self._decoder.seek(0)
            except DecodeError:
                pass
            self._eof = False
            if self._ring is not None:
                self._ring.drain()
            if self._dop_encoder is not None:
                self._dop_encoder.reset()
            self._integrity = IntegrityMonitor()
            if self._target_format is not None and self._source_format is not None:
                self._integrity.begin(
                    self._source_format,
                    self._target_format,
                    self._driver_label(),
                    self._conversion_note or "container passthrough",
                    self._conversion_exact,
                )
        needs_rebuild = (
            self._stream is None
            or self._ring is None
            or self._target_format is None
            or self._stream.closed
        )
        if needs_rebuild:
            info = self._decoder.info
            try:
                prepared = self._prepare_stream(info)
            except EngineError as exc:
                self._fail(str(exc))
                return
            if not prepared:
                return
        if self._feeder is None or not self._feeder.is_alive():
            self._start_feeder()
        self._ensure_prefill()
        try:
            self._stream.start()
        except Exception as exc:
            self._fail(f"driver refused to start the stream: {exc}")
            return
        self._state = EngineState.PLAYING
        self._bus.publish(ENGINE_STATE, state=self._state.value)
        LOGGER.info("playback started", extra={"rate": self._target_format.sample_rate if self._target_format else 0})
        self._publish_telemetry(force=True)

    def _cmd_pause(self) -> None:
        if self._state is not EngineState.PLAYING:
            return
        self._stop_stream()
        self._state = EngineState.PAUSED
        self._bus.publish(ENGINE_STATE, state=self._state.value)
        self._publish_telemetry(force=True)
        LOGGER.info("playback paused")

    def _cmd_stop(self) -> None:
        if self._state in (EngineState.IDLE, EngineState.CLOSED):
            return
        self._stop_stream()
        self._teardown_playback(keep_stream=False)
        if self._decoder is not None:
            try:
                self._decoder.seek(0)
            except DecodeError:
                pass
        self._frames_sent = 0
        self._state = EngineState.STOPPED
        self._bus.publish(ENGINE_STATE, state=self._state.value)
        self._publish_telemetry(force=True)
        LOGGER.info("playback stopped")

    def _cmd_seek(self, seconds: float) -> None:
        if self._decoder is None:
            return
        info = self._decoder.info
        duration = info.duration
        target = max(0.0, min(float(seconds), duration))
        was_playing = self._state is EngineState.PLAYING
        if was_playing:
            self._stop_stream()
            self._state = EngineState.PAUSED
            self._bus.publish(ENGINE_STATE, state=self._state.value)
        target_frame = int(target * info.sample_rate)
        if info.dsd_format is not None:
            target_frame = (target_frame // 16) * 16
        try:
            achieved = self._decoder.seek(target_frame)
        except DecodeError as exc:
            self._fail(f"seek failed: {exc}")
            return
        if self._ring is not None:
            self._ring.drain()
        self._frames_sent = 0
        self._eof = False
        self._suppress_xrun = True
        self._integrity = IntegrityMonitor()
        if self._target_format is not None and self._source_format is not None:
            self._integrity.begin(
                self._source_format,
                self._target_format,
                self._driver_label(),
                self._conversion_note or "container passthrough",
                self._conversion_exact,
            )
        if self._dop_encoder is not None:
            self._dop_encoder.reset()
        position_seconds = (achieved / info.sample_rate) if info.sample_rate else 0.0
        LOGGER.info("seek completed", extra={"seconds": round(position_seconds, 3)})
        if was_playing:
            self._ensure_prefill()
            try:
                self._stream.start()
            except Exception as exc:
                self._fail(f"driver refused to restart after seek: {exc}")
                return
            self._state = EngineState.PLAYING
            self._bus.publish(ENGINE_STATE, state=self._state.value)
        self._publish_telemetry(force=True)

    def _cmd_set_device(self, device_index: int, exclusive: Optional[bool]) -> None:
        if exclusive is not None:
            self._config.exclusive = bool(exclusive)
        self._config.device_index = int(device_index)
        device = self._devices.device(device_index)
        if device is None:
            self._fail(f"output device {device_index} is not present")
            return
        self._devices.invalidate(device_index)
        was_playing = self._state is EngineState.PLAYING
        position = self._telemetry.snapshot().position_seconds
        self._teardown_playback(keep_stream=False)
        self._device = device
        LOGGER.info(
            "output device selected",
            extra={"device": device.name, "host": device.host_api_name, "exclusive": self._config.exclusive},
        )
        if self._decoder is not None:
            try:
                prepared = self._prepare_stream(self._decoder.info)
            except EngineError as exc:
                self._fail(str(exc))
                return
            if prepared and was_playing:
                self._cmd_seek(position)
                self._cmd_play()
            elif prepared and self._pending_autoplay:
                self._cmd_play()
        self._publish_telemetry(force=True)

    # ------------------------------------------------------------------ stream
    def _select_device(self) -> DeviceInfo:
        if self._config.device_index >= 0:
            device = self._devices.device(self._config.device_index)
            if device is not None:
                return device
            LOGGER.warning(
                "configured device missing, falling back to automatic selection",
                extra={"device_index": self._config.device_index},
            )
        device = self._devices.best_device(prefer_asio=self._config.prefer_asio)
        if device is None:
            raise NoDeviceError("no audio output device is available")
        self._config.device_index = device.index
        return device

    def _driver_label(self) -> str:
        if self._device is None:
            return "unknown driver"
        device = self._device
        if device.is_asio:
            return "ASIO (exclusive)"
        if device.is_wasapi:
            return "WASAPI Exclusive" if self._effective_exclusive else "WASAPI Shared"
        if device.host_kind == "wdm-ks":
            return "WDM-KS (kernel streaming)" if self._effective_exclusive else "WDM-KS (shared)"
        return f"{device.driver_label} (system mixer)"

    def _prepare_stream(self, info) -> bool:
        self._close_stream()
        if self._ring is not None:
            self._ring.close()
            self._ring = None
        if not backend_available():
            raise AudioBackendUnavailable(
                "PortAudio backend is unavailable. Install the dependencies with setup_env.py."
            )
        backend = require_backend()
        device = self._select_device()
        self._device = device
        self._effective_exclusive = bool(self._config.exclusive and device.exclusive_capable)
        source_transport = info.transport_format
        self._state = EngineState.OPENING
        self._bus.publish(ENGINE_STATE, state=self._state.value)

        capability = self._devices.capabilities(device, exclusive=self._config.exclusive)
        self._capabilities = capability
        target, notes = self._devices.choose_transport(
            device, source_transport, self._config.exclusive, self._config.allow_degraded
        )
        for note in notes:
            LOGGER.warning("transport negotiation", extra={"note": note, "device": device.name})
        if target is None:
            self._state = EngineState.ERROR
            detail = "; ".join(notes) or "no compatible hardware format"
            raise ClockLockError(
                f"{device.name} cannot accept {source_transport.label} without resampling. {detail}"
            )

        device_buffer_ms = max(8, int(self._config.buffer_ms))
        blocksize = int(self._config.period_frames) if self._config.period_frames else 0
        latency_hint = device_buffer_ms / 1000.0
        attempts: List[Tuple[SampleFormat, bool]] = [(target.sample_format, False)]
        if target.sample_format is not SampleFormat.INT32:
            attempts.append((SampleFormat.INT32, True))

        last_error = ""
        for sample_format, fallback in attempts:
            prepared = target.with_format(sample_format, sample_format.bits)
            extra = self._devices.extra_settings(device, self._effective_exclusive) if self._effective_exclusive else None
            try:
                stream = backend.RawOutputStream(
                    device=device.index,
                    samplerate=prepared.sample_rate,
                    channels=prepared.channels,
                    dtype=prepared.sample_format.portaudio_dtype,
                    callback=self._callback,
                    blocksize=blocksize,
                    latency=latency_hint,
                    extra_settings=extra,
                    clip_off=True,
                    dither_off=True,
                    prime_output_buffers_using_stream_callback=False,
                )
            except Exception as exc:
                last_error = str(exc)
                LOGGER.warning(
                    "stream open failed",
                    extra={"format": prepared.short_label, "device": device.name, "error": last_error},
                )
                continue
            achieved_rate = int(round(float(stream.samplerate)))
            achieved_channels = int(stream.channels)
            if achieved_rate != prepared.sample_rate or achieved_channels != prepared.channels:
                try:
                    stream.close()
                except Exception:
                    pass
                last_error = (
                    f"driver accepted {achieved_rate} Hz / {achieved_channels} ch instead of "
                    f"{prepared.sample_rate} Hz / {prepared.channels} ch (silent resampling)"
                )
                LOGGER.error("clock lock refused", extra={"detail": last_error})
                continue
            self._stream = stream
            self._target_format = prepared
            self._stream_dtype = prepared.sample_format.portaudio_dtype
            if prepared.sample_format is not target.sample_format:
                self._conversion_note = (
                    f"container widened to {prepared.sample_format.code} to satisfy the driver"
                )
            else:
                self._conversion_note = f"{source_transport.short_label} -> {prepared.short_label}"
            break
        else:
            self._state = EngineState.ERROR
            raise ClockLockError(
                f"cannot open {source_transport.label} on {device.name}: {last_error or 'driver error'}"
            )

        assert self._target_format is not None
        self._ring = RingBuffer(
            capacity_bytes=self._ring_capacity(self._target_format),
            frame_bytes=self._target_format.frame_bytes,
            name="pcm",
        )
        self._frames_total = self._frames_total_for(info)
        self._frames_sent = 0
        self._eof = False
        self._xruns = 0
        self._silence_frames = 0
        self._overruns = 0
        self._callback_priority_ready = False
        self._integrity = IntegrityMonitor()
        self._integrity.begin(
            source_transport,
            self._target_format,
            self._driver_label(),
            self._conversion_note,
            True,
        )
        self._conversion_exact = True
        if not self._effective_exclusive and not device.is_asio:
            self._conversion_exact = False
            self._conversion_note = "shared-mode mixer (operating system may resample)"
        self._integrity.note_conversion(self._conversion_exact, self._conversion_note)
        self._last_error = ""
        self._state = EngineState.STOPPED
        self._bus.publish(
            ENGINE_STREAM_OPENED,
            device=device.name,
            host=device.host_api_name,
            driver=self._driver_label(),
            rate=self._target_format.sample_rate,
            bits=self._target_format.valid_bits,
            dtype=self._stream_dtype,
            exclusive=self._effective_exclusive,
            latency_ms=self._stream_latency_ms(),
            channels=self._target_format.channels,
        )
        LOGGER.info(
            "output stream opened",
            extra={
                "device": device.name,
                "host": device.host_api_name,
                "format": self._target_format.describe(),
                "dtype": self._stream_dtype,
                "exclusive": self._config.exclusive,
                "latency_ms": round(self._stream_latency_ms(), 2),
            },
        )
        self._publish_telemetry(force=True)
        return True

    def _ring_capacity(self, format: AudioFormat) -> int:
        desired = int(format.bytes_per_second * max(self._config.ring_ms, 100) / 1000.0)
        return max(MIN_RING_BYTES, min(desired, MAX_RING_BYTES))

    def _stream_latency_ms(self) -> float:
        if self._stream is None:
            return 0.0
        try:
            latency = self._stream.latency
            if isinstance(latency, (tuple, list)):
                return float(latency[-1]) * 1000.0
            return float(latency) * 1000.0
        except Exception:
            return 0.0

    def _stop_stream(self) -> None:
        if self._stream is None:
            return
        try:
            if self._stream.active:
                self._stream.stop()
        except Exception as exc:
            LOGGER.warning("stream stop failed", extra={"error": str(exc)})

    def _close_stream(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            if stream.active:
                stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception as exc:
            LOGGER.warning("stream close failed", extra={"error": str(exc)})

    def _teardown_playback(self, keep_stream: bool = False) -> None:
        self._stop_feeder()
        if not keep_stream:
            self._close_stream()
        if self._ring is not None:
            self._ring.close()
            self._ring = None
        if self._dop_encoder is not None:
            self._dop_encoder.reset()
        self._eof = False
        self._eof_action = "drain"
        self._suppress_xrun = False

    # ------------------------------------------------------------------ feeder
    def _start_feeder(self) -> None:
        if self._feeder is not None and self._feeder.is_alive():
            return
        self._feeder_stop = threading.Event()
        self._feeder = threading.Thread(target=self._feeder_loop, name="truehq-feeder", daemon=True)
        self._feeder.start()

    def _stop_feeder(self) -> None:
        self._feeder_stop.set()
        feeder = self._feeder
        self._feeder = None
        if (
            feeder is not None
            and feeder.is_alive()
            and feeder is not threading.current_thread()
        ):
            feeder.join(timeout=2.0)

    def _feeder_aborted(self) -> bool:
        return self._feeder_stop.is_set() or self._shutdown.is_set()

    def _chunk_frames(self) -> int:
        assert self._target_format is not None
        milliseconds = max(2, min(50, self._config.feeder_chunk_ms))
        frames = int(self._target_format.sample_rate * milliseconds / 1000)
        return max(256, frames)

    def _feeder_loop(self) -> None:
        with AudioThreadGuard("truehq-feeder", realtime=True, mmcss=True):
            LOGGER.debug("feeder thread online", extra={"priority": current_priority_state().summary()})
            while not self._feeder_stop.is_set() and not self._shutdown.is_set():
                ring = self._ring
                decoder = self._decoder
                target = self._target_format
                if ring is None or decoder is None or target is None:
                    time.sleep(0.005)
                    continue
                if ring.closed:
                    time.sleep(0.005)
                    continue
                if self._eof:
                    if ring.available_bytes == 0:
                        if self._eof_action == "drain":
                            self._finish_playback()
                        return
                    time.sleep(0.005)
                    continue
                chunk_bytes = self._chunk_frames() * target.frame_bytes
                if ring.free_bytes < chunk_bytes:
                    time.sleep(0.002)
                    continue
                try:
                    produced = self._produce_once(ring, decoder, target)
                except RingBufferClosed:
                    return
                except LossyConversionError as exc:
                    self._conversion_exact = False
                    self._conversion_note = exc.reason
                    self._integrity.note_conversion(False, exc.reason)
                    LOGGER.error("lossy conversion stopped", extra={"reason": exc.reason})
                    self._fail(f"refusing lossy conversion: {exc.reason}")
                    return
                except DecodeError as exc:
                    self._fail(f"decode error: {exc}")
                    return
                except Exception as exc:  # pragma: no cover - defensive
                    self._fail(f"feeder failure: {type(exc).__name__}: {exc}")
                    LOGGER.exception("feeder failure")
                    return
                if produced < 0:
                    return
                if produced == 0:
                    if self._next_decoder is not None and self._next_is_compatible():
                        # Splice while the tail is still buffered: waiting for a
                        # completely empty ring would insert a silent period.
                        threshold = max(
                            target.frame_bytes * 256,
                            int(target.bytes_per_second * 0.05),
                        )
                        if ring.available_bytes > threshold:
                            time.sleep(0.002)
                            continue
                        self._advance_after_eof()
                        continue
                    self._eof_action = self._advance_after_eof()
                    self._eof = True

    def _produce_once(self, ring: RingBuffer, decoder: Decoder, target: AudioFormat) -> int:
        info = decoder.info
        if info.dsd_format is not None:
            bits = self._chunk_frames() * 16
            payloads = decoder.read_dsd(bits)
            if not payloads:
                return 0
            encoder = self._dop_encoder
            if encoder is None:
                raise EngineError("DoP encoder missing for a DSD source")
            encoded = encoder.encode(payloads)
            frames = len(encoded) // target.frame_bytes
            self._integrity.record_source(b"".join(payloads))
            self._integrity.record_output(encoded, frames)
            written = ring.write_blocking(encoded, abort=self._feeder_aborted)
            if written < len(encoded):
                return -1
            self._frames_sent += frames
            return len(payloads[0]) or 1
        source = self._source_format
        if source is None:
            raise EngineError("source format lost")
        frames = self._chunk_frames()
        raw = decoder.read(frames)
        if not raw:
            return 0
        self._integrity.record_source(raw)
        result = repack(raw, source, target, allow_lossy=self._config.allow_degraded)
        if not result.exact:
            self._conversion_exact = False
            self._conversion_note = result.reason
        self._integrity.note_conversion(result.exact, result.reason)
        self._integrity.record_output(result.data, result.frames)
        written = ring.write_blocking(result.data, abort=self._feeder_aborted)
        if written < len(result.data):
            return -1
        self._frames_sent += result.frames
        return len(result.data)

    def _next_is_compatible(self) -> bool:
        with self._next_lock:
            next_decoder = self._next_decoder
        if next_decoder is None:
            return False
        try:
            return self._next_is_compatible_for(next_decoder.info)
        except Exception:
            return False

    def _advance_after_eof(self) -> str:
        """Handle the end of the current source.

        Returns ``"continue"`` when a compatible next track was spliced into the
        live stream, ``"drain"`` when the queue is finished (the buffered tail
        must still play) and ``"handoff"`` when the supervisor has to reopen the
        device for a different hardware format.
        """

        LOGGER.debug("end of stream reached", extra={"frames_sent": self._frames_sent})
        next_decoder = self._take_next_decoder()
        if next_decoder is None:
            return "drain"

        next_info = next_decoder.info
        if self._next_is_compatible_for(next_info):
            old = self._decoder
            self._decoder = next_decoder
            self._decode_state_reset(next_info)
            if old is not None:
                old.close()
            self._eof = False
            self._suppress_xrun = True
            self._gapless_transition = True
            self._publish_track(next_info, gapless=True)
            self._publish_telemetry(force=True)
            LOGGER.info("gapless transition", extra={"path": str(next_info.path)})
            return "continue"

        # Different hardware format: the stream must be reopened, which only the
        # supervisor may do.  Hand the path over and let this feeder retire.
        path = str(next_decoder.path)
        next_decoder.close()
        self._suppress_xrun = True
        self._commands.put(Command("load", {"path": path, "autoplay": True, "gapless": False}))
        LOGGER.info("format change between tracks; reopening the stream", extra={"path": path})
        return "handoff"

    def _next_is_compatible_for(self, next_info) -> bool:
        # The stream only has to exist, not to be running: the feeder can reach
        # the end of a short track while the supervisor is still pre-filling the
        # ring, before ``stream.start()`` is called.
        if self._target_format is None or self._stream is None:
            return False
        transport = next_info.transport_format
        return (
            transport.sample_rate == self._target_format.sample_rate
            and transport.channels == self._target_format.channels
            and transport.sample_format is self._target_format.sample_format
        )

    def _finish_playback(self) -> None:
        self._stop_stream()
        self._state = EngineState.STOPPED
        self._publish_telemetry(force=True)
        self._bus.publish(ENGINE_STATE, state=self._state.value)
        self._bus.publish(ENGINE_FINISHED, path=str(self._decoder.path) if self._decoder else "")

    def _start_transport(self) -> None:
        if self._stream is None:
            return
        self._last_error = ""
        if self._feeder is None or not self._feeder.is_alive():
            self._start_feeder()
        self._ensure_prefill()
        try:
            if not self._stream.active:
                self._stream.start()
        except Exception as exc:
            self._fail(f"driver refused to start the stream: {exc}")
            return
        self._state = EngineState.PLAYING
        self._bus.publish(ENGINE_STATE, state=self._state.value)
        self._publish_telemetry(force=True)

    def _ensure_prefill(self) -> None:
        ring = self._ring
        if ring is None:
            return
        if self._target_format is None:
            return
        target_bytes = int(self._target_format.bytes_per_second * 0.15)
        target_bytes = max(target_bytes, self._target_format.frame_bytes * 256)
        target_bytes = min(target_bytes, ring.capacity_bytes)
        deadline = time.monotonic() + 2.0
        while ring.available_bytes < target_bytes and time.monotonic() < deadline:
            if self._eof or self._feeder is None or not self._feeder.is_alive():
                break
            time.sleep(0.004)

    # ---------------------------------------------------------------- callback
    def _ensure_callback_priority(self) -> None:
        if self._callback_priority_ready:
            return
        self._callback_priority_ready = True
        try:
            disable_thread_priority_boost()
        except Exception:
            pass

    def _callback(self, outdata, frames, time_info, status) -> None:
        """PortAudio callback: never raises, never blocks, never allocates much."""

        try:
            self._callback_body(outdata, frames, time_info, status)
        except BaseException:  # pragma: no cover - the driver must never see an exception
            self._callback_errors += 1
            try:
                outdata[:] = b"\x00" * len(outdata)
            except Exception:
                pass

    def _callback_body(self, outdata, frames, time_info, status) -> None:
        ring = self._ring
        target = self._target_format
        if ring is None or target is None:
            outdata[:] = b"\x00" * len(outdata)
            return
        if not self._callback_priority_ready:
            self._ensure_callback_priority()
        needed = int(frames) * target.frame_bytes
        try:
            payload = ring.read(needed)
        except Exception:
            payload = b""
        got = len(payload)
        if got == needed:
            outdata[:] = payload
        else:
            if got:
                outdata[:got] = payload
            gap = needed - got
            outdata[got:needed] = b"\x00" * gap
            # A partial period at the natural end of the stream is not a
            # dropout: the encoder simply ran out of samples.  Counting it
            # would strip the badge from every correctly played track.
            if not self._suppress_xrun and not self._eof:
                self._xruns += 1
                self._silence_frames += gap // target.frame_bytes
                self._integrity.note_xruns(self._xruns)
                now = time.monotonic()
                if now - self._last_xrun_event > XRUN_EVENT_THROTTLE:
                    self._last_xrun_event = now
                    self._bus.publish(
                        ENGINE_XRUN,
                        count=self._xruns,
                        missing_frames=gap // target.frame_bytes,
                        fill=ring.fill_ratio,
                    )
                    LOGGER.warning(
                        "buffer underrun",
                        extra={"count": self._xruns, "missing_frames": gap // target.frame_bytes},
                    )
        if status:
            if getattr(status, "output_underflow", False) and not self._suppress_xrun:
                self._xruns += 1
                self._integrity.note_xruns(self._xruns)
        self._suppress_xrun = False

    # ------------------------------------------------------------- housekeeping
    def _housekeeping(self) -> None:
        now = time.monotonic()
        if now - self._last_device_poll >= DEVICE_POLL_INTERVAL:
            self._last_device_poll = now
            self._poll_devices()
        if now - self._last_housekeeping >= HOUSEKEEPING_INTERVAL:
            self._last_housekeeping = now
            self._publish_telemetry()

    def _poll_devices(self) -> None:
        try:
            signature = self._devices.signature()
        except AudioBackendUnavailable:
            return
        previous = self._signature
        self._signature = signature
        if previous == signature:
            return
        LOGGER.debug("device topology changed", extra={"before": previous, "after": signature})
        if self._device is None:
            return
        present = any(f"{self._device.host_kind}:{self._device.name}" == entry for entry in signature)
        if not present and not self._device_lost:
            self._handle_device_loss()
        elif present and self._device_lost:
            self._handle_device_return()

    def _handle_device_loss(self) -> None:
        if self._device is None:
            return
        self._device_lost = True
        self._lost_device_name = self._device.name
        self._resume_after_recovery = self._state is EngineState.PLAYING
        position = self._telemetry.snapshot().position_seconds
        self._stop_stream()
        self._close_stream()
        self._stop_feeder()
        self._state = EngineState.NO_DEVICE
        self._last_error = f"DAC disconnected: {self._lost_device_name}"
        self._bus.publish(
            ENGINE_DEVICE_LOST,
            device=self._lost_device_name,
            position=position,
            error=self._last_error,
        )
        LOGGER.error("output device lost", extra={"device": self._lost_device_name})
        self._publish_telemetry(force=True)

    def _handle_device_return(self) -> None:
        if self._decoder is None or self._target_format is None:
            self._device_lost = False
            return
        device = self._devices.find_by_name(self._lost_device_name)
        if device is None:
            return
        self._device_lost = False
        self._device = device
        self._config.device_index = device.index
        position = self._telemetry.snapshot().position_seconds
        LOGGER.info("output device restored", extra={"device": device.name, "position": position})
        try:
            prepared = self._prepare_stream(self._decoder.info)
        except EngineError as exc:
            self._fail(str(exc))
            return
        if not prepared:
            return
        self._cmd_seek(max(0.0, position))
        if self._resume_after_recovery:
            self._cmd_play()
        self._bus.publish(ENGINE_DEVICE_RESTORED, device=device.name, position=position)
        self._publish_telemetry(force=True)

    # ---------------------------------------------------------------- telemetry
    def _position_frames(self) -> int:
        """Frames already sent to the DAC.

        Derived from the decoder cursor minus whatever is still buffered, which
        stays exact across seeks, pauses and gapless transitions (a byte counter
        can drift when the ring holds a partial period).
        """

        decoder = self._decoder
        if decoder is None:
            return 0
        try:
            info = decoder.info
            consumed = int(decoder.position)
        except Exception:
            return 0
        if info.dsd_format is not None:
            consumed //= 16
        buffered = self._ring.available_frames if self._ring is not None else 0
        return max(0, consumed - buffered)

    def _publish_track(self, info, gapless: bool) -> None:
        self._gapless_transition = gapless
        self._bus.publish(
            ENGINE_TRACK_CHANGED,
            path=str(info.path),
            title=info.title,
            artist=info.artist,
            album=info.album,
            container=info.container,
            codec=info.codec,
            resolution=info.resolution_text,
            duration=info.duration,
            gapless=gapless,
            is_dsd=info.is_dsd,
            tags=dict(info.tags),
            notes=list(info.notes),
        )

    def _publish_telemetry(self, force: bool = False) -> None:
        target = self._target_format
        ring = self._ring
        info = self._decoder.info if self._decoder is not None else None
        position_frames = self._position_frames()
        rate_for_position = target.sample_rate if target is not None else (info.sample_rate if info else 0)
        position_seconds = (position_frames / rate_for_position) if rate_for_position else 0.0
        duration = info.duration if info is not None else 0.0
        if duration <= 0 and self._frames_total and rate_for_position:
            duration = self._frames_total / rate_for_position
        ring_stats = ring.stats() if ring is not None else None
        fill_ms = 0.0
        fill_ratio = 0.0
        capacity_ms = 0.0
        if ring is not None and target is not None:
            fill_ms = ring.available_bytes / (target.bytes_per_second / 1000.0)
            fill_ratio = ring.fill_ratio
            capacity_ms = ring.capacity_bytes / (target.bytes_per_second / 1000.0)
        priority = current_priority_state()
        bit_perfect, reason = self._evaluate_bit_perfect(position_seconds)
        report = self._integrity.report()
        source = info.transport_format if info is not None else None
        snapshot = TelemetrySnapshot(
            state=self._state.value,
            engine_thread="online" if self._supervisor is not None and self._supervisor.is_alive() else "offline",
            uptime_seconds=time.monotonic() - self._started_at,
            track_path=str(info.path) if info is not None else "",
            track_title=info.title if info is not None else "",
            track_artist=info.artist if info is not None else "",
            track_album=info.album if info is not None else "",
            track_container=info.container if info is not None else "",
            track_codec=info.codec if info is not None else "",
            track_notes=tuple(info.notes) if info is not None else (),
            source_format=source.describe() if source is not None else "",
            source_bits=source.valid_bits if source is not None else 0,
            source_rate=source.sample_rate if source is not None else 0,
            source_channels=source.channels if source is not None else 0,
            source_float=bool(source.is_float) if source is not None else False,
            is_dsd=bool(info.is_dsd) if info is not None else False,
            device_name=self._device.name if self._device is not None else "",
            device_host_api=self._device.host_api_name if self._device is not None else "",
            device_index=self._device.index if self._device is not None else -1,
            exclusive=bool(self._effective_exclusive),
            driver_label=self._driver_label() if self._device is not None else "",
            hardware_clock=target.sample_rate if target is not None else 0,
            hardware_bits=target.valid_bits if target is not None else 0,
            hardware_format=target.label if target is not None else "",
            hardware_channels=target.channels if target is not None else 0,
            stream_dtype=self._stream_dtype,
            latency_ms=(self._stream_latency_ms() + fill_ms) if target is not None else 0.0,
            buffer_ms=fill_ms,
            buffer_fill=fill_ratio,
            buffer_capacity_ms=capacity_ms,
            period_frames=self._config.period_frames,
            frames_played=position_frames,
            frames_total=self._frames_total,
            position_seconds=min(position_seconds, duration) if duration > 0 else position_seconds,
            duration_seconds=duration,
            bit_perfect=bit_perfect,
            bit_perfect_reason=reason,
            conversion_chain=self._conversion_note,
            dsp_chain=describe_dsp_chain(),
            integrity_digest=report.output_digest,
            integrity_bytes_sent=report.bytes_out,
            integrity_verified=report.is_bit_perfect,
            xruns=self._xruns,
            underrun_bytes=ring_stats.underrun_bytes if ring_stats is not None else 0,
            overruns=ring_stats.overrun_events if ring_stats is not None else 0,
            ring_discarded=ring_stats.discarded_bytes if ring_stats is not None else 0,
            silence_frames=self._silence_frames,
            stream_restarts=self._stream_restarts,
            clock_switches=self._clock_switches,
            priority=priority.summary(),
            priority_realtime=priority.realtime,
            last_error=self._last_error,
            device_present=not self._device_lost,
        )
        self._telemetry.observe(snapshot)
        self._telemetry.maybe_log(force=force)
        if report.started and self._state in (EngineState.PLAYING, EngineState.PAUSED):
            self._bus.publish(
                ENGINE_INTEGRITY,
                verdict=report.verdict.value,
                bit_perfect=report.is_bit_perfect,
                digest=report.output_digest,
                bytes=report.bytes_out,
                exact=report.exact_conversion,
                xruns=report.xruns,
            )

    def _evaluate_bit_perfect(self, position_seconds: float) -> Tuple[bool, str]:
        if self._state not in (EngineState.PLAYING, EngineState.PAUSED):
            return False, f"engine {self._state.value}"
        if self._decoder is None or self._target_format is None or self._source_format is None:
            return False, "no active stream"
        info = self._decoder.info
        if not info.lossless:
            return False, f"{info.codec} is not a lossless codec"
        if self._device_lost or self._device is None:
            return False, "DAC disconnected"
        if self._last_error:
            return False, self._last_error
        if not self._effective_exclusive and not self._device.is_asio:
            return False, f"shared-mode driver ({self._device.driver_label}): the OS mixer may resample"
        if self._target_format.sample_rate != self._source_format.sample_rate:
            return False, "hardware clock differs from the source rate"
        if self._xruns > 0:
            return False, f"{self._xruns} buffer underrun(s) inserted silence"
        if not self._conversion_exact:
            return False, self._conversion_note or "lossy container mapping"
        if not self._integrity.started:
            return False, "integrity monitor inactive"
        if self._device is not None and self._device.is_wasapi and not self._effective_exclusive:
            return False, "WASAPI shared mode is never bit-perfect"
        if info.dsd_format is not None:
            return True, f"DoP transport at {self._target_format.sample_rate} Hz"
        return True, "bit-exact path to the DAC buffer"

    # -------------------------------------------------------------------- error
    def _fail(self, message: str) -> None:
        previous = self._state
        self._last_error = message
        self._state = EngineState.ERROR
        self._stop_stream()
        self._stop_feeder()
        LOGGER.error("engine failure", extra={"error": message, "previous_state": previous.value})
        self._bus.publish(ENGINE_ERROR, message=message, previous=previous.value)
        self._bus.publish(ENGINE_STATE, state=self._state.value)
        self._publish_telemetry(force=True)
