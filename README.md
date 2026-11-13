# True HQ Audio

**Bit-perfect reference player and audio rendering engine for Windows.**
Zero DSP. Zero resampling. Direct hardware clock. Studio-grade PySide6 interface.

```
┌──────────┐   ┌──────────────┐   ┌──────────────┐   ┌───────────┐   ┌─────┐
│ Decoder  │ → │ Container    │ → │ Lock-free    │ → │ Exclusive │ → │ DAC │
│ (lossless│   │ mapper       │   │ SPSC ring    │   │ driver    │   │     │
│  only)   │   │ (bit shifts) │   │ (no locks)   │   │ (no mixer)│   │     │
└──────────┘   └──────────────┘   └──────────────┘   └───────────┘   └─────┘
      native WAV · AIFF · DSF · DFF · FLAC          WASAPI Exclusive · WDM-KS · ASIO
```

---

## 1. The doctrine: nothing is allowed to touch the samples

True HQ Audio exists to answer one question: *does the DAC receive exactly the
bits that were pressed on the master?* Every design decision follows from it.

| Forbidden by design | Why |
| --- | --- |
| Volume / gain control | Attenuation is multiplication: it destroys low-order bits |
| ReplayGain, LUFS, normalisation | Changes the signal to match a loudness target |
| Compressor / limiter | Dynamic processing, audible and irreversible |
| Equaliser / convolver / crossfeed | Frequency-domain modification |
| Dither / noise shaping | Adds noise that is not in the master |
| Software resampler | Any rate conversion invents samples |
| Channel up/down-mix | Requires matrixing |
| PortAudio clipping/dither flags | `paClipOff` and `paDitherOff` are always set |

What the engine *is* allowed to do, because these are pure representation
changes and are proven bit-exact at runtime:

* **Widening**: 16-bit → 32-bit (`<< 16`), 24-bit → 32-bit (`<< 8`).
* **Container repacking**: 24-bit packed ↔ 32-bit words.
* **Bit-order permutation**: DSF is LSB-first, DoP is MSB-first.
* **DoP encapsulation**: the 1-bit DSD stream is embedded verbatim in 24-bit
  PCM frames with the standard `0x05 / 0xFA` markers.

Anything else is *refused*, not silently applied. If the DAC cannot accept the
source depth, the engine reports the refusal and removes the BIT-PERFECT badge
instead of degrading the audio behind your back.

---

## 2. What you get

* **Native decoding, no external conversion**
  * WAV / RF64: PCM 8/16/24/32-bit, IEEE float 32/64, WAVE_FORMAT_EXTENSIBLE.
  * AIFF / AIFF-C: big-endian PCM, `sowt` little-endian, `fl32`/`fl64`.
  * DSF and DFF: native 1-bit DSD64…DSD512 read as bitstreams.
  * FLAC and other lossless containers via libsndfile, with an ffmpeg fallback.
* **Clock-locked output**: the engine asks the driver which rates it accepts
  (`Pa_IsFormatSupported`), opens the stream at the *source* rate and verifies
  that the opened stream really runs at that rate. A driver that silently
  resamples is detected and reported.
* **Lock-free ring buffer** with a consumer path that takes no locks, while the
  `Pro Audio` MMCSS task + `TIME_CRITICAL` thread priority keep the feeder ahead.
* **Live integrity telemetry**: BLAKE2b digest of every byte handed to the
  driver, byte counters, xrun accounting, buffer occupancy history.
* **Hot-plug recovery**: unplugging the DAC pauses the session with the exact
  position; plugging it back re-opens the device, seeks 0.3 s earlier and resumes.
* **Gapless queue**: the next track is pre-decoded and spliced into the running
  stream as soon as the buffer drains when its hardware format matches (a few
  milliseconds, not a device re-open).
* **Studio dark UI** at 60–120 FPS: hand-painted widgets, animated badges,
  vector icons, no bitmap assets, no per-frame allocations in the paint path.

---

## 3. Requirements

| Component | Version |
| --- | --- |
| Windows | 10 1809+ or Windows 11, x64 |
| Python (source install) | **3.15.2** (validated exactly) |
| PySide6 | 6.12.0 |
| NumPy | 2.5.4 |
| sounddevice (PortAudio) | 0.5.6 |
| Inno Setup (installer) | 6.x |
| libsndfile (optional) | 1.2.x, `libsndfile-1.dll` |
| ffmpeg / ffprobe (optional) | any recent static build |

The packaged installer needs none of the above: it ships a self-contained build.

---

## 4. Install and run from source

```powershell
# Sustituye OWNER/REPOSITORY por el repositorio GitHub real
git clone https://github.com/OWNER/REPOSITORY.git
cd REPOSITORY
python setup_env.py            # creates .venv, installs deps, fetches native DLLs
python setup_env.py --run      # launches the player
```

`setup_env.py` verifies CPython 3.15.2 x64, builds an isolated `.venv`, installs
`requirements.txt`, then downloads `libsndfile-1.dll` and `ffmpeg`/`ffprobe` into
`resources/native/`. Every download is hashed with SHA-256 and recorded in
`resources/native/manifest.json`, so later runs verify integrity instead of
trusting the network.

Useful switches:

```powershell
python setup_env.py --check             # verify the installation
python setup_env.py --repair            # rebuild .venv from scratch
python setup_env.py --dev               # add pytest + pyinstaller
python setup_env.py --install-native    # only the native binaries
python setup_env.py --run --realtime    # launch with REALTIME_PRIORITY_CLASS
```

If the automatic download is blocked by a corporate proxy, copy
`libsndfile-1.dll`, `ffmpeg.exe` and `ffprobe.exe` into `resources/native/`
manually. The player still starts and decodes WAV/AIFF/DSF/DFF without them.

---

## 5. Choosing a bit-perfect output

| Driver family | Exclusive? | Bit-perfect? | Notes |
| --- | --- | --- | --- |
| **ASIO** | yes | ✅ | Bypasses the Windows audio engine entirely. Requires a PortAudio build with the ASIO SDK (not redistributable); the UI detects and lists it when present. |
| **WASAPI Exclusive** | yes | ✅ | `IAudioClient::Initialize` with `AUDCLNT_SHAREMODE_EXCLUSIVE`; clock switching works up to the DAC limit. The default choice. |
| **WDM-KS** | kernel streaming | ✅ | Direct kernel streaming; lowest latency, occasionally picky with consumer drivers. |
| **MME / DirectSound** | no | ❌ | Goes through the system mixer, which can resample. The UI disables the Exclusive switch and never shows the badge. |

The engine verifies the negotiated format after the stream opens: if the driver
reports a different rate or channel count, the stream is rejected as a silent
resampler.

### DSD

| Source | Transport | PCM rate |
| --- | --- | --- |
| DSD64 (2.8224 MHz) | DoP v1.0 | 176.4 kHz |
| DSD128 (5.6448 MHz) | DoP v1.0 | 352.8 kHz |
| DSD256 (11.2896 MHz) | DoP v1.0 | 705.6 kHz |
| DSD512 (22.5792 MHz) | DoP v1.0 | 1411.2 kHz |

Native ASIO DSD (a raw DSD byte stream to the driver) is not exposed by
PortAudio, so DoP is used at every rate. DoP requires the DAC to support
24-bit PCM at the transport rate; the Hardware tab shows exactly which DoP rates
your device accepted during probing.

---

## 6. The interface

| Area | Contents |
| --- | --- |
| Header | BIT-PERFECT badge, DAC selector, exclusive switch, driver and clock pills |
| Now playing | Title, artist, album, format chips, millisecond seek bar, transport |
| Session queue | Reorderable table (drag), double-click to play, Delete to remove, context menu |
| Library | Header-only index, instant search, folder scanning in a background thread |
| Hardware | Device list, exclusive mode, clock probing, live telemetry tiles, buffer sparkline |
| Session | Source metadata, DAC transport, integrity verdict, DSP policy chips |

Keyboard: `Space` play/pause · `Ctrl+O` open files · `Ctrl+Shift+O` open folder ·
`Ctrl+←/→` seek ±5 s · `Alt+←/→` previous/next · `Ctrl+.` stop · `F5` rescan DACs ·
`Ctrl+L` library · `Ctrl+Q` quit.

There is deliberately **no volume slider**: the badge would be meaningless if
the application could scale the samples. Use the DAC or the amplifier.

---

## 7. Project layout

```
true-hq-audio/
├── main.py                       entry point (CLI flags, freeze support)
├── setup_env.py                  self-bootstrapping installer (venv + natives)
├── truehqaudio.spec              PyInstaller one-folder specification
├── requirements.txt              pinned runtime dependencies
├── requirements-dev.txt          pytest + pyinstaller
├── pyproject.toml                metadata, pytest and ruff configuration
├── src/
│   ├── app.py                    bootstrap: mutex, logging, Qt, engine wiring
│   ├── config.py                 paths, settings store, JSON-lines logging
│   ├── core/
│   │   ├── audio_engine.py       supervisor + feeder + callback, clock locking
│   │   ├── devices.py            PortAudio discovery, exclusive negotiation, probing
│   │   ├── formats.py            sample-format algebra, bit-exact repacking
│   │   ├── ring_buffer.py        SPSC ring buffer, lock-free consumer
│   │   ├── dsd.py                DSF/DFF bit handling and the DoP encoder
│   │   ├── integrity.py          BLAKE2b digests, verdicts, DSP attestation
│   │   ├── telemetry.py          immutable telemetry snapshots + history
│   │   ├── priority.py           MMCSS "Pro Audio", TIME_CRITICAL, 1 ms timer
│   │   ├── playlist.py           queue model, repeat/shuffle, controller
│   │   ├── library.py            cached header-only library index
│   │   ├── metadata.py           ID3v2 and tag normalisation
│   │   ├── events.py             decoupled publish/subscribe bus
│   │   ├── libsndfile.py         ctypes binding for libsndfile
│   │   └── decoders/
│   │       ├── wav.py            RIFF/RF64 parser
│   │       ├── aiff.py           AIFF/AIFF-C parser (80-bit float rate)
│   │       ├── dsf.py            Sony DSF reader
│   │       ├── dff.py            Philips DSDIFF reader
│   │       ├── sndfile_backend.py libsndfile decoder (FLAC)
│   │       ├── ffmpeg_backend.py  ffmpeg/ffprobe fallback decoder
│   │       └── registry.py       magic sniffing + ordered fallback ladder
│   └── ui/
│       ├── main_window.py        shell, header, tabs, transport wiring
│       ├── theme.py              palette, generated QSS, fonts, animations
│       ├── icons.py              QPainterPath icon set (no image assets)
│       ├── qt_bridge.py          EventBus → Qt signals (thread safe)
│       └── widgets/              badge, seek bar, transport, panels, tables
├── resources/
│   ├── native/                   libsndfile + ffmpeg (downloaded, git-ignored)
│   ├── icons/                    generated truehq.ico
│   └── styles/                   generated studio.qss (auditing artefact)
├── scripts/
│   ├── build.py                  tests → PyInstaller → Inno Setup
│   ├── make_icon.py              multi-resolution ICO renderer
│   └── export_stylesheet.py      writes resources/styles/studio.qss
├── installer/
│   ├── truehqaudio.iss           Inno Setup script (per-user, ES/EN)
│   ├── version_info.txt          Windows VERSIONINFO block
│   └── LICENSE.txt               EULA displayed by the installer
├── tests/
│   ├── assets/                   deterministic 24-bit/96 kHz WAV + FLAC fixture
│   ├── test_core.py              formats, ring buffer, DoP, playlist, settings
│   ├── test_native_decoders.py   WAV/AIFF/DSF/DFF round trips
│   ├── test_flac_backend.py      libsndfile/ffmpeg bit-identity + engine proof
│   ├── test_engine_loopback.py   bit-exact engine proof with a fake driver
│   ├── test_ui_widgets.py        headless model/delegate/dialog/paint tests
│   └── ui_smoke.py               renders the whole UI offscreen / on screen
└── .github/workflows/build.yml   CI: tests → freeze → installer → release
```

---

## 8. Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests
```

`test_engine_loopback.py` is the important one. It replaces the PortAudio backend
with a fake `RawOutputStream` that consumes the driver buffer in real time and
records every byte, then asserts:

1. the recorded bytes are **identical** to the WAV data chunk,
2. after a seek, the recorded bytes are identical to the file at that offset,
3. a 768 kHz file on a 48 kHz DAC is **refused** (no resampling),
4. a DSF file produces valid DoP frames with alternating `0x05/0xFA` markers,
5. starving the ring buffer raises xruns and removes the BIT-PERFECT badge,
6. a gapless splice is sample-continuous: the captured stream equals
   `track A + track B` byte for byte, with zero xruns,
7. a format change between tracks reopens the DAC at the new clock and keeps
   playing,
8. the natural end of a track is not counted as an underrun, and pressing play
   afterwards restarts it.

`test_flac_backend.py` repeats the bit-identity proof through libsndfile and
through the ffmpeg fallback, and `test_ui_widgets.py` renders every custom
widget offscreen so QPainter regressions fail the build.

`tests/ui_smoke.py` builds the full window, drives it and writes PNGs — useful
for reviewing the design without a DAC attached.

---

## 9. Build and package

```powershell
python setup_env.py --dev
python scripts/build.py
```

Outputs:

```
dist/TrueHQAudio/TrueHQAudio.exe                    frozen application
dist/TrueHQAudio-2.5.9-portable-x64.zip             portable archive
installer/Output/TrueHQAudio-2.5.9-x64-Setup.exe    installer
```

`scripts/build.py` renders the icon, exports the QSS, runs the tests, freezes the
app with PyInstaller and compiles the Inno Setup script. The installer is
per-user by default (no UAC), registers an "Open with" entry for
`.wav/.flac/.aiff/.dsf/.dff`, creates the `%LOCALAPPDATA%\TrueHQAudio` tree and
offers an optional start-with-Windows entry.

> **Installer size.** The frozen tree is ~460 MB because it bundles Qt and the
> static ffmpeg build. ffmpeg is only a fallback decoder: delete
> `resources/native/ffmpeg.exe` and `ffprobe.exe` before building to ship a
> ~280 MB installer that still decodes WAV/AIFF/DSF/DFF natively and FLAC via
> libsndfile. The application detects the missing binaries and simply disables
> the fallback.

### CI/CD

`.github/workflows/build.yml` runs on every push and pull request:

1. installs CPython 3.15.2 (with a python.org fallback if setup-python does not
   yet publish it),
2. installs `requirements-dev.txt`,
3. byte-compiles every module, runs the full test suite and the diagnostics,
4. downloads the native binaries, renders the icon, freezes the app,
5. smoke-tests the frozen `.exe` with `--diagnostics`,
6. builds the portable ZIP and the Inno Setup installer,
7. publishes SHA-256 checksums and, on a `v*` tag, creates a GitHub Release.

---

## 10. Where things live at runtime

| Path | Contents |
| --- | --- |
| `%LOCALAPPDATA%\TrueHQAudio\logs\truehq-YYYYMMDD.jsonl` | structured JSON-lines log |
| `%LOCALAPPDATA%\TrueHQAudio\config\settings.json` | preferences (atomic writes) |
| `%LOCALAPPDATA%\TrueHQAudio\cache\library-index.json` | library index cache |
| `%LOCALAPPDATA%\TrueHQAudio\playlists\last-session.json` | restored queue |

Settings never contain volume or DSP state, because those concepts do not exist
in the engine.

---

## 11. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `DAC cannot accept … without resampling` | The device does not support that clock in exclusive mode. Pick another output in **Hardware**, or use a DAC that supports the rate. Resampling is intentionally not offered. |
| Badge shows **NOT BIT-PERFECT** with "shared-mode driver" | MME/DirectSound/WASAPI shared are selected. Choose a WASAPI Exclusive or ASIO device. |
| Badge shows **NOT BIT-PERFECT** with "underrun" | The system stalled long enough to starve the buffer. Raise the ring buffer in **Engine preferences**, close CPU-heavy applications, or run with `--realtime`. |
| `libsndfile-1.dll was not found` | Run `python setup_env.py --install-native`, or copy the DLL into `resources/native/`. FLAC then falls back to ffmpeg. |
| FLAC will not play at all | Neither libsndfile nor ffmpeg is available. WAV/AIFF/DSF/DFF still work natively. |
| No ASIO entry in the device list | The bundled PortAudio has no ASIO support (Steinberg licensing). Install a PortAudio build with ASIO and point `TRUEEHQ_PORTAUDIO` at it, or use WASAPI Exclusive. |
| Clicking a 384 kHz file fails on a 192 kHz DAC | Expected: the engine refuses to resample. |
| Window appears but no devices | Another application holds the DAC in exclusive mode, or the driver is disabled in Device Manager. |

Diagnostics without launching the UI:

```powershell
.\.venv\Scripts\python.exe main.py --diagnostics
```

It prints the Python build, PortAudio version, decoder capabilities, every output
device with its driver family, and the probed clock matrix of the preferred DAC.

---

## 12. Honest limitations

* **No volume control.** By design. A digital volume stage would invalidate the
  entire premise of the application.
* **Lossless sources only for the badge.** MP3/AAC refuse to play: decoding them
  is inherently lossy, and pretending otherwise would be dishonest.
* **DoP only for DSD.** PortAudio does not expose native ASIO DSD.
* **DST-compressed DFF is refused** (it needs a dedicated lossless DST decoder).
* **Windows only.** The engine, the priority code and the packaging target
  Windows 10/11 x64.
* **Bit-perfect is verified structurally**, not acoustically: the engine proves
  the bytes it hands to the driver are the file's bytes, and that the driver
  accepted the exact format. Proving what happens *inside* the DAC requires a
  loopback capture, which is out of scope.

---

## 13. License

Application code: MIT — see `LICENSE`.
Bundled third-party components keep their own licenses: Qt/PySide6 (LGPL v3),
NumPy (BSD-3), PortAudio/sounddevice (MIT), libsndfile (LGPL v2.1),
FFmpeg (LGPL/GPL depending on the build).
