"""FLAC path tests: libsndfile decoding must be bit-identical to the source WAV.

The fixtures under ``tests/assets`` are a deterministic 24-bit/96 kHz ramp and
its FLAC encoding of the same PCM, so the libsndfile tests need no external
encoder. The ffmpeg fallback test is skipped when ffmpeg is not installed
(``python setup_env.py --install-native`` fetches it).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.decoders.ffmpeg_backend import FfmpegDecoder, ffmpeg_available, find_tool
from src.core.decoders.registry import open_decoder
from src.core.decoders.sndfile_backend import SndFileDecoder
from src.core.decoders.wav import WavDecoder
from src.core.formats import AudioFormat, SampleFormat, repack
from src.core.libsndfile import LibSndFile

ASSETS = Path(__file__).resolve().parent / "assets"
WAV_FIXTURE = ASSETS / "ramp_ref_96k_24.wav"
FLAC_FIXTURE = ASSETS / "ramp_ref_96k_24.flac"

HAS_LIBSNDFILE = LibSndFile.available()
HAS_FFMPEG = ffmpeg_available()

needs_sndfile = pytest.mark.skipif(
    not (HAS_LIBSNDFILE and FLAC_FIXTURE.exists()), reason="libsndfile or fixture missing"
)
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")


def reference_payload() -> bytes:
    decoder = WavDecoder(WAV_FIXTURE)
    try:
        info = decoder.info
        return decoder.read(info.total_frames)
    finally:
        decoder.close()


def encode_flac(wav: Path, flac: Path, bits: int = 24) -> None:
    ffmpeg = find_tool("ffmpeg")
    assert ffmpeg is not None
    sample_format = {16: "s16", 24: "s32"}[bits]
    subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(wav),
            "-c:a",
            "flac",
            "-sample_fmt",
            sample_format,
            str(flac),
        ],
        check=True,
        capture_output=True,
    )


@needs_sndfile
def test_flac_decoding_is_bit_identical() -> None:
    payload = reference_payload()
    decoder = SndFileDecoder(FLAC_FIXTURE)
    try:
        info = decoder.info
        assert info.container == "FLAC"
        assert info.codec == "FLAC lossless"
        assert info.audio_format.sample_format is SampleFormat.INT32
        assert info.audio_format.valid_bits == 24
        assert info.sample_rate == 96000
        assert info.channels == 2
        raw = decoder.read(info.total_frames)
        assert len(raw) == info.total_frames * 2 * 4
        result = repack(raw, info.audio_format, AudioFormat(96000, 2, SampleFormat.INT24))
        assert result.exact, result.reason
        assert result.data == payload, "FLAC decode differs from the source PCM"
    finally:
        decoder.close()


@needs_sndfile
def test_engine_streams_flac_bit_exactly() -> None:
    """The whole chain (FLAC → libsndfile → engine → driver) stays bit exact."""

    from src.core.audio_engine import AudioEngine, EngineConfig
    from src.core.events import EventBus
    from src.core.telemetry import TelemetryHub
    from tests.test_engine_loopback import FakeDeviceManager, install_fake_backend, wait_for

    install_fake_backend()
    payload = reference_payload()
    manager = FakeDeviceManager(container=SampleFormat.INT24)
    engine = AudioEngine(manager, TelemetryHub(), EventBus(), EngineConfig(exclusive=True))
    engine.start()
    try:
        engine.load(FLAC_FIXTURE, autoplay=True)
        assert wait_for(lambda: engine.snapshot().state == "playing"), engine.snapshot().last_error
        assert wait_for(lambda: engine.snapshot().position_seconds > 0.05, timeout=4.0)
        snapshot = engine.snapshot()
        assert snapshot.bit_perfect, snapshot.bit_perfect_reason
        assert snapshot.xruns == 0
        captured = engine._stream.captured_bytes()
        comparable = min(len(captured), len(payload))
        comparable -= comparable % 6
        assert comparable > 10000
        assert captured[:comparable] == payload[:comparable], "FLAC playback is not bit exact"
        print(f"  FLAC bit-exact: {comparable} bytes verified")
    finally:
        engine.shutdown()


@needs_ffmpeg
def test_ffmpeg_fallback_matches_source(tmp_path: Path) -> None:
    flac = tmp_path / "ramp.flac"
    encode_flac(WAV_FIXTURE, flac, 24)
    payload = reference_payload()
    decoder = FfmpegDecoder(flac)
    try:
        info = decoder.info
        assert info.audio_format.sample_format is SampleFormat.INT32
        assert info.audio_format.valid_bits == 24
        raw = decoder.read(info.total_frames)
        result = repack(raw, info.audio_format, AudioFormat(96000, 2, SampleFormat.INT24))
        assert result.exact
        assert result.data == payload
    finally:
        decoder.close()


@needs_sndfile
def test_registry_prefers_libsndfile_for_flac() -> None:
    decoder = open_decoder(FLAC_FIXTURE)
    try:
        assert decoder.info.container == "FLAC"
        assert isinstance(decoder, SndFileDecoder)
    finally:
        decoder.close()
