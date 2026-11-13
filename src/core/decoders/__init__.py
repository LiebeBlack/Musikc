"""Container decoders.  Import the registry, never a concrete decoder."""

from __future__ import annotations

from .base import (
    CorruptStreamError,
    DecodeError,
    Decoder,
    SourceInfo,
    UnsupportedFeature,
)
from .registry import (
    UnsupportedFormatError,
    decoder_capabilities,
    open_decoder,
    probe_file,
    supported_decoder_names,
)

__all__ = [
    "CorruptStreamError",
    "DecodeError",
    "Decoder",
    "SourceInfo",
    "UnsupportedFeature",
    "UnsupportedFormatError",
    "decoder_capabilities",
    "open_decoder",
    "probe_file",
    "supported_decoder_names",
]
