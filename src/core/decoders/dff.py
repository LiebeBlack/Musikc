"""Native Philips DSDIFF (.dff) reader.

DSDIFF structure::

    'FRM8' <size> 'DSD '
        'FVER' <size> <version>
        'PROP' <size> 'SND ' 'FS  ' <rate> 'CHNL' <count> <ids> 'CMPR' ...
        'DSD ' <size> <channel-interleaved 4096 byte blocks, MSB first>
        'ID3 ' <size> <ID3v2 tag>

DST compressed streams are rejected: decoding DST requires a dedicated lossless
codec, and silently substituting one would break the bit-perfect contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from ..dsd import DsdFormat, DsdFormatError
from ..formats import AudioFormat
from .base import CorruptStreamError, Decoder, DecodeError, SourceInfo, UnsupportedFeature
from .dsd_blocks import BlockChannelReader

DFF_MAGIC = b"FRM8"
DFF_BLOCK_BYTES = 4096


class DffDecoder(Decoder):
    container_name = "DFF"
    codec_name = "DSD"
    supports_dsd = True

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._dsd_format: DsdFormat | None = None
        self._data_offset = 0
        self._data_size = 0
        self._sample_count = 0
        self._reader: BlockChannelReader | None = None
        self._compression = b"DSD "
        self._format_version = 0
        self._channel_ids: List[str] = []
        self._metadata_offset = 0
        self._metadata_size = 0

    @staticmethod
    def matches(path: Path) -> bool:
        try:
            with open(path, "rb") as handle:
                header = handle.read(12)
        except OSError:
            return False
        return len(header) >= 12 and header[0:4] == DFF_MAGIC and header[12:16] in (b"DSD ", b"DST ")

    def _parse(self) -> None:
        handle = self._open()
        handle.seek(0)
        header = handle.read(16)
        if len(header) < 16 or header[0:4] != DFF_MAGIC:
            raise CorruptStreamError("missing DSDIFF signature")
        if header[12:16] != b"DSD ":
            raise UnsupportedFeature(
                f"DSDIFF form type {header[12:16]!r} is not raw DSD (DST compression is unsupported)"
            )
        offset = 16
        channels = 0
        rate = 0
        while offset + 12 <= self._file_size:
            handle.seek(offset)
            chunk_header = handle.read(12)
            if len(chunk_header) < 12:
                break
            chunk_id = chunk_header[0:4]
            chunk_size = int.from_bytes(chunk_header[4:12], "big", signed=False)
            payload_offset = offset + 12
            if chunk_id == b"FVER":
                payload = handle.read(min(chunk_size, 8))
                self._format_version = int.from_bytes(payload.ljust(4, b"\x00")[0:4], "big")
            elif chunk_id == b"PROP":
                payload = handle.read(min(chunk_size, 1 << 20))
                rate, channels = self._parse_prop(payload)
            elif chunk_id == b"DSD ":
                available = max(0, self._file_size - payload_offset)
                self._data_offset = payload_offset
                self._data_size = min(chunk_size, available)
            elif chunk_id == b"ID3 ":
                self._metadata_offset = payload_offset
                self._metadata_size = min(chunk_size, self._file_size - payload_offset)
            elif chunk_id == b"DST ":
                raise UnsupportedFeature(
                    "DST compressed DSD requires the lossy DST codec and is refused by the zero-DSP policy"
                )
            step = chunk_size + (chunk_size & 1)
            if step <= 0:
                break
            offset = payload_offset + step

        if rate <= 0 or channels <= 0:
            raise CorruptStreamError("DSDIFF PROP/SND metadata is missing sample rate or channels")
        if self._data_size <= 0:
            raise CorruptStreamError("DSDIFF file has no DSD payload")
        try:
            self._dsd_format = DsdFormat(rate, channels)
        except DsdFormatError as exc:
            raise DecodeError(str(exc)) from exc
        per_channel_bytes = self._data_size // channels
        self._sample_count = per_channel_bytes * 8
        self._reader = BlockChannelReader(
            handle=self._open(),
            data_offset=self._data_offset,
            data_size=self._data_size,
            channels=channels,
            block_bytes=DFF_BLOCK_BYTES,
            reverse=False,
            total_bytes_per_channel=per_channel_bytes,
        )

    def _parse_prop(self, payload: bytes) -> tuple[int, int]:
        if len(payload) < 4 or payload[0:4] != b"SND ":
            raise CorruptStreamError("DSDIFF PROP chunk is not a sound property block")
        rate = 0
        channels = 0
        offset = 4
        while offset + 12 <= len(payload):
            chunk_id = payload[offset : offset + 4]
            chunk_size = int.from_bytes(payload[offset + 4 : offset + 12], "big", signed=False)
            body_start = offset + 12
            body = payload[body_start : body_start + chunk_size]
            if chunk_id == b"FS  " and len(body) >= 4:
                rate = int.from_bytes(body[0:4], "big", signed=False)
            elif chunk_id == b"CHNL" and len(body) >= 2:
                channels = int.from_bytes(body[0:2], "big", signed=False)
                self._channel_ids = [
                    body[2 + index * 4 : 6 + index * 4].decode("latin-1", "replace").strip()
                    for index in range(max(0, min(channels, (len(body) - 2) // 4)))
                ]
            elif chunk_id == b"CMPR" and len(body) >= 4:
                self._compression = body[0:4]
                if self._compression not in (b"DSD ", b"DST "):
                    raise DecodeError(
                        f"unsupported DSDIFF compression {self._compression.decode('latin-1', 'replace')!r}"
                    )
            step = chunk_size + (chunk_size & 1)
            if step <= 0:
                break
            offset = body_start + step
        return rate, channels

    def _build_info(self) -> SourceInfo:
        self._parse()
        assert self._dsd_format is not None
        dop = self._dsd_format.dop_format
        duration = self._sample_count / float(self._dsd_format.dsd_rate)
        tags = {}
        if self._metadata_offset > 0 and self._metadata_size > 0:
            from ..metadata import gui_metadata_block, read_id3v2, read_id3v2_from_file

            if self._metadata_size > 10:
                payload = self._read_at(self._metadata_offset, min(self._metadata_size, 4 * 1024 * 1024))
                tags = gui_metadata_block(read_id3v2(payload) or read_id3v2_from_file(self._path, self._metadata_offset))
        notes = [
            f"format version {self._format_version}",
            f"compression {self._compression.decode('latin-1', 'replace').strip()}",
            f"channel ids {','.join(self._channel_ids) if self._channel_ids else 'unspecified'}",
            "MSB-first bit order used directly for DoP",
        ]
        bitrate = int(self._data_size * 8 / duration) if duration > 0 else 0
        return SourceInfo(
            path=self._path,
            container="DFF",
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
        raise DecodeError("DFF streams must be consumed through read_dsd() and the DoP encoder")

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


def dff_audio_format(rate: int, channels: int) -> AudioFormat:
    return DsdFormat(rate, channels).dop_format
