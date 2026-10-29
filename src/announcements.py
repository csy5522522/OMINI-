"""Public announcements with administrator writes and optimistic edit checks."""
import secrets
import time


class AnnouncementConflict(ValueError):
    pass


class AnnouncementStore:
    def __init__(self, database):
        self.database = database
        with database.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS announcements (
                id VARCHAR(32) PRIMARY KEY, title VARCHAR(120) NOT NULL,
                content LONGTEXT NOT NULL, version INTEGER NOT NULL,
                created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL)""")

    def listing(self):
        with self.database.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM announcements ORDER BY updated_at DESC, id DESC").fetchall()]

    def save(self, value):
        action = value.get("action", "save")
        if action not in {"save", "delete"}:
            raise ValueError("公告操作有误。")
        identifier = value.get("id")
        if identifier is not None and (not isinstance(identifier, str) or len(identifier) != 32
                                       or any(c not in "0123456789abcdef" for c in identifier)):
            raise ValueError("公告编号有误。")
        if action == "delete" and not identifier:
            raise ValueError("请选择要删除的公告。")
        if action == "save":
            for key, maximum, label in (("title", 120, "标题"), ("content", 20000, "正文")):
                text = value.get(key)
                if not isinstance(text, str) or not text.strip() or len(text.strip()) > maximum:
                    raise ValueError(f"公告{label}请填写 1 至 {maximum} 个字符。")
            title, content = value["title"].strip(), value["content"].strip()
        now = int(time.time())
        with self.database.connect() as db:
            if not identifier:
                identifier = secrets.token_hex(16)
                db.execute("INSERT INTO announcements VALUES (?, ?, ?, ?, ?, ?)",
                           (identifier, title, content, 1, now, now))
            else:
                version = value.get("version")
                if type(version) is not int or version < 1:
                    raise ValueError("请刷新公告后再编辑。")
                if action == "delete":
                    cursor = db.execute("DELETE FROM announcements WHERE id=? AND version=?", (identifier, version))
                else:
                    cursor = db.execute("UPDATE announcements SET title=?, content=?, version=version+1, updated_at=? "
                                        "WHERE id=? AND version=?", (title, content, now, identifier, version))
                if cursor.rowcount != 1:
                    raise AnnouncementConflict("公告已被更新或删除，请刷新后重试；当前输入仍保留。")
            if action == "delete":
                return {"ok": True}
            return {"announcement": dict(db.execute("SELECT * FROM announcements WHERE id=?", (identifier,)).fetchone())}
