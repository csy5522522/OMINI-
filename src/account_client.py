"""Desktop account client. Passwords are used for one request and never saved."""
from __future__ import annotations

import json
import secrets
from pathlib import Path
from urllib.parse import urlsplit

import requests

from campus_network import protect_password, unprotect_password

DEFAULT_SERVICE = "https://omini.xin"


def service_url(value):
    if not isinstance(value, str):
        raise ValueError("请输入完整的工具箱网页服务地址。")
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("服务地址格式：http://127.0.0.1:18765 或你的 HTTPS 服务地址。")
    try:
        parsed.port
    except ValueError:
        raise ValueError("服务端口应为 1 至 65535。") from None
    if parsed.port == 0:
        raise ValueError("服务端口应为 1 至 65535。")
    return value


class AccountClient:
    def __init__(self, url, token="", *, remember=False):
        self.url = service_url(url)
        self.token = token
        self.remember = remember

    def request(self, action, value=None):
        return self.api("/api/auth/" + action, value, action=action)

    def api(self, path, value=None, *, action=""):
        headers = {"X-Omini-Client": "desktop"}
        if action == "session" and self.remember: headers["X-Omini-Remember"] = "1"
        if self.token:
            headers["Cookie"] = "omini_session=" + self.token
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.request("GET" if value is None else "POST",
                                           self.url + path,
                                           json=value, headers=headers, timeout=(3, 10),
                                           allow_redirects=False)
            data = response.json()
        except (requests.RequestException, ValueError):
            raise ValueError("连接网页服务失败，请检查地址，或先点击「启动本机网页服务」。") from None
        if not 200 <= response.status_code < 300 or not isinstance(data, dict):
            raise ValueError(data.get("error", "账号请求失败，请稍后重试。") if isinstance(data, dict) else "账号响应格式有误。")
        if action == "logout":
            self.token = ""
        elif response.cookies.get("omini_session"):
            self.token = response.cookies["omini_session"]
        return data


class DesktopSession:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self, url):
        try:
            data = json.loads(unprotect_password(self.path.read_text(encoding="utf-8")))
            if data.get("url") == service_url(url) and isinstance(data.get("token"), str):
                return data["token"]
        except (OSError, ValueError, RuntimeError, TypeError, AttributeError):
            pass
        return ""

    def user(self, url):
        try:
            data = json.loads(unprotect_password(self.path.read_text(encoding="utf-8")))
            user = data.get("user")
            if data.get("url") == service_url(url) and isinstance(user, dict) and isinstance(user.get("id"), str) and isinstance(user.get("email"), str):
                return user
        except (OSError, ValueError, RuntimeError, TypeError, AttributeError):
            pass
        return None

    def save(self, url, token, user=None):
        if not token and user is None:
            self.path.unlink(missing_ok=True)
            return
        protected = protect_password(json.dumps({"url": service_url(url), "token": token, "user": user}))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + "." + secrets.token_hex(6) + ".tmp")
        try:
            temporary.write_text(protected, encoding="utf-8")
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
