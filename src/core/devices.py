"""DAC discovery, exclusive-mode negotiation and native capability probing.

PortAudio (via ``sounddevice``) is the only way to reach ASIO, WASAPI exclusive
and WDM-KS from Python without shipping a kernel driver.  This module wraps it
so the rest of the application never touches raw PortAudio indices, and so a
missing backend produces a clean, actionable error instead of an import crash.

Capability probing uses ``Pa_IsFormatSupported`` (exposed as
``sd.check_output_settings``), which asks the driver *without* opening the
device.  That lets the UI show exactly which clocks a DAC can lock to, at which
bit depth, before a single sample is sent.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..config import get_logger
from .formats import AudioFormat, SampleFormat

LOGGER = get_logger("devices")

try:  # pragma: no cover - import guard
    import sounddevice as sd
except Exception as exc:  # pragma: no cover - the UI still loads without audio
    sd = None
    _IMPORT_ERROR = str(exc)
else:
    _IMPORT_ERROR = ""

HOST_KIND_ALIASES = {
    "asio": "asio",
    "windows wasapi": "wasapi",
    "wasapi": "wasapi",
    "windows wdm-ks": "wdm-ks",
    "wdm-ks": "wdm-ks",
    "windows directsound": "directsound",
    "mme": "mme",
    "core audio": "coreaudio",
    "alsa": "alsa",
    "jack audio connection kit": "jack",
}

EXCLUSIVE_CAPABLE = ("wasapi", "wdm-ks", "asio")

DAC_KEYWORDS = (
    "dac",
    "usb audio",
    "usb dac",
    "xmos",
    "amanero",
    "combo384",
    "ess",
    "sabre",
    "akm",
    "ak4",
    "akm4497",
    "pcm179",
    "cs4398",
    "topping",
    "smsl",
    "gustard",
    "denafrips",
    "holo audio",
    "rme",
    "motu",
    "focusrite",
    "audient",
    "steinberg",
    "apogee",
    "universal audio",
    "benchmark",
    "chord",
    "ifi",
    "schiit",
    "fiio",
    "hiby",
    "astell",
    "questyle",
    "cambridge audio",
    "mark levinson",
    "luxman",
    "esoteric",
    "teac",
    "onkyo",
    "pioneer",
    "marantz",
    "arcam",
    "nad",
    "mytek",
    "antelope",
    "lynx",
    "prism sound",
    "merging",
    "ayre",
    "ps audio",
    "sonore",
    "lumin",
    "auralic",
    "matrix audio",
    "singxer",
    "denon",
    "yamaha",
)

DISPLAY_RATES = (44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000, 705600, 768000)

PROBE_FORMAT_ORDER: Tuple[SampleFormat, ...] = (
    SampleFormat.INT32,
    SampleFormat.INT24,
    SampleFormat.INT16,
    SampleFormat.FLOAT32,
)

DOP_RATES = (176400, 352800, 705600, 1411200)


class AudioBackendUnavailable(RuntimeError):
    """Raised when PortAudio/sounddevice is not usable on this machine."""


def backend_available() -> bool:
    return sd is not None


def require_backend() -> Any:
    if sd is None:
        raise AudioBackendUnavailable(
            "PortAudio backend unavailable: " + (_IMPORT_ERROR or "sounddevice is not installed")
        )
    return sd


@dataclass(frozen=True)
class HostApiInfo:
    index: int
    name: str
    kind: str
    device_count: int
    default_output_device: int

    @property
    def exclusive_capable(self) -> bool:
        return self.kind in EXCLUSIVE_CAPABLE

    @property
    def label(self) -> str:
        if self.kind == "asio":
            return "ASIO"
        if self.kind == "wasapi":
            return "WASAPI Exclusive"
        if self.kind == "wdm-ks":
            return "WDM-KS"
        return self.name


@dataclass(frozen=True)
class DeviceInfo:
    index: int
    name: str
    host_api_index: int
    host_api_name: str
    host_kind: str
    max_output_channels: int
    max_input_channels: int
    default_samplerate: float
    is_default_output: bool
    dac_score: int

    @property
    def is_asio(self) -> bool:
        return self.host_kind == "asio"

    @property
    def is_wasapi(self) -> bool:
        return self.host_kind == "wasapi"

    @property
    def exclusive_capable(self) -> bool:
        return self.host_kind in EXCLUSIVE_CAPABLE

    @property
    def is_dac_like(self) -> bool:
        return self.dac_score >= 2

    @property
    def uid(self) -> str:
        digest = hashlib.blake2b(
            f"{self.host_kind}|{self.host_api_name}|{self.name}".encode("utf-8"), digest_size=6
        )
        return digest.hexdigest()

    @property
    def label(self) -> str:
        suffix = " (default)" if self.is_default_output else ""
        return f"{self.name}{suffix}"

    @property
    def driver_label(self) -> str:
        if self.host_kind == "asio":
            return "ASIO"
        if self.host_kind == "wasapi":
            return "WASAPI Exclusive"
        if self.host_kind == "wdm-ks":
            return "WDM-KS (kernel streaming)"
        return self.host_api_name

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "host_api": self.host_api_name,
            "host_kind": self.host_kind,
            "channels": self.max_output_channels,
            "default_samplerate": self.default_samplerate,
            "default": self.is_default_output,
        }


@dataclass
class DeviceCapabilities:
    device_index: int
    exclusive: bool
    channels: int
    rates: Dict[int, Tuple[str, ...]] = field(default_factory=dict)
    format_by_rate: Dict[int, SampleFormat] = field(default_factory=dict)
    probed_at: float = 0.0
    errors: Tuple[str, ...] = ()

    @property
    def max_rate(self) -> int:
        return max(self.rates) if self.rates else 0

    @property
    def max_bits(self) -> int:
        best = 0
        for code in self.rates.values():
            for name in code:
                if name.startswith("float"):
                    best = max(best, 32)
                else:
                    best = max(best, int(name.replace("int", "")))
        return best

    def supports(self, rate: int, sample_format: SampleFormat) -> bool:
        return sample_format.code in self.rates.get(rate, ())

    def native_format_for(self, rate: int) -> Optional[SampleFormat]:
        return self.format_by_rate.get(rate)

    def describe(self) -> str:
        if not self.rates:
            return "no natives rates detected"
        parts = []
        for rate in sorted(self.rates):
            formats = self.rates[rate]
            parts.append(f"{rate / 1000:g}k={'/'.join(formats)}")
        return " · ".join(parts)

    def dop_capable_rates(self) -> List[int]:
        return [rate for rate in sorted(self.rates) if rate in DOP_RATES]


def classify_host(name: str) -> str:
    return HOST_KIND_ALIASES.get(name.strip().lower(), "unknown")


HOST_RANK = {"asio": 0, "wasapi": 1, "wdm-ks": 2, "coreaudio": 0, "alsa": 0, "jack": 0}


def host_rank(kind: str, prefer_asio: bool = True) -> int:
    """Lower is better; mixer families are always last."""

    if kind == "asio" and not prefer_asio:
        return 1
    return HOST_RANK.get(kind, 9)


def score_dac_name(name: str, channels: int, is_default: bool) -> int:
    lowered = name.lower()
    score = 0
    if any(keyword in lowered for keyword in DAC_KEYWORDS):
        score += 2
    if "speaker" in lowered or "headphone" in lowered or "mic" in lowered:
        score -= 2
    if "primary sound driver" in lowered or "sound mapper" in lowered:
        score -= 2
    if channels >= 2:
        score += 1
    if channels > 2:
        score += 1
    if is_default:
        score += 1
    return score


class DeviceManager:
    """Thread-safe façade over PortAudio device discovery."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._devices: List[DeviceInfo] = []
        self._host_apis: List[HostApiInfo] = []
        self._capability_cache: Dict[Tuple[int, bool], DeviceCapabilities] = {}
        self._last_refresh = 0.0
        self._signature: Tuple[str, ...] = ()

    # ------------------------------------------------------------------ query
    def backend_versions(self) -> Dict[str, str]:
        if sd is None:
            return {"portaudio": "unavailable", "sounddevice": "unavailable", "error": _IMPORT_ERROR}
        try:
            version = tuple(sd.get_portaudio_version())
            text = ".".join(str(part) for part in version) if isinstance(version, tuple) else str(version)
        except Exception:
            text = getattr(sd, "__version__", "unknown")
        return {
            "portaudio": text,
            "sounddevice": getattr(sd, "__version__", "unknown"),
            "asound_support": "true" if self.asio_available() else "false",
        }

    def asio_available(self) -> bool:
        if sd is None:
            return False
        try:
            return any(classify_host(api["name"]) == "asio" for api in sd.query_hostapis())
        except Exception:
            return False

    def refresh(self, force: bool = False) -> List[DeviceInfo]:
        with self._lock:
            if not force and self._devices and (time.monotonic() - self._last_refresh) < 1.0:
                return list(self._devices)
            backend = require_backend()
            try:
                host_apis = backend.query_hostapis()
                raw_devices = backend.query_devices()
                default_output = int(backend.default.device[1]) if backend.default.device else -1
            except Exception as exc:  # pragma: no cover - driver level failure
                raise AudioBackendUnavailable(f"PortAudio query failed: {exc}") from exc

            apis: List[HostApiInfo] = []
            for api_index, api in enumerate(host_apis):
                apis.append(
                    HostApiInfo(
                        index=int(api.get("index", api_index)),
                        name=str(api.get("name", f"Host API {api_index}")),
                        kind=classify_host(str(api.get("name", ""))),
                        device_count=len(api.get("devices", ()) or ()),
                        default_output_device=int(api.get("default_output_device", -1)),
                    )
                )
            devices: List[DeviceInfo] = []
            for index, raw in enumerate(raw_devices):
                max_out = int(raw.get("max_output_channels", 0))
                if max_out <= 0:
                    continue
                api_index = int(raw.get("hostapi", -1))
                api_name = apis[api_index].name if 0 <= api_index < len(apis) else "unknown"
                kind = apis[api_index].kind if 0 <= api_index < len(apis) else "unknown"
                name = str(raw.get("name", f"Device {index}"))
                devices.append(
                    DeviceInfo(
                        index=index,
                        name=name,
                        host_api_index=api_index,
                        host_api_name=api_name,
                        host_kind=kind,
                        max_output_channels=max_out,
                        max_input_channels=int(raw.get("max_input_channels", 0)),
                        default_samplerate=float(raw.get("default_samplerate", 0.0) or 0.0),
                        is_default_output=(index == default_output),
                        dac_score=score_dac_name(name, max_out, index == default_output),
                    )
                )
            self._devices = devices
            self._host_apis = apis
            self._last_refresh = time.monotonic()
            self._signature = tuple(f"{d.host_kind}:{d.name}" for d in devices)
            LOGGER.debug(
                "device list refreshed",
                extra={"output_devices": len(devices), "host_apis": [a.name for a in apis]},
            )
            return list(self._devices)

    def devices(self, refresh: bool = False) -> List[DeviceInfo]:
        return self.refresh(force=refresh)

    def host_apis(self, refresh: bool = False) -> List[HostApiInfo]:
        self.refresh(force=refresh)
        with self._lock:
            return list(self._host_apis)

    def device(self, index: int) -> Optional[DeviceInfo]:
        for device in self.refresh():
            if device.index == index:
                return device
        return None

    def best_device(self, prefer_asio: bool = True) -> Optional[DeviceInfo]:
        """Pick the most reference-grade output path available.

        Exclusive-capable driver families always win over the Windows mixer
        families (MME/DirectSound), because only those can bypass the OS volume
        stage and switch clocks.
        """

        scored = sorted(
            self.refresh(),
            key=lambda d: (
                host_rank(d.host_kind, prefer_asio),
                -d.dac_score,
                0 if d.is_default_output else 1,
                d.index,
            ),
        )
        return scored[0] if scored else None

    def find_by_name(self, name: str, host_api: str = "") -> Optional[DeviceInfo]:
        if not name:
            return None
        for device in self.refresh(force=True):
            if device.name == name and (not host_api or device.host_api_name == host_api):
                return device
        for device in self.refresh():
            if device.name == name:
                return device
        return None

    def signature(self) -> Tuple[str, ...]:
        self.refresh()
        with self._lock:
            return self._signature

    # ------------------------------------------------------------ capabilities
    def extra_settings(self, device: DeviceInfo, exclusive: bool) -> Optional[Any]:
        backend = require_backend()
        if not exclusive:
            return None
        if device.host_kind == "wasapi":
            return backend.WasapiSettings(exclusive=True)
        if device.host_kind == "asio":
            return backend.AsioSettings()
        return None

    def check_settings(
        self,
        device: DeviceInfo,
        rate: int,
        sample_format: SampleFormat,
        channels: int,
        exclusive: bool,
    ) -> Tuple[bool, str]:
        backend = require_backend()
        try:
            backend.check_output_settings(
                device=device.index,
                samplerate=rate,
                channels=channels,
                dtype=sample_format.portaudio_dtype,
                extra_settings=self.extra_settings(device, exclusive),
            )
            return True, ""
        except Exception as exc:
            return False, str(exc)

    def capabilities(
        self,
        device: DeviceInfo,
        exclusive: Optional[bool] = None,
        rates: Optional[Iterable[int]] = None,
        formats: Optional[Sequence[SampleFormat]] = None,
        reuse_cache: bool = True,
    ) -> DeviceCapabilities:
        """Probe every (rate, depth) combination the driver reports as valid."""

        exclusive = device.exclusive_capable if exclusive is None else bool(exclusive)
        key = (device.index, exclusive)
        with self._lock:
            if reuse_cache and key in self._capability_cache:
                return self._capability_cache[key]

        backend = require_backend()
        channels = min(device.max_output_channels, 2) or 1
        requested_rates = list(rates) if rates is not None else list(DISPLAY_RATES)
        extra = self.extra_settings(device, exclusive)
        capability = DeviceCapabilities(device_index=device.index, exclusive=exclusive, channels=channels)
        errors: List[str] = []

        for rate in requested_rates:
            supported: List[str] = []
            best: Optional[SampleFormat] = None
            for sample_format in (formats or PROBE_FORMAT_ORDER):
                try:
                    backend.check_output_settings(
                        device=device.index,
                        samplerate=rate,
                        channels=channels,
                        dtype=sample_format.portaudio_dtype,
                        extra_settings=extra,
                    )
                except Exception as exc:
                    message = str(exc)
                    if message and message not in errors and "Invalid sample rate" not in message:
                        errors.append(message)
                    continue
                supported.append(sample_format.code)
                if best is None:
                    best = sample_format
            if supported:
                capability.rates[rate] = tuple(supported)
                if best is not None:
                    capability.format_by_rate[rate] = best
        capability.probed_at = time.monotonic()
        capability.errors = tuple(errors[:8])
        with self._lock:
            self._capability_cache[key] = capability
        LOGGER.info(
            "device capabilities probed",
            extra={
                "device": device.name,
                "host": device.host_api_name,
                "exclusive": exclusive,
                "rates": sorted(capability.rates),
                "max_bits": capability.max_bits,
            },
        )
        return capability

    def invalidate(self, device_index: Optional[int] = None) -> None:
        with self._lock:
            if device_index is None:
                self._capability_cache.clear()
            else:
                for key in list(self._capability_cache):
                    if key[0] == device_index:
                        del self._capability_cache[key]

    def choose_transport(
        self,
        device: DeviceInfo,
        source: AudioFormat,
        exclusive: bool,
        allow_degraded: bool = False,
    ) -> Tuple[Optional[AudioFormat], List[str]]:
        """Find a lossless hardware container for ``source`` at its native clock."""

        exclusive = bool(exclusive and device.exclusive_capable)
        capability = self.capabilities(device, exclusive=exclusive)
        notes: List[str] = []
        requested_rate = source.sample_rate
        if requested_rate not in capability.rates:
            notes.append(
                f"DAC cannot lock at {requested_rate} Hz"
                + (f" (offered: {sorted(capability.rates)})" if capability.rates else "")
            )
            if not allow_degraded:
                return None, notes
            candidates = sorted(capability.rates, key=lambda rate: abs(rate - requested_rate))
            if not candidates:
                return None, notes
            notes.append("degraded mode: refusing to resample (bit-perfect lost)")
            return None, notes

        offered = capability.rates[requested_rate]
        lossless_order: List[SampleFormat] = []
        if source.sample_format is SampleFormat.INT24:
            lossless_order = [SampleFormat.INT32, SampleFormat.INT24]
        elif source.sample_format is SampleFormat.INT16:
            lossless_order = [SampleFormat.INT32, SampleFormat.INT16]
        elif source.sample_format is SampleFormat.INT32:
            lossless_order = [SampleFormat.INT32]
        elif source.sample_format is SampleFormat.FLOAT32:
            lossless_order = [SampleFormat.FLOAT32]
        else:
            # float64 -> float32 is a lossy narrowing: never offered silently.
            lossless_order = [SampleFormat.FLOAT64]
        for candidate in lossless_order:
            if candidate.code in offered:
                return (
                    AudioFormat(requested_rate, source.channels, candidate, candidate.bits),
                    notes,
                )
        if allow_degraded:
            for candidate in PROBE_FORMAT_ORDER:
                if candidate.code in offered:
                    notes.append(
                        f"degraded mode: {source.valid_bits}-bit source truncated to {candidate.bits}-bit"
                    )
                    return (
                        AudioFormat(requested_rate, source.channels, candidate, candidate.bits),
                        notes,
                    )
        notes.append(
            f"no lossless container offered at {requested_rate} Hz (driver offers {sorted(offered)})"
        )
        return None, notes


def describe_device_summary(manager: DeviceManager) -> str:
    devices = manager.devices()
    if not devices:
        return "no output devices"
    asio = sum(1 for device in devices if device.is_asio)
    wasapi = sum(1 for device in devices if device.is_wasapi)
    return f"{len(devices)} outputs · {wasapi} WASAPI · {asio} ASIO"
