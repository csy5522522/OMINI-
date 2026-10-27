"""Authenticated memo storage; keys survive updates and never enter the package."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import tempfile

from cryptography.fernet import Fernet, InvalidToken

PREFIX = b"OMINI-MEMO-1:"


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".omini-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class MemoCipher:
    def __init__(self, key_path, *, protected=False, existing=False):
        self.path = Path(key_path)
        self.protected = protected and os.name == "nt"
        if not self.path.exists():
            if existing:
                raise ValueError("备忘录加密密钥缺失，请恢复原密钥；原有内容保持原样。")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key()
            if self.protected:
                from campus_network import protect_password
                key = ("dpapi:" + protect_password(key.decode("ascii"))).encode("ascii")
            fd, temp = tempfile.mkstemp(prefix=".memo-key-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(key); stream.flush(); os.fsync(stream.fileno())
                os.chmod(temp, 0o600)
                try: os.link(temp, self.path)
                except FileExistsError: pass
            finally:
                Path(temp).unlink(missing_ok=True)
        try:
            key = self.path.read_bytes().strip()
            if key.startswith(b"dpapi:"):
                from campus_network import unprotect_password
                key = unprotect_password(key[6:].decode("ascii")).encode("ascii")
            self.key = key
            self.cipher = Fernet(key)
        except Exception:
            raise ValueError("备忘录密钥读取失败，请恢复原密钥或使用原 Windows 账号打开。") from None

    def encrypt(self, value, context):
        body = json.dumps({"context": context, "data": base64.b64encode(value).decode("ascii")},
                          ensure_ascii=False).encode("utf-8")
        return PREFIX + self.cipher.encrypt(body)

    def decrypt(self, value, context):
        raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        if not raw.startswith(PREFIX):
            raise ValueError("备忘录密文格式错误，已保留原数据。")
        try:
            result = json.loads(self.cipher.decrypt(raw[len(PREFIX):]))
            if result["context"] != context: raise ValueError()
            return base64.b64decode(result["data"], validate=True)
        except (InvalidToken, ValueError, KeyError, TypeError):
            raise ValueError("备忘录校验失败，请检查密钥或恢复备份；已保留原数据。") from None

    def text(self, value, context):
        return self.encrypt(value.encode("utf-8"), context).decode("ascii")

    def read_text(self, value, context):
        return self.decrypt(value, context).decode("utf-8")
