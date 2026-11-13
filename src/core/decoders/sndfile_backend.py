"""libsndfile backed decoder (FLAC, WAV, AIFF and any other sndfile format).

FLAC is lossless, therefore libsndfile's integer output is bit exact.  Samples
are exposed as left-justified int32 with ``valid_bits`` set to the true source
depth, so the hardware conversion that follows is a pure shift.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional


from ..formats import AudioFormat, SampleFormat
from ..libsndfile import LibSndFile, NativeLibraryMissing, SndFileHandle
from .base import CorruptStreamError, Decoder, DecodeError, SourceInfo, normalise_tags

SF_CONTAINER_CODEC = {
    "FLAC": "FLAC lossless",
    "WAV": "PCM WAVE",
    "AIFF": "PCM AIFF",
    "OGG": "OGG",
    "AU": "PCM AU",
}


class SndFileDecoder(Decoder):
    """Generic reader that delegates the container to libsndfile."""

    container_name = "SNDFILE"
    codec_name = "PCM"

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._sf: Optional[SndFileHandle] = None
        self._read_int = True

    @staticmethod
    def is_available() -> bool:
        return LibSndFile.available()

    @staticmethod
    def matches(path: Path) -> bool:
        return path.suffix.lower() in (".flac", ".wav", ".wave", ".aiff", ".aif", ".aifc", ".au")

    def _sndfile_handle(self) -> SndFileHandle:
        if self._sf is None:
            try:
                library = LibSndFile.instance()
            except NativeLibraryMissing as exc:
                raise DecodeError(str(exc)) from exc
            try:
                self._sf = library.open_read(self._path)
            except OSError as exc:
                raise CorruptStreamError(str(exc)) from exc
            self._sf.set_no_normalisation()
            self._read_int = not self._sf.is_float
        return self._sf

    def _build_info(self) -> SourceInfo:
        handle = self._sndfile_handle()
        bits = handle.bit_depth
        if bits <= 0:
            raise CorruptStreamError(f"{self._path.name}: libsndfile reported an unknown bit depth")
        if handle.is_float:
            sample_format = SampleFormat.FLOAT32 if bits == 32 else SampleFormat.FLOAT64
            valid_bits = bits
            codec = SF_CONTAINER_CODEC.get(handle.container, handle.container) + " float"
        else:
            sample_format = SampleFormat.INT32
            valid_bits = bits
            codec = SF_CONTAINER_CODEC.get(handle.container, handle.container)
        channels = handle.channels
        rate = handle.samplerate
        if channels <= 0 or rate <= 0:
            raise CorruptStreamError(f"{self._path.name}: invalid stream description")
        audio_format = AudioFormat(rate, channels, sample_format, valid_bits)
        total_frames = handle.frames
        if total_frames < 0:
            total_frames = 0
        duration = total_frames / float(rate) if rate else 0.0
        tags = normalise_tags(handle.tags())
        notes: List[str] = [f"decoded with {LibSndFile.instance().version}"]
        if handle.container == "FLAC" and bits < 16:
            notes.append(f"unusual FLAC depth {bits} bit")
        if handle.container in ("WAV", "AIFF") and bits in (16, 24):
            notes.append("native parser bypassed (libsndfile path)")
        bitrate = int(handle.frames * channels * (bits / 8.0) * 8 / duration) if duration > 0 else 0
        return SourceInfo(
            path=self._path,
            container=handle.container,
            codec=codec,
            audio_format=audio_format,
            total_frames=total_frames,
            duration=duration,
            tags=tags,
            notes=tuple(notes),
            file_size=self._file_size,
            bitrate=bitrate,
            lossless=handle.container in ("FLAC", "WAV", "AIFF", "AU"),
        )

    def read(self, frames: int) -> bytes:
        info = self.info
        if frames <= 0:
            return b""
        remaining = max(0, info.total_frames - self._position)
        want = min(int(frames), remaining)
        if want <= 0:
            return b""
        handle = self._sndfile_handle()
        if self._read_int:
            samples = handle.read_int(want)
            if samples.size == 0:
                self._position = info.total_frames
                return b""
            produced = samples.size // info.channels
            self._position += produced
            return samples.astype("<i4", copy=False).tobytes()
        if info.audio_format.sample_format is SampleFormat.FLOAT32:
            samples = handle.read_float(want)
            dtype = "<f4"
        else:
            samples = handle.read_double(want)
            dtype = "<f8"
        if samples.size == 0:
            self._position = info.total_frames
            return b""
        produced = samples.size // info.channels
        self._position += produced
        return samples.astype(dtype, copy=False).tobytes()

    def seek(self, frame: int) -> int:
        info = self.info
        target = max(0, min(int(frame), info.total_frames))
        handle = self._sndfile_handle()
        achieved = handle.seek(target)
        if achieved < 0:
            raise CorruptStreamError(f"{self._path.name}: libsndfile refused the seek")
        self._position = achieved
        return achieved

    def close(self) -> None:
        if self._sf is not None:
            self._sf.close()
            self._sf = None
        super().close()
