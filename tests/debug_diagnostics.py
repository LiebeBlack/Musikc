import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import setup_logging

setup_logging(verbose=True, log_to_file=False)

try:
    from src.app import collect_diagnostics

    print(collect_diagnostics())
except Exception:
    traceback.print_exc()
