"""Application bootstrap: locale, logging, single instance and Qt start-up."""

from __future__ import annotations

import ctypes
import os
import platform
import sys
import threading
import traceback
from typing import List, Optional

from .config import (
    APP_NAME,
    APP_SHORT_NAME,
    APP_VERSION,
    ConfigurationError,
    enforce_supported_platform,
    get_logger,
    setup_logging,
    SettingsStore,
    user_data_dir,
)
from .core.audio_engine import AudioEngine, EngineConfig
from .core.decoders.registry import decoder_capabilities, supported_decoder_names
from .core.devices import AudioBackendUnavailable, DeviceManager, backend_available
from .core.events import EventBus
from .core.library import Library
from .core.playlist import PlaybackController
from .core.priority import bootstrap_priority, shutdown_priority
from .core.telemetry import TelemetryHub

LOGGER = get_logger("app")

IS_WINDOWS = sys.platform.startswith("win")

ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "Local\\TrueHQAudio.SingleInstance.v1b"

_MUTEX_HANDLE: Optional[int] = None


def set_process_app_id() -> None:
    """Give the process a stable AppUserModelID so the taskbar groups correctly."""

    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            ctypes.c_wchar_p("TrueHQAudio.Player.1")
        )
    except (AttributeError, OSError):
        return


def acquire_single_instance() -> bool:
    """Return False when another instance already owns the mutex."""

    global _MUTEX_HANDLE
    if not IS_WINDOWS:
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, 0, ctypes.c_wchar_p(MUTEX_NAME))
        if not handle:
            return True
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        _MUTEX_HANDLE = int(handle)
        return True
    except (OSError, AttributeError):
        return True


def release_single_instance() -> None:
    global _MUTEX_HANDLE
    if _MUTEX_HANDLE is None or not IS_WINDOWS:
        return
    try:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(_MUTEX_HANDLE))
    except (AttributeError, OSError):
        pass
    _MUTEX_HANDLE = None


def _install_exception_hook() -> None:
    def handler(exc_type, exc_value, exc_traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        LOGGER.critical("unhandled exception", extra={"traceback": text})
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                QMessageBox.critical(
                    None,
                    f"{APP_NAME} — unexpected error",
                    "An unexpected error occurred. The session was logged to:\n"
                    f"{user_data_dir() / 'logs'}\n\n{exc_type.__name__}: {exc_value}",
                )
        except Exception:  # pragma: no cover - never fail while reporting a failure
            pass

    sys.excepthook = handler
    threading.excepthook = lambda args: handler(args.exc_type, args.exc_value, args.exc_traceback)


def collect_diagnostics() -> str:
    """Human readable environment report (used by --diagnostics and CI)."""

    lines: List[str] = []
    lines.append(f"{APP_NAME} {APP_VERSION}")
    lines.append(f"Python       : {platform.python_version()} ({platform.architecture()[0]})")
    lines.append(f"Platform     : {platform.platform()}")
    lines.append(f"Data folder  : {user_data_dir()}")
    lines.append(f"PortAudio    : {'available' if backend_available() else 'MISSING'}")
    lines.append(f"Decoders     : {', '.join(supported_decoder_names())}")
    capabilities = decoder_capabilities()
    lines.append(
        "Capabilities : "
        + ", ".join(f"{key}={'yes' if value else 'no'}" for key, value in sorted(capabilities.items()))
    )
    try:
        manager = DeviceManager()
        versions = manager.backend_versions()
        lines.append(
            f"Backend      : portaudio {versions.get('portaudio')} / sounddevice {versions.get('sounddevice')}"
        )
        devices = manager.refresh(force=True)
        lines.append(f"Outputs      : {len(devices)}")
        for device in devices:
            lines.append(
                f"  [{device.index:>2}] {device.name} · {device.host_api_name} · "
                f"{device.max_output_channels} ch · default {humanise(device.default_samplerate)}"
            )
        best = manager.best_device()
        if best is not None:
            capability = manager.capabilities(best, exclusive=best.exclusive_capable)
            lines.append(f"Probe        : {best.name} -> {capability.describe()}")
    except AudioBackendUnavailable as exc:
        lines.append(f"Backend error: {exc}")
    state = bootstrap_priority()
    lines.append(f"Scheduling   : {state.summary()}")
    shutdown_priority()
    return "\n".join(lines)


def humanise(rate: float) -> str:
    if not rate:
        return "—"
    return f"{rate / 1000:.1f} kHz"


def _build_engine(settings: SettingsStore) -> AudioEngine:
    values = settings.settings
    config = EngineConfig(
        device_index=-1,
        exclusive=bool(values.exclusive_mode),
        prefer_asio=True,
        buffer_ms=int(values.target_buffer_ms),
        period_frames=int(values.period_frames),
        allow_degraded=bool(values.allow_degraded_conversion),
    )
    return AudioEngine(devices=DeviceManager(), telemetry=TelemetryHub(), bus=EventBus(), config=config)


def run(argv: Optional[List[str]] = None) -> int:
    """Start the GUI; returns the Qt exit code."""

    arguments = list(argv if argv is not None else sys.argv)
    verbose = any(flag in arguments for flag in ("--verbose", "-v", "--debug"))
    try:
        enforce_supported_platform()
    except ConfigurationError as exc:
        print(f"{APP_NAME}: {exc}", file=sys.stderr)
        return 2

    setup_logging(verbose=verbose)
    _install_exception_hook()
    set_process_app_id()

    if "--diagnostics" in arguments:
        print(collect_diagnostics())
        return 0

    if not acquire_single_instance():
        LOGGER.warning("another instance is already running")
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            app = QApplication(arguments)
            QMessageBox.information(
                None,
                APP_NAME,
                f"{APP_NAME} is already running.\nClose the existing window to start a new session.",
            )
        except Exception:
            print(f"{APP_NAME} is already running.", file=sys.stderr)
        return 0

    try:
        os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
        os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtWidgets import QApplication

        from .ui.main_window import MainWindow, build_app_icon
        from .ui.qt_bridge import QtBridge
        from .ui.theme import apply_theme

        # Fractional scaling (125 %, 150 % …) must render exactly instead of
        # being rounded to the nearest integer factor.
        try:
            QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
                Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
            )
        except (AttributeError, ValueError):
            pass
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
        app = QApplication(arguments)
        app.setApplicationName(APP_NAME)
        app.setApplicationDisplayName(APP_NAME)
        app.setApplicationVersion(APP_VERSION)
        app.setOrganizationName("True HQ Audio Labs")
        app.setOrganizationDomain("truehq.audio")
        app.setDesktopFileName(APP_SHORT_NAME)
        app.setWindowIcon(build_app_icon())
        apply_theme(app)

        settings = SettingsStore()
        priority = bootstrap_priority(realtime="--realtime" in arguments)
        LOGGER.info("priority bootstrap", extra={"priority": priority.summary()})

        engine = _build_engine(settings)
        controller = PlaybackController(engine, engine.bus)
        library = Library(settings.settings.library_folders)
        restored = library.load_cache()
        LOGGER.info("library cache restored", extra={"entries": restored})
        bridge = QtBridge(engine.bus)
        window = MainWindow(engine, controller, library, settings, bridge)
        window.show()
        QTimer.singleShot(0, window.restore_state)

        exit_code = app.exec()
        shutdown_priority()
        release_single_instance()
        LOGGER.info("application exited", extra={"code": exit_code})
        return int(exit_code)
    except Exception:
        LOGGER.exception("fatal start-up failure")
        release_single_instance()
        return 1
