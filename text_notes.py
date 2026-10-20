"""Plain TXT storage. Edits are atomic and detect changes made by other editors."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import tempfile
import uuid


MAX_NOTE_BYTES = 16 * 1024 * 1024


def read_text_file(path: Path) -> tuple[str, str]:
    if path.stat().st_size > MAX_NOTE_BYTES:
        raise ValueError("此 TXT 超过 16 MiB，请使用外部编辑器打开。")
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("gb18030")
    if "\x00" in text:
        raise ValueError("文件包含二进制内容，请选择纯文本 TXT。")
    return text, hashlib.sha256(raw).hexdigest()


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".omini-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class TextNotesStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)

    def files(self) -> list[Path]:
        return sorted((p for p in self.directory.iterdir() if p.is_file() and p.suffix.lower() == ".txt"),
                      key=lambda p: p.name.casefold())

    def target(self, title: str) -> Path:
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")
        if name.lower().endswith(".txt"):
            name = name[:-4].rstrip(" .")
        if not name:
            raise ValueError("请输入文件名。")
        if name.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)],
                                           *[f"LPT{i}" for i in range(10)]}:
            name = "_" + name
        return self.directory / (name[:160] + ".txt")

    def create(self, title: str, text: str = "") -> Path:
        path = self.target(title)
        # Exclusive create also guards against collisions with an external editor.
        with path.open("x", encoding="utf-8", newline="") as stream:
            stream.write(text)
        return path

    def save(self, path: Path, text: str, expected_digest: str) -> str:
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected_digest:
            raise ValueError("文件已被其他程序修改或移动。请先「另存为」保留编辑，或「重新读取」加载磁盘内容。")
        atomic_text(path, text)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def trash(self, path: Path) -> Path:
        directory = self.directory / ".trash"
        directory.mkdir(exist_ok=True)
        destination = directory / f"{path.stem}-{uuid.uuid4().hex[:8]}.txt"
        path.rename(destination)
        return destination
