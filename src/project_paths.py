"""Shared project resources for source and frozen application entry points."""
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if not getattr(sys, "frozen", False) and str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))
