"""Account scoped, encrypted, versioned documents shared by every device."""
from __future__ import annotations

import copy
import hashlib
from datetime import date
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid

from cryptography.fernet import Fernet

from feature_config import DESKTOP_FEATURES, validate_features
from school_timetable import validate_cache
from api_members import validate_api_members

RESOURCES = ("preferences", "schedule", "assistants")
MISSING = object()


def merge_documents(base, incoming, current, path=()):
    """Merge independent edits; retain the server value and a backup on conflict.

    An OAuth credential and a timetable are atomic: mixing fields from separate
    credential refreshes or semesters would produce an invalid document.
    """
    if incoming == base:
        return current, []
    if current == base or incoming == current:
        return incoming, []
    atomic = path == ("schedule",) or len(path) == 2 and path[0] == "members" or len(path) == 3 and path[:2] == ("pool", "api_members")
    if not atomic and isinstance(incoming, dict) and isinstance(current, dict) and (isinstance(base, dict) or base is MISSING):
        result, conflicts = {}, []
        baseline = base if isinstance(base, dict) else {}
        for key in sorted(set(baseline) | set(incoming) | set(current)):
            value, issues = merge_documents(baseline.get(key, MISSING), incoming.get(key, MISSING), current.get(key, MISSING), path + (key,))
            if value is not MISSING:
                result[key] = copy.deepcopy(value)
            conflicts.extend(issues)
        return result, conflicts
    return current, ["/".join(path) or "document"]


def validate_resource(name, value):
    if name not in RESOURCES or not isinstance(value, dict):
        raise ValueError("同步数据类型有误。")
    value = copy.deepcopy(value)
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(raw.encode()) > 16 * 1024 * 1024:
        raise ValueError("单项账号同步数据最多 16 MB。")
    if name == "preferences":
        if set(value) - {"theme", "concurrency", "features"}:
            raise ValueError("账号偏好包含本机专用选项。")
        if "theme" in value and value["theme"] not in ("深色", "浅色"):
            raise ValueError("主题选项有误。")
        if "concurrency" in value and (type(value["concurrency"]) is not int or not 1 <= value["concurrency"] <= 6):
            raise ValueError("并发数应为 1 至 6。")
        # Read old account documents without resurrecting retired features.
        if "features" in value and isinstance(value["features"], dict):
            value = {**value, "features": {k: v for k, v in value["features"].items()
                                           if k not in {"brightdata", "omini_api"}}}
        validate_features(value.get("features", {}), DESKTOP_FEATURES)
    elif name == "schedule":
        if set(value) - {"schedule", "first_monday", "selected_week"}:
            raise ValueError("课表同步字段有误。")
        if value.get("schedule") is not None:
            value = {**value, "schedule": validate_cache(value["schedule"])}
        monday = value.get("first_monday", "")
        if not isinstance(monday, str) or monday and date.fromisoformat(monday).weekday() != 0:
            raise ValueError("第一周开始日期请选择周一。")
        if "selected_week" in value and (type(value["selected_week"]) is not int or not 0 <= value["selected_week"] <= 60):
            raise ValueError("周次应为 0（全学期）至 60 的整数。")
    else:
        if set(value) - {"profiles", "pool", "members", "quotas", "models", "history"}:
            raise ValueError("助手同步字段有误。")
        for key in ("profiles", "pool", "members", "quotas", "history"):
            if not isinstance(value.get(key, {}), dict):
                raise ValueError("助手同步数据格式有误。")
        if not isinstance(value.get("models", []), list) or not all(isinstance(x, str) for x in value.get("models", [])):
            raise ValueError("模型列表格式有误。")
        if set(value.get("profiles", {})) - {"codex", "gemini", "claude", "antigravity"}:
            raise ValueError("助手类型有误。")
        for profile in value.get("profiles", {}).values():
            if not isinstance(profile, dict) or set(profile) - {"mode", "proxy", "no_proxy", "args", "desktop"}:
                raise ValueError("助手启动目录和程序路径仅保存在本机。")
            if "mode" in profile and profile["mode"] not in ("custom", "direct", "inherit"):
                raise ValueError("代理模式有误。")
            if "args" in profile and (not isinstance(profile["args"], list) or not all(isinstance(x, str) and "\x00" not in x for x in profile["args"])):
                raise ValueError("助手参数格式有误。")
            if any(k in profile and not isinstance(profile[k], str) for k in ("proxy", "no_proxy")):
                raise ValueError("代理地址格式有误。")
            if "desktop" in profile and type(profile["desktop"]) is not bool:
                raise ValueError("客户端类型有误。")
        pool = value.get("pool", {})
        if set(pool) - {"strategy", "session_affinity", "member_rules", "model", "api_members"}:
            raise ValueError("账号池的端口、代理和本机密钥保存在当前设备。")
        if "strategy" in pool and pool["strategy"] not in ("balanced", "round-robin", "fill-first", "weighted-round-robin"):
            raise ValueError("账号池调度策略有误。")
        if "session_affinity" in pool and type(pool["session_affinity"]) is not bool:
            raise ValueError("会话保持选项有误。")
        if not isinstance(pool.get("member_rules", {}), dict) or not isinstance(pool.get("model", ""), str):
            raise ValueError("账号池配置格式有误。")
        if "api_members" in pool:
            value["pool"]["api_members"] = validate_api_members(pool["api_members"])
        for rule in pool.get("member_rules", {}).values():
            if not isinstance(rule, dict) or any(type(rule.get(k, default)) is not int or not low <= rule.get(k, default) <= 1000000
                for k, default, low in (("priority", 0, -1000000), ("weight", 1, 1))):
                raise ValueError("成员优先级或权重有误。")
        if any(not isinstance(x, dict) for x in value.get("quotas", {}).values()):
            raise ValueError("额度快照格式有误。")
        for snapshot in value.get("quotas", {}).values():
            if not snapshot: continue
            checked = snapshot.get("checked_at")
            if type(checked) not in (int, float) or not math.isfinite(checked) or not 0 <= checked < 253402300800:
                raise ValueError("额度快照时间有误。")
            for kind in ("five_hour", "weekly"):
                window = snapshot.get(kind)
                if window is None: continue
                if not isinstance(window, dict): raise ValueError("额度窗口有误。")
                for key in ("used_percent", "remaining_percent"):
                    number = window.get(key)
                    if number is not None and (type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= 100):
                        raise ValueError("额度百分比有误。")
        for record in value.get("history", {}).values():
            if not isinstance(record, dict) or any(not isinstance(record.get(k, ""), str) for k in ("time", "event", "message")):
                raise ValueError("操作记录格式有误。")
        members = value.get("members", {})
        if len(members) > 500:
            raise ValueError("每个账号最多同步 500 个 Codex 成员。")
        for filename, member in members.items():
            if not re.fullmatch(r"[A-Za-z0-9_@.+-]{1,200}\.json", filename) or filename.startswith("."):
                raise ValueError("成员文件名有误。")
            if not isinstance(member, dict) or member.get("type") != "codex":
                raise ValueError("请选择 Codex 成员凭据。")
            if not all(isinstance(member.get(k), str) and member[k].strip() for k in ("access_token", "refresh_token")):
                raise ValueError("成员凭据缺少访问或刷新令牌。")
            if "disabled" in member and type(member["disabled"]) is not bool:
                raise ValueError("成员状态有误。")
    return copy.deepcopy(value)


class AccountDataStore:
    def __init__(self, database, root):
        self.database = database
        self.lock = threading.RLock()
        with database.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS account_documents (
                    owner VARCHAR(64) NOT NULL, name VARCHAR(32) NOT NULL,
                    revision INTEGER NOT NULL, body LONGBLOB NOT NULL, updated_at DOUBLE NOT NULL,
                    PRIMARY KEY(owner, name)
                );
                CREATE TABLE IF NOT EXISTS account_document_versions (
                    owner VARCHAR(64) NOT NULL, name VARCHAR(32) NOT NULL,
                    revision INTEGER NOT NULL, body LONGBLOB NOT NULL, updated_at DOUBLE NOT NULL,
                    PRIMARY KEY(owner, name, revision)
                );
                CREATE TABLE IF NOT EXISTS account_sync_conflicts (
                    id VARCHAR(32) PRIMARY KEY, owner VARCHAR(64) NOT NULL, name VARCHAR(32) NOT NULL,
                    body LONGBLOB NOT NULL, created_at DOUBLE NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_member_deletions (
                    owner VARCHAR(64) NOT NULL, member_hash VARCHAR(64) NOT NULL, revision INTEGER NOT NULL,
                    PRIMARY KEY(owner, member_hash)
                );
            """)

        key_path = Path(os.environ.get("OMINI_SYNC_KEY_FILE", "")) if os.environ.get("OMINI_SYNC_KEY_FILE") else Path(root) / "account-sync.key"
        if not key_path.exists():
            with database.connect() as db:
                if db.execute("SELECT owner FROM account_documents LIMIT 1").fetchone():
                    raise ValueError("账号同步密钥缺失，请恢复原密钥文件后启动服务。")
            if os.environ.get("OMINI_SYNC_KEY_FILE"):
                raise ValueError("请先配置账号同步加密密钥文件。")
            key_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with key_path.open("xb") as stream:
                    stream.write(Fernet.generate_key())
                key_path.chmod(0o600)
            except FileExistsError:
                pass
        self.cipher = Fernet(key_path.read_bytes().strip())

    def encode(self, value):
        return self.cipher.encrypt(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())

    def decode(self, value):
        return json.loads(self.cipher.decrypt(bytes(value)))

    def get(self, owner, name, known_revision=None):
        if name not in RESOURCES:
            raise ValueError("同步数据类型有误。")
        with self.database.connect() as db:
            row = db.execute("SELECT * FROM account_documents WHERE owner=? AND name=?", (owner, name)).fetchone()
        if row and known_revision == row["revision"]:
            return {"revision": row["revision"], "unchanged": True}
        return {"revision": row["revision"], "data": self.decode(row["body"]), "updated_at": row["updated_at"]} if row else {"revision": 0, "data": {}}

    def exchange(self, owner, name, revision, incoming):
        incoming = validate_resource(name, incoming)
        if type(revision) is not int or revision < 0:
            raise ValueError("同步版本号有误。")
        # The SQL revision predicate also protects writes from other workers.
        for attempt in range(5):
            with self.lock:
                try:
                    with self.database.connect() as db:
                        row = db.execute("SELECT * FROM account_documents WHERE owner=? AND name=?", (owner, name)).fetchone()
                        number = row["revision"] if row else 0
                        current = self.decode(row["body"]) if row else {}
                        if revision > number:
                            raise ValueError("同步版本来自其他服务，请重新连接原服务。")
                        if revision:
                            previous = db.execute("SELECT body FROM account_document_versions WHERE owner=? AND name=? AND revision=?", (owner, name, revision)).fetchone()
                            if not previous:
                                raise ValueError("同步基准版本不存在，当前内容已保留。")
                            baseline = self.decode(previous["body"])
                        else:
                            baseline = {}
                        merged, paths = merge_documents(baseline, incoming, current)
                        if name == "assistants" and revision == 0:
                            deleted = {r["member_hash"] for r in db.execute("SELECT member_hash FROM account_member_deletions WHERE owner=?", (owner,)).fetchall()}
                            for filename in list(merged.get("members", {})):
                                if filename not in current.get("members", {}) and hashlib.sha256(filename.encode()).hexdigest() in deleted:
                                    del merged["members"][filename]
                                    paths.append("members/" + filename)
                            current_apis = current.get("pool", {}).get("api_members", {})
                            merged_apis = merged.get("pool", {}).get("api_members", {})
                            for identity in list(merged_apis):
                                if identity not in current_apis and hashlib.sha256(("pool/api_members/" + identity).encode()).hexdigest() in deleted:
                                    del merged_apis[identity]
                                    paths.append("pool/api_members/" + identity)
                        merged = validate_resource(name, merged)
                        conflict_id = ""
                        if paths:
                            conflict_id = uuid.uuid4().hex
                            db.execute("INSERT INTO account_sync_conflicts VALUES (?, ?, ?, ?, ?)",
                                (conflict_id, owner, name, self.encode({"incoming": incoming, "current": current, "paths": paths}), time.time()))
                        if merged != current or row is None:
                            updated, encrypted = time.time(), self.encode(merged)
                            if row:
                                count = db.execute("UPDATE account_documents SET revision=?, body=?, updated_at=? WHERE owner=? AND name=? AND revision=?",
                                    (number + 1, encrypted, updated, owner, name, number)).rowcount
                                if count != 1:
                                    raise ConcurrentUpdate()
                            else:
                                db.execute("INSERT INTO account_documents VALUES (?, ?, ?, ?, ?)", (owner, name, 1, encrypted, updated))
                            number += 1
                            db.execute("INSERT INTO account_document_versions VALUES (?, ?, ?, ?, ?)", (owner, name, number, encrypted, updated))
                            if name == "assistants":
                                old_members, new_members = current.get("members", {}), merged.get("members", {})
                                old_members = {**old_members, **{"pool/api_members/" + k: v for k, v in current.get("pool", {}).get("api_members", {}).items()}}
                                new_members = {**new_members, **{"pool/api_members/" + k: v for k, v in merged.get("pool", {}).get("api_members", {}).items()}}
                                for filename in set(old_members) ^ set(new_members):
                                    identity = hashlib.sha256(filename.encode()).hexdigest()
                                    db.execute("DELETE FROM account_member_deletions WHERE owner=? AND member_hash=?", (owner, identity))
                                    if filename not in new_members:
                                        db.execute("INSERT INTO account_member_deletions VALUES (?, ?, ?)", (owner, identity, number))
                        return {"revision": number, "data": merged, "conflicts": paths, "conflict_id": conflict_id}
                except Exception as error:
                    if isinstance(error, ConcurrentUpdate) or type(error).__name__ == "IntegrityError":
                        if attempt < 4:
                            continue
                    raise
        raise ValueError("其他设备正在保存，请稍后重试。")

    def conflicts(self, owner, conflict_id=None):
        with self.database.connect() as db:
            if conflict_id:
                row = db.execute("SELECT * FROM account_sync_conflicts WHERE owner=? AND id=?", (owner, conflict_id)).fetchone()
                if not row:
                    raise ValueError("同步备份不存在。")
                return {"id": row["id"], "name": row["name"], "created_at": row["created_at"], **self.decode(row["body"])}
            return [dict(row) for row in db.execute("SELECT id, name, created_at FROM account_sync_conflicts WHERE owner=? ORDER BY created_at DESC", (owner,)).fetchall()]


class ConcurrentUpdate(Exception):
    pass
