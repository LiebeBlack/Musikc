"""Native Sony DSF (DSD Stream File) reader.

DSF layout::

    'DSD ' chunk   (28 bytes) : total file size, pointer to ID3v2 metadata
    'fmt ' chunk   (52 bytes) : version, channel type, channels, DSD rate,
                                bit depth (1), sample count, block size
    'data' chunk              : channel-interleaved blocks, LSB-first bits

The 1-bit stream is delivered verbatim (bit-reversed once, because DSF is
LSB-first while DoP is MSB-first) and never converted to PCM.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from ..dsd import DsdFormat, DsdFormatError
from ..formats import AudioFormat
from .base import CorruptStreamError, Decoder, DecodeError, SourceInfo
from .dsd_blocks import BlockChannelReader

DSF_MAGIC = b"DSD "
DSF_DEFAULT_BLOCK = 4096
CHANNEL_TYPE_NAMES = {
    0: "unspecified",
    1: "mono",
    2: "stereo",
    3: "3 channels",
    4: "quad",
    5: "4 channels",
    6: "5 channels",
    7: "5.1",
}


class DsfDecoder(Decoder):
    container_name = "DSF"
    codec_name = "DSD"
    supports_dsd = True

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._dsd_format: DsdFormat | None = None
        self._sample_count = 0
        self._block_bytes = DSF_DEFAULT_BLOCK
        self._data_offset = 0
        self._data_size = 0
        self._metadata_offset = 0
        self._reader: BlockChannelReader | None = None
        self._channel_type = 0
        self._format_version = 0

    @staticmethod
    def matches(path: Path) -> bool:
        try:
            with open(path, "rb") as handle:
                header = handle.read(4)
        except OSError:
            return False
        return header == DSF_MAGIC

    def _parse(self) -> None:
        handle = self._open()
        handle.seek(0)
        header = handle.read(28)
        if len(header) < 28 or header[0:4] != DSF_MAGIC:
            raise CorruptStreamError("missing DSF signature")
        header_size = int.from_bytes(header[4:12], "little", signed=False)
        declared_size = int.from_bytes(header[12:20], "little", signed=False)
        self._metadata_offset = int.from_bytes(header[20:28], "little", signed=False)
        if header_size not in (0, 28):
            raise CorruptStreamError(f"unexpected DSF header size {header_size} (expected 28)")
        if declared_size and declared_size > self._file_size + 1024:
            raise CorruptStreamError(
                f"DSF header declares {declared_size} bytes but the file has {self._file_size}"
            )

        offset = 28
        fmt_seen = False
        data_seen = False
        while offset + 12 <= self._file_size:
            handle.seek(offset)
            chunk_header = handle.read(12)
            if len(chunk_header) < 12:
                break
            chunk_id = chunk_header[0:4]
            chunk_size = int.from_bytes(chunk_header[4:12], "little", signed=False)
            payload_offset = offset + 12
            if chunk_size < 12:
                raise CorruptStreamError(f"invalid DSF chunk size {chunk_size} for {chunk_id!r}")
            if chunk_id == b"fmt ":
                payload = handle.read(min(chunk_size - 12, 128))
                if len(payload) < 40:
                    raise CorruptStreamError("DSF fmt chunk is truncated")
                self._parse_fmt(payload)
                fmt_seen = True
            elif chunk_id == b"data":
                available = max(0, self._file_size - payload_offset)
                self._data_offset = payload_offset
                self._data_size = min(chunk_size - 12, available) if chunk_size > 12 else available
                data_seen = True
            offset = payload_offset + (chunk_size - 12)
            if data_seen:
                break

        if not fmt_seen:
            raise CorruptStreamError("DSF file has no fmt chunk")
        if not data_seen or self._data_size <= 0:
            raise CorruptStreamError("DSF file has no audio payload")
        if self._dsd_format is None:
            raise CorruptStreamError("DSF format description missing")

        bits_per_block = self._block_bytes * 8
        blocks = (self._sample_count + bits_per_block - 1) // bits_per_block
        declared_bytes = blocks * self._block_bytes * self._dsd_format.channels
        if declared_bytes > self._data_size:
            per_channel = self._data_size // max(1, self._dsd_format.channels)
            self._sample_count = min(self._sample_count, per_channel * 8)

        self._reader = BlockChannelReader(
            handle=self._open(),
            data_offset=self._data_offset,
            data_size=self._data_size,
            channels=self._dsd_format.channels,
            block_bytes=self._block_bytes,
            reverse=True,
            total_bytes_per_channel=self._sample_count // 8,
        )

    def _parse_fmt(self, payload: bytes) -> None:
        self._format_version = int.from_bytes(payload[0:4], "little", signed=False)
        format_id = int.from_bytes(payload[4:8], "little", signed=False)
        self._channel_type = int.from_bytes(payload[8:12], "little", signed=False)
        channels = int.from_bytes(payload[12:16], "little", signed=False)
        rate = int.from_bytes(payload[16:20], "little", signed=False)
        bits = int.from_bytes(payload[20:24], "little", signed=False)
        sample_count = int.from_bytes(payload[24:32], "little", signed=False)
        block_bytes = int.from_bytes(payload[32:36], "little", signed=False)

        if format_id != 0:
            raise DecodeError(f"unsupported DSF format id {format_id} (only raw DSD is defined)")
        if bits not in (1, 8):
            raise DecodeError(f"unsupported DSF bit depth {bits} (expected 1)")
        if channels <= 0:
            raise CorruptStreamError("DSF channel count is zero")
        if rate <= 0:
            raise CorruptStreamError("DSF sample rate is zero")
        if sample_count <= 0:
            raise CorruptStreamError("DSF sample count is zero")
        if block_bytes <= 0 or block_bytes > 1 << 20:
            raise CorruptStreamError(f"invalid DSF block size {block_bytes}")
        if block_bytes % 8 != 0:
            raise CorruptStreamError("DSF block size must be a multiple of 8 bytes")

        try:
            self._dsd_format = DsdFormat(rate, channels)
        except DsdFormatError as exc:
            raise DecodeError(str(exc)) from exc
        self._sample_count = sample_count
        self._block_bytes = block_bytes

    def _build_info(self) -> SourceInfo:
        self._parse()
        assert self._dsd_format is not None
        dop = self._dsd_format.dop_format
        duration = self._sample_count / float(self._dsd_format.dsd_rate)
        tags = {}
        if self._metadata_offset > 0:
            from ..metadata import read_id3v2_from_file, gui_metadata_block

            tags = gui_metadata_block(read_id3v2_from_file(self._path, self._metadata_offset))
        notes = [
            f"format version {self._format_version}",
            f"{CHANNEL_TYPE_NAMES.get(self._channel_type, 'custom layout')}",
            f"block size {self._block_bytes}",
            "LSB-first bit order converted to MSB-first for DoP",
        ]
        bitrate = int(self._data_size * 8 / duration) if duration > 0 else 0
        return SourceInfo(
            path=self._path,
            container="DSF",
            codec="DSD",
            audio_format=dop,
            total_frames=self._sample_count,
            duration=duration,
            dsd_format=self._dsd_format,
            tags=tags,
            notes=tuple(notes),
            file_size=self._file_size,
            bitrate=bitrate,
            lossless=True,
        )

    def read(self, frames: int) -> bytes:
        raise DecodeError("DSF streams must be consumed through read_dsd() and the DoP encoder")

    def read_dsd(self, bits_per_channel: int) -> List[bytes]:
        if self._reader is None:
            self.info
        assert self._reader is not None
        if bits_per_channel % 8 != 0:
            raise DecodeError("DSD reads must be byte aligned")
        remaining = max(0, self.info.total_frames - self._position)
        bits = min(int(bits_per_channel), remaining)
        # DoP consumes whole 16-bit words per channel; a trailing partial word
        # cannot be framed and is dropped (it is shorter than one DoP frame).
        bits -= bits % 16
        if bits <= 0:
            return []
        payload = self._reader.read(bits // 8)
        if not payload or not payload[0]:
            self._position = self.info.total_frames
            return []
        self._position += bits
        return payload

    def seek(self, frame: int) -> int:
        info = self.info
        target = max(0, min(int(frame), info.total_frames))
        target -= target % 16
        if self._reader is not None:
            self._reader.seek(target // 8)
        self._position = target
        return target

    def close(self) -> None:
        self._reader = None
        super().close()


def dsf_audio_format(rate: int, channels: int) -> AudioFormat:
    """Expose the DoP transport format for a DSF stream (helper for tests)."""

    return DsdFormat(rate, channels).dop_format
