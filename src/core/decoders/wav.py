"""Native RIFF/WAVE and RF64 reader (PCM 8/16/24/32, float 32/64).

The reader never trusts the RIFF size fields: every chunk is validated against
the real file length, so truncated or over-long files produce a clean
``CorruptStreamError`` instead of silent garbage in the DAC buffer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import numpy as np

from ..formats import AudioFormat, SampleFormat
from .base import CorruptStreamError, Decoder, DecodeError, SourceInfo, normalise_tags

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_IEEE_FLOAT = 0x0003
WAVE_FORMAT_ALAW = 0x0006
WAVE_FORMAT_MULAW = 0x0007
WAVE_FORMAT_EXTENSIBLE = 0xFFFE

INFO_TAG_MAP = {
    "INAM": "title",
    "IART": "artist",
    "IPRD": "album",
    "ITRK": "track",
    "IPRT": "track",
    "ICMT": "comment",
    "ICRD": "date",
    "IGNR": "genre",
    "ICOP": "copyright",
    "ISFT": "encoder",
    "IENG": "engineer",
    "ISBJ": "subject",
    "ITOC": "toc",
}


def _read_u16(payload: bytes, offset: int) -> int:
    return int.from_bytes(payload[offset : offset + 2], "little", signed=False)


def _read_u32(payload: bytes, offset: int) -> int:
    return int.from_bytes(payload[offset : offset + 4], "little", signed=False)


def _read_u64(payload: bytes, offset: int) -> int:
    return int.from_bytes(payload[offset : offset + 8], "little", signed=False)


class WavDecoder(Decoder):
    container_name = "WAV"
    codec_name = "PCM"

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._data_offset = 0
        self._data_size = 0
        self._block_align = 0
        self._container_bits = 0
        self._valid_bits = 0
        self._format_code = WAVE_FORMAT_PCM
        self._unsigned_8bit = False
        self._tags: Dict[str, str] = {}
        self._rf64_size = -1

    # ------------------------------------------------------------------ probe
    @staticmethod
    def matches(path: Path) -> bool:
        try:
            with open(path, "rb") as handle:
                header = handle.read(12)
        except OSError:
            return False
        if len(header) < 12:
            return False
        return header[0:4] in (b"RIFF", b"RF64") and header[8:12] == b"WAVE"

    # ------------------------------------------------------------------ parse
    def _parse(self) -> None:
        handle = self._open()
        handle.seek(0)
        header = handle.read(12)
        if len(header) < 12:
            raise CorruptStreamError("WAVE header truncated")
        riff_tag = header[0:4]
        if riff_tag not in (b"RIFF", b"RF64"):
            raise CorruptStreamError(f"not a RIFF file (found {riff_tag!r})")
        if header[8:12] != b"WAVE":
            raise CorruptStreamError("RIFF form type is not WAVE")

        file_length = self._file_size
        offset = 12
        fmt_seen = False
        while offset + 8 <= file_length:
            handle.seek(offset)
            chunk_header = handle.read(8)
            if len(chunk_header) < 8:
                break
            chunk_id = chunk_header[0:4]
            chunk_size = _read_u32(chunk_header, 4)
            payload_offset = offset + 8
            if chunk_id == b"ds64":
                payload = handle.read(min(chunk_size, 64))
                if len(payload) >= 24:
                    self._rf64_size = _read_u64(payload, 8)
            elif chunk_id == b"fmt ":
                payload = handle.read(min(chunk_size, 128))
                self._parse_fmt(payload, chunk_size)
                fmt_seen = True
            elif chunk_id == b"data":
                if chunk_size == 0xFFFFFFFF and self._rf64_size >= 0:
                    chunk_size = self._rf64_size
                available = max(0, file_length - payload_offset)
                if chunk_size > available:
                    chunk_size = available
                self._data_offset = payload_offset
                self._data_size = chunk_size
            elif chunk_id == b"LIST":
                payload = handle.read(min(chunk_size, 65536))
                self._parse_list(payload)
            if chunk_size == 0 and chunk_id in (b"data", b"fmt "):
                break
            offset = payload_offset + chunk_size + (chunk_size & 1)

        if not fmt_seen:
            raise CorruptStreamError("WAVE file has no fmt chunk")
        if self._data_size <= 0 or self._block_align <= 0:
            raise CorruptStreamError("WAVE file has no usable audio data")
        if self._data_size % self._block_align != 0:
            self._data_size -= self._data_size % self._block_align

    def _parse_fmt(self, payload: bytes, chunk_size: int) -> None:
        if len(payload) < 16:
            raise CorruptStreamError("fmt chunk truncated")
        format_code = _read_u16(payload, 0)
        channels = _read_u16(payload, 2)
        sample_rate = _read_u32(payload, 4)
        block_align = _read_u16(payload, 12)
        container_bits = _read_u16(payload, 14)
        valid_bits = container_bits

        if format_code == WAVE_FORMAT_EXTENSIBLE:
            if chunk_size < 40 or len(payload) < 40:
                raise CorruptStreamError("WAVE_FORMAT_EXTENSIBLE requires a 40 byte fmt chunk")
            valid_bits = _read_u16(payload, 18) or container_bits
            sub_format = _read_u16(payload, 24)
            format_code = sub_format

        if format_code == WAVE_FORMAT_ALAW:
            raise DecodeError("A-law WAV is not supported: a companding decoder is a lossy DSP stage")
        if format_code == WAVE_FORMAT_MULAW:
            raise DecodeError("mu-law WAV is not supported: a companding decoder is a lossy DSP stage")
        if channels <= 0:
            raise CorruptStreamError("WAVE channel count is zero")
        if sample_rate <= 0:
            raise CorruptStreamError("WAVE sample rate is zero")
        if block_align == 0:
            block_align = channels * max(1, container_bits // 8)

        expected_align = channels * (container_bits // 8)
        if expected_align and block_align != expected_align and format_code in (
            WAVE_FORMAT_PCM,
            WAVE_FORMAT_IEEE_FLOAT,
        ):
            raise CorruptStreamError(
                f"inconsistent block alignment ({block_align} != {expected_align})"
            )

        if format_code == WAVE_FORMAT_PCM:
            if container_bits == 8:
                self._format_code = WAVE_FORMAT_PCM
                self._unsigned_8bit = True
            elif container_bits == 16:
                self._unsigned_8bit = False
            elif container_bits == 24:
                self._unsigned_8bit = False
            elif container_bits == 32:
                self._unsigned_8bit = False
            else:
                raise DecodeError(f"unsupported PCM bit depth: {container_bits}")
        elif format_code == WAVE_FORMAT_IEEE_FLOAT:
            if container_bits not in (32, 64):
                raise DecodeError(f"unsupported float bit depth: {container_bits}")
        else:
            raise DecodeError(f"unsupported WAVE format tag 0x{format_code:04X}")

        self._format_code = format_code
        self._block_align = block_align
        self._container_bits = container_bits
        self._valid_bits = valid_bits if 0 < valid_bits <= container_bits else container_bits
        self._channels = channels
        self._sample_rate = sample_rate

    def _parse_list(self, payload: bytes) -> None:
        if len(payload) < 4 or payload[0:4] != b"INFO":
            return
        offset = 4
        while offset + 8 <= len(payload):
            tag = payload[offset : offset + 4]
            size = _read_u32(payload, offset + 4)
            start = offset + 8
            end = min(start + size, len(payload))
            raw = payload[start:end]
            offset = start + size + (size & 1)
            if tag == b"ID3 " or tag == b"id3 ":
                continue
            label = INFO_TAG_MAP.get(tag.decode("latin-1", "replace"))
            if not label:
                continue
            text = raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
            if text:
                self._tags[label] = text

    def _build_info(self) -> SourceInfo:
        self._parse()
        if self._format_code == WAVE_FORMAT_IEEE_FLOAT:
            sample_format = SampleFormat.FLOAT32 if self._container_bits == 32 else SampleFormat.FLOAT64
            codec = "IEEE float"
            valid_bits = self._container_bits
            unsigned_8bit = False
        elif self._container_bits == 8:
            sample_format = SampleFormat.INT16
            codec = "PCM 8-bit"
            valid_bits = 8
            unsigned_8bit = True
        elif self._container_bits == 16:
            sample_format = SampleFormat.INT16
            codec = "PCM 16-bit"
            valid_bits = self._valid_bits
            unsigned_8bit = False
        elif self._container_bits == 24:
            sample_format = SampleFormat.INT24
            codec = "PCM 24-bit"
            valid_bits = self._valid_bits
            unsigned_8bit = False
        else:
            sample_format = SampleFormat.INT32
            codec = "PCM 32-bit" if self._valid_bits >= 32 else f"PCM {self._valid_bits}-bit in 32-bit container"
            valid_bits = self._valid_bits
            unsigned_8bit = False

        self._unsigned_8bit = unsigned_8bit
        audio_format = AudioFormat(self._sample_rate, self._channels, sample_format, valid_bits)
        total_frames = self._data_size // self._block_align
        duration = total_frames / float(self._sample_rate)
        notes = []
        if self._data_size and self._data_size % self._block_align:
            notes.append("trailing partial frame ignored")
        if self._valid_bits < self._container_bits:
            notes.append(f"{self._valid_bits} valid bits in a {self._container_bits}-bit container")
        bitrate = int(self._data_size * 8 / duration) if duration > 0 else 0
        return SourceInfo(
            path=self._path,
            container="WAV",
            codec=codec,
            audio_format=audio_format,
            total_frames=total_frames,
            duration=duration,
            tags=normalise_tags(self._tags),
            notes=tuple(notes),
            file_size=self._file_size,
            bitrate=bitrate,
            lossless=True,
        )

    # ------------------------------------------------------------------- read
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
        self._position += produced
        payload = payload[: produced * self._block_align]
        if self._unsigned_8bit:
            samples = np.frombuffer(payload, dtype=np.uint8).astype(np.int16)
            signed = (samples.astype(np.int32) - 128) << 8
            return signed.astype("<i2").tobytes()
        return payload

    def seek(self, frame: int) -> int:
        info = self.info
        target = max(0, min(int(frame), info.total_frames))
        self._position = target
        return target
