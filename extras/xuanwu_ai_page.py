"""Qt page embedding the local XuanwuAi UI and backend."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal, Slot
from PySide6.QtWidgets import QFileDialog, QLabel, QVBoxLayout, QWidget
from PySide6.QtWebEngineWidgets import QWebEngineView

from xuanwu_ai import create_server
from xuanwu_ai.app import ROOT


class _FolderPicker(QObject):
    requested = Signal(object)

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.parent_widget = parent
        self.requested.connect(self._show_dialog, Qt.ConnectionType.QueuedConnection)

    def choose(self) -> str | None:
        event = threading.Event()
        result: dict[str, str | None] = {}
        self.requested.emit((event, result))
        event.wait()
        return result.get("path")

    @Slot(object)
    def _show_dialog(self, request: tuple[threading.Event, dict[str, str | None]]) -> None:
        event, result = request
        try:
            result["path"] = QFileDialog.getExistingDirectory(
                self.parent_widget,
                "选择玄武Ai工作目录",
                str(Path.home()),
            ) or None
        finally:
            event.set()


class XuanwuAiPage(QWidget):
    def __init__(self, parent=None, data_directory=None):
        super().__init__(parent)
        self.data_directory = data_directory
        self.server = None
        self.server_thread: threading.Thread | None = None
        self.load_failed = False
        self.folder_picker = _FolderPicker(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.status = QLabel("选择侧边栏「玄武Ai」以启动本机助手。")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setObjectName("muted")
        self.status.hide()
        self.web = QWebEngineView(self)
        self.web.loadFinished.connect(self._load_finished)
        self.web.page().profile().downloadRequested.connect(self._download_requested)
        layout.addWidget(self.status)
        layout.addWidget(self.web, 1)

    def ensure_started(self) -> None:
        if self.server is not None:
            if self.load_failed:
                self.status.setText("正在重新连接玄武Ai…")
                self.status.show()
                self.web.reload()
            return
        if not (ROOT / "ui" / "index.html").is_file():
            self.status.setText("玄武Ai界面资源缺失，请重新打包工具箱。")
            self.status.show()
            return
        self.status.setText("正在启动玄武Ai…")
        self.status.show()
        try:
            self.server = create_server(self.data_directory)
            self.server.runtime.folder_picker = self.folder_picker.choose
            self.server_thread = threading.Thread(
                target=self.server.serve_forever,
                name="xuanwu-ai-http",
                daemon=True,
            )
            self.server_thread.start()
            self.web.setUrl(QUrl(f"http://127.0.0.1:{self.server.runtime.port}/"))
        except Exception as error:
            self.status.setText(f"玄武Ai启动失败：{str(error).splitlines()[0]}")
            self.status.show()

    @Slot(bool)
    def _load_finished(self, success: bool) -> None:
        self.load_failed = not success
        if success:
            self.status.hide()
        else:
            self.status.setText("玄武Ai页面加载失败，重新选择侧边栏入口可重试。")
            self.status.show()

    @Slot(object)
    def _download_requested(self, request) -> None:
        filename = request.downloadFileName() or "玄武Ai-对话.md"
        default_path = Path.home() / filename
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "导出玄武Ai对话",
            str(default_path),
            "Markdown 文件 (*.md);;所有文件 (*)",
        )
        if not selected:
            request.cancel()
            return
        target = Path(selected)
        request.setDownloadDirectory(str(target.parent))
        request.setDownloadFileName(target.name)
        request.accept()

    def shutdown(self) -> None:
        server = self.server
        if server is None:
            return
        runtime = server.runtime
        runtime.speech.stop()
        for request_id in list(runtime.jobs):
            runtime.cancel(request_id)
        deadline = time.monotonic() + 2.0
        while runtime.jobs and time.monotonic() < deadline:
            time.sleep(0.02)
        server.shutdown()
        server.server_close()
        if self.server_thread is not None:
            self.server_thread.join(timeout=2.0)
        self.server = None
        self.server_thread = None
