"""Regression checks for responsive layout and interactive control dimensions."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSize
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import SettingsStore
from src.core.audio_engine import AudioEngine, EngineConfig
from src.core.devices import DeviceManager
from src.core.events import EventBus
from src.core.library import Library
from src.core.playlist import PlaybackController, Playlist
from src.core.telemetry import TelemetryHub
from src.ui.main_window import MainWindow
from src.ui.qt_bridge import QtBridge
from src.ui.theme import apply_theme, build_stylesheet
from src.ui.widgets.base import IconButton
from src.ui.widgets.now_playing import NowPlayingCard


@pytest.fixture(scope="module")
def app() -> QApplication:
    application = QApplication.instance() or QApplication([])
    apply_theme(application)
    return application


def make_window(tmp_path: Path) -> MainWindow:
    engine = AudioEngine(DeviceManager(), TelemetryHub(), EventBus(), EngineConfig())
    controller = PlaybackController(engine, engine.bus, Playlist())
    settings = SettingsStore(tmp_path / "settings.json")
    library = Library([])
    window = MainWindow(engine, controller, library, settings, QtBridge(engine.bus))
    window._poll_timer.stop()
    return window


def test_icon_buttons_meet_accessible_touch_targets(app: QApplication):
    for size in (30, 34, 36, 38, 40, 56):
        button = IconButton("play", "Play", size=size)
        assert button.sizeHint().width() == size
        assert button.sizeHint().height() == size
        assert button.toolTip() == "Play"


def test_transport_controls_keep_expected_hierarchy(app: QApplication):
    now = NowPlayingCard()
    assert now.transport.play_button.width() > now.transport.previous_button.width()
    assert now.transport.play_button.height() > now.transport.previous_button.height()
    assert now.seek_bar.minimumHeight() >= 32
    assert not now.transport.play_button.isEnabled()
    now.set_track({"title": "A", "artist": "B", "duration": 10.0})
    assert now.transport.play_button.isEnabled()
    assert now.transport.shuffle_button.isEnabled()
    now.clear_track()
    assert not now.transport.play_button.isEnabled()
    assert not now.seek_bar._enabled


def test_window_minimum_covers_panel_requirements(app: QApplication, tmp_path: Path):
    window = make_window(tmp_path)
    window.show()
    app.processEvents()
    available = window.screen().availableGeometry()
    # The floor never exceeds the desktop, so a scaled laptop keeps it on screen.
    assert window.minimumWidth() <= available.width()
    assert window.minimumHeight() <= available.height()
    assert window.minimumWidth() >= 320
    window.resize(window.minimumWidth(), window.minimumHeight())
    app.processEvents()
    assert window.width() >= window.minimumWidth()
    # Neither panel may be squeezed below the width its own controls require.
    assert window.left_panel.width() >= window.left_panel.minimumWidth()
    assert window.right_panel.width() >= window.right_panel.minimumWidth()
    assert window.queue_clear_button.isVisible()
    window.close()
    app.processEvents()


def test_compact_playlist_hides_lower_priority_columns(app: QApplication):
    from src.ui.widgets.playlist_view import PlaylistView

    view = PlaylistView(Playlist(), toolbar=False)
    view.resize(480, 200)
    view.show()
    app.processEvents()
    assert view.table.isColumnHidden(2)
    assert view.table.isColumnHidden(3)
    view.resize(800, 320)
    app.processEvents()
    assert not view.table.isColumnHidden(2)
    assert not view.table.isColumnHidden(3)
    view.close()


def test_table_columns_never_clip_the_duration_header(app: QApplication):
    from src.ui.widgets.library_view import LibraryView
    from src.ui.widgets.playlist_view import PlaylistView

    queue = PlaylistView(Playlist(), toolbar=False)
    library = LibraryView(Library([]))
    for view in (queue, library):
        view.resize(900, 320)
        view.show()
        app.processEvents()
        table = view.table
        header = table.horizontalHeader()
        for column in (3, 4):
            if table.isColumnHidden(column):
                continue
            assert table.columnWidth(column) >= header.sectionSizeHint(column), (
                type(view).__name__,
                column,
            )
        assert table.horizontalScrollBar().maximum() == 0
        view.close()
    app.processEvents()


def test_window_minimum_is_capped_to_the_available_desktop(app: QApplication, tmp_path: Path):
    window = make_window(tmp_path)
    ideal = window._ideal_minimum_size()
    left_need, right_need = window._panel_minimums

    small = QSize(1024, 600)
    assert window._minimum_for(small) == QSize(min(ideal.width(), 1024), min(ideal.height(), 600))
    window._apply_minimum_size(small)
    assert window.minimumWidth() <= 1024
    assert window.minimumHeight() <= 600
    # The splitter gives the space back instead of pushing the window off-screen.
    assert window.left_panel.minimumWidth() <= left_need
    assert window.right_panel.minimumWidth() <= right_need
    assert window.left_panel.minimumWidth() + window.right_panel.minimumWidth() < left_need + right_need

    window._apply_minimum_size(QSize(3840, 2160))
    assert window.minimumWidth() == ideal.width()
    assert window.left_panel.minimumWidth() == left_need
    assert window.right_panel.minimumWidth() == right_need
    window.close()
    app.processEvents()


def test_session_policy_chips_wrap_instead_of_disappearing(app: QApplication):
    from src.ui.main_window import SessionPanel

    panel = SessionPanel()
    panel.show()
    app.processEvents()
    row = panel.chip_row
    assert row.chip_count() == 5

    panel.resize(1100, 600)
    app.processEvents()
    single_row = row.minimumHeight()
    panel.resize(320, 600)
    app.processEvents()
    wrapped = row.minimumHeight()
    assert single_row > 0
    assert wrapped > single_row
    panel.close()
    app.processEvents()


def test_redundant_album_is_hidden(app: QApplication):
    now = NowPlayingCard()
    now.set_track({
        "title": "Example title",
        "artist": "Example artist",
        "album": "Example title",
        "resolution": "24-bit · 96 kHz",
        "container": "FLAC",
        "codec": "FLAC lossless",
        "duration": 120.0,
        "notes": [],
    })
    assert now.album_label.isHidden()
    assert now.spec_label.isHidden()
    now.set_track({
        "title": "Example title",
        "artist": "Example artist",
        "album": "Different album",
        "resolution": "24-bit · 96 kHz",
        "container": "FLAC",
        "codec": "FLAC lossless",
        "duration": 120.0,
        "notes": ["unusual source metadata"],
    })
    assert not now.album_label.isHidden()
    assert not now.spec_label.isHidden()


def test_seekbar_accessibility_and_sanitized_ranges(app: QApplication):
    from src.ui.widgets.seekbar import SeekBar

    bar = SeekBar()
    bar.set_duration(float("nan"))
    assert bar.duration == 0.0
    bar.set_duration(60.0)
    bar.set_position(float("inf"))
    assert bar.position == 0.0
    bar.set_enabled(True)
    assert bar.accessibleName() == "Posición de reproducción"
    assert "flechas izquierda y derecha" in bar.accessibleDescription()
    bar.set_enabled(False)
    assert "No hay ninguna pista" in bar.accessibleDescription()


def test_generated_stylesheet_uses_compact_button_metrics(app: QApplication):
    stylesheet = build_stylesheet()
    assert "min-height: 26px;" in stylesheet
    assert "padding: 6px 12px;" in stylesheet
