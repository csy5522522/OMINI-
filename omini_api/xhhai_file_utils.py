from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


def write_json_atomic(path: Path, data: Any, *, trailing_newline: bool = False) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if trailing_newline:
        text += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    temp_path.write_text(text, encoding="utf-8")
    os.replace(temp_path, path)
