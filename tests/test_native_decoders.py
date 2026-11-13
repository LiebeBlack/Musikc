"""Round-trip tests for the native WAV / AIFF / DSF / DFF readers.

Run directly (``python tests/test_native_decoders.py``) or with pytest.  The
fixtures are synthesised so the tests carry no binary blobs.
"""

import math
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.decoders.aiff import AiffDecoder
from src.core.decoders.dff import DffDecoder
from src.core.decoders.dsf import DsfDecoder
from src.core.decoders.registry import open_decoder
from src.core.decoders.wav import WavDecoder
from src.core.dsd import DOP_MARKER_A, DOP_MARKER_B, DopEncoder
from src.core.formats import AudioFormat, SampleFormat, repack


def build_wav(sample_rate: int, channels: int, bits: int, frames: int, float_format: bool = False) -> bytes:
    width = bits // 8
    block_align = width * channels
    payload = bytearray()
    for frame in range(frames):
        for channel in range(channels):
            value = int(0.5 * ((1 << (bits - 1)) - 1) * math.sin(2 * math.pi * 440 * frame / sample_rate + channel))
            if float_format:
                payload += struct.pack("<f", value / float(1 << (bits - 1)))
            elif width == 3:
                payload += int(value).to_bytes(3, "little", signed=True)
            elif width == 2:
                payload += struct.pack("<h", value)
            else:
                payload += struct.pack("<i", value)
    format_code = 3 if float_format else 1
    fmt = struct.pack("<HHIIHH", format_code, channels, sample_rate, sample_rate * block_align, block_align, bits)
    data = b"data" + struct.pack("<I", len(payload)) + bytes(payload)
    if len(payload) % 2:
        data += b"\x00"
    riff = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + data
    return b"RIFF" + struct.pack("<I", len(riff)) + riff


def build_aiff(sample_rate: int, channels: int, bits: int, frames: int, little_endian: bool = False) -> bytes:
    def extended(value: float) -> bytes:
        if value == 0:
            return b"\x00" * 10
        exponent = int(math.floor(math.log2(value)))
        mantissa = int(value / (2 ** exponent) * (1 << 63))
        return struct.pack(">H", exponent + 16383) + mantissa.to_bytes(8, "big")

    width = bits // 8
    payload = bytearray()
    for frame in range(frames):
        for channel in range(channels):
            value = int(0.5 * ((1 << (bits - 1)) - 1) * math.sin(2 * math.pi * 440 * frame / sample_rate + channel))
            order = "little" if little_endian else "big"
            payload += int(value).to_bytes(width if not little_endian else 3 if width == 3 else width, order, signed=True)
    comm = struct.pack(">hIh", channels, frames, bits) + extended(sample_rate)
    if little_endian:
        comm += b"sowt" + b"\x00\x00"
        form_type = b"AIFC"
    else:
        form_type = b"AIFF"
    comm_chunk = b"COMM" + struct.pack(">I", len(comm)) + comm
    ssnd = b"SSND" + struct.pack(">I", 8 + len(payload)) + struct.pack(">II", 0, 0) + bytes(payload)
    body = form_type + comm_chunk + ssnd
    return b"FORM" + struct.pack(">I", len(body)) + body


def build_dsf(sample_rate: int, channels: int, block_bytes: int, blocks: int) -> bytes:
    samples_per_block = block_bytes * 8
    sample_count = samples_per_block * blocks
    fmt = (
        struct.pack("<IIII", 1, 0, 2 if channels == 2 else 1, channels)
        + struct.pack("<I", sample_rate)
        + struct.pack("<I", 1)
        + struct.pack("<Q", sample_count)
        + struct.pack("<I", block_bytes)
        + struct.pack("<I", 0)
    )
    fmt_chunk = b"fmt " + struct.pack("<Q", 12 + len(fmt)) + fmt
    data = bytearray()
    for _block in range(blocks):
        for channel in range(channels):
            data += bytes(((index + channel) * 37) & 0xFF for index in range(block_bytes))
    data_chunk = b"data" + struct.pack("<Q", 12 + len(data)) + bytes(data)
    total = 28 + len(fmt_chunk) + len(data_chunk)
    header = b"DSD " + struct.pack("<Q", 28) + struct.pack("<Q", total) + struct.pack("<Q", 0)
    return header + fmt_chunk + data_chunk


def _dff_chunk(tag: bytes, payload: bytes) -> bytes:
    padded = payload + (b"\x00" if len(payload) % 2 else b"")
    return tag + struct.pack(">Q", len(payload)) + padded


def build_dff(sample_rate: int, channels: int, blocks: int, block_bytes: int = 4096) -> bytes:
    fver = _dff_chunk(b"FVER", struct.pack(">I", 0x01050000))
    fs = _dff_chunk(b"FS  ", struct.pack(">I", sample_rate))
    chnl_payload = struct.pack(">H", channels) + b"".join(struct.pack(">4s", f"C{index + 1}".encode()) for index in range(channels))
    chnl = _dff_chunk(b"CHNL", chnl_payload)
    cmpr = _dff_chunk(b"CMPR", b"DSD " + b"\x00" + b"not compressed")
    sound_props = b"SND " + fs + chnl + cmpr
    prop = _dff_chunk(b"PROP", sound_props)
    data = bytearray()
    for _block in range(blocks):
        for channel in range(channels):
            data += bytes(((index * (channel + 1)) * 13) & 0xFF for index in range(block_bytes))
    dsd_chunk = _dff_chunk(b"DSD ", bytes(data))
    body = b"DSD " + fver + prop + dsd_chunk
    return b"FRM8" + struct.pack(">Q", len(body)) + body


def test_wav_16_bit():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "test16.wav"
        path.write_bytes(build_wav(44100, 2, 16, 1000))
        decoder = WavDecoder(path)
        info = decoder.info
        assert info.audio_format.sample_format is SampleFormat.INT16
        assert info.total_frames == 1000
        assert info.sample_rate == 44100
        first = decoder.read(100)
        assert len(first) == 100 * 4
        seeked = decoder.seek(500)
        assert seeked == 500
        assert decoder.position == 500
        rest = decoder.read(1000)
        assert len(rest) == 500 * 4
        decoder.close()


def test_wav_24_bit():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "test24.wav"
        path.write_bytes(build_wav(96000, 2, 24, 500))
        decoder = WavDecoder(path)
        assert decoder.info.bit_depth_text == "24-bit"
        payload = decoder.read(10)
        assert len(payload) == 10 * 6
        converted = repack(payload, decoder.info.audio_format, AudioFormat(96000, 2, SampleFormat.INT32))
        assert converted.exact
        decoder.close()


def test_wav_float():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "test32f.wav"
        path.write_bytes(build_wav(192000, 2, 32, 300, float_format=True))
        decoder = WavDecoder(path)
        assert decoder.info.audio_format.sample_format is SampleFormat.FLOAT32
        assert decoder.info.sample_rate == 192000
        assert len(decoder.read(300)) == 300 * 8
        decoder.close()


def test_aiff_big_endian():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "test.aiff"
        path.write_bytes(build_aiff(44100, 2, 16, 400))
        decoder = AiffDecoder(path)
        assert decoder.info.total_frames == 400
        assert decoder.info.sample_rate == 44100
        payload = decoder.read(400)
        assert len(payload) == 400 * 4
        decoder.close()


def test_aiff_sowt():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "test_sowt.aifc"
        path.write_bytes(build_aiff(48000, 2, 16, 128, little_endian=True))
        decoder = AiffDecoder(path)
        payload = decoder.read(128)
        assert len(payload) == 128 * 4
        assert decoder.info.sample_rate == 48000
        decoder.close()


def test_dsf():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "test.dsf"
        path.write_bytes(build_dsf(2822400, 2, 4096, 2))
        decoder = DsfDecoder(path)
        info = decoder.info
        assert info.is_dsd
        assert info.dsd_format.label == "DSD64"
        assert info.dsd_format.dop_rate == 176400
        assert info.total_frames == 4096 * 8 * 2
        payloads = decoder.read_dsd(4096)
        assert len(payloads) == 2
        assert len(payloads[0]) == 512
        assert payloads[0][0] == int(f"{0:08b}"[::-1], 2)
        encoder = DopEncoder(info.dsd_format)
        encoded = encoder.encode(payloads)
        assert len(encoded) == 512 // 2 * 6
        assert encoded[2] == DOP_MARKER_A
        assert encoded[5] == DOP_MARKER_A
        assert encoded[8] == DOP_MARKER_B
        total = decoder.seek(0)
        assert total == 0
        decoder.close()


def test_dff():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "test.dff"
        path.write_bytes(build_dff(5644800, 2, 2))
        decoder = DffDecoder(path)
        info = decoder.info
        assert info.is_dsd
        assert info.dsd_format.label == "DSD128"
        assert info.dsd_format.dop_rate == 352800
        payloads = decoder.read_dsd(2048)
        assert len(payloads) == 2
        assert len(payloads[0]) == 256
        assert payloads[0] == bytes((index * 13) & 0xFF for index in range(256))
        decoder.close()


def test_registry_selects_native():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "auto.wav"
        path.write_bytes(build_wav(48000, 1, 16, 64))
        decoder = open_decoder(path)
        assert decoder.container_name == "WAV"
        decoder.close()


def dev_main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(dev_main())
