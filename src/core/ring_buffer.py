"""Single-producer / single-consumer ring buffer with a lock-free consumer path.

Design constraints that motivated this implementation:

* The PortAudio callback (consumer) must never block, never allocate unbounded
  memory and never take a mutex.  ``collections.deque`` gives us atomic
  ``append`` / ``popleft`` / indexing in CPython, so the callback only performs
  a constant number of atomic operations plus one byte copy into the driver
  buffer.
* The feeder thread (producer) may block.  It never signals the consumer with a
  lock; it simply sleeps for a millisecond when the buffer is full and retries.
  Because the buffer target is measured in hundreds of milliseconds, this never
  affects timing.
* Frame alignment is enforced at both ends so a byte-oriented buffer can never
  desynchronise the channel interleave (a classical source of channel swaps).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

DEFAULT_CAPACITY_BYTES = 1 << 20
SLEEP_SLICE_SECONDS = 0.001


@dataclass(frozen=True, slots=True)
class RingStats:
    """Point-in-time snapshot of ring buffer health."""

    capacity_bytes: int
    available_bytes: int
    peak_fill_bytes: int
    produced_bytes: int
    consumed_bytes: int
    underrun_events: int
    underrun_bytes: int
    overrun_events: int
    discarded_bytes: int
    frame_bytes: int
    closed: bool

    @property
    def fill_ratio(self) -> float:
        if self.capacity_bytes <= 0:
            return 0.0
        return min(1.0, self.available_bytes / float(self.capacity_bytes))

    @property
    def available_frames(self) -> int:
        return self.available_bytes // self.frame_bytes if self.frame_bytes else 0


class RingBufferClosed(RuntimeError):
    """Raised when writing into a buffer that was closed by the engine."""


class RingBuffer:
    """Bounded byte FIFO for one producer and one consumer."""

    __slots__ = (
        "_chunks",
        "_capacity",
        "_frame_bytes",
        "_available",
        "_peak",
        "_produced",
        "_consumed",
        "_underruns",
        "_underrun_bytes",
        "_overruns",
        "_discarded",
        "_closed",
        "_name",
    )

    def __init__(self, capacity_bytes: int, frame_bytes: int = 4, name: str = "audio") -> None:
        if frame_bytes <= 0:
            raise ValueError("frame_bytes must be positive")
        if capacity_bytes <= 0:
            raise ValueError("capacity_bytes must be positive")
        self._capacity = (int(capacity_bytes) // frame_bytes) * frame_bytes
        self._frame_bytes = int(frame_bytes)
        self._chunks: Deque[bytes] = deque()
        self._available = 0
        self._peak = 0
        self._produced = 0
        self._consumed = 0
        self._underruns = 0
        self._underrun_bytes = 0
        self._overruns = 0
        self._discarded = 0
        self._closed = False
        self._name = name

    # ------------------------------------------------------------------ info
    @property
    def name(self) -> str:
        return self._name

    @property
    def capacity_bytes(self) -> int:
        return self._capacity

    @property
    def frame_bytes(self) -> int:
        return self._frame_bytes

    @property
    def available_bytes(self) -> int:
        return self._available

    @property
    def available_frames(self) -> int:
        return self._available // self._frame_bytes

    @property
    def free_bytes(self) -> int:
        return self._capacity - self._available

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def fill_ratio(self) -> float:
        return self._available / float(self._capacity) if self._capacity else 0.0

    def stats(self) -> RingStats:
        return RingStats(
            capacity_bytes=self._capacity,
            available_bytes=self._available,
            peak_fill_bytes=self._peak,
            produced_bytes=self._produced,
            consumed_bytes=self._consumed,
            underrun_events=self._underruns,
            underrun_bytes=self._underrun_bytes,
            overrun_events=self._overruns,
            discarded_bytes=self._discarded,
            frame_bytes=self._frame_bytes,
            closed=self._closed,
        )

    # -------------------------------------------------------------- producer
    def write(self, data: bytes) -> int:
        """Append as much of ``data`` as fits; returns the number of bytes stored."""

        if self._closed:
            raise RingBufferClosed(f"ring buffer '{self._name}' is closed")
        if not data:
            return 0
        if len(data) % self._frame_bytes != 0:
            raise ValueError("write length must be frame aligned")
        free = self._capacity - self._available
        if free < self._frame_bytes:
            self._overruns += 1
            return 0
        if len(data) > free:
            usable = (free // self._frame_bytes) * self._frame_bytes
            data = data[:usable]
            self._overruns += 1
        self._chunks.append(bytes(data))
        self._available += len(data)
        self._produced += len(data)
        if self._available > self._peak:
            self._peak = self._available
        return len(data)

    def write_blocking(self, data: bytes, timeout: Optional[float] = None, abort=None) -> int:
        """Write everything, waiting for the consumer when the buffer is full.

        The wait is a plain sleep on the producer thread; the audio callback is
        never involved in the synchronisation.  ``timeout=None`` waits until the
        consumer makes room (the correct behaviour while paused: no sample may
        ever be dropped).  ``abort`` is polled so a stopping feeder can leave
        promptly.
        """

        written = 0
        total = len(data)
        deadline = None if timeout is None else time.monotonic() + timeout
        while written < total:
            if self._closed:
                raise RingBufferClosed(f"ring buffer '{self._name}' is closed")
            if abort is not None and abort():
                return written
            accepted = self.write(data[written:])
            if accepted == 0:
                if deadline is not None and time.monotonic() >= deadline:
                    self._discarded += total - written
                    return written
                time.sleep(SLEEP_SLICE_SECONDS)
                continue
            written += accepted
        return written

    def reserve(self, byte_count: int) -> None:
        """Producer-side pacing: wait until ``byte_count`` bytes are free."""

        deadline = time.monotonic() + 5.0
        while self.free_bytes < byte_count:
            if self._closed:
                raise RingBufferClosed(f"ring buffer '{self._name}' is closed")
            if time.monotonic() >= deadline:
                return
            time.sleep(SLEEP_SLICE_SECONDS)

    # -------------------------------------------------------------- consumer
    def read(self, byte_count: int) -> bytes:
        """Pop up to ``byte_count`` bytes; the callback path takes no locks."""

        if byte_count <= 0 or self._available <= 0:
            return b""
        requested = min(byte_count, self._available)
        requested -= requested % self._frame_bytes
        if requested <= 0:
            return b""
        front = self._chunks[0]
        if len(front) == requested:
            self._chunks.popleft()
            self._available -= requested
            self._consumed += requested
            return front
        if len(front) > requested:
            self._chunks[0] = front[requested:]
            self._available -= requested
            self._consumed += requested
            return front[:requested]
        parts = []
        missing = requested
        while missing > 0:
            chunk = self._chunks[0]
            if len(chunk) <= missing:
                parts.append(self._chunks.popleft())
                missing -= len(chunk)
            else:
                parts.append(chunk[:missing])
                self._chunks[0] = chunk[missing:]
                missing = 0
        payload = b"".join(parts)
        self._available -= requested
        self._consumed += requested
        return payload

    def read_exact(self, byte_count: int) -> Optional[bytes]:
        """Pop exactly ``byte_count`` bytes or return ``None`` without consuming."""

        if byte_count <= 0 or byte_count > self._available:
            return None
        return self.read(byte_count)

    def read_or_silence(self, byte_count: int) -> tuple[bytes, int]:
        """Consumer helper returning ``(payload, missing_bytes)`` report."""

        payload = self.read(byte_count)
        missing = byte_count - len(payload)
        if missing > 0:
            self._underruns += 1
            self._underrun_bytes += missing
        return payload, missing

    def peek(self, byte_count: int) -> bytes:
        """Copy the next ``byte_count`` bytes without consuming them (UI only)."""

        if byte_count <= 0 or self._available <= 0:
            return b""
        wanted = min(byte_count, self._available)
        out = bytearray()
        for chunk in self._chunks:
            if len(out) >= wanted:
                break
            out.extend(chunk[: wanted - len(out)])
        return bytes(out)

    def drain(self) -> int:
        """Discard everything currently buffered."""

        dropped = self._available
        self._chunks.clear()
        self._available = 0
        self._discarded += dropped
        return dropped

    # ------------------------------------------------------------ lifecycle
    def close(self) -> None:
        self._closed = True
        self.drain()

    def reopen(self) -> None:
        self._closed = False

    def record_underrun(self, byte_count: int) -> None:
        self._underruns += 1
        self._underrun_bytes += byte_count

    def reset_counters(self) -> None:
        self._peak = self._available
        self._produced = 0
        self._consumed = 0
        self._underruns = 0
        self._underrun_bytes = 0
        self._overruns = 0
        self._discarded = 0

    def __len__(self) -> int:
        return self._available

    def __bool__(self) -> bool:
        return self._available > 0


def capacity_for_milliseconds(
    bytes_per_second: int, milliseconds: float, minimum_bytes: int = 1 << 16
) -> int:
    """Compute a frame aligned capacity for the requested buffering window."""

    raw = int(bytes_per_second * max(milliseconds, 1.0) / 1000.0)
    return max(minimum_bytes, raw)
