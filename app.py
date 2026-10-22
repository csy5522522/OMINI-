"""Omini工具箱 - 下载、校园网、课表、备忘录、同步、API 与 AI 编程助手。"""
from __future__ import annotations

import sys

# API workers use the same executable in frozen distributions, before Qt starts.
if __name__ == "__main__" and sys.argv[1:2] == ["--omini-api"]:
    from omini_api.runtime import main as run_omini_api
    raise SystemExit(run_omini_api(sys.argv[2:]))

if __name__ == "__main__" and sys.argv[1:2] == ["--web"]:
    sys.argv.pop(1)
    from web_server import main as run_web
    raise SystemExit(run_web())

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PySide6.QtCore import QObject, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QPalette, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea,
    QSpinBox, QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem,
    QStyle, QSystemTrayIcon, QTabWidget, QTextBrowser, QTextEdit, QToolButton, QVBoxLayout, QWidget,
)

from local_ffmpeg import resolve_ffmpeg
from feature_config import DESKTOP_FEATURES, PAGE_FEATURES, enabled_features
from account_panel import AccountPanel
from updater_page import UpdatePanel
from updater import launch_installer
from app_version import APP_VERSION

APP_NAME = "Omini工具箱"
APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
SETTINGS_FILE = APP_DIR / "settings.json"
TASKS_FILE = APP_DIR / "tasks.json"
LOG_DIR = APP_DIR / "logs"
TEMP_COOKIE_DIR = APP_DIR / "runtime-cookies"
STATUS_TEXT = {
    "queued": "等待中", "running": "下载中", "paused": "已暂停",
    "completed": "已完成", "failed": "失败", "cancelled": "已取消",
}
TYPE_TEXT = {
    "video": "视频", "audio": "音频", "spotify": "Spotify 音频", "file": "文件", "gallery": "图库",
    "torrent": "磁力/Torrent", "subtitle": "字幕", "m3u8": "M3U8",
}
URL_PATTERN = re.compile(r"(?:https?://|magnet:\?)[^\s<>\[\]()\"'`]+", re.IGNORECASE)


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_app_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_COOKIE_DIR.mkdir(parents=True, exist_ok=True)


def resource_path(relative: str) -> Path:
    """Resolve bundled files both in development and PyInstaller builds."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def default_download_dir() -> str:
    return str(Path.home() / "Downloads")


def extract_usable_source(value: str, allow_search: bool = False) -> str:
    """Extract the actionable URL from share text copied from social apps or browsers."""
    text = value.strip()
    matches = URL_PATTERN.findall(text)
    if matches:
        # Markdown links and Chinese share text commonly leave a closing bracket or
        # punctuation next to the URL. Those characters are not part of the link.
        return matches[0].rstrip(".,;!?，。；！？)]}】》\"'")
    return text if allow_search else ""


def cookie_file_kind(path: str) -> str:
    """Return the supported cookie-file representation without retaining its content."""
    try:
        content = Path(path).read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise ValueError(f"无法读取 Cookie 文件：{error}") from error
    meaningful = [line.strip() for line in content.splitlines() if line.strip()]
    if any(line.startswith("# Netscape HTTP Cookie File") for line in meaningful):
        return "netscape"
    if any(line.count("\t") >= 6 for line in meaningful):
        return "netscape"
    if ";" in content and "=" in content:
        pairs = [part.strip() for part in content.replace("\r", "").replace("\n", "").split(";") if "=" in part]
        if pairs:
            return "header"
    raise ValueError("Cookie 文件格式不正确。请选择 Netscape cookies.txt，或包含 name=value; 的浏览器 Cookie 文本。")


def make_runtime_cookie_file(source_path: str, source_url: str, task_id: str) -> Path | None:
    """Convert a copied Cookie request-header into a temporary Netscape file for yt-dlp."""
    kind = cookie_file_kind(source_path)
    if kind == "netscape":
        return None
    try:
        content = Path(source_path).read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        content = Path(source_path).read_text(encoding="utf-8", errors="replace")
    hostname = urlparse(source_url).hostname or ""
    if not hostname:
        raise ValueError("无法确定 Cookie 所属网站")
    domain = ".douyin.com" if hostname.endswith(".douyin.com") or hostname == "douyin.com" else "." + hostname.lstrip(".")
    content = re.sub(r"^\s*cookie\s*:\s*", "", content, flags=re.IGNORECASE)
    pairs: list[tuple[str, str]] = []
    for part in content.replace("\r", "").replace("\n", "").split(";"):
        name, separator, value = part.strip().partition("=")
        if not separator or not name or any(char.isspace() for char in name):
            continue
        pairs.append((name, value))
    if not pairs:
        raise ValueError("Cookie 文本中没有可用的 name=value 项")
    ensure_app_dirs()
    runtime_path = TEMP_COOKIE_DIR / f"{task_id}-{uuid.uuid4().hex}.cookies.txt"
    lines = ["# Netscape HTTP Cookie File", "# Temporary file generated locally by Omini Toolbox"]
    lines.extend(f"{domain}\tTRUE\t/\tTRUE\t0\t{name}\t{value}" for name, value in pairs)
    runtime_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return runtime_path


def cookie_names(path: str) -> set[str]:
    """Read cookie names only, never returning or logging cookie values."""
    try:
        content = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return set()
    names: set[str] = set()
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if any(line.startswith("# Netscape HTTP Cookie File") for line in lines):
        for line in lines:
            if line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) >= 7:
                names.add(fields[5])
        return names
    content = content.replace("\r", "").replace("\n", "")
    for part in content.split(";"):
        name, separator, _ = part.strip().partition("=")
        if separator:
            name = name.removeprefix("Cookie:").strip()
            if name and not any(char.isspace() for char in name):
                names.add(name)
    return names


def is_douyin_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "douyin.com" or host.endswith(".douyin.com")


def redact_log_line(line: str) -> str:
    """Prevent cookie parser diagnostics and HTTP headers from persisting secrets."""
    lowered = line.lower()
    if "skipping cookie file entry" in lowered:
        return "[Cookie file diagnostic redacted: the downloader included cookie values]"
    if re.search(r"(?:^|\s)(?:cookie|set-cookie|authorization):\s*", line, re.I):
        return "[Sensitive authentication header redacted]"
    line = re.sub(
        r"(?i)\b(sessionid(?:_ss)?|sid_tt|sid_guard|ttwid|odin_tt|s_v_web_id|msToken|passport_csrf_token|x_tt_token)=([^;\s'\"]+)",
        r"\1=[REDACTED]", line,
    )
    return line


def load_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return fallback


def save_json(path: Path, value: Any) -> None:
    ensure_app_dirs()
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


@dataclass
class DownloadTask:
    id: str
    type: str
    source: str
    output_dir: str
    options: dict[str, Any] = field(default_factory=dict)
    title: str = ""
    status: str = "queued"
    progress: float = 0.0
    speed: str = "—"
    eta: str = "—"
    error: str = ""
    created_at: str = field(default_factory=now_text)
    updated_at: str = field(default_factory=now_text)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DownloadTask":
        allowed = {key: value for key, value in data.items() if key in cls.__dataclass_fields__}
        task = cls(**allowed)
        if task.status == "running":
            task.status = "paused"
        return task


class EngineError(RuntimeError):
    pass


def find_engine(name: str) -> str:
    if name == "ffmpeg":
        try:
            configured = load_json(SETTINGS_FILE, {}).get("ffmpeg_path", "")
            excluded = [resource_path("tools"), Path(__file__).resolve().parent / "tools",
                        Path(sys.executable).resolve().parent / "tools"]
            return str(resolve_ffmpeg(configured, excluded))
        except ValueError as error:
            raise EngineError(str(error)) from error
    executable = f"{name}.exe" if os.name == "nt" else name
    if getattr(sys, "frozen", False):
        adjacent = Path(sys.executable).resolve().parent / "tools" / executable
        if adjacent.exists():
            return str(adjacent)
    bundled = resource_path(f"tools/{executable}")
    if bundled.exists():
        return str(bundled)
    local = Path(__file__).resolve().parent / "tools" / executable
    if local.exists():
        return str(local)
    found = shutil.which(executable) or shutil.which(name)
    if found:
        return found
    raise EngineError(f"未找到 {executable}。请使用完整发布包，或将该工具加入系统 PATH。")


def engine_environment(ffmpeg_path: str | None = None) -> dict[str, str]:
    """Expose local FFmpeg and packaged download engines to child processes."""
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    candidates = [resource_path("tools"), Path(__file__).resolve().parent / "tools"]
    if getattr(sys, "frozen", False):
        candidates.insert(0, Path(sys.executable).resolve().parent / "tools")
    paths = [str(path) for path in candidates if path.exists()]
    if not ffmpeg_path:
        configured = load_json(SETTINGS_FILE, {}).get("ffmpeg_path", "")
        if configured:
            try:
                ffmpeg_path = str(resolve_ffmpeg(configured))
            except ValueError:
                pass  # Tasks that do not require FFmpeg still work.
    if ffmpeg_path:
        paths.insert(0, str(Path(ffmpeg_path).parent))
    if paths:
        environment["PATH"] = os.pathsep.join(paths + [environment.get("PATH", "")])
    return environment


def safe_name(source: str, kind: str) -> str:
    compact = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", source.replace("https://", "").replace("http://", ""))
    return f"{TYPE_TEXT.get(kind, kind)}_{compact[:48] or uuid.uuid4().hex[:8]}"


def build_command(
    task: DownloadTask,
    cookie_file: str | None = None,
    cookies_from_browser: str | None = None,
    media_info_file: str | None = None,
) -> list[str]:
    output = Path(task.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    options = task.options
    needs_ffmpeg = task.type in {"video", "audio", "m3u8", "spotify"} or (
        task.type == "gallery" and is_douyin_url(task.source) and not media_info_file)
    image_note = options.get("content_kind") == "images" or re.search(r"/(?:share/)?note/\d+", task.source)
    ffmpeg = find_engine("ffmpeg") if needs_ffmpeg and not media_info_file and not image_note else None
    ffmpeg_args = ["--ffmpeg-location", ffmpeg] if ffmpeg else []

    if task.type == "m3u8":
        yt_dlp = find_engine("yt-dlp")
        filename = str(options.get("filename", "")).strip()
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename).strip(" .")
        if filename:
            suffix = Path(filename).suffix.lower()
            if suffix in {".mp4", ".mkv", ".mov", ".m4v", ".webm", ".ts"}:
                filename = filename[:-len(suffix)].rstrip(" .")
            filename = filename[:180]
            output_template = str(output / f"{filename}.%(ext)s")
        else:
            output_template = str(output / "%(title)s.%(ext)s")
        return [
            yt_dlp, *ffmpeg_args, "--encoding", "utf-8", "--newline", "--continue", "--no-playlist",
            "--format", "bv*+ba/b", "--merge-output-format", "mp4",
            "--concurrent-fragments", "4", "--fragment-retries", "10",
            "--output", output_template, task.source,
        ]

    if task.type == "video" or (task.type == "gallery" and is_douyin_url(task.source)):
        yt_dlp = find_engine("yt-dlp")
        quality = options.get("quality", "best")
        format_map = {
            "最佳": "bv*+ba/b", "2160p": "bv[height<=2160]+ba/b",
            "1080p": "bv[height<=1080]+ba/b", "720p": "bv[height<=720]+ba/b",
            "480p": "bv[height<=480]+ba/b",
        }
        command = [yt_dlp, *ffmpeg_args, "--encoding", "utf-8", "--newline", "--continue", "-f", format_map.get(quality, "bv*+ba/b"),
                   "--merge-output-format", "mp4", "-o", str(output / "%(title)s.%(ext)s")]
        if options.get("subtitles"):
            command += ["--write-subs", "--sub-langs", "zh.*,en.*"]
        if options.get("metadata"):
            command.append("--embed-metadata")
        if options.get("thumbnail"):
            command.append("--embed-thumbnail")
        if options.get("playlist"):
            command.append("--yes-playlist")
        else:
            command.append("--no-playlist")
        if cookies_from_browser:
            command += ["--cookies-from-browser", cookies_from_browser]
        elif cookie_file or options.get("cookies"):
            command += ["--cookies", cookie_file or options["cookies"]]
        # Preserve entries when reading a browser-resolved image playlist.
        # yt-dlp's default clean-infojson mode otherwise discards them and re-extracts.
        return command + (["--no-clean-info-json", "--load-info-json", media_info_file] if media_info_file else [task.source])

    if task.type == "audio":
        yt_dlp = find_engine("yt-dlp")
        audio_format = options.get("format", "mp3").lower()
        command = [yt_dlp, *ffmpeg_args, "--encoding", "utf-8", "--newline", "--continue", "-x", "--audio-format", audio_format,
                   "--audio-quality", "0", "-o", str(output / "%(title)s.%(ext)s")]
        if cookies_from_browser:
            command += ["--cookies-from-browser", cookies_from_browser]
        elif cookie_file or options.get("cookies"):
            command += ["--cookies", cookie_file or options["cookies"]]
        return command + [task.source]

    if task.type == "spotify":
        return [find_engine("spotdl"), task.source, "--ffmpeg", ffmpeg, "--output", str(output / "{artists} - {title}.{output-ext}")]

    if task.type == "file":
        aria2 = find_engine("aria2c")
        command = [aria2, "--continue=true", "--auto-file-renaming=false", "-x", "16", "-s", "16",
                   "-k", "1M", "--max-tries=3", "--retry-wait=3", "--summary-interval=1", "-d", str(output)]
        if options.get("filename"):
            command += ["-o", options["filename"]]
        return command + [task.source]

    if task.type == "gallery":
        return [find_engine("gallery-dl"), "-d", str(output), task.source]

    if task.type == "torrent":
        return [find_engine("aria2c"), "--seed-time=0", "--continue=true", "--summary-interval=1",
                "-d", str(output), task.source]

    if task.type == "subtitle":
        yt_dlp = find_engine("yt-dlp")
        language = options.get("language", "zh.*,en.*")
        target = task.source if re.match(r"https?://", task.source, re.I) else f"ytsearch1:{task.source}"
        command = [yt_dlp, "--encoding", "utf-8", "--newline", "--skip-download", "--write-subs", "--write-auto-subs",
                   "--sub-langs", language, "-o", str(output / "%(title)s.%(ext)s")]
        if cookies_from_browser:
            command += ["--cookies-from-browser", cookies_from_browser]
        elif cookie_file or options.get("cookies"):
            command += ["--cookies", cookie_file or options["cookies"]]
        return command + [target]

    raise EngineError("未知的任务类型")


class DownloadWorker(QThread):
    output = Signal(str, str)
    progress = Signal(str, float, str, str)
    finished_task = Signal(str, str, str, str)

    def __init__(self, task: DownloadTask):
        super().__init__()
        self.task = task
        self._process: subprocess.Popen[str] | None = None
        self._stop_requested = False
        self._pause_requested = False
        self._cookie_hint = ""
        self._runtime_cookie_file: Path | None = None
        self._runtime_media_file: Path | None = None
        self._process_lock = threading.Lock()

    def request_stop(self, pause: bool = False) -> None:
        with self._process_lock:
            self._stop_requested = True
            self._pause_requested = pause
            process = self._process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
            # Never wait for a subprocess on the GUI thread.
            def kill_if_still_running() -> None:
                try:
                    if process.poll() is None:
                        process.kill()
                except OSError:
                    pass
            timer = threading.Timer(3, kill_if_still_running)
            timer.daemon = True
            timer.start()

    def run(self) -> None:
        try:
            cookie_file = self.task.options.get("cookies")
            browser_cookies = self.task.options.get("cookies_from_browser")
            if cookie_file and not browser_cookies:
                self._runtime_cookie_file = make_runtime_cookie_file(cookie_file, self.task.source, self.task.id)
                cookie_file = str(self._runtime_cookie_file or cookie_file)
            if self.task.options.get("_resolved_media"):
                directory = APP_DIR / "runtime-media"
                directory.mkdir(parents=True, exist_ok=True)
                self._runtime_media_file = directory / f"{self.task.id}.json"
                self._runtime_media_file.write_text(json.dumps(self.task.options["_resolved_media"], ensure_ascii=False), encoding="utf-8")
            command = build_command(
                self.task,
                cookie_file=cookie_file,
                cookies_from_browser=browser_cookies,
                media_info_file=str(self._runtime_media_file) if self._runtime_media_file else None,
            )
            self.output.emit(self.task.id, "$ " + subprocess.list2cmdline(command))
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            local_ffmpeg = next((command[index + 1] for index, argument in enumerate(command[:-1])
                                 if argument in {"--ffmpeg-location", "--ffmpeg"}), None)
            environment = engine_environment(local_ffmpeg)
            with self._process_lock:
                if self._stop_requested:
                    self.finished_task.emit(self.task.id, "paused" if self._pause_requested else "cancelled", "", "")
                    return
                self._process = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", creationflags=flags, env=environment,
                )
            assert self._process.stdout is not None
            for line in self._process.stdout:
                line = line.rstrip()
                if line:
                    self.output.emit(self.task.id, line)
                    self._detect_cookie_requirement(line)
                    self._parse_progress(line)
            code = self._process.wait()
            if self._pause_requested:
                self.finished_task.emit(self.task.id, "paused", "", "")
            elif self._stop_requested:
                self.finished_task.emit(self.task.id, "cancelled", "", "")
            elif code == 0:
                self.progress.emit(self.task.id, 100.0, "—", "—")
                self.finished_task.emit(self.task.id, "completed", "", "")
            else:
                hint = self._cookie_hint if self.task.type in {"video", "audio", "subtitle", "gallery"} else ""
                self.finished_task.emit(self.task.id, "failed", f"下载引擎退出，代码 {code}", hint)
        except (EngineError, OSError) as error:
            self.finished_task.emit(self.task.id, "failed", str(error), "")
        except Exception as error:  # Keep unexpected engine errors visible in the task list.
            self.finished_task.emit(self.task.id, "failed", f"执行异常：{error}", "")
        finally:
            with self._process_lock:
                self._process = None
            if self._runtime_cookie_file:
                try:
                    self._runtime_cookie_file.unlink(missing_ok=True)
                except OSError:
                    pass
            if self._runtime_media_file:
                try:
                    self._runtime_media_file.unlink(missing_ok=True)
                except OSError:
                    pass

    def _parse_progress(self, line: str) -> None:
        yt = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%.*?(?:at\s+([^\s]+))?.*?(?:ETA\s+(.+))?$", line)
        if yt:
            self.progress.emit(self.task.id, float(yt.group(1)), yt.group(2) or "—", yt.group(3) or "—")
            return
        aria = re.search(r"\((\d+)%\).*?DL:([^\s]+).*?(?:ETA:([^\]]+))?", line)
        if aria:
            self.progress.emit(self.task.id, float(aria.group(1)), aria.group(2), aria.group(3) or "—")

    def _detect_cookie_requirement(self, line: str) -> None:
        if "OMINI_BROWSER_REQUIRED" in line:
            self._cookie_hint = "OMINI_BROWSER_REQUIRED"
            return
        lowered = line.lower()
        markers = ("fresh cookies", "login required", "sign in", "authentication required", "请登录", "需要登录", "登录后")
        douyin_denial = is_douyin_url(self.task.source) and (
            "failed to parse json" in lowered
            or ("403" in lowered and ("error" in lowered or "forbidden" in lowered))
        )
        if any(marker in lowered for marker in markers) or douyin_denial:
            self._cookie_hint = redact_log_line(line)


class TaskManager(QObject):
    task_added = Signal(object)
    task_updated = Signal(object)
    task_log = Signal(str, str)
    cookie_required = Signal(str, str)
    browser_required = Signal(str)
    queue_cleared = Signal(int)

    def __init__(self, settings: dict[str, Any]):
        super().__init__()
        self.settings = settings
        self.tasks = [DownloadTask.from_dict(item) for item in load_json(TASKS_FILE, [])]
        self.workers: dict[str, DownloadWorker] = {}
        self._lock = threading.RLock()

    def save_tasks(self) -> None:
        save_json(TASKS_FILE, [asdict(task) for task in self.tasks])

    def clear_queue(self) -> int:
        """Remove queue records, but retain media, logs and live QThread references."""
        with self._lock:
            count = len(self.tasks)
            if self.tasks:
                backup_dir = APP_DIR / "queue-backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup = backup_dir / f"tasks-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.json"
                save_json(backup, [asdict(task) for task in self.tasks])
            save_json(TASKS_FILE, [])
            self.tasks.clear()
            for worker in self.workers.values():
                worker.request_stop()
            self.queue_cleared.emit(count)
            return count

    def add(self, task_type: str, source: str, output_dir: str, options: dict[str, Any]) -> DownloadTask:
        source = extract_usable_source(source, allow_search=task_type == "subtitle")
        if not source:
            raise ValueError("未识别到可用链接。请粘贴完整链接，或在字幕页输入搜索关键词。")
        if task_type == "m3u8":
            parsed = urlparse(source)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
                raise ValueError("请输入有效的 HTTP 或 HTTPS M3U8 播放列表链接。")
        if not output_dir.strip():
            raise ValueError("请选择保存目录")
        task = DownloadTask(
            id=uuid.uuid4().hex, type=task_type, source=source, output_dir=output_dir,
            options=options, title=safe_name(source, task_type),
        )
        with self._lock:
            self.tasks.insert(0, task)
            self.save_tasks()
            self.task_added.emit(task)
            self._start_waiting_locked()
        return task

    def pause(self, task_id: str) -> None:
        with self._lock:
            worker = self.workers.get(task_id)
            if worker:
                worker.request_stop(pause=True)
            else:
                self._set_status(task_id, "paused")

    def resume(self, task_id: str) -> None:
        with self._lock:
            task = self._task(task_id)
            if task and task.status in {"paused", "failed", "cancelled"}:
                task.status, task.error, task.updated_at = "queued", "", now_text()
                task.progress, task.speed, task.eta = 0.0, "—", "—"
                self._publish(task)
                self._start_waiting_locked()

    def cancel(self, task_id: str) -> None:
        with self._lock:
            worker = self.workers.get(task_id)
            if worker:
                worker.request_stop(pause=False)
            else:
                self._set_status(task_id, "cancelled")

    def retry(self, task_id: str) -> None:
        self.resume(task_id)

    def attach_browser_media_and_retry(self, task_id: str, info: dict) -> None:
        with self._lock:
            task = self._task(task_id)
            if not task:
                return
            task.options["_resolved_media"] = info
            task.options["content_kind"] = "images"
            task.title = f"图文 · {info['title']}"
            task.status, task.error, task.updated_at = "queued", "", now_text()
            task.progress, task.speed, task.eta = 0.0, "—", "—"
            self._on_output(task_id, f"浏览器解析完成：{len(info['entries'])} 张图片，将保存图片原文件（不是视频）。")
            self._publish(task)
            self._start_waiting_locked()

    def attach_cookie_and_retry(self, task_id: str, cookie_path: str) -> None:
        with self._lock:
            task = self._task(task_id)
            if not task:
                return
            cookie_file_kind(cookie_path)
            task.options["cookies"] = cookie_path
            task.options.pop("cookies_from_browser", None)
            task.status, task.error, task.updated_at = "queued", "", now_text()
            self._publish(task)
            self._start_waiting_locked()

    def attach_browser_cookies_and_retry(self, task_id: str, browser: str) -> None:
        if browser not in {"chrome", "edge", "firefox"}:
            raise ValueError("不支持的浏览器 Cookie 来源")
        with self._lock:
            task = self._task(task_id)
            if not task:
                return
            task.options.pop("cookies", None)
            task.options["cookies_from_browser"] = browser
            task.status, task.error, task.updated_at = "queued", "", now_text()
            self._publish(task)
            self._start_waiting_locked()

    def output_for(self, task_id: str) -> Path:
        return LOG_DIR / f"{task_id}.log"

    def _start_waiting_locked(self) -> None:
        if not enabled_features(self.settings)["downloads"]:
            return
        maximum = max(1, int(self.settings.get("concurrency", 2)))
        running = len(self.workers)
        for task in reversed(self.tasks):
            if running >= maximum:
                break
            if task.status != "queued" or task.id in self.workers:
                continue
            task.status, task.updated_at = "running", now_text()
            worker = DownloadWorker(task)
            worker.output.connect(self._on_output)
            worker.progress.connect(self._on_progress)
            worker.finished_task.connect(self._on_finished)
            # Keep the QThread alive until Qt has fully left DownloadWorker.run().
            # Releasing it from finished_task can destroy a still-running thread.
            worker.finished.connect(self._on_worker_thread_finished)
            self.workers[task.id] = worker
            self._publish(task)
            worker.start()
            running += 1

    def _on_output(self, task_id: str, line: str) -> None:
        task = self._task(task_id)
        if not task:
            return
        count = re.search(r"识别到图文作品，下载 (\d+) 张图片", line)
        if count:
            task.options["content_kind"] = "images"
            task.title = f"抖音图文（{count.group(1)} 张图片）"
            self._publish(task)
        ensure_app_dirs()
        line = redact_log_line(line)
        with self.output_for(task_id).open("a", encoding="utf-8") as log:
            log.write(f"[{now_text()}] {line}\n")
        self.task_log.emit(task_id, line)

    def _on_progress(self, task_id: str, progress: float, speed: str, eta: str) -> None:
        with self._lock:
            task = self._task(task_id)
            if task:
                task.progress, task.speed, task.eta, task.updated_at = progress, speed, eta, now_text()
                self._publish(task)

    def _on_finished(self, task_id: str, status: str, error: str, cookie_hint: str) -> None:
        with self._lock:
            task = self._task(task_id)
            if task:
                task.status, task.error, task.updated_at = status, error, now_text()
                if error:
                    self._on_output(task_id, f"错误：{error}")
                self._publish(task)
                if status == "failed" and cookie_hint:
                    if cookie_hint == "OMINI_BROWSER_REQUIRED" and task.type in {"video", "gallery"}:
                        self.browser_required.emit(task_id)
                    else:
                        self.cookie_required.emit(task_id, cookie_hint)

    def _on_worker_thread_finished(self) -> None:
        worker = self.sender()
        if not isinstance(worker, DownloadWorker):
            return
        task_id = worker.task.id
        with self._lock:
            if self.workers.get(task_id) is worker:
                self.workers.pop(task_id)
            worker.deleteLater()
            self._start_waiting_locked()

    def _set_status(self, task_id: str, status: str) -> None:
        task = self._task(task_id)
        if task:
            task.status, task.updated_at = status, now_text()
            self._publish(task)

    def _task(self, task_id: str) -> DownloadTask | None:
        return next((task for task in self.tasks if task.id == task_id), None)

    def _publish(self, task: DownloadTask) -> None:
        self.save_tasks()
        self.task_updated.emit(task)


class FolderField(QWidget):
    def __init__(self, value: str = ""):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(value)
        self.button = QToolButton(text="浏览")
        self.button.clicked.connect(self.choose)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button)

    def choose(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择保存目录", self.edit.text() or default_download_dir())
        if path:
            self.edit.setText(path)

    def text(self) -> str:
        return self.edit.text().strip()


class DownloadPage(QWidget):
    def __init__(self, manager: TaskManager, settings: dict[str, Any]):
        super().__init__()
        self.manager = manager
        self.settings = settings
        self.rows: dict[str, int] = {}
        self.current_task_id: str | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        title = QLabel("下载中心")
        title.setObjectName("pageTitle")
        subtitle = QLabel("输入链接、选择保存位置，然后交给下载队列处理。")
        subtitle.setObjectName("muted")
        root.addWidget(title)
        root.addWidget(subtitle)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._video_tab(), "视频")
        self.tabs.addTab(self._m3u8_tab(), "M3U8")
        self.tabs.addTab(self._audio_tab(), "音频")
        self.tabs.addTab(self._file_tab(), "文件")
        self.tabs.addTab(self._gallery_tab(), "图库")
        self.tabs.addTab(self._torrent_tab(), "磁力/Torrent")
        self.tabs.addTab(self._subtitle_tab(), "字幕")
        self.tabs.addTab(self._guide_tab(), "资源导航")
        root.addWidget(self.tabs)

        queue_label = QLabel("任务队列")
        queue_label.setObjectName("sectionTitle")
        root.addWidget(queue_label)
        queue_toolbar = QHBoxLayout()
        self.pause_button = QPushButton("暂停")
        self.resume_button = QPushButton("继续")
        self.cancel_button = QPushButton("取消")
        self.retry_button = QPushButton("重试")
        self.open_button = QPushButton("打开目录")
        self.clear_button = QPushButton("清空队列")
        self.clear_button.setToolTip("停止所有任务并清空队列记录，保留已下载文件；记录会自动备份")
        self.pause_button.setToolTip("先选择等待中或下载中的任务，再暂停")
        self.resume_button.setToolTip("先选择已暂停、失败或已取消的任务，再继续")
        self.cancel_button.setToolTip("先选择等待中、下载中或已暂停的任务，再取消")
        self.retry_button.setToolTip("先选择失败任务，再重试；新建任务请点击上方“加入下载队列”")
        for button, method in ((self.pause_button, self.pause_selected), (self.resume_button, self.resume_selected),
                               (self.cancel_button, self.cancel_selected), (self.retry_button, self.retry_selected),
                               (self.open_button, self.open_selected), (self.clear_button, self.clear_queue)):
            button.clicked.connect(method)
            queue_toolbar.addWidget(button)
        queue_toolbar.addStretch()
        root.addLayout(queue_toolbar)
        self.queue_feedback = QLabel("选择任务可暂停、继续、取消或重试；清空队列保留下载文件。")
        self.queue_feedback.setObjectName("muted")
        self.queue_feedback.setWordWrap(True)
        root.addWidget(self.queue_feedback)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["状态", "任务", "类型", "进度", "速度", "剩余时间", "保存位置"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._on_selection)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 75)
        self.table.setColumnWidth(1, 220)
        self.table.setColumnWidth(2, 90)
        self.table.setColumnWidth(3, 80)
        self.table.setColumnWidth(4, 90)
        self.table.setColumnWidth(5, 90)
        splitter.addWidget(self.table)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("选择一个任务以查看实时日志")
        splitter.addWidget(self.log)
        splitter.setSizes([280, 140])
        root.addWidget(splitter, 1)

        manager.task_added.connect(self.add_new_task)
        manager.task_updated.connect(self.update_row)
        manager.task_log.connect(self.append_log)
        manager.cookie_required.connect(self.request_cookie)
        self.browser_dialogs = {}
        manager.browser_required.connect(self.request_browser)
        manager.queue_cleared.connect(self._queue_cleared)
        for task in manager.tasks:
            self.add_row(task)
        self._update_actions(None)

    def _form(self) -> tuple[QWidget, QFormLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        box = QGroupBox("新建下载任务")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(box)
        layout.addStretch()
        return page, form

    def _add_submit(self, form: QFormLayout, callback: Any, text: str = "加入下载队列") -> None:
        button = QPushButton(text)
        button.setObjectName("primary")
        button.clicked.connect(callback)
        form.addRow("", button)

    def _video_tab(self) -> QWidget:
        page, form = self._form()
        self.video_url = QLineEdit(); self.video_url.setPlaceholderText("粘贴视频或播放列表链接")
        self.video_quality = QComboBox(); self.video_quality.addItems(["最佳", "2160p", "1080p", "720p", "480p"])
        self.video_dir = FolderField(self.settings["download_dir"])
        self.video_subs = QCheckBox("下载字幕（中文、英文）")
        self.video_metadata = QCheckBox("嵌入媒体元数据"); self.video_metadata.setChecked(True)
        self.video_thumbnail = QCheckBox("嵌入封面"); self.video_thumbnail.setChecked(True)
        self.video_playlist = QCheckBox("下载整个播放列表")
        form.addRow("视频链接", self.video_url); form.addRow("清晰度", self.video_quality); form.addRow("保存目录", self.video_dir)
        form.addRow("", self.video_subs); form.addRow("", self.video_metadata)
        form.addRow("", self.video_thumbnail); form.addRow("", self.video_playlist)
        self._add_submit(form, self.submit_video)
        return page

    def _audio_tab(self) -> QWidget:
        page, form = self._form()
        self.audio_url = QLineEdit(); self.audio_url.setPlaceholderText("粘贴视频链接或 Spotify 链接")
        self.audio_format = QComboBox(); self.audio_format.addItems(["MP3", "Opus", "FLAC"])
        self.audio_dir = FolderField(self.settings["download_dir"])
        form.addRow("音频链接", self.audio_url); form.addRow("输出格式", self.audio_format); form.addRow("保存目录", self.audio_dir)
        self._add_submit(form, self.submit_audio)
        return page

    def _m3u8_tab(self) -> QWidget:
        page, form = self._form()
        self.m3u8_url = QLineEdit()
        self.m3u8_url.setPlaceholderText("粘贴 HTTP/HTTPS 的 .m3u8 播放列表链接")
        self.m3u8_filename = QLineEdit()
        self.m3u8_filename.setPlaceholderText("可选；留空时按播放列表信息自动命名")
        self.m3u8_dir = FolderField(self.settings["download_dir"])
        hint = QLabel("使用 yt-dlp 下载 HLS 分片，支持主播放列表并合并为 MP4；文件扩展名由下载内容自动确定。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        form.addRow("M3U8 地址", self.m3u8_url)
        form.addRow("文件名", self.m3u8_filename)
        form.addRow("保存目录", self.m3u8_dir)
        form.addRow("", hint)
        self._add_submit(form, self.submit_m3u8)
        return page

    def _file_tab(self) -> QWidget:
        page, form = self._form()
        self.file_url = QLineEdit(); self.file_url.setPlaceholderText("粘贴直链，例如 https://example.com/file.zip")
        self.file_name = QLineEdit(); self.file_name.setPlaceholderText("可选：自定义文件名")
        self.file_dir = FolderField(self.settings["download_dir"])
        form.addRow("文件链接", self.file_url); form.addRow("文件名", self.file_name); form.addRow("保存目录", self.file_dir)
        self._add_submit(form, self.submit_file)
        return page

    def _gallery_tab(self) -> QWidget:
        page, form = self._form()
        self.gallery_url = QLineEdit(); self.gallery_url.setPlaceholderText("粘贴公开图库、作者页或作品页链接")
        self.gallery_dir = FolderField(self.settings["download_dir"])
        form.addRow("图库链接", self.gallery_url); form.addRow("保存目录", self.gallery_dir)
        self._add_submit(form, self.submit_gallery)
        return page

    def _torrent_tab(self) -> QWidget:
        page, form = self._form()
        self.torrent_source = QLineEdit(); self.torrent_source.setPlaceholderText("粘贴 magnet 链接或 .torrent 文件/URL")
        self.torrent_dir = FolderField(self.settings["download_dir"])
        form.addRow("磁力/Torrent", self.torrent_source); form.addRow("保存目录", self.torrent_dir)
        self._add_submit(form, self.submit_torrent)
        return page

    def _subtitle_tab(self) -> QWidget:
        page, form = self._form()
        self.subtitle_source = QLineEdit(); self.subtitle_source.setPlaceholderText("粘贴视频链接，或输入要搜索的视频标题")
        self.subtitle_language = QLineEdit("zh.*,en.*")
        self.subtitle_dir = FolderField(self.settings["download_dir"])
        form.addRow("链接或关键词", self.subtitle_source); form.addRow("字幕语言", self.subtitle_language); form.addRow("保存目录", self.subtitle_dir)
        self._add_submit(form, self.submit_subtitle)
        return page

    def _guide_tab(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setHtml("""
        <h3>资源导航</h3>
        <p>下载中心使用本地下载引擎处理您输入的链接。遇到失败时，请确认链接可公开访问、保存位置可写入且网络连接正常。</p>
        <h4>常用工具</h4>
        <ul><li><a href='https://github.com/yt-dlp/yt-dlp'>yt-dlp：视频、音频、字幕和 M3U8/HLS 分片</a></li>
        <li><a href='https://aria2.github.io/'>aria2：直链、续传及 BitTorrent</a></li>
        <li><a href='https://github.com/mikf/gallery-dl'>gallery-dl：公开图库批量下载</a></li>
        <li><a href='https://github.com/spotDL/spotify-downloader'>spotDL：Spotify 链接音频下载</a></li></ul>
        <p>抖音公开视频优先从公开分享页解析，通常不需要 Cookie。确实需要登录或验证时会弹出提示，可选择本地 cookies.txt；只有主动点击浏览器会话按钮时才读取该浏览器的 Cookie。</p>
        """)
        layout.addWidget(browser)
        return page

    def submit_video(self) -> None:
        self._submit("video", self.video_url.text(), self.video_dir.text(), {
            "quality": self.video_quality.currentText(), "subtitles": self.video_subs.isChecked(),
            "metadata": self.video_metadata.isChecked(), "thumbnail": self.video_thumbnail.isChecked(),
            "playlist": self.video_playlist.isChecked(),
        })

    def submit_audio(self) -> None:
        source = self.audio_url.text()
        if "spotify.com" in source.lower():
            QMessageBox.information(self, "Spotify 下载", "Spotify 链接将由内置 spotdl 引擎处理；输出格式由该引擎自动选择。")
            self._submit("spotify", source, self.audio_dir.text(), {"format": self.audio_format.currentText()})
        else:
            self._submit("audio", source, self.audio_dir.text(), {"format": self.audio_format.currentText()})

    def submit_m3u8(self) -> None:
        self._submit("m3u8", self.m3u8_url.text(), self.m3u8_dir.text(), {
            "filename": self.m3u8_filename.text().strip(),
        })

    def submit_file(self) -> None:
        self._submit("file", self.file_url.text(), self.file_dir.text(), {"filename": self.file_name.text().strip()})

    def submit_gallery(self) -> None:
        self._submit("gallery", self.gallery_url.text(), self.gallery_dir.text(), {})

    def submit_torrent(self) -> None:
        self._submit("torrent", self.torrent_source.text(), self.torrent_dir.text(), {})

    def submit_subtitle(self) -> None:
        self._submit("subtitle", self.subtitle_source.text(), self.subtitle_dir.text(), {"language": self.subtitle_language.text().strip() or "zh.*,en.*"})

    def _submit(self, task_type: str, source: str, output_dir: str, options: dict[str, Any]) -> None:
        try:
            self.manager.add(task_type, source, output_dir, options)
        except ValueError as error:
            QMessageBox.warning(self, "无法创建任务", str(error))

    def request_browser(self, task_id: str) -> None:
        task = self.manager._task(task_id)
        if not task:
            return
        if task_id in self.browser_dialogs:
            self.browser_dialogs[task_id].raise_()
            return
        from douyin_browser import DouyinBrowserDialog
        dialog = DouyinBrowserDialog(task.source, self)
        self.browser_dialogs[task_id] = dialog
        dialog.media_ready.connect(lambda info: self.manager.attach_browser_media_and_retry(task_id, info))
        dialog.finished.connect(lambda _: self.browser_dialogs.pop(task_id, None))
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.queue_feedback.setText("请在系统浏览器查看图文，可导入公开图片地址加入下载队列。")
        dialog.show()

    def request_cookie(self, task_id: str, engine_message: str) -> None:
        task = self.manager._task(task_id)
        if not task:
            return
        has_cookie_file = bool(task.options.get("cookies"))
        douyin = is_douyin_url(task.source)
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Information)
        dialog.setWindowTitle("抖音需要登录或验证" if douyin else "此链接需要登录")
        dialog.setText("公开分享页与常规视频接口均未解析成功，请确认视频页面可正常播放。" if douyin else "该网站要求登录或额外验证，请提供当前会话的 Cookie。")
        if task.options.get("cookies_from_browser"):
            detail = (
                f"刚才读取 {task.options['cookies_from_browser']} 会话仍被网站拒绝。"
                "请先在浏览器打开视频并完成安全验证，再重试；也可以改选另一个浏览器会话。"
            )
        elif has_cookie_file:
            detail = "已尝试选中的 Cookie 文件。请先在浏览器打开视频并完成页面验证，再导出新的 cookies.txt，或主动选择同一个浏览器会话重试。"
        else:
            detail = "先在常用浏览器打开这个视频并完成页面验证，再选择同一个浏览器读取本机登录态。"
        dialog.setInformativeText(
            detail + " 浏览器 Cookie 只在你点击对应按钮后由 yt-dlp 在本机读取。"
        )
        dialog.setDetailedText(engine_message)
        open_link = dialog.addButton("先在浏览器打开此视频", QMessageBox.ButtonRole.ActionRole)
        chrome = dialog.addButton("用 Chrome 会话重试", QMessageBox.ButtonRole.AcceptRole)
        edge = dialog.addButton("用 Edge 会话重试", QMessageBox.ButtonRole.AcceptRole)
        choose_file = dialog.addButton("选择 cookies.txt", QMessageBox.ButtonRole.ActionRole)
        dialog.addButton("暂不处理", QMessageBox.ButtonRole.RejectRole)
        while True:
            dialog.exec()
            clicked = dialog.clickedButton()
            if clicked is open_link:
                QDesktopServices.openUrl(QUrl(task.source))
                continue
            if clicked is chrome or clicked is edge:
                browser = "chrome" if clicked is chrome else "edge"
                try:
                    self.manager.attach_browser_cookies_and_retry(task_id, browser)
                except ValueError as error:
                    QMessageBox.warning(self, "浏览器 Cookie 不可用", str(error))
                return
            if clicked is choose_file:
                path, _ = QFileDialog.getOpenFileName(
                    self, "选择 cookies.txt", "", "Cookie 文件 (*.txt);;所有文件 (*.*)"
                )
                if not path:
                    return
                try:
                    self.manager.attach_cookie_and_retry(task_id, path)
                    return
                except ValueError as error:
                    QMessageBox.warning(self, "Cookie 文件不可用", str(error))
                    continue
            return

    def add_row(self, task: DownloadTask) -> None:
        if task.id in self.rows:
            self.update_row(task); return
        row = self.table.rowCount(); self.table.insertRow(row); self.rows[task.id] = row
        for column in range(7):
            self.table.setItem(row, column, QTableWidgetItem())
        self.update_row(task)

    def add_new_task(self, task: DownloadTask) -> None:
        # Historical rows must not hide a new submission below the viewport.
        self.rows = {task_id: row + 1 for task_id, row in self.rows.items()}
        self.table.insertRow(0)
        self.rows[task.id] = 0
        for column in range(7):
            self.table.setItem(0, column, QTableWidgetItem())
        self.update_row(task)
        self.table.selectRow(0)
        self.table.scrollToItem(self.table.item(0, 0))

    def clear_queue(self) -> None:
        try:
            self.manager.clear_queue()
        except OSError as error:
            QMessageBox.warning(self, "清空队列失败", f"记录备份或保存失败：{error}")

    def _queue_cleared(self, count: int) -> None:
        for dialog in list(self.browser_dialogs.values()):
            dialog.reject()
        self.current_task_id = None
        self.rows.clear()
        self.table.setRowCount(0)
        self.log.clear()
        self._update_actions(None)
        self.queue_feedback.setText(f"已清空 {count} 个任务，正在运行的任务已请求停止。下载文件和日志保留，队列记录已备份。")

    def update_row(self, task: DownloadTask) -> None:
        if not self.manager._task(task.id):
            return  # Ignore signals already queued before a bulk clear.
        if task.id not in self.rows:
            self.add_row(task); return
        row = self.rows[task.id]
        values = [STATUS_TEXT.get(task.status, task.status), task.title, "图文" if task.options.get("content_kind") == "images" else TYPE_TEXT.get(task.type, task.type),
                  f"{task.progress:.1f}%", task.speed, task.eta, task.output_dir]
        for column, value in enumerate(values):
            item = self.table.item(row, column)
            item.setText(value)
            item.setData(Qt.ItemDataRole.UserRole, task.id)
            item.setToolTip(task.error if column == 0 and task.status == "failed" else value)
        self.clear_button.setEnabled(bool(self.manager.tasks))
        if self.current_task_id == task.id:
            self._update_actions(task)

    def append_log(self, task_id: str, line: str) -> None:
        if task_id == self.current_task_id:
            self.log.append(line)

    def _on_selection(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            self.current_task_id = None
            self.log.clear()
            self._update_actions(None)
            return
        item = self.table.item(rows[0].row(), 0)
        if item is None:
            return
        task_id = item.data(Qt.ItemDataRole.UserRole)
        self.current_task_id = task_id
        task = self.manager._task(task_id)
        if task:
            self._update_actions(task)
            try:
                self.log.setPlainText(self.manager.output_for(task_id).read_text(encoding="utf-8"))
            except OSError:
                self.log.setPlainText("")

    def _update_actions(self, task: DownloadTask | None) -> None:
        status = task.status if task else ""
        self.pause_button.setEnabled(status in {"queued", "running"})
        self.resume_button.setEnabled(status in {"paused", "failed", "cancelled"})
        self.cancel_button.setEnabled(status in {"queued", "running", "paused"})
        self.retry_button.setEnabled(status == "failed")
        self.open_button.setEnabled(bool(task and task.output_dir))
        self.clear_button.setEnabled(bool(self.manager.tasks))
        if task:
            kind = "图文（保存图片）" if task.options.get("content_kind") == "images" else TYPE_TEXT.get(task.type, task.type)
            feedback = f"当前任务：{kind} · {STATUS_TEXT.get(status, status)} · {task.source}"
            if status == "completed":
                feedback += f"；文件已保存到 {task.output_dir}，可点击“打开目录”。"
            elif status == "queued":
                feedback += "；等待空闲下载通道。"
            elif status == "failed":
                feedback += f"；{task.error or '下载失败'}，可点击“重试”，下方日志显示详情。"
            self.queue_feedback.setText(feedback)

    def _selected(self) -> str | None:
        return self.current_task_id

    def pause_selected(self) -> None:
        if (task_id := self._selected()): self.manager.pause(task_id)

    def resume_selected(self) -> None:
        if (task_id := self._selected()): self.manager.resume(task_id)

    def cancel_selected(self) -> None:
        if (task_id := self._selected()): self.manager.cancel(task_id)

    def retry_selected(self) -> None:
        if (task_id := self._selected()): self.manager.retry(task_id)

    def open_selected(self) -> None:
        if (task_id := self._selected()):
            task = self.manager._task(task_id)
            if task:
                Path(task.output_dir).mkdir(parents=True, exist_ok=True)
                QDesktopServices.openUrl(QUrl.fromLocalFile(task.output_dir))


class SettingsPage(QWidget):
    settings_changed = Signal(dict)

    def __init__(self, settings: dict[str, Any]):
        super().__init__()
        self.settings = settings
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        content = QWidget(); scroll.setWidget(content); outer.addWidget(scroll)
        layout = QVBoxLayout(content); layout.setContentsMargins(32, 28, 32, 28)
        title = QLabel("设置"); title.setObjectName("pageTitle"); layout.addWidget(title)
        self.before_save = None
        self.account = AccountPanel(APP_DIR, settings, lambda value: save_json(SETTINGS_FILE, value), self)
        layout.addWidget(self.account)
        features_box = QGroupBox("可选功能加载"); features_layout = QVBoxLayout(features_box)
        note = QLabel("勾选要使用的功能，点击「保存设置」后立即生效。取消勾选会隐藏侧栏入口并停止该功能的后台任务；下载会暂停，个人数据保留。设置和邮箱账号入口始终可用。")
        note.setWordWrap(True); features_layout.addWidget(note)
        self.feature_checks = {}
        for key, label in DESKTOP_FEATURES.items():
            check = QCheckBox(label); check.setChecked(enabled_features(settings)[key])
            self.feature_checks[key] = check; features_layout.addWidget(check)
        layout.addWidget(features_box)
        box = QGroupBox("下载设置"); form = QFormLayout(box)
        self.download_dir = FolderField(settings["download_dir"])
        self.concurrency = QSpinBox(); self.concurrency.setRange(1, 6); self.concurrency.setValue(settings["concurrency"])
        self.theme = QComboBox(); self.theme.addItems(["深色", "浅色"]); self.theme.setCurrentText(settings.get("theme", "深色"))
        form.addRow("默认保存目录", self.download_dir); form.addRow("同时下载任务数", self.concurrency); form.addRow("界面主题", self.theme)
        save = QPushButton("保存设置"); save.setObjectName("primary"); save.clicked.connect(self.save)
        form.addRow("", save)
        layout.addWidget(box)
        ffmpeg_box = QGroupBox("本机 FFmpeg"); ffmpeg_form = QFormLayout(ffmpeg_box)
        self.ffmpeg_path = QLineEdit(settings.get("ffmpeg_path", ""))
        self.ffmpeg_path.setPlaceholderText("留空自动检测系统 PATH；也可选择本机 ffmpeg.exe")
        path_row = QWidget(); path_layout = QHBoxLayout(path_row); path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.addWidget(self.ffmpeg_path, 1)
        choose = QPushButton("选择程序"); choose.clicked.connect(self.choose_ffmpeg); path_layout.addWidget(choose)
        detect = QPushButton("检测"); detect.clicked.connect(self.detect_ffmpeg); path_layout.addWidget(detect)
        ffmpeg_form.addRow("FFmpeg 路径", path_row)
        self.ffmpeg_status = QLabel("视频合并、M3U8 和音频转换使用本机 FFmpeg。选择后点击上方「保存设置」生效。")
        self.ffmpeg_status.setWordWrap(True); self.ffmpeg_status.setTextFormat(Qt.TextFormat.PlainText)
        ffmpeg_form.addRow(self.ffmpeg_status)
        layout.addWidget(ffmpeg_box)
        from component_installer import ComponentPanel
        self.components = ComponentPanel(APP_DIR, self)
        self.components.ffmpeg_installed.connect(self.installed_ffmpeg)
        layout.addWidget(self.components)
        self.updates = UpdatePanel(APP_VERSION, APP_DIR / "updates", self)
        layout.addWidget(self.updates)
        layout.addStretch()

    def save(self) -> None:
        features = {key: check.isChecked() for key, check in self.feature_checks.items()}
        if self.before_save and not self.before_save(features):
            return
        try:
            self.account.selected_url()
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "网页服务地址有误", str(error))
            return
        self.settings.update({"download_dir": self.download_dir.text(), "concurrency": self.concurrency.value(), "theme": self.theme.currentText(),
                              "features": features,
                              "ffmpeg_path": self.ffmpeg_path.text().strip().strip('"')})
        save_json(SETTINGS_FILE, self.settings)
        self.settings_changed.emit(self.settings)
        QMessageBox.information(self, "设置已保存", "功能加载与侧栏已更新，下载设置和主题已保存。")

    def choose_ffmpeg(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择本机 FFmpeg", self.ffmpeg_path.text(), "FFmpeg (ffmpeg.exe);;全部文件 (*)")
        if path:
            self.ffmpeg_path.setText(path)
            self.detect_ffmpeg()

    def installed_ffmpeg(self, path):
        self.ffmpeg_path.setText(path)
        self.settings["ffmpeg_path"] = path
        save_json(SETTINGS_FILE, self.settings)
        self.detect_ffmpeg()

    def detect_ffmpeg(self):
        try:
            path = resolve_ffmpeg(self.ffmpeg_path.text(), [resource_path("tools"), Path(sys.executable).parent / "tools"])
            result = subprocess.run([str(path), "-version"], capture_output=True, timeout=5,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                raise ValueError("FFmpeg 检测失败，请检查所选程序。")
            lines = result.stdout.decode("utf-8", "replace").splitlines()
            if not lines or not lines[0].lower().startswith("ffmpeg version"):
                raise ValueError("所选程序未返回 FFmpeg 版本，请重新选择 ffmpeg.exe。")
            version = lines[0]
            self.ffmpeg_status.setText(f"已检测：{path}\n{version}")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            self.ffmpeg_status.setText(str(error))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        ensure_app_dirs()
        self.settings = {"download_dir": default_download_dir(), "concurrency": 2, "theme": "深色"}
        self.settings.update(load_json(SETTINGS_FILE, {}))
        if self.settings.get("account_workspace_active"):
            from assistant_data import read_protected
            guest, _ = read_protected(APP_DIR / "guest-desktop.json", {})
            for key in ("theme", "features", "concurrency", "school_timetable"):
                if key in guest: self.settings[key] = guest[key]
                else: self.settings.pop(key, None)
        self.manager = TaskManager(self.settings)
        self.pending_update = None
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setWindowIcon(QIcon(str(resource_path("assets/omini_icon.ico"))))
        self.resize(1280, 860)
        self.setMinimumSize(980, 680)
        self.quitting = False
        self._build_ui()
        previous_page = self.settings.get("last_page", 0)
        if isinstance(previous_page, int) and 0 <= previous_page < self.nav.count() and not self.nav.item(previous_page).isHidden():
            self.nav.setCurrentRow(previous_page)
        self.apply_theme()
        self.quitting = False
        self.tray = QSystemTrayIcon(QIcon(str(resource_path("assets/omini_icon.ico"))), self)
        self.tray.setToolTip(f"{APP_NAME} · 文件夹同步与校园网")
        menu = QMenu(self)
        menu.addAction("打开工具箱", self.restore_window)
        self.feature_tray_actions = {
            "campus": menu.addAction("校园网立即登录", self.login_campus),
            "sync": menu.addAction("打开文件夹同步", self.open_folder_sync),
            "omini_api": menu.addAction("打开 Omini API", self.open_omini_api),
            "ai_assistants": menu.addAction("打开 AI 编程助手", self.open_ai_assistants),
        }
        for key, action in self.feature_tray_actions.items(): action.setVisible(enabled_features(self.settings)[key])
        menu.addSeparator()
        menu.addAction("退出工具箱", self.quit_from_tray)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.restore_window() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self.tray.show()
        from account_sync import DesktopAccountSync
        self.cloud_sync = DesktopAccountSync(APP_DIR, self, lambda value: save_json(SETTINGS_FILE, value))

    def _build_ui(self) -> None:
        root = QWidget(); self.setCentralWidget(root); layout = QHBoxLayout(root); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)
        sidebar = QFrame(); sidebar.setObjectName("sidebar"); sidebar.setFixedWidth(210); side_layout = QVBoxLayout(sidebar); side_layout.setContentsMargins(14, 22, 14, 18)
        brand = QWidget()
        brand_layout = QHBoxLayout(brand)
        brand_layout.setContentsMargins(0, 0, 0, 0)
        brand_layout.setSpacing(10)
        logo_icon = QLabel()
        logo_icon.setObjectName("brandIcon")
        logo_icon.setFixedSize(44, 44)
        pixmap = QPixmap(str(resource_path("assets/omini_icon_128.png")))
        if not pixmap.isNull():
            logo_icon.setPixmap(pixmap.scaled(
                44, 44, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
            ))
        logo = QLabel("OMINI\n工具箱")
        logo.setObjectName("logo")
        brand_layout.addWidget(logo_icon)
        brand_layout.addWidget(logo)
        brand_layout.addStretch()
        side_layout.addWidget(brand)
        side_layout.addSpacing(24)
        self.nav = QListWidget(); self.nav.setObjectName("navigation")
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for label in ("↓  下载中心", "◎  校园网自动登录", "▦  学校课表", "▤  超级备忘录", "⚙  设置", "↗  Bright Data", "⇄  文件夹同步", "⌘  Omini API", "◇  AI 编程助手", "＋  更多工具（敬请期待）"):
            item = QListWidgetItem(label); item.setSizeHint(item.sizeHint() + QSize(0, 12)); self.nav.addItem(item)
        side_layout.addWidget(self.nav, 1)
        self.account_button = QPushButton("✉  邮箱注册 / 登录")
        self.account_button.clicked.connect(self.open_account)
        side_layout.addWidget(self.account_button)
        self.theme_button = QPushButton()
        self.theme_button.setObjectName("themeToggle")
        self.theme_button.setAccessibleName("切换白天黑夜模式")
        self.theme_button.clicked.connect(self.toggle_theme)
        side_layout.addWidget(self.theme_button)
        side_layout.addSpacing(12)
        tip = QLabel("下载引擎在本地运行\nCookie 按任务临时使用")
        tip.setObjectName("muted"); tip.setWordWrap(True); side_layout.addWidget(tip)
        layout.addWidget(sidebar)
        self.stack = QStackedWidget()
        self.retired_pages = []
        for feature in PAGE_FEATURES:
            self.stack.addWidget(QWidget())
            if feature:
                setattr(self, self.page_attribute(feature), None)
        self.settings_page = SettingsPage(self.settings)
        self.settings_page.account.account_changed.connect(self.update_account_label)
        self.settings_page.account.before_account_change = self.prepare_account_switch
        self.settings_page.before_save = self.prepare_features
        self.settings_page.settings_changed.connect(self.on_settings_changed)
        self.replace_page(4, self.settings_page)
        self.settings_page.updates.install_requested.connect(self.install_update)
        self.apply_features(select=False)
        self.nav.currentRowChanged.connect(self.change_page)
        selected = self.settings.get("last_page", 0)
        if type(selected) is not int or not 0 <= selected < self.nav.count() or self.nav.item(selected).isHidden():
            selected = next(i for i in range(self.nav.count()) if not self.nav.item(i).isHidden())
        self.nav.setCurrentRow(selected)
        layout.addWidget(self.stack, 1)
        about = QAction("关于", self); about.triggered.connect(lambda: QMessageBox.about(self, APP_NAME, f"Omini工具箱\n版本 {APP_VERSION}\n下载中心 · 校园网自动登录 · 学校课表 · 超级备忘录 · Bright Data · 文件夹同步 · Omini API · AI 编程助手")); self.menuBar().addAction(about)

    @staticmethod
    def page_attribute(feature):
        return "download_page" if feature == "downloads" else feature + "_page"

    def replace_page(self, index, page):
        previous = self.stack.widget(index)
        self.stack.removeWidget(previous)
        self.stack.insertWidget(index, page)
        previous.deleteLater()

    def create_feature_page(self, feature):
        save = lambda value: save_json(SETTINGS_FILE, value)
        workspace = self.cloud_sync.root if hasattr(self, "cloud_sync") else APP_DIR
        if feature == "downloads": return DownloadPage(self.manager, self.settings)
        if feature == "campus":
            from campus_page import CampusPage
            return CampusPage(self.settings, save, self)
        if feature == "timetable":
            from timetable_page import TimetablePage
            return TimetablePage(self.settings, save, workspace / "school-timetable.json",
                                 lambda value: save_json(workspace / "school-timetable.json", value), self)
        if feature == "notes":
            from text_notes_page import TextNotesPage
            return TextNotesPage(APP_DIR / "text-notes.json", self, account=self.settings_page.account)
        if feature == "brightdata":
            from brightdata_page import BrightDataPage
            return BrightDataPage(APP_DIR / "brightdata.json", self)
        if feature == "sync":
            from folder_sync_page import FolderSyncPage
            return FolderSyncPage(APP_DIR / "sync", self)
        if feature == "omini_api":
            from omini_api_page import OminiApiPage
            return OminiApiPage(self.settings, save, self)
        if feature == "ai_assistants":
            from ai_assistants_page import AiAssistantsPage
            return AiAssistantsPage(workspace / "ai-assistants", self)
        page = QLabel("更多工具正在规划中"); page.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return page

    def prepare_features(self, features):
        if self.notes_page and not features["notes"] and not self.notes_page.flush():
            QMessageBox.warning(self, "超级备忘录尚未保存", "请先保存当前笔记，再关闭此功能。")
            return False
        return True

    def apply_features(self, select=True):
        features = enabled_features(self.settings)
        for index, feature in enumerate(PAGE_FEATURES):
            if feature is None: continue
            enabled = features[feature]
            self.nav.item(index).setHidden(not enabled)
            attribute = self.page_attribute(feature)
            page = getattr(self, attribute)
            if enabled and page is None:
                if any(getattr(old, "feature_key", None) == feature and getattr(old, "worker", None)
                       and old.worker.isRunning() for old in self.retired_pages):
                    QTimer.singleShot(100, self.reload_features)
                    continue
                page = self.create_feature_page(feature)
                self.replace_page(index, page)
                setattr(self, attribute, page)
            elif not enabled and page is not None:
                if feature == "downloads":
                    for task in self.manager.tasks:
                        if task.status in {"running", "queued"}: self.manager.pause(task.id)
                if hasattr(page, "stop"): page.stop()
                self.stack.removeWidget(page)
                loading = QLabel("功能已关闭；重新勾选后加载。")
                loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self.stack.insertWidget(index, loading)
                page.feature_key = feature
                self.retired_pages.append(page)
                setattr(self, attribute, None)
        for key, action in getattr(self, "feature_tray_actions", {}).items(): action.setVisible(features[key])
        if self.retired_pages: QTimer.singleShot(50, self.reap_pages)
        if select:
            current = self.nav.currentRow()
            if current < 0 or self.nav.item(current).isHidden():
                self.nav.setCurrentRow(4)
            else:
                self.change_page(current)

    def reload_features(self):
        if not self.quitting: self.apply_features()

    def reap_pages(self):
        for page in self.retired_pages[:]:
            worker = getattr(page, "worker", None)
            if worker and worker.isRunning(): continue
            self.retired_pages.remove(page); page.deleteLater()
        if self.retired_pages: QTimer.singleShot(100, self.reap_pages)

    def open_account(self):
        self.nav.setCurrentRow(4)
        self.settings_page.findChild(QScrollArea).ensureWidgetVisible(self.settings_page.account)
        self.settings_page.account.email.setFocus()

    def update_account_label(self, user):
        self.account_button.setText("✉  " + user["email"] if user else "✉  邮箱注册 / 登录")
        self.account_button.setToolTip(user["email"] if user else "打开账号注册、登录及同步设置")

    def prepare_account_switch(self):
        return (not self.notes_page or self.notes_page.flush()) and (not hasattr(self, "cloud_sync") or self.cloud_sync.prepare_switch())

    def login_campus(self):
        if self.campus_page: self.campus_page.manual_login()

    def open_ai_assistants(self) -> None:
        self.nav.setCurrentRow(8 if self.ai_assistants_page else 4)
        self.restore_window()

    def open_omini_api(self) -> None:
        self.nav.setCurrentRow(7 if self.omini_api_page else 4)
        self.restore_window()

    def open_folder_sync(self) -> None:
        self.nav.setCurrentRow(6 if self.sync_page else 4)
        self.restore_window()

    def install_update(self, artifact) -> None:
        self.pending_update = artifact
        self.quitting = True
        self.close()

    def restore_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_from_tray(self) -> None:
        self.quitting = True
        self.close()

    def change_page(self, index: int) -> None:
        if not 0 <= index < self.nav.count(): return
        if self.nav.item(index).isHidden():
            self.nav.setCurrentRow(4)
            return
        self.stack.setCurrentIndex(index)
        self.settings["last_page"] = index
        save_json(SETTINGS_FILE, self.settings)
        if getattr(self, "ai_assistants_page", None):
            self.ai_assistants_page.report("打开工具：" + self.nav.item(index).text().strip(), "navigation")
        if getattr(self, "omini_api_page", None) and self.stack.currentWidget() is self.omini_api_page:
            self.omini_api_page.ensure_started()

    def on_settings_changed(self, settings: dict[str, Any]) -> None:
        self.manager.settings = settings
        self.apply_features()
        self.apply_theme()

    def toggle_theme(self) -> None:
        self.settings["theme"] = "浅色" if self.settings.get("theme", "深色") == "深色" else "深色"
        save_json(SETTINGS_FILE, self.settings)
        self.apply_theme()

    def apply_theme(self) -> None:
        dark = self.settings.get("theme", "深色") == "深色"
        self.theme_button.setText("☀  切换白天模式" if dark else "☾  切换黑夜模式")
        self.theme_button.setToolTip("当前：黑夜模式" if dark else "当前：白天模式")
        self.settings_page.theme.setCurrentText("深色" if dark else "浅色")
        # Keep menus and editor selections consistent with the current theme.
        palette = QPalette()
        for role, color in {
            QPalette.ColorRole.Window: "#111827" if dark else "#f8fafc",
            QPalette.ColorRole.WindowText: "#e5e7eb" if dark else "#172033",
            QPalette.ColorRole.Base: "#1e293b" if dark else "#ffffff",
            QPalette.ColorRole.AlternateBase: "#172033" if dark else "#f1f5f9",
            QPalette.ColorRole.Text: "#e5e7eb" if dark else "#172033",
            QPalette.ColorRole.Button: "#334155" if dark else "#e2e8f0",
            QPalette.ColorRole.ButtonText: "#e5e7eb" if dark else "#172033",
            QPalette.ColorRole.Highlight: "#2563eb",
            QPalette.ColorRole.HighlightedText: "#ffffff" if dark else "#172033",
        }.items():
            palette.setColor(role, QColor(color))
        QApplication.instance().setPalette(palette)
        if self.notes_page: self.notes_page.apply_theme(dark)
        if self.omini_api_page: self.omini_api_page.apply_theme(dark)
        if dark:
            self.setStyleSheet("""
                QWidget { background: #111827; color: #e5e7eb; font-family: 'Microsoft YaHei UI'; font-size: 13px; }
                QFrame#sidebar { background: #0b1220; border-right: 1px solid #243244; }
                QLabel#logo { color: #7dd3fc; font-size: 23px; font-weight: 700; letter-spacing: 1px; }
                QLabel#pageTitle { font-size: 26px; font-weight: 700; color: #f8fafc; }
                QLabel#sectionTitle { font-size: 17px; font-weight: 700; margin-top: 8px; }
                QLabel#muted { color: #94a3b8; }
                QListWidget#navigation { border: 0; background: transparent; outline: none; }
                QListWidget#navigation::item { border-radius: 7px; padding: 11px 10px; color: #cbd5e1; margin: 2px 0; }
                QListWidget#navigation::item:selected { background: #1d4ed8; color: white; }
                QGroupBox { border: 1px solid #334155; border-radius: 9px; margin-top: 12px; padding: 16px 10px 10px; font-weight: 700; }
                QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }
                QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QTimeEdit, QDateEdit { background: #1e293b; border: 1px solid #475569; border-radius: 5px; padding: 7px; selection-background-color: #2563eb; }
                QLineEdit:focus, QComboBox:focus { border-color: #60a5fa; }
                QPushButton, QToolButton { background: #334155; border: 0; border-radius: 5px; padding: 8px 13px; }
                QPushButton:hover, QToolButton:hover { background: #475569; }
                QPushButton:disabled, QToolButton:disabled { background: #1e293b; color: #64748b; }
                QPushButton#primary { background: #2563eb; color: white; font-weight: 700; } QPushButton#primary:hover { background: #1d4ed8; }
                QTabWidget::pane { border: 1px solid #334155; border-radius: 7px; top: -1px; } QTabBar::tab { background: #1e293b; padding: 8px 14px; margin-right: 2px; } QTabBar::tab:selected { background: #2563eb; }
                QTableWidget { background: #172033; gridline-color: #334155; border: 1px solid #334155; border-radius: 6px; } QHeaderView::section { background: #1e293b; border: 0; padding: 8px; color: #cbd5e1; } QTableWidget::item:selected { background: #1d4ed8; }
            """)
        else:
            self.setStyleSheet("""
                QWidget { background: #f8fafc; color: #172033; font-family: 'Microsoft YaHei UI'; font-size: 13px; }
                QFrame#sidebar { background: #eaf1fb; border-right: 1px solid #cbd5e1; } QLabel#logo { color: #1d4ed8; font-size: 23px; font-weight: 700; } QLabel#pageTitle { font-size: 26px; font-weight: 700; } QLabel#sectionTitle { font-size: 17px; font-weight: 700; margin-top: 8px; } QLabel#muted { color: #64748b; }
                QListWidget#navigation { border: 0; background: transparent; } QListWidget#navigation::item { border-radius: 7px; padding: 11px 10px; margin: 2px 0; } QListWidget#navigation::item:selected { background: #2563eb; color: white; }
                QGroupBox { border: 1px solid #cbd5e1; border-radius: 9px; margin-top: 12px; padding: 16px 10px 10px; font-weight: 700; } QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }
                QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QTimeEdit, QDateEdit { background: white; border: 1px solid #94a3b8; border-radius: 5px; padding: 7px; } QLineEdit:focus, QComboBox:focus { border-color: #2563eb; }
                QPushButton, QToolButton { background: #e2e8f0; border: 0; border-radius: 5px; padding: 8px 13px; } QPushButton:hover, QToolButton:hover { background: #cbd5e1; } QPushButton#primary { background: #2563eb; color: white; font-weight: 700; }
                QPushButton:disabled, QToolButton:disabled { background: #f1f5f9; color: #94a3b8; }
                QTabWidget::pane { border: 1px solid #cbd5e1; border-radius: 7px; top: -1px; } QTabBar::tab { background: #e2e8f0; padding: 8px 14px; margin-right: 2px; } QTabBar::tab:selected { background: #2563eb; color: white; }
                QTableWidget { background: white; gridline-color: #e2e8f0; border: 1px solid #cbd5e1; border-radius: 6px; } QHeaderView::section { background: #eaf1fb; border: 0; padding: 8px; } QTableWidget::item:selected { background: #bfdbfe; }
            """)

    def closeEvent(self, event: Any) -> None:
        if not self.quitting and (bool(self.campus_page and self.campus_page.config.get("background", False)) or
                                  bool(self.sync_page and self.sync_page.background_enabled) or
                                  bool(self.ai_assistants_page and self.ai_assistants_page.wants_background)) and QSystemTrayIcon.isSystemTrayAvailable():
            self.hide()
            self.tray.showMessage(APP_NAME, "已在托盘后台运行，右键托盘图标可退出。")
            event.ignore(); return
        active = len(self.manager.workers)
        if active:
            answer = QMessageBox.question(self, "退出工具箱", f"仍有 {active} 个下载任务在进行。退出后将暂停这些任务，确定退出吗？")
            if answer != QMessageBox.StandardButton.Yes:
                self.quitting = False
                self.pending_update = None
                event.ignore(); return
            for task_id in list(self.manager.workers):
                self.manager.pause(task_id)
                self.manager.workers[task_id].wait(4000)
        if self.notes_page and not self.notes_page.flush():
            answer = QMessageBox.question(self, "超级备忘录尚未保存", "TXT 保存失败。仍要退出并放弃未保存的编辑吗？")
            if answer != QMessageBox.StandardButton.Yes:
                self.quitting = False; self.pending_update = None; event.ignore(); return
        pages = [getattr(self, self.page_attribute(feature)) for feature in PAGE_FEATURES if feature]
        pages = [page for page in pages if page is not None] + self.retired_pages[:]
        for page in pages:
            if hasattr(page, "stop"): page.stop()
        if hasattr(self, "cloud_sync"): self.cloud_sync.stop()
        self.settings_page.account.stop()
        self.settings_page.updates.stop()
        self.settings_page.components.stop()
        pending = [page.worker for page in pages + [self.settings_page.account]
                   if getattr(page, "worker", None) and page.worker.isRunning()]
        update_worker = self.settings_page.updates.worker
        if update_worker and update_worker.isRunning():
            pending.append(update_worker)
        sync_worker = getattr(getattr(self, "cloud_sync", None), "worker", None)
        if sync_worker and sync_worker.isRunning(): pending.append(sync_worker)
        component_worker = self.settings_page.components.worker
        if component_worker and component_worker.isRunning(): pending.append(component_worker)
        if pending:
            self.quitting = True
            # A thread may finish between isRunning() and connecting finished.
            # Recheck on the event loop so that race cannot leave a hidden app alive.
            QTimer.singleShot(50, self.close)
            event.ignore(); return
        self.tray.hide()
        event.accept()
        if self.quitting:
            # Closing an already-hidden window does not emit lastWindowClosed.
            QTimer.singleShot(0, QApplication.instance().quit)


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = MainWindow(); window.show()
    exit_code = app.exec()
    if window.pending_update:
        try:
            launch_installer(window.pending_update)
        except (ValueError, OSError):
            QMessageBox.warning(None, "更新安装未启动", "安装包读取或启动失败，请重新打开工具箱并再次下载安装。")
            exit_code = 1
    sys.exit(exit_code)


if __name__ == "__main__":
    if "--self-test-settings" in sys.argv:
        from settings_self_test import run_settings_test
        sys.exit(run_settings_test(sys.modules[__name__]))
    if "--self-test-ai-assistants" in sys.argv:
        from ai_assistants_self_test import run_ai_assistants_test
        sys.exit(run_ai_assistants_test(sys.modules[__name__]))
    if "--self-test-omini-api" in sys.argv:
        from omini_api_self_test import run_api_test
        sys.exit(run_api_test(sys.modules[__name__]))
    if "--self-test-update" in sys.argv:
        from update_self_test import run_update_test
        sys.exit(run_update_test(sys.modules[__name__]))
    if "--self-test-sync" in sys.argv:
        from sync_self_test import run_sync_test
        sys.exit(run_sync_test(sys.modules[__name__]))
    if "--self-test-brightdata" in sys.argv:
        from brightdata_self_test import run_brightdata_test
        sys.exit(run_brightdata_test(sys.modules[__name__]))
    if "--self-test-notes" in sys.argv:
        from notes_self_test import run_notes_test
        sys.exit(run_notes_test(sys.modules[__name__]))
    if "--self-test-timetable" in sys.argv:
        from timetable_self_test import run_timetable_test
        sys.exit(run_timetable_test(sys.modules[__name__]))
    if "--self-test-download" in sys.argv:
        from download_self_test import run_download_test
        sys.exit(run_download_test(sys.modules[__name__]))
    main()
