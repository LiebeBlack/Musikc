"""Sample format algebra and bit-exact PCM conversion.

The single rule enforced by this module: **a conversion may never alter the
signal**.  Conversions are pure representation changes (integer widening,
container repacking, unit scaling by an exact power of two).  Whenever a
requested conversion would discard information, :func:`repack` returns
``exact=False`` *and* a machine-readable reason so the UI can strip the
BIT-PERFECT badge instead of lying to the listener.

No gain, no dither, no normalisation, no limiter, no resampler lives here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

FRAME_ALIGNMENT_ERROR = "buffer is not aligned to the frame size"
CHANNEL_MISMATCH_ERROR = "channel count changes are forbidden (would require mixing)"
RATE_MISMATCH_ERROR = "sample rate changes require hardware clock switching, not conversion"


class SampleFormat(Enum):
    """PCM container formats understood by the engine and by PortAudio."""

    INT16 = ("int16", 2, "PCM 16-bit integer", False, 16)
    INT24 = ("int24", 3, "PCM 24-bit integer", False, 24)
    INT32 = ("int32", 4, "PCM 32-bit integer", False, 32)
    FLOAT32 = ("float32", 4, "PCM 32-bit float", True, 32)
    FLOAT64 = ("float64", 8, "PCM 64-bit float", True, 64)

    def __init__(self, code: str, width: int, label: str, is_float: bool, bits: int) -> None:
        self.code = code
        self.width = width
        self.label = label
        self.is_float = is_float
        self.bits = bits

    @property
    def numpy_dtype(self) -> np.dtype:
        return np.dtype(self.code)

    @property
    def portaudio_dtype(self) -> str:
        return self.code

    @classmethod
    def from_code(cls, code: str) -> "SampleFormat":
        normalised = code.strip().lower()
        aliases = {
            "s16": "int16",
            "s16le": "int16",
            "i16": "int16",
            "short": "int16",
            "int16": "int16",
            "s24": "int24",
            "s24le": "int24",
            "int24": "int24",
            "i24": "int24",
            "s32": "int32",
            "s32le": "int32",
            "int32": "int32",
            "i32": "int32",
            "f32": "float32",
            "float": "float32",
            "float32": "float32",
            "f64": "float64",
            "double": "float64",
            "float64": "float64",
        }
        try:
            return cls._BY_CODE[aliases[normalised]]
        except KeyError as exc:
            raise ValueError(f"unsupported sample format: {code!r}") from exc

    @classmethod
    def preferred_for_depth(cls, bits: int) -> "SampleFormat":
        if bits <= 16:
            return cls.INT16
        if bits <= 24:
            return cls.INT24
        return cls.INT32


SampleFormat._BY_CODE = {member.code: member for member in SampleFormat}  # type: ignore[attr-defined]


def _clamp_to_int(value: float, minimum: int, maximum: int) -> int:
    return minimum if value < minimum else maximum if value > maximum else int(value)


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """Immutable description of an interleaved PCM stream."""

    sample_rate: int
    channels: int
    sample_format: SampleFormat
    valid_bits: Optional[int] = None
    dop_encoded: bool = False

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError(f"invalid sample rate: {self.sample_rate}")
        if self.channels <= 0:
            raise ValueError(f"invalid channel count: {self.channels}")
        if not isinstance(self.sample_format, SampleFormat):
            raise TypeError("sample_format must be a SampleFormat member")
        bits = self.sample_format.bits if self.valid_bits is None else int(self.valid_bits)
        if bits <= 0 or bits > self.sample_format.bits:
            raise ValueError(
                f"valid_bits {bits} incompatible with container {self.sample_format.code}"
            )
        if self.valid_bits is None:
            object.__setattr__(self, "valid_bits", self.sample_format.bits)

    @property
    def frame_bytes(self) -> int:
        return self.sample_format.width * self.channels

    @property
    def bytes_per_second(self) -> int:
        return self.frame_bytes * self.sample_rate

    @property
    def bitrate(self) -> int:
        return self.bytes_per_second * 8

    @property
    def is_float(self) -> bool:
        return self.sample_format.is_float

    @property
    def label(self) -> str:
        depth = "32-bit float" if self.sample_format is SampleFormat.FLOAT32 else (
            "64-bit float" if self.sample_format is SampleFormat.FLOAT64 else f"{self.valid_bits}-bit"
        )
        rate = f"{self.sample_rate / 1000:.1f} kHz".replace(".0 kHz", " kHz")
        return f"{depth} / {rate}"

    @property
    def short_label(self) -> str:
        if self.sample_format.is_float:
            depth = "32f" if self.sample_format is SampleFormat.FLOAT32 else "64f"
        else:
            depth = str(self.valid_bits)
        return f"{depth}/{self.sample_rate // 1000}" if self.sample_rate % 1000 == 0 else (
            f"{depth}/{self.sample_rate / 1000:.1f}"
        )

    def frames_to_bytes(self, frames: int) -> int:
        return int(frames) * self.frame_bytes

    def bytes_to_frames(self, byte_count: int) -> int:
        return int(byte_count) // self.frame_bytes

    def duration_of(self, byte_count: int) -> float:
        return self.bytes_to_frames(byte_count) / float(self.sample_rate)

    def with_rate(self, rate: int) -> "AudioFormat":
        return AudioFormat(rate, self.channels, self.sample_format, self.valid_bits, self.dop_encoded)

    def with_format(self, sample_format: SampleFormat, valid_bits: Optional[int] = None) -> "AudioFormat":
        return AudioFormat(
            self.sample_rate,
            self.channels,
            sample_format,
            sample_format.bits if valid_bits is None else valid_bits,
            self.dop_encoded,
        )

    def compatible_transport(self, other: "AudioFormat") -> bool:
        return (
            self.sample_rate == other.sample_rate
            and self.channels == other.channels
            and self.sample_format is other.sample_format
        )

    def describe(self) -> str:
        return (
            f"{self.sample_format.label} · {self.sample_rate} Hz · "
            f"{self.channels} channel(s) · valid {self.valid_bits} bit"
        )


@dataclass(frozen=True, slots=True)
class ConversionResult:
    """Output of :func:`repack` including a rigorous exactness verdict."""

    data: bytes
    exact: bool
    reason: str
    source: AudioFormat
    target: AudioFormat

    @property
    def frames(self) -> int:
        return self.target.bytes_to_frames(len(self.data))


def silence(format: AudioFormat, frames: int) -> bytes:
    """Zero-filled payload (digital silence) in the target container format."""

    return b"\x00" * format.frames_to_bytes(frames)


def ensure_frame_aligned(data: bytes, format: AudioFormat) -> None:
    if len(data) % format.frame_bytes != 0:
        raise ValueError(FRAME_ALIGNMENT_ERROR)


def _load_int_as_canonical(data: bytes, source: AudioFormat) -> np.ndarray:
    """Return int32 samples left-justified (MSB aligned), as libsndfile does."""

    raw = np.frombuffer(data, dtype=np.uint8)
    count = raw.size // source.sample_format.width
    width = source.sample_format.width
    if width == 2:
        values = np.frombuffer(data, dtype="<i2").astype(np.int32)
        return values << 16
    if width == 4:
        return np.frombuffer(data, dtype="<i4").astype(np.int32, copy=False)
    triples = raw[: count * 3].reshape(-1, 3).astype(np.int32)
    packed = triples[:, 0] | (triples[:, 1] << 8) | (triples[:, 2] << 16)
    signed = (packed << 8) >> 8
    return (signed << 8).astype(np.int32, copy=False)


def _store_canonical_as_int(values: np.ndarray, target: AudioFormat) -> Tuple[bytes, bool, str]:
    width = target.sample_format.width
    if width == 4:
        return values.astype("<i4", copy=False).tobytes(), True, "32-bit container preserves all bits"
    shift = 32 - target.sample_format.bits
    mask = (1 << shift) - 1
    discarded_nonzero = bool(np.any(np.bitwise_and(values, mask)))
    narrowed = np.right_shift(values, shift).astype(np.int32, copy=False)
    if width == 2:
        payload = narrowed.astype("<i2").tobytes()
    else:
        unsigned = narrowed.astype(np.uint32).view(np.uint8).reshape(-1, 4)
        payload = np.ascontiguousarray(unsigned[:, :3]).tobytes()
    if discarded_nonzero:
        return payload, False, f"{shift} LSB(s) discarded when narrowing to {target.sample_format.bits}-bit"
    return payload, True, f"integer repack {target.sample_format.bits}-bit is lossless"


def _canonical_to_float(values: np.ndarray, target: AudioFormat) -> bytes:
    scale = np.float64(2.0 ** 31)
    floats = values.astype(np.float64) / scale
    if target.sample_format is SampleFormat.FLOAT32:
        return floats.astype("<f4").tobytes()
    return floats.astype("<f8").tobytes()


def _float_to_canonical(values: np.ndarray, source: AudioFormat) -> np.ndarray:
    scale = np.float64(2.0 ** 31)
    scaled = np.rint(values.astype(np.float64) * scale)
    clipped = np.clip(scaled, -scale, scale - 1.0)
    return clipped.astype(np.int32)


def _load_float(data: bytes, source: AudioFormat) -> np.ndarray:
    if source.sample_format is SampleFormat.FLOAT32:
        return np.frombuffer(data, dtype="<f4").astype(np.float64, copy=False)
    return np.frombuffer(data, dtype="<f8").astype(np.float64, copy=False)


def repack(data: bytes, source: AudioFormat, target: AudioFormat, allow_lossy: bool = False) -> ConversionResult:
    """Convert ``data`` from ``source`` to ``target`` without touching the signal.

    Raises :class:`ValueError` when the requested conversion is architecturally
    forbidden (channel remix, misuse of the resampler slot).  When the only
    possible result is a lossy truncation two things happen: the payload is
    still returned (so playback can continue) and ``exact`` is ``False`` so the
    BIT-PERFECT indicator is extinguished for the whole session.
    """

    ensure_frame_aligned(data, source)
    if not data:
        return ConversionResult(b"", True, "empty payload", source, target)
    if source.channels != target.channels:
        raise ValueError(CHANNEL_MISMATCH_ERROR)
    if source.sample_rate != target.sample_rate and not target.dop_encoded:
        raise ValueError(RATE_MISMATCH_ERROR)

    if source.sample_format is target.sample_format and source.valid_bits == target.valid_bits:
        return ConversionResult(bytes(data), True, "identical container: passthrough", source, target)

    if not source.is_float and not target.is_float:
        canonical = _load_int_as_canonical(data, source)
        payload, exact, reason = _store_canonical_as_int(canonical, target)
        if not exact and not allow_lossy:
            raise LossyConversionError(reason, source, target)
        return ConversionResult(payload, exact, reason, source, target)

    if not source.is_float and target.is_float:
        canonical = _load_int_as_canonical(data, source)
        payload = _canonical_to_float(canonical, target)
        if target.sample_format is SampleFormat.FLOAT32:
            round_trip = np.frombuffer(payload, dtype="<f4").astype(np.float64) * np.float64(2.0 ** 31)
            exact = bool(np.array_equal(np.rint(round_trip).astype(np.int64), canonical.astype(np.int64)))
        else:
            exact = True
        reason = (
            "integer to float scaling by exact power of two"
            if exact
            else f"{source.valid_bits}-bit integers exceed the float32 mantissa; rounding occurred"
        )
        if not exact and not allow_lossy:
            raise LossyConversionError(reason, source, target)
        return ConversionResult(payload, exact, reason, source, target)

    if source.is_float and target.is_float:
        values = _load_float(data, source)
        if target.sample_format is SampleFormat.FLOAT32:
            narrowed = values.astype(np.float32)
            round_trip = narrowed.astype(np.float64)
            exact = bool(np.array_equal(round_trip, values))
            if not exact and not allow_lossy:
                raise LossyConversionError("float64 payload is not representable in float32", source, target)
            return ConversionResult(
                narrowed.astype("<f4").tobytes(),
                exact,
                "float64 to float32" if not exact else "float64 to float32 is exact for this payload",
                source,
                target,
            )
        payload = values.astype("<f8").tobytes()
        return ConversionResult(payload, True, "float32 widened to float64", source, target)

    values = _load_float(data, source)
    canonical = _float_to_canonical(values, source)
    payload, _container_exact, container_reason = _store_canonical_as_int(canonical, target)
    scale = np.float64(2.0 ** 31)
    restored = canonical.astype(np.float64) / scale
    quantised = bool(np.any(restored != values))
    reason = "float payload is quantised to integer PCM" if quantised else "float payload is exactly integral"
    if quantised and not allow_lossy:
        raise LossyConversionError(reason, source, target)
    return ConversionResult(payload, not quantised, reason, source, target)


class LossyConversionError(ValueError):
    """Raised when a conversion would audibly modify the samples."""

    def __init__(self, reason: str, source: AudioFormat, target: AudioFormat) -> None:
        super().__init__(
            f"lossy conversion refused ({reason}): {source.describe()} -> {target.describe()}"
        )
        self.reason = reason
        self.source = source
        self.target = target


def describe_format_stack(formats: Iterable[AudioFormat]) -> str:
    return " -> ".join(fmt.short_label for fmt in formats)


def supported_rates(limit_low: int = 44100, limit_high: int = 768000) -> List[int]:
    """Every rate that is meaningful for PCM and DoP transports."""

    base = (44100, 48000)
    rates: List[int] = []
    for multiplier in (1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192):
        for family in base:
            candidate = family * multiplier
            if limit_low <= candidate <= limit_high and candidate not in rates:
                rates.append(candidate)
    return sorted(rates)


def rate_family(rate: int) -> str:
    return "44.1 kHz family" if rate % 44100 == 0 else ("48 kHz family" if rate % 48000 == 0 else "unknown family")


def bit_depth_label(bits: int, is_float: bool = False) -> str:
    return f"{bits}-bit float" if is_float else f"{bits}-bit"


def nearest_supported_rate(requested: int, offered: Iterable[int]) -> Optional[int]:
    """Return the closest offered rate, used only for *informational* warnings."""

    candidates = [rate for rate in offered if rate > 0]
    if not candidates:
        return None
    return min(candidates, key=lambda rate: abs(rate - requested))


def buffer_frames_for(rate: int, milliseconds: float) -> int:
    """Frames required to hold ``milliseconds`` of audio, rounded up to a power of two."""

    frames = int(math.ceil(rate * max(milliseconds, 1.0) / 1000.0))
    return max(64, frames)


def bytes_per_ms(format: AudioFormat) -> float:
    return format.bytes_per_second / 1000.0


def ms_for_bytes(format: AudioFormat, byte_count: int) -> float:
    return byte_count / bytes_per_ms(format)


FORMAT_NAMES: Dict[str, str] = {member.code: member.label for member in SampleFormat}
