"""Desktop proxy launchers and Codex member pool controls."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QLockFile, QProcess, QProcessEnvironment, QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLayout, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget,
    QTextBrowser, QVBoxLayout, QWidget,
)

from ai_assistants import (
    ASSISTANTS, ENGINE_REPO, ENGINE_VERSION, AssistantStore, PoolClient, atomic_json,
    available_port, detect_program, engine_path, install_engine, launch_assistant,
    pool_config, powershell_environment, proxy_environment, redact, test_connection,
    validate_profile, validate_proxy,
)


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
        return validate_profile(self.name, {
            "mode": self.mode.currentData(), "proxy": self.proxy.text().strip(),
            "no_proxy": self.no_proxy.text().strip(), "program": self.program.text().strip(),
            "cwd": self.cwd.text().strip(), "args": self.args.toPlainText().splitlines(),
            "desktop": self.name == "codex" and self.kind.currentIndex() == 1,
        })

    def detect(self):
        desktop = self.name == "codex" and self.kind.currentIndex() == 1
        def found(value):
            if value:
                self.program.setText(value)
                self.status.setText("已找到程序；保存后即可带代理启动。")
            else:
                self.status.setText("未检测到程序，请安装该助手或手动选择程序文件。")
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
        except ValueError as error:
            self.owner.report(str(error))


class AiAssistantsPage(QWidget):
    def __init__(self, root: Path, parent=None):
        super().__init__(parent)
        self.store = AssistantStore(root)
        self.root = self.store.root
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
        subtitle = QLabel("分别设置代理，一键启动 Codex、Gemini、反重力和 Claude Code；集中管理 Codex 成员账号。")
        subtitle.setWordWrap(True)
        subtitle.setObjectName("muted")
        layout.addWidget(subtitle)
        self.tabs = QTabWidget()
        self.profiles = {}
        for name, (title, _) in ASSISTANTS.items():
            profile = AssistantProfile(name, self.store.data["profiles"][name], self)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setWidget(profile)
            self.profiles[name] = profile
            self.tabs.addTab(scroll, title)
        self.build_pool()
        self.build_help()
        layout.addWidget(self.tabs, 1)
        self.status = QLabel(self.store.error or "就绪。各助手的代理配置独立保存。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.update_controls()

    def build_pool(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        box = QGroupBox("本地账号池服务")
        form = QFormLayout(box)
        pool = self.store.data["pool"]
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(int(pool["port"]))
        self.pool_proxy = QLineEdit(pool["proxy"])
        self.pool_proxy.setPlaceholderText("http://127.0.0.1:7890；留空直接连接")
        self.strategy = QComboBox()
        self.strategy.addItem("轮询分配", "round-robin")
        self.strategy.addItem("优先使用首个可用账号", "fill-first")
        self.strategy.setCurrentIndex(max(0, self.strategy.findData(pool["strategy"])))
        self.affinity = QCheckBox("同一会话保持账号，新增会话按策略分配")
        self.affinity.setChecked(bool(pool.get("session_affinity", True)))
        self.background = QCheckBox("关闭窗口后在托盘继续运行账号池")
        self.background.setChecked(bool(pool.get("background", True)))
        endpoint_row = QHBoxLayout()
        endpoint_row.addWidget(QLabel("端口"))
        endpoint_row.addWidget(self.port)
        endpoint_row.addWidget(QLabel("调度"))
        endpoint_row.addWidget(self.strategy, 1)
        form.addRow(endpoint_row)
        form.addRow("出口代理", self.pool_proxy)
        form.addRow(self.affinity)
        form.addRow(self.background)
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
        self.stop_button.clicked.connect(self.stop_pool)
        self.save_pool_button = QPushButton("保存账号池配置")
        self.save_pool_button.clicked.connect(self.save_pool)
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
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["成员账号", "凭据文件", "状态", "参与分配"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.setMinimumHeight(130)
        self.table.itemSelectionChanged.connect(self.update_controls)
        layout.addWidget(self.table, 1)
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
        self.pool_note = QLabel("服务仅在本机运行。先安装引擎并启动，再添加成员；登录窗口请切换到对应成员账号。切换到其他页面后服务继续运行，退出工具箱时停止。")
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
        self.tabs.addTab(scroll, "Codex 成员账号池")

    def build_help(self):
        help_view = QTextBrowser()
        help_view.setOpenExternalLinks(True)
        help_view.setHtml(f"""
        <h2>从这里开始</h2>
        <p>1. 进入对应助手标签，填代理地址（常见为 http://127.0.0.1:7890），检测程序并保存。</p>
        <p>2. 点击「测试网络」。HTTP 401 代表已连到服务但尚未认证；403/429/5xx 会显示原始状态码。测试不消耗模型额度。</p>
        <p>3. 正常退出已有助手，点击「带此环境启动」。CLI 在单独的 PowerShell 窗口运行；桌面程序同时收到代理环境和网络启动参数。</p>
        <h3>Codex 多成员轮询</h3>
        <p>在「Codex 成员账号池」安装引擎、设置出口代理并启动，逐个点击「登录并添加成员」，
        或导入 Codex auth.json / CLIProxyAPI 的 Codex JSON。重复导入同一账号更新它的凭据。</p>
        <p>「轮询分配」配合会话保持，让新会话轮流使用可用成员；取消会话保持则按请求分配。
        账号失效或额度等待由引擎处理，停用成员会退出分配。移出只删除本地池中的凭据副本。</p>
        <p>在 Codex 标签选择 CLI 或桌面版、设置程序和工作目录。读取模型后点击「通过账号池启动 Codex」。
        此入口使用独立的 codex-pool/ 配置目录和本地 Responses 接口；账号池会话存放在该目录，
        原有 Codex 配置和当前登录保持原样。桌面版需要先正常退出已有窗口，再从这里启动。</p>
        <h3>保存位置与服务生命周期</h3>
        <p>配置与本地密钥使用 Windows 用户加密存入 %APPDATA%/Omini工具箱/ai-assistants/。
        引擎按其原生格式在 auths/ 保存 OAuth 凭据；运行配置在服务停止后清理。个人数据留在用户目录。</p>
        <p>账号池服务随工具箱运行，关闭到托盘时继续，完全退出时停止。已经打开的助手窗口由你自行退出。</p>
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

    def report(self, text):
        pool = self.store.data["pool"]
        text = redact(str(text), [pool["api_key"], pool["management_key"], pool.get("proxy", "")])
        self.status.setText(text)
        self.log.appendPlainText(text)

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
            return profile
        except (ValueError, OSError, RuntimeError) as error:
            self.report(str(error))
            return None

    def test_profile(self, name):
        profile = self.save_profile(name)
        if profile:
            self.run_action(lambda _: test_connection(name, profile),
                            lambda value: self.profiles[name].status.setText(value["message"]),
                            lambda message: self.profiles[name].status.setText(message))

    def launch_profile(self, name):
        profile = self.save_profile(name)
        if profile:
            try:
                pid = launch_assistant(name, profile, root=self.root)
                self.profiles[name].status.setText(f"已发起启动（PID {pid}）。已有窗口请先正常退出后再启动。")
            except (ValueError, OSError) as error:
                self.report(str(error))

    def pool_value(self):
        pool = copy.deepcopy(self.store.data["pool"])
        pool.update(port=self.port.value(), proxy=validate_proxy(self.pool_proxy.text(), allow_empty=True),
                    strategy=self.strategy.currentData(), session_affinity=self.affinity.isChecked(),
                    background=self.background.isChecked(),
                    model=self.model.currentText().strip())
        pool_config(self.root, pool)
        return pool

    def save_pool(self):
        try:
            pool = self.pool_value()
            data = copy.deepcopy(self.store.data)
            data["pool"] = pool
            self.store.save(data)
            self.report("账号池配置已保存。")
            return pool
        except (ValueError, OSError, RuntimeError) as error:
            self.report(str(error))
            return None

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
            self.ready = False
            self.started_at = time.monotonic()
            self.process.start()
            self.start_timer.start()
            self.report("正在启动本地账号池…")
        except (ValueError, OSError) as error:
            self.cleanup_runtime()
            self.report(str(error))
        self.update_controls()

    def check_ready(self):
        if not self.is_running:
            self.start_timer.stop()
            return
        if time.monotonic() - self.started_at > 25:
            self.stop_pool()
            self.report("服务启动超时，请检查端口和引擎日志后重试。")
            return
        client = PoolClient(self.active_pool)
        def success(rows):
            if self.is_running:
                self.ready = True
                self.start_timer.stop()
                self.set_accounts(rows)
                self.report("本地账号池已就绪，可逐个添加成员。")
                self.update_controls()
        self.run_action(lambda _: client.accounts(), success, lambda _: None, quiet=True)

    def client(self):
        if not self.ready or not self.is_running:
            raise ValueError("请先启动账号池服务。")
        return PoolClient(self.active_pool)

    def refresh_accounts(self):
        try:
            client = self.client()
            self.run_action(lambda _: client.accounts(), self.set_accounts)
        except ValueError as error:
            self.report(str(error))

    def set_accounts(self, rows):
        selected = self.selected_account()
        selected_name = selected.get("name") if selected else None
        self.accounts = rows
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            status = row.get("status", "")
            status = {"ready": "就绪", "active": "可用", "error": "需检查", "disabled": "已停用"}.get(status, status)
            if row.get("unavailable"):
                status = "等待恢复 / 额度冷却"
            values = (row.get("email") or row.get("label") or row.get("id", "—"), row.get("name", ""),
                      status, "已停用" if row.get("disabled") else "已启用")
            for col, value in enumerate(values):
                self.table.setItem(index, col, QTableWidgetItem(str(value)))
            if row.get("name") == selected_name:
                self.table.selectRow(index)
        self.update_controls()

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
            client = self.client()
            def action(_):
                client.set_disabled(row["name"], not row.get("disabled", False))
                return client.accounts()
            self.run_action(action, self.set_accounts)

    def remove_account(self):
        row = self.selected_account()
        if not row:
            return
        answer = QMessageBox.question(self, "移出账号池", "移出所选成员，并删除本地账号池里的凭据副本？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        client = self.client()
        def action(_):
            client.remove(row["name"])
            return client.accounts()
        self.run_action(action, self.set_accounts)

    def read_models(self):
        try:
            client = self.client()
            def success(models):
                previous = self.model.currentText()
                self.model.clear()
                self.model.addItems(models)
                if previous in models:
                    self.model.setCurrentText(previous)
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
        busy = self.worker is not None and self.worker.isRunning()
        running = self.is_running
        selected = self.selected_account() is not None
        for field in (self.port, self.pool_proxy, self.strategy, self.affinity):
            field.setEnabled(not running and not busy and not self.stopping)
        self.install_button.setEnabled(not running and not busy and not self.stopping)
        self.start_button.setEnabled(not running and not busy and not self.stopping)
        self.save_pool_button.setEnabled(not busy and not self.stopping)
        self.stop_button.setEnabled(running and not busy)
        for button in (self.import_button, self.refresh_button, self.models_button, self.pool_launch_button):
            button.setEnabled(self.ready and not busy and not self.stopping)
        self.login_button.setEnabled(self.ready and not busy and not self.login_state and not self.stopping)
        self.cancel_button.setEnabled(bool(self.login_state) and not busy)
        self.reopen_login.setEnabled(bool(self.login_state))
        self.toggle_button.setEnabled(self.ready and selected and not busy)
        self.remove_button.setEnabled(self.ready and selected and not busy)
        path = engine_path(self.root)
        self.install_button.setText("重新安装引擎" if path else "安装代理引擎")
        self.engine_status.setText((f"CLIProxyAPI v{ENGINE_VERSION} · " + ("运行中" if self.ready else "正在启动" if running else "已安装"))
                                   if path else f"尚未安装 · CLIProxyAPI v{ENGINE_VERSION} · 官方 GitHub，SHA-256 校验")

    def read_output(self):
        output = bytes(self.process.readAllStandardOutput()).decode("utf-8", "replace")
        pool = self.active_pool or self.store.data["pool"]
        for line in output.splitlines():
            # HTTP request logs are disabled. Retain short startup diagnostics only.
            self.log.appendPlainText(redact(line[:1000], [pool["api_key"], pool["management_key"]]))

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
        if not self.stopping:
            self.report(f"账号池服务已停止（退出码 {code}）。")
        self.update_controls()

    def cleanup_runtime(self):
        if self.runtime is not None:
            self.runtime.cleanup()
            self.runtime = None
        if self.lock is not None:
            self.lock.unlock()
            self.lock = None
        self.active_pool = None

    def stop_pool(self):
        self.start_timer.stop()
        self.ready = False
        self.clear_login()
        if self.is_running:
            self.process.terminate()
            if not self.process.waitForFinished(1000):
                self.process.kill()
                self.process.waitForFinished(1000)
        self.cleanup_runtime()
        self.update_controls()

    def stop(self):
        self.stopping = True
        if self.worker is not None:
            self.worker.requestInterruption()
        self.stop_pool()
