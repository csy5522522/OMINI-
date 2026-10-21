"""Per-account memos, immutable action snapshots and content-addressed images."""
from __future__ import annotations
import base64
import hashlib
import io
import json
import re
import secrets
import time
from database import Database


class MemoConflict(ValueError):
    pass


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32,64}", value):
        raise ValueError("记录标识无效。")
    return value


def snapshot(row):
    value = dict(row)
    value["deleted"] = bool(value["deleted"])
    return value


class MemoStore:
    def __init__(self, database):
        self.database = database if isinstance(database, Database) else Database(database, local=True)
        with self.database.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS memos (
                    user_id VARCHAR(64) NOT NULL, id VARCHAR(64) NOT NULL,
                    title VARCHAR(255) NOT NULL, content LONGTEXT NOT NULL,
                    version INTEGER NOT NULL, deleted INTEGER NOT NULL, updated DOUBLE NOT NULL,
                    PRIMARY KEY(user_id, id)
                );
                CREATE TABLE IF NOT EXISTS memo_versions (
                    user_id VARCHAR(64) NOT NULL, memo_id VARCHAR(64) NOT NULL,
                    version INTEGER NOT NULL, title VARCHAR(255) NOT NULL, content LONGTEXT NOT NULL,
                    deleted INTEGER NOT NULL, action VARCHAR(32) NOT NULL, created DOUBLE NOT NULL,
                    device VARCHAR(80) NOT NULL, PRIMARY KEY(user_id, memo_id, version)
                );
                CREATE TABLE IF NOT EXISTS memo_assets (
                    user_id VARCHAR(64) NOT NULL, id VARCHAR(64) NOT NULL, mime VARCHAR(32) NOT NULL,
                    name VARCHAR(255) NOT NULL, data MEDIUMBLOB NOT NULL, created DOUBLE NOT NULL,
                    PRIMARY KEY(user_id, id)
                );
                CREATE TABLE IF NOT EXISTS workspace_data (
                    user_id VARCHAR(64) NOT NULL, name VARCHAR(80) NOT NULL, value LONGTEXT NOT NULL,
                    PRIMARY KEY(user_id, name)
                );
            """)

    def listing(self, user_id, *, include_deleted=True):
        with self.database.connect() as db:
            rows = db.execute("SELECT * FROM memos WHERE user_id=? ORDER BY updated DESC", (user_id,)).fetchall()
        return [snapshot(row) for row in rows if include_deleted or not row["deleted"]]

    def get(self, user_id, memo_id):
        identifier(memo_id)
        with self.database.connect() as db:
            row = db.execute("SELECT * FROM memos WHERE user_id=? AND id=?", (user_id, memo_id)).fetchone()
        if not row: raise ValueError("备忘录不存在。")
        return snapshot(row)

    def save(self, user_id, value, *, action="edit", device="web"):
        memo_id = identifier(value.get("id") or secrets.token_hex(16))
        title, content = value.get("title", "未命名备忘录"), value.get("content", "")
        if not isinstance(title, str) or not title.strip() or len(title) > 240:
            raise ValueError("标题需为 1 至 240 个字符。")
        if not isinstance(content, str) or len(content.encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError("每篇备忘录正文最多 2 MB。")
        base = value.get("base_version", 0)
        if type(base) is not int or base < 0: raise ValueError("请提供有效的基础版本。")
        deleted = value.get("deleted", False)
        if type(deleted) is not bool: raise ValueError("删除标识有误。")
        now = time.time()
        with self.database.connect() as db:
            # Lock the existing row in MySQL; SQLite's guarded UPDATE detects concurrent writes.
            row = db.execute("SELECT version FROM memos WHERE user_id=? AND id=?" +
                (" FOR UPDATE" if self.database.mysql else ""), (user_id, memo_id)).fetchone()
            current = row["version"] if row else 0
            if base != current: raise MemoConflict("已有更新版本，请先同步；本次编辑已保留。")
            version = current + 1
            if row:
                changed = db.execute("UPDATE memos SET title=?, content=?, version=?, deleted=?, updated=? "
                    "WHERE user_id=? AND id=? AND version=?",
                    (title.strip(), content, version, int(deleted), now, user_id, memo_id, current))
                if changed.rowcount != 1: raise MemoConflict("记录刚被更新，请重新同步。")
            else:
                count = db.execute("SELECT COUNT(*) AS n FROM memos WHERE user_id=?", (user_id,)).fetchone()["n"]
                if count >= 2000: raise ValueError("每个账号最多保存 2000 篇备忘录。")
                db.execute("INSERT INTO memos VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (user_id, memo_id, title.strip(), content, version, int(deleted), now))
                action = "create"
            db.execute("INSERT INTO memo_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, memo_id, version, title.strip(), content, int(deleted), action[:32], now, device[:80]))
        return self.get(user_id, memo_id)

    def history(self, user_id, memo_id):
        self.get(user_id, memo_id)
        with self.database.connect() as db:
            return [snapshot(row) for row in db.execute("SELECT * FROM memo_versions WHERE user_id=? "
                "AND memo_id=? ORDER BY version DESC", (user_id, memo_id)).fetchall()]

    def restore(self, user_id, memo_id, version, base_version, device="web"):
        if type(version) is not int: raise ValueError("请选择历史版本。")
        with self.database.connect() as db:
            row = db.execute("SELECT * FROM memo_versions WHERE user_id=? AND memo_id=? AND version=?",
                (user_id, identifier(memo_id), version)).fetchone()
        if not row: raise ValueError("历史版本不存在。")
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
                used = db.execute("SELECT COALESCE(SUM(LENGTH(data)),0) AS n FROM memo_assets WHERE user_id=?", (user_id,)).fetchone()["n"]
                if used + len(raw) > 200 * 1024 * 1024: raise ValueError("当前账号图片空间已达到 200 MB。")
                db.execute("INSERT INTO memo_assets VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, asset_id, mime, str(name or "image")[:240], raw, time.time()))
        return {"id": asset_id, "mime": mime, "name": str(name or "image")[:240]}

    def asset(self, user_id, asset_id):
        with self.database.connect() as db:
            row = db.execute("SELECT * FROM memo_assets WHERE user_id=? AND id=?", (user_id, identifier(asset_id))).fetchone()
        if not row: raise ValueError("图片不存在。")
        return dict(row)

    def get_data(self, user_id, name, default=None):
        with self.database.connect() as db:
            row = db.execute("SELECT value FROM workspace_data WHERE user_id=? AND name=?", (user_id, name)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_data(self, user_id, name, value):
        encoded = json.dumps(value, ensure_ascii=False)
        with self.database.connect() as db:
            db.execute("DELETE FROM workspace_data WHERE user_id=? AND name=?", (user_id, name))
            db.execute("INSERT INTO workspace_data VALUES (?, ?, ?)", (user_id, name, encoded))
