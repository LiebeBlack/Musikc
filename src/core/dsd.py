"""Native DSD handling and DoP (DSD over PCM) encapsulation.

No sigma-delta modulation, no downsampling, no low-pass, no DSD-to-PCM
conversion is performed: the 1-bit stream is embedded verbatim into 24-bit PCM
frames following the DoP v1.0 open specification, which is the only bit-exact
way to transport DSD through a standard USB Audio Class driver.

DoP frame layout (24-bit little endian container, stereo example):

    byte 0 : DSD bits 15..8   of channel 0   (MSB first)
    byte 1 : DSD bits  7..0   of channel 0
    byte 2 : marker 0x05 | 0xFA  (alternates every frame)
    byte 3 : DSD bits 15..8   of channel 1
    byte 4 : DSD bits  7..0   of channel 1
    byte 5 : marker 0x05 | 0xFA

Each DoP frame therefore carries 16 DSD samples per channel, so the PCM
transport clock is always ``dsd_rate / 16`` (DSD64 -> 176.4 kHz,
DSD128 -> 352.8 kHz, DSD256 -> 705.6 kHz, DSD512 -> 1411.2 kHz).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

import numpy as np

from .formats import AudioFormat, SampleFormat

DOP_MARKER_A = 0x05
DOP_MARKER_B = 0xFA
DOP_SAMPLES_PER_FRAME = 16
DOP_BYTES_PER_CHANNEL = DOP_SAMPLES_PER_FRAME // 8
DOP_CONTAINER = SampleFormat.INT24

DSD_BASE_RATE = 2_822_400
DSD_RATES: Dict[int, int] = {
    64: DSD_BASE_RATE,
    128: DSD_BASE_RATE * 2,
    256: DSD_BASE_RATE * 4,
    512: DSD_BASE_RATE * 8,
}
DSD_RATE_TO_DSD: Dict[int, int] = {rate: multiplier for multiplier, rate in DSD_RATES.items()}

SUPPORTED_DSD_CHANNELS = (1, 2, 3, 4, 5, 6)


def _build_reverse_table() -> np.ndarray:
    table = bytearray(256)
    for value in range(256):
        reversed_bits = int(f"{value:08b}"[::-1], 2)
        table[value] = reversed_bits
    return np.frombuffer(bytes(table), dtype=np.uint8)


BIT_REVERSE_TABLE = _build_reverse_table()


class DsdFormatError(ValueError):
    """Raised when a DSD stream is malformed or unsupported."""


@dataclass(frozen=True, slots=True)
class DsdFormat:
    """Description of a native 1-bit stream."""

    dsd_rate: int
    channels: int
    modulation: str = "DSD"

    def __post_init__(self) -> None:
        if self.dsd_rate <= 0:
            raise DsdFormatError(f"invalid DSD rate: {self.dsd_rate}")
        if self.channels <= 0:
            raise DsdFormatError(f"invalid DSD channel count: {self.channels}")

    @property
    def multiplier(self) -> int:
        return DSD_RATE_TO_DSD.get(self.dsd_rate, max(1, self.dsd_rate // DSD_BASE_RATE))

    @property
    def label(self) -> str:
        return f"DSD{self.multiplier if self.multiplier in DSD_RATES else self.dsd_rate // 44100}"

    @property
    def rate_mhz(self) -> float:
        return self.dsd_rate / 1_000_000.0

    @property
    def dop_rate(self) -> int:
        return self.dsd_rate // DOP_SAMPLES_PER_FRAME

    @property
    def dop_format(self) -> AudioFormat:
        return AudioFormat(self.dop_rate, self.channels, DOP_CONTAINER, 24, dop_encoded=True)

    @property
    def bytes_per_second(self) -> int:
        return (self.dsd_rate // 8) * self.channels

    def describe(self) -> str:
        return f"{self.label} ({self.rate_mhz:.4f} MHz) · {self.channels} channel(s) · DoP @ {self.dop_rate} Hz"


def reverse_bits(payload: bytes) -> bytes:
    """Convert LSB-first (DSF) bit order to MSB-first (DoP) bit order."""

    if not payload:
        return b""
    source = np.frombuffer(payload, dtype=np.uint8)
    return BIT_REVERSE_TABLE[source].tobytes()


def dsd_bytes_for_frames(frames: int) -> int:
    """Bytes per channel required to carry ``frames`` DoP frames."""

    return frames * DOP_BYTES_PER_CHANNEL


class DopEncoder:
    """Encapsulates interleaved-by-block 1-bit channel payloads into DoP."""

    __slots__ = ("_channels", "_dsd_format", "_phase", "_frames")

    def __init__(self, dsd_format: DsdFormat) -> None:
        self._dsd_format = dsd_format
        self._channels = dsd_format.channels
        self._phase = 0
        self._frames = 0

    @property
    def format(self) -> AudioFormat:
        return self._dsd_format.dop_format

    @property
    def frames_encoded(self) -> int:
        return self._frames

    def reset(self) -> None:
        self._phase = 0
        self._frames = 0

    def encode(self, channel_payloads: Sequence[bytes]) -> bytes:
        """Return DoP PCM bytes for the supplied per-channel 1-bit payloads.

        ``channel_payloads`` must contain MSB-first byte packed DSD, one entry
        per channel, all of identical length and an even number of bytes.
        """

        if len(channel_payloads) != self._channels:
            raise DsdFormatError(
                f"DoP encoder expects {self._channels} channels, received {len(channel_payloads)}"
            )
        if not channel_payloads:
            return b""
        length = len(channel_payloads[0])
        if length == 0:
            return b""
        for index, payload in enumerate(channel_payloads):
            if len(payload) != length:
                raise DsdFormatError(
                    f"channel {index} payload length {len(payload)} differs from {length}"
                )
        if length % DOP_BYTES_PER_CHANNEL != 0:
            raise DsdFormatError("DoP payload must contain a whole number of 16-bit DSD frames")
        frames = length // DOP_BYTES_PER_CHANNEL
        words: List[np.ndarray] = []
        for payload in channel_payloads:
            data = np.frombuffer(payload, dtype=np.uint8)
            high = data[0::2].astype(np.uint16)
            low = data[1::2].astype(np.uint16)
            words.append((high << 8) | low)
        output = np.empty((frames, self._channels * 3), dtype=np.uint8)
        frame_indices = np.arange(frames, dtype=np.uint32) + np.uint32(self._phase)
        markers = np.where((frame_indices & 1) == 0, DOP_MARKER_A, DOP_MARKER_B).astype(np.uint8)
        for channel in range(self._channels):
            word = words[channel]
            base = channel * 3
            output[:, base] = (word & 0xFF).astype(np.uint8)
            output[:, base + 1] = (word >> 8).astype(np.uint8)
            output[:, base + 2] = markers
        self._phase = (self._phase + frames) & 1
        self._frames += frames
        return output.tobytes()


def validate_dop_payload(payload: bytes, channels: int) -> Tuple[bool, str]:
    """Structurally validate a DoP buffer, used by the integrity monitor."""

    frame_bytes = channels * 3
    if len(payload) % frame_bytes != 0:
        return False, "payload is not a whole number of DoP frames"
    frames = len(payload) // frame_bytes
    markers_a = 0
    markers_b = 0
    for frame in range(frames):
        base = frame * frame_bytes
        for channel in range(channels):
            marker = payload[base + channel * 3 + 2]
            if marker == DOP_MARKER_A:
                markers_a += 1
            elif marker == DOP_MARKER_B:
                markers_b += 1
            else:
                return False, f"illegal DoP marker 0x{marker:02X} at frame {frame}"
    if frames <= 1:
        return True, "single DoP frame"
    if markers_a == 0 or markers_b == 0:
        return False, "DoP marker alternation missing (decoder may be misaligned)"
    return True, f"{frames} DoP frames validated"


def dop_rate_for(rate: int) -> int:
    return rate // DOP_SAMPLES_PER_FRAME


def describe_dsd_capability(rates: Sequence[int]) -> str:
    """Return a compact list of the DoP rates a DAC accepted during probing."""

    dop = sorted({dop_rate_for(rate) for rate in rates if rate % DOP_SAMPLES_PER_FRAME == 0})
    return ", ".join(f"{rate / 1000:.1f} kHz" for rate in dop) if dop else "none"
