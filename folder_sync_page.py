"""Native folder/device management UI for the managed Syncthing service."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import queue
import secrets
import threading
import time

from PySide6.QtCore import Qt, QThread, QTime, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSpinBox, QTabWidget,
    QTableWidget, QTableWidgetItem, QTextBrowser, QTextEdit, QTimeEdit, QVBoxLayout, QWidget,
)

from folder_sync import MODES, TYPES, SyncError, SyncManager
from tailscale_sync import read_status


class SyncWorker(QThread):
    updated = Signal(dict)
    message = Signal(str)
    service = Signal(dict)
    busy = Signal(bool)
    tailscale = Signal(dict)

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.commands = queue.Queue()
        self.stopping = threading.Event()

    def submit(self, action, *args):
        if not self.stopping.is_set():
            self.commands.put((action, args))

    def request_stop(self):
        self.stopping.set()
        self.commands.put(("exit", ()))

    def publish_service(self):
        self.service.emit({"running": self.manager.running, "enabled": self.manager.settings["enabled"],
                           "background": self.manager.settings["background"]})

    def run(self):
        manager = self.manager
        try:
            if manager.settings["enabled"]:
                self.commands.put(("start", ()))
            self.publish_service()
            next_poll = 0
            while not self.stopping.is_set():
                try:
                    action, args = self.commands.get(timeout=0.25)
                except queue.Empty:
                    action, args = "", ()
                if self.stopping.is_set():
                    break
                if action:
                    self.busy.emit(True)
                    try:
                        if action == "start":
                            manager.start()
                            self.message.emit("同步服务已启动。请在其他电脑上添加本机 ID，并互相选择共享文件夹。")
                        elif action == "stop":
                            manager.settings["enabled"] = False
                            manager.save()
                            manager.stop()
                            self.message.emit("同步服务已停止。")
                        elif action == "preferences":
                            manager.settings.update(args[0])
                            manager.save()
                        elif action == "refresh":
                            pass
                        elif action == "tailscale":
                            try:
                                self.tailscale.emit(read_status())
                            except ValueError as error:
                                self.tailscale.emit({"state": "Unavailable", "self": {}, "peers": [], "error": str(error)})
                        elif not manager.running:
                            raise SyncError("请先启动同步服务。")
                        else:
                            getattr(manager, action)(*args)
                            self.message.emit({"device": "设备已保存。", "remove_device": "设备已移除。",
                                               "rename": "本机名称已保存。", "folder": "同步文件夹已保存。",
                                               "remove_folder": "已移除同步配置，本机文件保留。",
                                               "sync_now": "已开始本轮同步，等待对方设备在线并启用对应文件夹。",
                                               "pause": "已暂停此文件夹及其定时任务。"}.get(action, "操作完成。"))
                    except Exception as error:
                        self.message.emit(str(error))
                    finally:
                        self.publish_service()
                        self.busy.emit(False)
                    next_poll = 0
                if manager.running and time.monotonic() >= next_poll:
                    try:
                        snapshot = manager.snapshot()
                        if not self.stopping.is_set():
                            self.updated.emit(snapshot)
                    except Exception as error:
                        self.message.emit(str(error))
                    self.publish_service()
                    next_poll = time.monotonic() + 5
                elif manager.process is not None and not manager.running:
                    self.publish_service()
                    self.message.emit("同步引擎已退出，请点击启动同步服务重试。")
                    manager.stop()
        finally:
            try:
                manager.close()
            except Exception as error:
                self.message.emit("同步服务退出时遇到问题：" + str(error))


class DeviceDialog(QDialog):
    refresh_tailscale = Signal()

    def __init__(self, device=None, parent=None, inventory=None):
        super().__init__(parent)
        self.setWindowTitle("添加电脑" if not device else "编辑电脑")
        self.resize(730, 390)
        form = QFormLayout(self)
        self.device_id = QLineEdit((device or {}).get("deviceID", ""))
        self.device_id.setPlaceholderText("粘贴对方电脑显示的完整设备 ID")
        self.device_id.setReadOnly(bool(device))
        self.name = QLineEdit((device or {}).get("name", ""))
        self.name.setPlaceholderText("例如：家里的台式机、公司的笔记本")
        self.addresses = QLineEdit(", ".join((device or {}).get("addresses", ["dynamic"])))
        self.addresses.setPlaceholderText("填写 100.x.x.x、电脑名.tailnet.ts.net，或 tcp://IP:端口")
        form.addRow("设备 ID", self.device_id)
        form.addRow("电脑名称", self.name)
        form.addRow("连接地址", self.addresses)
        tip = QLabel("你可以直接填写对方的 Tailscale IP 或 MagicDNS 名称，自动补为 tcp://地址:22000。多个地址用逗号分隔；dynamic 表示自动发现。")
        tip.setWordWrap(True)
        form.addRow(tip)
        row = QHBoxLayout()
        self.tailscale_peers = QComboBox()
        self.tailscale_peers.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.tailscale_peers.setMinimumContentsLength(22)
        self.tailscale_peers.activated.connect(self.choose_tailscale)
        self.read_tailscale_button = QPushButton("读取 Tailscale 设备")
        self.read_tailscale_button.clicked.connect(self.refresh_tailscale.emit)
        row.addWidget(self.tailscale_peers, 1)
        row.addWidget(self.read_tailscale_button)
        form.addRow("可选：从组网选择", row)
        self.tailscale_info = QLabel()
        self.tailscale_info.setWordWrap(True)
        self.tailscale_info.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow(self.tailscale_info)
        self.set_tailscale(inventory or {})
        identity_tip = QLabel("Tailscale 地址用于连接；设备 ID 请填对方同步程序生成的 ID。选择组网电脑后仍需填写设备 ID。两台电脑需要互相添加。")
        identity_tip.setWordWrap(True)
        form.addRow(identity_tip)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存电脑")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def set_tailscale(self, inventory):
        self.tailscale_peers.clear()
        self.tailscale_peers.addItem("自行填写地址，或读取设备后选择…", None)
        for peer in inventory.get("peers", []):
            address = next((ip for ip in peer["ips"] if ":" not in ip), peer.get("dns") or next(iter(peer["ips"]), ""))
            if address:
                self.tailscale_peers.addItem(
                    f"{peer['name']} · {address} · {'在线' if peer['online'] else '离线'} · {peer['os']}", peer)
        self.tailscale_info.setText(inventory.get("error") or (
            "已读取；请选择要连接的电脑，也可继续手动填写地址。" if inventory.get("state") == "Running"
            else "按需读取当前组网设备；程序会保留你的手动输入。"))

    def choose_tailscale(self, index):
        peer = self.tailscale_peers.itemData(index)
        if not peer:
            return
        address = next((ip for ip in peer["ips"] if ":" not in ip), peer.get("dns") or next(iter(peer["ips"]), ""))
        self.addresses.setText(address)
        if not self.name.text().strip():
            self.name.setText(peer["name"])

    def values(self):
        return (self.device_id.text(), self.name.text(),
                [a.strip() for a in self.addresses.text().replace("，", ",").split(",") if a.strip()] or ["dynamic"])


class FolderDialog(QDialog):
    def __init__(self, devices, folder=None, parent=None):
        super().__init__(parent)
        self.folder = folder or {}
        self.setWindowTitle("编辑同步文件夹" if folder else "添加同步文件夹")
        self.resize(700, 630)
        form = QFormLayout(self)
        self.label = QLineEdit(self.folder.get("label", ""))
        form.addRow("显示名称", self.label)
        self.folder_id = QLineEdit(self.folder.get("id", "share-" + secrets.token_hex(4)))
        self.folder_id.setReadOnly(bool(folder))
        form.addRow("共享 ID", self.folder_id)
        tip = QLabel("在另一台电脑加入此文件夹时，填写相同的共享 ID；本地路径可不同。")
        tip.setWordWrap(True)
        form.addRow(tip)
        path_row = QHBoxLayout()
        self.path = QLineEdit(self.folder.get("path", ""))
        self.path.setPlaceholderText("选择本机要同步的文件夹")
        browse = QPushButton("浏览…")
        browse.clicked.connect(self.browse)
        path_row.addWidget(self.path, 1)
        path_row.addWidget(browse)
        form.addRow("本机路径", path_row)
        self.devices = QListWidget()
        self.devices.setMaximumHeight(115)
        selected = {d["deviceID"] for d in self.folder.get("devices", [])}
        for device in devices:
            item = QListWidgetItem(f"{device['name'] or device['deviceID'][:7]}   ·   {device['deviceID'][:7]}")
            item.setData(Qt.ItemDataRole.UserRole, device["deviceID"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if (device["deviceID"] in selected or (not folder and len(devices) == 1)) else Qt.CheckState.Unchecked)
            self.devices.addItem(item)
        form.addRow("同步到电脑", self.devices)
        self.kind = QComboBox()
        for key, label in TYPES.items():
            self.kind.addItem(label, key)
        self.kind.setCurrentIndex(max(0, self.kind.findData(self.folder.get("type", "sendreceive"))))
        form.addRow("同步方向", self.kind)
        policy = self.folder.get("policy", {})
        self.mode = QComboBox()
        for key, label in MODES.items():
            self.mode.addItem(label, key)
        self.mode.setCurrentIndex(max(0, self.mode.findData(policy.get("mode", "auto"))))
        form.addRow("同步模式", self.mode)
        self.minutes = QSpinBox()
        self.minutes.setRange(1, 10080)
        self.minutes.setValue(policy.get("minutes", 30))
        self.minutes.setSuffix(" 分钟")
        form.addRow("定时间隔", self.minutes)
        self.clock = QTimeEdit(QTime.fromString(policy.get("time", "20:00"), "HH:mm"))
        self.clock.setDisplayFormat("HH:mm")
        form.addRow("每天时间", self.clock)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.mode_changed()
        warning = QLabel("双向同步会传播修改和删除。其他电脑传入的覆盖/删除文件保留约 30 天历史版本；并发修改保留冲突副本。首次同步会合并两边文件。")
        warning.setWordWrap(True)
        warning.setObjectName("muted")
        form.addRow(warning)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存同步文件夹")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def mode_changed(self):
        self.minutes.setEnabled(self.mode.currentData() == "interval")
        self.clock.setEnabled(self.mode.currentData() == "daily")

    def browse(self):
        value = QFileDialog.getExistingDirectory(self, "选择本机同步文件夹", self.path.text())
        if value:
            self.path.setText(value)
            if not self.label.text():
                self.label.setText(Path(value).name)

    def values(self):
        return {"id": self.folder_id.text(), "label": self.label.text(), "path": self.path.text(),
                "devices": [self.devices.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.devices.count())
                            if self.devices.item(i).checkState() == Qt.CheckState.Checked],
                "type": self.kind.currentData(), "mode": self.mode.currentData(), "minutes": self.minutes.value(),
                "time": self.clock.time().toString("HH:mm"), "editing": bool(self.folder)}


class FolderSyncPage(QWidget):
    def __init__(self, directory, parent=None, manager=None):
        super().__init__(parent)
        self.directory = Path(directory)
        self.snapshot_data = {"devices": [], "folders": []}
        self.stopping = False
        self.running = False
        self.command_busy = False
        self.background_enabled = False
        self.worker = None
        self.tailscale_inventory = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        bar = QHBoxLayout()
        title = QLabel("文件夹同步")
        title.setObjectName("pageTitle")
        bar.addWidget(title)
        bar.addStretch()
        self.start_button = QPushButton("启动同步服务")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(lambda: self.submit("start"))
        self.stop_button = QPushButton("停止服务")
        self.stop_button.clicked.connect(lambda: self.submit("stop"))
        bar.addWidget(self.start_button)
        bar.addWidget(self.stop_button)
        layout.addLayout(bar)
        subtitle = QLabel("给每台电脑一个身份，让指定文件夹保持同步。文件通过设备之间的加密连接传输。")
        subtitle.setWordWrap(True)
        subtitle.setObjectName("muted")
        layout.addWidget(subtitle)
        identity = QGroupBox("本机身份")
        identity_layout = QFormLayout(identity)
        id_row = QHBoxLayout()
        self.my_id = QLineEdit()
        self.my_id.setReadOnly(True)
        self.my_id.setPlaceholderText("启动同步服务后生成；同一台电脑重启后保持不变")
        self.copy_id_button = QPushButton("复制设备 ID")
        self.copy_id_button.clicked.connect(lambda: self.copy(self.my_id.text(), "本机设备 ID 已复制。"))
        id_row.addWidget(self.my_id, 1)
        id_row.addWidget(self.copy_id_button)
        identity_layout.addRow("设备 ID", id_row)
        name_row = QHBoxLayout()
        self.my_name = QLineEdit()
        self.rename_button = QPushButton("保存名称")
        self.rename_button.clicked.connect(lambda: self.submit("rename", self.my_name.text()))
        name_row.addWidget(self.my_name, 1)
        name_row.addWidget(self.rename_button)
        identity_layout.addRow("本机名称", name_row)
        tailscale_row = QHBoxLayout()
        self.tailscale_address = QLineEdit()
        self.tailscale_address.setReadOnly(True)
        self.tailscale_address.setPlaceholderText("可选：点击读取，查看本机 Tailscale IP")
        self.copy_tailscale_button = QPushButton("复制本机 IP")
        self.copy_tailscale_button.clicked.connect(lambda: self.copy(self.tailscale_address.text(), "本机 Tailscale IP 已复制。"))
        self.read_tailscale_button = QPushButton("读取 Tailscale")
        self.read_tailscale_button.clicked.connect(lambda: self.submit("tailscale"))
        tailscale_row.addWidget(self.tailscale_address, 1)
        tailscale_row.addWidget(self.copy_tailscale_button)
        tailscale_row.addWidget(self.read_tailscale_button)
        identity_layout.addRow("Tailscale IP", tailscale_row)
        options = QHBoxLayout()
        self.autostart = QCheckBox("下次打开工具箱时启动同步")
        self.background = QCheckBox("关闭窗口后在托盘继续同步")
        self.autostart.toggled.connect(lambda value: self.submit("preferences", {"enabled": value}))
        self.background.toggled.connect(lambda value: self.submit("preferences", {"background": value}))
        options.addWidget(self.autostart)
        options.addWidget(self.background)
        options.addStretch()
        identity_layout.addRow(options)
        layout.addWidget(identity)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        folders = QWidget()
        folder_layout = QVBoxLayout(folders)
        folder_bar = QHBoxLayout()
        self.folder_buttons = []
        for label, attr, callback in (
            ("添加文件夹", "add_folder_button", self.add_folder),
            ("编辑", "edit_folder_button", self.edit_folder),
            ("立即同步", "sync_button", self.sync_selected),
            ("暂停", "pause_button", lambda: self.folder_action("pause")),
            ("复制共享 ID", "copy_folder_button", self.copy_folder_id),
            ("打开目录", "open_folder_button", self.open_folder),
            ("移除", "remove_folder_button", self.remove_folder),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            setattr(self, attr, button)
            self.folder_buttons.append(button)
            folder_bar.addWidget(button)
        folder_layout.addLayout(folder_bar)
        self.folder_table = self.table(["文件夹 / 共享 ID", "本机路径", "模式 / 下次运行", "状态", "完成度"])
        self.folder_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.folder_table.setWordWrap(False)
        self.folder_table.itemSelectionChanged.connect(self.selection_changed)
        self.folder_table.cellDoubleClicked.connect(lambda *_: self.edit_folder())
        folder_layout.addWidget(self.folder_table, 1)
        self.detail = QLabel("添加电脑后，选择本机文件夹；另一台电脑使用相同共享 ID 加入。")
        self.detail.setWordWrap(True)
        folder_layout.addWidget(self.detail)
        self.tabs.addTab(folders, "同步文件夹")
        devices = QWidget()
        device_layout = QVBoxLayout(devices)
        device_bar = QHBoxLayout()
        self.device_buttons = []
        for label, callback in (("添加电脑", self.add_device), ("编辑电脑", self.edit_device), ("移除电脑", self.remove_device)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            self.device_buttons.append(button)
            device_bar.addWidget(button)
        refresh = QPushButton("刷新状态")
        refresh.clicked.connect(lambda: self.submit("refresh"))
        device_bar.addWidget(refresh)
        device_bar.addStretch()
        device_layout.addLayout(device_bar)
        self.device_table = self.table(["电脑名称", "设备 ID", "连接状态", "当前地址"])
        self.device_table.cellDoubleClicked.connect(lambda *_: self.edit_device())
        device_layout.addWidget(self.device_table, 1)
        note = QLabel("设备需互相添加。已用 Tailscale 组网时，连接地址填对方 Tailscale IP 或 MagicDNS 名称，默认端口 22000。")
        note.setWordWrap(True)
        device_layout.addWidget(note)
        self.tabs.addTab(devices, "电脑设备")
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.document().setMaximumBlockCount(300)
        self.tabs.addTab(self.log, "运行记录")
        help_view = QTextBrowser()
        help_view.setHtml("""<h2>第一次连接两台电脑</h2>
            <ol><li>在两台 Windows 电脑上运行工具箱，进入「文件夹同步」，点击「启动同步服务」。</li>
            <li>分别复制设备 ID，在对方的「电脑设备 → 添加电脑」中粘贴。可自行设置易辨认的电脑名称。</li>
            <li>第一台电脑点击「添加文件夹」，选择本机目录和对方电脑，保存后复制共享 ID。</li>
            <li>第二台电脑也添加文件夹，填入同一个共享 ID，选择自己的本地目录和第一台电脑。</li>
            <li>默认自动同步；两边都启用文件夹后，设备在线时开始合并和传输。</li></ol>
            <h3>三种同步方式</h3>
            <p><b>自动同步：</b>监听文件变化，通常几秒后开始同步，每小时补充扫描一次。<br>
            <b>手动同步：</b>平时暂停，点击「立即同步」后执行本轮；所有共享电脑完成后自动暂停。对方也需要启用对应文件夹。<br>
            <b>定时同步：</b>按分钟间隔或每天本机时间触发，完成后暂停；休眠后恢复会补执行错过的一轮。
            两台电脑都采用定时时，请安排相同时间；接收端也可保持自动模式。<br>
            <b>暂停：</b>同时暂停自动和定时任务；「立即同步」恢复原模式。离线设备上线后继续未完成的任务。</p>
            <h3>数据与连接</h3>
            <p><b>使用 Tailscale：</b>在「添加电脑 / 编辑电脑」填写对方设备 ID 和 Tailscale IP（例如 100.101.102.103）
            或 MagicDNS 名称。程序自动补齐 tcp:// 和 22000 端口，也可手动指定 主机:端口。
            点击「读取 Tailscale 设备」后可自行选择，读取不会替换已填内容，选择电脑才会填入地址。
            设备 ID 仍使用对方同步程序的 ID。只有填写的固定地址会用于主动连接；加入 dynamic 可保留自动发现。</p>
            <p>两台电脑的 Tailscale 需在线，网络访问规则和系统防火墙需允许 TCP 22000。
            Tailscale 在线状态与同步程序在线状态各自显示；对方只开 Tailscale 而未启用同步服务时，仍会等待。
            Mac / Linux 可使用同版本 Syncthing，配置相同共享 ID 和双向信任，与本 Windows 程序互通。</p>
            <p>双向模式会传播新增、修改和删除。并发编辑会产生 sync-conflict 冲突副本。
            由其他设备造成的覆盖或删除，旧文件保存在该目录的 .stversions 文件夹，约保留 30 天；
            本机主动删除的文件依靠其他电脑的历史版本保留。恢复时可暂停同步，从 .stversions 复制需要的旧文件回原路径后再次同步。
            历史版本会占用磁盘空间。</p>
            <p>局域网会尝试自动发现，跨网络尝试直连或公共中继。连接使用设备证书加密，设备 ID 是身份校验值，支持多台电脑共享。
            公共发现与中继服务由 Syncthing 提供，实际连接取决于网络状况。固定地址可在编辑电脑中填写 tcp://IP:22000。</p>
            <p>保持工具箱运行才能执行计划任务。勾选托盘后台后关闭窗口仍会运行；从托盘「退出工具箱」会停止同步。
            「下次打开时启动」随工具箱启动，不会设置 Windows 开机启动。</p>
            <p>设备身份、索引、同步配置保存在 %APPDATA%/Omini工具箱/sync/，每台电脑单独生成。
            传给其他电脑时使用完整发布包，不复制这个个人配置目录。移除同步项只移除配置，保留已同步文件。</p>""")
        self.tabs.addTab(help_view, "使用说明")
        self.status = QLabel("点击启动同步服务，生成本机设备 ID。")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        try:
            manager = manager or SyncManager(directory)
            self.worker = SyncWorker(manager, self)
            self.worker.updated.connect(self.render_snapshot)
            self.worker.message.connect(self.show_message)
            self.worker.service.connect(self.service_changed)
            self.worker.busy.connect(self.busy_changed)
            self.worker.tailscale.connect(self.tailscale_loaded)
            self.worker.start()
        except Exception as error:
            self.show_message(str(error))
            self.start_button.setEnabled(False)
        self.update_controls()

    @staticmethod
    def table(columns):
        table = QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.horizontalHeader().setStretchLastSection(True)
        table.verticalHeader().setVisible(False)
        for column in range(len(columns)):
            table.setColumnWidth(column, (210, 240, 165, 175, 110)[column])
        return table

    def submit(self, action, *args):
        if self.worker and not self.stopping:
            self.worker.submit(action, *args)

    def busy_changed(self, value):
        self.command_busy = value
        self.update_controls()

    def service_changed(self, state):
        self.running = state["running"]
        self.start_button.setText("同步服务运行中" if self.running else "启动同步服务")
        self.background_enabled = state["background"] and self.running
        for control, key in ((self.autostart, "enabled"), (self.background, "background")):
            control.blockSignals(True)
            control.setChecked(state[key])
            control.blockSignals(False)
        self.update_controls()

    def update_controls(self):
        ready = self.running and not self.command_busy and not self.stopping
        self.start_button.setEnabled(bool(self.worker) and not self.running and not self.command_busy and not self.stopping)
        self.stop_button.setEnabled(ready)
        self.copy_id_button.setEnabled(bool(self.my_id.text()))
        self.copy_tailscale_button.setEnabled(bool(self.tailscale_address.text()))
        self.read_tailscale_button.setEnabled(bool(self.worker) and not self.command_busy and not self.stopping)
        self.rename_button.setEnabled(ready)
        for button in self.folder_buttons + self.device_buttons:
            button.setEnabled(ready)
        selected = self.selected_folder() is not None
        for button in self.folder_buttons[1:]:
            button.setEnabled(ready and selected)
        self.add_folder_button.setEnabled(ready and bool(self.snapshot_data["devices"]))

    def copy(self, value, message):
        if value:
            QApplication.clipboard().setText(value)
            self.show_message(message)

    def show_message(self, message):
        self.status.setText(message)
        self.log.append(datetime.now().strftime("%H:%M:%S") + "  " + message.replace("<", "&lt;").replace(">", "&gt;"))

    def selected_folder(self):
        row = self.folder_table.currentRow()
        return self.snapshot_data["folders"][row] if 0 <= row < len(self.snapshot_data["folders"]) else None

    def selected_device(self):
        row = self.device_table.currentRow()
        return self.snapshot_data["devices"][row] if 0 <= row < len(self.snapshot_data["devices"]) else None

    def selection_changed(self):
        folder = self.selected_folder()
        self.detail.setText(folder.get("detail", "") if folder else "请选择同步文件夹查看详情。")
        self.update_controls()

    def render_snapshot(self, data):
        folder_id = (self.selected_folder() or {}).get("id")
        device_id = (self.selected_device() or {}).get("deviceID")
        self.snapshot_data = data
        self.my_id.setText(data["device_id"])
        self.my_id.setCursorPosition(0)
        if not self.my_name.hasFocus():
            self.my_name.setText(data.get("name", ""))
        self.folder_table.blockSignals(True)
        self.folder_table.setRowCount(len(data["folders"]))
        for row, folder in enumerate(data["folders"]):
            policy = folder["policy"]
            mode = MODES.get(policy["mode"], policy["mode"])
            due = policy.get("next_run", 0)
            if due and policy["mode"] in ("daily", "interval"):
                mode += "\n" + datetime.fromtimestamp(due).strftime("%m-%d %H:%M")
            values = [folder["label"] + "\n" + folder["id"], folder["path"], mode, folder["status"], folder["progress"]]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value + ("\n" + folder.get("detail", "") if column == 3 else ""))
                self.folder_table.setItem(row, column, item)
            self.folder_table.setRowHeight(row, 56)
            if folder["id"] == folder_id:
                self.folder_table.selectRow(row)
        self.folder_table.blockSignals(False)
        self.device_table.setRowCount(len(data["devices"]))
        for row, device in enumerate(data["devices"]):
            values = [device["name"], device["deviceID"], "在线" if device["connected"] else "离线 / 等待对方添加", device["address"]]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.device_table.setItem(row, column, item)
            if device["deviceID"] == device_id:
                self.device_table.selectRow(row)
        self.selection_changed()

    def tailscale_loaded(self, inventory):
        self.tailscale_inventory = inventory
        ips = inventory.get("self", {}).get("ips", [])
        address = next((ip for ip in ips if ":" not in ip), next(iter(ips), ""))
        self.tailscale_address.setText(address)
        self.tailscale_address.setCursorPosition(0)
        self.show_message(inventory.get("error") or (
            f"Tailscale：{inventory.get('state', 'Unknown')}；读取到 {len(inventory.get('peers', []))} 台组网设备，可在添加电脑时自行选择。"))
        self.update_controls()

    def device_dialog(self, device=None):
        dialog = DeviceDialog(device, self, inventory=self.tailscale_inventory)
        dialog.refresh_tailscale.connect(lambda: self.submit("tailscale"))
        if self.worker:
            self.worker.tailscale.connect(dialog.set_tailscale)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.submit("device", *dialog.values())
        finally:
            if self.worker:
                self.worker.tailscale.disconnect(dialog.set_tailscale)
            dialog.deleteLater()

    def add_device(self):
        self.device_dialog()

    def edit_device(self):
        device = self.selected_device()
        if device:
            self.device_dialog(device)

    def remove_device(self):
        device = self.selected_device()
        if device and QMessageBox.question(self, "移除电脑", f"停止与 {device['name']} 共享所有文件夹？本机文件会保留。") == QMessageBox.StandardButton.Yes:
            self.submit("remove_device", device["deviceID"])

    def add_folder(self):
        dialog = FolderDialog(self.snapshot_data["devices"], parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.submit("folder", dialog.values())

    def edit_folder(self):
        folder = self.selected_folder()
        if folder:
            dialog = FolderDialog(self.snapshot_data["devices"], folder, self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.submit("folder", dialog.values())

    def folder_action(self, action):
        folder = self.selected_folder()
        if folder:
            self.submit(action, folder["id"])

    def sync_selected(self):
        self.folder_action("sync_now")

    def copy_folder_id(self):
        folder = self.selected_folder()
        if folder:
            self.copy(folder["id"], "共享 ID 已复制，请在另一台电脑添加文件夹时粘贴。")

    def open_folder(self):
        folder = self.selected_folder()
        if folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder["path"]))

    def remove_folder(self):
        folder = self.selected_folder()
        if folder and QMessageBox.question(self, "移除同步文件夹", f"移除 {folder['label']} 的同步配置？已同步的本机文件会保留。") == QMessageBox.StandardButton.Yes:
            self.submit("remove_folder", folder["id"])

    def stop(self):
        if self.stopping:
            return
        self.stopping = True
        self.background_enabled = False
        if self.worker:
            self.worker.request_stop()
        self.update_controls()
