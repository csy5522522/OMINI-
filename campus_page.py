"""Campus login page; all WLAN and HTTP operations run outside the UI thread."""
from __future__ import annotations

import time
import subprocess
from datetime import datetime

from PySide6.QtCore import QThread, QTimer, QTime, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSpinBox, QTextEdit, QTimeEdit, QVBoxLayout, QWidget,
)

from campus_network import DEFAULT_PORTAL, PortalClient, protect_password, unprotect_password, wifi_ssids


class LoginSchedule:
    def __init__(self, config: dict, clock=time.monotonic):
        self.config = config
        self.clock = clock
        self.previous = ()
        self.next_interval = clock() + config.get("interval_minutes", 30) * 60
        self.last_daily = ""
        self.retry_at = 0.0
        self.failures = 0

    def reason(self, ssids: tuple[str, ...], now: datetime) -> str:
        changed = ssids != self.previous
        self.previous = ssids
        target = self.config.get("ssid", "LEDE1")
        if not any(self.config.get(key, False) for key in ("wifi_auto", "daily_enabled", "interval_enabled")):
            return ""
        if target not in ssids:
            self.retry_at = 0
            self.failures = 0
            return ""
        if changed:
            self.failures = 0
            self.retry_at = 0
        if self.failures >= 3:
            return ""
        if self.retry_at:
            if self.clock() < self.retry_at:
                return ""
            self.retry_at = 0
            return "失败后重试"
        if changed and self.config.get("wifi_auto", False):
            return "连接目标 Wi-Fi"
        today = now.strftime("%Y-%m-%d")
        if self.config.get("daily_enabled", False) and self.last_daily != today:
            if now.strftime("%H:%M") >= self.config.get("daily_time", "08:00"):
                self.last_daily = today
                return "每日定时登录"
        if self.config.get("interval_enabled", False) and self.clock() >= self.next_interval:
            self.next_interval = self.clock() + self.config.get("interval_minutes", 30) * 60
            return "间隔定时检查"
        return ""

    def completed(self, success: bool):
        self.next_interval = self.clock() + self.config.get("interval_minutes", 30) * 60
        if success:
            self.failures = 0
            self.retry_at = 0
        else:
            self.failures += 1
            self.retry_at = self.clock() + 60 if self.failures < 3 else 0


class CampusWorker(QThread):
    def __init__(self, client, credentials=None, parent=None):
        super().__init__(parent)
        self.client = client
        self.credentials = credentials
        self.result = {}

    def run(self):
        try:
            ssids = wifi_ssids()
            self.result["ssids"] = ssids
            if self.credentials and not self.isInterruptionRequested():
                username, password, service, target = self.credentials
                if target not in ssids:
                    raise RuntimeError(f"请先连接 Wi-Fi：{target}。")
                self.result["message"] = self.client.login(username, password, service, self.isInterruptionRequested)
            self.result["success"] = True
        except (RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as error:
            self.result.update(success=False, message=str(error) if not isinstance(error, KeyError)
                               else "认证页面缺少公钥，请检查门户地址。")
        except Exception:
            self.result.update(success=False, message="校园网检测发生异常，请检查连接与账号设置。")


class CampusPage(QWidget):
    def __init__(self, settings, save_settings, parent=None):
        super().__init__(parent)
        self.settings, self.save_settings = settings, save_settings
        self.config = dict(settings.get("campus_network", {}))
        self.worker = None
        self.stopping = False
        self.last_ssids = None
        self.config.setdefault("ssid", "LEDE1")
        self.config.setdefault("portal_url", DEFAULT_PORTAL)
        self.client = None
        self.schedule = LoginSchedule(self.config)
        self._build_ui()
        self._load_credentials()
        self.browser_client = self.client
        self.active_password = self.password.text()
        self.timer = QTimer(self)
        self.timer.setInterval(10000)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        QTimer.singleShot(500, self.poll)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 28)
        title = QLabel("校园网自动登录"); title.setObjectName("pageTitle"); layout.addWidget(title)
        self.status = QLabel("正在检测 Wi-Fi…"); self.status.setWordWrap(True); layout.addWidget(self.status)
        box = QGroupBox("登录配置"); form = QFormLayout(box)
        self.portal = QLineEdit(self.config["portal_url"])
        self.ssid = QLineEdit(self.config["ssid"])
        self.username = QLineEdit(self.config.get("username", "")); self.username.setPlaceholderText("校园网账号 / 学号")
        self.password = QLineEdit(); self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("输入校园网密码")
        self.service = QLineEdit(self.config.get("service", "")); self.service.setPlaceholderText("当前门户留空即可")
        self.remember = QCheckBox("使用 Windows 本机加密保存密码")
        self.remember.setChecked(bool(self.config.get("password_blob")))
        for label, field in [("校园网页地址", self.portal), ("目标 Wi-Fi", self.ssid), ("账号", self.username),
                             ("密码", self.password), ("服务名称", self.service), ("", self.remember)]:
            form.addRow(label, field)
        layout.addWidget(box)
        automation = QGroupBox("自动登录"); auto = QFormLayout(automation)
        self.wifi_auto = QCheckBox("连接目标 Wi-Fi 后自动登录（启动时也检测）")
        self.wifi_auto.setChecked(self.config.get("wifi_auto", True))
        self.interval_enabled = QCheckBox("定时检查，已在线时跳过登录")
        self.interval_enabled.setChecked(self.config.get("interval_enabled", True))
        self.interval = QSpinBox(); self.interval.setRange(1, 1440)
        self.interval.setValue(self.config.get("interval_minutes", 30)); self.interval.setSuffix(" 分钟")
        self.daily_enabled = QCheckBox("每天定时登录")
        self.daily_enabled.setChecked(self.config.get("daily_enabled", False))
        self.daily = QTimeEdit(QTime.fromString(self.config.get("daily_time", "08:00"), "HH:mm"))
        self.daily.setDisplayFormat("HH:mm")
        self.background = QCheckBox("关闭窗口后保留在系统托盘，继续自动登录")
        self.background.setChecked(self.config.get("background", False))
        auto.addRow("Wi-Fi 触发", self.wifi_auto)
        interval_row = QHBoxLayout(); interval_row.addWidget(self.interval_enabled); interval_row.addWidget(self.interval)
        auto.addRow("间隔定时", interval_row)
        daily_row = QHBoxLayout(); daily_row.addWidget(self.daily_enabled); daily_row.addWidget(self.daily)
        auto.addRow("每日定时", daily_row); auto.addRow("后台运行", self.background)
        note = QLabel("自动操作仅在目标 Wi-Fi 下触发，每 10 秒检测一次。失败后间隔 60 秒重试，连续失败 3 次后等待重新连接或保存配置。电脑休眠期间暂停，唤醒后补检。")
        note.setObjectName("muted"); note.setWordWrap(True); auto.addRow(note)
        layout.addWidget(automation)
        buttons = QHBoxLayout()
        self.save_button = QPushButton("保存配置"); self.save_button.clicked.connect(self.save)
        self.login_button = QPushButton("立即登录"); self.login_button.setObjectName("primary")
        self.login_button.clicked.connect(self.manual_login)
        self.web_button = QPushButton("打开校园网页")
        self.web_button.clicked.connect(self.open_portal)
        for button in (self.save_button, self.login_button, self.web_button): buttons.addWidget(button)
        buttons.addStretch(); layout.addLayout(buttons)
        self.log = QTextEdit(); self.log.setReadOnly(True); self.log.document().setMaximumBlockCount(200)
        self.log.setPlaceholderText("检测与登录日志（仅保留本次运行记录）"); layout.addWidget(self.log, 1)

    def _load_credentials(self):
        try:
            self.password.setText(unprotect_password(self.config.get("password_blob", "")))
        except (RuntimeError, ValueError, UnicodeError):
            self.append_log("已保存密码读取失败，请重新输入并保存。")
        try:
            self.client = PortalClient(self.config["portal_url"])
        except ValueError:
            self.append_log("门户地址格式错误，请重新填写。")

    def append_log(self, message):
        # Plain text insertion prevents server messages from rendering HTML.
        self.log.append(html_escape(f"{datetime.now():%H:%M:%S}  {message}"))

    def save(self):
        if self.worker:
            return
        try:
            client = PortalClient(self.portal.text())
            if not self.ssid.text().strip():
                raise ValueError("请填写目标 Wi-Fi 名称。")
            config = {
                "portal_url": self.portal.text().strip(), "ssid": self.ssid.text().strip(),
                "username": self.username.text().strip(), "service": self.service.text().strip(),
                "password_blob": protect_password(self.password.text()) if self.remember.isChecked() and self.password.text() else "",
                "wifi_auto": self.wifi_auto.isChecked(), "interval_enabled": self.interval_enabled.isChecked(),
                "interval_minutes": self.interval.value(), "daily_enabled": self.daily_enabled.isChecked(),
                "daily_time": self.daily.time().toString("HH:mm"), "background": self.background.isChecked(),
            }
            previous = self.settings.get("campus_network")
            self.settings["campus_network"] = config
            try:
                self.save_settings(self.settings)
            except OSError:
                if previous is None: self.settings.pop("campus_network", None)
                else: self.settings["campus_network"] = previous
                raise RuntimeError("配置保存失败，请检查配置目录写入权限。")
            self.config, self.client = config, client
            self.browser_client = client
            self.active_password = self.password.text()
            self.schedule = LoginSchedule(config)
            self.append_log("配置已保存，自动登录设置已生效。")
            self.poll()
        except (RuntimeError, ValueError) as error:
            self.append_log(str(error))

    def poll(self):
        if not self.stopping and not self.worker:
            self._start_worker()

    def _start_worker(self, credentials=None, client=None):
        self.worker = CampusWorker(client or self.client, credentials, self)
        self.login_button.setEnabled(False); self.save_button.setEnabled(False)
        self.worker.finished.connect(self._finished)
        self.worker.start()

    def manual_login(self):
        if self.worker or self.stopping:
            return
        try:
            client = self.client if self.portal.text().strip() == self.config["portal_url"] else PortalClient(self.portal.text())
            client = client or PortalClient(self.portal.text())
        except ValueError as error:
            self.append_log(str(error)); return
        self.append_log("正在手动检查并登录…")
        self._start_worker((self.username.text(), self.password.text(), self.service.text(), self.ssid.text().strip()), client)

    def open_portal(self):
        try:
            entered = self.portal.text().strip()
            client = self.browser_client if self.browser_client and self.browser_client.portal_url == entered else PortalClient(entered)
            QDesktopServices.openUrl(QUrl(client.browser_url))
        except ValueError as error:
            self.append_log(str(error))

    def _finished(self):
        worker = self.worker
        result = worker.result
        self.worker = None
        worker.deleteLater()
        self.login_button.setEnabled(True); self.save_button.setEnabled(True)
        if self.stopping:
            return
        ssids = result.get("ssids", ())
        if ssids != self.last_ssids:
            changed_connection = self.last_ssids is not None
            self.last_ssids = ssids
            if changed_connection and not worker.credentials:
                if self.client: self.client.user_index = ""
                if self.browser_client: self.browser_client.user_index = ""
            self.append_log("当前 Wi-Fi：" + ("、".join(ssids) or "未连接"))
        self.status.setText("当前 Wi-Fi：" + ("、".join(ssids) or "未连接"))
        if result.get("message"):
            self.append_log(result["message"])
            self.status.setText(self.status.text() + "  ·  " + result["message"])
        if worker.credentials:
            self.browser_client = worker.client
            self.schedule.previous = ssids
            self.schedule.completed(result.get("success", False))
            return
        if not result.get("success"):
            return
        # Use saved options; credentials may be entered for this run without storage.
        if not self.client or not self.config.get("username") or not self.active_password:
            return
        reason = self.schedule.reason(ssids, datetime.now())
        if reason:
            self.append_log(reason + "，正在检查并登录…")
            self._start_worker((self.config["username"], self.active_password,
                                self.config.get("service", ""), self.config["ssid"]))

    def stop(self):
        self.stopping = True
        self.timer.stop()
        if self.worker: self.worker.requestInterruption()


def html_escape(value):
    from html import escape
    return escape(value)
