"""Local/Tailscale web edition. Run: python web_server.py --open."""
from __future__ import annotations

import argparse
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

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "web"
MAX_BODY = 32 * 1024 * 1024
WEB_VERSION = "1.2.0-web"


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

    def authenticated(self, cookie):
        jar = SimpleCookie()
        try:
            jar.load(cookie or "")
            key = jar["omini_session"].value
        except (KeyError, ValueError, CookieError):
            return False
        with self.lock:
            return self.sessions.get(key, 0) > time.time()

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
        if cookie:
            self.send_header("Set-Cookie", f"omini_session={cookie}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200")

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
        if origin and origin != f"http://{host}":
            self.respond(403, {"error": "请求来源不匹配。"})
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.respond(403, {"error": "请直接打开工具箱地址。"})
            return False
        if mutation and self.headers.get("X-Omini-Client") != "web":
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
                if not self.state.authenticated(self.headers.get("Cookie")):
                    return self.respond(401, {"error": "请先输入访问密码。"})
                return self.get_api(path)
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if path == "/favicon.ico":
                file, content_type = ROOT / "assets/omini_icon.ico", "image/x-icon"
            elif path in assets:
                filename, content_type = assets[path]
                file = STATIC / filename
            else:
                return self.respond(404, {"error": "页面不存在。"})
            data = file.read_bytes()
            cookie = self.state.issue_session() if path == "/" and not self.state.password else None
            self.send_response(200)
            self.headers_common(content_type, len(data), cookie)
            self.end_headers()
            self.wfile.write(data)
        except (ValueError, KeyError, TypeError, OSError) as error:
            self.respond(400, {"error": str(error) if isinstance(error, ValueError) else "读取失败，请重试。"})

    def get_api(self, path):
        if path == "/api/state":
            tasks = self.state.dispatcher.call(lambda: [asdict(t) for t in self.state.manager.tasks])
            engines = {}
            for name in ("yt-dlp", "aria2c", "ffmpeg", "gallery-dl", "spotdl"):
                try:
                    desktop.find_engine(name)
                    engines[name] = True
                except desktop.EngineError:
                    engines[name] = False
            return self.respond(200, {"version": WEB_VERSION, "tasks": tasks,
                                     "download_dir": str(self.state.download_dir), "engines": engines,
                                     "concurrency": self.state.settings.get("concurrency", 2)})
        if path == "/api/timetable":
            return self.respond(200, self.state.schedule())
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "tasks"]:
            task_id, action = parts[2:]
            task = self.state.task(task_id)
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

    def do_POST(self):
        if not self.check_request(mutation=True):
            return
        try:
            path = urlsplit(self.path).path
            value = self.read_json()
            if path == "/api/login":
                address = self.client_address[0]
                with self.state.lock:
                    now = time.time()
                    attempts = [t for t in self.state.attempts.get(address, []) if t > now - 60]
                    self.state.attempts[address] = attempts
                    if len(attempts) >= 10:
                        return self.respond(429, {"error": "尝试次数较多，请一分钟后重试。"})
                    if not secrets.compare_digest(str(value.get("password", "")).encode(), self.state.password.encode()):
                        attempts.append(now)
                        return self.respond(401, {"error": "访问密码不正确。"})
                    self.state.attempts.pop(address, None)
                return self.respond(200, {"ok": True}, self.state.issue_session())
            if not self.state.authenticated(self.headers.get("Cookie")):
                return self.respond(401, {"error": "请先输入访问密码。"})
            self.post_api(path, value)
        except (ValueError, KeyError, TypeError, OSError) as error:
            self.respond(400, {"error": str(error) if isinstance(error, ValueError) else "操作失败，请检查输入后重试。"})
        except Exception:
            self.respond(502, {"error": "服务请求失败，请稍后重试；已有任务和课表已保留。"})

    def post_api(self, path, value):
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
            directory = self.state.download_dir / secrets.token_hex(8)
            task = self.state.dispatcher.call(lambda: asdict(self.state.manager.add(kind, source, str(directory), options)))
            return self.respond(201, {"task": task})
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "tasks"]:
            task_id, action = parts[2:]
            self.state.task(task_id)
            if action not in {"pause", "resume", "cancel", "retry"}:
                raise ValueError("任务操作有误。")
            self.state.dispatcher.call(lambda: getattr(self.state.manager, action)(task_id))
            return self.respond(200, {"ok": True})
        if path == "/api/settings":
            count = value.get("concurrency")
            if type(count) is not int or not 1 <= count <= 5:
                raise ValueError("并发数应为 1 至 5。")
            def save():
                self.state.settings["concurrency"] = count
                desktop.save_json(self.state.settings_path, self.state.settings)
                self.state.manager._start_waiting_locked()
            self.state.dispatcher.call(save)
            return self.respond(200, {"ok": True})
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
            return self.respond(200, self.state.save_schedule(schedule))
        if path == "/api/timetable/refresh":
            # Cookie stays in this request; never saved with timetable data.
            schedule = school.fetch_schedule(str(value.get("cookie", "")), str(value.get("year", "")), str(value.get("term", "")))
            return self.respond(200, self.state.save_schedule(schedule))
        if path == "/api/timetable/semester":
            monday = str(value.get("first_monday", ""))
            if monday and date.fromisoformat(monday).weekday() != 0:
                raise ValueError("第一周开始日期请选择周一。")
            with self.state.lock:
                desktop.save_json(self.state.data_dir / "semester.json", {"first_monday": monday})
            return self.respond(200, self.state.schedule())
        self.respond(404, {"error": "接口不存在。"})


def make_server(state, host, port):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.state = state
    actual_port = server.server_address[1]
    server.allowed_hosts = {f"{host}:{actual_port}"}
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
    args = parser.parse_args()
    try:
        address = ipaddress.IPv4Address(args.host)
    except ValueError:
        parser.error("--host 请填写本机 IPv4 地址。")
    password = os.environ.get("OMINI_WEB_PASSWORD", "")
    if not address.is_loopback and (address.is_unspecified or len(password) < 12):
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
