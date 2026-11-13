import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.dsd import DopEncoder, DsdFormat
from src.core.formats import AudioFormat, SampleFormat, repack, supported_rates
from src.core.ring_buffer import RingBuffer

f16 = AudioFormat(44100, 2, SampleFormat.INT16)
f24 = AudioFormat(44100, 2, SampleFormat.INT24)
f32 = AudioFormat(44100, 2, SampleFormat.INT32)
data = struct.pack("<4h", -32768, 0, 32767, 1234)

r = repack(data, f16, f32)
print("int16->int32:", r.exact, r.reason, len(r.data))
back = repack(r.data, f32, f16)
print("int32->int16:", back.exact, back.data == data)
r24 = repack(data, f16, f24)
print("int16->int24:", r24.exact, r24.reason, r24.data.hex())
print("int24->int16:", repack(r24.data, f24, f16).data == data)

ring = RingBuffer(1024, frame_bytes=4)
print("write", ring.write(b"\x01" * 512), "read", len(ring.read(256)), "avail", ring.available_bytes)
payload, missing = ring.read_or_silence(1024)
print("underrun missing:", missing, "stats", ring.stats().underrun_events)

enc = DopEncoder(DsdFormat(2822400, 2))
out = enc.encode([b"\xff" * 8, b"\x00" * 8])
print("dop bytes:", len(out), out.hex())
enc2 = DopEncoder(DsdFormat(2822400, 2))
o1 = enc2.encode([b"\xff" * 4, b"\xff" * 4])
o2 = enc2.encode([b"\xff" * 4, b"\xff" * 4])
print("markers:", [o1[2], o1[5], o1[8], o2[2], o2[5]])
print("rates:", len(supported_rates()), supported_rates()[:5], supported_rates()[-3:])
print("float32 exact for 24-bit int:", repack(b"\x00\x00\x80\x7f\xff\xff", AudioFormat(48000, 2, SampleFormat.INT24), AudioFormat(48000, 2, SampleFormat.FLOAT32)).exact)
