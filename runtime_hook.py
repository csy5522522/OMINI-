"""PyInstaller hook: make bundled Qt and its dependent DLLs discoverable on Windows."""
import os
import sys
from pathlib import Path


if sys.platform == "win32" and hasattr(sys, "_MEIPASS"):
    root = Path(sys._MEIPASS)
    directories = [root, root / "PySide6", root / "shiboken6"]
    for directory in directories:
        if directory.is_dir():
            os.add_dll_directory(str(directory))
    os.environ["PATH"] = os.pathsep.join([str(path) for path in directories if path.is_dir()] + [os.environ.get("PATH", "")])
