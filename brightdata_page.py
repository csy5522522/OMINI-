"""Native Qt controls for Bright Data search, scrape, batch and export."""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Qt
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QGroupBox,
                              QHBoxLayout, QLabel, QLineEdit, QPushButton, QTabWidget,
                              QTextEdit, QVBoxLayout, QWidget, QFormLayout, QScrollArea,
                              QSpinBox, QTableWidget, QTableWidgetItem, QAbstractItemView, QHeaderView, QSplitter)

from brightdata import (BrightDataClient, BrightDataStore, BrightDataTasks, BrightDataError,
                        build_request, result_text, result_data, result_records, records_csv,
                        endpoint_from_token, token_from_endpoint, validate_scraper_inputs)

METHOD_LABELS = {"collect_by_url": "按链接采集", "discover_by_keyword": "按关键词发现",
                 "discover_by_keywords": "按关键词发现", "discover_by_category_url": "按分类发现",
                 "discover_by_best_sellers_url": "按热销榜发现"}
FIELD_LABELS = {"url": "网页地址", "keyword": "关键词", "keywords": "关键词",
               "zipcode": "邮政编码", "language": "语言", "country": "国家",
               "asin": "商品编号", "origin_url": "来源地址", "all_variations": "包含所有款式"}
STATUS_LABELS = {"starting": "准备中", "running": "采集中", "ready": "已完成",
                 "failed": "失败", "canceled": "已取消", "error": "请求错误"}


class BrightDataWorker(QThread):
    def __init__(self, endpoint, tool, arguments, parent=None):
        super().__init__(parent)
        self.endpoint = endpoint
        self.tool = tool
        self.arguments = arguments
        self.result = None
        self.definitions = []
        self.error = ""
        self.cancelled = threading.Event()

    def run(self):
        client = BrightDataClient(self.endpoint, cancelled=self.cancelled)
        try:
            client.initialize()
            if self.tool == "test":
                tools = client.list_tools()
                self.definitions = tools
                self.result = {"tool_count": len(tools), "tools": [item["name"] for item in tools]}
            else:
                self.result = client.call(self.tool, self.arguments)
                if self.result.get("isError"):
                    self.error = "服务返回任务错误，详情见结果区。"
        except Exception as error:
            # Network exceptions can include the entire credential-bearing URL.
            from brightdata import BrightDataError
            self.error = str(error) if isinstance(error, BrightDataError) else "任务处理失败，请重试连接。"
        finally:
            client.close()


class BrightDataPage(QWidget):
    def __init__(self, config_path, parent=None):
        super().__init__(parent)
        self.store = BrightDataStore(config_path)
        self.tasks = BrightDataTasks(Path(config_path).with_name("brightdata-tasks.json"))
        self.worker = None
        self.last_result = None
        self.stopping = False
        self.catalog_rows = []
        self.loaded_details = None
        self.fields = {}
        self.batch_inputs = []
        self.records = []
        self.next_cursor = ""
        self.last_search = None
        self.poll_snapshot = ""
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(10000)
        self.poll_timer.timeout.connect(self.poll_progress)
        self.action_buttons = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        title = QLabel("Bright Data 数据工具")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        subtitle = QLabel("网页搜索 · 正文抓取 · 采集器 · 采集任务 · 表格与文件导出")
        subtitle.setObjectName("muted")
        layout.addWidget(subtitle)

        connection = QGroupBox("连接设置")
        row = QHBoxLayout(connection)
        row.addWidget(QLabel("Token"))
        self.token = QLineEdit(token_from_endpoint(self.store.endpoint))
        self.endpoint = self.token  # Existing connection access remains compatible.
        self.endpoint.setEchoMode(QLineEdit.EchoMode.Password)
        self.endpoint.setPlaceholderText("粘贴链接中 token= 后面的值；也支持完整 MCP 地址")
        self.endpoint.setAccessibleName("Bright Data Token")
        row.addWidget(self.endpoint, 1)
        show = QCheckBox("显示 Token")
        show.toggled.connect(lambda visible: self.endpoint.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password))
        row.addWidget(show)
        self.save_button = QPushButton("保存连接")
        self.save_button.clicked.connect(self.save_connection)
        row.addWidget(self.save_button)
        self.test_button = QPushButton("测试连接")
        self.test_button.clicked.connect(lambda: self.start("test", {}))
        row.addWidget(self.test_button)
        self.action_buttons += [self.save_button, self.test_button]
        layout.addWidget(connection)

        tabs = QTabWidget()
        self.tabs = tabs
        layout.addWidget(tabs)
        search = QWidget()
        search_layout = QVBoxLayout(search)
        options = QHBoxLayout()
        options.addWidget(QLabel("搜索引擎"))
        self.engine = QComboBox()
        for label, value in (("Google", "google"), ("Bing", "bing"), ("Yandex", "yandex")):
            self.engine.addItem(label, value)
        options.addWidget(self.engine)
        options.addWidget(QLabel("国家代码（可选）"))
        self.country = QLineEdit()
        self.country.setPlaceholderText("例如 us / uk / cn")
        self.country.setMaxLength(2)
        self.country.setMaximumWidth(150)
        options.addWidget(self.country)
        self.cursor = QLineEdit()
        self.cursor.setPlaceholderText("分页标记（可选）")
        self.cursor.setMaximumWidth(160)
        options.addWidget(self.cursor)
        options.addStretch()
        self.search_button = QPushButton("开始搜索")
        self.search_button.setObjectName("primary")
        self.search_button.clicked.connect(lambda: self.run_input("search"))
        options.addWidget(self.search_button)
        self.next_button = QPushButton("下一页")
        self.next_button.clicked.connect(self.next_page)
        self.next_button.setEnabled(False)
        options.addWidget(self.next_button)
        search_layout.addLayout(options)
        self.queries = QTextEdit()
        self.queries.setAcceptRichText(False)
        self.queries.setPlaceholderText("输入搜索关键词；批量搜索时每行一条，最多 10 条")
        self.queries.setMaximumHeight(100)
        self.queries.textChanged.connect(self.clear_pagination)
        self.engine.currentIndexChanged.connect(self.clear_pagination)
        self.country.textChanged.connect(self.clear_pagination)
        search_layout.addWidget(self.queries)
        tabs.addTab(search, "网页搜索")

        scrape = QWidget()
        scrape_layout = QVBoxLayout(scrape)
        scrape_row = QHBoxLayout()
        scrape_row.addWidget(QLabel("提取网页正文为 Markdown；每行一个 URL，最多 10 条"))
        scrape_row.addStretch()
        self.scrape_button = QPushButton("开始抓取")
        self.scrape_button.setObjectName("primary")
        self.scrape_button.clicked.connect(lambda: self.run_input("scrape"))
        scrape_row.addWidget(self.scrape_button)
        scrape_layout.addLayout(scrape_row)
        self.urls = QTextEdit()
        self.urls.setAcceptRichText(False)
        self.urls.setPlaceholderText("https://example.com\nhttps://example.org")
        self.urls.setMaximumHeight(100)
        scrape_layout.addWidget(self.urls)
        tabs.addTab(scrape, "网页抓取")
        self.action_buttons += [self.search_button, self.scrape_button]
        self.action_buttons.append(self.next_button)
        self._build_catalog(tabs)
        self._build_tasks(tabs)
        self._build_help(tabs)
        for button in self.action_buttons:
            if button.objectName() == "primary":
                button.setStyleSheet("QPushButton#primary:disabled { background: transparent; color: #7a889c; }")
        tabs.setMaximumHeight(240)
        tabs.currentChanged.connect(lambda index: tabs.setMaximumHeight(420 if index == 2 else 260 if index == 3 else 240))

        result_bar = QHBoxLayout()
        result_bar.addWidget(QLabel("处理结果"))
        result_bar.addStretch()
        self.copy_button = QPushButton("复制结果")
        self.copy_button.clicked.connect(self.copy_result)
        self.export_button = QPushButton("导出结果")
        self.export_button.clicked.connect(self.export_result)
        self.copy_button.setEnabled(False)
        self.export_button.setEnabled(False)
        result_bar.addWidget(self.copy_button)
        result_bar.addWidget(self.export_button)
        layout.addLayout(result_bar)
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("搜索、抓取或测试连接后，结果将显示在这里。")
        self.result_tabs = QTabWidget()
        self.result_tabs.addTab(self.output, "文本 / Markdown")
        self.result_table = self.make_table(["暂无记录"])
        self.result_table.cellDoubleClicked.connect(self.result_to_scrape)
        self.result_tabs.addTab(self.result_table, "表格（双击地址可送到抓取页）")
        layout.addWidget(self.result_tabs, 1)
        self.status = QLabel(self.store.error or self.tasks.error or ("Token 已配置，可以直接使用。" if self.store.endpoint else "请填写 Token 并保存连接。"))
        self.status.setWordWrap(True)
        self.status.setObjectName("muted")
        layout.addWidget(self.status)
        self.refresh_tasks()
        self.update_action_state()

    def save_connection(self):
        try:
            self.store.save(self.current_endpoint())
            self.token.setText(token_from_endpoint(self.store.endpoint))
            self.status.setText("连接已加密保存，重启后自动恢复。")
        except (ValueError, OSError, RuntimeError):
            self.status.setText("保存失败，请检查 Token 和配置目录。")

    def current_endpoint(self):
        value = self.token.text().strip()
        if self.store.endpoint and value == token_from_endpoint(self.store.endpoint):
            return self.store.endpoint
        return endpoint_from_token(value)

    def run_input(self, kind):
        try:
            text = self.queries.toPlainText() if kind == "search" else self.urls.toPlainText()
            tool, arguments = build_request(kind, text, self.engine.currentData(), self.country.text(), self.cursor.text() if kind == "search" else "")
            self.start(tool, arguments)
        except ValueError as error:
            self.status.setText(str(error))

    def start(self, tool, arguments):
        if self.worker is not None:
            return
        try:
            endpoint = self.current_endpoint()
        except ValueError:
            self.status.setText("请先填写有效的 Token，也可粘贴完整 MCP 地址。")
            return
        self.stopping = False
        for button in self.action_buttons:
            button.setEnabled(False)
        self.endpoint.setEnabled(False)
        self.status.setText("正在测试连接…" if tool == "test" else "正在处理，请稍候…")
        self.worker = BrightDataWorker(endpoint, tool, arguments, self)
        self.worker.finished.connect(self.finished)
        self.worker.start()

    def finished(self):
        worker = self.worker
        self.worker = None
        if worker is None:
            return
        if not self.stopping:
            if worker.result is not None:
                self.last_result = worker.result
                self.output.setPlainText(result_text(worker.result))
                self.show_records(result_records(result_data(worker.result)))
                self.copy_button.setEnabled(True)
                self.export_button.setEnabled(True)
            if worker.error:
                self.status.setText(worker.error)
                if worker.tool in ("get_scraper_progress", "get_scraper_results"):
                    self.poll_timer.stop()
            elif worker.tool == "test":
                self.status.setText(f"连接正常，已发现 {worker.result['tool_count']} 个工具。")
            else:
                self.status.setText("处理完成，可以复制或导出结果。")
        self.endpoint.setEnabled(True)
        for button in self.action_buttons:
            button.setEnabled(True)
        if worker.result is not None and not worker.error:
            try:
                self.handle_result(worker)
            except (ValueError, OSError) as error:
                self.status.setText(str(error))
        self.update_action_state()
        worker.deleteLater()

    def copy_result(self):
        QApplication.clipboard().setText(self.output.toPlainText())
        self.status.setText("结果已复制。")

    def export_result(self):
        if self.last_result is None:
            return
        filename, selected = QFileDialog.getSaveFileName(
            self, "导出 Bright Data 结果", "brightdata-result.md",
            "Markdown (*.md);;JSON (*.json);;CSV 表格 (*.csv);;文本 (*.txt)")
        if not filename:
            return
        path = Path(filename)
        if not path.suffix:
            path = path.with_suffix(".json" if selected.startswith("JSON") else ".csv" if selected.startswith("CSV") else ".txt" if selected.startswith("文本") else ".md")
        try:
            text = json.dumps(result_data(self.last_result), ensure_ascii=False, indent=2) if path.suffix.lower() == ".json" else records_csv(self.records) if path.suffix.lower() == ".csv" else self.output.toPlainText()
            path.write_text(text, encoding="utf-8-sig" if path.suffix.lower() == ".csv" else "utf-8", newline="")
            self.status.setText(f"结果已导出到 {path}")
        except OSError:
            self.status.setText("导出失败，请检查目标目录。")
        except ValueError as error:
            self.status.setText(str(error))

    def stop(self):
        self.stopping = True
        self.poll_timer.stop()
        if self.worker and self.worker.tool != "run_scraper":
            self.worker.cancelled.set()

    @staticmethod
    def make_table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.horizontalHeader().setStretchLastSection(True)
        table.verticalHeader().hide()
        return table

    def _build_catalog(self, tabs):
        page = QWidget()
        layout = QVBoxLayout(page)
        bar = QHBoxLayout()
        self.catalog_query = QLineEdit()
        self.catalog_query.setPlaceholderText("输入站点或数据类型，如 amazon products")
        self.catalog_query.returnPressed.connect(self.find_scrapers)
        bar.addWidget(self.catalog_query, 1)
        self.catalog_limit = QSpinBox()
        self.catalog_limit.setRange(1, 50)
        self.catalog_limit.setValue(10)
        bar.addWidget(QLabel("数量"))
        bar.addWidget(self.catalog_limit)
        self.find_button = QPushButton("查找采集器")
        self.find_button.clicked.connect(self.find_scrapers)
        self.refresh_button = QPushButton("刷新目录")
        self.refresh_button.clicked.connect(lambda: self.start("refresh_scrapers", {}))
        bar.addWidget(self.find_button)
        bar.addWidget(self.refresh_button)
        layout.addLayout(bar)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.catalog_table = self.make_table(["采集器名称", "网站", "数据集 ID"])
        self.catalog_table.setColumnWidth(0, 170)
        self.catalog_table.setColumnWidth(1, 100)
        self.catalog_table.currentCellChanged.connect(lambda *_: self.select_scraper())
        splitter.addWidget(self.catalog_table)
        editor = QWidget()
        edit_layout = QVBoxLayout(editor)
        identifiers = QFormLayout()
        self.dataset = QLineEdit()
        self.dataset.setPlaceholderText("选择左侧采集器，也可输入数据集 ID")
        self.dataset.textChanged.connect(self.invalidate_details)
        identifiers.addRow("数据集", self.dataset)
        method_bar = QHBoxLayout()
        self.method = QComboBox()
        self.method.setEditable(True)
        self.method.addItem("按链接采集", "collect_by_url")
        self.method.currentTextChanged.connect(self.invalidate_details)
        method_bar.addWidget(self.method, 1)
        self.details_button = QPushButton("读取参数")
        self.details_button.clicked.connect(self.get_details)
        method_bar.addWidget(self.details_button)
        identifiers.addRow("采集方式", method_bar)
        edit_layout.addLayout(identifiers)
        self.input_tabs = QTabWidget()
        self.form_scroll = QScrollArea()
        self.form_scroll.setWidgetResizable(True)
        self.form_scroll.setMinimumHeight(90)
        self.input_tabs.addTab(self.form_scroll, "表单输入")
        self.json_inputs = QTextEdit()
        self.json_inputs.setAcceptRichText(False)
        self.json_inputs.setPlaceholderText('批量输入示例：[ {"url": "https://example.com"} ]')
        self.input_tabs.addTab(self.json_inputs, "批量 JSON（高级）")
        self.input_tabs.setMaximumHeight(170)
        edit_layout.addWidget(self.input_tabs, 1)
        batch_bar = QHBoxLayout()
        self.sample_button = QPushButton("填入示例")
        self.sample_button.clicked.connect(self.fill_sample)
        self.add_input_button = QPushButton("加入批次")
        self.add_input_button.clicked.connect(self.add_input)
        self.clear_inputs_button = QPushButton("清空批次")
        self.clear_inputs_button.clicked.connect(self.clear_inputs)
        self.batch_count = QLabel("批次：0 条")
        for button in (self.sample_button, self.add_input_button, self.clear_inputs_button):
            batch_bar.addWidget(button)
        batch_bar.addWidget(self.batch_count)
        edit_layout.addLayout(batch_bar)
        run_bar = QHBoxLayout()
        run_bar.addWidget(QLabel("每个输入最多返回"))
        self.record_limit = QSpinBox()
        self.record_limit.setRange(1, 1000)
        self.record_limit.setValue(10)
        run_bar.addWidget(self.record_limit)
        run_bar.addStretch()
        self.run_button = QPushButton("运行采集")
        self.run_button.setObjectName("primary")
        self.run_button.clicked.connect(self.run_scraper)
        run_bar.addWidget(self.run_button)
        edit_layout.addLayout(run_bar)
        self.collection_note = QLabel("先读取参数，再填写输入。发现式采集按返回记录计费。")
        self.collection_note.setWordWrap(True)
        self.collection_note.setObjectName("muted")
        edit_layout.addWidget(self.collection_note)
        splitter.addWidget(editor)
        splitter.setSizes([430, 570])
        layout.addWidget(splitter, 1)
        tabs.addTab(page, "采集器")
        self.action_buttons += [self.find_button, self.refresh_button, self.details_button,
                                self.sample_button, self.add_input_button, self.clear_inputs_button, self.run_button]
        self.invalidate_details()

    def _build_tasks(self, tabs):
        page = QWidget()
        layout = QVBoxLayout(page)
        bar = QHBoxLayout()
        self.snapshot = QLineEdit()
        self.snapshot.setPlaceholderText("粘贴已有任务 ID，或选择下方任务记录")
        self.snapshot.textChanged.connect(lambda *_: self.poll_timer.stop())
        self.snapshot.editingFinished.connect(lambda: self.poll_setting_changed(self.auto_poll.isChecked()))
        bar.addWidget(self.snapshot, 1)
        self.progress_button = QPushButton("查询进度")
        self.progress_button.clicked.connect(lambda: self.query_snapshot("get_scraper_progress"))
        self.results_button = QPushButton("获取结果")
        self.results_button.clicked.connect(lambda: self.query_snapshot("get_scraper_results"))
        bar.addWidget(self.progress_button)
        bar.addWidget(self.results_button)
        layout.addLayout(bar)
        self.task_table = self.make_table(["任务 ID", "采集器", "状态", "创建时间"])
        self.task_table.currentCellChanged.connect(lambda row, *_: self.select_task(row))
        self.task_table.setColumnWidth(0, 250)
        self.task_table.setColumnWidth(1, 200)
        layout.addWidget(self.task_table, 1)
        poll_bar = QHBoxLayout()
        self.auto_poll = QCheckBox("自动更新进度（每 10 秒），完成后获取结果")
        self.auto_poll.setChecked(True)
        self.auto_poll.toggled.connect(self.poll_setting_changed)
        poll_bar.addWidget(self.auto_poll)
        poll_bar.addStretch()
        pause = QPushButton("暂停自动更新")
        pause.clicked.connect(lambda: self.auto_poll.setChecked(False))
        poll_bar.addWidget(pause)
        layout.addLayout(poll_bar)
        tabs.addTab(page, "采集任务")
        self.action_buttons += [self.progress_button, self.results_button]

    def _build_help(self, tabs):
        page = QWidget()
        layout = QVBoxLayout(page)
        label = QLabel("Bright Data 官方支持查询：填写产品、配额、采集配置或错误问题，点击按钮获取说明。")
        label.setWordWrap(True)
        layout.addWidget(label)
        self.help_question = QTextEdit()
        self.help_question.setAcceptRichText(False)
        self.help_question.setPlaceholderText("例如：如何选择适合商品评论采集的产品？")
        layout.addWidget(self.help_question)
        row = QHBoxLayout()
        self.dialog_id = QLineEdit()
        self.dialog_id.setPlaceholderText("参考查询 ID（可选，24 个字符）")
        self.dialog_id.setMaxLength(24)
        row.addWidget(self.dialog_id, 1)
        self.help_button = QPushButton("获取官方说明")
        self.help_button.clicked.connect(self.ask_support)
        row.addWidget(self.help_button)
        layout.addLayout(row)
        tabs.addTab(page, "使用帮助")
        self.action_buttons.append(self.help_button)

    def clear_pagination(self, *_):
        self.next_cursor = ""
        self.last_search = None
        self.cursor.clear()
        self.next_button.setEnabled(False)

    def next_page(self):
        if self.next_cursor and self.last_search:
            args = dict(self.last_search, cursor=self.next_cursor)
            self.cursor.setText(self.next_cursor)
            self.start("search_engine", args)

    def find_scrapers(self):
        query = self.catalog_query.text().strip()
        if not query:
            self.status.setText("请填写站点或数据类型。")
            return
        self.start("search_scrapers", {"query": query, "limit": self.catalog_limit.value()})

    def select_scraper(self):
        row = self.catalog_table.currentRow()
        if not 0 <= row < len(self.catalog_rows):
            return
        item = self.catalog_rows[row]
        self.dataset.setText(str(item.get("dataset_id", "")))
        self.method.clear()
        for method in item.get("collection_methods", []):
            self.method.addItem(METHOD_LABELS.get(method, method), method)
        self.invalidate_details()

    def selected_method(self):
        text = self.method.currentText().strip()
        if text == self.method.itemText(self.method.currentIndex()):
            return self.method.currentData() or text
        return text

    def invalidate_details(self, *_):
        self.loaded_details = None
        self.fields = {}
        self.batch_inputs = []
        if hasattr(self, "batch_count"):
            self.batch_count.setText("批次：0 条")
            self.json_inputs.clear()
            empty = QLabel("点击「读取参数」，自动生成此采集器的输入表单。")
            empty.setWordWrap(True)
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.form_scroll.setWidget(empty)
            self.update_action_state()

    def update_action_state(self):
        idle = self.worker is None
        self.next_button.setEnabled(idle and bool(self.next_cursor and self.last_search))
        ready = idle and self.loaded_details is not None
        for name in ("run_button", "add_input_button", "sample_button"):
            if hasattr(self, name):
                getattr(self, name).setEnabled(ready)
        if hasattr(self, "record_limit"):
            self.record_limit.setEnabled(self.selected_method().startswith("discover_by_"))

    def get_details(self):
        dataset, method = self.dataset.text().strip(), self.selected_method()
        if not dataset or not method:
            self.status.setText("请选择采集器及采集方式。")
            return
        self.start("get_scraper_details", {"dataset_id": dataset, "method": method})

    def load_details(self, details, arguments):
        if not isinstance(details, dict) or not isinstance(details.get("input_fields"), list):
            self.status.setText("服务未返回输入表单；请检查采集器和采集方式，详情见结果区。")
            return
        if (arguments["dataset_id"], arguments["method"]) != (self.dataset.text().strip(), self.selected_method()):
            return
        self.loaded_details = dict(details, dataset_id=arguments["dataset_id"], method=arguments["method"])
        form_widget = QWidget()
        form = QFormLayout(form_widget)
        form.setContentsMargins(8, 6, 8, 6)
        self.fields = {}
        examples = details.get("sample_input", [])
        example = examples[0] if examples and isinstance(examples[0], dict) else {}
        for field in details["input_fields"]:
            key = field.get("name")
            if not key:
                continue
            if field.get("type") == "boolean":
                widget = QComboBox()
                for label, value in (("未填写", None), ("是", True), ("否", False)):
                    widget.addItem(label, value)
            else:
                widget = QLineEdit()
                sample = example.get(key, "")
                widget.setPlaceholderText(json.dumps(sample, ensure_ascii=False) if isinstance(sample, (dict, list)) else str(sample))
            widget.setToolTip(str(field.get("description", key)))
            self.fields[key] = (field, widget)
            form.addRow(FIELD_LABELS.get(key, key) + (" *" if field.get("required") else ""), widget)
        self.form_scroll.setWidget(form_widget)
        self.json_inputs.setPlainText("[]")
        self.batch_inputs = []
        self.batch_count.setText("批次：0 条")
        self.collection_note.setToolTip(str(details.get("description", "")))

    def form_record(self):
        record = {}
        for key, (field, widget) in self.fields.items():
            if field.get("type") == "boolean":
                value = widget.currentData()
                if value is None:
                    continue
            else:
                value = widget.text().strip()
                if not value:
                    continue
                if field.get("type") in ("integer", "number", "float"):
                    try:
                        value = int(value) if field["type"] == "integer" else float(value)
                    except ValueError:
                        raise BrightDataError(f"{key} 需填写数字。")
                elif field.get("type") in ("array", "list", "object", "json"):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        raise BrightDataError(f"{key} 需填写有效 JSON。")
            record[key] = value
        validate_scraper_inputs(self.loaded_details or {}, [record])
        return record

    def fill_sample(self):
        samples = (self.loaded_details or {}).get("sample_input", [])
        if not samples or not isinstance(samples[0], dict):
            self.status.setText("此采集器没有提供示例，请按必填项填写。")
            return
        for key, (field, widget) in self.fields.items():
            value = samples[0].get(key)
            if field.get("type") == "boolean":
                widget.setCurrentIndex(widget.findData(value))
            else:
                widget.setText("" if value is None else json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value))
        self.json_inputs.setPlainText(json.dumps(samples, ensure_ascii=False, indent=2))
        self.status.setText("示例已填入，请按实际采集目标修改后运行。")

    def add_input(self):
        try:
            self.batch_inputs.append(self.form_record())
            self.batch_count.setText(f"批次：{len(self.batch_inputs)} 条")
            self.json_inputs.setPlainText(json.dumps(self.batch_inputs, ensure_ascii=False, indent=2))
            self.status.setText(f"已加入批次，共 {len(self.batch_inputs)} 条；继续改填表单可添加下一条。")
        except ValueError as error:
            self.status.setText(str(error))

    def clear_inputs(self):
        self.batch_inputs = []
        self.batch_count.setText("批次：0 条")
        self.json_inputs.setPlainText("[]")

    def run_scraper(self):
        try:
            details = self.loaded_details
            if not details or (details["dataset_id"], details["method"]) != (self.dataset.text().strip(), self.selected_method()):
                raise BrightDataError("请先读取当前采集方式的参数。")
            if self.input_tabs.currentIndex() == 1:
                try:
                    inputs = json.loads(self.json_inputs.toPlainText())
                except ValueError:
                    raise BrightDataError("批量 JSON 格式有误，请填写对象数组。")
            else:
                inputs = self.batch_inputs or [self.form_record()]
            arguments = {"dataset_id": details["dataset_id"], "method": details["method"],
                         "input": validate_scraper_inputs(details, inputs)}
            if details["method"].startswith("discover_by_"):
                arguments["limit_per_input"] = self.record_limit.value()
            self.start("run_scraper", arguments)
        except ValueError as error:
            self.status.setText(str(error))

    def ask_support(self):
        question = self.help_question.toPlainText().strip()
        dialog = self.dialog_id.text().strip()
        if not question or len(question) > 20480 or (dialog and len(dialog) != 24):
            self.status.setText("请填写问题（最多 20480 字符）；参考查询 ID 为 24 个字符。")
            return
        self.start("ask_brightdata_assistant", dict(question=question, **({"dialog_id": dialog} if dialog else {})))

    def refresh_tasks(self):
        self.task_table.blockSignals(True)
        self.task_table.setRowCount(len(self.tasks.items))
        for row, item in enumerate(self.tasks.items):
            values = [item["snapshot_id"], item.get("name", item.get("dataset_id", "")),
                      STATUS_LABELS.get(item.get("status"), item.get("status", "待查询")), item.get("started", "")]
            for column, value in enumerate(values):
                self.task_table.setItem(row, column, QTableWidgetItem(str(value)))
        self.task_table.blockSignals(False)

    def select_task(self, row):
        if 0 <= row < len(self.tasks.items):
            self.snapshot.setText(self.tasks.items[row]["snapshot_id"])
            self.poll_setting_changed(self.auto_poll.isChecked())

    def query_snapshot(self, tool):
        snapshot = self.snapshot.text().strip()
        if not snapshot:
            self.status.setText("请填写或选择一个任务 ID。")
            return
        self.start(tool, {"snapshot_id": snapshot})

    def poll_setting_changed(self, enabled):
        self.poll_timer.stop()
        self.poll_snapshot = self.snapshot.text().strip()
        item = next((item for item in self.tasks.items if item["snapshot_id"] == self.poll_snapshot), {})
        if enabled and not self.stopping and item.get("status") in ("starting", "running"):
            self.poll_timer.start()

    def poll_progress(self):
        if self.worker is None and not self.stopping and self.auto_poll.isChecked() and self.poll_snapshot:
            self.start("get_scraper_progress", {"snapshot_id": self.poll_snapshot})

    def handle_result(self, worker):
        data = result_data(worker.result)
        tool = worker.tool
        if tool == "search_scrapers" and not self.stopping:
            rows = result_records(data)
            self.catalog_rows = [row for row in rows if row.get("dataset_id")]
            self.catalog_table.blockSignals(True)
            self.catalog_table.setRowCount(len(self.catalog_rows))
            for row, item in enumerate(self.catalog_rows):
                for column, key in enumerate(("name", "domain", "dataset_id")):
                    self.catalog_table.setItem(row, column, QTableWidgetItem(str(item.get(key, ""))))
            self.catalog_table.blockSignals(False)
            if self.catalog_rows:
                self.catalog_table.selectRow(0)
                self.select_scraper()
            self.status.setText(f"找到 {len(self.catalog_rows)} 个采集器，选择采集方式后点击「读取参数」。")
        elif tool == "get_scraper_details" and not self.stopping:
            self.load_details(data, worker.arguments)
        elif tool == "search_engine" and not self.stopping:
            self.last_search = worker.arguments
            self.next_cursor = str(data.get("next_cursor") or data.get("nextCursor") or data.get("cursor") or "") if isinstance(data, dict) else ""
        elif tool in ("run_scraper", "get_scraper_progress", "get_scraper_results"):
            snapshot = data.get("snapshot_id", "") if isinstance(data, dict) else ""
            snapshot = snapshot or worker.arguments.get("snapshot_id", "")
            if not snapshot:
                return
            values = {}
            state = data.get("status") if isinstance(data, dict) else None
            if not state:
                state = "ready" if tool == "get_scraper_results" or (tool == "run_scraper" and result_records(data)) else "running"
            values["status"] = state
            if tool == "run_scraper":
                values.update(dataset_id=worker.arguments["dataset_id"], method=worker.arguments["method"],
                              name=(self.loaded_details or {}).get("name", worker.arguments["dataset_id"]),
                              started=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            self.tasks.update(snapshot, **values)
            self.refresh_tasks()
            if self.stopping:
                return
            if tool == "run_scraper":
                self.snapshot.setText(snapshot)
                self.tabs.setCurrentIndex(3)
            if snapshot == self.snapshot.text().strip():
                self.poll_setting_changed(self.auto_poll.isChecked())
            if state == "ready" and tool != "get_scraper_results" and not result_records(data) and self.auto_poll.isChecked() and snapshot == self.snapshot.text().strip():
                QTimer.singleShot(0, lambda sid=snapshot: self.auto_fetch(sid))
            self.status.setText(f"任务 {snapshot}：{STATUS_LABELS.get(state, state)}")

    def auto_fetch(self, snapshot):
        if not self.stopping and self.auto_poll.isChecked() and self.worker is None and snapshot == self.snapshot.text().strip():
            self.start("get_scraper_results", {"snapshot_id": snapshot})

    def show_records(self, records):
        self.records = records
        columns = list(dict.fromkeys(key for row in records[:300] for key in row))
        self.result_table.setColumnCount(len(columns))
        self.result_table.setHorizontalHeaderLabels(columns)
        self.result_table.setRowCount(min(300, len(records)))
        for row, record in enumerate(records[:300]):
            for column, key in enumerate(columns):
                value = record.get(key, "")
                text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
                self.result_table.setItem(row, column, QTableWidgetItem(text))
        self.result_tabs.setTabText(1, f"表格（{len(records)} 条，预览最多 300 条）")

    def result_to_scrape(self, row, column):
        item = self.result_table.item(row, column)
        if item and item.text().startswith(("http://", "https://")):
            self.urls.setPlainText(item.text())
            self.tabs.setCurrentIndex(1)
            self.status.setText("地址已填入网页抓取页，点击「开始抓取」即可处理。")
