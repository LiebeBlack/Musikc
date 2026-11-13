"""Fast, cached music library index.

Scanning reads only container headers (a few kilobytes per file), never audio
data, so a 50 000 track collection indexes in seconds.  Results are cached per
folder with modification times so subsequent launches are instant.
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..config import SUPPORTED_EXTENSIONS, cache_directory, get_logger

LOGGER = get_logger("library")

CACHE_VERSION = 2
CACHE_FILE = "library-index.json"
MAX_WORKERS = 8


@dataclass
class LibraryEntry:
    path: str
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float = 0.0
    resolution: str = ""
    container: str = ""
    codec: str = ""
    size_bytes: int = 0
    is_dsd: bool = False
    sample_rate: int = 0
    bit_depth: int = 0
    channels: int = 0
    modified: float = 0.0

    @property
    def filename(self) -> str:
        return Path(self.path).name

    @property
    def display_title(self) -> str:
        return self.title or Path(self.path).stem

    def matches(self, needle: str) -> bool:
        if not needle:
            return True
        haystack = " ".join(
            (self.display_title, self.artist, self.album, self.container, self.resolution, self.filename)
        ).lower()
        return all(token in haystack for token in needle.lower().split())

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, object]) -> "LibraryEntry":
        allowed = {name for name in cls.__dataclass_fields__}
        cleaned = {key: value for key, value in payload.items() if key in allowed and value is not None}
        cleaned.setdefault("path", "")
        if not cleaned["path"]:
            raise ValueError("entry without path")
        return cls(**cleaned)  # type: ignore[arg-type]


@dataclass
class ScanReport:
    scanned: int = 0
    added: int = 0
    skipped: int = 0
    failed: int = 0
    elapsed: float = 0.0
    cancelled: bool = False
    errors: List[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        base = f"{self.added} tracks indexed in {self.elapsed:.1f}s"
        if self.failed:
            base += f" · {self.failed} unreadable"
        if self.cancelled:
            base += " · cancelled"
        return base


def discover_files(folders: Iterable[Path], recursive: bool = True) -> List[Path]:
    found: List[Path] = []
    for folder in folders:
        root = Path(folder)
        if not root.exists():
            continue
        if root.is_file():
            if root.suffix.lower() in SUPPORTED_EXTENSIONS:
                found.append(root)
            continue
        try:
            if recursive:
                for current_root, _dirs, files in os.walk(root):
                    for name in files:
                        if Path(name).suffix.lower() in SUPPORTED_EXTENSIONS:
                            found.append(Path(current_root) / name)
            else:
                for entry in root.iterdir():
                    if entry.is_file() and entry.suffix.lower() in SUPPORTED_EXTENSIONS:
                        found.append(entry)
        except (OSError, PermissionError) as exc:
            LOGGER.warning("cannot walk folder", extra={"folder": str(root), "error": str(exc)})
    return found


class Library:
    """Thread-safe index with a header-only scanner."""

    def __init__(self, folders: Optional[Sequence[str]] = None) -> None:
        self._lock = threading.RLock()
        self._entries: Dict[str, LibraryEntry] = {}
        self._folders: List[str] = [str(folder) for folder in (folders or [])]
        self._loaded = False
        self._cache_path = cache_directory() / CACHE_FILE
        self._version = CACHE_VERSION

    # --------------------------------------------------------------- properties
    @property
    def folders(self) -> List[str]:
        with self._lock:
            return list(self._folders)

    @property
    def entries(self) -> List[LibraryEntry]:
        with self._lock:
            return list(self._entries.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def set_folders(self, folders: Sequence[str]) -> None:
        with self._lock:
            self._folders = [str(folder) for folder in folders]

    def add_folder(self, folder: str) -> None:
        with self._lock:
            if folder not in self._folders:
                self._folders.append(folder)

    # --------------------------------------------------------------------- cache
    def load_cache(self) -> int:
        with self._lock:
            if self._loaded:
                return len(self._entries)
            self._loaded = True
            if not self._cache_path.exists():
                return 0
            try:
                payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                LOGGER.warning("library cache unreadable", extra={"error": str(exc)})
                return 0
            if payload.get("version") != self._version:
                return 0
            restored = 0
            for raw in payload.get("entries", []):
                try:
                    entry = LibraryEntry.from_dict(raw)
                except (TypeError, ValueError):
                    continue
                if Path(entry.path).exists():
                    self._entries[entry.path] = entry
                    restored += 1
            return restored

    def save_cache(self) -> None:
        with self._lock:
            payload = {
                "version": self._version,
                "generated": time.time(),
                "entries": [entry.to_dict() for entry in self._entries.values()],
            }
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._cache_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self._cache_path)
        except OSError as exc:
            LOGGER.warning("library cache write failed", extra={"error": str(exc)})

    # --------------------------------------------------------------------- scan
    def scan(
        self,
        folders: Optional[Sequence[str]] = None,
        probe=None,
        progress: Optional[Callable[[int, int, Path], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
        max_workers: int = MAX_WORKERS,
    ) -> ScanReport:
        started = time.monotonic()
        targets = [Path(folder) for folder in (folders or self.folders)]
        files = discover_files(targets)
        report = ScanReport(scanned=len(files))
        if probe is None:
            from .decoders.registry import probe_file as probe
        with self._lock:
            known = {
                path: entry.modified
                for path, entry in self._entries.items()
            }
        total = len(files)
        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="truehq-scan") as executor:
            futures = {}
            for path in files:
                try:
                    modified = path.stat().st_mtime
                except OSError:
                    report.skipped += 1
                    continue
                key = str(path)
                if known.get(key) == modified:
                    completed += 1
                    report.skipped += 1
                    continue
                futures[executor.submit(self._probe_one, path, probe)] = (str(path), modified)
            for future in as_completed(futures):
                if should_cancel is not None and should_cancel():
                    report.cancelled = True
                    for pending in futures:
                        pending.cancel()
                    break
                completed += 1
                key, modified = futures[future]
                entry, error = future.result()
                if entry is None:
                    report.failed += 1
                    if error:
                        report.errors.append(error)
                    if progress is not None:
                        progress(completed, total, Path(key))
                    continue
                entry.modified = modified
                with self._lock:
                    self._entries[entry.path] = entry
                report.added += 1
                if progress is not None:
                    progress(completed, total, Path(entry.path))
        if should_cancel is not None and should_cancel():
            report.cancelled = True
        report.elapsed = time.monotonic() - started
        with self._lock:
            stale = [path for path in self._entries if not Path(path).exists()]
            for path in stale:
                del self._entries[path]
        if not report.cancelled:
            self.save_cache()
        LOGGER.info(
            "library scan finished",
            extra={
                "added": report.added,
                "skipped": report.skipped,
                "failed": report.failed,
                "elapsed": round(report.elapsed, 2),
            },
        )
        return report

    @staticmethod
    def _probe_one(path: Path, probe) -> Tuple[Optional[LibraryEntry], str]:
        try:
            info = probe(path)
            return (
                LibraryEntry(
                    path=str(path),
                    title=info.title,
                    artist=info.artist,
                    album=info.album,
                    duration=info.duration,
                    resolution=info.resolution_text,
                    container=info.container,
                    codec=info.codec,
                    size_bytes=info.file_size,
                    is_dsd=info.is_dsd,
                    sample_rate=info.sample_rate,
                    bit_depth=info.audio_format.valid_bits,
                    channels=info.channels,
                ),
                "",
            )
        except Exception as exc:  # noqa: BLE001 - one broken file must not stop the scan
            return None, f"{path}: {exc}"

    # ------------------------------------------------------------------- queries
    def search(self, needle: str = "", limit: int = 5000) -> List[LibraryEntry]:
        results = [entry for entry in self.entries if entry.matches(needle)]
        results.sort(key=lambda entry: (entry.artist.lower(), entry.album.lower(), entry.display_title.lower()))
        return results[:limit]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self.save_cache()

    def remove_missing(self) -> int:
        removed = 0
        with self._lock:
            for path in [path for path in self._entries if not Path(path).exists()]:
                del self._entries[path]
                removed += 1
        return removed

    def stats(self) -> Dict[str, object]:
        entries = self.entries
        total_bytes = sum(entry.size_bytes for entry in entries)
        total_seconds = sum(entry.duration for entry in entries)
        formats: Dict[str, int] = {}
        for entry in entries:
            formats[entry.container or "?"] = formats.get(entry.container or "?", 0) + 1
        return {
            "tracks": len(entries),
            "folders": len(self.folders),
            "bytes": total_bytes,
            "seconds": total_seconds,
            "formats": formats,
        }
