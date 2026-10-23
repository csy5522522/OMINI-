"""Plain TXT storage. Edits are atomic and detect changes made by other editors."""
from __future__ import annotations

import hashlib
import hmac
import base64
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
from memo_store import MemoStore
from memo_crypto import PREFIX, MemoCipher, atomic_bytes


MAX_NOTE_BYTES = 16 * 1024 * 1024


def read_text_file(path: Path) -> tuple[str, str]:
    if path.stat().st_size > MAX_NOTE_BYTES:
        raise ValueError("此 TXT 超过 16 MiB，请使用外部编辑器打开。")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if raw.startswith(PREFIX):
        directory = path.parent.parent if path.parent.name in {".trash", ".migration-backup"} else path.parent
        cipher = MemoCipher(directory / ".omini-memos.sqlite3.key", protected=True, existing=True)
        value = json.loads(cipher.decrypt(raw, "local-mirror"))
        return value["content"], digest
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
        self.memos = MemoStore(self.directory / ".omini-memos.sqlite3",
                               existing_files=any(self.directory.rglob("*.ominote")))
        self.owner = "local"
        self._titles = {}
        self._migrate_files()

    def _write(self, path, title, content):
        payload = json.dumps({"title": title, "content": content}, ensure_ascii=False).encode("utf-8")
        atomic_bytes(path, self.memos.cipher.encrypt(payload, "local-mirror"))

    def _title(self, path):
        raw = path.read_bytes()
        if raw.startswith(PREFIX):
            return json.loads(self.memos.cipher.decrypt(raw, "local-mirror"))["title"]
        return path.stem

    def _migrate_files(self):
        for folder in (self.directory, self.directory / ".trash"):
            if not folder.exists(): continue
            for path in list(folder.glob("*.txt")):
                text, _ = read_text_file(path)
                title = self._title(path)
                # Recovery copy is encrypted before the original mirror is replaced.
                recovery = self.directory / ".migration-backup" / (uuid.uuid4().hex + ".enc")
                raw = path.read_bytes()
                atomic_bytes(recovery, self.memos.cipher.encrypt(json.dumps({"name": path.name,
                    "raw": base64.b64encode(raw).decode()}, ensure_ascii=False).encode(), "file-migration-backup"))
                if folder.name == ".trash":
                    destination = folder / (uuid.uuid4().hex + ".ominote")
                    self._write(destination, title, text)
                else:
                    migrated = self.memos.get_data(self.owner, "migrated_files", {})
                    memo_id = migrated.get(path.name)
                    memo = self.memos.get(self.owner, memo_id) if memo_id else self.for_path(path)
                    destination = self.target(title)
                    mapping = self.mapping()
                    occupied_by_other = any(key != memo["id"] and name == destination.name for key, name in mapping.items())
                    if destination.exists() and (occupied_by_other or read_text_file(destination)[0] != text):
                        destination = self.target(title + " " + memo["id"][:8])
                    self._write(destination, title, text)
                    mapping[memo["id"]] = destination.name
                    self.memos.set_data(self.owner, "paths", mapping)
                    migrated[path.name] = memo["id"]
                    self.memos.set_data(self.owner, "migrated_files", migrated)
                path.unlink()


    def mapping(self):
        return self.memos.get_data(self.owner, "paths", {})

    def record(self, path, action="edit", deleted=False):
        mapping = self.mapping()
        memo_id = next((key for key, name in mapping.items() if name == path.name), None)
        if memo_id:
            old = self.memos.get(self.owner, memo_id)
        else:
            memo_id = uuid.uuid4().hex; old = {"version": 0}
        text, _ = read_text_file(path)
        memo = self.memos.save(self.owner, {"id": memo_id, "title": self._title(path), "content": text,
            "base_version": old["version"], "deleted": deleted}, action=action, device="desktop")
        mapping[memo_id] = path.name
        self.memos.set_data(self.owner, "paths", mapping)
        return memo

    def for_path(self, path):
        mapping = self.mapping()
        memo_id = next((key for key, name in mapping.items() if name == path.name), None)
        return self.memos.get(self.owner, memo_id) if memo_id else self.record(path, "import")

    def write_snapshot(self, value, action="sync"):
        mapping = self.mapping()
        path = self.directory / mapping[value["id"]] if value["id"] in mapping else self.target(value["title"])
        desired = self.target(value["title"])
        if value["id"] in mapping and desired != path and not desired.exists():
            if path.exists(): path.rename(desired)
            path = desired
        if value["id"] not in mapping:
            occupied = set(mapping.values())
            if path.name in occupied or path.exists(): path = self.target(value["title"] + " " + value["id"][:8])
        try: old = self.memos.get(self.owner, value["id"])
        except ValueError: old = {"version": 0}
        result = self.memos.save(self.owner, {**value, "deleted": bool(value["deleted"]), "base_version": old["version"]}, action=action, device="sync")
        if value["deleted"]:
            if path.exists():
                trash = self.directory / ".trash"; trash.mkdir(exist_ok=True)
                path.rename(trash / (uuid.uuid4().hex + ".ominote"))
        else: self._write(path, value["title"], value["content"])
        if value["deleted"]: mapping.pop(value["id"], None)
        else: mapping[value["id"]] = path.name
        self.memos.set_data(self.owner, "paths", mapping)
        return result, path

    def files(self) -> list[Path]:
        return sorted((p for p in self.directory.iterdir() if p.is_file() and p.suffix.lower() == ".ominote"),
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
        token = hmac.new(self.memos.cipher.key, name[:160].encode("utf-8"), hashlib.sha256).hexdigest()
        path = self.directory / (token + ".ominote")
        self._titles[path.name] = name[:160]
        return path

    def create(self, title: str, text: str = "") -> Path:
        path = self.target(title)
        # Exclusive create also guards against collisions with an external editor.
        payload = self.memos.cipher.encrypt(json.dumps({"title": self._titles[path.name], "content": text}, ensure_ascii=False).encode(), "local-mirror")
        with path.open("xb") as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        self.record(path, "create")
        return path

    def save(self, path: Path, text: str, expected_digest: str) -> str:
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected_digest:
            raise ValueError("文件已被其他程序修改或移动。请先「另存为」保留编辑，或「重新读取」加载磁盘内容。")
        self.for_path(path)
        self._write(path, self._title(path), text)
        self.record(path)
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def trash(self, path: Path) -> Path:
        memo = self.record(path, "delete", deleted=True)
        directory = self.directory / ".trash"
        directory.mkdir(exist_ok=True)
        destination = directory / f"{uuid.uuid4().hex}.ominote"
        path.rename(destination)
        mapping = self.mapping(); mapping.pop(memo["id"], None)
        self.memos.set_data(self.owner, "paths", mapping)
        return destination

    def rename(self, path, target):
        memo = self.for_path(path)
        self._write(path, self._titles.get(target.name, self._title(path)), read_text_file(path)[0])
        path.rename(target)
        mapping = self.mapping(); mapping[memo["id"]] = target.name
        self.memos.set_data(self.owner, "paths", mapping)
        self.record(target, "rename")
