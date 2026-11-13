"""One-off generator for the committed FLAC fixture (run once, then delete)."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.decoders.ffmpeg_backend import find_tool
from tests.test_engine_loopback import build_wav

assets = ROOT / "tests" / "assets"
assets.mkdir(parents=True, exist_ok=True)
wav = assets / "ramp_ref_96k_24.wav"
flac = assets / "ramp_ref_96k_24.flac"
build_wav(wav, 96000, 2, 24, 60000)
ffmpeg = find_tool("ffmpeg")
assert ffmpeg is not None
subprocess.run(
    [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav), "-c:a", "flac", "-sample_fmt", "s32", str(flac)],
    check=True,
)
print(wav, wav.stat().st_size, flac, flac.stat().st_size)
