"""Durable, Windows-user-encrypted state, history and credential backups."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

from campus_network import protect_password, unprotect_password


def atomic_bytes(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def encode_protected(value: dict) -> bytes:
    envelope = {"version": 1, "protected": protect_password(json.dumps(value, ensure_ascii=False))}
    return json.dumps(envelope, ensure_ascii=True).encode("utf-8")


def decode_protected(content: bytes) -> dict:
    envelope = json.loads(content.decode("utf-8"))
    value = json.loads(unprotect_password(envelope["protected"]))
    if not isinstance(value, dict):
        raise ValueError("保存的数据格式有误。")
    return value


READ_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError)


def settings_fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def read_protected(path: Path, default: dict, validate=None) -> tuple[dict, str]:
    backup = path.with_name(path.name + ".bak")
    for candidate in (path, backup):
        if not candidate.exists():
            continue
        try:
            value = decode_protected(candidate.read_bytes())
            if validate is not None and not validate(value):
                raise ValueError("保存的数据结构有误。")
            return value, ("已从自动备份恢复 " + path.name + "。" if candidate == backup else "")
        except READ_ERRORS:
            continue
    if path.exists() or backup.exists():
        raise ValueError(path.name + " 读取失败，原文件与备份已保留，请在操作记录页打开数据目录检查。")
    return copy.deepcopy(default), ""


def write_protected(path: Path, value: dict, *, recovered=False):
    content = encode_protected(value)
    backup = path.with_name(path.name + ".bak")
    previous = None
    if path.exists():
        previous = path.read_bytes()
        try:
            if recovered:
                raise ValueError("恢复时保留原有有效备份。")
            decode_protected(previous)
        except READ_ERRORS:
            # Keep the exact damaged file before recovering from the good backup.
            digest = hashlib.sha256(previous).hexdigest()[:16]
            recovery = path.parent / "recovery" / (path.name + "." + digest + ".corrupt")
            if not recovery.exists():
                atomic_bytes(recovery, previous)
            previous = None
    if previous is not None:
        atomic_bytes(backup, previous)
    atomic_bytes(path, content)
    if not backup.exists():
        atomic_bytes(backup, content)


class AssistantState:
    def __init__(self, root: Path):
        self.path = Path(root) / "state.json"
        self.error = ""
        self.writable = True
        try:
            self.data, self.error = read_protected(self.path, {
                "quotas": {}, "ui": {}, "models": [], "draft": {}, "service_wanted": False,
            }, lambda value: all(isinstance(value.get(key, {}), dict) for key in ("quotas", "ui", "draft", "api_checks"))
               and all(isinstance(item, dict) for item in value.get("quotas", {}).values())
               and all(isinstance(item, dict) for item in value.get("api_checks", {}).values())
               and isinstance(value.get("models", []), list))
        except ValueError as error:
            self.data = {"quotas": {}, "ui": {}, "models": [], "draft": {}, "service_wanted": False}
            self.error = str(error)
            self.writable = False

    def save(self, value: dict):
        if not self.writable:
            raise ValueError(self.error)
        if value != self.data or not self.path.exists() or self.error:
            write_protected(self.path, value, recovered=bool(self.error))
            self.data = copy.deepcopy(value)
            self.error = ""


class OperationHistory:
    """Append each action separately so a partial final line loses no earlier actions."""
    def __init__(self, root: Path):
        self.root = Path(root) / "history"

    def append(self, message: str, event="action") -> dict:
        stamp = datetime.now().astimezone()
        record = {"time": stamp.isoformat(timespec="seconds"), "event": event,
                  "message": message[:8000]}
        content = encode_protected(record) + b"\n"
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / ("operations-" + stamp.strftime("%Y-%m") + ".jsonl")
        # A separator also isolates a final line interrupted by power loss.
        prefix = b""
        if path.exists() and path.stat().st_size:
            with path.open("rb") as stream:
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b"\n":
                    prefix = b"\n"
        with path.open("ab") as stream:
            stream.write(prefix + content)
            stream.flush()
            os.fsync(stream.fileno())
        return record

    def records(self, limit=1000, *, include_engine=False):
        result = deque(maxlen=limit)
        for path in sorted(self.root.glob("operations-*.jsonl"), reverse=True):
            current = deque(maxlen=limit)
            with path.open("rb") as stream:
                for line in stream:
                    try:
                        record = decode_protected(line)
                        if include_engine or record.get("event") != "engine":
                            current.append(record)
                    except READ_ERRORS:
                        continue
            # Read newest months first; retain chronological order within the result.
            result = deque([*current, *result], maxlen=limit)
            if limit is not None and len(result) >= limit:
                break
        return list(result)


def history_line(record):
    stamp = str(record.get("time", "")).replace("T", " ")
    return f"[{stamp}] {record.get('message', '')}"


def local_members(root: Path) -> tuple[list[dict], int]:
    """Read display metadata only; never copy OAuth tokens into UI state or history."""
    rows, errors = [], 0
    for path in sorted((Path(root) / "auths").glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict):
                raise ValueError("invalid credential")
            if raw.get("type", "").lower() != "codex":
                continue
            priority, weight = raw.get("priority", 0), raw.get("weight", 1)
            if not isinstance(priority, int) or isinstance(priority, bool) or not -1000000 <= priority <= 1000000:
                priority = 0
            if not isinstance(weight, int) or isinstance(weight, bool) or not 1 <= weight <= 1000000:
                weight = 1
            rows.append({"name": path.name, "email": str(raw.get("email", "")),
                         "label": str(raw.get("label", "")), "id": path.stem,
                         "disabled": bool(raw.get("disabled", False)),
                         "priority": priority, "weight": weight,
                         "status": "本地已保存 · 服务未启动", "offline": True})
        except (OSError, ValueError, TypeError, AttributeError):
            errors += 1
    return rows, errors


def backup_credentials(root: Path):
    root = Path(root)
    files = {path.name: base64.b64encode(path.read_bytes()).decode("ascii")
             for path in sorted((root / "auths").glob("*.json"))}
    if not files:
        return None
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode("ascii")).hexdigest()
    target = root / "backups" / "credentials" / (digest + ".json")
    if not target.exists():
        atomic_bytes(target, encode_protected({"created_at": datetime.now().astimezone().isoformat(),
                                               "files": files}))
    return target
