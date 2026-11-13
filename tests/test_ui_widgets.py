"""Headless widget tests: models, delegates, dialogs and custom painting.

Qt runs with the ``offscreen`` platform plugin, so these tests are safe on CI
machines without a desktop.  Rendering into a QPixmap exercises every paintEvent
and therefore catches QPainter misuse that a simple import would not.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from src.config import SettingsStore
from src.core.audio_engine import EngineConfig
from src.core.library import Library, LibraryEntry
from src.core.playlist import Playlist, RepeatMode, Track
from src.core.telemetry import TelemetrySnapshot
from src.ui.main_window import PreferencesDialog
from src.ui.widgets.badge import BitPerfectBadge
from src.ui.widgets.library_view import LibraryTableModel, LibraryView
from src.ui.widgets.playlist_view import MIME_TYPE, PlaylistTableModel, PlaylistView
from src.ui.widgets.seekbar import SeekBar
from src.ui.widgets.telemetry_panel import TelemetryPanel


@pytest.fixture(scope="module")
def app(qapp: QApplication) -> QApplication:
    return qapp


def make_playlist() -> Playlist:
    playlist = Playlist("UI")
    for index in range(3):
        playlist.add(Track(path=f"C:/music/{index}.flac", title=f"Track {index}", artist=f"Artist {index}", duration=90.0))
    return playlist


def render(widget) -> QPixmap:
    widget.resize(max(320, widget.sizeHint().width()), max(120, widget.sizeHint().height()))
    pixmap = QPixmap(widget.size())
    pixmap.fill(Qt.GlobalColor.transparent)
    widget.render(pixmap)
    return pixmap


def test_playlist_model_drag_and_drop(app: QApplication) -> None:
    playlist = make_playlist()
    model = PlaylistTableModel(playlist)
    assert model.rowCount() == 3
    assert model.columnCount() == 5
    mimedata = model.mimeData([model.index(0, 0), model.index(0, 1)])
    assert mimedata.hasFormat(MIME_TYPE)
    assert model.dropMimeData(mimedata, Qt.DropAction.MoveAction, 3, 0, model.index(-1, -1))
    assert [track.title for track in playlist] == ["Track 1", "Track 2", "Track 0"]


def test_playlist_view_renders(app: QApplication) -> None:
    view = PlaylistView(make_playlist())
    view.resize(760, 320)
    view.set_current(1)
    pixmap = QPixmap(view.size())
    view.render(pixmap)
    assert not pixmap.isNull()


def test_badge_states_render(app: QApplication) -> None:
    badge = BitPerfectBadge()
    for state in ("active", "dop", "degraded", "off", "error"):
        badge.set_state(state, f"state {state}")
        pixmap = render(badge)
        assert pixmap.width() >= 170


def test_seekbar_interaction(app: QApplication) -> None:
    bar = SeekBar()
    bar.resize(600, 34)
    bar.set_duration(300.0)
    bar.set_position(120.0)
    assert bar.position == pytest.approx(120.0)
    captured = {}
    bar.seekRequested.connect(lambda value: captured.setdefault("value", value))
    bar.keyPressEvent(_key_event(Qt.Key.Key_Right))
    assert captured["value"] == pytest.approx(125.0)
    render(bar)


def _key_event(key):
    from PySide6.QtGui import QKeyEvent

    return QKeyEvent(QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)


def test_telemetry_panel_update(app: QApplication) -> None:
    panel = TelemetryPanel()
    panel.resize(520, 700)
    panel.update_from(
        TelemetrySnapshot(
            state="playing",
            hardware_clock=192000,
            hardware_bits=24,
            source_rate=192000,
            source_bits=24,
            buffer_ms=18.0,
            buffer_fill=0.62,
            buffer_capacity_ms=1200.0,
            latency_ms=24.0,
            xruns=0,
            integrity_digest="abcdef0123456789",
            integrity_bytes_sent=1_000_000,
            conversion_chain="24-bit / 192 kHz -> 24-bit / 192 kHz",
            driver_label="WASAPI Exclusive",
            device_name="Test DAC",
            priority="mmcss",
            priority_realtime=True,
            bit_perfect=True,
        ),
        [0.4, 0.5, 0.6, 0.55, 0.7],
    )
    pixmap = QPixmap(panel.size())
    panel.render(pixmap)
    assert panel.buffer_state.text() == "ÓPTIMO"
    assert panel.xrun_row._value.text().startswith("0")


def test_library_model_and_view(app: QApplication) -> None:
    library = Library([])
    entries = [
        LibraryEntry(path=f"C:/music/{index}.flac", title=f"Song {index}", artist="Artist", album="Album", duration=100.0, container="FLAC")
        for index in range(5)
    ]
    model = LibraryTableModel()
    model.set_entries(entries)
    assert model.rowCount() == 5
    assert model.data(model.index(0, 0)) == "Song 0"
    view = LibraryView(library)
    view.model.set_entries(entries)
    view.resize(720, 400)
    render(view)


def test_preferences_dialog(app: QApplication, tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    dialog = PreferencesDialog(store, EngineConfig(buffer_ms=32, ring_ms=900, period_frames=256), None)
    values = dialog.values()
    assert values["buffer_ms"] == 32
    assert values["ring_ms"] == 900
    assert values["period_frames"] == 256
    assert values["allow_degraded"] is False
    dialog.deleteLater()


def test_playlist_repeat_cycle(app: QApplication) -> None:
    playlist = make_playlist()
    assert playlist.cycle_repeat() is RepeatMode.ALL
    assert playlist.cycle_repeat() is RepeatMode.ONE
    assert playlist.cycle_repeat() is RepeatMode.OFF
