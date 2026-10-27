"""Settings panel for checking, downloading and handing off GitHub updates."""
from pathlib import Path
import threading

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QProgressBar, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from updater import GitHubUpdater, RELEASES_URL, REPOSITORY, TokenStore, UpdateError, verify_installer


class UpdateWorker(QThread):
    progress = Signal(int, int)

    def __init__(self, action, current, token, directory, release=None, artifact=None, parent=None):
        super().__init__(parent)
        self.action = action
        self.current = current
        self.token = token
        self.directory = directory
        self.release = release
        self.artifact = artifact
        self.cancel = threading.Event()
        self.result = None
        self.error = ""

    def run(self):
        client = None
        try:
            if self.action == "install":
                verify_installer(self.artifact)
                self.result = self.artifact
            else:
                client = GitHubUpdater(self.current, self.token)
                if self.action == "check":
                    self.result = client.check()
                else:
                    self.result = client.download(self.release, self.directory, self.cancel, self.progress.emit)
        except UpdateError as error:
            self.error = str(error)
        except Exception:
            self.error = "更新操作失败，请检查网络、磁盘空间和更新设置后重试。"
        finally:
            self.token = ""
            if client:
                client.close()


class UpdatePanel(QWidget):
    install_requested = Signal(object)

    def __init__(self, version, directory, parent=None):
        super().__init__(parent)
        self.version = version
        self.directory = Path(directory)
        self.store = TokenStore(self.directory / "credentials.json")
        self.worker = None
        self.release = None
        self.artifact = None
        self.stopping = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        box = QGroupBox("软件更新")
        form = QFormLayout(box)
        self.version_label = QLabel(f"当前版本 {version}  ·  更新源 {REPOSITORY}")
        self.version_label.setWordWrap(True)
        form.addRow(self.version_label)
        row = QHBoxLayout()
        self.check_button = QPushButton("检查更新")
        self.check_button.setObjectName("primary")
        self.check_button.clicked.connect(self.check)
        self.download_button = QPushButton("下载更新")
        self.download_button.clicked.connect(lambda: self.start("download"))
        self.install_button = QPushButton("退出并安装更新")
        self.install_button.clicked.connect(lambda: self.start("install"))
        self.cancel_button = QPushButton("取消下载")
        self.cancel_button.clicked.connect(self.cancel)
        self.release_button = QPushButton("GitHub 发布页")
        self.release_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(RELEASES_URL)))
        for button in (self.check_button, self.download_button, self.install_button, self.cancel_button, self.release_button):
            row.addWidget(button)
        form.addRow(row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("尚未下载")
        form.addRow(self.progress)
        self.status = QLabel(self.store.error or "点击检查 GitHub 最新正式版；发现新版后可下载安装。")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow(self.status)
        self.notes = QTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMaximumHeight(90)
        self.notes.setPlaceholderText("新版本的更新说明将显示在这里")
        form.addRow(self.notes)
        self.token = QLineEdit(self.store.token)
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("私有仓库填写 GitHub Token；公开仓库可留空")
        self.token.setAccessibleName("软件更新 GitHub Token")
        form.addRow("GitHub Token", self.token)
        credential_row = QHBoxLayout()
        self.remember = QCheckBox("使用 Windows 本机加密保存 Token")
        self.remember.setChecked(bool(self.store.token))
        self.save_button = QPushButton("保存更新设置")
        self.save_button.clicked.connect(self.save_credentials)
        credential_row.addWidget(self.remember)
        credential_row.addStretch()
        credential_row.addWidget(self.save_button)
        form.addRow(credential_row)
        hint = QLabel("私有仓库需要 Token 可访问 csy5522522/OMINI-，权限为 Contents 只读。取消勾选并保存会删除已保存的 Token。下载校验通过后才会启用安装。")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        form.addRow(hint)
        layout.addWidget(box)
        self.update_controls()

    def persist_credentials(self):
        try:
            self.store.save(self.token.text().strip(), self.remember.isChecked())
            return True
        except (OSError, ValueError, RuntimeError):
            self.status.setText("更新凭据保存失败，请检查目录权限或取消加密保存后重试。")
            return False

    def save_credentials(self):
        if self.persist_credentials():
            self.status.setText("更新设置已保存。" if self.remember.isChecked() else "Token 仅用于本次运行，已移除保存的凭据。")

    def check(self):
        if self.worker or self.stopping or not self.persist_credentials():
            return
        self.release = None
        self.artifact = None
        self.notes.clear()
        self.progress.setValue(0)
        self.progress.setFormat("尚未下载")
        self.start("check")

    def start(self, action):
        if self.worker or self.stopping:
            return
        if (action == "download" and not self.release) or (action == "install" and not self.artifact):
            return
        self.worker = UpdateWorker(action, self.version, self.token.text(), self.directory / "downloads",
                                   self.release, self.artifact, self)
        self.worker.progress.connect(self.show_progress)
        self.worker.finished.connect(self.finished)
        self.status.setText({"check": "正在检查 GitHub 最新正式版本…", "download": "正在下载并校验更新安装包…",
                             "install": "正在再次核对安装包，随后退出并安装…"}[action])
        self.update_controls()
        self.worker.start()

    def show_progress(self, done, total):
        self.progress.setValue(int(done * 100 / total) if total else 0)
        self.progress.setFormat(f"{done / 1024 ** 2:.1f} / {total / 1024 ** 2:.1f} MB  ·  %p%")

    def finished(self):
        worker = self.worker
        self.worker = None
        if not self.stopping:
            if worker.error:
                self.status.setText(worker.error)
                if worker.action == "install":
                    self.artifact = None
            elif worker.action == "check":
                self.release = worker.result
                if self.release:
                    self.notes.setPlainText(self.release.notes)
                    self.status.setText(f"发现新版本 {self.release.version}（当前 {self.version}），安装包 {self.release.size / 1024 ** 2:.1f} MB。")
                else:
                    self.status.setText(f"当前 {self.version} 已是最新正式版本。")
            elif worker.action == "download":
                self.artifact = worker.result
                self.status.setText("下载完成，SHA-256 校验通过。点击「退出并安装更新」继续。")
            elif worker.action == "install":
                self.install_requested.emit(worker.result)
        self.update_controls()
        worker.deleteLater()

    def cancel(self):
        if self.worker:
            self.worker.cancel.set()
            self.status.setText("正在取消下载…")

    def update_controls(self):
        ready = self.worker is None and not self.stopping
        self.check_button.setEnabled(ready)
        self.download_button.setEnabled(ready and self.release is not None)
        self.install_button.setEnabled(ready and self.artifact is not None)
        self.cancel_button.setEnabled(self.worker is not None and self.worker.action == "download" and not self.stopping)
        self.save_button.setEnabled(ready)
        self.token.setEnabled(ready)
        self.remember.setEnabled(ready)

    def stop(self):
        self.stopping = True
        if self.worker:
            self.worker.cancel.set()
        self.update_controls()
