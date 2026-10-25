"""Per-account memos, immutable action snapshots and content-addressed images."""
from __future__ import annotations
import base64
import hashlib
import io
import json
import re
import secrets
import time
import os
import hmac
import threading
from contextlib import contextmanager
from pathlib import Path
from memo_crypto import MemoCipher, PREFIX
from database import Database


class MemoConflict(ValueError):
    pass


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32,64}", value):
        raise ValueError("记录标识无效。")
    return value


def validate_text(title, content):
    if not isinstance(title, str) or not title.strip() or len(title) > 240:
        raise ValueError("标题需为 1 至 240 个字符。")
    if not isinstance(content, str) or len(content.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("每篇备忘录正文最多 2 MB。")


def snapshot(row):
    value = dict(row)
    value["deleted"] = bool(value["deleted"])
    return value


class MemoStore:
    def __init__(self, database, *, key_root=None, existing_files=False):
        self.database = database if isinstance(database, Database) else Database(database, local=True)
        self._transactions = threading.local()
        with self.database.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS memos (
                    user_id VARCHAR(64) NOT NULL, id VARCHAR(64) NOT NULL,
                    title LONGTEXT NOT NULL, content LONGTEXT NOT NULL,
                    version INTEGER NOT NULL, deleted INTEGER NOT NULL, updated DOUBLE NOT NULL,
                    PRIMARY KEY(user_id, id)
                );
                CREATE TABLE IF NOT EXISTS memo_versions (
                    user_id VARCHAR(64) NOT NULL, memo_id VARCHAR(64) NOT NULL,
                    version INTEGER NOT NULL, title LONGTEXT NOT NULL, content LONGTEXT NOT NULL,
                    deleted INTEGER NOT NULL, action VARCHAR(32) NOT NULL, created DOUBLE NOT NULL,
                    device VARCHAR(80) NOT NULL, PRIMARY KEY(user_id, memo_id, version)
                );
                CREATE TABLE IF NOT EXISTS memo_assets (
                    user_id VARCHAR(64) NOT NULL, id VARCHAR(64) NOT NULL, mime VARCHAR(32) NOT NULL,
                    name LONGTEXT NOT NULL, data LONGBLOB NOT NULL, created DOUBLE NOT NULL,
                    raw_size INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(user_id, id)
                );
                CREATE TABLE IF NOT EXISTS workspace_data (
                    user_id VARCHAR(64) NOT NULL, name VARCHAR(80) NOT NULL, value LONGTEXT NOT NULL,
                    PRIMARY KEY(user_id, name)
                );
                CREATE TABLE IF NOT EXISTS memo_storage_meta (
                    name VARCHAR(80) PRIMARY KEY, value LONGTEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memo_operations (
                    user_id VARCHAR(64) NOT NULL, memo_id VARCHAR(64) NOT NULL,
                    operation_id VARCHAR(64) NOT NULL, fingerprint VARCHAR(64) NOT NULL,
                    version INTEGER NOT NULL,
                    PRIMARY KEY(user_id, memo_id, operation_id)
                );
            """)
            columns = db.execute("SHOW COLUMNS FROM memo_assets" if self.database.mysql else "PRAGMA table_info(memo_assets)").fetchall()
            if "raw_size" not in {row["Field" if self.database.mysql else "name"] for row in columns}:
                db.execute("ALTER TABLE memo_assets ADD raw_size INTEGER NOT NULL DEFAULT 0")

        # Existing ciphertext must never cause a replacement key to be generated.
        encrypted = existing_files
        with self.database.connect() as db:
            marker = db.execute("SELECT value FROM memo_storage_meta WHERE name='cipher-check'").fetchone()
        if marker:
            encrypted = True
        else:
            encrypted = encrypted or self._has_encrypted_rows()
        key_path = Path(key_root) / "memo-storage.key" if key_root else self.database.path.with_suffix(self.database.path.suffix + ".key")
        self.cipher = MemoCipher(key_path, protected=key_root is None, existing=encrypted)
        if marker:
            self.cipher.decrypt(marker["value"], "cipher-check")
        else:
            self._migrate()

    @contextmanager
    def transaction(self):
        """Share one commit across a memo action, its mapping and local sync metadata."""
        if getattr(self._transactions, "connection", None) is not None:
            yield
            return
        with self.database.connect() as db:
            self._transactions.connection = db
            try:
                yield
            finally:
                del self._transactions.connection

    @contextmanager
    def _connect(self):
        db = getattr(self._transactions, "connection", None)
        if db is not None:
            yield db
        else:
            with self.database.connect() as db:
                yield db

    def _has_encrypted_rows(self):
        with self.database.connect() as db:
            for table, field in (("memos", "content"), ("memo_versions", "content"),
                                 ("memo_assets", "data"), ("workspace_data", "value")):
                cursor = db.execute(f"SELECT {field} FROM {table}")
                for row in cursor:
                    raw = row[field]
                    if (raw.encode() if isinstance(raw, str) else bytes(raw)).startswith(PREFIX):
                        return True
        return False

    @staticmethod
    def _context(table, row, field):
        identity = row.get("memo_id", row.get("id", row.get("name", "")))
        return f"{table}/{row['user_id']}/{identity}/{row.get('version', '')}/{field}"

    def _decoded(self, row, table="memos"):
        value = dict(row)
        fields = {"memos": ("title", "content"), "memo_versions": ("title", "content"),
                  "memo_assets": ("name", "data"), "workspace_data": ("value",)}[table]
        for field in fields:
            raw = self.cipher.decrypt(value[field], self._context(table, value, field))
            value[field] = raw if field == "data" else raw.decode("utf-8")
        if "deleted" in value: value["deleted"] = bool(value["deleted"])
        return value

    def _migrate(self):
        import tempfile
        tables = {"memos": ("title", "content"), "memo_versions": ("title", "content"),
                  "memo_assets": ("name", "data"), "workspace_data": ("value",)}
        backup = None; temporary = None; changed = False
        try:
            with self.database.connect() as db:
                # Wider ciphertext includes authentication and context; old MySQL title fields were short.
                if self.database.mysql:
                    for table in ("memos", "memo_versions"):
                        db.execute(f"ALTER TABLE {table} MODIFY title LONGTEXT NOT NULL")
                    db.execute("ALTER TABLE memo_assets MODIFY name LONGTEXT NOT NULL")
                    db.execute("ALTER TABLE memo_assets MODIFY data LONGBLOB NOT NULL")
                else: db.execute("PRAGMA secure_delete=ON")
                for table, fields in tables.items():
                    for original in db.execute(f"SELECT * FROM {table}").fetchall():
                        row = dict(original); changes = {}
                        for field in fields:
                            raw = row[field].encode("utf-8") if isinstance(row[field], str) else bytes(row[field])
                            context = self._context(table, row, field)
                            if raw.startswith(PREFIX):
                                self.cipher.decrypt(raw, context)
                            else: changes[field] = self.cipher.encrypt(raw, context)
                        if table == "memo_assets":
                            raw = row["data"]
                            if bytes(raw).startswith(PREFIX): raw = self.cipher.decrypt(raw, self._context(table, row, "data"))
                            db.execute("UPDATE memo_assets SET raw_size=? WHERE user_id=? AND id=?", (len(raw), row["user_id"], row["id"]))
                        if not changes: continue
                        if backup is None:
                            fd, temporary = tempfile.mkstemp(prefix=".memo-migration-", dir=self.cipher.path.parent)
                            backup = os.fdopen(fd, "wb")
                        serial = {k: {"bytes": base64.b64encode(bytes(v)).decode()} if isinstance(v, (bytes, bytearray)) else v for k,v in row.items()}
                        backup.write(self.cipher.encrypt(json.dumps({"table": table, "row": serial}, ensure_ascii=False).encode(), "migration-backup") + b"\n")
                        backup.flush(); os.fsync(backup.fileno())
                        keys = [k for k in row if k not in fields]
                        # Only stable primary keys, never content or timestamps, identify migrations.
                        keys = [k for k in keys if k in {"user_id", "id", "memo_id", "version", "name"}]
                        values = [v if k == "data" else v.decode("ascii") for k,v in changes.items()]
                        db.execute(f"UPDATE {table} SET " + ", ".join(k+"=?" for k in changes) +
                                   " WHERE " + " AND ".join(k+"=?" for k in keys), values + [row[k] for k in keys])
                        changed = True
                db.execute("INSERT INTO memo_storage_meta VALUES (?, ?)",
                           ("cipher-check", self.cipher.encrypt(b"memo-storage-v1", "cipher-check").decode("ascii")))
            if changed and not self.database.mysql:
                with self.database.connect() as db: db.execute("VACUUM")
        finally:
            if backup:
                backup.close()
                # Keep an authenticated, encrypted recovery copy even when migration rolls back.
                os.replace(temporary, str(temporary) + ".enc")

    def listing(self, user_id, *, include_deleted=True):
        with self._connect() as db:
            rows = db.execute("SELECT * FROM memos WHERE user_id=? ORDER BY updated DESC", (user_id,)).fetchall()
        return [self._decoded(row) for row in rows if include_deleted or not row["deleted"]]

    def get(self, user_id, memo_id):
        identifier(memo_id)
        with self._connect() as db:
            row = db.execute("SELECT * FROM memos WHERE user_id=? AND id=?", (user_id, memo_id)).fetchone()
        if not row: raise ValueError("备忘录不存在。")
        return self._decoded(row)

    def save(self, user_id, value, *, action="edit", device="web"):
        memo_id = identifier(value.get("id") or secrets.token_hex(16))
        title, content = value.get("title", "未命名备忘录"), value.get("content", "")
        validate_text(title, content)
        base = value.get("base_version", 0)
        if type(base) is not int or base < 0: raise ValueError("请提供有效的基础版本。")
        deleted = value.get("deleted", False)
        if type(deleted) is not bool: raise ValueError("删除标识有误。")
        operation = value.get("operation_id")
        fingerprint = None
        if operation is not None:
            identifier(operation)
            body = json.dumps([memo_id, title.strip(), content, base, deleted, action], ensure_ascii=False).encode()
            fingerprint = hmac.new(self.cipher.key, body, hashlib.sha256).hexdigest()
        now = time.time()
        with self._connect() as db:
            # Lock the existing row in MySQL; SQLite's guarded UPDATE detects concurrent writes.
            row = db.execute("SELECT version FROM memos WHERE user_id=? AND id=?" +
                (" FOR UPDATE" if self.database.mysql else ""), (user_id, memo_id)).fetchone()
            current = row["version"] if row else 0
            if operation:
                committed = db.execute("SELECT * FROM memo_operations WHERE user_id=? AND memo_id=? AND operation_id=?",
                    (user_id, memo_id, operation)).fetchone()
                if committed:
                    if committed["fingerprint"] != fingerprint:
                        raise ValueError("操作标识已用于另一项编辑。")
                    saved = db.execute("SELECT * FROM memo_versions WHERE user_id=? AND memo_id=? AND version=?",
                        (user_id, memo_id, committed["version"])).fetchone()
                    saved = self._decoded(saved, "memo_versions")
                    return {"user_id": user_id, "id": memo_id, **{k: saved[k] for k in ("title", "content", "version", "deleted")},
                            "updated": saved["created"]}
            if base != current: raise MemoConflict("已有更新版本，请先同步；本次编辑已保留。")
            version = current + 1
            context = {"user_id": user_id, "id": memo_id, "version": version}
            title_enc = self.cipher.text(title.strip(), self._context("memos", context, "title"))
            content_enc = self.cipher.text(content, self._context("memos", context, "content"))
            history_title = self.cipher.text(title.strip(), self._context("memo_versions", context, "title"))
            history_content = self.cipher.text(content, self._context("memo_versions", context, "content"))
            if row:
                changed = db.execute("UPDATE memos SET title=?, content=?, version=?, deleted=?, updated=? "
                    "WHERE user_id=? AND id=? AND version=?",
                    (title_enc, content_enc, version, int(deleted), now, user_id, memo_id, current))
                if changed.rowcount != 1: raise MemoConflict("记录刚被更新，请重新同步。")
            else:
                count = db.execute("SELECT COUNT(*) AS n FROM memos WHERE user_id=?", (user_id,)).fetchone()["n"]
                if count >= 2000: raise ValueError("每个账号最多保存 2000 篇备忘录。")
                db.execute("INSERT INTO memos VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (user_id, memo_id, title_enc, content_enc, version, int(deleted), now))
                action = "create"
            db.execute("INSERT INTO memo_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, memo_id, version, history_title, history_content, int(deleted), action[:32], now, device[:80]))
            if operation:
                db.execute("INSERT INTO memo_operations VALUES (?, ?, ?, ?, ?)",
                    (user_id, memo_id, operation, fingerprint, version))
        # Return this action's snapshot even if another writer commits immediately.
        return {"user_id": user_id, "id": memo_id, "title": title.strip(), "content": content,
                "version": version, "deleted": deleted, "updated": now}

    def history(self, user_id, memo_id):
        self.get(user_id, memo_id)
        with self._connect() as db:
            operations = {row["version"]: row["operation_id"] for row in db.execute(
                "SELECT version, operation_id FROM memo_operations WHERE user_id=? AND memo_id=?", (user_id, memo_id)).fetchall()}
            versions = [self._decoded(row, "memo_versions") for row in db.execute("SELECT * FROM memo_versions WHERE user_id=? "
                "AND memo_id=? ORDER BY version DESC", (user_id, memo_id)).fetchall()]
            for version in versions:
                if version["version"] in operations: version["operation_id"] = operations[version["version"]]
            return versions

    def restore(self, user_id, memo_id, version, base_version, device="web"):
        if type(version) is not int: raise ValueError("请选择历史版本。")
        with self.database.connect() as db:
            row = db.execute("SELECT * FROM memo_versions WHERE user_id=? AND memo_id=? AND version=?",
                (user_id, identifier(memo_id), version)).fetchone()
        if not row: raise ValueError("历史版本不存在。")
        row = self._decoded(row, "memo_versions")
        return self.save(user_id, {"id": memo_id, "title": row["title"], "content": row["content"],
            "deleted": bool(row["deleted"]), "base_version": base_version}, action="rollback", device=device)

    def put_asset(self, user_id, name, encoded):
        from PIL import Image
        if not isinstance(encoded, str) or len(encoded) > 14 * 1024 * 1024:
            raise ValueError("图片最大为 10 MB。")
        try: raw = base64.b64decode(encoded, validate=True)
        except Exception: raise ValueError("图片编码有误。") from None
        if not raw or len(raw) > 10 * 1024 * 1024: raise ValueError("图片最大为 10 MB。")
        try:
            with Image.open(io.BytesIO(raw)) as im:
                if im.width * im.height > 40_000_000: raise ValueError()
                mime = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp"}[im.format]
                im.verify()
        except Exception: raise ValueError("请选择有效的 PNG、JPEG、GIF 或 WebP 图片（最多 4000 万像素）。") from None
        asset_id = hashlib.sha256(raw).hexdigest()
        with self.database.connect() as db:
            existing = db.execute("SELECT id FROM memo_assets WHERE user_id=? AND id=?", (user_id, asset_id)).fetchone()
            if not existing:
                used = db.execute("SELECT COALESCE(SUM(raw_size),0) AS n FROM memo_assets WHERE user_id=?", (user_id,)).fetchone()["n"]
                if used + len(raw) > 200 * 1024 * 1024: raise ValueError("当前账号图片空间已达到 200 MB。")
                db.execute("INSERT INTO memo_assets VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (user_id, asset_id, mime,
                     self.cipher.text(str(name or "image")[:240], f"memo_assets/{user_id}/{asset_id}//name"),
                     self.cipher.encrypt(raw, f"memo_assets/{user_id}/{asset_id}//data"), time.time(), len(raw)))
        return {"id": asset_id, "mime": mime, "name": str(name or "image")[:240]}

    def asset(self, user_id, asset_id):
        with self.database.connect() as db:
            row = db.execute("SELECT * FROM memo_assets WHERE user_id=? AND id=?", (user_id, identifier(asset_id))).fetchone()
        if not row: raise ValueError("图片不存在。")
        return self._decoded(row, "memo_assets")

    def get_data(self, user_id, name, default=None):
        with self._connect() as db:
            row = db.execute("SELECT value FROM workspace_data WHERE user_id=? AND name=?", (user_id, name)).fetchone()
        return json.loads(self.cipher.read_text(row["value"], f"workspace_data/{user_id}/{name}//value")) if row else default

    def set_data(self, user_id, name, value):
        encoded = self.cipher.text(json.dumps(value, ensure_ascii=False), f"workspace_data/{user_id}/{name}//value")
        with self._connect() as db:
            db.execute("DELETE FROM workspace_data WHERE user_id=? AND name=?", (user_id, name))
            db.execute("INSERT INTO workspace_data VALUES (?, ?, ?)", (user_id, name, encoded))
