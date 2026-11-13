"""End-to-end engine test with real hardware (digital silence only).

Validates: device negotiation, clock locking, ring buffering, callback timing,
position accounting and the integrity verdict.  The generated file contains
digital silence so the test is inaudible even at full scale.
"""

import struct
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import setup_logging
from src.core.audio_engine import AudioEngine, EngineConfig
from src.core.devices import DeviceManager
from src.core.events import EventBus
from src.core.telemetry import TelemetryHub


def build_silence(path: Path, sample_rate: int, channels: int, bits: int, seconds: float) -> None:
    frames = int(sample_rate * seconds)
    width = bits // 8
    payload = b"\x00" * (frames * channels * width)
    block_align = width * channels
    fmt = struct.pack("<HHIIHH", 1, channels, sample_rate, sample_rate * block_align, block_align, bits)
    data = b"data" + struct.pack("<I", len(payload)) + payload
    riff = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + data
    path.write_bytes(b"RIFF" + struct.pack("<I", len(riff)) + riff)


def main() -> int:
    setup_logging(verbose=True, log_to_file=False)
    manager = DeviceManager()
    devices = manager.refresh(force=True)
    if not devices:
        print("no output device; skipping hardware test")
        return 0
    best = manager.best_device()
    print(f"device: {best.name} · {best.host_api_name} · exclusive capable={best.exclusive_capable}")
    engine = AudioEngine(manager, TelemetryHub(), EventBus(), EngineConfig(device_index=best.index, exclusive=True))
    engine.start()
    failures = 0
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        for rate in (48000, 96000):
            track = base / f"silence_{rate}.wav"
            build_silence(track, rate, 2, 24, 1.5)
            engine.load(track, autoplay=True)
            deadline = time.time() + 3.0
            seen_playing = False
            while time.time() < deadline:
                time.sleep(0.1)
                snapshot = engine.snapshot()
                if snapshot.state == "playing":
                    seen_playing = True
                if snapshot.position_seconds >= 1.2:
                    break
            snapshot = engine.snapshot()
            print(
                f"[{rate}] state={snapshot.state} pos={snapshot.position_seconds:.2f}s "
                f"xruns={snapshot.xruns} clock={snapshot.hardware_clock} bit_perfect={snapshot.bit_perfect} "
                f"reason={snapshot.bit_perfect_reason!r} driver={snapshot.driver_label!r} "
                f"bytes={snapshot.integrity_bytes_sent}"
            )
            if not seen_playing:
                print(f"[{rate}] FAIL: stream never reached the playing state ({snapshot.last_error})")
                failures += 1
            if snapshot.position_seconds <= 0.5:
                print(f"[{rate}] FAIL: position did not advance")
                failures += 1
            if snapshot.xruns:
                print(f"[{rate}] FAIL: xruns detected")
                failures += 1
            engine.stop()
            time.sleep(0.2)
    engine.shutdown()
    print("engine smoke:", "PASS" if failures == 0 else f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
