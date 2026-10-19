"""Desktop page hosting the complete Omini API interface on Windows."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, QTimer, QUrl, Qt
from PySide6.QtGui import QDesktopServices, QWindow
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QVBoxLayout, QWidget,
)

from omini_api.runtime import base_command, initial_workspace


class OminiApiPage(QWidget):
    def __init__(self, settings: dict, save_settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.save_settings = save_settings
        self.workspace = initial_workspace(str(settings.get("omini_api_workspace", "")))
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._error)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._poll)
        self.control = None
        self.container = None
        self.foreign_window = None
        self.started_at = 0.0
        self.stopping = False
        self.dark = settings.get("theme", "深色") == "深色"
        self.diagnostic = ""
        self.runtime_state = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 10)
        heading = QHBoxLayout()
        title = QLabel("Omini API")
        title.setObjectName("pageTitle")
        heading.addWidget(title)
        heading.addStretch()
        self.restart_button = QPushButton("重新启动")
        self.restart_button.clicked.connect(self.restart)
        heading.addWidget(self.restart_button)
        self.stop_button = QPushButton("停止 API 工具")
        self.stop_button.clicked.connect(self.stop)
        self.stop_button.setEnabled(False)
        heading.addWidget(self.stop_button)
        layout.addLayout(heading)
        data_row = QHBoxLayout()
        data_row.addWidget(QLabel("数据目录"))
        self.path_field = QLineEdit(str(self.workspace))
        self.path_field.setReadOnly(True)
        data_row.addWidget(self.path_field, 1)
        choose = QPushButton("选择目录")
        choose.clicked.connect(self.choose_workspace)
        data_row.addWidget(choose)
        open_folder = QPushButton("打开目录")
        open_folder.clicked.connect(self.open_workspace)
        data_row.addWidget(open_folder)
        layout.addLayout(data_row)
        self.host = QWidget(self)
        self.host_layout = QVBoxLayout(self.host)
        self.host_layout.setContentsMargins(0, 0, 0, 0)
        self.empty = QLabel("Codex 注册 · 网站搜索 · 号池账号 · 号池网站 · Claude 独立 · 设置与分组 · 日志\n\n选择侧栏「Omini API」启动。")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.host_layout.addWidget(self.empty)
        layout.addWidget(self.host, 1)
        self.status = QLabel("就绪，配置与账号保存在上方数据目录。")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    @property
    def is_running(self) -> bool:
        return self.process.state() != QProcess.ProcessState.NotRunning

    def ensure_started(self) -> None:
        if self.is_running:
            return
        self.stopping = False
        self.diagnostic = ""
        try:
            self.workspace.mkdir(parents=True, exist_ok=True)
            self.control = tempfile.TemporaryDirectory(prefix="omini-api-")
        except OSError as error:
            self.status.setText(f"启动失败：{error}")
            return
        self.settings["omini_api_workspace"] = str(self.workspace)
        self.save_settings(self.settings)
        command = base_command()
        if sys.platform == "win32":
            command += ["--embed", self.control.name]
        env = QProcessEnvironment.systemEnvironment()
        env.insert("OMINI_API_TOOLBOX_HOME", str(self.workspace))
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("OMINI_API_PARENT_PID", str(os.getpid()))
        env.remove("OMINI_TOOLBOX_SMOKE_TEST")
        self.process.setProcessEnvironment(env)
        self.process.setWorkingDirectory(str(self.workspace))
        self.process.setProgram(command[0])
        self.process.setArguments(command[1:])
        self.started_at = time.monotonic()
        self.status.setText("正在启动 Omini API…")
        self.empty.setText("正在载入 Omini API 的七个功能页面…")
        self.process.start()
        self.stop_button.setEnabled(True)
        self.timer.start()

    def _send_command(self) -> None:
        if self.control is None:
            return
        target = Path(self.control.name) / "command.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps({"dark": self.dark, "attached": self.container is not None}), encoding="utf-8")
        temporary.replace(target)

    def _poll(self) -> None:
        if not self.is_running or self.control is None:
            return
        directory = Path(self.control.name)
        if self.container is None and sys.platform == "win32":
            try:
                ready = json.loads((directory / "ready.json").read_text(encoding="utf-8"))
                if int(ready["pid"]) != self.process.processId():
                    return
                self.foreign_window = QWindow.fromWinId(int(ready["hwnd"]))
                if self.foreign_window is None:
                    raise RuntimeError("窗口载入失败，请点击重新启动。")
                self.container = QWidget.createWindowContainer(self.foreign_window, self.host)
                self.container.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                self.host_layout.addWidget(self.container, 1)
                self.empty.hide()
                self._send_command()
            except (OSError, ValueError, KeyError):
                if time.monotonic() - self.started_at > 25:
                    self.stop()
                    self.status.setText("启动超时，请点击重新启动，或检查数据目录。")
                return
            except RuntimeError as error:
                self.stop()
                self.status.setText(str(error))
                return
        if sys.platform != "win32":
            self.empty.setText("Omini API 已在独立窗口打开。")
        try:
            state = json.loads((directory / "status.json").read_text(encoding="utf-8"))
            self.runtime_state = state
            tasks = state.get("tasks", [])
            self.status.setText("运行中：" + "、".join(tasks) if tasks else "Omini API 已就绪，切换页面后任务继续运行。")
        except (OSError, ValueError):
            pass

    def _read_output(self) -> None:
        # Retain only a short startup diagnostic; account logs stay in the API UI.
        self.diagnostic = (self.diagnostic + bytes(self.process.readAllStandardOutput()).decode("utf-8", "replace"))[-4000:]

    def _error(self, error) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self.timer.stop()
            self.stop_button.setEnabled(False)
            self.status.setText("启动失败：" + self.process.errorString())
            self._cleanup()

    def _cleanup(self) -> None:
        if self.container is not None:
            self.host_layout.removeWidget(self.container)
            self.container.hide()
            self.container.deleteLater()
            self.container = None
            self.foreign_window = None
        self.empty.show()
        self.runtime_state = {}
        self.empty.setText("Omini API 已停止，点击「重新启动」继续使用。")
        if self.control is not None:
            self.control.cleanup()
            self.control = None

    def _finished(self, exit_code: int, _exit_status) -> None:
        self._read_output()
        self.timer.stop()
        self.stop_button.setEnabled(False)
        self._cleanup()
        if exit_code and not self.stopping:
            self.status.setText(f"Omini API 已退出（{exit_code}），请检查目录后重新启动。")
        else:
            self.status.setText("Omini API 已停止，账号和配置已保留。")

    def stop(self) -> None:
        self.stopping = True
        self.timer.stop()
        if not self.is_running:
            self._cleanup()
            return
        pid = int(self.process.processId())
        if self.control is not None:
            (Path(self.control.name) / "stop").touch()
        if not self.process.waitForFinished(6500):
            if sys.platform == "win32" and pid:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
            self.process.kill()
            self.process.waitForFinished(2500)
        self._cleanup()
        self.stop_button.setEnabled(False)

    def restart(self) -> None:
        self.stop()
        self.ensure_started()

    def apply_theme(self, dark: bool) -> None:
        self.dark = dark
        if self.is_running:
            self._send_command()

    def choose_workspace(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择 Omini API 数据目录（可选择原工具目录）", str(self.workspace))
        if not selected or Path(selected).resolve() == self.workspace.resolve():
            return
        if self.is_running:
            answer = QMessageBox.question(self, "切换数据目录", "切换目录会保存配置并停止当前 API 任务，随后载入新目录。继续吗？")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.stop()
        self.workspace = Path(selected).resolve()
        self.path_field.setText(str(self.workspace))
        self.settings["omini_api_workspace"] = str(self.workspace)
        self.save_settings(self.settings)
        self.ensure_started()

    def open_workspace(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.workspace)))
