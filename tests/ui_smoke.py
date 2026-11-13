"""UI smoke test: builds the window, renders every tab and saves PNGs.

Usage:  python tests/ui_smoke.py [output_dir]
"""

import os
import sys
import tempfile
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from src.config import SettingsStore, setup_logging
from src.core.audio_engine import AudioEngine, EngineConfig
from src.core.devices import DeviceManager
from src.core.events import EventBus
from src.core.library import Library
from src.core.playlist import PlaybackController, Playlist, Track
from src.core.telemetry import TelemetryHub
from src.ui.main_window import MainWindow
from src.ui.qt_bridge import QtBridge
from src.ui.theme import apply_theme


def make_tracks() -> Playlist:
    playlist = Playlist("Smoke")
    samples = [
        ("C:/Music/Alice Coltrane - Journey in Satchidananda.flac", "Journey in Satchidananda", "Alice Coltrane", "Journey in Satchidananda", 381.0, "24-bit · 96 kHz", "FLAC"),
        ("C:/Music/Bill Evans - Peace Piece.flac", "Peace Piece", "Bill Evans", "Everybody Digs Bill Evans", 419.0, "24-bit · 192 kHz", "FLAC"),
        ("C:/Music/Miles Davis - So What.dsf", "So What", "Miles Davis", "Kind of Blue", 545.0, "DSD64 · DoP 176.4 kHz", "DSF"),
        ("C:/Music/Radiohead - Everything In Its Right Place.wav", "Everything In Its Right Place", "Radiohead", "Kid A", 251.0, "16-bit · 44.1 kHz", "WAV"),
    ]
    for path, title, artist, album, duration, resolution, container in samples:
        playlist.add(
            Track(
                path=path,
                title=title,
                artist=artist,
                album=album,
                duration=duration,
                resolution=resolution,
                container=container,
                codec="PCM",
                size_bytes=42_000_000,
                is_dsd=container == "DSF",
            )
        )
    return playlist


def main() -> int:
    output_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tests" / "artifacts"
    output_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(verbose=False, log_to_file=False)

    app = QApplication(sys.argv[:1])
    apply_theme(app)

    with tempfile.TemporaryDirectory() as directory:
        settings = SettingsStore(Path(directory) / "settings.json")
        settings.update(
            window_width=int(os.environ.get("TRUEHQ_SMOKE_WIDTH", "1440")),
            window_height=int(os.environ.get("TRUEHQ_SMOKE_HEIGHT", "900")),
        )
        engine = AudioEngine(DeviceManager(), TelemetryHub(), EventBus(), EngineConfig(exclusive=True))
        playlist = make_tracks()
        controller = PlaybackController(engine, engine.bus, playlist)
        library = Library([str(Path.home())])
        bridge = QtBridge(engine.bus)
        window = MainWindow(engine, controller, library, settings, bridge)
        window.show()
        window.restore_state()
        app.processEvents()
        print(f"window geometry: {window.width()}x{window.height()} (requested {os.environ.get('TRUEHQ_SMOKE_WIDTH', '1440')}x{os.environ.get('TRUEHQ_SMOKE_HEIGHT', '900')})")

        now_playing = window.now_playing
        now_playing.set_track(
            {
                "title": "Journey in Satchidananda",
                "artist": "Alice Coltrane",
                "album": "Journey in Satchidananda",
                "resolution": "24-bit · 96 kHz",
                "container": "FLAC",
                "codec": "FLAC lossless",
                "duration": 381.0,
                "notes": ["decoded with libsndfile 1.2.2"],
            }
        )
        now_playing.set_position(126.412)
        now_playing.set_playback_state("playing")
        window.badge.set_state("active", "bit-exact path to the DAC buffer")
        window.playlist_view.set_current(0)
        window._on_playlist_changed({"count": len(playlist), "current": 0})
        window.footer.update_from(
            engine.snapshot().__class__(
                state="playing",
                track_path="C:/Music/Alice Coltrane - Journey in Satchidananda.flac",
                source_rate=96000,
                source_bits=24,
                hardware_clock=96000,
                hardware_bits=24,
                hardware_format="24-bit / 96 kHz",
                buffer_ms=18.4,
                buffer_fill=0.61,
                buffer_capacity_ms=1200.0,
                latency_ms=24.6,
                xruns=0,

                integrity_digest="9f2c41d8a7b03c55",
                priority="process=HIGH_PRIORITY_CLASS · mmcss=on · thread=TIME_CRITICAL · timer=1ms",
                priority_realtime=True,
                driver_label="WASAPI Exclusive",
                device_name="Topping E30 II",
                position_seconds=126.412,
                duration_seconds=381.0,
            )
        )
        window.telemetry_panel.update_from(
            engine.snapshot().__class__(
                state="playing",
                source_rate=96000,
                source_bits=24,
                hardware_clock=96000,
                hardware_bits=24,
                buffer_ms=18.4,
                buffer_fill=0.61,
                buffer_capacity_ms=1200.0,
                latency_ms=24.6,
                xruns=0,
                integrity_digest="9f2c41d8a7b03c5531",
                integrity_bytes_sent=87_000_000,
                conversion_chain="24-bit / 96 kHz -> 24-bit / 96 kHz",
                driver_label="WASAPI Exclusive",
                device_name="Topping E30 II",
                priority="process=HIGH_PRIORITY_CLASS · mmcss=on",
                priority_realtime=True,
                bit_perfect=True,
            ),
            [0.45 + 0.1 * ((index * 7) % 11) / 11 for index in range(240)],
        )
        window.session_panel.update_from(engine.snapshot(), engine)

        shots = []

        def shoot(name: str):
            def _capture() -> None:
                try:
                    pixmap = window.grab()
                    target = output_dir / f"{name}.png"
                    pixmap.save(str(target), "PNG")
                    shots.append(f"{target} ({pixmap.width()}x{pixmap.height()})")
                except Exception:
                    traceback.print_exc()

            return _capture

        def show_hardware() -> None:
            window.tabs.setCurrentIndex(1)
            QTimer.singleShot(200, window.device_panel.probe_now)

        def show_session() -> None:
            window.tabs.setCurrentIndex(2)

        def finish() -> None:
            app.quit()

        QTimer.singleShot(700, shoot("01_overview"))
        QTimer.singleShot(900, show_hardware)
        QTimer.singleShot(2600, shoot("02_hardware"))
        QTimer.singleShot(2700, show_session)
        QTimer.singleShot(3100, shoot("03_session"))
        QTimer.singleShot(3300, finish)
        code = app.exec()
        engine.shutdown(timeout=2.0)
        for shot in shots:
            print("saved", shot)
        return code


if __name__ == "__main__":
    raise SystemExit(main())
