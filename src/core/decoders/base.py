"""Decoder contracts shared by every container implementation."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..dsd import DsdFormat
from ..formats import AudioFormat, SampleFormat


class DecodeError(RuntimeError):
    """Raised when a file cannot be decoded."""


class UnsupportedFeature(DecodeError):
    """Raised for valid but unsupported encodings (DST, ALAC, ...)."""


class CorruptStreamError(DecodeError):
    """Raised when the bitstream violates its own container specification."""


@dataclass(frozen=True)
class SourceInfo:
    """Everything the UI and the engine need to know about a source file."""

    path: Path
    container: str
    codec: str
    audio_format: AudioFormat
    total_frames: int
    duration: float
    dsd_format: Optional[DsdFormat] = None
    tags: Dict[str, str] = field(default_factory=dict)
    notes: Tuple[str, ...] = ()
    file_size: int = 0
    bitrate: int = 0
    lossless: bool = True

    @property
    def is_dsd(self) -> bool:
        return self.dsd_format is not None

    @property
    def sample_rate(self) -> int:
        return self.dsd_format.dsd_rate if self.dsd_format else self.audio_format.sample_rate

    @property
    def channels(self) -> int:
        return self.audio_format.channels

    @property
    def bit_depth_text(self) -> str:
        if self.dsd_format is not None:
            return self.dsd_format.label
        if self.audio_format.is_float:
            return "32-bit float" if self.audio_format.sample_format is SampleFormat.FLOAT32 else "64-bit float"
        return f"{self.audio_format.valid_bits}-bit"

    @property
    def resolution_text(self) -> str:
        if self.dsd_format is not None:
            return f"{self.dsd_format.label} · DoP {self.dsd_format.dop_rate / 1000:.1f} kHz"
        rate = self.audio_format.sample_rate
        rate_text = f"{rate / 1000:.1f} kHz".replace(".0 kHz", " kHz")
        return f"{self.bit_depth_text} · {rate_text}"

    @property
    def transport_format(self) -> AudioFormat:
        """The format the DAC will actually receive (DoP for DSD)."""

        if self.dsd_format is not None:
            return self.dsd_format.dop_format
        return self.audio_format

    @property
    def title(self) -> str:
        return self.tags.get("title") or self.path.stem

    @property
    def artist(self) -> str:
        return self.tags.get("artist") or "Unknown artist"

    @property
    def album(self) -> str:
        return self.tags.get("album") or ""

    @property
    def track_number(self) -> str:
        return self.tags.get("track") or ""

    @property
    def technical_summary(self) -> str:
        parts = [
            self.container.upper(),
            self.codec,
            f"{self.sample_rate / 1000:.1f} kHz".replace(".0 kHz", " kHz"),
            self.bit_depth_text,
            f"{self.channels} ch",
        ]
        if self.dsd_format is not None:
            parts.append("DoP")
        return " · ".join(parts)


class Decoder(ABC):
    """Streaming decoder producing native-layout payloads without any DSP."""

    container_name = "unknown"
    codec_name = "unknown"
    supports_dsd = False

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        if not self._path.exists():
            raise DecodeError(f"file not found: {self._path}")
        if not self._path.is_file():
            raise DecodeError(f"not a regular file: {self._path}")
        stat = self._path.stat()
        self._file_size = stat.st_size
        self._handle = None
        self._info: Optional[SourceInfo] = None
        self._position = 0
        self._closed = False

    # --------------------------------------------------------------- metadata
    @property
    def path(self) -> Path:
        return self._path

    @property
    def info(self) -> SourceInfo:
        if self._info is None:
            self._info = self._build_info()
        return self._info

    @abstractmethod
    def _build_info(self) -> SourceInfo: ...

    @property
    def format(self) -> AudioFormat:
        return self.info.transport_format

    @property
    def eof(self) -> bool:
        return self._position >= self.info.total_frames

    @property
    def position(self) -> int:
        return self._position

    @property
    def remaining_frames(self) -> int:
        return max(0, self.info.total_frames - self._position)

    # --------------------------------------------------------------- contract
    @abstractmethod
    def read(self, frames: int) -> bytes:
        """Return up to ``frames`` frames in the native container layout."""

    @abstractmethod
    def seek(self, frame: int) -> int:
        """Seek to an absolute frame and return the achieved position."""

    def read_dsd(self, bits_per_channel: int) -> List[bytes]:
        raise UnsupportedFeature(f"{self.container_name} does not carry a DSD bitstream")

    def set_tags(self, tags: Dict[str, str]) -> None:
        if self._info is not None and tags:
            merged = dict(self._info.tags)
            merged.update({k: v for k, v in tags.items() if v})
            self._info = SourceInfo(
                path=self._info.path,
                container=self._info.container,
                codec=self._info.codec,
                audio_format=self._info.audio_format,
                total_frames=self._info.total_frames,
                duration=self._info.duration,
                dsd_format=self._info.dsd_format,
                tags=merged,
                notes=self._info.notes,
                file_size=self._info.file_size,
                bitrate=self._info.bitrate,
                lossless=self._info.lossless,
            )

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None
        self._closed = True

    def __enter__(self) -> "Decoder":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        self.close()

    # ----------------------------------------------------------------- helper
    def _open(self):
        if self._handle is None:
            self._handle = open(self._path, "rb", buffering=1 << 20)
        return self._handle

    def _read_at(self, offset: int, length: int) -> bytes:
        handle = self._open()
        handle.seek(offset)
        return handle.read(length)

    def _u16(self, payload: bytes, offset: int, little_endian: bool = True) -> int:
        order = "little" if little_endian else "big"
        return int.from_bytes(payload[offset : offset + 2], order, signed=False)

    def _u32(self, payload: bytes, offset: int, little_endian: bool = True) -> int:
        order = "little" if little_endian else "big"
        return int.from_bytes(payload[offset : offset + 4], order, signed=False)

    def _u64(self, payload: bytes, offset: int, little_endian: bool = True) -> int:
        order = "little" if little_endian else "big"
        return int.from_bytes(payload[offset : offset + 8], order, signed=False)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self._path.name}>"


def _read_exact(handle, length: int) -> bytes:
    payload = handle.read(length)
    if len(payload) != length:
        raise CorruptStreamError(f"truncated read: wanted {length} bytes, received {len(payload)}")
    return payload


def file_size(path: Path) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def clamp_frames(frames: int, available: int) -> int:
    return max(0, min(int(frames), int(available)))


def normalise_tags(raw: Dict[str, Optional[str]]) -> Dict[str, str]:
    aliases = {
        "title": "title",
        "album": "album",
        "artist": "artist",
        "albumartist": "album_artist",
        "album artist": "album_artist",
        "performer": "artist",
        "tracknumber": "track",
        "track": "track",
        "date": "date",
        "year": "date",
        "genre": "genre",
        "comment": "comment",
        "description": "comment",
        "copyright": "copyright",
        "encoded_by": "encoder",
        "encoder": "encoder",
        "composer": "composer",
    }
    output: Dict[str, str] = {}
    for key, value in raw.items():
        if not value:
            continue
        normalised = str(key).strip().lower().replace("-", " ")
        target = aliases.get(normalised, aliases.get(normalised.replace(" ", ""), normalised.replace(" ", "_")))
        cleaned = str(value).strip()
        if cleaned:
            output[target] = cleaned
    return output
