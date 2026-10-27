"""Test suite for the Omini toolbox."""
from pathlib import Path
import sys

_root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_root / "src"), str(_root / "extras"), str(_root)]
