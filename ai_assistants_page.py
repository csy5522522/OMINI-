"""Desktop proxy launchers and Codex member pool controls."""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QLockFile, QProcess, QProcessEnvironment, QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLayout, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget,
    QTextBrowser, QVBoxLayout, QWidget,
)

from ai_assistants import (
    ASSISTANTS, ENGINE_REPO, ENGINE_VERSION, SCHEDULING, AssistantStore, PoolClient, atomic_json,
    available_port, detect_program, engine_path, install_engine, launch_assistant,
    pool_config, powershell_environment, proxy_environment, redact, test_connection,
    member_rule, remember_member_rules, validate_profile, validate_proxy,
)
from codex_quota import quota_cell
from assistant_data import AssistantState, OperationHistory, backup_credentials, history_line, local_members, settings_fingerprint
from api_members import (API_PROTOCOLS, api_member_rows, models_text, parse_models,
                         probe_api_member, validate_api_member, validate_api_members)


class AssistantWorker(QThread):
    result = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, action, parent=None):
        super().__init__(parent)
        self.action = action

    def run(self):
        try:
            self.result.emit(self.action(self))
        except Exception as error:
            self.failed.emit(str(error))


class AssistantProfile(QWidget):
    def __init__(self, name, profile, owner):
        super().__init__()
        self.name = name
        self.owner = owner
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 14, 12, 10)
        connection = QGroupBox("独立代理环境")
        form = QFormLayout(connection)
        self.mode = QComboBox()
        for title, key in (("使用指定代理", "custom"), ("直接连接", "direct"), ("继承工具箱启动环境", "inherit")):
            self.mode.addItem(title, key)
        self.mode.setCurrentIndex(max(0, self.mode.findData(profile["mode"])))
        self.proxy = QLineEdit(profile["proxy"])
        self.proxy.setPlaceholderText("http://127.0.0.1:7890")
        self.no_proxy = QLineEdit(profile["no_proxy"])
        form.addRow("连接方式", self.mode)
        form.addRow("代理地址", self.proxy)
        form.addRow("直连地址", self.no_proxy)
        description = QLabel("代理随此助手启动，并传给它的子进程。Claude Code 请选择 HTTP/HTTPS 端口。")
        description.setObjectName("muted")
        description.setWordWrap(True)
        form.addRow(description)
        layout.addWidget(connection)
        program_box = QGroupBox("启动程序")
        launch_form = QFormLayout(program_box)
        self.kind = QComboBox()
        if name == "codex":
            self.kind.addItems(["Codex CLI", "Codex 桌面版"])
            self.kind.setCurrentIndex(1 if profile.get("desktop") else 0)
            launch_form.addRow("客户端", self.kind)
        self.program = QLineEdit(profile.get("program", ""))
        self.program.setPlaceholderText("点击检测程序，或选择已安装的 .exe / .cmd")
        program_row = QHBoxLayout()
        program_row.addWidget(self.program, 1)
        detect = QPushButton("检测程序")
        detect.clicked.connect(self.detect)
        program_row.addWidget(detect)
        choose = QPushButton("选择文件")
        choose.clicked.connect(self.choose_program)
        program_row.addWidget(choose)
        launch_form.addRow("程序位置", program_row)
        self.cwd = QLineEdit(profile.get("cwd", str(Path.home())))
        cwd_row = QHBoxLayout()
        cwd_row.addWidget(self.cwd, 1)
        browse = QPushButton("选择目录")
        browse.clicked.connect(self.choose_cwd)
        cwd_row.addWidget(browse)
        launch_form.addRow("工作目录", cwd_row)
        self.args = QPlainTextEdit("\n".join(profile.get("args", [])))
        self.args.setPlaceholderText("可选，每行一个参数；路径里的空格无需额外加引号")
        self.args.setMaximumHeight(70)
        launch_form.addRow("附加参数", self.args)
        layout.addWidget(program_box)
        row = QHBoxLayout()
        self.save_button = QPushButton("保存配置")
        self.save_button.clicked.connect(lambda: owner.save_profile(name))
        row.addWidget(self.save_button)
        self.test_button = QPushButton("测试网络")
        self.test_button.clicked.connect(lambda: owner.test_profile(name))
        row.addWidget(self.test_button)
        self.launch_button = QPushButton("带此环境启动")
        self.launch_button.clicked.connect(lambda: owner.launch_profile(name))
        row.addWidget(self.launch_button)
        copy_button = QPushButton("复制 PowerShell 环境")
        copy_button.clicked.connect(self.copy_environment)
        row.addWidget(copy_button)
        row.addStretch()
        layout.addLayout(row)
        self.status = QLabel("尚未测试网络。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        note = QLabel("先保存，再从这里启动。已打开的客户端请先正常退出，再重新启动，让新的代理环境生效。网络测试只验证连接，不代表账号已登录。")
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note)
        layout.addStretch()
        self.mode.currentIndexChanged.connect(self.update_mode)
        if name == "codex":
            self.kind.currentIndexChanged.connect(lambda: self.program.clear())
        self.update_mode()

    def update_mode(self):
        self.proxy.setEnabled(self.mode.currentData() == "custom")
        self.no_proxy.setEnabled(self.mode.currentData() != "inherit")

    def value(self):
        return validate_profile(self.name, self.raw_value())

    def raw_value(self):
        return {
            "mode": self.mode.currentData(), "proxy": self.proxy.text().strip(),
            "no_proxy": self.no_proxy.text().strip(), "program": self.program.text().strip(),
            "cwd": self.cwd.text().strip(), "args": self.args.toPlainText().splitlines(),
            "desktop": self.name == "codex" and self.kind.currentIndex() == 1,
        }

    def detect(self):
        desktop = self.name == "codex" and self.kind.currentIndex() == 1
        def found(value):
            if value:
                self.program.setText(value)
                self.status.setText("已找到程序；保存后即可带代理启动。")
                self.owner.report(ASSISTANTS[self.name][0] + "：程序检测成功。", "profile.detect")
            else:
                self.status.setText("未检测到程序，请安装该助手或手动选择程序文件。")
                self.owner.report(ASSISTANTS[self.name][0] + "：未检测到程序。", "profile.detect")
        self.owner.run_action(lambda _: detect_program(self.name, desktop), found)

    def choose_program(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择助手程序", "", "程序 (*.exe *.cmd *.bat);;所有文件 (*)")
        if path:
            self.program.setText(path)

    def choose_cwd(self):
        path = QFileDialog.getExistingDirectory(self, "选择工作目录", self.cwd.text())
        if path:
            self.cwd.setText(path)

    def copy_environment(self):
        try:
            QApplication.clipboard().setText(powershell_environment(self.value()))
            self.status.setText("已复制代理环境，在新的 PowerShell 中粘贴后可运行助手命令。")
            self.owner.report(ASSISTANTS[self.name][0] + "：已复制代理环境。", "profile.copy_environment")
        except ValueError as error:
            self.owner.report(str(error))


class ApiMemberDialog(QDialog):
    def __init__(self, owner, member=None):
        super().__init__(owner)
        self.owner = owner
        self.value = None
        self._probe_worker = None
        member = member or {}
        self.setWindowTitle("编辑 API 线路" if member else "添加 API 线路")
        self.resize(660, 640)
        layout = QFormLayout(self)
        self.label = QLineEdit(member.get("label", ""))
        self.label.setPlaceholderText("例如：主线路 / 备用线路")
        self.protocol = QComboBox()
        for key, spec in API_PROTOCOLS.items(): self.protocol.addItem(spec[0], key)
        self.protocol.setCurrentIndex(max(0, self.protocol.findData(member.get("protocol", "openai"))))
        self.base_url = QLineEdit(member.get("base_url", API_PROTOCOLS[self.protocol.currentData()][2]))
        self.api_key = QLineEdit(member.get("api_key", ""))
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("API Key，加密保存")
        self.proxy = QLineEdit(member.get("proxy", ""))
        self.proxy.setPlaceholderText("留空继承账号池；direct 直连；或 http://127.0.0.1:7890")
        self.models = QPlainTextEdit(models_text(member.get("models", [])))
        self.models.setPlaceholderText("每行一个模型 ID；可用 上游ID => 对外模型ID 设置别名\n不同线路使用相同的对外 ID 时可共同参与分配")
        self.models.setMinimumHeight(120)
        self.headers = QPlainTextEdit(json.dumps(member.get("headers", {}), ensure_ascii=False, indent=2))
        self.headers.setPlaceholderText('可选 JSON，例如 {"X-Custom-Header": "value"}')
        self.headers.setMaximumHeight(75)
        self.disabled = QCheckBox("停用此线路")
        self.disabled.setChecked(member.get("disabled", False))
        self.fields = (self.label, self.protocol, self.base_url, self.api_key, self.proxy, self.models, self.headers, self.disabled)
        for label, field in (("线路名称", self.label), ("API 协议", self.protocol), ("API 基础地址", self.base_url),
                             ("API Key", self.api_key), ("出口代理", self.proxy), ("模型 ID / 别名", self.models), ("附加请求头", self.headers)):
            layout.addRow(label, field)
        layout.addRow(self.disabled)
        self.note = QLabel("读取模型检查鉴权和线路；验证模型会向上游发送一次小请求，消耗少量用量。验证使用第一行的上游模型 ID。")
        self.note.setWordWrap(True)
        self.note.setTextFormat(Qt.TextFormat.PlainText)
        layout.addRow(self.note)
        actions = QHBoxLayout()
        self.discover = QPushButton("读取模型 / 检查线路")
        self.discover.clicked.connect(lambda: self.probe(False))
        self.verify = QPushButton("验证模型（少量用量）")
        self.verify.clicked.connect(lambda: self.probe(True))
        actions.addWidget(self.discover); actions.addWidget(self.verify)
        layout.addRow(actions)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存线路")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        layout.addRow(self.buttons)
        self.protocol.currentIndexChanged.connect(self.protocol_changed)

    def protocol_changed(self):
        if not self.base_url.text().strip() or self.base_url.text() in {s[2] for s in API_PROTOCOLS.values()}:
            self.base_url.setText(API_PROTOCOLS[self.protocol.currentData()][2])

    def member_value(self, *, require_models=True):
        try:
            headers = json.loads(self.headers.toPlainText().strip() or "{}")
        except ValueError:
            raise ValueError("附加请求头请填写 JSON 对象。") from None
        return validate_api_member({"label": self.label.text(), "protocol": self.protocol.currentData(),
            "base_url": self.base_url.text(), "api_key": self.api_key.text(), "proxy": self.proxy.text(),
            "models": parse_models(self.models.toPlainText()), "headers": headers,
            "disabled": self.disabled.isChecked()}, require_models=require_models)

    def save(self):
        try:
            self.value = self.member_value()
            self.accept()
        except ValueError as error:
            self.note.setText(str(error))

    def probe(self, generate):
        try:
            member = self.member_value(require_models=generate)
            proxy = validate_proxy(self.owner.pool_proxy.text(), allow_empty=True)
        except ValueError as error:
            self.note.setText(str(error)); return
        def success(result):
            self.note.setText(result["message"])
            if result["ok"] and not generate and result["models"]:
                existing = parse_models(self.models.toPlainText())
                names = {m["name"] for m in existing}
                aliases = {m["alias"] for m in existing}
                existing.extend({"name": name, "alias": name} for name in result["models"] if name not in names and name not in aliases)
                self.models.setPlainText(models_text(existing[:500]))
        if self.owner.run_action(lambda _: probe_api_member(member, proxy, generate=generate), success,
                                 lambda message: self.note.setText(message)):
            self._probe_worker = self.owner.worker
            self.note.setText("正在验证模型…" if generate else "正在检查线路并读取模型…")
            self.buttons.setEnabled(False); self.discover.setEnabled(False); self.verify.setEnabled(False)
            for field in self.fields: field.setEnabled(False)
            self._probe_worker.finished.connect(self.probe_finished)

    def probe_finished(self):
        self._probe_worker = None
        self.buttons.setEnabled(True); self.discover.setEnabled(True); self.verify.setEnabled(True)
        for field in self.fields: field.setEnabled(True)

    def reject(self):
        if self._probe_worker is None:
            super().reject()


class AiAssistantsPage(QWidget):
    def __init__(self, root: Path, parent=None):
        super().__init__(parent)
        self.store = AssistantStore(root)
        self.root = self.store.root
        self.state_store = AssistantState(self.root)
        self.history = OperationHistory(self.root)
        self.load_warning = self.store.error or self.state_store.error
        fingerprint = settings_fingerprint(self.store.data)
        self.saved_draft = (self.state_store.data.get("draft", {})
                            if self.state_store.data.get("settings_fingerprint", fingerprint) == fingerprint else {})
        self._restoring = True
        self._pending_changes = set()
        self.service_wanted = bool(self.state_store.data.get("service_wanted", False))
        self._stop_requested = False
        self.worker = None
        self.stopping = False
        self.ready = False
        self.active_pool = None
        self.runtime = None
        self.lock = None
        self.login_state = ""
        self.login_started = 0.0
        self.started_at = 0.0
        self.accounts = []
        self.quotas = copy.deepcopy(self.state_store.data.get("quotas", {}))
        self.api_checks = copy.deepcopy(self.state_store.data.get("api_checks", {}))
        for snapshot in self.quotas.values():
            snapshot["cached"] = True
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(700)
        self.save_timer.timeout.connect(self.save_session)
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.finished.connect(self.process_finished)
        self.process.errorOccurred.connect(self.process_error)
        self.start_timer = QTimer(self)
        self.start_timer.setInterval(750)
        self.start_timer.timeout.connect(self.check_ready)
        self.login_timer = QTimer(self)
        self.login_timer.setInterval(2000)
        self.login_timer.timeout.connect(self.poll_login)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 12)
        heading = QLabel("AI 编程助手")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        subtitle = QLabel("分别设置代理，一键启动 Codex、Gemini、反重力和 Claude Code；混合管理登录成员与 API Key 线路。")
        subtitle.setWordWrap(True)
        subtitle.setObjectName("muted")
        layout.addWidget(subtitle)
        self.tabs = QTabWidget()
        self.profiles = {}
        draft = self.saved_draft
        for name, (title, _) in ASSISTANTS.items():
            value = copy.deepcopy(self.store.data["profiles"][name])
            value.update(draft.get("profiles", {}).get(name, {}))
            profile = AssistantProfile(name, value, self)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setWidget(profile)
            self.profiles[name] = profile
            self.tabs.addTab(scroll, title)
        self.build_pool()
        self.build_history()
        self.build_help()
        layout.addWidget(self.tabs, 1)
        self.status = QLabel(self.store.error or self.state_store.error or "就绪。配置、成员与操作记录保存在用户数据目录。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.restore_session()
        self.connect_persistence()
        self._restoring = False
        try:
            backup_credentials(self.root)
        except (OSError, ValueError, RuntimeError) as error:
            self.report("成员备份失败：" + str(error), "backup.error")
        self.report(f"已加载 {len(self.accounts)} 个本地成员；" +
                    (f"已恢复 {len(self.quotas)} 个额度快照。" if self.quotas else "可启动服务查询额度。"), "app.open")
        if self.load_warning:
            self.report(self.load_warning, "storage.warning")
        self.update_controls()
        QTimer.singleShot(500, self.resume_pool)

    def build_pool(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        box = QGroupBox("本地账号池服务")
        form = QFormLayout(box)
        pool = copy.deepcopy(self.store.data["pool"])
        pool.update(self.saved_draft.get("pool", {}))
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(int(pool["port"]))
        self.pool_proxy = QLineEdit(pool["proxy"])
        self.pool_proxy.setPlaceholderText("http://127.0.0.1:7890；留空直接连接")
        self.strategy = QComboBox()
        for key, (label, _) in SCHEDULING.items(): self.strategy.addItem(label, key)
        self.strategy.setCurrentIndex(max(0, self.strategy.findData(pool["strategy"])))
        self.affinity = QCheckBox("同一会话保持账号，新增会话按策略分配")
        self.affinity.setChecked(bool(pool.get("session_affinity", True)))
        self.background = QCheckBox("关闭窗口后在托盘继续运行账号池")
        self.background.setChecked(bool(pool.get("background", True)))
        self.resume = QCheckBox("启动工具箱时恢复上次运行的账号池")
        self.resume.setChecked(bool(pool.get("resume_on_launch", True)))
        endpoint_row = QHBoxLayout()
        endpoint_row.addWidget(QLabel("端口"))
        endpoint_row.addWidget(self.port)
        endpoint_row.addWidget(QLabel("调度"))
        endpoint_row.addWidget(self.strategy, 1)
        form.addRow(endpoint_row)
        form.addRow("出口代理", self.pool_proxy)
        self.strategy_hint = QLabel()
        self.strategy_hint.setWordWrap(True); self.strategy_hint.setObjectName("muted")
        form.addRow(self.strategy_hint)
        form.addRow(self.affinity)
        form.addRow(self.background)
        form.addRow(self.resume)
        self.endpoint = QLineEdit()
        self.endpoint.setReadOnly(True)
        self.port.valueChanged.connect(self.update_endpoint)
        self.update_endpoint()
        endpoint_bar = QHBoxLayout()
        endpoint_bar.addWidget(self.endpoint, 1)
        key = QPushButton("复制本地 API Key")
        key.clicked.connect(lambda: self.copy_key())
        endpoint_bar.addWidget(key)
        form.addRow("本地接口", endpoint_bar)
        controls = QHBoxLayout()
        self.install_button = QPushButton("安装代理引擎")
        self.install_button.clicked.connect(self.install)
        self.start_button = QPushButton("保存并启动服务")
        self.start_button.clicked.connect(self.start_pool)
        self.stop_button = QPushButton("停止服务")
        self.stop_button.clicked.connect(lambda: self.stop_pool())
        self.save_pool_button = QPushButton("保存并应用调度")
        self.save_pool_button.clicked.connect(self.apply_schedule)
        for button in (self.install_button, self.start_button, self.stop_button, self.save_pool_button):
            controls.addWidget(button)
        controls.addStretch()
        form.addRow(controls)
        self.engine_status = QLabel()
        self.engine_status.setObjectName("muted")
        form.addRow(self.engine_status)
        layout.addWidget(box)
        members = QHBoxLayout()
        self.login_button = QPushButton("登录并添加成员")
        self.login_button.clicked.connect(self.begin_login)
        self.import_button = QPushButton("导入账号 JSON")
        self.import_button.clicked.connect(self.import_accounts)
        self.refresh_button = QPushButton("刷新账号")
        self.refresh_button.clicked.connect(self.refresh_accounts)
        self.toggle_button = QPushButton("启用 / 停用")
        self.toggle_button.clicked.connect(self.toggle_account)
        self.remove_button = QPushButton("移出账号池")
        self.remove_button.clicked.connect(self.remove_account)
        for button in (self.login_button, self.import_button, self.refresh_button, self.toggle_button, self.remove_button):
            members.addWidget(button)
        members.addStretch()
        layout.addLayout(members)
        api_tools = QHBoxLayout()
        self.add_api_button = QPushButton("添加 API / Key")
        self.add_api_button.clicked.connect(lambda: self.edit_api_member(False))
        self.edit_api_button = QPushButton("编辑 API 线路")
        self.edit_api_button.clicked.connect(lambda: self.edit_api_member(True))
        self.check_api_button = QPushButton("检查所选线路")
        self.check_api_button.clicked.connect(lambda: self.check_api_member(False))
        self.verify_api_button = QPushButton("验证模型（少量用量）")
        self.verify_api_button.clicked.connect(lambda: self.check_api_member(True))
        for button in (self.add_api_button, self.edit_api_button, self.check_api_button, self.verify_api_button):
            api_tools.addWidget(button)
        api_tools.addStretch(); layout.addLayout(api_tools)
        login_row = QHBoxLayout()
        self.login_url = QLineEdit()
        self.login_url.setReadOnly(True)
        self.login_url.setPlaceholderText("添加成员时，在浏览器登录；每个成员分别登录一次")
        login_row.addWidget(self.login_url, 1)
        self.reopen_login = QPushButton("打开登录页")
        self.reopen_login.clicked.connect(self.open_login_url)
        login_row.addWidget(self.reopen_login)
        self.cancel_button = QPushButton("取消登录")
        self.cancel_button.clicked.connect(self.cancel_login)
        login_row.addWidget(self.cancel_button)
        layout.addLayout(login_row)
        quota_tools = QHBoxLayout()
        self.quota_button = QPushButton("查询所选额度"); self.quota_button.clicked.connect(lambda: self.query_quotas(False))
        self.all_quotas_button = QPushButton("查询全部额度"); self.all_quotas_button.clicked.connect(lambda: self.query_quotas(True))
        self.member_rule_button = QPushButton("成员优先级 / 权重"); self.member_rule_button.clicked.connect(self.edit_member_rule)
        self.prefer_button = QPushButton("设为优先账号"); self.prefer_button.clicked.connect(self.prefer_account)
        for button in (self.quota_button, self.all_quotas_button, self.member_rule_button, self.prefer_button):
            quota_tools.addWidget(button)
        quota_tools.addStretch(); layout.addLayout(quota_tools)
        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(["成员账号 / API 线路", "状态", "5 小时剩余", "周剩余", "优先级", "权重", "参与分配", "API 协议", "模型 ID", "线路检查"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 250)
        header.setStretchLastSection(True)
        for column, width in ((1, 120), (2, 160), (3, 160), (4, 65), (5, 60), (6, 75)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive); self.table.setColumnWidth(column, width)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.verticalHeader().hide()
        self.table.setMinimumHeight(130)
        self.table.itemSelectionChanged.connect(self.update_controls)
        layout.addWidget(self.table, 1)
        self.quota_note = QLabel("额度为查询时的剩余百分比与重置时间；未返回的周期显示「未返回」。选中账号后可单独查询。")
        self.quota_note.setWordWrap(True); self.quota_note.setObjectName("muted"); layout.addWidget(self.quota_note)
        models = QHBoxLayout()
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setPlaceholderText("读取模型列表后选择，或填写账号实际支持的模型")
        self.model.setCurrentText(pool.get("model", ""))
        models.addWidget(QLabel("模型"))
        models.addWidget(self.model, 1)
        self.models_button = QPushButton("读取模型")
        self.models_button.clicked.connect(self.read_models)
        models.addWidget(self.models_button)
        self.pool_launch_button = QPushButton("通过账号池启动 Codex")
        self.pool_launch_button.clicked.connect(self.launch_pool_codex)
        models.addWidget(self.pool_launch_button)
        layout.addLayout(models)
        self.pool_note = QLabel("登录成员与 API 线路可混合使用。相同对外模型 ID 的可用成员参与调度；API 线路保存后点击「保存并应用调度」生效。线路检查读取模型；验证模型消耗少量用量。")
        self.pool_note.setWordWrap(True)
        self.pool_note.setObjectName("muted")
        layout.addWidget(self.pool_note)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(120)
        self.log.setMaximumHeight(90)
        self.log.setMinimumHeight(70)
        layout.addWidget(self.log)
        for field in (self.port, self.pool_proxy, self.strategy, self.endpoint, self.login_url, self.model):
            field.setMinimumHeight(32)
        for button in widget.findChildren(QPushButton):
            button.setMinimumHeight(30)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(widget)
        self.pool_scroll = scroll
        self.tabs.addTab(scroll, "成员账号 / API 池")
        self.strategy.currentIndexChanged.connect(self.schedule_changed)
        self.schedule_changed()

    def build_history(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        note = QLabel("操作及结果按时间自动保存，重启与升级后保留。这里显示最近 1000 条操作，导出包含全部已保存记录。配置和记录使用 Windows 用户加密；成员凭据变更前自动备份。")
        note.setWordWrap(True); note.setObjectName("muted"); layout.addWidget(note)
        buttons = QHBoxLayout()
        export = QPushButton("导出全部操作记录 TXT"); export.clicked.connect(self.export_history)
        directory = QPushButton("打开数据与备份目录"); directory.clicked.connect(self.open_data_directory)
        buttons.addWidget(export); buttons.addWidget(directory); buttons.addStretch(); layout.addLayout(buttons)
        self.history_view = QPlainTextEdit(); self.history_view.setReadOnly(True)
        self.history_view.setMaximumBlockCount(1500)
        layout.addWidget(self.history_view, 1)
        self.tabs.addTab(widget, "操作记录")

    def restore_session(self):
        state = self.state_store.data
        models = [str(value) for value in state.get("models", [])]
        current = self.model.currentText()
        self.model.addItems(models)
        self.model.setCurrentText(current)
        try:
            records = self.history.records()
        except OSError:
            records = []
            self.status.setText("操作记录读取异常，请检查用户数据目录权限。")
        self.history_view.setPlainText("\n".join(history_line(record) for record in records))
        self.log.setPlainText("\n".join(history_line(record) for record in records[-100:]))
        rows, errors = local_members(self.root)
        self.set_accounts(rows, persist=False)
        ui = state.get("ui", {})
        name = ui.get("member", "")
        for index, row in enumerate(rows):
            if row.get("name") == name:
                self.table.selectRow(index)
                break
        tab = max(0, min(int(ui.get("tab", 4 if rows else 0)), self.tabs.count() - 1))
        self.tabs.setCurrentIndex(tab)
        for column, width in enumerate(ui.get("columns", [])[:self.table.columnCount()]):
            if isinstance(width, int) and 40 <= width <= 1200:
                self.table.setColumnWidth(column, width)
        QTimer.singleShot(0, lambda: self.pool_scroll.verticalScrollBar().setValue(int(ui.get("pool_scroll", 0))))
        if errors:
            self.report(f"{errors} 个本地凭据文件读取异常，原文件已保留。", "storage.warning")

    def apply_cloud(self):
        self._restoring = True
        try:
            local_drafts = {name for name, profile in self.profiles.items()
                            if profile.raw_value() != self.store.data["profiles"][name]}
            self.store = AssistantStore(self.root)
            self.state_store = AssistantState(self.root)
            for name, profile in self.profiles.items():
                if name in local_drafts: continue
                value = self.store.data["profiles"][name]
                profile.mode.setCurrentIndex(max(0, profile.mode.findData(value["mode"])))
                for key in ("proxy", "no_proxy"):
                    if getattr(profile, key).text() != value[key]: getattr(profile, key).setText(value[key])
                profile.args.setPlainText("\n".join(value.get("args", [])))
                if name == "codex": profile.kind.setCurrentIndex(1 if value.get("desktop") else 0)
            pool = self.store.data["pool"]
            self.strategy.setCurrentIndex(max(0, self.strategy.findData(pool["strategy"])))
            self.affinity.setChecked(pool["session_affinity"])
            self.model.setCurrentText(pool.get("model", ""))
            self.quotas = copy.deepcopy(self.state_store.data.get("quotas", {}))
            self.set_accounts(local_members(self.root)[0], persist=False)
            self.history_view.setPlainText("\n".join(history_line(r) for r in self.history.records()))
            self.status.setText("账号配置、登录成员与 API 线路已同步。运行中的服务配置可通过「保存并应用调度」生效。")
        finally:
            self._restoring = False
        if self.ready and not self.worker: self.refresh_accounts()

    def connect_persistence(self):
        for name, profile in self.profiles.items():
            label = ASSISTANTS[name][0] + " 配置"
            for field in (profile.proxy, profile.no_proxy, profile.program, profile.cwd):
                field.textChanged.connect(lambda _value, label=label: self.queue_save(label))
            profile.args.textChanged.connect(lambda label=label: self.queue_save(label))
            profile.mode.currentIndexChanged.connect(lambda _value, label=label: self.queue_save(label))
            profile.kind.currentIndexChanged.connect(lambda _value, label=label: self.queue_save(label))
        self.pool_proxy.textChanged.connect(lambda: self.queue_save("账号池配置"))
        self.port.valueChanged.connect(lambda: self.queue_save("账号池配置"))
        self.strategy.currentIndexChanged.connect(lambda: self.queue_save("调度选择"))
        for field in (self.affinity, self.background, self.resume):
            field.toggled.connect(lambda: self.queue_save("账号池配置"))
        self.model.currentTextChanged.connect(lambda: self.queue_save("模型选择"))
        self.tabs.currentChanged.connect(lambda: self.queue_save("助手标签"))
        self.table.itemSelectionChanged.connect(lambda: self.queue_save("所选成员"))
        self.table.horizontalHeader().sectionResized.connect(lambda: self.queue_save())
        self.pool_scroll.verticalScrollBar().valueChanged.connect(lambda: self.queue_save())

    def queue_save(self, label=""):
        if self._restoring or self.stopping:
            return
        if label:
            self._pending_changes.add(label)
        self.save_timer.start()

    def persist_state(self):
        data = copy.deepcopy(self.state_store.data)
        selected = self.selected_account()
        data.update(quotas=self.quotas, api_checks=self.api_checks, service_wanted=self.service_wanted,
                    settings_fingerprint=settings_fingerprint(self.store.data),
                    models=[self.model.itemText(i) for i in range(self.model.count())],
                    ui={"tab": self.tabs.currentIndex(), "member": selected.get("name", "") if selected else "",
                        "pool_scroll": self.pool_scroll.verticalScrollBar().value(),
                        "columns": [self.table.columnWidth(i) for i in range(self.table.columnCount())]},
                    draft={"profiles": {name: profile.raw_value() for name, profile in self.profiles.items()},
                           "pool": {"port": self.port.value(), "proxy": self.pool_proxy.text(),
                                    "strategy": self.strategy.currentData(), "session_affinity": self.affinity.isChecked(),
                                    "background": self.background.isChecked(), "resume_on_launch": self.resume.isChecked(),
                                    "model": self.model.currentText()}})
        try:
            self.state_store.save(data)
            return True
        except (OSError, ValueError, RuntimeError) as error:
            self.report("操作状态保存失败：" + str(error), "storage.error")
            return False

    def save_session(self):
        self.save_timer.stop()
        saved = False
        data = copy.deepcopy(self.store.data)
        for name, profile in self.profiles.items():
            try:
                data["profiles"][name] = profile.value()
            except ValueError:
                pass  # Preserve incomplete edits in the encrypted draft, without applying them.
        try:
            data["pool"] = self.pool_value()
        except ValueError:
            pass
        try:
            if data != self.store.data or not self.store.path.exists() or self.store.error:
                self.store.save(data)
            saved = self.persist_state()
            if saved and self._pending_changes:
                details = []
                for label in sorted(self._pending_changes):
                    if label == "调度选择":
                        label += "：" + self.strategy.currentText()
                    elif label == "模型选择":
                        label += "：" + (self.model.currentText() or "未选择")
                    elif label == "助手标签":
                        label += "：" + self.tabs.tabText(self.tabs.currentIndex())
                    elif label == "所选成员":
                        row = self.selected_account()
                        label += "：" + str(row.get("email") or row["name"]) if row else "：未选择"
                    details.append(label)
                self.report("已记住：" + "；".join(details) + "。", "settings.autosave")
        except (OSError, ValueError, RuntimeError) as error:
            self.report("配置保存失败：" + str(error), "storage.error")
        self._pending_changes.clear()
        return saved

    def resume_pool(self):
        if not self.stopping and self.service_wanted and self.resume.isChecked() and not self.is_running:
            self.report("正在恢复上次运行的账号池。", "service.resume")
            self.start_pool()

    def open_data_directory(self):
        self.root.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.root.resolve())))
        self.report("已打开数据与备份目录。", "data.open")

    def export_history(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出操作记录", "Omini-操作记录.txt", "文本文件 (*.txt)")
        if path:
            try:
                Path(path).write_text("\n".join(history_line(record) for record in self.history.records(limit=None, include_engine=True)) + "\n", encoding="utf-8-sig")
                self.report("操作记录已导出。", "history.export")
            except OSError as error:
                self.report("记录导出失败：" + str(error), "history.error")

    def build_help(self):
        help_view = QTextBrowser()
        help_view.setOpenExternalLinks(True)
        help_view.setHtml(f"""
        <h2>从这里开始</h2>
        <p>1. 进入对应助手标签，填代理地址（常见为 http://127.0.0.1:7890），检测程序并保存。</p>
        <p>2. 点击「测试网络」。HTTP 401 代表已连到服务但尚未认证；403/429/5xx 会显示原始状态码。测试不消耗模型额度。</p>
        <p>3. 正常退出已有助手，点击「带此环境启动」。CLI 在单独的 PowerShell 窗口运行；桌面程序同时收到代理环境和网络启动参数。</p>
        <h3>Codex 多成员轮询</h3>
        <p>在「成员账号 / API 池」安装引擎、设置出口代理并启动，逐个点击「登录并添加成员」，
        或导入 Codex auth.json / CLIProxyAPI 的 Codex JSON。重复导入同一账号更新它的凭据。</p>
        <p>点击「添加 API / Key」可加入 OpenAI Chat、Responses、Anthropic Messages、Gemini、Interactions、Vertex API Key、xAI 或 Meta 线路。
        填写基础地址和 Key，读取模型，或每行手填一个模型 ID；使用「上游ID =&gt; 对外ID」设置别名。
        多条线路的对外模型 ID 相同时可共同参与调度。每条线路可单独指定代理；留空继承账号池，direct 表示直连。
        「检查所选线路」只读取模型列表并显示鉴权、HTTP 状态与耗时；「验证模型」使用第一行上游 ID 发送一次小请求，消耗少量用量。
        API Key 线路的额度请在上游控制台查看。API 线路在停服时也可添加、编辑、启停或移出。</p>
        <p>「轮询分配」可保持同一会话；「雨露均沾」关闭会话保持，所有可用成员逐请求均分；
        「优先账号」先用高优先级成员，冷却时切换；「加权轮询」按成员权重分配。
        点击「成员优先级 / 权重」调整，或选中后「设为优先账号」。运行中点击「保存并应用调度」立即生效。
        优先级仅在优先模式使用，其余模式的成员按同级参与。均分请求数量不代表每个账号消耗相同额度。</p>
        <p>「查询所选额度 / 查询全部额度」读取账号的 5 小时及周限额，显示剩余百分比、重置时间和查询时间。
        未返回的周期显示「未返回」；查询失败保留旧快照并标记。查询只读取用量，不发起模型生成。
        账号失效或额度等待由引擎处理，停用成员会退出分配。移出只删除本地池中的凭据副本。</p>
        <p>在 Codex 标签选择 CLI 或桌面版、设置程序和工作目录。读取模型后点击「通过账号池启动 Codex」。
        此入口使用独立的 codex-pool/ 配置目录和本地 Responses 接口；账号池会话存放在该目录，
        原有 Codex 配置和当前登录保持原样。桌面版需要先正常退出已有窗口，再从这里启动。</p>
        <h3>保存位置与服务生命周期</h3>
        <p>配置与本地密钥使用 Windows 用户加密存入 %APPDATA%/Omini工具箱/ai-assistants/。
        引擎按其原生格式在 auths/ 保存 OAuth 凭据；运行配置在服务停止后清理。个人数据留在用户目录。</p>
        <p>账号池服务随工具箱运行，关闭到托盘时继续，完全退出时停止。已经打开的助手窗口由你自行退出。</p>
        <p>本地成员在服务未启动时仍可查看；额度保留上次查询快照，重启后会标记「上次」。
        配置编辑自动保存，所选成员、模型、标签和表格宽度会恢复。
        勾选恢复选项后，下次打开工具箱会恢复上次正在运行的服务；手动停止后保持停止。</p>
        <p>「操作记录」可查看与导出启动、停止、代理配置、调度、成员变更、额度查询和助手启动的记录。
        history/ 保存加密记录，settings.json.bak 与 state.json.bak 保存上次有效配置；
        backups/credentials/ 保存 Windows 用户加密的成员备份。卸载与升级保留用户数据目录。</p>
        <h3>项目与文档</h3>
        <p>账号池引擎：<a href="{ENGINE_REPO}">CLIProxyAPI v{ENGINE_VERSION}</a>（MIT，固定版本并校验 SHA-256）<br>
        <a href="https://help.router-for.me/management/apiv8">CLIProxyAPI v8 管理接口</a><br>
        <a href="https://developers.openai.com/codex/config-advanced/">Codex 自定义模型接口</a><br>
        <a href="https://code.claude.com/docs/en/network-config">Claude Code 代理配置</a><br>
        <a href="https://geminicli.com/docs/reference/configuration/">Gemini CLI 配置</a></p>
        """)
        self.tabs.addTab(help_view, "使用说明")

    @property
    def is_running(self):
        return self.process.state() != QProcess.ProcessState.NotRunning

    @property
    def wants_background(self):
        return self.is_running and self.background.isChecked()

    def update_endpoint(self):
        self.endpoint.setText(f"http://127.0.0.1:{self.port.value()}/v1")

    def report(self, text, event="action"):
        pool = self.store.data["pool"]
        secrets = [pool["api_key"], pool["management_key"]]
        for member in pool.get("api_members", {}).values():
            secrets.extend([member.get("api_key", ""), member.get("proxy", ""), *member.get("headers", {}).values()])
        secrets += [value.get("proxy", "") for value in self.store.data["profiles"].values()]
        text = redact(str(text), secrets)
        self.status.setText(text)
        try:
            record = self.history.append(text, event)
            line = history_line(record)
            if event != "engine":
                self.history_view.appendPlainText(line)
        except (OSError, ValueError, RuntimeError):
            line = text
            self.status.setText(text + "（操作记录写入失败，请检查数据目录。）")
        self.log.appendPlainText(line)

    def run_action(self, action, success=None, failure=None, *, quiet=False):
        if self.worker is not None and self.worker.isRunning():
            if not quiet:
                self.report("正在处理上一项操作，请稍后重试。")
            return False
        if self.stopping:
            return False
        worker = AssistantWorker(action, self)
        self.worker = worker
        def result(value):
            if not self.stopping and success:
                try:
                    success(value)
                except (ValueError, OSError, RuntimeError) as error:
                    self.report(str(error))
        def failed(message):
            if not self.stopping:
                (failure or self.report)(message)
        worker.result.connect(result)
        worker.failed.connect(failed)
        worker.progress.connect(self.report)
        worker.finished.connect(self.worker_finished)
        worker.start()
        self.update_controls()
        return True

    def worker_finished(self):
        worker = self.sender()
        if self.worker is worker:
            self.worker = None
        worker.deleteLater()
        self.update_controls()

    def save_profile(self, name):
        try:
            profile = self.profiles[name].value()
            data = copy.deepcopy(self.store.data)
            data["profiles"][name] = profile
            self.store.save(data)
            self.profiles[name].status.setText("配置已保存，下次从本页启动时生效。")
            self.persist_state()
            self.report(ASSISTANTS[name][0] + "：代理与启动配置已保存。", "profile.save")
            return profile
        except (ValueError, OSError, RuntimeError) as error:
            self.report(str(error))
            return None

    def test_profile(self, name):
        profile = self.save_profile(name)
        if profile:
            def success(value):
                self.profiles[name].status.setText(value["message"])
                self.report(ASSISTANTS[name][0] + " 网络测试：" + value["message"], "profile.test")
            self.run_action(lambda _: test_connection(name, profile),
                            success, lambda message: self.report(ASSISTANTS[name][0] + " 网络测试：" + message, "profile.test"))

    def launch_profile(self, name):
        profile = self.save_profile(name)
        if profile:
            try:
                pid = launch_assistant(name, profile, root=self.root)
                self.profiles[name].status.setText(f"已发起启动（PID {pid}）。已有窗口请先正常退出后再启动。")
                self.report(f"已启动 {ASSISTANTS[name][0]}（PID {pid}）。", "profile.launch")
            except (ValueError, OSError) as error:
                self.report(str(error))

    def pool_value(self):
        pool = copy.deepcopy(self.store.data["pool"])
        pool.update(port=self.port.value(), proxy=validate_proxy(self.pool_proxy.text(), allow_empty=True),
                    strategy=self.strategy.currentData(), session_affinity=self.affinity.isChecked(),
                    background=self.background.isChecked(),
                    resume_on_launch=self.resume.isChecked(),
                    model=self.model.currentText().strip())
        pool_config(self.root, pool)
        return pool

    def save_pool(self):
        try:
            pool = self.pool_value()
            data = copy.deepcopy(self.store.data)
            data["pool"] = pool
            self.store.save(data)
            self.persist_state()
            self.report("账号池配置已保存。" + ("调度变更请点击「保存并应用调度」。" if self.ready else ""))
            return pool
        except (ValueError, OSError, RuntimeError) as error:
            self.report(str(error))
            return None

    def schedule_changed(self):
        strategy = self.strategy.currentData()
        if strategy == "balanced":
            if not getattr(self, "_balanced_mode", False):
                self._affinity_before_balanced = self.affinity.isChecked()
            self.affinity.setChecked(False)
        elif getattr(self, "_balanced_mode", False):
            self.affinity.setChecked(getattr(self, "_affinity_before_balanced", True))
        self._balanced_mode = strategy == "balanced"
        self.strategy_hint.setText(SCHEDULING[strategy][1] + " 优先级仅在优先模式使用。")
        self.update_controls()

    def apply_schedule(self):
        if self.worker is not None: return
        pool = self.save_pool()
        if pool is None: return
        if not self.is_running:
            self.report("账号池调度已保存，下次启动服务生效。")
            return
        try:
            client = self.client()
            def action(worker):
                value = remember_member_rules(pool, client.accounts())
                rows = client.apply_scheduling(value, cancelled=worker.isInterruptionRequested)
                return value, rows
            def success(result):
                value, rows = result
                self.active_pool = copy.deepcopy(value)
                data = copy.deepcopy(self.store.data); data["pool"] = value; self.store.save(data)
                self.set_accounts(rows)
                self.report("调度已生效：" + SCHEDULING[value["strategy"]][0] + "。已建立的会话绑定按引擎规则继续处理。")
            self.run_action(action, success)
        except ValueError as error:
            self.report(str(error))

    def edit_member_rule(self):
        row = self.selected_account()
        if not row: return
        rule = member_rule(self.store.data["pool"], row)
        dialog = QDialog(self); dialog.setWindowTitle("成员调度设置")
        layout = QFormLayout(dialog)
        account = QLabel(str(row.get("email") or row["name"])); account.setTextFormat(Qt.TextFormat.PlainText)
        layout.addRow("成员", account)
        priority = QSpinBox(); priority.setRange(-1000000, 1000000); priority.setValue(rule["priority"])
        weight = QSpinBox(); weight.setRange(1, 1000000); weight.setValue(rule["weight"])
        layout.addRow("优先级（数值大先用）", priority); layout.addRow("权重（比例）", weight)
        note = QLabel("优先级用于优先账号模式；权重用于加权轮询。保存后立即应用到运行中的账号池。")
        note.setWordWrap(True); layout.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); layout.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.save_member_rule(row, {"priority": priority.value(), "weight": weight.value()})

    def save_member_rule(self, row, rule):
        data = copy.deepcopy(self.store.data)
        data["pool"].setdefault("member_rules", {})[row["name"]] = rule
        member_rule(data["pool"], row)
        try:
            self.store.save(data)
            self.report(f"已保存成员 {row.get('email') or row['name']} 的优先级 {rule['priority']}、权重 {rule['weight']}。", "member.rule")
            self.apply_schedule()
        except (OSError, RuntimeError, ValueError) as error:
            self.report(str(error))

    def prefer_account(self):
        row = self.selected_account()
        if not row: return
        data = copy.deepcopy(self.store.data)
        data["pool"] = remember_member_rules(data["pool"], self.accounts)
        for name, rule in data["pool"]["member_rules"].items():
            rule["priority"] = 100 if name == row["name"] else 0
        try:
            self.store.save(data)
            self.report("已设为优先账号：" + str(row.get("email") or row["name"]) + "。", "member.prefer")
            self.strategy.setCurrentIndex(self.strategy.findData("fill-first"))
            self.apply_schedule()
        except (OSError, RuntimeError, ValueError) as error:
            self.report(str(error))

    def copy_key(self):
        # Persist the generated key before allowing clients to copy it.
        pool = self.save_pool()
        if pool:
            QApplication.clipboard().setText(pool["api_key"])
            self.report("已复制本地 API Key。")

    def install(self):
        if self.is_running:
            return
        try:
            proxy = validate_proxy(self.pool_proxy.text(), allow_empty=True)
        except ValueError as error:
            self.report(str(error))
            return
        self.report(f"正在从 GitHub 下载 CLIProxyAPI v{ENGINE_VERSION} 并校验…")
        self.run_action(lambda worker: str(install_engine(self.root / "bin", proxy,
                        worker.progress.emit, worker.isInterruptionRequested)),
                        lambda _: self.report("代理引擎已安装并通过 SHA-256 校验。"))

    def start_pool(self):
        if self.is_running or self.worker is not None:
            return
        pool = self.save_pool()
        if pool is None:
            return
        executable = engine_path(self.root)
        if executable is None:
            self.report("请先点击「安装代理引擎」，完成后再启动服务。")
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.lock = QLockFile(str(self.root / "pool.lock"))
            self.lock.setStaleLockTime(0)
            if not self.lock.tryLock(0):
                self.lock = None
                raise ValueError("另一个工具箱窗口正在运行此账号池。")
            available_port(pool["port"])
            (self.root / "auths").mkdir(exist_ok=True)
            backup_credentials(self.root)
            self.runtime = tempfile.TemporaryDirectory(prefix="omini-codex-pool-")
            config = Path(self.runtime.name) / "config.yaml"
            atomic_json(config, pool_config(self.root, pool))
            environment = proxy_environment({"mode": "direct"})
            environment.pop("MANAGEMENT_PASSWORD", None)
            env = QProcessEnvironment()
            for key, value in environment.items():
                env.insert(key, value)
            self.process.setProcessEnvironment(env)
            self.process.setProgram(str(executable))
            self.process.setArguments(["--config", str(config), "--local-model"])
            self.process.setWorkingDirectory(self.runtime.name)
            self.active_pool = copy.deepcopy(pool)
            self._stop_requested = False
            self.ready = False
            self.started_at = time.monotonic()
            self.service_wanted = True
            self.persist_state()
            self.process.start()
            self.start_timer.start()
            self.report("正在启动本地账号池…", "service.start")
        except (ValueError, OSError, RuntimeError) as error:
            self.cleanup_runtime()
            self.report(str(error))
        self.update_controls()

    def check_ready(self):
        if not self.is_running:
            self.start_timer.stop()
            return
        if time.monotonic() - self.started_at > 25:
            self.stop_pool(remember=False)
            self.report("服务启动超时，请检查端口和引擎日志后重试。")
            return
        client = PoolClient(self.active_pool)
        pool = copy.deepcopy(self.active_pool)
        def action(worker):
            value = remember_member_rules(pool, client.accounts())
            return value, client.apply_scheduling(value, cancelled=worker.isInterruptionRequested)
        def success(result):
            if self.is_running:
                value, rows = result
                self.active_pool = copy.deepcopy(value)
                data = copy.deepcopy(self.store.data); data["pool"] = value; self.store.save(data)
                self.ready = True
                self.start_timer.stop()
                self.set_accounts(rows)
                self.report("本地账号池已就绪，可逐个添加成员。")
                self.update_controls()
        self.run_action(action, success, lambda _: None, quiet=True)

    def client(self):
        if not self.ready or not self.is_running:
            raise ValueError("请先启动账号池服务。")
        return PoolClient(self.active_pool)

    def refresh_accounts(self):
        if not self.ready:
            rows, errors = local_members(self.root)
            self.set_accounts(rows)
            self.report(f"已读取 {len(rows)} 个本地成员。" + (f"{errors} 个凭据文件需检查。" if errors else "启动服务后可查询实时状态与额度。"), "member.refresh")
            return
        try:
            client = self.client()
            def success(rows):
                self.set_accounts(rows)
                self.report(f"已刷新 {len(rows)} 个成员。", "member.refresh")
            self.run_action(lambda _: client.accounts(), success)
        except ValueError as error:
            self.report(str(error))

    def set_accounts(self, rows, *, persist=True):
        selected = self.selected_account()
        selected_name = selected.get("name") if selected else None
        rows = [row for row in rows if not row.get("api_member")]
        rows += api_member_rows(self.store.data["pool"])
        self.accounts = rows
        # Keep the user's preference even when non-priority modes neutralize engine priorities.
        data = copy.deepcopy(self.store.data)
        value = remember_member_rules(data["pool"], rows)
        if value != data["pool"]:
            data["pool"] = value
            try: self.store.save(data)
            except (OSError, ValueError, RuntimeError) as error: self.report(str(error))
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            status = row.get("status", "")
            if row.get("api_member"):
                current = self.store.data["pool"]["api_members"][row["name"]]
                applied = self.ready and self.active_pool and self.active_pool.get("api_members", {}).get(row["name"]) == current
                status = "已停用" if row.get("disabled") else "已应用" if applied else "已保存 · 待应用" if self.is_running else "本地已保存 · 服务未启动"
            status = {"ready": "就绪", "active": "可用", "error": "需检查", "disabled": "已停用"}.get(status, status)
            if row.get("unavailable"):
                status = "等待恢复 / 额度冷却"
            rule = member_rule(self.store.data["pool"], row)
            snapshot = self.quotas.get(row.get("name"))
            five, five_tip = quota_cell(snapshot, "five_hour")
            weekly, weekly_tip = quota_cell(snapshot, "weekly")
            if row.get("api_member"):
                five = weekly = "—"
                five_tip = weekly_tip = "API Key 线路的用量请在上游控制台查看。"
            check = self.api_checks.get(row["name"], {}) if row.get("api_member") else {}
            if check and check.get("fingerprint") != settings_fingerprint(self.store.data["pool"]["api_members"][row["name"]]):
                check = {}
            values = (row.get("email") or row.get("label") or row.get("id", "—"), status, five, weekly,
                      rule["priority"], rule["weight"], "已停用" if row.get("disabled") else "已启用",
                      API_PROTOCOLS[row["protocol"]][0] if row.get("api_member") else "Codex OAuth",
                      ", ".join(m["alias"] for m in row.get("models", [])) if row.get("api_member") else "自动读取",
                      (("通过" if check.get("ok") else "需检查") + f" · {check.get('latency_ms', 0)} ms") if check else "未检查" if row.get("api_member") else "—")
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value)); self.table.setItem(index, col, item)
                item.setToolTip(five_tip if col == 2 else weekly_tip if col == 3 else
                                (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(check.get("checked_at", 0))) + "\n" + check.get("message", "")) if col == 9 and check else
                                row.get("base_url", "") if col == 7 and row.get("api_member") else
                                (str(value) + "\n凭据文件：" + row.get("name", "")))
            self.table.setRowHeight(index, 54)
            if row.get("name") == selected_name:
                self.table.selectRow(index)
        self.table.blockSignals(False)
        # An empty live response or stopped service must never erase saved quotas.
        if persist and not self._restoring:
            try:
                backup_credentials(self.root)
            except (OSError, ValueError, RuntimeError) as error:
                self.report("成员备份失败：" + str(error), "backup.error")
            self.persist_state()
        self.update_controls()

    def query_quotas(self, all_accounts=False):
        if self.worker is not None: return
        try:
            client = self.client()
            selected = self.selected_account()
            rows = [row for row in self.accounts if not row.get("api_member")] if all_accounts else ([selected] if selected and not selected.get("api_member") else [])
            if not rows:
                self.report("请先添加成员账号，并选择要查询的成员。")
                return
            self.report(f"正在查询 {len(rows)} 个成员的额度" +
                        ("：" + str(rows[0].get("email") or rows[0]["name"]) if len(rows) == 1 else "") + "…", "quota.start")
            def action(worker):
                results = {}
                with ThreadPoolExecutor(max_workers=min(3, len(rows)), thread_name_prefix="codex-quota") as executor:
                    pending = {executor.submit(client.quota, row): row["name"] for row in rows}
                    for future in as_completed(pending):
                        if worker.isInterruptionRequested():
                            for item in pending: item.cancel()
                            break
                        name = pending[future]
                        try:
                            results[name] = future.result()
                        except (ValueError, OSError) as error:
                            results[name] = {"error": str(error), "checked_at": int(time.time())}
                        worker.progress.emit(f"额度查询 {len(results)}/{len(rows)}")
                return results
            def success(results):
                updated = 0
                for name, snapshot in results.items():
                    if snapshot.get("error"):
                        old = copy.deepcopy(self.quotas.get(name, snapshot))
                        old["error"] = snapshot["error"]
                        self.quotas[name] = old
                    else:
                        updated += 1; self.quotas[name] = snapshot
                    row = next((row for row in rows if row["name"] == name), {"name": name})
                    details = ("查询失败：" + snapshot["error"] if snapshot.get("error") else
                               "5 小时 " + quota_cell(snapshot, "five_hour")[0].splitlines()[0] +
                               "；周 " + quota_cell(snapshot, "weekly")[0].splitlines()[0])
                    self.report(str(row.get("email") or name) + "：" + details, "quota.result")
                self.set_accounts(self.accounts)
                self.quota_note.setText(f"本次已更新 {updated}/{len(results)} 个账号。额度为查询时的快照；悬停可查看查询时间与错误原因。")
                self.report(f"额度查询完成：成功 {updated}，失败 {len(results) - updated}。")
            self.run_action(action, success)
        except ValueError as error:
            self.report(str(error))

    def selected_account(self):
        index = self.table.currentRow()
        return self.accounts[index] if 0 <= index < len(self.accounts) else None

    def begin_login(self):
        try:
            client = self.client()
            def success(data):
                parsed = urlsplit(data.get("url", ""))
                if (parsed.scheme != "https" or parsed.hostname not in
                        ("auth.openai.com", "auth0.openai.com", "chatgpt.com") or not data.get("state")):
                    self.report("登录服务未返回有效的 OpenAI 登录链接。")
                    return
                self.login_state = data["state"]
                self.login_started = time.monotonic()
                self.login_url.setText(data["url"])
                self.open_login_url()
                self.login_timer.start()
                self.report("请在浏览器完成当前成员登录。完成后会自动刷新账号池。")
                self.update_controls()
            self.run_action(lambda _: client.begin_login(), success)
        except ValueError as error:
            self.report(str(error))

    def open_login_url(self):
        if self.login_url.text():
            QDesktopServices.openUrl(QUrl(self.login_url.text()))

    def clear_login(self):
        self.login_timer.stop()
        self.login_state = ""
        self.login_url.clear()
        self.update_controls()

    def poll_login(self):
        if not self.login_state or not self.ready:
            self.login_timer.stop()
            return
        if time.monotonic() - self.login_started > 600:
            self.cancel_login()
            return
        client, state = self.client(), self.login_state
        def action(_):
            data = client.login_status(state)
            if data.get("status") == "ok":
                data["accounts"] = client.accounts()
            return data
        def success(data):
            if state != self.login_state:
                return
            if data.get("status") == "ok":
                self.clear_login()
                self.set_accounts(data.get("accounts", []))
                self.report("成员已加入账号池；可继续登录下一个成员。")
            elif data.get("status") == "error":
                self.clear_login()
                self.report("成员登录未完成，请重新添加。")
        self.run_action(action, success, lambda message: self.report(message), quiet=True)

    def cancel_login(self):
        if not self.login_state:
            return
        try:
            client, state = self.client(), self.login_state
            self.run_action(lambda _: client.cancel_login(state),
                            lambda _: (self.clear_login(), self.report("已取消本次成员登录。")))
        except ValueError:
            self.clear_login()

    def import_accounts(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择 Codex / CLIProxyAPI 账号凭据", "", "JSON 凭据 (*.json)")
        if not paths:
            return
        if len(paths) > 20:
            self.report("每次最多导入 20 个账号文件。")
            return
        try:
            client = self.client()
            backup_credentials(self.root)
            def action(worker):
                imported, errors = 0, []
                for path in paths:
                    if worker.isInterruptionRequested():
                        break
                    try:
                        client.import_account(Path(path))
                        imported += 1
                    except (OSError, ValueError) as error:
                        errors.append(Path(path).name + "：" + str(error))
                return imported, errors, client.accounts()
            def success(value):
                imported, errors, rows = value
                self.set_accounts(rows)
                self.report(f"已导入 {imported} 个账号。" + ("\n" + "\n".join(errors) if errors else ""))
            self.run_action(action, success)
        except ValueError as error:
            self.report(str(error))

    def toggle_account(self):
        row = self.selected_account()
        if row:
            if row.get("api_member"):
                value = copy.deepcopy(self.store.data["pool"]["api_members"][row["name"]])
                value["disabled"] = not value.get("disabled", False)
                self.save_api_member(row["name"], value)
                return
            client = self.client()
            def action(_):
                backup_credentials(self.root)
                client.set_disabled(row["name"], not row.get("disabled", False))
                return client.accounts()
            def success(rows):
                self.set_accounts(rows)
                self.report(("已启用成员：" if row.get("disabled") else "已停用成员：") + str(row.get("email") or row["name"]) + "。", "member.toggle")
            self.run_action(action, success)

    def remove_account(self):
        row = self.selected_account()
        if not row:
            return
        answer = QMessageBox.question(self, "移出账号池", "移出所选 API 线路？" if row.get("api_member") else "移出所选成员，并删除本地账号池里的凭据副本？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        if row.get("api_member"):
            self.remove_api_member(row["name"])
            return
        client = self.client()
        def action(_):
            backup_credentials(self.root)
            client.remove(row["name"])
            return client.accounts()
        def success(rows):
            self.set_accounts(rows)
            self.report("已移出成员：" + str(row.get("email") or row["name"]) + "；移出前已保存加密备份。", "member.remove")
        self.run_action(action, success)

    def edit_api_member(self, editing=False):
        row = self.selected_account() if editing else None
        if editing and (not row or not row.get("api_member")): return
        member = self.store.data["pool"].get("api_members", {}).get(row["name"]) if row else None
        dialog = ApiMemberDialog(self, member)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.save_api_member(row["name"] if row else "api-" + uuid.uuid4().hex, dialog.value)
        finally:
            dialog.deleteLater()

    def save_api_member(self, name, value):
        try:
            value = validate_api_member(value)
            data = copy.deepcopy(self.store.data)
            data["pool"].setdefault("api_members", {})[name] = value
            validate_api_members(data["pool"]["api_members"])
            self.store.save(data)
            self.api_checks.pop(name, None)
            self.set_accounts(self.accounts)
            self.report("已保存 API 线路：" + value["label"] + "。", "member.api.save")
            if self.ready:
                self.apply_schedule()
            return True
        except (ValueError, OSError, RuntimeError) as error:
            self.report(str(error)); return False

    def remove_api_member(self, name):
        try:
            data = copy.deepcopy(self.store.data)
            member = data["pool"]["api_members"].pop(name)
            data["pool"].get("member_rules", {}).pop(name, None)
            self.store.save(data)
            self.api_checks.pop(name, None)
            self.set_accounts(self.accounts)
            self.report("已移出 API 线路：" + member["label"] + "。", "member.api.remove")
            if self.ready: self.apply_schedule()
        except (KeyError, ValueError, OSError, RuntimeError) as error:
            self.report(str(error))

    def check_api_member(self, generate=False):
        row = self.selected_account()
        if not row or not row.get("api_member"): return
        try:
            member = copy.deepcopy(self.store.data["pool"]["api_members"][row["name"]])
            proxy = validate_proxy(self.pool_proxy.text(), allow_empty=True)
            def success(result):
                result["checked_at"] = int(time.time())
                result["fingerprint"] = settings_fingerprint(member)
                self.api_checks[row["name"]] = result
                self.set_accounts(self.accounts)
                self.report(member["label"] + "：" + result["message"], "member.api.check")
            self.run_action(lambda _: probe_api_member(member, proxy, generate=generate), success)
        except (ValueError, OSError) as error:
            self.report(str(error))

    def read_models(self):
        try:
            client = self.client()
            def success(models):
                previous = self.model.currentText()
                self.model.clear()
                self.model.addItems(models)
                if previous:
                    self.model.setCurrentText(previous)
                self.persist_state()
                self.report(f"已读取 {len(models)} 个可用模型。" if models else "暂无模型，请先添加并启用成员账号。")
            self.run_action(lambda _: client.models(), success)
        except ValueError as error:
            self.report(str(error))

    def launch_pool_codex(self):
        try:
            self.client()
            pool = self.save_pool()
            if not pool:
                return
            profile = self.profiles["codex"].value()
            if not profile.get("program"):
                self.report("请先在 Codex 标签检测或选择程序，然后保存。")
                return
            pid = launch_assistant("codex", profile, root=self.root, pool=pool)
            self.report(f"已通过账号池启动 Codex {'桌面版' if profile.get('desktop') else 'CLI'}（PID {pid}）。")
        except (ValueError, OSError) as error:
            self.report(str(error))

    def update_controls(self):
        busy = self.worker is not None
        running = self.is_running
        selected = self.selected_account() is not None
        api_selected = selected and self.selected_account().get("api_member", False)
        for field in (self.port, self.pool_proxy):
            field.setEnabled(not running and not busy and not self.stopping)
        self.strategy.setEnabled(not busy and not self.stopping and (not running or self.ready))
        self.affinity.setEnabled(self.strategy.currentData() != "balanced" and not busy and not self.stopping
                                 and (not running or self.ready))
        self.install_button.setEnabled(not running and not busy and not self.stopping)
        self.start_button.setEnabled(not running and not busy and not self.stopping)
        self.save_pool_button.setEnabled(not busy and not self.stopping)
        self.stop_button.setEnabled((running or self.service_wanted) and not busy and not self.stopping)
        for button in (self.import_button, self.models_button, self.pool_launch_button):
            button.setEnabled(self.ready and not busy and not self.stopping)
        self.refresh_button.setEnabled(not busy and not self.stopping)
        self.login_button.setEnabled(self.ready and not busy and not self.login_state and not self.stopping)
        self.cancel_button.setEnabled(bool(self.login_state) and not busy)
        self.reopen_login.setEnabled(bool(self.login_state))
        self.toggle_button.setEnabled((self.ready or api_selected) and selected and not busy and not self.stopping)
        self.remove_button.setEnabled((self.ready or api_selected) and selected and not busy and not self.stopping)
        self.quota_button.setEnabled(self.ready and selected and not api_selected and not busy and not self.stopping)
        self.all_quotas_button.setEnabled(self.ready and any(not row.get("api_member") for row in self.accounts) and not busy and not self.stopping)
        self.add_api_button.setEnabled(not busy and not self.stopping and (not running or self.ready))
        for button in (self.edit_api_button, self.check_api_button, self.verify_api_button):
            button.setEnabled(api_selected and not busy and not self.stopping and (not running or self.ready))
        self.member_rule_button.setEnabled(selected and not busy and not self.stopping)
        self.prefer_button.setEnabled(selected and not busy and not self.stopping
                                     and not self.selected_account().get("disabled", False))
        path = engine_path(self.root)
        self.install_button.setText("重新安装引擎" if path else "安装代理引擎")
        self.engine_status.setText((f"CLIProxyAPI v{ENGINE_VERSION} · " + ("运行中" if self.ready else "正在启动" if running else "服务未启动 · 本地成员已保留"))
                                   if path else f"尚未安装 · CLIProxyAPI v{ENGINE_VERSION} · 官方 GitHub，SHA-256 校验")

    def read_output(self):
        output = bytes(self.process.readAllStandardOutput()).decode("utf-8", "replace")
        pool = self.active_pool or self.store.data["pool"]
        for line in output.splitlines():
            # HTTP request logs are disabled. Retain short startup diagnostics only.
            secret_values = [pool["api_key"], pool["management_key"]]
            for member in pool.get("api_members", {}).values():
                secret_values.extend([member.get("api_key", ""), *member.get("headers", {}).values()])
            clean = redact(line[:1000], secret_values)
            if re.search(r"gin_logger\.go:\d+\]\s+2\d\d\b", clean):
                continue  # The corresponding user action is recorded separately.
            try:
                self.log.appendPlainText(history_line(self.history.append(clean, "engine")))
            except (OSError, ValueError, RuntimeError):
                self.log.appendPlainText(clean)

    def process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.start_timer.stop()
            self.ready = False
            self.cleanup_runtime()
            self.report("代理引擎启动失败：" + self.process.errorString())
            self.update_controls()

    def process_finished(self, code, _status):
        self.start_timer.stop()
        self.ready = False
        self.clear_login()
        self.cleanup_runtime()
        self.set_accounts(local_members(self.root)[0])
        if not self.stopping:
            self.report("账号池服务已停止，本地成员与记录已保留。" if self._stop_requested else
                        f"账号池服务已退出（退出码 {code}）；本地成员与记录已保留。", "service.exit")
        self.update_controls()

    def cleanup_runtime(self):
        if self.runtime is not None:
            self.runtime.cleanup()
            self.runtime = None
        if self.lock is not None:
            self.lock.unlock()
            self.lock = None
        self.active_pool = None

    def stop_pool(self, *, remember=True):
        self._stop_requested = True
        if remember:
            self.service_wanted = False
            self.persist_state()
            self.report("已手动停止账号池；下次打开工具箱保持停止。", "service.stop")
        self.start_timer.stop()
        self.ready = False
        self.clear_login()
        if self.is_running:
            self.process.terminate()
            if not self.process.waitForFinished(1000):
                self.process.kill()
                self.process.waitForFinished(1000)
        self.cleanup_runtime()
        self.set_accounts(local_members(self.root)[0])
        self.update_controls()

    def stop(self):
        if self.stopping:
            return
        saved = self.save_session()
        self.report("退出工具箱，已保存操作与运行状态。" if saved else
                    "退出工具箱，部分状态保存失败；请检查操作记录与数据目录。", "app.close")
        self.stopping = True
        if self.worker is not None:
            self.worker.requestInterruption()
        self.stop_pool(remember=False)
