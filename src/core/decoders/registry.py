"""Decoder registry: magic sniffing with an explicit, ordered fallback ladder.

Order matters for the bit-perfect contract:

* WAV/AIFF/DSF/DFF use the native parsers first (no external DLL, no version
  drift, guaranteed sample layout).
* FLAC goes to libsndfile, then to ffmpeg.
* Anything else that ffmpeg understands is accepted as a last resort, but the
  UI marks it as ``non-reference`` because ffmpeg may apply container level
  transformations we cannot audit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Type

from ...config import SUPPORTED_EXTENSIONS, get_logger
from .aiff import AiffDecoder
from .base import DecodeError, Decoder, UnsupportedFeature
from .dff import DffDecoder
from .dsf import DsfDecoder
from .ffmpeg_backend import FfmpegDecoder, ffmpeg_available
from .sndfile_backend import SndFileDecoder
from .wav import WavDecoder

LOGGER = get_logger("decoder.registry")

DecoderFactory = Callable[[Path], Decoder]

_CLASSES: Dict[str, Type[Decoder]] = {
    "wav": WavDecoder,
    "aiff": AiffDecoder,
    "dsf": DsfDecoder,
    "dff": DffDecoder,
    "sndfile": SndFileDecoder,
    "ffmpeg": FfmpegDecoder,
}

_EXTENSION_ORDER: Dict[str, List[str]] = {
    ".wav": ["wav", "sndfile", "ffmpeg"],
    ".wave": ["wav", "sndfile", "ffmpeg"],
    ".aiff": ["aiff", "sndfile", "ffmpeg"],
    ".aif": ["aiff", "sndfile", "ffmpeg"],
    ".aifc": ["aiff", "sndfile", "ffmpeg"],
    ".flac": ["sndfile", "ffmpeg"],
    ".dsf": ["dsf"],
    ".dff": ["dff"],
}

_MAGIC_ORDER: Dict[bytes, List[str]] = {
    b"RIFF": ["wav", "ffmpeg"],
    b"RF64": ["wav", "ffmpeg"],
    b"FORM": ["aiff", "ffmpeg"],
    b"DSD ": ["dsf"],
    b"FRM8": ["dff"],
}

_FLAC_MAGIC = b"fLaC"
_OGG_MAGIC = b"OggS"


class UnsupportedFormatError(DecodeError):
    """Raised when no decoder in the ladder accepts the file."""


def _read_magic(path: Path, length: int = 12) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read(length)
    except OSError:
        return b""


def _candidate_order(path: Path) -> List[str]:
    extension = path.suffix.lower()
    ordered = list(_EXTENSION_ORDER.get(extension, []))
    magic = _read_magic(path)
    if magic:
        if magic[0:4] == _FLAC_MAGIC:
            ordered = ["sndfile", "ffmpeg"] + [name for name in ordered if name not in ("sndfile", "ffmpeg")]
        for signature, names in _MAGIC_ORDER.items():
            if magic[0:4] == signature:
                ordered = names + [name for name in ordered if name not in names]
                break
        if magic[0:4] == _OGG_MAGIC:
            ordered = ["sndfile", "ffmpeg"]

    if ordered:
        return ordered

    if path.suffix.lower() in SUPPORTED_EXTENSIONS:
        return ["sndfile", "ffmpeg"]
    if ffmpeg_available():
        return ["ffmpeg"]
    return []


def open_decoder(path: Path) -> Decoder:
    """Open the best decoder for ``path`` or raise :class:`UnsupportedFormatError`."""

    resolved = Path(path)
    candidates = _candidate_order(resolved)
    if not candidates:
        raise UnsupportedFormatError(
            f"no decoder available for {resolved.name} (extension {resolved.suffix or 'none'})"
        )
    failures: List[str] = []
    for name in candidates:
        build = _CLASSES.get(name)
        if build is None:
            continue
        if name == "sndfile" and not SndFileDecoder.is_available():
            failures.append("sndfile: libsndfile-1.dll missing")
            continue
        if name == "ffmpeg" and not ffmpeg_available():
            failures.append("ffmpeg: binary missing")
            continue
        try:
            decoder = build(resolved)
            info = decoder.info
            if info.total_frames <= 0 and info.duration <= 0:
                raise DecodeError("stream reports zero frames")
            LOGGER.debug(
                "decoder selected",
                extra={"file": resolved.name, "decoder": name, "format": info.resolution_text},
            )
            return decoder
        except UnsupportedFeature as exc:
            failures.append(f"{name}: {exc}")
        except (DecodeError, OSError, ValueError) as exc:
            failures.append(f"{name}: {exc}")
    detail = " | ".join(failures) if failures else "no decoder accepted the file"
    raise UnsupportedFormatError(f"cannot decode {resolved.name}: {detail}")


def probe_file(path: Path):
    """Open, read the header, and close immediately (used by the library scan)."""

    decoder = open_decoder(path)
    try:
        return decoder.info
    finally:
        decoder.close()


def supported_decoder_names() -> List[str]:
    names = ["wav", "aiff", "dsf", "dff"]
    if SndFileDecoder.is_available():
        names.append("sndfile")
    if ffmpeg_available():
        names.append("ffmpeg")
    return names


def decoder_capabilities() -> Dict[str, bool]:
    return {
        "native_wav": True,
        "native_aiff": True,
        "native_dsf": True,
        "native_dff": True,
        "libsndfile": SndFileDecoder.is_available(),
        "ffmpeg": ffmpeg_available(),
    }
