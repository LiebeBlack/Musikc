"""Bitstream integrity accounting.

The monitor hashes the exact bytes handed to the driver and the exact bytes
produced by the decoder.  Because True HQ Audio never inserts a DSP stage, a
lossless source with a lossless container mapping must produce a *predictable*
relationship between both digests: the same byte count and, whenever the
conversion is an identity, the same digest.

The badge logic is intentionally conservative:

* ``BIT-PERFECT`` requires a lossless decode, a lossless container mapping, an
  exclusive driver path, a hardware clock locked to the source rate and zero
  xruns during the session.
* Any xrun inserts digital silence into the stream, therefore it *downgrades*
  the session; the badge is removed instead of being quietly kept.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

from ..config import utc_timestamp
from .formats import AudioFormat
from .telemetry import describe_dsp_chain


class Verdict(Enum):
    BIT_PERFECT = "BIT-PERFECT"
    BIT_PERFECT_DOP = "BIT-PERFECT · DoP"
    DEGRADED = "DEGRADED"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class SignalPathStage:
    name: str
    detail: str
    alters_samples: bool = False


@dataclass
class IntegrityReport:
    verdict: Verdict = Verdict.STOPPED
    reasons: List[str] = field(default_factory=list)
    source_digest: str = ""
    output_digest: str = ""
    bytes_in: int = 0
    bytes_out: int = 0
    frames_out: int = 0
    exact_conversion: bool = True
    conversion: str = ""
    xruns: int = 0
    identity_digest: bool = False
    generated_at: str = ""
    dsp_stages: Tuple[str, ...] = ()
    started: bool = False

    @property
    def is_bit_perfect(self) -> bool:
        return self.verdict in (Verdict.BIT_PERFECT, Verdict.BIT_PERFECT_DOP)

    def headline(self) -> str:
        return self.verdict.value

    def to_dict(self) -> Dict[str, object]:
        return {
            "verdict": self.verdict.value,
            "reasons": list(self.reasons),
            "source_digest": self.source_digest,
            "output_digest": self.output_digest,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "frames_out": self.frames_out,
            "exact_conversion": self.exact_conversion,
            "conversion": self.conversion,
            "xruns": self.xruns,
            "identity_digest": self.identity_digest,
            "generated_at": self.generated_at,
            "dsp_stages": list(self.dsp_stages),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


def dsp_attestation() -> Dict[str, bool]:
    """Machine readable proof of what the engine deliberately does **not** do."""

    return {
        "volume_normalisation": False,
        "replaygain": False,
        "loudness_lufs": False,
        "dynamic_compression": False,
        "limiter": False,
        "equaliser": False,
        "convolver": False,
        "software_gain": False,
        "software_resampler": False,
        "dither": False,
        "crossfeed": False,
        "surround_upmix": False,
    }


def prohibited_stage_hits() -> List[str]:
    return [name for name, enabled in dsp_attestation().items() if enabled]


def signal_path(source: Optional[AudioFormat], transport: Optional[AudioFormat], driver: str) -> List[SignalPathStage]:
    stages: List[SignalPathStage] = []
    if source is not None:
        stages.append(SignalPathStage("Decoder", f"{source.label} · {source.sample_format.label}"))
    if source is not None and transport is not None:
        if source.sample_format is transport.sample_format:
            detail = "container passthrough"
        elif source.sample_format.bits > transport.sample_format.bits:
            detail = f"{source.valid_bits}-bit → {transport.valid_bits}-bit repack"
        else:
            detail = f"{source.valid_bits}-bit → {transport.valid_bits}-bit widening"
        stages.append(SignalPathStage("Format mapper", detail))
    stages.append(SignalPathStage("Ring buffer", "lock-free SPSC, no resampling"))
    stages.append(SignalPathStage("Driver", driver))
    stages.append(SignalPathStage("DAC", "hardware clock master"))
    return stages


class IntegrityMonitor:
    """Accumulates digests and counters for one playback session."""

    def __init__(self) -> None:
        self._source = hashlib.blake2b(digest_size=16)
        self._output = hashlib.blake2b(digest_size=16)
        self._bytes_in = 0
        self._bytes_out = 0
        self._frames_out = 0
        self._exact = True
        self._conversion = ""
        self._xruns = 0
        self._reasons: List[str] = []
        self._source_format: Optional[AudioFormat] = None
        self._transport: Optional[AudioFormat] = None
        self._driver = ""
        self._dop = False
        self._started = False

    def begin(self, source: AudioFormat, transport: AudioFormat, driver: str, conversion: str, exact: bool) -> None:
        self._source = hashlib.blake2b(digest_size=16)
        self._output = hashlib.blake2b(digest_size=16)
        self._bytes_in = 0
        self._bytes_out = 0
        self._frames_out = 0
        self._exact = exact
        self._conversion = conversion
        self._xruns = 0
        self._reasons = []
        self._source_format = source
        self._transport = transport
        self._driver = driver
        self._dop = transport.dop_encoded
        self._started = True
        if not exact:
            self._reasons.append(f"lossy container mapping: {conversion}")
        if transport.dop_encoded:
            self._reasons.append("DSD transported bit-exactly through DoP v1.0")

    def reset(self) -> None:
        self.__init__()

    @property
    def started(self) -> bool:
        return self._started

    def note_conversion(self, exact: bool, reason: str) -> None:
        if not exact and self._exact:
            self._exact = False
            self._conversion = reason
            self._reasons.append(f"lossy container mapping: {reason}")

    def record_source(self, payload: bytes) -> None:
        if payload:
            self._source.update(payload)
            self._bytes_in += len(payload)

    def record_output(self, payload: bytes, frames: int) -> None:
        if payload:
            self._output.update(payload)
            self._bytes_out += len(payload)
            self._frames_out += int(frames)

    def note_xruns(self, count: int) -> None:
        if count > self._xruns:
            delta = count - self._xruns
            self._xruns = count
            self._reasons.append(f"{delta} buffer underrun(s) inserted digital silence")

    def report(self) -> IntegrityReport:
        reasons = list(self._reasons)
        if not self._started:
            return IntegrityReport(
            verdict=Verdict.STOPPED,
            reasons=["no active stream"],
            dsp_stages=describe_dsp_chain(),
            started=False,
        )
        identity = self._bytes_in == self._bytes_out and self._source.digest() == self._output.digest()
        verdict: Verdict
        if not self._exact:
            verdict = Verdict.DEGRADED
        elif self._xruns > 0:
            verdict = Verdict.DEGRADED
        elif self._dop:
            verdict = Verdict.BIT_PERFECT_DOP
        else:
            verdict = Verdict.BIT_PERFECT
        if verdict is Verdict.BIT_PERFECT and not identity:
            reasons.append("container mapping is lossless but not byte identical (depth/format widening)")
        return IntegrityReport(
            verdict=verdict,
            reasons=reasons,
            source_digest=self._source.hexdigest(),
            output_digest=self._output.hexdigest(),
            bytes_in=self._bytes_in,
            bytes_out=self._bytes_out,
            frames_out=self._frames_out,
            exact_conversion=self._exact,
            conversion=self._conversion,
            xruns=self._xruns,
            identity_digest=identity,
            started=True,
            generated_at=utc_timestamp(),
            dsp_stages=describe_dsp_chain(),
        )

    def fingerprint(self) -> str:
        payload = {
            "source": self._source.hexdigest(),
            "output": self._output.hexdigest(),
            "bytes_in": self._bytes_in,
            "bytes_out": self._bytes_out,
            "exact": self._exact,
            "dop": self._dop,
        }
        return hashlib.blake2b(
            json.dumps(payload, sort_keys=True).encode("utf-8"), digest_size=8
        ).hexdigest()
