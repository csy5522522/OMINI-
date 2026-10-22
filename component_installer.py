"""Verified, cancellable per-user optional component installation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile

import requests
from PySide6.QtCore import QThread, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ai_assistants import ENGINE_REPO, ENGINE_VERSION, install_engine

FFMPEG_VERSION = "8.1.2"
FFMPEG_URL = f"https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-{FFMPEG_VERSION}-essentials_build.7z"
FFMPEG_SHA256 = "e25b682664025d49034c981afb4bae36238a40f29a3cc1c713ad9a8b5b3528f6"
FFMPEG_SOURCE = "https://ffmpeg.org/download.html"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def install_ffmpeg(destination, *, progress=lambda text: None, cancelled=lambda: False, archive_path=None):
    if os.name != "nt" or platform.machine().lower() not in {"amd64", "x86_64", "arm64", "aarch64"}:
        raise ValueError("此下载入口适用于 Windows 64 位；其他系统请使用官网对应的软件包。")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="install-", dir=destination) as temporary:
        staging = Path(temporary)
        archive = Path(archive_path) if archive_path else staging / "ffmpeg.7z"
        if not archive_path:
            try:
                with requests.get(FFMPEG_URL, stream=True, timeout=(10, 15)) as response:
                    response.raise_for_status()
                    received = 0
                    with archive.open("wb") as stream:
                        for block in response.iter_content(256 * 1024):
                            if cancelled(): raise ValueError("下载已取消。")
                            received += len(block)
                            if received > 200 * 1024 * 1024: raise ValueError("下载包超过大小限制。")
                            stream.write(block)
                            progress(f"下载 FFmpeg：{received / 1024 ** 2:.1f} MB")
            except requests.RequestException:
                raise ValueError("FFmpeg 下载失败，请检查网络后重试，也可打开官网下载。") from None
        if cancelled(): raise ValueError("下载已取消。")
        progress("正在校验 FFmpeg SHA-256…")
        if digest(archive) != FFMPEG_SHA256:
            raise ValueError("FFmpeg SHA-256 校验失败，请重新下载。")
        tar = shutil.which("tar.exe") or shutil.which("tar")
        if not tar: raise ValueError("请启用 Windows 自带的 tar.exe 后重试。")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        entries = subprocess.run([tar, "-tf", str(archive)], capture_output=True, check=True,
                                 timeout=30, encoding="utf-8", creationflags=flags).stdout.splitlines()
        outputs = {}
        for suffix, filename in (("/bin/ffmpeg.exe", "ffmpeg.exe"), ("/bin/ffprobe.exe", "ffprobe.exe"),
                                 ("/LICENSE", "LICENSE.txt"), ("/README.txt", "README.txt")):
            if cancelled(): raise ValueError("安装已取消。")
            member = next((entry for entry in entries if entry.endswith(suffix)), None)
            if member is None: raise ValueError("FFmpeg 压缩包缺少所需文件。")
            output = staging / filename
            progress("正在安装 " + filename + "…")
            with output.open("wb") as stream:
                subprocess.run([tar, "-xOf", str(archive), member], stdout=stream, stderr=subprocess.PIPE,
                               check=True, timeout=60, creationflags=flags)
            outputs[filename] = output
        for name in ("ffmpeg.exe", "ffprobe.exe"):
            result = subprocess.run([str(outputs[name]), "-version"], capture_output=True, timeout=10, creationflags=flags)
            if result.returncode or not result.stdout.lower().startswith(name[:-4].encode() + b" version"):
                raise ValueError("FFmpeg 安装验证失败，原有设置已保留。")
        if cancelled(): raise ValueError("安装已取消。")
        target = destination / FFMPEG_VERSION
        target.mkdir(exist_ok=True)
        for name, output in outputs.items(): output.replace(target / name)
        (target / "installation.json").write_text(json.dumps({"version": FFMPEG_VERSION, "url": FFMPEG_URL,
            "archive_sha256": FFMPEG_SHA256, "executable_sha256": digest(target / "ffmpeg.exe")}, indent=2), encoding="utf-8")
        return target / "ffmpeg.exe"


class ComponentWorker(QThread):
    progress = Signal(str)

    def __init__(self, component, root, parent=None):
        super().__init__(parent)
        self.component, self.root = component, Path(root)
        self.result, self.error = None, ""

    def run(self):
        try:
            if self.component == "ffmpeg":
                self.result = install_ffmpeg(self.root / "components/ffmpeg", progress=self.progress.emit, cancelled=self.isInterruptionRequested)
            else:
                proxies = requests.utils.get_environ_proxies(ENGINE_REPO)
                proxy = proxies.get("https", proxies.get("all", ""))
                self.result = install_engine(self.root / "ai-assistants/bin", proxy=proxy, progress=self.progress.emit, cancelled=self.isInterruptionRequested)
        except ValueError as error:
            self.error = str(error)
        except Exception:
            self.error = "组件安装失败，原有程序与设置已保留，请重试。"


class ComponentPanel(QGroupBox):
    ffmpeg_installed = Signal(str)

    def __init__(self, root, parent=None):
        super().__init__("可选组件 · 下载安装", parent)
        self.root, self.worker, self.stopping = Path(root), None, False
        layout = QVBoxLayout(self)
        self.buttons = []
        for name, label, source in (("ffmpeg", f"FFmpeg {FFMPEG_VERSION} · 视频合并与音频转换", FFMPEG_SOURCE),
                                    ("codex", f"CLIProxyAPI {ENGINE_VERSION} · Codex 账号池引擎", ENGINE_REPO)):
            row = QHBoxLayout(); row.addWidget(QLabel(label), 1)
            install = QPushButton("下载安装"); install.clicked.connect(lambda _checked=False, name=name: self.install(name))
            website = QPushButton("打开官网 / GitHub"); website.clicked.connect(lambda _checked=False, source=source: QDesktopServices.openUrl(QUrl(source)))
            row.addWidget(install); row.addWidget(website); layout.addLayout(row)
            self.buttons.append(install)
        self.cancel_button = QPushButton("取消下载"); self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(lambda: self.worker.requestInterruption() if self.worker else None)
        layout.addWidget(self.cancel_button)
        self.status = QLabel("组件安装到当前用户目录。下载后校验 SHA-256；FFmpeg 安装成功会自动写入设置。")
        self.status.setWordWrap(True); self.status.setTextFormat(Qt.TextFormat.PlainText); layout.addWidget(self.status)

    def install(self, name):
        if self.worker or self.stopping: return
        self.worker = ComponentWorker(name, self.root, self)
        self.worker.progress.connect(self.status.setText)
        self.worker.finished.connect(self.finished)
        for button in self.buttons: button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.status.setText("正在连接组件下载源…")
        self.worker.start()

    def finished(self):
        worker, self.worker = self.worker, None
        worker.deleteLater()
        self.cancel_button.setEnabled(False)
        for button in self.buttons: button.setEnabled(not self.stopping)
        if worker.error:
            self.status.setText(worker.error)
        elif worker.result:
            self.status.setText("安装并验证成功：" + str(worker.result))
            if worker.component == "ffmpeg": self.ffmpeg_installed.emit(str(worker.result))

    def stop(self):
        self.stopping = True
        if self.worker: self.worker.requestInterruption()
