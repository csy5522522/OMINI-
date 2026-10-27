"""Small transactional SQLite/MySQL adapter; MySQL is used by Docker deployments."""
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3


def secret(name, default=""):
    filename = os.environ.get(name + "_FILE")
    return Path(filename).read_text(encoding="utf-8").strip() if filename else os.environ.get(name, default)


class Connection:
    def __init__(self, connection, mysql):
        self.connection, self.mysql = connection, mysql

    def execute(self, sql, values=()):
        cursor = self.connection.cursor()
        cursor.execute(sql.replace("?", "%s") if self.mysql else sql, values)
        return cursor

    def executescript(self, sql):
        for statement in sql.split(";"):
            if statement.strip(): self.execute(statement)


class Database:
    def __init__(self, path, *, local=False):
        self.path = Path(path)
        self.mysql = bool(os.environ.get("OMINI_MYSQL_HOST")) and not local
        if not self.mysql: self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self):
        if self.mysql:
            import pymysql
            raw = pymysql.connect(host=os.environ["OMINI_MYSQL_HOST"],
                port=int(os.environ.get("OMINI_MYSQL_PORT", "3306")),
                user=os.environ.get("OMINI_MYSQL_USER", "omini"), password=secret("OMINI_MYSQL_PASSWORD"),
                database=os.environ.get("OMINI_MYSQL_DATABASE", "omini"), charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor, connect_timeout=10, read_timeout=30,
                autocommit=False)
        else:
            raw = sqlite3.connect(self.path, timeout=30)
            raw.row_factory = sqlite3.Row
            raw.execute("PRAGMA foreign_keys=ON")
            raw.execute("PRAGMA busy_timeout=30000")
        try:
            yield Connection(raw, self.mysql)
            raw.commit()
        except Exception:
            raw.rollback()
            raise
        finally:
            raw.close()
