"""Unit tests for the DSP-free primitives: formats, ring buffer, DoP, playlist."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import AppSettings, SettingsStore, format_duration, humanise_rate
from src.core.dsd import DOP_MARKER_A, DOP_MARKER_B, DopEncoder, DsdFormat, reverse_bits
from src.core.formats import (
    AudioFormat,
    LossyConversionError,
    SampleFormat,
    repack,
    supported_rates,
)
from src.core.integrity import IntegrityMonitor, Verdict, dsp_attestation
from src.core.playlist import PlaybackController, Playlist, RepeatMode, Track
from src.core.ring_buffer import RingBuffer
from src.core.telemetry import TelemetryHub, TelemetrySnapshot


# --------------------------------------------------------------------------- formats
def test_audio_format_labels():
    fmt = AudioFormat(192000, 2, SampleFormat.INT24, 24)
    assert fmt.label == "24-bit / 192 kHz"
    assert fmt.short_label == "24/192"
    assert fmt.frame_bytes == 6
    assert fmt.bytes_per_second == 192000 * 6
    with pytest.raises(ValueError):
        AudioFormat(0, 2, SampleFormat.INT16)
    with pytest.raises(ValueError):
        AudioFormat(48000, 2, SampleFormat.INT16, 24)


def test_int16_to_int32_is_bit_exact():
    data = struct.pack("<4h", -32768, -1, 0, 32767)
    result = repack(data, AudioFormat(44100, 2, SampleFormat.INT16), AudioFormat(44100, 2, SampleFormat.INT32))
    assert result.exact
    widened = struct.unpack("<4i", result.data)
    assert widened == (-32768 << 16, -1 << 16, 0, 32767 << 16)
    back = repack(result.data, AudioFormat(44100, 2, SampleFormat.INT32), AudioFormat(44100, 2, SampleFormat.INT16))
    assert back.exact and back.data == data


def test_24_bit_roundtrip_preserves_every_bit():
    values = (-8388608, -1, 0, 1, 8388607, 4660)
    data = b"".join(int(value).to_bytes(3, "little", signed=True) for value in values)
    source = AudioFormat(96000, 2, SampleFormat.INT24)
    for target in (SampleFormat.INT32, SampleFormat.INT24):
        result = repack(data, source, AudioFormat(96000, 2, target))
        assert result.exact, result.reason
    back = repack(
        repack(data, source, AudioFormat(96000, 2, SampleFormat.INT32)).data,
        AudioFormat(96000, 2, SampleFormat.INT32),
        source,
    )
    assert back.data == data


def test_narrowing_is_refused_by_default():
    data = b"\x01\x00\x00\x00\x00\x00"  # one stereo frame, 24-bit value 1
    with pytest.raises(LossyConversionError):
        repack(data, AudioFormat(48000, 2, SampleFormat.INT24), AudioFormat(48000, 2, SampleFormat.INT16))
    allowed = repack(
        data,
        AudioFormat(48000, 2, SampleFormat.INT24),
        AudioFormat(48000, 2, SampleFormat.INT16),
        allow_lossy=True,
    )
    assert not allowed.exact
    assert "discarded" in allowed.reason


def test_channel_and_rate_changes_are_forbidden():
    data = b"\x00" * 8
    with pytest.raises(ValueError):
        repack(data, AudioFormat(48000, 2, SampleFormat.INT16), AudioFormat(48000, 1, SampleFormat.INT16))
    with pytest.raises(ValueError):
        repack(data, AudioFormat(48000, 2, SampleFormat.INT16), AudioFormat(44100, 2, SampleFormat.INT16))


def test_float_and_integer_have_no_gain():
    data = struct.pack("<2i", 1 << 30, -(1 << 30))
    source = AudioFormat(48000, 2, SampleFormat.INT32)
    result = repack(data, source, AudioFormat(48000, 2, SampleFormat.FLOAT32))
    assert result.exact
    left, right = struct.unpack("<2f", result.data)
    assert left == 0.5 and right == -0.5


def test_supported_rates_are_the_full_family():
    rates = supported_rates()
    assert 44100 in rates and 48000 in rates
    assert 768000 in rates
    assert 352800 in rates


# --------------------------------------------------------------------------- ring buffer
def test_ring_buffer_basic_flow():
    ring = RingBuffer(1024, frame_bytes=4)
    assert ring.write(b"\x01" * 512) == 512
    assert ring.available_bytes == 512
    assert ring.read(256) == b"\x01" * 256
    assert ring.available_frames == 64
    payload, missing = ring.read_or_silence(1024)
    assert missing == 768
    assert ring.stats().underrun_events == 1


def test_ring_buffer_is_frame_aligned_and_never_overflows():
    ring = RingBuffer(64, frame_bytes=6)
    assert ring.write(b"\x00" * 6) == 6
    assert ring.write(b"\x00" * 60) == 54  # clamped by the 60 byte capacity
    assert ring.available_bytes == 60
    assert ring.stats().overrun_events >= 1
    with pytest.raises(ValueError):
        ring.write(b"\x00" * 5)


def test_ring_buffer_write_blocking_aborts_without_dropping():
    ring = RingBuffer(12, frame_bytes=6)
    assert ring.write(b"\x00" * 12) == 12
    assert ring.write_blocking(b"\x01" * 6, abort=lambda: True) == 0
    assert ring.stats().discarded_bytes == 0
    assert ring.read(6) == b"\x00" * 6
    assert ring.write_blocking(b"\x01" * 6, timeout=0.2) == 6


def test_ring_buffer_peek_and_drain():
    ring = RingBuffer(256, frame_bytes=2)
    ring.write(b"ABCDEF")
    assert ring.peek(4) == b"ABCD"
    assert ring.available_bytes == 6
    assert ring.drain() == 6
    assert ring.available_bytes == 0


# --------------------------------------------------------------------------- dsd
def test_bit_reversal():
    assert reverse_bits(b"\x01") == b"\x80"
    assert reverse_bits(b"\x0f") == b"\xf0"


def test_dop_marker_alternation_across_buffers():
    encoder = DopEncoder(DsdFormat(2822400, 2))
    first = encoder.encode([b"\xff" * 4, b"\x00" * 4])
    second = encoder.encode([b"\xff" * 4, b"\x00" * 4])
    assert len(first) == 12
    assert first[2] == DOP_MARKER_A and first[5] == DOP_MARKER_A
    assert first[8] == DOP_MARKER_B and first[11] == DOP_MARKER_B
    assert second[2] == DOP_MARKER_A, "marker phase must restart on an even frame"
    joined = first + second
    markers = [joined[index] for index in range(2, len(joined), 6)]
    assert markers == [DOP_MARKER_A, DOP_MARKER_B, DOP_MARKER_A, DOP_MARKER_B]


def test_dop_frame_carries_the_exact_bits():
    encoder = DopEncoder(DsdFormat(2822400, 2))
    payload = encoder.encode([b"\x12\x34", b"\xab\xcd"])
    assert payload[0:2] == b"\x34\x12"
    assert payload[3:5] == b"\xcd\xab"
    assert payload[2] == DOP_MARKER_A


def test_dsd_rate_derivations():
    fmt = DsdFormat(11289600, 2)
    assert fmt.label == "DSD256"
    assert fmt.dop_rate == 705600
    assert fmt.dop_format.sample_rate == 705600


# --------------------------------------------------------------------------- integrity
def test_integrity_verdict_and_attestation():
    monitor = IntegrityMonitor()
    source = AudioFormat(96000, 2, SampleFormat.INT24)
    monitor.begin(source, AudioFormat(96000, 2, SampleFormat.INT32), "WASAPI Exclusive", "24 -> 32", True)
    monitor.record_source(b"abc")
    monitor.record_output(b"abcdef", 1)
    report = monitor.report()
    assert report.verdict is Verdict.BIT_PERFECT
    assert report.exact_conversion
    assert not any(dsp_attestation().values()), "no DSP stage may ever be enabled"
    monitor.note_xruns(2)
    assert monitor.report().verdict is Verdict.DEGRADED


def test_integrity_lossy_downgrades():
    monitor = IntegrityMonitor()
    monitor.begin(
        AudioFormat(48000, 2, SampleFormat.INT24),
        AudioFormat(48000, 2, SampleFormat.INT16),
        "MME",
        "24 -> 16",
        False,
    )
    assert monitor.report().verdict is Verdict.DEGRADED


# --------------------------------------------------------------------------- playlist
def make_playlist() -> Playlist:
    playlist = Playlist("Test")
    for index in range(4):
        playlist.add(Track(path=f"C:/music/{index}.flac", title=f"Track {index}", duration=60.0))
    return playlist


def test_playlist_navigation_and_repeat():
    playlist = make_playlist()
    assert playlist.set_current(0)
    assert playlist.next_index() == 1
    assert playlist.previous_index() is None
    assert playlist.set_current(3)
    assert playlist.next_index() is None
    playlist.set_repeat(RepeatMode.ALL)
    assert playlist.next_index() == 0
    playlist.set_repeat(RepeatMode.ONE)
    assert playlist.next_index(manual=False) == playlist.current_index


def test_playlist_move_keeps_current_track():
    playlist = make_playlist()
    playlist.set_current(0)
    playlist.move(0, 2)
    assert playlist.current_index == 2
    assert playlist[2].title == "Track 0"
    assert [track.title for track in playlist] == ["Track 1", "Track 2", "Track 0", "Track 3"]


def test_repeat_one_preloads_the_same_track():
    class FakeEngine:
        def __init__(self) -> None:
            self.next_track = None

        def set_next_track(self, path) -> None:
            self.next_track = path

    from src.core.events import EventBus

    engine = FakeEngine()
    controller = PlaybackController(engine, EventBus(), make_playlist())
    controller.playlist.set_current(1)
    controller.playlist.set_repeat(RepeatMode.ONE)
    controller._preload_successor()
    assert engine.next_track == "C:/music/1.flac"
    controller.playlist.set_repeat(RepeatMode.OFF)
    controller._preload_successor()
    assert engine.next_track == "C:/music/2.flac"


def test_playlist_roundtrip(tmp_path):
    playlist = make_playlist()
    playlist.set_repeat(RepeatMode.ALL)
    playlist.set_shuffle(True)
    target = playlist.save(tmp_path / "queue.json")
    restored = Playlist.load(target)
    assert len(restored) == 4
    assert restored.repeat_mode is RepeatMode.ALL
    assert restored.shuffle is True


def test_shuffle_visits_every_track():
    playlist = make_playlist()
    playlist.set_shuffle(True)
    order = playlist.play_order
    assert sorted(order) == [0, 1, 2, 3]
    playlist.set_current(order[0])
    seen = {order[0]}
    for _ in range(6):
        following = playlist.next_index()
        if following is None:
            break
        seen.add(following)
        playlist.set_current(following)
    assert seen == {0, 1, 2, 3}


# --------------------------------------------------------------------------- config / telemetry
def test_settings_store_roundtrip(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.update(output_device_name="DAC X", exclusive_mode=False, library_folders=["C:/Music"])
    reloaded = SettingsStore(tmp_path / "settings.json")
    settings = reloaded.settings
    assert settings.output_device_name == "DAC X"
    assert settings.exclusive_mode is False
    assert settings.library_folders == ["C:/Music"]


def test_settings_coerce_invalid_types():
    settings = AppSettings.from_mapping(
        {
            "library_folders": "C:/Music",
            "exclusive_mode": "yes",
            "target_buffer_ms": "48",
            "window_width": 1600,
            "unknown_key": 1,
        }
    )
    assert settings.library_folders == []
    assert settings.exclusive_mode is True
    assert settings.target_buffer_ms == 48
    assert settings.window_width == 1600
    valid = AppSettings.from_mapping({"library_folders": ["C:/Music", "D:/Audio"]})
    assert valid.library_folders == ["C:/Music", "D:/Audio"]


def test_settings_survive_corruption(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    store = SettingsStore(path)
    assert store.settings.schema_version == AppSettings().schema_version


def test_telemetry_history_and_health():
    hub = TelemetryHub(history_length=8)
    hub.observe(TelemetrySnapshot(state="playing", buffer_fill=0.5, bit_perfect=True))
    hub.observe(TelemetrySnapshot(state="playing", buffer_fill=0.7, xruns=1))
    assert hub.history()[-1] == 0.7
    assert hub.snapshot().health() == "degraded"
    assert "buffer_fill" in hub.export()


def test_formatting_helpers():
    assert humanise_rate(44100) == "44.1 kHz"
    assert humanise_rate(96000) == "96 kHz"
    assert format_duration(3661) == "1:01:01"
    assert format_duration(61) == "01:01"
