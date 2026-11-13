"""Windows scheduling primitives for glitch-free audio rendering.

Three independent mechanisms are used together, because each one covers a
different scheduler layer:

1. ``SetPriorityClass`` on the process (HIGH_PRIORITY_CLASS; REALTIME only on
   explicit opt-in, since a realtime process can starve input handling).
2. ``AvSetMmThreadCharacteristicsW("Pro Audio")`` — the Multimedia Class
   Scheduler Service guarantee that gives the thread 80%% of a CPU slice with
   guaranteed latency, which is what professional audio drivers expect.
3. ``timeBeginPeriod(1)`` so ``Sleep``/waitable timers do not round up to the
   default 15.6 ms kernel tick while the feeder paces itself.

Every call is defensive: if a privilege is unavailable the engine keeps running
with reduced guarantees and the telemetry reports it, instead of crashing.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from dataclasses import dataclass, field
from typing import List, Optional

IS_WINDOWS = sys.platform.startswith("win")

HIGH_PRIORITY_CLASS = 0x00000080
REALTIME_PRIORITY_CLASS = 0x00000100
ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
NORMAL_PRIORITY_CLASS = 0x00000020

THREAD_PRIORITY_LOWEST = -2
THREAD_PRIORITY_BELOW_NORMAL = -1
THREAD_PRIORITY_NORMAL = 0
THREAD_PRIORITY_ABOVE_NORMAL = 1
THREAD_PRIORITY_HIGHEST = 2
THREAD_PRIORITY_TIME_CRITICAL = 15

MMCSS_TASK = "Pro Audio"
TIMER_RESOLUTION_MS = 1


@dataclass
class PriorityState:
    """Result of the priority escalation chain, surfaced in telemetry."""

    process_class: str = "unknown"
    mmcss_registered: bool = False
    mmcss_task_index: int = 0
    thread_priority: str = "unknown"
    timer_resolution_ms: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def realtime(self) -> bool:
        return self.process_class.upper().startswith("REALTIME") or self.mmcss_registered

    def summary(self) -> str:
        parts = [f"process={self.process_class}"]
        parts.append(f"mmcss={'on' if self.mmcss_registered else 'off'}")
        parts.append(f"thread={self.thread_priority}")
        parts.append(f"timer={self.timer_resolution_ms}ms")
        return " · ".join(parts)


_STATE_LOCK = threading.Lock()
_GLOBAL_STATE = PriorityState()
_MMCSS_HANDLE: Optional[int] = None
_TIMER_ACTIVE = False


def _kernel32() -> Optional[ctypes.WinDLL]:
    if not IS_WINDOWS:
        return None
    try:
        return ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return None


def _winmm() -> Optional[ctypes.WinDLL]:
    if not IS_WINDOWS:
        return None
    try:
        return ctypes.WinDLL("winmm", use_last_error=True)
    except OSError:
        return None


def _avrt() -> Optional[ctypes.WinDLL]:
    if not IS_WINDOWS:
        return None
    try:
        return ctypes.WinDLL("avrt", use_last_error=True)
    except OSError:
        return None


def _set_thread_name(name: str) -> None:
    """Name the calling thread so debuggers and ETW traces are readable."""

    if not IS_WINDOWS:
        return
    try:
        kernel32 = _kernel32()
        if kernel32 is None:
            return
        handle = kernel32.GetCurrentThread()
        kernel32.SetThreadDescription.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.SetThreadDescription.restype = ctypes.c_long
        kernel32.SetThreadDescription(handle, ctypes.c_wchar_p(name))
    except (AttributeError, OSError, ctypes.ArgumentError):
        return


def elevate_process(realtime: bool = False) -> str:
    """Raise the process priority class; returns the resulting class name."""

    with _STATE_LOCK:
        if not IS_WINDOWS:
            return "unsupported"
        kernel32 = _kernel32()
        if kernel32 is None:
            _GLOBAL_STATE.errors.append("kernel32 unavailable")
            return "unknown"
        desired = REALTIME_PRIORITY_CLASS if realtime else HIGH_PRIORITY_CLASS
        label = "REALTIME_PRIORITY_CLASS" if realtime else "HIGH_PRIORITY_CLASS"
        try:
            kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel32.SetPriorityClass.restype = ctypes.c_int
            handle = kernel32.GetCurrentProcess()
            if not kernel32.SetPriorityClass(handle, desired):
                error = ctypes.get_last_error()
                _GLOBAL_STATE.errors.append(f"SetPriorityClass failed ({error})")
                if realtime:
                    return elevate_process(realtime=False)
                return "NORMAL_PRIORITY_CLASS"
        except (OSError, ctypes.ArgumentError) as exc:
            _GLOBAL_STATE.errors.append(f"SetPriorityClass exception: {exc}")
            return "unknown"
        _GLOBAL_STATE.process_class = label
        return label


def enable_high_resolution_timer(milliseconds: int = TIMER_RESOLUTION_MS) -> bool:
    """Request a 1 ms kernel timer resolution for the lifetime of the process."""

    global _TIMER_ACTIVE
    with _STATE_LOCK:
        if _TIMER_ACTIVE or not IS_WINDOWS:
            return _TIMER_ACTIVE
        winmm = _winmm()
        if winmm is None:
            _GLOBAL_STATE.errors.append("winmm unavailable")
            return False
        try:
            winmm.timeBeginPeriod.argtypes = [ctypes.c_uint]
            winmm.timeBeginPeriod.restype = ctypes.c_uint
            result = winmm.timeBeginPeriod(int(milliseconds))
        except (OSError, ctypes.ArgumentError) as exc:
            _GLOBAL_STATE.errors.append(f"timeBeginPeriod exception: {exc}")
            return False
        if result == 0:
            _TIMER_ACTIVE = True
            _GLOBAL_STATE.timer_resolution_ms = int(milliseconds)
            return True
        _GLOBAL_STATE.errors.append(f"timeBeginPeriod returned {result}")
        return False


def release_high_resolution_timer() -> None:
    global _TIMER_ACTIVE
    with _STATE_LOCK:
        if not _TIMER_ACTIVE:
            return
        winmm = _winmm()
        if winmm is not None:
            try:
                winmm.timeEndPeriod.argtypes = [ctypes.c_uint]
                winmm.timeEndPeriod.restype = ctypes.c_uint
                winmm.timeEndPeriod(TIMER_RESOLUTION_MS)
            except (OSError, ctypes.ArgumentError):
                pass
        _TIMER_ACTIVE = False


def register_mmcss(task_name: str = MMCSS_TASK) -> bool:
    """Register the calling thread with the MMCSS 'Pro Audio' task."""

    global _MMCSS_HANDLE
    if not IS_WINDOWS:
        return False
    avrt = _avrt()
    if avrt is None:
        with _STATE_LOCK:
            _GLOBAL_STATE.errors.append("avrt.dll unavailable")
        return False
    try:
        avrt.AvSetMmThreadCharacteristicsW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        avrt.AvSetMmThreadCharacteristicsW.restype = ctypes.c_void_p
        index = ctypes.c_uint32(0)
        handle = avrt.AvSetMmThreadCharacteristicsW(ctypes.c_wchar_p(task_name), ctypes.byref(index))
    except (OSError, ctypes.ArgumentError) as exc:
        with _STATE_LOCK:
            _GLOBAL_STATE.errors.append(f"AvSetMmThreadCharacteristics failed: {exc}")
        return False
    if not handle:
        with _STATE_LOCK:
            _GLOBAL_STATE.errors.append("MMCSS registration rejected by the service")
        return False
    with _STATE_LOCK:
        _MMCSS_HANDLE = int(handle)
        _GLOBAL_STATE.mmcss_registered = True
        _GLOBAL_STATE.mmcss_task_index = int(index.value)
    return True


def unregister_mmcss() -> None:
    global _MMCSS_HANDLE
    with _STATE_LOCK:
        handle = _MMCSS_HANDLE
        _MMCSS_HANDLE = None
        _GLOBAL_STATE.mmcss_registered = False
    if handle is None or not IS_WINDOWS:
        return
    avrt = _avrt()
    if avrt is None:
        return
    try:
        avrt.AvRevertMmThreadCharacteristics.argtypes = [ctypes.c_void_p]
        avrt.AvRevertMmThreadCharacteristics.restype = ctypes.c_int
        avrt.AvRevertMmThreadCharacteristics(ctypes.c_void_p(handle))
    except (OSError, ctypes.ArgumentError):
        return


def raise_current_thread_priority(realtime: bool = True) -> str:
    """Set the calling thread to TIME_CRITICAL (or HIGHEST as a safe fallback)."""

    if not IS_WINDOWS:
        try:
            os.nice(-10)
            return "nice(-10)"
        except (AttributeError, PermissionError, OSError):
            return "default"
    kernel32 = _kernel32()
    if kernel32 is None:
        return "unknown"
    level = THREAD_PRIORITY_TIME_CRITICAL if realtime else THREAD_PRIORITY_HIGHEST
    label = "TIME_CRITICAL" if realtime else "HIGHEST"
    try:
        kernel32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        kernel32.SetThreadPriority.restype = ctypes.c_int
        kernel32.GetCurrentThread.restype = ctypes.c_void_p
        thread = kernel32.GetCurrentThread()
        if kernel32.SetThreadPriority(thread, level):
            with _STATE_LOCK:
                _GLOBAL_STATE.thread_priority = label
            return label
        error = ctypes.get_last_error()
        with _STATE_LOCK:
            _GLOBAL_STATE.errors.append(f"SetThreadPriority failed ({error})")
        if realtime:
            return raise_current_thread_priority(realtime=False)
        kernel32.SetThreadPriorityBoost.argtypes = [ctypes.c_void_p, ctypes.c_int]
        kernel32.SetThreadPriorityBoost(thread, 0)
        return "default"
    except (OSError, ctypes.ArgumentError) as exc:
        with _STATE_LOCK:
            _GLOBAL_STATE.errors.append(f"SetThreadPriority exception: {exc}")
        return "unknown"


def disable_thread_priority_boost() -> None:
    """Stop Windows from temporarily boosting the priority of the audio thread."""

    if not IS_WINDOWS:
        return
    kernel32 = _kernel32()
    if kernel32 is None:
        return
    try:
        kernel32.SetThreadPriorityBoost.argtypes = [ctypes.c_void_p, ctypes.c_int]
        kernel32.SetThreadPriorityBoost.restype = ctypes.c_int
        kernel32.GetCurrentThread.restype = ctypes.c_void_p
        kernel32.SetThreadPriorityBoost(kernel32.GetCurrentThread(), 0)
    except (OSError, ctypes.ArgumentError):
        return


class AudioThreadGuard:
    """Context manager applying the full realtime escalation to one thread."""

    def __init__(self, name: str, realtime: bool = True, mmcss: bool = True) -> None:
        self.name = name
        self.realtime = realtime
        self.use_mmcss = mmcss
        self.registered_mmcss = False

    def __enter__(self) -> "AudioThreadGuard":
        _set_thread_name(self.name)
        raise_current_thread_priority(realtime=self.realtime)
        disable_thread_priority_boost()
        if self.use_mmcss:
            self.registered_mmcss = register_mmcss()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        if self.registered_mmcss:
            unregister_mmcss()
        return None


def bootstrap_priority(realtime: bool = False) -> PriorityState:
    """Apply process wide settings once at start-up."""

    elevate_process(realtime=realtime)
    enable_high_resolution_timer(TIMER_RESOLUTION_MS)
    with _STATE_LOCK:
        snapshot = PriorityState(
            process_class=_GLOBAL_STATE.process_class,
            mmcss_registered=_GLOBAL_STATE.mmcss_registered,
            mmcss_task_index=_GLOBAL_STATE.mmcss_task_index,
            thread_priority=_GLOBAL_STATE.thread_priority,
            timer_resolution_ms=_GLOBAL_STATE.timer_resolution_ms,
            errors=list(_GLOBAL_STATE.errors),
        )
    return snapshot


def current_priority_state() -> PriorityState:
    with _STATE_LOCK:
        return PriorityState(
            process_class=_GLOBAL_STATE.process_class,
            mmcss_registered=_GLOBAL_STATE.mmcss_registered,
            mmcss_task_index=_GLOBAL_STATE.mmcss_task_index,
            thread_priority=_GLOBAL_STATE.thread_priority,
            timer_resolution_ms=_GLOBAL_STATE.timer_resolution_ms,
            errors=list(_GLOBAL_STATE.errors),
        )


def shutdown_priority() -> None:
    unregister_mmcss()
    release_high_resolution_timer()
