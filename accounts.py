"""Email/password accounts and durable, revocable sessions for one toolbox service."""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import time
from pathlib import Path
from database import Database

ITERATIONS = 600_000
SESSION_SECONDS = 12 * 3600
EMAIL_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}\Z")


def normalize_email(value):
    if not isinstance(value, str):
        raise ValueError("请输入有效的邮箱地址。")
    email = value.strip().casefold()
    if len(email) > 254 or not EMAIL_RE.fullmatch(email) or ".." in email:
        raise ValueError("请输入有效的邮箱地址。")
    return email


def validate_password(value):
    if not isinstance(value, str) or not 8 <= len(value) <= 128:
        raise ValueError("密码长度应为 8 至 128 个字符。")
    return value


class AccountStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.database = Database(path)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id VARCHAR(64) PRIMARY KEY, email VARCHAR(254) UNIQUE NOT NULL,
                    salt VARBINARY(64) NOT NULL, password_hash VARBINARY(64) NOT NULL,
                    iterations INTEGER NOT NULL, created_at DOUBLE NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash VARCHAR(64) PRIMARY KEY, user_id VARCHAR(64) NOT NULL,
                    expires_at DOUBLE NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS account_profiles (
                    user_id VARCHAR(64) PRIMARY KEY, role VARCHAR(16) NOT NULL,
                    disabled INTEGER NOT NULL DEFAULT 0
                );
            """)

    def connect(self):
        return self.database.connect()

    @staticmethod
    def derive(password, salt, iterations=ITERATIONS):
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)

    def register(self, email, password):
        email, password = normalize_email(email), validate_password(password)
        user = {"id": secrets.token_hex(16), "email": email}
        salt = secrets.token_bytes(16)
        hashed = self.derive(password, salt)
        try:
            with self.connect() as db:
                db.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?, ?)",
                           (user["id"], email, salt, hashed, ITERATIONS, time.time()))
        except Exception as error:
            if isinstance(error, sqlite3.IntegrityError) or type(error).__name__ == "IntegrityError":
                raise ValueError("该邮箱已注册，请直接登录。") from None
            raise
        return user

    def login(self, email, password):
        email = "admin" if email == "admin" else normalize_email(email)
        password = validate_password(password)
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        # Unknown accounts still perform a password derivation.
        actual = self.derive(password, row["salt"] if row else b"omini-missing-user",
                             row["iterations"] if row else ITERATIONS)
        if row is None or not secrets.compare_digest(actual, row["password_hash"]):
            raise ValueError("邮箱或密码不正确。")
        with self.connect() as db:
            profile = db.execute("SELECT role, disabled FROM account_profiles WHERE user_id=?", (row["id"],)).fetchone()
        if profile and profile["disabled"]: raise ValueError("账号已停用，请联系管理员。")
        user = {"id": row["id"], "email": row["email"]}
        if profile and profile["role"] == "admin": user["role"] = "admin"
        return user

    def issue_session(self, user):
        token = secrets.token_urlsafe(32)
        hashed = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at<=?", (time.time(),))
            # Bound retained sessions per account, leaving other devices logged in.
            retained = db.execute("SELECT token_hash FROM sessions WHERE user_id=? ORDER BY expires_at DESC", (user["id"],)).fetchall()
            for old in retained[19:]:
                db.execute("DELETE FROM sessions WHERE token_hash=?", (old["token_hash"],))
            db.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                       (hashed, user["id"], time.time() + SESSION_SECONDS))
        return token

    def session_user(self, token):
        if not isinstance(token, str) or not 20 <= len(token) <= 200:
            return None
        with self.connect() as db:
            row = db.execute("SELECT users.id, users.email, account_profiles.role, account_profiles.disabled FROM sessions JOIN users "
                             "ON users.id=sessions.user_id LEFT JOIN account_profiles ON account_profiles.user_id=users.id "
                             "WHERE token_hash=? AND expires_at>?",
                             (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        if not row or row["disabled"]: return None
        user = {"id": row["id"], "email": row["email"]}
        if row["role"] == "admin": user["role"] = "admin"
        return user

    def revoke_session(self, token):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?",
                       (hashlib.sha256(token.encode()).hexdigest(),))

    def bootstrap_admin(self, password):
        validate_password(password)
        with self.connect() as db:
            if db.execute("SELECT id FROM users WHERE email=?", ("admin",)).fetchone(): return
            user_id, salt = secrets.token_hex(16), secrets.token_bytes(16)
            db.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?, ?)",
                       (user_id, "admin", salt, self.derive(password, salt), ITERATIONS, time.time()))
            db.execute("INSERT INTO account_profiles VALUES (?, ?, ?)", (user_id, "admin", 0))

    def list_users(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT users.id, users.email, users.created_at, "
                "COALESCE(account_profiles.role, 'user') AS role, COALESCE(account_profiles.disabled, 0) AS disabled "
                "FROM users LEFT JOIN account_profiles ON users.id=account_profiles.user_id ORDER BY users.created_at DESC").fetchall()]

    def manage_user(self, user_id, *, disabled=None, password=None):
        with self.connect() as db:
            row = db.execute("SELECT users.id, account_profiles.role FROM users LEFT JOIN account_profiles "
                "ON users.id=account_profiles.user_id WHERE users.id=?", (user_id,)).fetchone()
            if not row: raise ValueError("账号不存在。")
            if disabled is not None:
                if type(disabled) is not bool: raise ValueError("账号状态有误。")
                if row["role"] == "admin": raise ValueError("管理员账号请保留启用状态。")
                db.execute("DELETE FROM account_profiles WHERE user_id=?", (user_id,))
                db.execute("INSERT INTO account_profiles VALUES (?, ?, ?)", (user_id, "user", int(disabled)))
            if password is not None:
                validate_password(password)
                salt = secrets.token_bytes(16)
                db.execute("UPDATE users SET salt=?, password_hash=?, iterations=? WHERE id=?",
                           (salt, self.derive(password, salt), ITERATIONS, user_id))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
