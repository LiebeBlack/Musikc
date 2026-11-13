"""Single Qt application for the whole test session.

Every UI module used to build its own ``QApplication`` fixture. When the first
module was torn down, dropping the last Python reference deleted the C++
application while pytest still held windows from the other modules: Qt warned
``shared QObject was deleted directly`` and the interpreter crashed on exit
(SIGBUS on Linux CI). One session-wide application, kept alive in a module
global and torn down after every widget, removes the ordering hazard.

Qt is optional on purpose: the non-UI tests keep running on a machine where
PySide6 cannot be imported.
"""

from __future__ import annotations

import gc
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

try:  # pragma: no cover - exercised only when Qt is missing
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    QCoreApplication = QEvent = QApplication = None  # type: ignore[assignment]

if QApplication is not None:  # pragma: no branch
    from src.ui.theme import apply_theme

    _APPLICATION = QApplication.instance() or QApplication([])
    apply_theme(_APPLICATION)
else:  # pragma: no cover
    _APPLICATION = None


@pytest.fixture(scope="session")
def qapp():
    """The process-wide application; never destroyed by a fixture."""

    if _APPLICATION is None:  # pragma: no cover
        pytest.skip("PySide6 is not installed")
    return _APPLICATION


@pytest.fixture(scope="session", autouse=True)
def _destroy_widgets_before_exit():
    yield
    if _APPLICATION is None:  # pragma: no cover
        return
    # Qt GUI objects must disappear while the application is still valid: the
    # order is what used to crash the interpreter at shutdown.
    from src.ui.main_window import clear_icon_cache

    clear_icon_cache()
    for widget in list(_APPLICATION.topLevelWidgets()):
        widget.close()
        widget.deleteLater()
    # deleteLater() only queues the deletion: without this flush the windows
    # are still alive when the interpreter destroys the application.
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    _APPLICATION.processEvents()
    gc.collect()
