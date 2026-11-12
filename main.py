"""True HQ Audio — entry point.

Usage
-----
    python main.py                 launch the player
    python main.py --diagnostics   print the audio environment and exit
    python main.py --verbose       debug logging
    python main.py --realtime      request REALTIME_PRIORITY_CLASS (opt-in)
"""

from __future__ import annotations

import multiprocessing
import os
import sys
from pathlib import Path


def _ensure_source_tree_on_path() -> None:
    if getattr(sys, "frozen", False):
        return
    root = Path(__file__).resolve().parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def main() -> int:
    _ensure_source_tree_on_path()
    multiprocessing.freeze_support()
    from src.app import run

    return run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
