"""Native AIFF / AIFF-C reader.

Python removed the ``aifc`` module in 3.13, so the FORM/COMM/SSND structure is
parsed here directly, including the 80-bit IEEE 754 extended sample rate and
the AIFF-C compression identifiers (``NONE``, ``twos``, ``sowt``, ``fl32``,
``fl64``).  Big-endian sources are byte swapped with numpy, which is a pure
representation change: the sample values are untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import numpy as np

from ..formats import AudioFormat, SampleFormat
from .base import CorruptStreamError, Decoder, DecodeError, SourceInfo, normalise_tags

COMPRESSION_NONE = b"NONE"
COMPRESSION_TWOS = b"twos"
COMPRESSION_SOWT = b"sowt"
COMPRESSION_FL32 = b"fl32"
COMPRESSION_FL32_UPPER = b"FL32"
COMPRESSION_FL64 = b"fl64"
COMPRESSION_FL64_UPPER = b"FL64"
COMPRESSION_IMA4 = b"ima4"
COMPRESSION_ALAW = b"alaw"
COMPRESSION_ULAW = b"ulaw"


def read_extended_float(payload: bytes) -> float:
    """Decode an Apple 80-bit IEEE 754 extended precision float."""

    if len(payload) < 10:
        raise CorruptStreamError("AIFF extended float truncated")
    exponent_word = int.from_bytes(payload[0:2], "big", signed=False)
    mantissa = int.from_bytes(payload[2:10], "big", signed=False)
    if exponent_word == 0 and mantissa == 0:
        return 0.0
    sign = -1.0 if exponent_word & 0x8000 else 1.0
    exponent = (exponent_word & 0x7FFF) - 16383
    try:
        return sign * mantissa * (2.0 ** (exponent - 63))
    except OverflowError as exc:
        raise CorruptStreamError("AIFF sample rate is out of range") from exc


class AiffDecoder(Decoder):
    container_name = "AIFF"
    codec_name = "PCM"

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._channels = 0
        self._sample_rate = 0
        self._sample_bits = 0
        self._total_frames = 0
        self._data_offset = 0
        self._data_size = 0
        self._block_align = 0
        self._compression = COMPRESSION_NONE
        self._little_endian = False
        self._is_float = False
        self._unsigned_8bit = False
        self._tags: Dict[str, str] = {}

    @staticmethod
    def matches(path: Path) -> bool:
        try:
            with open(path, "rb") as handle:
                header = handle.read(12)
        except OSError:
            return False
        return len(header) >= 12 and header[0:4] == b"FORM" and header[8:12] in (b"AIFF", b"AIFC")

    def _parse(self) -> None:
        handle = self._open()
        handle.seek(0)
        header = handle.read(12)
        if len(header) < 12:
            raise CorruptStreamError("AIFF header truncated")
        form_type = header[8:12]
        if header[0:4] != b"FORM":
            raise CorruptStreamError("not an AIFF file")
        if form_type not in (b"AIFF", b"AIFC"):
            raise CorruptStreamError(f"unsupported FORM type {form_type!r}")

        offset = 12
        comm_seen = False
        while offset + 8 <= self._file_size:
            handle.seek(offset)
            chunk_header = handle.read(8)
            if len(chunk_header) < 8:
                break
            chunk_id = chunk_header[0:4]
            chunk_size = int.from_bytes(chunk_header[4:8], "big", signed=False)
            payload_offset = offset + 8
            if chunk_id == b"COMM":
                payload = handle.read(min(chunk_size, 256))
                self._parse_comm(payload, form_type)
                comm_seen = True
            elif chunk_id == b"SSND":
                payload = handle.read(min(chunk_size, 16))
                if len(payload) < 8:
                    raise CorruptStreamError("SSND chunk truncated")
                data_start = payload_offset + 8 + int.from_bytes(payload[0:4], "big", signed=False)
                available = max(0, self._file_size - data_start)
                declared = chunk_size - 8
                self._data_offset = data_start
                self._data_size = min(max(0, declared), available)
            elif chunk_id in (b"NAME", b"AUTH", b"ANNO", b"(c) "):
                payload = handle.read(min(chunk_size, 8192))
                text = payload.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
                if text:
                    mapping = {
                        b"NAME": "title",
                        b"AUTH": "artist",
                        b"ANNO": "comment",
                        b"(c) ": "copyright",
                    }
                    self._tags[mapping[chunk_id]] = text
            offset = payload_offset + chunk_size + (chunk_size & 1)

        if not comm_seen:
            raise CorruptStreamError("AIFF file has no COMM chunk")
        if self._channels <= 0 or self._sample_rate <= 0:
            raise CorruptStreamError("AIFF COMM chunk is invalid")
        if self._data_size <= 0:
            raise CorruptStreamError("AIFF file has no SSND payload")
        if self._data_size % self._block_align:
            self._data_size -= self._data_size % self._block_align
        declared_frames = self._total_frames
        actual_frames = self._data_size // self._block_align
        self._total_frames = min(declared_frames, actual_frames) if declared_frames else actual_frames

    def _parse_comm(self, payload: bytes, form_type: bytes) -> None:
        if len(payload) < 18:
            raise CorruptStreamError("COMM chunk truncated")
        self._channels = int.from_bytes(payload[0:2], "big", signed=False)
        self._total_frames = int.from_bytes(payload[2:6], "big", signed=False)
        self._sample_bits = int.from_bytes(payload[6:8], "big", signed=False)
        self._sample_rate = int(round(read_extended_float(payload[8:18])))
        if self._sample_rate <= 0:
            raise CorruptStreamError("AIFF sample rate is zero")
        if form_type == b"AIFC" and len(payload) >= 22:
            self._compression = payload[18:22]
        else:
            self._compression = COMPRESSION_NONE

        compression = self._compression
        if compression in (COMPRESSION_NONE, COMPRESSION_TWOS):
            self._little_endian = False
            self._is_float = False
        elif compression == COMPRESSION_SOWT:
            self._little_endian = True
            self._is_float = False
        elif compression in (COMPRESSION_FL32, COMPRESSION_FL32_UPPER):
            self._little_endian = False
            self._is_float = True
            self._sample_bits = 32
        elif compression in (COMPRESSION_FL64, COMPRESSION_FL64_UPPER):
            self._little_endian = False
            self._is_float = True
            self._sample_bits = 64
        elif compression in (COMPRESSION_IMA4, COMPRESSION_ALAW, COMPRESSION_ULAW):
            raise DecodeError(
                f"AIFF-C compression {compression.decode('latin-1')!r} requires a lossy codec "
                "and is refused by the zero-DSP policy"
            )
        else:
            raise DecodeError(
                f"unsupported AIFF-C compression {compression.decode('latin-1', 'replace')!r}"
            )

        if not self._is_float and self._sample_bits not in (8, 16, 24, 32):
            raise DecodeError(f"unsupported AIFF bit depth: {self._sample_bits}")
        self._block_align = self._channels * max(1, self._sample_bits // 8)
        self._unsigned_8bit = False

    def _build_info(self) -> SourceInfo:
        self._parse()
        if self._is_float:
            sample_format = SampleFormat.FLOAT32 if self._sample_bits == 32 else SampleFormat.FLOAT64
            valid_bits = self._sample_bits
            codec = "IEEE float"
        elif self._sample_bits == 8:
            sample_format = SampleFormat.INT16
            valid_bits = 8
            codec = "PCM 8-bit"
            self._unsigned_8bit = True
        elif self._sample_bits == 16:
            sample_format = SampleFormat.INT16
            valid_bits = 16
            codec = "PCM 16-bit"
        elif self._sample_bits == 24:
            sample_format = SampleFormat.INT24
            valid_bits = 24
            codec = "PCM 24-bit"
        else:
            sample_format = SampleFormat.INT32
            valid_bits = 32
            codec = "PCM 32-bit"

        audio_format = AudioFormat(self._sample_rate, self._channels, sample_format, valid_bits)
        duration = self._total_frames / float(self._sample_rate) if self._sample_rate else 0.0
        bitrate = int(self._data_size * 8 / duration) if duration > 0 else 0
        notes = []
        if self._compression not in (COMPRESSION_NONE, COMPRESSION_TWOS):
            notes.append(f"AIFF-C {self._compression.decode('latin-1', 'replace')}")
        if self._little_endian:
            notes.append("little-endian sample order (sowt)")
        return SourceInfo(
            path=self._path,
            container="AIFF-C" if self._compression != COMPRESSION_NONE else "AIFF",
            codec=codec,
            audio_format=audio_format,
            total_frames=self._total_frames,
            duration=duration,
            tags=normalise_tags(self._tags),
            notes=tuple(notes),
            file_size=self._file_size,
            bitrate=bitrate,
            lossless=True,
        )

    def _swap_if_needed(self, payload: bytes) -> bytes:
        if not self._little_endian:
            return payload
        width = max(1, self._sample_bits // 8)
        if width == 1:
            return payload
        raw = np.frombuffer(payload, dtype=np.uint8)
        groups = raw.reshape(-1, width)[:, ::-1]
        return np.ascontiguousarray(groups).tobytes()

    def read(self, frames: int) -> bytes:
        info = self.info
        if frames <= 0:
            return b""
        remaining = max(0, info.total_frames - self._position)
        want = min(int(frames), remaining)
        if want <= 0:
            return b""
        handle = self._open()
        handle.seek(self._data_offset + self._position * self._block_align)
        payload = handle.read(want * self._block_align)
        if not payload:
            self._position = info.total_frames
            return b""
        produced = len(payload) // self._block_align
        payload = payload[: produced * self._block_align]
        self._position += produced
        payload = self._swap_if_needed(payload)
        if self._unsigned_8bit:
            samples = np.frombuffer(payload, dtype=np.int8).astype(np.int32)
            shifted = np.left_shift(samples, 8).astype("<i2")
            return shifted.tobytes()
        return payload

    def seek(self, frame: int) -> int:
        target = max(0, min(int(frame), self.info.total_frames))
        self._position = target
        return target
