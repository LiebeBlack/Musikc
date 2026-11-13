"""Minimal, allocation-conscious ctypes binding for libsndfile.

libsndfile is used exclusively for *lossless* decoding (FLAC) and as a
secondary path for WAV/AIFF.  ``sf_readf_int`` returns samples left-justified
in 32-bit integers (the same canonical layout used by :mod:`src.core.formats`),
which means a 24-bit FLAC becomes ``int32`` with ``valid_bits=24`` and is
reduced to 24-bit hardware with a pure 8-bit shift: no rounding, no dither.

The binding is optional.  When the DLL is absent the engine reports
``NativeLibraryMissing`` and the decoder registry falls back to ffmpeg.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import POINTER, Structure, c_char_p, c_double, c_float, c_int, c_int32, c_int64, c_void_p
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import numpy as np

from ..config import get_logger, native_path

IS_WINDOWS = sys.platform.startswith("win")

SFM_READ = 0x10
SFM_WRITE = 0x20
SFM_RDWR = 0x30

SFC_GET_LIB_VERSION = 0x1000
SFC_GET_LOG_INFO = 0x1001
SFC_SET_NORM_DOUBLE = 0x1010
SFC_SET_NORM_FLOAT = 0x1011
SFC_SET_SCALE_FLOAT_INT_READ = 0x1014

SEEK_SET = 0
SEEK_CUR = 1
SEEK_END = 2

SF_FORMAT_WAV = 0x010000
SF_FORMAT_AIFF = 0x020000
SF_FORMAT_AU = 0x030000
SF_FORMAT_RAW = 0x040000
SF_FORMAT_FLAC = 0x170000
SF_FORMAT_OGG = 0x200000
SF_FORMAT_MPEG = 0x230000

SF_FORMAT_SUBMASK = 0x0000FFFF
SF_FORMAT_TYPEMASK = 0x0FFF0000
SF_FORMAT_PCM_S8 = 0x0001
SF_FORMAT_PCM_16 = 0x0002
SF_FORMAT_PCM_24 = 0x0003
SF_FORMAT_PCM_32 = 0x0004
SF_FORMAT_PCM_U8 = 0x0005
SF_FORMAT_FLOAT = 0x0006
SF_FORMAT_DOUBLE = 0x0007

SF_STR_TITLE = 0x01
SF_STR_COPYRIGHT = 0x02
SF_STR_SOFTWARE = 0x03
SF_STR_ARTIST = 0x04
SF_STR_COMMENT = 0x05
SF_STR_DATE = 0x06
SF_STR_ALBUM = 0x07
SF_STR_LICENSE = 0x08
SF_STR_TRACKNUMBER = 0x09
SF_STR_GENRE = 0x10

STRING_TYPES: Dict[int, str] = {
    SF_STR_TITLE: "title",
    SF_STR_COPYRIGHT: "copyright",
    SF_STR_SOFTWARE: "encoder",
    SF_STR_ARTIST: "artist",
    SF_STR_COMMENT: "comment",
    SF_STR_DATE: "date",
    SF_STR_ALBUM: "album",
    SF_STR_LICENSE: "license",
    SF_STR_TRACKNUMBER: "track",
    SF_STR_GENRE: "genre",
}

SUBTYPE_BITS: Dict[int, int] = {
    SF_FORMAT_PCM_S8: 8,
    SF_FORMAT_PCM_16: 16,
    SF_FORMAT_PCM_24: 24,
    SF_FORMAT_PCM_32: 32,
    SF_FORMAT_PCM_U8: 8,
    SF_FORMAT_FLOAT: 32,
    SF_FORMAT_DOUBLE: 64,
}

CONTAINER_NAMES: Dict[int, str] = {
    SF_FORMAT_WAV: "WAV",
    SF_FORMAT_AIFF: "AIFF",
    SF_FORMAT_AU: "AU",
    SF_FORMAT_RAW: "RAW",
    SF_FORMAT_FLAC: "FLAC",
    SF_FORMAT_OGG: "OGG",
    SF_FORMAT_MPEG: "MPEG",
}

DLL_CANDIDATES = (
    "libsndfile-1.dll",
    "libsndfile.dll",
    "libsndfile_x64.dll",
)


class NativeLibraryMissing(RuntimeError):
    """Raised when libsndfile cannot be located."""


class SfInfo(Structure):
    _fields_ = [
        ("frames", c_int64),
        ("samplerate", c_int),
        ("channels", c_int),
        ("format", c_int),
        ("sections", c_int),
        ("seekable", c_int),
    ]


def _search_paths() -> Iterable[Path]:
    env = os.environ.get("TRUEEHQ_LIBSNDFILE")
    if env:
        yield Path(env)
    for name in DLL_CANDIDATES:
        yield native_path(name)
    if IS_WINDOWS:
        yield Path(sys.executable).parent / "libsndfile-1.dll"
        for base in (os.environ.get("SystemRoot", r"C:\Windows"), os.environ.get("ProgramFiles", r"C:\Program Files")):
            for name in DLL_CANDIDATES:
                yield Path(base) / name
                yield Path(base) / "System32" / name
    else:
        import ctypes.util

        found = ctypes.util.find_library("sndfile")
        if found:
            yield Path(found)


class LibSndFile:
    """Process-wide singleton wrapper around the libsndfile shared library."""

    _instance: Optional["LibSndFile"] = None
    _error: Optional[str] = None

    def __init__(self, library_path: Path) -> None:
        self._logger = get_logger("libsndfile")
        self._path = library_path
        try:
            if IS_WINDOWS:
                self._lib = ctypes.WinDLL(str(library_path))
            else:
                self._lib = ctypes.CDLL(str(library_path))
        except OSError as exc:
            raise NativeLibraryMissing(f"cannot load {library_path}: {exc}") from exc
        self._bind()

    def _bind(self) -> None:
        lib = self._lib
        lib.sf_open.argtypes = [c_char_p, c_int, POINTER(SfInfo)]
        lib.sf_open.restype = c_void_p
        lib.sf_close.argtypes = [c_void_p]
        lib.sf_close.restype = c_int
        lib.sf_error.argtypes = [c_void_p]
        lib.sf_error.restype = c_char_p
        lib.sf_strerror.argtypes = [c_void_p]
        lib.sf_strerror.restype = c_char_p
        lib.sf_readf_int.argtypes = [c_void_p, POINTER(c_int32), c_int64]
        lib.sf_readf_int.restype = c_int64
        lib.sf_readf_float.argtypes = [c_void_p, POINTER(c_float), c_int64]
        lib.sf_readf_float.restype = c_int64
        lib.sf_readf_double.argtypes = [c_void_p, POINTER(c_double), c_int64]
        lib.sf_readf_double.restype = c_int64
        lib.sf_seek.argtypes = [c_void_p, c_int64, c_int]
        lib.sf_seek.restype = c_int64
        lib.sf_command.argtypes = [c_void_p, c_int, c_void_p, c_int]
        lib.sf_command.restype = c_int
        lib.sf_get_string.argtypes = [c_void_p, c_int]
        lib.sf_get_string.restype = c_char_p

    # ---------------------------------------------------------------- factory
    @classmethod
    def instance(cls) -> "LibSndFile":
        if cls._error is not None:
            raise NativeLibraryMissing(cls._error)
        if cls._instance is None:
            errors: List[str] = []
            for candidate in _search_paths():
                try:
                    if not Path(candidate).exists():
                        continue
                    cls._instance = cls(Path(candidate))
                    get_logger("libsndfile").info("libsndfile loaded", extra={"path": str(candidate)})
                    break
                except NativeLibraryMissing as exc:
                    errors.append(str(exc))
            if cls._instance is None:
                cls._error = (
                    "libsndfile-1.dll was not found. Run 'python setup_env.py --install-native' "
                    "or copy libsndfile-1.dll into resources/native. Details: " + "; ".join(errors)
                )
                raise NativeLibraryMissing(cls._error)
        return cls._instance

    @classmethod
    def available(cls) -> bool:
        try:
            cls.instance()
            return True
        except NativeLibraryMissing:
            return False

    @property
    def version(self) -> str:
        try:
            buffer = ctypes.create_string_buffer(256)
            self._lib.sf_command(None, SFC_GET_LIB_VERSION, buffer, 256)
            return buffer.value.decode("latin-1", "replace").strip()
        except Exception:
            return "unknown"

    # ------------------------------------------------------------------- open
    def open_read(self, path: Path) -> "SndFileHandle":
        info = SfInfo()
        handle = self._lib.sf_open(str(path).encode("utf-8"), SFM_READ, ctypes.byref(info))
        if not handle:
            error = self._lib.sf_strerror(None) or b"unknown libsndfile error"
            raise OSError(f"sf_open failed for {path.name}: {error.decode('latin-1', 'replace')}")
        return SndFileHandle(self._lib, handle, info, path)


class SndFileHandle:
    """RAII wrapper around an open SNDFILE*."""

    def __init__(self, lib, handle: int, info: SfInfo, path: Path) -> None:
        self._lib = lib
        self._handle = handle
        self.info = info
        self.path = path
        self._closed = False

    @property
    def frames(self) -> int:
        return int(self.info.frames)

    @property
    def samplerate(self) -> int:
        return int(self.info.samplerate)

    @property
    def channels(self) -> int:
        return int(self.info.channels)

    @property
    def container(self) -> str:
        return CONTAINER_NAMES.get(self.info.format & SF_FORMAT_TYPEMASK, "unknown")

    @property
    def subtype(self) -> int:
        return int(self.info.format & SF_FORMAT_SUBMASK)

    @property
    def bit_depth(self) -> int:
        return SUBTYPE_BITS.get(self.subtype, 0)

    @property
    def is_float(self) -> bool:
        return self.subtype in (SF_FORMAT_FLOAT, SF_FORMAT_DOUBLE)

    def string(self, string_type: int) -> str:
        try:
            value = self._lib.sf_get_string(self._handle, string_type)
        except (OSError, ctypes.ArgumentError):
            return ""
        if not value:
            return ""
        return value.decode("utf-8", "replace").strip()

    def tags(self) -> Dict[str, str]:
        collected: Dict[str, str] = {}
        for string_type, key in STRING_TYPES.items():
            value = self.string(string_type)
            if value:
                collected[key] = value
        return collected

    def set_no_normalisation(self) -> None:
        """Forbid libsndfile from scaling float data (defence in depth)."""

        try:
            disabled = c_int(0)
            self._lib.sf_command(
                self._handle, SFC_SET_SCALE_FLOAT_INT_READ, ctypes.byref(disabled), ctypes.sizeof(disabled)
            )
            self._lib.sf_command(
                self._handle, SFC_SET_NORM_FLOAT, ctypes.byref(disabled), ctypes.sizeof(disabled)
            )
            self._lib.sf_command(
                self._handle, SFC_SET_NORM_DOUBLE, ctypes.byref(disabled), ctypes.sizeof(disabled)
            )
        except (OSError, ctypes.ArgumentError):
            return

    def read_int(self, frames: int) -> np.ndarray:
        """Read as left-justified int32 (bit exact for <= 24 bit sources)."""

        total = int(frames) * self.channels
        buffer = np.zeros(total, dtype=np.int32)
        pointer = buffer.ctypes.data_as(POINTER(c_int32))
        read = int(self._lib.sf_readf_int(self._handle, pointer, c_int64(int(frames))))
        if read <= 0:
            return np.zeros(0, dtype=np.int32)
        return buffer[: read * self.channels].copy()

    def read_float(self, frames: int) -> np.ndarray:
        total = int(frames) * self.channels
        buffer = np.zeros(total, dtype=np.float32)
        pointer = buffer.ctypes.data_as(POINTER(c_float))
        read = int(self._lib.sf_readf_float(self._handle, pointer, c_int64(int(frames))))
        if read <= 0:
            return np.zeros(0, dtype=np.float32)
        return buffer[: read * self.channels].copy()

    def read_double(self, frames: int) -> np.ndarray:
        total = int(frames) * self.channels
        buffer = np.zeros(total, dtype=np.float64)
        pointer = buffer.ctypes.data_as(POINTER(c_double))
        read = int(self._lib.sf_readf_double(self._handle, pointer, c_int64(int(frames))))
        if read <= 0:
            return np.zeros(0, dtype=np.float64)
        return buffer[: read * self.channels].copy()

    def seek(self, frame: int) -> int:
        position = int(self._lib.sf_seek(self._handle, c_int64(int(frame)), SEEK_SET))
        if position < 0:
            return -1
        return position

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._lib.sf_close(self._handle)
        except (OSError, ctypes.ArgumentError):
            pass
        self._handle = 0

    def __enter__(self) -> "SndFileHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        self.close()


def load_library(path: Optional[Path] = None) -> LibSndFile:
    """Load a specific DLL or the auto-detected one."""

    if path is not None:
        return LibSndFile(path)
    return LibSndFile.instance()
