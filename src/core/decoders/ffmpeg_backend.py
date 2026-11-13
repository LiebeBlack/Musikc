"""ffmpeg/ffprobe fallback decoder.

Used only when libsndfile is unavailable or cannot open a lossless container.
ffmpeg is invoked with explicit ``pcm_s32le`` / ``pcm_f32le`` / ``pcm_f64le``
outputs and *no* filter graph at all, so the decoder stage is bit exact; the
process must never be asked to resample or to change the channel layout.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional


from ...config import bundled_root, native_path
from ..formats import AudioFormat, SampleFormat
from .base import CorruptStreamError, Decoder, DecodeError, SourceInfo, normalise_tags

CREATE_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0

SAMPLE_FMT_MAP: Dict[str, tuple[SampleFormat, int]] = {
    "u8": (SampleFormat.INT16, 8),
    "u8p": (SampleFormat.INT16, 8),
    "s16": (SampleFormat.INT16, 16),
    "s16p": (SampleFormat.INT16, 16),
    "s32": (SampleFormat.INT32, 32),
    "s32p": (SampleFormat.INT32, 32),
    "flt": (SampleFormat.FLOAT32, 32),
    "fltp": (SampleFormat.FLOAT32, 32),
    "dbl": (SampleFormat.FLOAT64, 64),
    "dblp": (SampleFormat.FLOAT64, 64),
}

CODEC_LOSSLESS = {
    "flac",
    "alac",
    "wavpack",
    "ape",
    "tta",
    "truehd",
    "mlp",
    "pcm_s16le",
    "pcm_s24le",
    "pcm_s32le",
    "pcm_f32le",
    "pcm_f64le",
}


def find_tool(name: str) -> Optional[Path]:
    """Locate ffmpeg/ffprobe in the bundle, next to the app, or on PATH."""

    executable = f"{name}.exe" if sys.platform.startswith("win") else name
    candidates = [
        native_path(executable),
        bundled_root() / executable,
        bundled_root() / "resources" / "native" / executable,
        Path(__file__).resolve().parents[3] / "resources" / "native" / executable,
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    located = shutil.which(name)
    return Path(located) if located else None


def ffmpeg_available() -> bool:
    return find_tool("ffmpeg") is not None and find_tool("ffprobe") is not None


class FfmpegDecoder(Decoder):
    container_name = "FFMPEG"
    codec_name = "ffmpeg"

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._ffmpeg = find_tool("ffmpeg")
        self._ffprobe = find_tool("ffprobe")
        if self._ffmpeg is None or self._ffprobe is None:
            raise DecodeError("ffmpeg/ffprobe binaries are not available")
        self._process: Optional[subprocess.Popen] = None
        self._probe: Dict[str, object] = {}
        self._output_codec = "pcm_s32le"
        self._pcm_format = SampleFormat.INT32
        self._valid_bits = 24

    @staticmethod
    def matches(path: Path) -> bool:
        return ffmpeg_available()

    def _run_probe(self) -> Dict[str, object]:
        command = [
            str(self._ffprobe),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(self._path),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=30,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DecodeError(f"ffprobe failed to start: {exc}") from exc
        if completed.returncode != 0:
            message = completed.stderr.decode("utf-8", "replace").strip()
            raise CorruptStreamError(f"ffprobe rejected {self._path.name}: {message}")
        try:
            payload = json.loads(completed.stdout.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise CorruptStreamError("ffprobe returned malformed JSON") from exc
        streams = payload.get("streams") or []
        if not streams:
            raise CorruptStreamError(f"{self._path.name}: no audio stream found")
        stream = streams[0]
        merged = dict(stream)
        merged["_format"] = payload.get("format", {})
        return merged

    def _build_info(self) -> SourceInfo:
        probe = self._run_probe()
        self._probe = probe
        codec_name = str(probe.get("codec_name", "unknown"))
        channels = int(probe.get("channels") or 0)
        rate = int(probe.get("sample_rate") or 0)
        if channels <= 0 or rate <= 0:
            raise CorruptStreamError("ffprobe reported an invalid stream description")

        sample_fmt = str(probe.get("sample_fmt") or "s16")
        pcm_format, default_bits = SAMPLE_FMT_MAP.get(sample_fmt, (SampleFormat.INT32, 32))
        raw_bits = int(probe.get("bits_per_raw_sample") or 0)
        sample_bits = int(probe.get("bits_per_sample") or 0)
        valid_bits = raw_bits or sample_bits or default_bits

        if pcm_format is SampleFormat.INT32:
            if valid_bits <= 16:
                self._output_codec = "pcm_s32le"
                self._valid_bits = 16
            elif valid_bits <= 24:
                self._output_codec = "pcm_s32le"
                self._valid_bits = 24
            else:
                self._output_codec = "pcm_s32le"
                self._valid_bits = 32
            self._pcm_format = SampleFormat.INT32
            audio_format = AudioFormat(rate, channels, SampleFormat.INT32, self._valid_bits)
        elif pcm_format is SampleFormat.INT16:
            self._output_codec = "pcm_s16le"
            self._pcm_format = SampleFormat.INT16
            self._valid_bits = 16
            audio_format = AudioFormat(rate, channels, SampleFormat.INT16, 16)
        elif pcm_format is SampleFormat.FLOAT32:
            self._output_codec = "pcm_f32le"
            self._pcm_format = SampleFormat.FLOAT32
            self._valid_bits = 32
            audio_format = AudioFormat(rate, channels, SampleFormat.FLOAT32, 32)
        else:
            self._output_codec = "pcm_f64le"
            self._pcm_format = SampleFormat.FLOAT64
            self._valid_bits = 64
            audio_format = AudioFormat(rate, channels, SampleFormat.FLOAT64, 64)

        duration = float(probe.get("duration") or 0.0)
        if duration <= 0:
            duration = float((probe.get("_format") or {}).get("duration") or 0.0)  # type: ignore[union-attr]
        total_frames = int(round(duration * rate)) if duration > 0 else 0
        tags = self._tags_from_probe(probe)
        notes: List[str] = ["decoded with ffmpeg (libsndfile path unavailable)"]
        if codec_name.lower() in CODEC_LOSSLESS:
            notes.append("lossless codec")
        bitrate = int((probe.get("_format") or {}).get("bit_rate") or 0)  # type: ignore[union-attr]
        return SourceInfo(
            path=self._path,
            container=str((probe.get("_format") or {}).get("format_name", "ffmpeg")),  # type: ignore[union-attr]
            codec=codec_name,
            audio_format=audio_format,
            total_frames=total_frames,
            duration=duration,
            tags=tags,
            notes=tuple(notes),
            file_size=self._file_size,
            bitrate=bitrate,
            lossless=codec_name.lower() in CODEC_LOSSLESS,
        )

    @staticmethod
    def _tags_from_probe(probe: Dict[str, object]) -> Dict[str, str]:
        raw = {}
        stream_tags = probe.get("tags") or {}
        format_tags = (probe.get("_format") or {}).get("tags") or {}  # type: ignore[union-attr]
        if isinstance(stream_tags, dict):
            raw.update({str(k): str(v) for k, v in stream_tags.items()})
        if isinstance(format_tags, dict):
            for key, value in format_tags.items():
                raw.setdefault(str(key), str(value))
        return normalise_tags(raw)

    # ---------------------------------------------------------------- process
    def _start_process(self, start_frame: int = 0) -> None:
        info = self.info
        command = [
            str(self._ffmpeg),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-accurate_seek",
        ]
        if start_frame > 0:
            command += ["-ss", f"{start_frame / info.sample_rate:.6f}"]
        command += [
            "-i",
            str(self._path),
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-acodec",
            self._output_codec,
            "-ar",
            str(info.sample_rate),
            "-ac",
            str(info.channels),
            "-f",
            "s32le" if self._output_codec == "pcm_s32le" else (
                "f32le" if self._output_codec == "pcm_f32le" else (
                    "f64le" if self._output_codec == "pcm_f64le" else "s16le"
                )
            ),
            "pipe:1",
        ]
        try:
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1 << 20,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DecodeError(f"cannot start ffmpeg decoder: {exc}") from exc

    def _stop_process(self) -> None:
        if self._process is None:
            return
        process = self._process
        self._process = None
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        except (OSError, subprocess.SubprocessError):
            pass
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    def read(self, frames: int) -> bytes:
        info = self.info
        if frames <= 0:
            return b""
        remaining = max(0, info.total_frames - self._position)
        want = min(int(frames), remaining if info.total_frames else int(frames))
        if want <= 0:
            return b""
        if self._process is None:
            self._start_process(self._position)
        assert self._process is not None and self._process.stdout is not None
        frame_bytes = info.audio_format.frame_bytes
        expected = want * frame_bytes
        payload = bytearray()
        while len(payload) < expected:
            chunk = self._process.stdout.read(expected - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
        if not payload:
            if info.total_frames:
                self._position = info.total_frames
            self._stop_process()
            return b""
        produced = len(payload) // frame_bytes
        self._position += produced
        return bytes(payload[: produced * frame_bytes])

    def seek(self, frame: int) -> int:
        info = self.info
        target = max(0, min(int(frame), info.total_frames or int(frame)))
        self._stop_process()
        self._position = target
        if target > 0:
            self._start_process(target)
        return target

    def close(self) -> None:
        self._stop_process()
        super().close()
