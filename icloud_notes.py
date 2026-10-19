"""Offline Notes snapshots from user-selected HAR responses; no HTTP or session replay."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import urlsplit
import zlib

from campus_network import protect_password, unprotect_password

NOTES_URL = "https://www.icloud.com.cn/notes/"
MAX_BODY = 32 * 1024 * 1024


def _bytes(value):
    return base64.b64decode(value, validate=True)


def _text(fields, name):
    value = fields.get(name, {}).get("value", "")
    if not isinstance(value, str):
        return ""
    if fields.get(name, {}).get("type") in ("BYTES", "ENCRYPTED_BYTES"):
        try:
            value = _bytes(value).decode("utf-8")
        except (ValueError, UnicodeError):
            return ""
    return value if all(c.isprintable() or c.isspace() for c in value) else ""


def _varint(data, pos):
    value = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            break
        byte = data[pos]; pos += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, pos
    raise ValueError("正文编码不完整")


def _protobuf(data):
    pos = 0
    while pos < len(data):
        tag, pos = _varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if not number:
            raise ValueError("正文标签无效")
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire == 2:
            size, pos = _varint(data, pos)
            value = data[pos:pos + size]; pos += size
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            value = data[pos:pos + size]; pos += size
        else:
            raise ValueError("正文格式尚未支持")
        if pos > len(data):
            raise ValueError("正文编码不完整")
        yield number, value


def decode_note_body(value):
    """Notes uses zlib/gzip compressed NoteStore protobuf (document.note.text)."""
    data = _bytes(value)
    if len(data) > MAX_BODY:
        raise ValueError("正文超过大小上限")
    if data.startswith(b"\x1f\x8b") or data[:1] == b"x":
        decoder = zlib.decompressobj(47)
        data = decoder.decompress(data, MAX_BODY + 1)
        if len(data) > MAX_BODY or not decoder.eof:
            raise ValueError("正文压缩数据不完整或过大")
    for field_number in (2, 3, 2):
        data = next((value for number, value in _protobuf(data)
                     if number == field_number and isinstance(value, bytes)), None)
        if data is None:
            raise ValueError("HAR 未包含完整正文")
    return data.decode("utf-8")


def import_notes_har(path):
    path = Path(path)
    if path.stat().st_size > 256 * 1024 * 1024:
        raise ValueError("HAR 超过 256 MB，请仅导出备忘录请求")
    try:
        entries = json.loads(path.read_text(encoding="utf-8-sig"))["log"]["entries"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("请选择有效的 HAR 文件") from error
    records = {}
    for entry in entries:
        url = urlsplit(entry.get("request", {}).get("url", ""))
        host = (url.hostname or "").lower()
        if not (host.endswith("-ckdatabasews.icloud.com.cn") or host.endswith("-ckdatabasews.icloud.com")):
            continue
        if not url.path.startswith("/database/1/com.apple.notes/"):
            continue
        if entry.get("response", {}).get("status") != 200:
            continue
        try:
            content = entry["response"]["content"]
            raw = content.get("text", "")
            if content.get("encoding") == "base64":
                raw = _bytes(raw).decode("utf-8")
            response = json.loads(raw)
            batch = list(response.get("records", []))
            for zone in response.get("zones", []):
                batch.extend(zone.get("records", []))
            for record in batch:
                key = record.get("recordName")
                if not key or "serverErrorCode" in record:
                    continue
                previous = records.get(key, {})
                # Change responses can contain only changed fields. Preserve earlier body.
                records[key] = {**previous, **record, "fields": {
                    **previous.get("fields", {}), **record.get("fields", {})}}
        except (KeyError, TypeError, ValueError, UnicodeError):
            continue
    folders = {}
    pins = {}
    for key, record in records.items():
        fields = record.get("fields", {})
        kind = record.get("recordType", "")
        if kind == "Folder":
            folders[key] = _text(fields, "TitleEncrypted") or _text(fields, "Title") or "备忘录"
        if kind in ("Note_UserSpecific", "PasswordProtectedNote_UserSpecific"):
            note = fields.get("Note", {}).get("value", {}).get("recordName")
            if note:
                pins[note] = bool(fields.get("IsPinned", {}).get("value", 0))
    notes = []
    for key, record in records.items():
        kind = record.get("recordType")
        if kind not in ("Note", "PasswordProtectedNote") or record.get("deleted"):
            continue
        fields = record.get("fields", {})
        locked = kind == "PasswordProtectedNote"
        title = _text(fields, "TitleEncrypted") or _text(fields, "Title") or "未命名备忘录"
        snippet = _text(fields, "SnippetEncrypted") or _text(fields, "Snippet")
        body = ""
        complete = False
        if not locked:
            try:
                body = decode_note_body(fields.get("TextDataEncrypted", {}).get("value", ""))
                complete = True
            except (ValueError, UnicodeError, zlib.error, StopIteration):
                body = snippet
        folder_id = fields.get("Folder", {}).get("value", {}).get("recordName", "")
        stamp = fields.get("ModificationDate", {}).get("value") or record.get("modified", {}).get("timestamp", 0)
        try:
            modified = datetime.fromtimestamp(float(stamp) / 1000, timezone.utc).isoformat()
        except (ValueError, TypeError, OverflowError, OSError):
            modified = ""
        notes.append(dict(id="icloud:" + key, title=title, body=body, snippet=snippet,
                          folder=folders.get(folder_id, "备忘录"), modified=modified,
                          pinned=pins.get(key, False), locked=locked, complete=complete,
                          deleted=bool(fields.get("Deleted", {}).get("value", 0)), edited=False))
    if not notes:
        raise ValueError("HAR 中没有备忘录记录。请在官网打开备忘录并浏览正文后重新导出。")
    return {"notes": notes, "folders": sorted(set(folders.values()) | {n["folder"] for n in notes})}


class NotesStore:
    """Atomic Windows DPAPI cache. Personal contents stay outside the release directory."""
    def __init__(self, path):
        self.path = Path(path)
        self.data = {"notes": [], "folders": ["备忘录"]}
        self.error = ""
        if self.path.exists():
            try:
                envelope = json.loads(self.path.read_text(encoding="utf-8"))
                data = json.loads(unprotect_password(envelope["protected"]))
                if not isinstance(data.get("notes"), list) or not isinstance(data.get("folders"), list):
                    raise ValueError()
                self.data = data
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                self.error = "本地副本读取失败。请使用原 Windows 账号打开，或备份缓存后重新导入。"

    def save(self):
        if self.error:
            raise ValueError(self.error)
        protected = protect_password(json.dumps(self.data, ensure_ascii=False))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 1, "protected": protected}), encoding="utf-8")
        temporary.replace(self.path)

    def merge(self, snapshot):
        previous = self.data
        notes = {n["id"]: dict(n) for n in previous["notes"]}
        imported = kept = 0
        for note in snapshot["notes"]:
            existing = notes.get(note["id"], {})
            if existing.get("edited"):
                kept += 1
                continue
            incoming = dict(note)
            if existing.get("complete") and not incoming.get("complete") and not incoming.get("locked"):
                incoming.update(body=existing["body"], complete=True)
            notes[note["id"]] = incoming; imported += 1
        self.data = {"notes": list(notes.values()), "folders": sorted(
            set(previous["folders"]) | set(snapshot["folders"]))}
        try:
            self.save()
        except Exception:
            self.data = previous
            raise
        return imported, kept
