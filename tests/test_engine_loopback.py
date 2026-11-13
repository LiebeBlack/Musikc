"""End-to-end bit-exactness test with a simulated PortAudio backend.

A fake ``RawOutputStream`` consumes the driver buffer at real time and stores
every byte it receives.  The test then proves that those bytes are *identical*
to the bytes stored in the source WAV file (same container, same offsets), which
is the operational definition of bit-perfect playback.

Run directly: ``python tests/test_engine_loopback.py``
"""

from __future__ import annotations

import hashlib
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.core.audio_engine as engine_module
from src.core.audio_engine import AudioEngine, EngineConfig
from src.core.devices import DeviceCapabilities, DeviceInfo
from src.core.events import EventBus
from src.core.formats import AudioFormat, SampleFormat
from src.core.telemetry import TelemetryHub


# --------------------------------------------------------------------------- assets
def build_wav(path: Path, sample_rate: int, channels: int, bits: int, frames: int) -> bytes:
    """Write a deterministic ramp so every byte can be compared."""

    width = bits // 8
    payload = bytearray()
    for frame in range(frames):
        for channel in range(channels):
            value = ((frame * 37 + channel * 911) % 20000) - 10000
            payload += int(value).to_bytes(width, "little", signed=True)
    block_align = width * channels
    fmt = struct.pack("<HHIIHH", 1, channels, sample_rate, sample_rate * block_align, block_align, bits)
    data = b"data" + struct.pack("<I", len(payload)) + bytes(payload)
    riff = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + data
    path.write_bytes(b"RIFF" + struct.pack("<I", len(riff)) + riff)
    return bytes(payload)


def build_dsf(path: Path, channels: int = 2) -> None:
    rate = 2822400
    block_bytes = 4096
    blocks = 40
    sample_count = block_bytes * 8 * blocks
    fmt = (
        struct.pack("<IIII", 1, 0, 2 if channels == 2 else 1, channels)
        + struct.pack("<I", rate)
        + struct.pack("<I", 1)
        + struct.pack("<Q", sample_count)
        + struct.pack("<I", block_bytes)
        + struct.pack("<I", 0)
    )
    fmt_chunk = b"fmt " + struct.pack("<Q", 12 + len(fmt)) + fmt
    data = bytearray()
    for block in range(blocks):
        for channel in range(channels):
            data += bytes(((index * 7 + channel * 13 + block) & 0xFF) for index in range(block_bytes))
    data_chunk = b"data" + struct.pack("<Q", 12 + len(data)) + bytes(data)
    total = 28 + len(fmt_chunk) + len(data_chunk)
    header = b"DSD " + struct.pack("<Q", 28) + struct.pack("<Q", total) + struct.pack("<Q", 0)
    path.write_bytes(header + fmt_chunk + data_chunk)


# --------------------------------------------------------------------------- fake backend
class FakeStream:
    """Consumes the driver buffer at real time and records every byte."""

    def __init__(self, device, samplerate, channels, dtype, callback, blocksize, latency, extra_settings, **kwargs):
        self.device = device
        self.samplerate = samplerate
        self.channels = channels
        self.dtype = dtype
        self.blocksize = blocksize or 256
        self.frame_bytes = {"int16": 2, "int24": 3, "int32": 4, "float32": 4}[dtype] * channels
        self.latency = 0.01
        self.active = False
        self.closed = False
        self._callback = callback
        self._thread: threading.Thread | None = None
        self.captured = bytearray()
        self._capture_lock = threading.Lock()

    def start(self) -> None:
        if self.active:
            return
        self.active = True
        self._thread = threading.Thread(target=self._run, name="fake-driver", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.active = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def close(self) -> None:
        self.stop()
        self.closed = True

    def _run(self) -> None:
        frames = self.blocksize
        buffer = bytearray(frames * self.frame_bytes)
        interval = frames / float(self.samplerate)
        while self.active:
            started = time.monotonic()
            try:
                self._callback(buffer, frames, None, None)
            except Exception:
                break
            with self._capture_lock:
                self.captured.extend(buffer)
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, interval - elapsed))

    def captured_bytes(self) -> bytes:
        with self._capture_lock:
            return bytes(self.captured)


class FakeSettings:
    def __init__(self, exclusive: bool = True) -> None:
        self.exclusive = exclusive


class FakeBackend:
    RawOutputStream = FakeStream

    @staticmethod
    def WasapiSettings(exclusive: bool = True) -> FakeSettings:
        return FakeSettings(exclusive)

    @staticmethod
    def AsioSettings(**kwargs):
        return FakeSettings(True)

    @staticmethod
    def check_output_settings(**kwargs) -> None:
        return None


class FakeDeviceManager:
    """Duck-typed DeviceManager for deterministic tests."""

    def __init__(self, rates=(44100, 48000, 88200, 96000, 176400, 192000), container=None) -> None:
        self.container = container
        self.device_info = DeviceInfo(
            index=3,
            name="True HQ Test DAC",
            host_api_index=2,
            host_api_name="Windows WASAPI",
            host_kind="wasapi",
            max_output_channels=2,
            max_input_channels=0,
            default_samplerate=48000.0,
            is_default_output=True,
            dac_score=4,
        )
        self.rates = tuple(rates)

    def refresh(self, force: bool = False):
        return [self.device_info]

    def device(self, index: int):
        return self.device_info if index == self.device_info.index else None

    def best_device(self, prefer_asio: bool = True):
        return self.device_info

    def signature(self):
        return (f"{self.device_info.host_kind}:{self.device_info.name}",)

    def find_by_name(self, name: str, host_api: str = ""):
        return self.device_info if name == self.device_info.name else None

    def extra_settings(self, device, exclusive):
        return FakeSettings(exclusive) if exclusive else None

    def capabilities(self, device, exclusive=None, **kwargs) -> DeviceCapabilities:
        formats = (SampleFormat.INT24.code, SampleFormat.INT32.code, SampleFormat.INT16.code)
        capability = DeviceCapabilities(device_index=device.index, exclusive=bool(exclusive), channels=2)
        for rate in self.rates:
            capability.rates[rate] = formats
            capability.format_by_rate[rate] = SampleFormat.INT24
        return capability

    def choose_transport(self, device, source: AudioFormat, exclusive: bool, allow_degraded: bool = False):
        if source.sample_rate not in self.rates:
            return None, [f"DAC cannot lock at {source.sample_rate} Hz"]
        order = [source.sample_format, SampleFormat.INT32, SampleFormat.INT24, SampleFormat.INT16]
        if self.container is not None and self.container.bits >= source.valid_bits:
            order.insert(0, self.container)
        chosen = next((candidate for candidate in order if candidate.bits >= source.valid_bits or candidate is source.sample_format), SampleFormat.INT32)
        return AudioFormat(source.sample_rate, source.channels, chosen, chosen.bits), []


def install_fake_backend(monkeypatch_target=engine_module) -> None:
    monkeypatch_target.require_backend = lambda: FakeBackend  # type: ignore[assignment]
    monkeypatch_target.backend_available = lambda: True  # type: ignore[assignment]


def wait_for(predicate, timeout: float = 4.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# --------------------------------------------------------------------------- tests
def test_bit_exact_wav() -> None:
    install_fake_backend()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "ramp_48k_24.wav"
        frames = 48000
        payload = build_wav(path, 48000, 2, 24, frames)
        engine = AudioEngine(FakeDeviceManager(), TelemetryHub(), EventBus(), EngineConfig(exclusive=True))
        engine.start()
        try:
            engine.load(path, autoplay=True)
            assert wait_for(lambda: engine.snapshot().state == "playing"), engine.snapshot().last_error
            assert wait_for(lambda: engine.snapshot().position_seconds > 0.35, timeout=4.0)
            time.sleep(0.25)
            snapshot = engine.snapshot()
            assert snapshot.xruns == 0, f"xruns: {snapshot.xruns}"
            assert snapshot.bit_perfect, snapshot.bit_perfect_reason
            assert snapshot.hardware_clock == 48000
            assert snapshot.integrity_bytes_sent > 0
            stream = engine._stream
            captured = stream.captured_bytes()
            assert captured, "the driver received no bytes"
            assert len(captured) % 6 == 0
            comparable = min(len(captured), len(payload))
            comparable -= comparable % 6
            assert comparable > 20000, f"only {comparable} bytes captured"
            assert captured[:comparable] == payload[:comparable], "DAC bytes differ from the source file"
            digest = hashlib.blake2b(captured[:comparable], digest_size=16).hexdigest()
            print(f"  bit-exact: {comparable} bytes verified, blake2b={digest[:16]}")
        finally:
            engine.shutdown()


def test_clock_refusal() -> None:
    install_fake_backend()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "ramp_768k.wav"
        build_wav(path, 768000, 2, 24, 7680)
        engine = AudioEngine(FakeDeviceManager(), TelemetryHub(), EventBus(), EngineConfig(exclusive=True))
        engine.start()
        try:
            engine.load(path, autoplay=True)
            assert wait_for(lambda: engine.snapshot().state == "error", timeout=3.0), engine.snapshot().state
            snapshot = engine.snapshot()
            assert "cannot accept" in snapshot.last_error or "cannot lock" in snapshot.last_error, snapshot.last_error
            print(f"  refusal message: {snapshot.last_error[:90]}")
        finally:
            engine.shutdown()


def test_seek_accuracy() -> None:
    install_fake_backend()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "ramp_seek.wav"
        frames = 96000
        payload = build_wav(path, 96000, 2, 16, frames)
        engine = AudioEngine(FakeDeviceManager(), TelemetryHub(), EventBus(), EngineConfig(exclusive=True))
        engine.start()
        try:
            engine.load(path, autoplay=False)
            assert wait_for(lambda: engine.snapshot().state == "stopped")
            engine.seek(0.5)
            assert wait_for(lambda: engine.snapshot().position_seconds >= 0.49, timeout=3.0)
            snapshot = engine.snapshot()
            assert abs(snapshot.position_seconds - 0.5) < 0.05, snapshot.position_seconds
            engine.play()
            assert wait_for(lambda: engine.snapshot().state == "playing")
            time.sleep(0.3)
            stream = engine._stream
            captured = stream.captured_bytes()
            offset = int(0.5 * 96000) * 4
            comparable = min(len(captured), len(payload) - offset)
            comparable -= comparable % 4
            assert comparable > 4000
            assert captured[:comparable] == payload[offset : offset + comparable], "seek landed on the wrong sample"
            print(f"  seek verified at frame {offset // 4}")
        finally:
            engine.shutdown()


def test_dsd_dop_stream() -> None:
    install_fake_backend()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "tone.dsf"
        build_dsf(path)
        manager = FakeDeviceManager(rates=(44100, 48000, 88200, 96000, 176400, 192000))
        engine = AudioEngine(manager, TelemetryHub(), EventBus(), EngineConfig(exclusive=True))
        engine.start()
        try:
            engine.load(path, autoplay=True)
            assert wait_for(lambda: engine.snapshot().state == "playing"), engine.snapshot().last_error
            assert wait_for(lambda: engine.snapshot().position_seconds > 0.05, timeout=2.0)
            snapshot = engine.snapshot()
            assert snapshot.is_dsd
            assert snapshot.hardware_clock == 176400, snapshot.hardware_clock
            assert snapshot.bit_perfect, snapshot.bit_perfect_reason
            stream = engine._stream
            captured = stream.captured_bytes()
            assert len(captured) >= 6
            assert len(captured) % 6 == 0
            markers = {captured[index] for index in range(2, min(len(captured), 600), 6)}
            assert markers <= {0x05, 0xFA}, markers
            assert markers == {0x05, 0xFA}, "DoP marker alternation missing"
            print(f"  DoP verified: {len(captured) // 6} frames, markers={sorted(markers)}")
        finally:
            engine.shutdown()


def test_gapless_advance_is_bit_exact() -> None:
    install_fake_backend()
    with tempfile.TemporaryDirectory() as directory:
        first = Path(directory) / "first.wav"
        second = Path(directory) / "second.wav"
        payload_a = build_wav(first, 48000, 2, 24, 14400)  # 0.3 s
        payload_b = build_wav(second, 48000, 2, 24, 14400)
        engine = AudioEngine(FakeDeviceManager(), TelemetryHub(), EventBus(), EngineConfig(exclusive=True))
        engine.start()
        try:
            engine.set_next_track(second)
            engine.load(first, autoplay=True)
            assert wait_for(lambda: engine.snapshot().state == "playing")
            assert wait_for(
                lambda: engine.snapshot().track_path.endswith("second.wav"), timeout=5.0
            ), engine.snapshot().track_path
            # Wait until the driver has consumed the whole first track plus a
            # healthy slice of the second one, then compare the concatenation.
            assert wait_for(
                lambda: len(engine._stream.captured_bytes()) > len(payload_a) + 30000,
                timeout=6.0,
            ), len(engine._stream.captured_bytes())
            snapshot = engine.snapshot()
            assert snapshot.xruns == 0, f"gapless splice produced {snapshot.xruns} xrun(s)"
            assert snapshot.bit_perfect, snapshot.bit_perfect_reason
            captured = engine._stream.captured_bytes()
            expected = payload_a + payload_b
            comparable = min(len(captured), len(expected))
            comparable -= comparable % 6
            assert comparable > len(payload_a), "the second track was never reached"
            assert captured[:comparable] == expected[:comparable], "gapless splice is not bit exact"
            print(f"  gapless splice verified across {comparable} bytes")
        finally:
            engine.shutdown()


def test_format_change_advances_to_next_track() -> None:
    install_fake_backend()
    with tempfile.TemporaryDirectory() as directory:
        first = Path(directory) / "first_48k.wav"
        second = Path(directory) / "second_96k.wav"
        build_wav(first, 48000, 2, 24, 9600)
        build_wav(second, 96000, 2, 24, 192000)  # 2 s so the assertions are not racing EOF
        engine = AudioEngine(FakeDeviceManager(), TelemetryHub(), EventBus(), EngineConfig(exclusive=True))
        engine.start()
        try:
            engine.set_next_track(second)
            engine.load(first, autoplay=True)
            assert wait_for(lambda: engine.snapshot().state == "playing")
            assert wait_for(
                lambda: engine.snapshot().track_path.endswith("second_96k.wav"), timeout=6.0
            ), engine.snapshot().track_path
            assert wait_for(lambda: engine.snapshot().state == "playing", timeout=3.0)
            assert wait_for(lambda: engine.snapshot().position_seconds > 0.1, timeout=3.0)
            snapshot = engine.snapshot()
            assert snapshot.hardware_clock == 96000, snapshot.hardware_clock
            assert snapshot.bit_perfect, snapshot.bit_perfect_reason
            print(f"  format change reopened the stream at {snapshot.hardware_clock} Hz")
        finally:
            engine.shutdown()


def test_play_after_end_restarts_track() -> None:
    install_fake_backend()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "short.wav"
        build_wav(path, 48000, 2, 24, 9600)  # 0.2 s
        engine = AudioEngine(FakeDeviceManager(), TelemetryHub(), EventBus(), EngineConfig(exclusive=True))
        engine.start()
        try:
            engine.load(path, autoplay=True)
            assert wait_for(lambda: engine.snapshot().state == "stopped", timeout=4.0)
            engine.play()
            assert wait_for(lambda: engine.snapshot().state == "playing", timeout=3.0)
            assert wait_for(lambda: engine.snapshot().position_seconds > 0.03, timeout=3.0)
            print("  play after end-of-stream restarted the track")
        finally:
            engine.shutdown()


def test_end_of_stream_is_clean() -> None:
    install_fake_backend()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "short.wav"
        payload = build_wav(path, 48000, 2, 24, 14400)  # 0.3 s
        engine = AudioEngine(FakeDeviceManager(), TelemetryHub(), EventBus(), EngineConfig(exclusive=True))
        engine.start()
        try:
            engine.load(path, autoplay=True)
            assert wait_for(lambda: engine.snapshot().state == "playing")
            assert wait_for(lambda: engine.snapshot().state == "stopped", timeout=4.0), engine.snapshot().state
            snapshot = engine.snapshot()
            assert snapshot.xruns == 0, f"the end of the track must not count as an underrun ({snapshot.xruns})"
            captured = engine._stream.captured_bytes()
            comparable = min(len(captured), len(payload))
            comparable -= comparable % 6
            assert comparable >= len(payload) - 6
            assert captured[:comparable] == payload[:comparable]
            print(f"  clean end of stream after {len(captured) // 6} frames")
        finally:
            engine.shutdown()


def test_xrun_detection() -> None:
    install_fake_backend()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "ramp_long.wav"
        build_wav(path, 48000, 2, 24, 96000)
        config = EngineConfig(exclusive=True, ring_ms=160, feeder_chunk_ms=4)
        engine = AudioEngine(FakeDeviceManager(), TelemetryHub(), EventBus(), config)
        engine.start()
        try:
            engine.load(path, autoplay=True)
            assert wait_for(lambda: engine.snapshot().position_seconds > 0.05, timeout=2.0)
            ring = engine._ring
            assert ring is not None
            engine._stop_feeder()
            ring.drain()
            assert wait_for(lambda: engine.snapshot().xruns > 0, timeout=1.5), "underrun never raised"
            snapshot = engine.snapshot()
            assert snapshot.xruns > 0, "underrun not detected"
            assert not snapshot.bit_perfect
            assert "underrun" in snapshot.bit_perfect_reason, snapshot.bit_perfect_reason
            print(f"  xrun detected: {snapshot.xruns}, badge reason: {snapshot.bit_perfect_reason}")
        finally:
            engine.shutdown()


def dev_main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for test in tests:
        started = time.monotonic()
        try:
            test()
            print(f"PASS {test.__name__} ({time.monotonic() - started:.2f}s)")
        except Exception as exc:
            failures += 1
            import traceback

            traceback.print_exc()
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(dev_main())
