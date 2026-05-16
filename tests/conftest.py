"""Make `src/` importable so tests can `import metrics_calculator` etc.

pytest auto-discovers this file when running from the repo root.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
