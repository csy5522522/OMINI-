"""Local/Tailscale web edition. Run: python web_server.py --open."""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import Future
from dataclasses import asdict
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
from urllib.parse import quote, unquote, urlsplit
import webbrowser

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal, Slot

import app as desktop
import school_timetable as school
from accounts import AccountStore
from database import secret
from memo_store import MemoStore, MemoConflict
from feature_config import WEB_FEATURES, enabled_features, validate_features

ROOT = Path(__file__).resolve().parent
STATIC = desktop.resource_path("web")
MAX_BODY = 32 * 1024 * 1024
WEB_VERSION = desktop.APP_VERSION + "-web"


class Dispatcher(QObject):
    requested = Signal(object)

    def __init__(self):
        super().__init__()
        self.requested.connect(self.execute)

    @Slot(object)
    def execute(self, job):
        future, callback = job
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(callback())
            except Exception as error:
                future.set_exception(error)

    def call(self, callback):
        future = Future()
        self.requested.emit((future, callback))
        try:
            return future.result(timeout=15)
        except TimeoutError:
            future.cancel()
            raise ValueError("服务正忙，请稍后重试。") from None


class WebState:
    def __init__(self, data_dir: Path, download_dir: Path, password: str = ""):
        self.data_dir = data_dir.resolve()
        self.download_dir = download_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        # Separate web queue, logs and temporary cookies from desktop data.
        desktop.APP_DIR = self.data_dir
        desktop.TASKS_FILE = self.data_dir / "tasks.json"
        desktop.LOG_DIR = self.data_dir / "logs"
        desktop.TEMP_COOKIE_DIR = self.data_dir / "runtime-cookies"
        desktop.ensure_app_dirs()
        self.settings_path = self.data_dir / "settings.json"
        self.settings = desktop.load_json(self.settings_path, {"concurrency": 2})
        self.manager = desktop.TaskManager(self.settings)
        # Restart never silently starts pending downloads.
        for task in self.manager.tasks:
            if task.status == "queued":
                task.status = "paused"
        self.manager.save_tasks()
        self.dispatcher = Dispatcher()
        self.lock = threading.RLock()
        self.password = password
        self.accounts = AccountStore(self.data_dir / "accounts.sqlite3")
        self.memos = MemoStore(self.accounts.database)
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
                    "first_monday": preferences.get("first_monday", "")}

    def save_schedule(self, value):
        value = school.validate_cache(value)
        with self.lock:
            old = desktop.load_json(self.data_dir / "timetable.json", {})
            if (old.get("year"), old.get("term")) != (value["year"], value["term"]):
                desktop.save_json(self.data_dir / "semester.json", {})
            desktop.save_json(self.data_dir / "timetable.json", value)
        return self.schedule()

    def task(self, task_id):
        task = self.dispatcher.call(lambda: next((asdict(t) for t in self.manager.tasks if t.id == task_id), None))
        if not task:
            raise ValueError("任务不存在。")
        return task

    def task_root(self, task_id):
        task = self.task(task_id)
        path = Path(task["output_dir"]).resolve()
        if not path.is_relative_to(self.download_dir) or path == self.download_dir:
            raise ValueError("此记录不在 Web 下载目录中。")
        return path


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
        return self.state.memos.get_data(self.user["id"], "settings", {"concurrency": 2})

    @property
    def features(self):
        return enabled_features(self.preferences(), WEB_FEATURES)

    def owns_task(self, task):
        root = self.state.download_dir / self.user["id"] if self.user else self.state.download_dir
        target = Path(task["output_dir"]).resolve()
        return target.is_relative_to(root) and target != root and (bool(self.user) or target.parent == root)

    def personal_task(self, task_id):
        task = self.state.task(task_id)
        if not self.owns_task(task): raise ValueError("任务不存在。")
        return task

    def schedule(self):
        return self.state.memos.get_data(self.user["id"], "schedule", {"schedule": None, "first_monday": ""}) if self.user else self.state.schedule()

    def save_schedule(self, value):
        if not self.user: return self.state.save_schedule(value)
        schedule = school.validate_cache(value)
        old = self.schedule()
        monday = old.get("first_monday", "") if old.get("schedule") and (old["schedule"].get("year"), old["schedule"].get("term")) == (schedule["year"], schedule["term"]) else ""
        result = {"schedule": schedule, "first_monday": monday}
        self.state.memos.set_data(self.user["id"], "schedule", result)
        return result

    def log_message(self, *args):
        # Request paths and headers may contain private data.
        pass

    def headers_common(self, content_type, length, cookie=None):
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
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
                    return self.respond(200, {"user": self.state.accounts.session_user(self.state.session_token(cookie)),
                                              "authenticated": self.state.authenticated(cookie),
                                              "access_password_required": bool(self.state.password)})
                if not self.state.authenticated(self.headers.get("Cookie")):
                    return self.respond(401, {"error": "请先使用邮箱账号登录。"})
                if not self.feature_available(path):
                    return
                return self.get_api(path)
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/memos.js": ("memos.js", "text/javascript; charset=utf-8"),
                      "/memo-editor.js": ("memo-editor.js", "text/javascript; charset=utf-8"),
                      "/memos.css": ("memos.css", "text/css; charset=utf-8"),
                      "/admin": ("admin.html", "text/html; charset=utf-8"),
                      "/admin/": ("admin.html", "text/html; charset=utf-8"),
                      "/admin.js": ("admin.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if path == "/favicon.ico":
                file, content_type = desktop.resource_path("assets/omini_icon.ico"), "image/x-icon"
            elif path in assets:
                filename, content_type = assets[path]
                file = STATIC / filename
            else:
                return self.respond(404, {"error": "页面不存在。"})
            data = file.read_bytes()
            self.send_response(200)
            self.headers_common(content_type, len(data))
            self.end_headers()
            self.wfile.write(data)
        except (ValueError, KeyError, TypeError, OSError) as error:
            self.respond(400, {"error": str(error) if isinstance(error, ValueError) else "读取失败，请重试。"})
        except Exception:
            self.respond(502, {"error": "服务读取失败，请稍后重试。"})

    def get_api(self, path):
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
            tasks = self.state.dispatcher.call(lambda: [asdict(t) for t in self.state.manager.tasks if self.owns_task(asdict(t))]) if features["downloads"] else []
            engines = {}
            for name in (("yt-dlp", "aria2c", "ffmpeg", "gallery-dl", "spotdl") if features["downloads"] else ()):
                try:
                    desktop.find_engine(name)
                    engines[name] = True
                except desktop.EngineError:
                    engines[name] = False
            return self.respond(200, {"version": WEB_VERSION, "tasks": tasks,
                                     "download_dir": str(self.state.download_dir), "engines": engines,
                                     "features": features,
                                     "concurrency": self.preferences().get("concurrency", 2)})
        if path == "/api/timetable":
            return self.respond(200, self.schedule())
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "tasks"]:
            task_id, action = parts[2:]
            task = self.personal_task(task_id)
            if action == "log":
                logfile = desktop.LOG_DIR / f"{task['id']}.log"
                text = ""
                if logfile.exists():
                    with logfile.open("rb") as stream:
                        stream.seek(max(0, logfile.stat().st_size - 64000))
                        text = stream.read(64000).decode("utf-8", errors="replace")
                return self.respond(200, {"log": text})
            if action == "files":
                root = self.state.task_root(task_id)
                files = []
                if root.exists():
                    for item in root.rglob("*"):
                        if item.is_file() and item.resolve().is_relative_to(root) and not item.name.endswith((".part", ".aria2", ".ytdl")):
                            relative = item.relative_to(root).as_posix()
                            files.append({"name": relative, "bytes": item.stat().st_size,
                                          "url": f"/api/files/{task_id}/{quote(relative, safe='/')}"})
                        if len(files) >= 500:
                            break
                return self.respond(200, {"files": files, "complete": task["status"] == "completed"})
        if len(parts) >= 4 and parts[:2] == ["api", "files"]:
            self.personal_task(parts[2])
            root = self.state.task_root(parts[2])
            target = root.joinpath(*parts[3:]).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                return self.respond(404, {"error": "文件不存在。"})
            with target.open("rb") as stream:
                self.send_response(200)
                self.headers_common("application/octet-stream", os.fstat(stream.fileno()).st_size)
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(target.name))
                self.end_headers()
                import shutil
                shutil.copyfileobj(stream, self.wfile, 256 * 1024)
            return
        self.respond(404, {"error": "接口不存在。"})

    def feature_available(self, path):
        feature = ("timetable" if path.startswith("/api/timetable") else
                   "notes" if path.startswith("/api/memos") else
                   "downloads" if path.startswith(("/api/tasks", "/api/files")) else None)
        if feature and not self.features[feature]:
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
                token = self.state.accounts.issue_session(user)
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
        if path == "/api/tasks":
            kind = value.get("type")
            if kind not in desktop.TYPE_TEXT:
                raise ValueError("请选择有效的下载类型。")
            source = str(value.get("source", "")).strip()
            if len(source) > 8000:
                raise ValueError("链接过长。")
            usable = desktop.extract_usable_source(source, allow_search=kind == "subtitle")
            parsed = urlsplit(usable)
            if not (parsed.scheme in {"http", "https"} and parsed.hostname or kind == "torrent" and parsed.scheme == "magnet" or kind == "subtitle" and not parsed.scheme and usable):
                raise ValueError("请输入 HTTP/HTTPS 链接；磁力任务可填写 magnet 链接。")
            supplied = value.get("options", {})
            options = {k: supplied[k] for k in ("quality", "format", "language", "subtitles", "metadata", "thumbnail", "playlist") if k in supplied}
            if options.get("quality", "最佳") not in {"最佳", "2160p", "1080p", "720p", "480p"}:
                raise ValueError("清晰度有误。")
            if options.get("format", "mp3") not in {"mp3", "m4a", "flac", "wav", "opus"}:
                raise ValueError("音频格式有误。")
            if not isinstance(options.get("language", "zh.*,en.*"), str) or len(options.get("language", "")) > 100:
                raise ValueError("字幕语言格式有误。")
            for key in ("subtitles", "metadata", "thumbnail", "playlist"):
                if key in options and type(options[key]) is not bool:
                    raise ValueError("下载选项格式有误。")
            filename = str(supplied.get("filename", "")).strip()
            if filename:
                if len(filename) > 160 or any(c in filename for c in '<>:"/\\|?*') or filename in {".", ".."} or any(ord(c) < 32 for c in filename):
                    raise ValueError("文件名包含无效字符，请只填写文件名。")
                options["filename"] = filename
            directory = self.state.download_dir / (self.user["id"] if self.user else "") / secrets.token_hex(8)
            task = self.state.dispatcher.call(lambda: asdict(self.state.manager.add(kind, source, str(directory), options)))
            return self.respond(201, {"task": task})
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "tasks"]:
            task_id, action = parts[2:]
            self.personal_task(task_id)
            if action not in {"pause", "resume", "cancel", "retry"}:
                raise ValueError("任务操作有误。")
            self.state.dispatcher.call(lambda: getattr(self.state.manager, action)(task_id))
            return self.respond(200, {"ok": True})
        if path == "/api/settings":
            count = value.get("concurrency", self.preferences().get("concurrency", 2))
            if type(count) is not int or not 1 <= count <= 5:
                raise ValueError("并发数应为 1 至 5。")
            selected = validate_features(value.get("features", {}), WEB_FEATURES)
            if self.user:
                updated = {**self.preferences(), "concurrency": count, "features": {**self.features, **selected}}
                self.state.memos.set_data(self.user["id"], "settings", updated)
                if not updated["features"]["downloads"]:
                    def pause_owned():
                        for task in self.state.manager.tasks:
                            if self.owns_task(asdict(task)) and task.status in {"running", "queued"}: self.state.manager.pause(task.id)
                    self.state.dispatcher.call(pause_owned)
                return self.respond(200, {"ok": True, "features": updated["features"]})
            def save():
                with self.state.lock:
                    updated = {**self.state.settings, "concurrency": count,
                               "features": {**self.state.features, **selected}}
                    desktop.save_json(self.state.settings_path, updated)
                    self.state.settings.update(updated)
                    if not self.state.features["downloads"]:
                        for task in self.state.manager.tasks:
                            if task.status in {"running", "queued"}:
                                self.state.manager.pause(task.id)
                    else:
                        self.state.manager._start_waiting_locked()
            self.state.dispatcher.call(save)
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
            return self.respond(200, self.save_schedule(schedule))
        if path == "/api/timetable/refresh":
            # Cookie stays in this request; never saved with timetable data.
            schedule = school.fetch_schedule(str(value.get("cookie", "")), str(value.get("year", "")), str(value.get("term", "")))
            return self.respond(200, self.save_schedule(schedule))
        if path == "/api/timetable/semester":
            monday = str(value.get("first_monday", ""))
            if monday and date.fromisoformat(monday).weekday() != 0:
                raise ValueError("第一周开始日期请选择周一。")
            if self.user:
                schedule = self.schedule(); schedule["first_monday"] = monday
                self.state.memos.set_data(self.user["id"], "schedule", schedule)
                return self.respond(200, schedule)
            with self.state.lock:
                desktop.save_json(self.state.data_dir / "semester.json", {"first_monday": monday})
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
    parser = argparse.ArgumentParser(description="Omini Web：下载中心与学校课表")
    parser.add_argument("--host", default="127.0.0.1", help="127.0.0.1 或本机 Tailscale IPv4")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--data-dir", type=Path, default=desktop.APP_DIR / "web")
    parser.add_argument("--download-dir", type=Path, default=Path.home() / "Downloads/Omini-Web")
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
    state = WebState(args.data_dir, args.download_dir, password)
    try:
        server = make_server(state, args.host, args.port)
    except OSError:
        parser.exit(1, "服务地址或端口暂不可用，请确认 IP 属于本机，或通过 --port 18766 选择其他端口。\n")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://{args.host}:{server.server_address[1]}"
    if args.status_file:
        desktop.save_json(args.status_file, {"url": url, "version": WEB_VERSION})
    print(f"Omini Web {WEB_VERSION}\nLocal URL: {url}\nDownload directory: {state.download_dir}\nPress Ctrl+C to stop.", flush=True)
    if args.open:
        webbrowser.open(url)
    def stop(*_):
        if state.stopping:
            return
        state.stopping = True
        for task in state.manager.tasks:
            if task.status == "queued":
                task.status = "paused"
        state.manager.save_tasks()
        for worker in state.manager.workers.values():
            worker.request_stop(pause=True)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    timer = QTimer()
    def tick():
        if args.stop_file and args.stop_file.exists():
            stop()
        if state.stopping and not state.manager.workers:
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
