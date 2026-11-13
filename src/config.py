"""Application-wide configuration, paths and structured logging for True HQ Audio.

This module has **no third-party imports** so it can be used by the bootstrap
installer, the build scripts and the frozen executable alike.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
import threading
import time
from dataclasses import MISSING, asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, MutableMapping, Optional

APP_NAME = "True HQ Audio"
APP_SHORT_NAME = "TrueHQAudio"
APP_VERSION = "2.5.9"
APP_BUILD = "20260101"
APP_PUBLISHER = "True HQ Audio Labs"
APP_DESCRIPTION = "Bit-Perfect reference audio player and rendering engine"
APP_ID = "TrueHQAudio.Player"
APP_MUTEX_NAME = "Local\\TrueHQAudio.SingleInstance.v1b"

MINIMUM_WINDOWS_BUILD = 17763

SETTINGS_SCHEMA_VERSION = 3

SUPPORTED_EXTENSIONS = (
    ".wav",
    ".flac",
    ".aiff",
    ".aif",
    ".aifc",
    ".dsf",
    ".dff",
)

IS_WINDOWS = sys.platform.startswith("win")
IS_FROZEN = bool(getattr(sys, "frozen", False))


class ConfigurationError(RuntimeError):
    """Raised when the runtime environment cannot satisfy a hard requirement."""


def utc_timestamp() -> str:
    """Return an ISO-8601 UTC timestamp with millisecond resolution."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def bundled_root() -> Path:
    """Directory that contains bundled resources (works frozen and from source)."""

    if IS_FROZEN:
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def project_root() -> Path:
    """Root of the source tree."""

    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str) -> Path:
    """Resolve a read-only resource inside the bundle."""

    return bundled_root().joinpath("resources", *parts)


def native_path(filename: str) -> Path:
    """Resolve a native binary that may live in the bundle or next to it."""

    candidates = [
        resource_path("native", filename),
        bundled_root() / filename,
        bundled_root() / "resources" / filename,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def user_data_dir() -> Path:
    """Per-user writable data directory."""

    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / APP_SHORT_NAME


def ensure_user_dirs() -> Dict[str, Path]:
    """Create (idempotently) every writable directory used by the application."""

    root = user_data_dir()
    directories = {
        "root": root,
        "logs": root / "logs",
        "cache": root / "cache",
        "config": root / "config",
        "playlists": root / "playlists",
    }
    for path in directories.values():
        path.mkdir(parents=True, exist_ok=True)
    return directories


def log_directory() -> Path:
    return ensure_user_dirs()["logs"]


def settings_file() -> Path:
    return ensure_user_dirs()["config"] / "settings.json"


def cache_directory() -> Path:
    return ensure_user_dirs()["cache"]


def playlists_directory() -> Path:
    return ensure_user_dirs()["playlists"]


def windows_build_number() -> int:
    """Return the Windows build number, or 0 on other platforms."""

    if not IS_WINDOWS:
        return 0
    try:
        return int(platform.version().split(".")[-1])
    except (ValueError, IndexError):  # pragma: no cover - defensive
        return 0


def enforce_supported_platform() -> None:
    """True HQ Audio targets Windows only; refuse to start elsewhere."""

    if not IS_WINDOWS:
        raise ConfigurationError(
            "True HQ Audio requires Windows 10/11 (x64). "
            f"Detected platform: {platform.platform()}"
        )


class JsonLineFormatter(logging.Formatter):
    """Structured JSON-lines log formatter used by the telemetry pipeline."""

    _RESERVED = frozenset(
        {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "taskName",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": utc_timestamp(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "thread": record.threadName,
        }
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = repr(value)
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class ConsoleFormatter(logging.Formatter):
    """Human readable formatter for developer consoles."""

    COLORS = {
        "DEBUG": "\x1b[38;5;244m",
        "INFO": "\x1b[38;5;39m",
        "WARNING": "\x1b[38;5;214m",
        "ERROR": "\x1b[38;5;203m",
        "CRITICAL": "\x1b[1;38;5;197m",
    }
    RESET = "\x1b[0m"

    def __init__(self) -> None:
        super().__init__(fmt="%(asctime)s %(levelname)-8s %(name)-28s %(message)s", datefmt="%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if os.environ.get("NO_COLOR") or not sys.stderr.isatty():
            return text
        color = self.COLORS.get(record.levelname, "")
        return f"{color}{text}{self.RESET}"


_LOGGING_LOCK = threading.Lock()
_LOGGING_READY = False


def setup_logging(verbose: bool = False, log_to_file: bool = True) -> logging.Logger:
    """Configure root logging exactly once (idempotent and thread-safe)."""

    global _LOGGING_READY
    root = logging.getLogger()
    with _LOGGING_LOCK:
        if _LOGGING_READY:
            if verbose:
                root.setLevel(logging.DEBUG)
            return logging.getLogger("truehq")
        level = logging.DEBUG if verbose else logging.INFO
        root.setLevel(level)
        for handler in list(root.handlers):
            root.removeHandler(handler)

        console = logging.StreamHandler(stream=sys.stderr)
        console.setLevel(level)
        console.setFormatter(ConsoleFormatter())
        root.addHandler(console)

        if log_to_file:
            try:
                log_file = log_directory() / f"truehq-{datetime.now().strftime('%Y%m%d')}.jsonl"
                file_handler = logging.FileHandler(log_file, encoding="utf-8", delay=True)
                file_handler.setLevel(logging.DEBUG)
                file_handler.setFormatter(JsonLineFormatter())
                root.addHandler(file_handler)
            except OSError:
                root.warning("log file unavailable; continuing with console logging only")

        logging.captureWarnings(True)
        for noisy in ("PIL", "matplotlib", "numba", "asyncio"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        _LOGGING_READY = True
    logger = logging.getLogger("truehq")
    logger.info(
        "logging initialised",
        extra={
            "app": APP_NAME,
            "version": APP_VERSION,
            "python": platform.python_version(),
            "frozen": IS_FROZEN,
            "platform": platform.platform(),
        },
    )
    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger."""

    return logging.getLogger(f"truehq.{name}" if not name.startswith("truehq") else name)


@dataclass
class AppSettings:
    """Persisted user preferences (never contains DSP/volume state by design)."""

    schema_version: int = SETTINGS_SCHEMA_VERSION
    output_device_name: str = ""
    output_host_api: str = ""
    exclusive_mode: bool = True
    prefer_asio: bool = True
    target_buffer_ms: int = 24
    period_frames: int = 0
    allow_degraded_conversion: bool = False
    last_directory: str = str(Path.home())
    repeat_mode: str = "off"
    shuffle: bool = False
    window_width: int = 1440
    window_height: int = 900
    window_maximised: bool = False
    library_folders: list[str] = field(default_factory=list)
    telemetry_poll_ms: int = 200
    restore_last_track: bool = True
    last_track: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AppSettings":
        """Coerce a JSON mapping into settings, ignoring unknown or invalid keys.

        The coercion is driven by the *default value* of each field rather than
        by its annotation, because ``from __future__ import annotations`` turns
        annotations into strings (``list[str]`` would never match ``list``).
        """

        cleaned: Dict[str, Any] = {}
        for key, value in data.items():
            field_info = cls.__dataclass_fields__.get(key)
            if field_info is None:
                continue
            if field_info.default is not MISSING:
                default = field_info.default
            elif field_info.default_factory is not MISSING:  # type: ignore[comparison-overlap]
                default = field_info.default_factory()  # type: ignore[misc]
            else:
                continue
            try:
                if isinstance(default, bool):
                    if isinstance(value, (bool, int)):
                        cleaned[key] = bool(value)
                elif isinstance(default, int):
                    cleaned[key] = int(value)
                elif isinstance(default, str):
                    cleaned[key] = str(value)
                elif isinstance(default, list):
                    if isinstance(value, (list, tuple)):
                        cleaned[key] = [str(item) for item in value]
                else:
                    cleaned[key] = value
            except (TypeError, ValueError):
                continue
        return cls(**cleaned)


class SettingsStore:
    """Atomic JSON settings store with corruption recovery."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or settings_file()
        self._lock = threading.RLock()
        self._settings = AppSettings()
        self._loaded = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def settings(self) -> AppSettings:
        with self._lock:
            if not self._loaded:
                self.reload()
            return self._settings

    def reload(self) -> AppSettings:
        with self._lock:
            self._loaded = True
            if not self._path.exists():
                self._settings = AppSettings()
                return self._settings
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("settings root is not an object")
                self._settings = AppSettings.from_mapping(raw)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                get_logger("config").warning("settings unreadable, using defaults", extra={"error": str(exc)})
                try:
                    backup = self._path.with_suffix(".corrupt.json")
                    self._path.replace(backup)
                except OSError:
                    pass
                self._settings = AppSettings()
            return self._settings

    def update(self, **changes: Any) -> AppSettings:
        with self._lock:
            current = self.settings.to_dict()
            current.update(changes)
            self._settings = AppSettings.from_mapping(current)
            self._write()
            return self._settings

    def save(self) -> None:
        with self._lock:
            self._write()

    def _write(self) -> None:
        payload = self._settings.to_dict()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f".tmp-{os.getpid()}")
        try:
            temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self._path)
        except OSError as exc:
            get_logger("config").error("settings write failed", extra={"error": str(exc)})
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def iter_supported_files(root: Path) -> Iterator[Path]:
    """Yield every audio file below ``root`` that True HQ Audio can decode."""

    try:
        for entry in sorted(root.iterdir()):
            if entry.is_dir():
                yield from iter_supported_files(entry)
            elif entry.suffix.lower() in SUPPORTED_EXTENSIONS:
                yield entry
    except (OSError, PermissionError):
        return


def format_bytes(count: float) -> str:
    """Human readable byte count."""

    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(count)
    for unit in units:
        if abs(value) < 1024.0 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} TiB"


def format_duration(seconds: float) -> str:
    """Format seconds as ``H:MM:SS`` or ``M:SS`` with millisecond precision option."""

    if seconds is None or seconds != seconds or seconds < 0:
        return "--:--"
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_duration_precise(seconds: float) -> str:
    """Format seconds with millisecond precision for the transport read-out."""

    if seconds is None or seconds != seconds or seconds < 0:
        return "--:--.---"
    total_ms = int(round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}.{millis:03d}"
    return f"{minutes:02d}:{secs:02d}.{millis:03d}"


def monotonic_ms() -> int:
    """Millisecond monotonic clock."""

    return int(time.monotonic() * 1000)


def humanise_rate(rate: int) -> str:
    """Render a sample rate using the professional kHz convention."""

    if rate % 1000 == 0:
        return f"{rate // 1000} kHz"
    return f"{rate / 1000:.1f} kHz"


def channel_layout_name(channels: int) -> str:
    return {
        1: "Mono",
        2: "Stereo",
        3: "2.1",
        4: "Quad",
        5: "5.0",
        6: "5.1",
        7: "6.1",
        8: "7.1",
    }.get(channels, f"{channels} ch")


def all_environment_flags() -> MutableMapping[str, str]:
    """Return the subset of environment variables relevant to diagnostics."""

    interesting = (
        "TRUEEHQ_DEBUG",
        "TRUEEHQ_DEVICE",
        "TRUEEHQ_EXCLUSIVE",
        "TRUEEHQ_LOG_LEVEL",
        "QT_ENABLE_HIGHDPI_SCALING",
        "PYTHONFAULTHANDLER",
    )
    return {key: os.environ[key] for key in interesting if key in os.environ}
