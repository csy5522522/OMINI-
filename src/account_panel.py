"""Email account controls and the bundled local web service in desktop settings."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit

from PySide6.QtCore import QProcess, QProcessEnvironment, QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QFormLayout, QGroupBox, QHBoxLayout,
                              QLabel, QLineEdit, QPushButton, QTabWidget, QWidget)

from account_client import AccountClient, DesktopSession, service_url, DEFAULT_SERVICE
from accounts import normalize_email, validate_password


class AccountWorker(QThread):
    def __init__(self, url, token, action, value, parent=None, *, remember=False):
        super().__init__(parent)
        self.url, self.token, self.action, self.value = url, token, action, value
        self.remember = remember

    def run(self):
        try:
            client = AccountClient(self.url, self.token, remember=self.remember)
            result = client.request(self.action, self.value)
            self.result = {"data": result, "token": client.token, "url": self.url}
        except Exception as error:
            self.result = {"error": str(error) if isinstance(error, ValueError) else "账号请求失败，请稍后重试。"}
        finally:
            self.value = None


class AccountPanel(QGroupBox):
    account_changed = Signal(object)

    def __init__(self, root, settings, save_settings, parent=None):
        super().__init__("账号与同步", parent)
        self.root, self.settings, self.save_settings = Path(root), settings, save_settings
        self.store = DesktopSession(self.root / "account-session.bin")
        self.url = settings.get("account_server", DEFAULT_SERVICE)
        self.token = self.store.load(self.url)
        self.user = self.store.user(self.url)
        self.before_account_change = None
        self.worker = None
        self.stopping = False
        self.control = None
        self.output = ""
        self.service_ready = False
        self.ready_timer = QTimer(self)
        self.ready_timer.setInterval(150)
        self.ready_timer.timeout.connect(self.check_service_ready)
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.finished.connect(self.service_finished)
        self.process.errorOccurred.connect(lambda _: self.status.setText("网页服务启动失败，请检查程序和端口。"))
        form = QFormLayout(self)
        self.address = QLineEdit(self.url)
        self.address.setPlaceholderText(DEFAULT_SERVICE)
        form.addRow("网页 / 账号服务地址", self.address)
        row = QWidget(); buttons = QHBoxLayout(row); buttons.setContentsMargins(0, 0, 0, 0)
        self.start_button = QPushButton("启动本机网页服务")
        self.start_button.clicked.connect(self.start_service)
        self.open_button = QPushButton("打开网页端"); self.open_button.clicked.connect(self.open_web)
        self.stop_button = QPushButton("停止网页服务"); self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_service)
        self.cloud_button = QPushButton("连接官方云端"); self.cloud_button.clicked.connect(self.use_cloud)
        for button in (self.cloud_button, self.start_button, self.open_button, self.stop_button): buttons.addWidget(button)
        form.addRow(row)
        self.auth_tabs = QTabWidget()
        self.login_form = QWidget(); login_layout = QFormLayout(self.login_form)
        self.register_form = QWidget(); register_layout = QFormLayout(self.register_form)
        self.login_email = QLineEdit(); self.login_email.setPlaceholderText("name@example.com")
        self.login_password = QLineEdit(); self.login_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.login_password.setPlaceholderText("输入登录密码")
        self.register_email = QLineEdit(); self.register_email.setPlaceholderText("name@example.com")
        self.register_password = QLineEdit(); self.register_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.register_password.setPlaceholderText("8 至 128 个字符")
        self.confirm = QLineEdit(); self.confirm.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm.setPlaceholderText("注册时再次输入密码")
        self.access_password = QLineEdit(); self.access_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.access_password.setPlaceholderText("服务设置了访问密码时，注册需填写；本机默认留空")
        login_layout.addRow("邮箱", self.login_email)
        login_layout.addRow("密码", self.login_password)
        for label, field in (("邮箱", self.register_email), ("设置密码", self.register_password),
                             ("确认密码", self.confirm), ("服务访问密码", self.access_password)):
            register_layout.addRow(label, field)
        self.login_button = QPushButton("登录"); self.login_button.setObjectName("primary")
        self.register_button = QPushButton("创建账号"); self.register_button.setObjectName("primary")
        self.login_button.clicked.connect(lambda: self.request("login"))
        self.register_button.clicked.connect(lambda: self.request("register"))
        self.login_password.returnPressed.connect(self.login_button.click)
        self.confirm.returnPressed.connect(self.register_button.click)
        login_layout.addRow(self.login_button)
        register_layout.addRow(self.register_button)
        self.auth_tabs.addTab(self.login_form, "登录")
        self.auth_tabs.addTab(self.register_form, "注册")
        self.auth_tabs.currentChanged.connect(self.auth_mode_changed)
        form.addRow(self.auth_tabs)
        self.remember = QCheckBox("使用 Windows 本机加密记住登录状态")
        self.remember.setChecked(settings.get("account_remember", True)); form.addRow(self.remember)
        row = QWidget(); buttons = QHBoxLayout(row); buttons.setContentsMargins(0, 0, 0, 0)
        self.logout_button = QPushButton("退出登录")
        self.check_button = QPushButton("检查登录状态")
        self.logout_button.clicked.connect(lambda: self.request("logout"))
        self.check_button.clicked.connect(lambda: self.request("session"))
        for button in (self.logout_button, self.check_button): buttons.addWidget(button)
        form.addRow(row)
        self.status = QLabel("登录后在电脑与网页间同步账号数据。新用户可切换到「注册」创建账号。")
        self.status.setWordWrap(True); self.status.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow(self.status)
        sync_row = QWidget(); sync_buttons = QHBoxLayout(sync_row); sync_buttons.setContentsMargins(0, 0, 0, 0)
        self.sync_button = QPushButton("立即同步全部账号数据")
        self.backups_button = QPushButton("同步备份与恢复")
        sync_buttons.addWidget(self.sync_button); sync_buttons.addWidget(self.backups_button)
        form.addRow(sync_row)
        self.sync_status = QLabel("登录后自动同步课表、备忘录、偏好与 Codex 成员；离线修改保存在当前账号工作区。")
        self.sync_status.setWordWrap(True); self.sync_status.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow(self.sync_status)
        self.remember.toggled.connect(self.remember_changed)
        self.set_busy(False)
        if self.token: QTimer.singleShot(0, lambda: self.request("session"))
        elif self.user: self.status.setText("登录已过期，本机账号工作区和未同步编辑均已保留；请重新登录以继续同步。")

    @property
    def email(self):
        return self.register_email if self.auth_tabs.currentIndex() == 1 else self.login_email

    @property
    def password(self):
        return self.register_password if self.auth_tabs.currentIndex() == 1 else self.login_password

    def auth_mode_changed(self, index):
        source = self.login_email if index == 1 else self.register_email
        self.email.setText(source.text())
        self.login_password.clear(); self.register_password.clear()
        self.confirm.clear(); self.access_password.clear()

    def selected_url(self):
        url = service_url(self.address.text())
        if url != self.url:
            if self.before_account_change and not self.before_account_change():
                raise ValueError("请先保存当前备忘录，再切换账号服务。")
            self.url = url
            self.token = self.store.load(url)
            self.user = self.store.user(url)
            self.account_changed.emit(self.user)
            self.status.setText("账号服务地址已切换，请检查登录状态或重新登录。")
        self.settings["account_server"] = url
        self.save_settings(self.settings)
        return url

    def set_busy(self, busy):
        for field in (self.address, self.auth_tabs, self.login_email, self.login_password,
                      self.register_email, self.register_password, self.confirm, self.access_password, self.remember):
            field.setEnabled(not busy)
        for button in (self.login_button, self.register_button, self.check_button, self.cloud_button): button.setEnabled(not busy)
        self.logout_button.setEnabled(not busy and bool(self.token or self.user))

    def request(self, action):
        if self.worker or self.stopping: return
        if action in {"login", "register", "logout"} and self.before_account_change and not self.before_account_change():
            self.status.setText("当前工作区正在保存或执行后台操作，请稍后重试。"); return
        try:
            url = self.selected_url()
            value = None if action == "session" else {}
            if action in {"login", "register"}:
                email = self.register_email if action == "register" else self.login_email
                password = self.register_password if action == "register" else self.login_password
                value = {"email": normalize_email(email.text()), "password": validate_password(password.text()),
                         "remember": self.remember.isChecked()}
                if action == "register":
                    if password.text() != self.confirm.text():
                        raise ValueError("两次输入的密码不一致。")
                    value["access_password"] = self.access_password.text()
            self.worker = AccountWorker(url, self.token, action, value, self, remember=self.remember.isChecked())
            self.worker.finished.connect(self.request_finished)
            self.set_busy(True); self.status.setText("正在连接账号服务…")
            self.worker.start()
        except (ValueError, OSError) as error:
            self.status.setText(str(error))

    def request_finished(self):
        worker = self.worker
        if worker is None: return
        result = worker.result
        action = worker.action
        worker.deleteLater(); self.worker = None
        self.login_password.clear(); self.register_password.clear(); self.confirm.clear(); self.access_password.clear()
        if self.stopping: return
        if "error" in result:
            self.status.setText(result["error"])
        else:
            data = result["data"]
            cached_user = self.user
            self.token, self.user = result["token"], data.get("user")
            if action == "session" and not data.get("authenticated"):
                self.token = ""
                self.user = cached_user
            if action == "logout":
                self.user = None
            if action in {"register", "logout"}:
                self.auth_tabs.setCurrentIndex(0)
            self.status.setText("已登录：" + self.user["email"] if self.user else
                                "已通过服务访问密码登录。" if data.get("authenticated") else "已退出登录。" if action == "logout" else "尚未登录，请使用邮箱注册或登录。")
            if self.user and not self.token:
                self.status.setText("登录已过期，本机账号工作区和未同步编辑均已保留；请重新登录以继续同步。")
            self.remember_changed(self.remember.isChecked())
            self.account_changed.emit(self.user)
        self.set_busy(False)

    def remember_changed(self, checked):
        try:
            self.settings["account_remember"] = checked
            self.save_settings(self.settings)
            self.store.save(self.url, self.token if checked else "", self.user if checked else None)
        except (OSError, ValueError, RuntimeError):
            self.status.setText(self.status.text() + "\n登录状态仅在本次运行保留，本机加密保存未完成。")

    def start_service(self):
        if self.stopping or self.process.state() != QProcess.ProcessState.NotRunning: return
        try:
            url = self.selected_url()
            parsed = urlsplit(url)
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
                raise ValueError("启动本机服务请填写 http://127.0.0.1:端口；远程服务可直接登录。")
            self.control = tempfile.TemporaryDirectory(prefix="omini-web-control-")
            arguments = ["--host", "127.0.0.1", "--port", str(parsed.port or 80),
                         "--data-dir", str(self.root / "web"),
                         "--stop-file", str(Path(self.control.name) / "stop"),
                         "--status-file", str(Path(self.control.name) / "status.json")]
            if getattr(sys, "frozen", False):
                self.process.setProgram(sys.executable)
                self.process.setArguments(["--web", *arguments])
            else:
                self.process.setProgram(sys.executable)
                self.process.setArguments([str(Path(__file__).with_name("web_server.py")), *arguments])
            environment = QProcessEnvironment.systemEnvironment()
            environment.insert("PYTHONIOENCODING", "utf-8")
            self.process.setProcessEnvironment(environment)
            self.output = ""
            self.service_ready = False
            self.process.start()
            self.ready_timer.start()
            self.start_button.setEnabled(False); self.stop_button.setEnabled(True)
            self.status.setText("正在启动网页服务…")
        except (ValueError, OSError) as error:
            self.status.setText(str(error))

    def read_output(self):
        self.output = (self.output + bytes(self.process.readAllStandardOutput()).decode("utf-8", "replace"))[-6000:]

    def check_service_ready(self):
        if not self.control or self.service_ready: return
        try:
            data = json.loads((Path(self.control.name) / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not data.get("url"): return
        self.service_ready = True
        self.ready_timer.stop()
        self.status.setText("网页服务已启动：" + data["url"] + "；可以注册或登录邮箱账号。")
        if self.token and not self.worker: self.request("session")

    def service_finished(self):
        self.service_ready = False
        self.ready_timer.stop()
        if self.control:
            self.control.cleanup(); self.control = None
        self.start_button.setEnabled(not self.stopping); self.stop_button.setEnabled(False)
        if not self.stopping:
            self.status.setText("网页服务已停止；如该端口已有服务，可直接登录或打开网页端。")

    def open_web(self):
        try:
            QDesktopServices.openUrl(QUrl(self.selected_url()))
        except (ValueError, OSError) as error:
            self.status.setText(str(error))

    def use_cloud(self):
        self.address.setText(DEFAULT_SERVICE)
        self.request("session")

    def stop_service(self):
        if self.control and self.process.state() != QProcess.ProcessState.NotRunning:
            (Path(self.control.name) / "stop").touch()
            self.status.setText("正在暂停网页下载并停止服务…")

    def stop(self):
        self.stopping = True
        self.ready_timer.stop()
        self.stop_service()
        if self.process.state() != QProcess.ProcessState.NotRunning and not self.process.waitForFinished(5000):
            self.process.kill(); self.process.waitForFinished(2000)
