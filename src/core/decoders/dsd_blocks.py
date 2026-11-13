"""Block-interleaved 1-bit readers shared by DSF and DFF.

Both containers store DSD as fixed-size blocks per channel that are interleaved
channel by channel.  DSF uses LSB-first bit order, DFF uses MSB-first.  DoP
requires MSB-first, so DSF payloads are bit-reversed (a pure bit-order
permutation, not a filter).
"""

from __future__ import annotations

from typing import List

from ..dsd import reverse_bits
from .base import CorruptStreamError

DSD_SILENCE_BYTE = 0x69


class BlockChannelReader:
    """Streams per-channel byte payloads out of block interleaved DSD data."""

    def __init__(
        self,
        handle,
        data_offset: int,
        data_size: int,
        channels: int,
        block_bytes: int,
        reverse: bool,
        total_bytes_per_channel: int,
    ) -> None:
        if channels <= 0:
            raise CorruptStreamError("DSD channel count must be positive")
        if block_bytes <= 0:
            raise CorruptStreamError("DSD block size must be positive")
        self._handle = handle
        self._data_offset = data_offset
        self._data_size = max(0, data_size)
        self._channels = channels
        self._block_bytes = block_bytes
        self._reverse = reverse
        self._buffers: List[bytearray] = [bytearray() for _ in range(channels)]
        self._block_index = 0
        self._block_offset = 0
        self._consumed = 0
        self._total = max(0, total_bytes_per_channel)
        self._exhausted = False

    @property
    def channels(self) -> int:
        return self._channels

    @property
    def consumed_bytes(self) -> int:
        return self._consumed

    @property
    def total_bytes(self) -> int:
        return self._total

    @property
    def exhausted(self) -> bool:
        return self._exhausted and all(len(buffer) == 0 for buffer in self._buffers)

    def _block_span(self) -> int:
        return self._block_bytes * self._channels

    def _load_next_block(self) -> bool:
        span = self._block_span()
        position = self._data_offset + self._block_index * span
        length = min(span, max(0, self._data_size - self._block_index * span))
        if length <= 0:
            self._exhausted = True
            return False
        self._handle.seek(position)
        payload = self._handle.read(length)
        if len(payload) < length:
            payload = payload + bytes([DSD_SILENCE_BYTE]) * (length - len(payload))
        for channel in range(self._channels):
            start = channel * self._block_bytes
            chunk = payload[start : start + self._block_bytes]
            if len(chunk) < self._block_bytes:
                chunk = chunk + bytes([DSD_SILENCE_BYTE]) * (self._block_bytes - len(chunk))
            if self._reverse:
                chunk = reverse_bits(chunk)
            self._buffers[channel].extend(chunk)
        self._block_index += 1
        self._block_offset = 0
        if self._data_size <= self._block_index * span:
            self._exhausted = True
        return True

    def read(self, bytes_per_channel: int) -> List[bytes]:
        """Return ``bytes_per_channel`` bytes for every channel."""

        if bytes_per_channel <= 0:
            return [b"" for _ in range(self._channels)]
        while len(self._buffers[0]) < bytes_per_channel:
            if not self._load_next_block():
                break
        if len(self._buffers[0]) < bytes_per_channel:
            return [b"" for _ in range(self._channels)]
        output: List[bytes] = []
        for channel in range(self._channels):
            buffer = self._buffers[channel]
            output.append(bytes(buffer[:bytes_per_channel]))
            del buffer[:bytes_per_channel]
        self._consumed += bytes_per_channel
        return output

    def seek(self, byte_offset: int) -> int:
        """Reposition at an absolute per-channel byte offset."""

        target = max(0, min(int(byte_offset), self._total))
        span = self._block_span()
        block_index = target // self._block_bytes
        offset_in_block = target % self._block_bytes
        for buffer in self._buffers:
            buffer.clear()
        self._block_index = block_index
        self._block_offset = offset_in_block
        self._consumed = block_index * self._block_bytes
        self._exhausted = False
        if offset_in_block:
            begin = self._data_offset + block_index * span
            length = min(span, max(0, self._data_size - block_index * span))
            if length <= 0:
                self._exhausted = True
                return self._consumed
            self._handle.seek(begin)
            payload = self._handle.read(length)
            for channel in range(self._channels):
                start = channel * self._block_bytes + offset_in_block
                chunk = payload[start : (channel + 1) * self._block_bytes]
                if self._reverse:
                    chunk = reverse_bits(chunk)
                self._buffers[channel].extend(chunk)
            self._block_index += 1
            if self._data_size <= self._block_index * span:
                self._exhausted = True
        self._consumed = target
        return target
