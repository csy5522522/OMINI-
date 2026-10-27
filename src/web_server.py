"""Local/Tailscale web edition. Run: python web_server.py --open."""
from __future__ import annotations
from project_paths import PROJECT_ROOT

import argparse
import base64
from datetime import date
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import secrets
import signal
import tempfile
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit
import webbrowser

from PySide6.QtCore import QCoreApplication, QTimer

import app as desktop
import school_timetable as school
from accounts import AccountStore
from account_data import AccountDataStore, RESOURCES
from database import secret
from memo_store import MemoStore, MemoConflict
from feature_config import WEB_FEATURES, enabled_features, validate_features
from web_assets import StaticAssets

ROOT = PROJECT_ROOT
STATIC = desktop.resource_path("web")
PUBLIC_ASSETS = StaticAssets({
    "/": (STATIC / "index.html", "text/html; charset=utf-8"),
    "/admin": (STATIC / "admin.html", "text/html; charset=utf-8"),
    "/admin/": (STATIC / "admin.html", "text/html; charset=utf-8"),
    **{f"/{name}.js": (STATIC / f"{name}.js", "text/javascript; charset=utf-8")
       for name in ("app", "memos", "memo-editor", "account-sync", "draft-store", "admin")},
    **{f"/{name}.css": (STATIC / f"{name}.css", "text/css; charset=utf-8")
       for name in ("style", "memos", "account-sync")},
    "/favicon.ico": (desktop.resource_path("assets/omini_icon.ico"), "image/x-icon"),
})
MAX_BODY = 32 * 1024 * 1024
WEB_VERSION = desktop.APP_VERSION + "-web"


class WebState:
    def __init__(self, data_dir: Path, password: str = ""):
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.data_dir / "settings.json"
        self.settings = desktop.load_json(self.settings_path, {})
        self.lock = threading.RLock()
        self.password = password
        self.accounts = AccountStore(self.data_dir / "accounts.sqlite3")
        self.memos = MemoStore(self.accounts.database, key_root=self.data_dir)
        self.account_data = AccountDataStore(self.accounts.database, self.data_dir)
        admin_password = secret("OMINI_ADMIN_PASSWORD")
        if admin_password: self.accounts.bootstrap_admin(admin_password)
        self.sessions = {}
        self.attempts = {}
        self.stopping = False

    def issue_session(self):
        with self.lock:
            now = time.time()
            self.sessions = {k: v for k, v in self.sessions.items() if v > now}
            if len(self.sessions) >= 100:
                self.sessions.pop(next(iter(self.sessions)))
            key = secrets.token_urlsafe(32)
            self.sessions[key] = now + 12 * 3600
            return key

    @staticmethod
    def session_token(cookie):
        jar = SimpleCookie()
        try:
            jar.load(cookie or "")
            key = jar["omini_session"].value
        except (KeyError, ValueError, CookieError):
            return ""
        return key

    def authenticated(self, cookie):
        key = self.session_token(cookie)
        if self.accounts.session_user(key):
            return True
        with self.lock:
            return self.sessions.get(key, 0) > time.time()

    @property
    def features(self):
        return enabled_features(self.settings, WEB_FEATURES)

    def check_attempt(self, address, action):
        with self.lock:
            now = time.time()
            self.attempts = {key: [t for t in values if t > now - 60]
                             for key, values in self.attempts.items() if values and values[-1] > now - 60}
            key = (address, action)
            attempts = self.attempts.setdefault(key, [])
            if len(attempts) >= 10:
                return False
            attempts.append(now)
            return True

    def schedule(self):
        with self.lock:
            value = desktop.load_json(self.data_dir / "timetable.json", None)
            preferences = desktop.load_json(self.data_dir / "semester.json", {})
            return {"schedule": school.validate_cache(value) if value else None,
                    "first_monday": preferences.get("first_monday", ""), "selected_week": preferences.get("selected_week", 0)}

    def save_schedule(self, value):
        value = school.validate_cache(value)
        with self.lock:
            old = desktop.load_json(self.data_dir / "timetable.json", {})
            if (old.get("year"), old.get("term")) != (value["year"], value["term"]):
                desktop.save_json(self.data_dir / "semester.json", {})
            desktop.save_json(self.data_dir / "timetable.json", value)
        return self.schedule()



class Handler(BaseHTTPRequestHandler):
    server_version = "OminiWeb"

    @property
    def state(self):
        return self.server.state

    @property
    def user(self):
        if not hasattr(self, "_user"):
            self._user = self.state.accounts.session_user(self.state.session_token(self.headers.get("Cookie")))
        return self._user

    def preferences(self):
        if not self.user: return self.state.settings
        return self.resource("preferences")["data"]

    def resource(self, name, known_revision=None):
        owner = self.user["id"]
        result = self.state.account_data.get(owner, name, known_revision)
        if result["revision"] == 0 and name in {"preferences", "schedule"}:
            legacy = self.state.memos.get_data(owner, "settings" if name == "preferences" else "schedule", {})
            if name == "preferences":
                legacy = {k: v for k, v in legacy.items() if k in {"theme", "features", "concurrency"}}
            if legacy:
                result = self.state.account_data.exchange(owner, name, 0, legacy)
        return result

    @property
    def features(self):
        return enabled_features(self.preferences(), WEB_FEATURES)

    def schedule(self):
        if not self.user: return self.state.schedule()
        result = self.resource("schedule")
        return {"schedule": None, "first_monday": "", **result["data"], "revision": result["revision"]}

    def save_schedule(self, value, base_revision=None):
        if not self.user: return self.state.save_schedule(value)
        schedule = school.validate_cache(value)
        old = self.schedule()
        monday = old.get("first_monday", "") if old.get("schedule") and (old["schedule"].get("year"), old["schedule"].get("term")) == (schedule["year"], schedule["term"]) else ""
        result = {"schedule": schedule, "first_monday": monday, "selected_week": old.get("selected_week", 0)}
        saved = self.state.account_data.exchange(self.user["id"], "schedule", old["revision"] if base_revision is None else base_revision, result)
        return {**saved["data"], "revision": saved["revision"], "conflicts": saved["conflicts"]}

    def log_message(self, *args):
        # Request paths and headers may contain private data.
        pass

    def headers_common(self, content_type, length, cookie=None, *, cache_control="no-store"):
        self.send_header("Content-Type", content_type)
        if length is not None: self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie is not None:
            secure = "; Secure" if os.environ.get("OMINI_PUBLIC_ORIGIN", "").startswith("https://") else ""
            self.send_header("Set-Cookie", f"omini_session={cookie}; HttpOnly; SameSite=Strict; Path=/; Max-Age={43200 if cookie else 0}{secure}")

    def respond(self, status, data, cookie=None):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.headers_common("application/json; charset=utf-8", len(raw), cookie)
        self.end_headers()
        self.wfile.write(raw)

    def check_request(self, mutation=False):
        host = self.headers.get("Host", "")
        if host not in self.server.allowed_hosts:
            self.respond(403, {"error": "访问地址未配置。"})
            return False
        origin = self.headers.get("Origin")
        allowed_origins = {f"http://{host}"}
        if os.environ.get("OMINI_PUBLIC_ORIGIN"): allowed_origins.add(os.environ["OMINI_PUBLIC_ORIGIN"])
        if origin and origin not in allowed_origins:
            self.respond(403, {"error": "请求来源不匹配。"})
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.respond(403, {"error": "请直接打开工具箱地址。"})
            return False
        if mutation and self.headers.get("X-Omini-Client") not in {"web", "desktop"}:
            self.respond(403, {"error": "请求缺少客户端标识。"})
            return False
        return True

    def read_json(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("请发送 JSON 数据。")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= MAX_BODY:
            raise ValueError("数据大小应在 1 字节至 32 MB 之间。")
        self.connection.settimeout(30)
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("数据格式有误。")
        return value

    def do_GET(self):
        if not self.check_request():
            return
        path = unquote(urlsplit(self.path).path)
        try:
            if path.startswith("/api/"):
                if path == "/api/auth/session":
                    cookie = self.headers.get("Cookie")
                    remember = self.headers.get("X-Omini-Client") == "desktop" and self.headers.get("X-Omini-Remember") == "1"
                    return self.respond(200, {"user": self.state.accounts.session_user(self.state.session_token(cookie), remember=remember),
                                              "authenticated": self.state.authenticated(cookie),
                                              "access_password_required": bool(self.state.password)})
                if not self.state.authenticated(self.headers.get("Cookie")):
                    return self.respond(401, {"error": "请先使用邮箱账号登录。"})
                if not self.feature_available(path):
                    return
                return self.get_api(path)
            if path not in PUBLIC_ASSETS.files:
                return self.respond(404, {"error": "页面不存在。"})
            version = parse_qs(urlsplit(self.path).query).get("v", [""])[0]
            asset = PUBLIC_ASSETS.get(path, version)
            unchanged = asset.matches(self.headers.get("If-None-Match", ""))
            self.send_response(304 if unchanged else 200)
            self.headers_common(asset.content_type, None if unchanged else len(asset.data),
                                cache_control=asset.cache_control)
            self.send_header("ETag", asset.etag)
            self.end_headers()
            if not unchanged: self.wfile.write(asset.data)
        except (ValueError, KeyError, TypeError, OSError) as error:
            self.respond(400, {"error": str(error) if isinstance(error, ValueError) else "读取失败，请重试。"})
        except Exception:
            self.respond(502, {"error": "服务读取失败，请稍后重试。"})

    def get_api(self, path):
        if path.startswith("/api/sync"):
            if not self.user: return self.respond(401, {"error": "请使用邮箱账号登录同步。"})
            if path == "/api/sync":
                supplied = parse_qs(urlsplit(self.path).query).get("revisions", ["{}"])[0]
                known = json.loads(supplied)
                if not isinstance(known, dict): raise ValueError("同步版本列表有误。")
                return self.respond(200, {"resources": {name: self.resource(name, known.get(name)) for name in RESOURCES},
                                          "conflicts": self.state.account_data.conflicts(self.user["id"])})
            if path.startswith("/api/sync/conflicts/"):
                return self.respond(200, self.state.account_data.conflicts(self.user["id"], path.rsplit("/", 1)[-1]))
            return self.respond(404, {"error": "同步接口不存在。"})
        if path.startswith("/api/admin/"):
            if not self.user or self.user.get("role") != "admin": return self.respond(403, {"error": "请使用管理员账号登录。"})
            if path == "/api/admin/users": return self.respond(200, {"users": self.state.accounts.list_users(), "version": WEB_VERSION})
        if path.startswith("/api/memos"):
            if not self.user: return self.respond(401, {"error": "请使用邮箱账号登录备忘录。"})
            owner = self.user["id"]
            parts = path.strip("/").split("/")
            if path == "/api/memos": return self.respond(200, {"memos": self.state.memos.listing(owner)})
            if len(parts) == 4 and parts[2] == "assets":
                asset = self.state.memos.asset(owner, parts[3])
                return self.respond(200, {"id": asset["id"], "name": asset["name"], "mime": asset["mime"], "data": base64.b64encode(asset["data"]).decode()})
            if len(parts) == 4 and parts[3] == "history": return self.respond(200, {"versions": self.state.memos.history(owner, parts[2])})
            return self.respond(404, {"error": "备忘录接口不存在。"})
        if path == "/api/state":
            features = self.features
            return self.respond(200, {"version": WEB_VERSION, "features": features,
                                     "theme": self.preferences().get("theme")})
        if path == "/api/timetable":
            return self.respond(200, self.schedule())
        self.respond(404, {"error": "接口不存在。"})

    def feature_available(self, path):
        feature = ("timetable" if path.startswith("/api/timetable") else
                   "notes" if path.startswith("/api/memos") else None)
        # Feature preferences control the web UI; desktop tools are always available.
        if feature and self.headers.get("X-Omini-Client") != "desktop" and not self.features[feature]:
            self.respond(403, {"error": "此功能已关闭，请在偏好设置中勾选后使用。"})
            return False
        return True

    def do_POST(self):
        if not self.check_request(mutation=True):
            return
        try:
            path = urlsplit(self.path).path
            value = self.read_json()
            if path in {"/api/auth/register", "/api/auth/login"}:
                address = self.headers.get("X-Real-IP", self.client_address[0]) if os.environ.get("OMINI_PUBLIC_ORIGIN") else self.client_address[0]
                if not self.state.check_attempt(address, "account"):
                    return self.respond(429, {"error": "尝试次数较多，请一分钟后重试。"})
                if path.endswith("register"):
                    access = value.get("access_password", "")
                    if self.state.password and (not isinstance(access, str) or not secrets.compare_digest(access.encode(), self.state.password.encode())):
                        return self.respond(401, {"error": "注册此服务需要正确的访问密码。"})
                    user = self.state.accounts.register(value.get("email"), value.get("password"))
                else:
                    try:
                        user = self.state.accounts.login(value.get("email"), value.get("password"))
                    except ValueError as error:
                        return self.respond(401, {"error": str(error)})
                token = self.state.accounts.issue_session(user, remember=(
                    self.headers.get("X-Omini-Client") == "desktop" and value.get("remember") is True))
                # Rotate the initiating device's session without logging out other devices.
                old = self.state.session_token(self.headers.get("Cookie"))
                self.state.accounts.revoke_session(old)
                with self.state.lock:
                    self.state.sessions.pop(old, None)
                return self.respond(201 if path.endswith("register") else 200, {"user": user}, token)
            if path == "/api/auth/logout":
                token = self.state.session_token(self.headers.get("Cookie"))
                self.state.accounts.revoke_session(token)
                with self.state.lock:
                    self.state.sessions.pop(token, None)
                return self.respond(200, {"ok": True}, "")
            if path == "/api/login":
                if not self.state.check_attempt(self.client_address[0], "access"):
                    return self.respond(429, {"error": "尝试次数较多，请一分钟后重试。"})
                if not self.state.password or not secrets.compare_digest(str(value.get("password", "")).encode(), self.state.password.encode()):
                    return self.respond(401, {"error": "访问密码不正确。"})
                return self.respond(200, {"ok": True}, self.state.issue_session())
            if not self.state.authenticated(self.headers.get("Cookie")):
                return self.respond(401, {"error": "请先使用邮箱账号登录。"})
            if not self.feature_available(path):
                return
            self.post_api(path, value)
        except MemoConflict as error:
            self.respond(409, {"error": str(error), "conflict": True})
        except (ValueError, KeyError, TypeError, OSError) as error:
            self.respond(400, {"error": str(error) if isinstance(error, ValueError) else "操作失败，请检查输入后重试。"})
        except Exception:
            self.respond(502, {"error": "服务请求失败，请稍后重试；已有任务和课表已保留。"})

    def post_api(self, path, value):
        if path.startswith("/api/sync/"):
            if not self.user: return self.respond(401, {"error": "请使用邮箱账号登录同步。"})
            name = path.rsplit("/", 1)[-1]
            result = self.state.account_data.exchange(self.user["id"], name, value.get("revision"), value.get("data"))
            return self.respond(200, result)
        if path.startswith("/api/admin/"):
            if not self.user or self.user.get("role") != "admin": return self.respond(403, {"error": "请使用管理员账号登录。"})
            if path == "/api/admin/user":
                self.state.accounts.manage_user(value.get("id"), disabled=value.get("disabled"), password=value.get("password"))
                return self.respond(200, {"ok": True})
        if path.startswith("/api/memos"):
            if not self.user: return self.respond(401, {"error": "请使用邮箱账号登录备忘录。"})
            owner = self.user["id"]
            device = "desktop" if self.headers.get("X-Omini-Client") == "desktop" else "web"
            parts = path.strip("/").split("/")
            if path == "/api/memos":
                action = value.get("action", "delete" if value.get("deleted") else "edit")
                if action not in {"create", "edit", "rename", "delete", "restore", "rollback", "import", "external_edit"}: action = "edit"
                return self.respond(200, {"memo": self.state.memos.save(owner, value, device=device, action=action)})
            if path == "/api/memos/assets": return self.respond(201, self.state.memos.put_asset(owner, value.get("name"), value.get("data")))
            if len(parts) == 4 and parts[3] == "restore":
                return self.respond(200, {"memo": self.state.memos.restore(owner, parts[2], value.get("version"), value.get("base_version"), device)})
            return self.respond(404, {"error": "备忘录接口不存在。"})
        if path == "/api/settings":
            selected = validate_features(value.get("features", {}), WEB_FEATURES)
            if self.user:
                baseline = self.resource("preferences")
                updated = {**baseline["data"], "features": {**baseline["data"].get("features", {}), **selected}}
                if "theme" in value: updated["theme"] = value["theme"]
                self.state.account_data.exchange(self.user["id"], "preferences", baseline["revision"], updated)
                return self.respond(200, {"ok": True, "features": enabled_features(updated, WEB_FEATURES)})
            with self.state.lock:
                updated = {**self.state.settings, "features": {**self.state.features, **selected}}
                desktop.save_json(self.state.settings_path, updated)
                self.state.settings.update(updated)
            return self.respond(200, {"ok": True, "features": self.state.features})
        if path == "/api/timetable/import":
            content = value.get("content")
            if not isinstance(content, str):
                raise ValueError("请选择 HAR 或课表 JSON 文件。")
            payload = json.loads(content)
            if isinstance(payload, dict) and "log" in payload:
                with tempfile.TemporaryDirectory() as temp:
                    file = Path(temp) / "schedule.har"
                    file.write_text(content, encoding="utf-8")
                    schedule = school.import_har(file)
            else:
                schedule = school.validate_cache(payload)
            return self.respond(200, self.save_schedule(schedule, value.get("base_revision")))
        if path == "/api/timetable/refresh":
            # Cookie stays in this request; never saved with timetable data.
            schedule = school.fetch_schedule(str(value.get("cookie", "")), str(value.get("year", "")), str(value.get("term", "")))
            return self.respond(200, self.save_schedule(schedule, value.get("base_revision")))
        if path in {"/api/timetable/semester", "/api/timetable/week"}:
            schedule = self.schedule()
            monday = str(value.get("first_monday", schedule.get("first_monday", "")))
            week = value.get("selected_week", schedule.get("selected_week", 0))
            if type(week) is not int or not 0 <= week <= 60:
                raise ValueError("周次应为 0（全学期）至 60 的整数。")
            if monday and date.fromisoformat(monday).weekday() != 0:
                raise ValueError("第一周开始日期请选择周一。")
            if self.user:
                schedule = self.schedule()
                result = self.state.account_data.exchange(self.user["id"], "schedule", value.get("base_revision", schedule["revision"]),
                    {"schedule": schedule["schedule"], "first_monday": monday, "selected_week": week})
                return self.respond(200, {**result["data"], "revision": result["revision"]})
            with self.state.lock:
                desktop.save_json(self.state.data_dir / "semester.json", {"first_monday": monday, "selected_week": week})
            return self.respond(200, self.state.schedule())
        self.respond(404, {"error": "接口不存在。"})


def make_server(state, host, port):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.state = state
    actual_port = server.server_address[1]
    server.allowed_hosts = {f"{host}:{actual_port}", f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"}
    public = os.environ.get("OMINI_PUBLIC_ORIGIN", "")
    if public: server.allowed_hosts.add(urlsplit(public).netloc)
    if host == "127.0.0.1":
        server.allowed_hosts.add(f"localhost:{actual_port}")
    return server


def main():
    parser = argparse.ArgumentParser(description="Omini Web：课表、备忘录与账号同步")
    parser.add_argument("--host", default="127.0.0.1", help="127.0.0.1 或本机 Tailscale IPv4")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--data-dir", type=Path, default=desktop.APP_DIR / "web")
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    parser.add_argument("--stop-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--status-file", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        address = ipaddress.IPv4Address(args.host)
    except ValueError:
        parser.error("--host 请填写本机 IPv4 地址。")
    password = os.environ.get("OMINI_WEB_PASSWORD", "")
    if not os.environ.get("OMINI_PUBLIC_ORIGIN") and not address.is_loopback and (address.is_unspecified or len(password) < 12):
        parser.error("远程访问请绑定本机 Tailscale IPv4，并设置至少 12 位的 OMINI_WEB_PASSWORD 环境变量。")
    qt = QCoreApplication([])
    state = WebState(args.data_dir, password)
    try:
        server = make_server(state, args.host, args.port)
    except OSError:
        parser.exit(1, "服务地址或端口暂不可用，请确认 IP 属于本机，或通过 --port 18766 选择其他端口。\n")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://{args.host}:{server.server_address[1]}"
    if args.status_file:
        desktop.save_json(args.status_file, {"url": url, "version": WEB_VERSION})
    print(f"Omini Web {WEB_VERSION}\nLocal URL: {url}\nPress Ctrl+C to stop.", flush=True)
    if args.open:
        webbrowser.open(url)
    def stop(*_):
        if state.stopping:
            return
        state.stopping = True
        qt.quit()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    timer = QTimer()
    def tick():
        if args.stop_file and args.stop_file.exists():
            stop()
        if state.stopping:
            qt.quit()
    timer.timeout.connect(tick)
    timer.start(150)
    try:
        qt.exec()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
