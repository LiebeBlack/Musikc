import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sounddevice as sd

devices = sd.query_devices()
apis = sd.query_hostapis()
for index, raw in enumerate(devices):
    if raw["max_output_channels"] <= 0:
        continue
    api = apis[raw["hostapi"]]["name"]
    for exclusive in (True, False):
        if "WASAPI" not in api and exclusive:
            continue
        extra = sd.WasapiSettings(exclusive=True) if ("WASAPI" in api and exclusive) else None
        for dtype in ("int24", "int32"):
            try:
                stream = sd.RawOutputStream(
                    device=index,
                    samplerate=48000,
                    channels=2,
                    dtype=dtype,
                    blocksize=0,
                    latency=0.02,
                    extra_settings=extra,
                )
                stream.close()
                print(f"OK   [{index:>2}] {api:<22} {raw['name'][:42]:<42} excl={exclusive} {dtype}")
            except Exception as exc:
                print(f"FAIL [{index:>2}] {api:<22} {raw['name'][:42]:<42} excl={exclusive} {dtype}: {exc}")
