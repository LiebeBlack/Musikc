import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import setup_logging
from src.core.audio_engine import AudioEngine, EngineConfig
from src.core.events import EventBus
from src.core.telemetry import TelemetryHub
from tests.test_engine_loopback import (
    FakeDeviceManager,
    build_wav,
    install_fake_backend,
    wait_for,
)

setup_logging(verbose=False, log_to_file=False)
install_fake_backend()

with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / "ramp_seek.wav"
    payload = build_wav(path, 96000, 2, 16, 96000)
    engine = AudioEngine(FakeDeviceManager(), TelemetryHub(), EventBus(), EngineConfig(exclusive=True))
    engine.start()
    engine.load(path, autoplay=False)
    wait_for(lambda: engine.snapshot().state == "stopped")
    print("state after load", engine.snapshot().state, "pos", engine.snapshot().position_seconds)
    engine.seek(0.5)
    time.sleep(0.4)
    print("state after seek", engine.snapshot().state, "pos", engine.snapshot().position_seconds)
    engine.play()
    wait_for(lambda: engine.snapshot().state == "playing")
    time.sleep(0.4)
    snapshot = engine.snapshot()
    print("playing pos", snapshot.position_seconds, "xruns", snapshot.xruns, "sent", snapshot.integrity_bytes_sent)
    captured = engine._stream.captured_bytes()
    offset = int(0.5 * 96000) * 4
    print("captured len", len(captured), "offset", offset)
    print("captured head", captured[:24].hex())
    print("expected head", payload[offset:offset + 24].hex())
    for shift in range(-12, 13, 2):
        start = offset + shift * 2
        if start >= 0 and captured[:64] == payload[start:start + 64]:
            print("MATCH at shift", shift, "bytes")
    engine.shutdown()
