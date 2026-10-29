"""Offline timetable with HAR import and external browser login."""
from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
import base64
from pathlib import Path

from PySide6.QtCore import QDate, QThread, QTimer, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDialog, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
    QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from campus_network import protect_password, unprotect_password
from class_timetable_page import ClassTimetablePage
from timetable_table import import_table
from school_session_bridge import SchoolSessionBridge
from project_paths import RESOURCE_ROOT
from school_timetable import (
    CLASS_PAGE_URL, HOST, LOGIN_URL, PAGE_URL, TERMS, fetch_schedule, import_har, normalize_cookie, validate_cache,
)

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


class TimetableWorker(QThread):
    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation
        self.result = None
        self.error = ""

    def run(self):
        try:
            self.result = self.operation(self.isInterruptionRequested)
        except (ValueError, RuntimeError) as error:
            self.error = str(error)
        except Exception:
            self.error = "课表读取失败，请检查文件或重新登录教务系统。"


class SchoolLoginDialog(QDialog):
    """Receive an opt-in school session through the browser connector or paste."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.cookie_header = ""
        self.bridge = None
        self.setWindowTitle("学校课表 · 系统浏览器登录")
        self.resize(720, 340)
        layout = QVBoxLayout(self)
        note = QLabel("1. 在打开的桌面教务连接页面安装并连接「Omini 教务连接」扩展。\n"
                      "2. 完成学校统一认证后，教务 Cookie 将自动填入这里，点击「读取课表」。\n"
                      "3. 也可手动粘贴 Cookie，或在导入课表中选择表格、HAR / JSON 文件。")
        note.setWordWrap(True); layout.addWidget(note)
        row = QHBoxLayout()
        login = QPushButton("打开统一认证"); login.clicked.connect(self.open_login)
        timetable = QPushButton("打开个人课表"); timetable.clicked.connect(lambda: self.open_url(PAGE_URL))
        row.addWidget(login); row.addWidget(timetable); row.addStretch(); layout.addLayout(row)
        self.cookie = QLineEdit(); self.cookie.setEchoMode(QLineEdit.EchoMode.Password)
        self.cookie.setPlaceholderText("粘贴教务系统课表请求的 Cookie 请求头")
        layout.addWidget(self.cookie)
        self.message = QLabel("HAR 导入与在线刷新均保留离线课表。Cookie 仅用于学校教务接口。")
        self.message.setWordWrap(True); layout.addWidget(self.message)
        buttons = QHBoxLayout()
        read = QPushButton("读取课表"); read.setObjectName("primary"); read.clicked.connect(self._read)
        close = QPushButton("关闭"); close.clicked.connect(self.reject)
        buttons.addStretch(); buttons.addWidget(read); buttons.addWidget(close); layout.addLayout(buttons)
        self.poll = QTimer(self); self.poll.setInterval(300); self.poll.timeout.connect(self._receive_session); self.poll.start()
        self.finished.connect(self._close_bridge)

    def _receive_session(self):
        if self.bridge and self.bridge.cookie:
            self.cookie.setText(self.bridge.cookie)
            self.bridge.cookie = ''
            self.message.setText("教务会话已自动填入，点击「读取课表」继续。")

    def _close_bridge(self, *_):
        self.poll.stop()
        if self.bridge:
            self.bridge.close(); self.bridge = None

    def open_url(self, url):
        if QDesktopServices.openUrl(QUrl(url)):
            self.message.setText("已交给系统浏览器。完成登录后可导出 HAR，或粘贴课表请求 Cookie。")
        else:
            self.message.setText("系统浏览器启动失败，请手动打开：" + url)

    def open_login(self):
        try:
            if not self.bridge: self.bridge = SchoolSessionBridge(RESOURCE_ROOT / 'web/school-connector.zip')
            self.open_url(self.bridge.url)
            self.message.setText("请在连接页面点击浏览器扩展图标，完成学校登录后会自动填入 Cookie。")
        except OSError as error:
            self.message.setText(f"浏览器连接启动失败：{error}，可手动粘贴 Cookie。")

    def _read(self):
        try:
            self.cookie_header = normalize_cookie(self.cookie.text())
        except ValueError as error:
            self.message.setText(str(error)); return
        self.accept()


class TimetablePage(QWidget):
    def __init__(self, settings, save_settings, cache_path, save_cache, parent=None):
        super().__init__(parent)
        self.settings, self.save_settings = settings, save_settings
        self.cache_path, self.save_cache = Path(cache_path), save_cache
        self.config = dict(settings.get("school_timetable", {}))
        self.schedule = None
        self.worker = None
        self.stopping = False
        self.login_dialog = None
        self._build_ui()
        try:
            self.cookie.setText(unprotect_password(self.config.get("session_blob", "")))
        except (RuntimeError, ValueError, UnicodeError):
            self.status.setText("已保存会话读取失败，请重新登录。")
        try:
            if self.cache_path.is_file():
                value = validate_cache(json.loads(self.cache_path.read_text(encoding="utf-8")))
                if (str(self.config.get("year", "")), self.config.get("term")) != (value["year"], value["term"]):
                    self.use_start.setChecked(False)
                self._display(value)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.status.setText("本地课表读取失败，请重新导入 HAR。")
        self.clock = QTimer(self); self.clock.setInterval(60000)
        self.clock.timeout.connect(self._update_today); self.clock.start()

    def _build_ui(self):
        layout = QVBoxLayout(self); layout.setContentsMargins(28, 24, 28, 20)
        title = QLabel("学校课表"); title.setObjectName("pageTitle"); layout.addWidget(title)
        subtitle = QLabel("广州华立学院 · 教务系统课表 · 导入后可离线查看")
        subtitle.setObjectName("muted"); layout.addWidget(subtitle)
        actions = QHBoxLayout()
        self.import_button = QPushButton("导入课表"); self.import_button.clicked.connect(self.choose_import)
        self.login_button = QPushButton("浏览器登录 / 导入说明"); self.login_button.setObjectName("primary")
        self.login_button.clicked.connect(self.login)
        self.refresh_button = QPushButton("在线刷新"); self.refresh_button.clicked.connect(self.refresh)
        self.class_button = QPushButton("查询班级课表"); self.class_button.clicked.connect(self.open_class_query)
        self.web_button = QPushButton("打开教务网页"); self.web_button.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl(CLASS_PAGE_URL if self.tabs.currentWidget() is self.class_page else PAGE_URL)))
        self.login_button.hide()
        for button in (self.import_button, self.refresh_button, self.class_button, self.web_button): actions.addWidget(button)
        self.session_toggle = QToolButton(); self.session_toggle.setText("会话设置")
        self.session_toggle.setCheckable(True); actions.addWidget(self.session_toggle)
        actions.addStretch(); layout.addLayout(actions)
        selectors = QHBoxLayout()
        self.year = QSpinBox(); self.year.setRange(2000, 2100)
        today = date.today(); self.year.setValue(int(self.config.get("year", today.year if today.month >= 8 else today.year - 1)))
        self.year.setSuffix(" 学年起始年")
        self.term = QComboBox()
        for value, label in TERMS.items(): self.term.addItem(label, value)
        self.term.setCurrentIndex(max(0, self.term.findData(self.config.get("term", "3" if today.month >= 8 else "12"))))
        self.week = QComboBox(); self.week.addItem("全学期", 0)
        self.week.currentIndexChanged.connect(self._week_changed)
        self.previous = QPushButton("上一周"); self.previous.clicked.connect(lambda: self.step_week(-1))
        self.next = QPushButton("下一周"); self.next.clicked.connect(lambda: self.step_week(1))
        self.current = QPushButton("本周"); self.current.clicked.connect(self.current_week)
        selectors.addWidget(self.year); selectors.addWidget(self.term); selectors.addStretch()
        for widget in (self.previous, self.week, self.next, self.current): selectors.addWidget(widget)
        self.selectors_row = QWidget(); selectors.setContentsMargins(0, 0, 0, 0)
        self.selectors_row.setLayout(selectors); layout.addWidget(self.selectors_row)
        date_row = QHBoxLayout()
        self.use_start = QCheckBox("设置第一周周一")
        self.start = QDateEdit(); self.start.setCalendarPopup(True); self.start.setDisplayFormat("yyyy-MM-dd")
        saved = QDate.fromString(self.config.get("start_date", ""), "yyyy-MM-dd")
        self.start.setDate(saved if saved.isValid() else QDate.currentDate().addDays(1 - QDate.currentDate().dayOfWeek()))
        self.use_start.setChecked(saved.isValid()); self.start.setEnabled(saved.isValid())
        self.start_note = QLabel("设置开学日期后可定位本周与今日课程。"); self.start_note.setObjectName("muted")
        self.start_note.setWordWrap(True)
        self.save_button = QPushButton("保存设置"); self.save_button.clicked.connect(self.save)
        self.use_start.toggled.connect(self.start.setEnabled)
        self.use_start.toggled.connect(self.render); self.start.dateChanged.connect(self.render)
        for widget in (self.use_start, self.start, self.start_note, self.save_button): date_row.addWidget(widget)
        date_row.addStretch(); date_row.setContentsMargins(0, 0, 0, 0)
        self.date_row = QWidget(); self.date_row.setLayout(date_row); layout.addWidget(self.date_row)
        self.status = QLabel("点击「导入课表」选择表格、教务系统登录或 HAR / JSON 导入；已连接会话可在线刷新。")
        self.status.setWordWrap(True); self.status.setTextFormat(Qt.TextFormat.PlainText); layout.addWidget(self.status)
        self.today_label = QLabel(); self.today_label.setWordWrap(True); self.today_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.today_label)
        self.tabs = QTabWidget(); layout.addWidget(self.tabs, 1)
        self.grid = QTableWidget(12, 7); self.grid.setHorizontalHeaderLabels(WEEKDAYS)
        self.grid.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.grid.verticalHeader().setDefaultSectionSize(66)
        self.grid.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.grid.setWordWrap(True); self.grid.cellDoubleClicked.connect(self.details)
        self.tabs.addTab(self.grid, "周课表")
        self.list = QTableWidget(0, 8)
        self.list.setHorizontalHeaderLabels(["课程", "星期", "节次", "周次", "教师", "教室", "校区", "学分"])
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.list.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.list.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tabs.addTab(self.list, "课程列表")
        self.class_page = ClassTimetablePage(self)
        self.tabs.addTab(self.class_page, "班级课表")
        self.session_box = QGroupBox("会话设置（可选）"); form = QFormLayout(self.session_box)
        self.cookie = QLineEdit(); self.cookie.setEchoMode(QLineEdit.EchoMode.Password)
        self.cookie.setPlaceholderText("也可粘贴当前学校教务系统的 Cookie 请求头")
        self.remember = QCheckBox("使用 Windows 本机加密保存会话")
        self.remember.setChecked(bool(self.config.get("session_blob")))
        form.addRow("教务会话", self.cookie); form.addRow("", self.remember)
        note = QLabel("粘贴的会话仅用于学校课表接口。默认只在本次运行保留；会话过期时在系统浏览器重新登录。")
        note.setWordWrap(True); note.setObjectName("muted"); form.addRow(note)
        layout.addWidget(self.session_box)
        self.session_box.hide()
        self.session_toggle.toggled.connect(self.session_box.setVisible)
        self.tabs.currentChanged.connect(self._tab_changed)

    def _tab_changed(self, _index):
        is_class = self.tabs.currentWidget() is self.class_page
        for widget in (self.import_button, self.login_button, self.refresh_button, self.session_toggle,
                       self.selectors_row, self.date_row, self.status, self.today_label): widget.setVisible(not is_class)
        self.session_box.setVisible(not is_class and self.session_toggle.isChecked())
        self.login_button.hide()

    def open_class_query(self):
        self.tabs.setCurrentWidget(self.class_page)
        self.class_page.open_query()

    def _config(self):
        start = self.start.date()
        if self.use_start.isChecked() and start.dayOfWeek() != 1:
            raise ValueError("请将第一周起始日期设置为周一。")
        return {"year": self.year.value(), "term": self.term.currentData(),
                "selected_week": self.week.currentData() or 0,
                "start_date": start.toString("yyyy-MM-dd") if self.use_start.isChecked() else "",
                "session_blob": protect_password(normalize_cookie(self.cookie.text()))
                if self.remember.isChecked() and self.cookie.text().strip() else ""}

    def _save_config(self):
        config = self._config(); previous = self.settings.get("school_timetable")
        self.settings["school_timetable"] = config
        try:
            self.save_settings(self.settings)
        except OSError:
            if previous is None: self.settings.pop("school_timetable", None)
            else: self.settings["school_timetable"] = previous
            raise RuntimeError("课表设置保存失败，请检查配置目录。")
        self.config = config

    def _week_changed(self, *_):
        self.render()
        previous = dict(self.settings.get("school_timetable", {}))
        config = {**previous, "selected_week": self.week.currentData() or 0}
        self.settings["school_timetable"] = config
        try:
            self.save_settings(self.settings)
        except OSError:
            self.settings["school_timetable"] = previous
            self.status.setText("周次保存失败，请检查配置目录后重试。")
            return
        self.config["selected_week"] = config["selected_week"]

    def save(self):
        try:
            self._save_config(); self.status.setText("课表设置已保存。")
            self.render()
        except (ValueError, RuntimeError) as error:
            self.status.setText(str(error))

    def choose_har(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入学校课表 HAR", str(Path.home() / "Downloads"), "浏览器网络记录 (*.har)")
        if path: self.load_har(path)

    def choose_import(self):
        dialog = QDialog(self); dialog.setWindowTitle("导入学校课表"); dialog.resize(560, 290)
        layout = QVBoxLayout(dialog)
        note = QLabel("选择导入方式。表格按模板填写，每行一条课程安排；学年、学期使用主界面当前选择。")
        note.setWordWrap(True); layout.addWidget(note)
        for label, action in (("表格导入（XLSX / CSV / TSV）", self.choose_table),
                              ("教务系统登录导入（自动填入 Cookie）", self.login),
                              ("HAR / JSON 文件导入", self.choose_file)):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, callback=action: (dialog.accept(), callback()))
            layout.addWidget(button)
        template = QPushButton("下载表格模板"); template.clicked.connect(self.save_table_template); layout.addWidget(template)
        dialog.exec()

    def save_table_template(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存课表模板", "课表模板.csv", "CSV (*.csv)")
        if path:
            try:
                Path(path).write_text("课程名称,星期,节次,周次,教师,教室,校区,学分\n高等数学,周一,1-2,1-16周,张老师,A201,广州,3\n", encoding='utf-8-sig')
            except OSError as error: self.status.setText(str(error))

    def choose_table(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入课表表格", str(Path.home() / "Downloads"), "课表表格 (*.xlsx *.csv *.tsv)")
        if path:
            year, term = str(self.year.value()), self.term.currentData()
            def operation(cancelled):
                file = Path(path)
                if file.stat().st_size > 24 * 1024 * 1024: raise ValueError("请选择小于 24 MB 的表格。")
                content = base64.b64encode(file.read_bytes()).decode() if file.suffix.lower() == '.xlsx' else file.read_text(encoding='utf-8-sig')
                return import_table(content, file.name, year, term)
            self._start(operation, "正在导入表格…", importing=True)

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入 HAR / JSON", str(Path.home() / "Downloads"), "课表文件 (*.har *.json)")
        if path:
            def operation(cancelled):
                if Path(path).stat().st_size > 24 * 1024 * 1024: raise ValueError("请选择小于 24 MB 的文件。")
                value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
                return import_har(path) if isinstance(value, dict) and 'log' in value else validate_cache(value)
            self._start(operation, "正在导入课表…", importing=True)

    def load_har(self, path):
        self._start(lambda cancelled: import_har(path), "正在导入课表…", importing=True)

    def login(self):
        if self.worker or self.stopping: return
        if self.login_dialog:
            self.login_dialog.raise_(); self.login_dialog.activateWindow(); return
        dialog = SchoolLoginDialog(self)
        dialog.cookie.setText(self.cookie.text())
        self.login_dialog = dialog
        dialog.finished.connect(self._login_finished)
        dialog.open()
        dialog.open_login()

    def _login_finished(self, result):
        dialog = self.login_dialog; self.login_dialog = None
        if result == QDialog.DialogCode.Accepted and not self.stopping:
            self.cookie.setText(dialog.cookie_header)
            self.refresh()
        dialog.deleteLater()

    def refresh(self):
        if self.worker or self.stopping: return
        try:
            cookie = normalize_cookie(self.cookie.text())
            year, term = str(self.year.value()), self.term.currentData()
        except ValueError as error:
            self.status.setText(str(error) + " 请完成教务登录后读取。")
            self.login()
            return
        self._start(lambda cancelled: fetch_schedule(cookie, year, term, cancelled), "正在从教务系统读取课表…")

    def _start(self, operation, message, importing=False):
        if self.worker or self.stopping: return
        self.importing = importing
        self.worker = TimetableWorker(operation, self)
        self._busy(True); self.status.setText(message)
        self.worker.finished.connect(self._finished); self.worker.start()

    def _busy(self, busy):
        for widget in (self.import_button, self.login_button, self.refresh_button, self.save_button,
                       self.year, self.term, self.cookie, self.remember, self.start, self.use_start):
            widget.setEnabled(not busy)
        if not busy: self.start.setEnabled(self.use_start.isChecked())

    def _finished(self):
        worker = self.worker; self.worker = None; self._busy(False)
        if not self.stopping:
            if worker.error:
                self.status.setText(worker.error + " 已有本地课表仍可查看。")
            elif worker.result:
                try:
                    value = validate_cache(worker.result)
                    self.save_cache(value)
                    # A different semester needs an independently verified first-week date.
                    if self.schedule and (self.schedule["year"], self.schedule["term"]) != (value["year"], value["term"]):
                        self.use_start.setChecked(False)
                    if self.importing:
                        self.cookie.clear(); self.remember.setChecked(False)
                    self._display(value)
                    self._save_config()
                    if worker.result.get("warning"): self.status.setText(worker.result["warning"])
                except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
                    self.status.setText(str(error) if isinstance(error, (ValueError, RuntimeError))
                                        else "课表保存失败，请检查配置目录后重试。")
        worker.deleteLater()

    def _display(self, schedule):
        previous_week = self.config.get("selected_week", self.week.currentData() or 0)
        self.schedule = schedule
        self.year.setValue(int(schedule["year"]))
        self.term.setCurrentIndex(self.term.findData(schedule["term"]))
        maximum = max((max(c["weeks"]) for c in schedule["courses"]), default=20)
        self.week.blockSignals(True); self.week.clear(); self.week.addItem("全学期", 0)
        for n in range(1, max(20, maximum) + 1): self.week.addItem(f"第 {n} 周", n)
        self.week.setCurrentIndex(max(0, self.week.findData(previous_week or 0))); self.week.blockSignals(False)
        self.status.setText(f'{schedule["year"]}–{int(schedule["year"]) + 1} 学年 {TERMS[schedule["term"]]} · '
                            f'{len(schedule["courses"])} 条安排 · 更新于 {schedule["updated_at"].replace("T", " ")} · 双击课程查看详情')
        self.render()

    def _start_date(self):
        if not self.use_start.isChecked() or self.start.date().dayOfWeek() != 1: return None
        return self.start.date().toPython()

    def apply_cloud(self, value):
        """Apply a cloud snapshot without replacing local school login cookies."""
        monday = value.get("first_monday", "")
        self.config["start_date"] = monday
        if "selected_week" in value:
            self.config["selected_week"] = value["selected_week"]
        self.use_start.setChecked(bool(monday))
        if monday: self.start.setDate(QDate.fromString(monday, "yyyy-MM-dd"))
        if value.get("schedule") is not None:
            schedule = value["schedule"]
            self.config.update(year=int(schedule["year"]), term=schedule["term"])
            self.settings["school_timetable"] = self.config
            self._display(schedule)
        else:
            self.schedule = None
            self.grid.clearContents(); self.list.setRowCount(0)
            self.status.setText("账号中尚未保存课表，可导入 HAR 或在线读取。")
            self.render()

    def step_week(self, step):
        n = self.week.currentData() or 1
        index = self.week.findData(max(1, n + step))
        if index >= 0: self.week.setCurrentIndex(index)

    def current_week(self):
        start = self._start_date()
        if not start:
            self.status.setText("请先设置第一周的周一日期，再定位本周。")
            return
        index = self.week.findData((date.today() - start).days // 7 + 1)
        if index < 1:
            self.status.setText("今天在课表周次范围外，可手动选择周次查看。")
            return
        self.week.setCurrentIndex(index)

    def render(self, *_):
        if not self.schedule: return
        courses = self.schedule["courses"]; week = self.week.currentData() or 0
        selected = [i for i, c in enumerate(courses) if not week or week in c["weeks"]]
        slots = max(12, max((max(c["periods"]) for c in courses), default=12))
        self.grid.clearSpans(); self.grid.clearContents(); self.grid.setRowCount(slots)
        times = self.schedule.get("period_times", {})
        self.grid.setVerticalHeaderLabels([f'第 {n} 节\n{times[str(n)]["start"]}–{times[str(n)]["end"]}'
            if str(n) in times else f"第 {n} 节" for n in range(1, slots + 1)])
        start = self._start_date()
        monday = start + timedelta(weeks=week - 1) if start and week else None
        self.grid.setHorizontalHeaderLabels([label + (f"\n{monday + timedelta(days=i):%m/%d}" if monday else "")
                                             for i, label in enumerate(WEEKDAYS)])
        cells = [[[] for _ in range(7)] for _ in range(slots)]
        for i in selected:
            c = courses[i]
            for period in c["periods"]: cells[period - 1][c["weekday"] - 1].append(i)
        palette = ["#2563eb", "#7c3aed", "#047857", "#b45309", "#be185d", "#0e7490"]
        for column in range(7):
            row = 0
            while row < slots:
                ids = cells[row][column]; end = row + 1
                if not ids: row += 1; continue
                while end < slots and cells[end][column] == ids: end += 1
                texts = [f'{courses[i]["name"]}\n{courses[i]["location"] or "教室待定"} · {courses[i]["teacher"]}\n{courses[i]["week_text"]}' for i in ids]
                item = QTableWidgetItem("\n\n".join(texts)); item.setData(Qt.ItemDataRole.UserRole, ids)
                item.setToolTip("\n\n".join(texts)); item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                colour = int(hashlib.sha256(courses[ids[0]]["name"].encode()).hexdigest()[:4], 16) % len(palette)
                item.setBackground(QColor(palette[colour])); item.setForeground(QColor("#ffffff"))
                self.grid.setItem(row, column, item)
                if end - row > 1: self.grid.setSpan(row, column, end - row, 1)
                row = end
        self.list.setRowCount(len(selected))
        for row, i in enumerate(sorted(selected, key=lambda i: (courses[i]["weekday"], min(courses[i]["periods"])))):
            c = courses[i]
            values = [c["name"], WEEKDAYS[c["weekday"] - 1], ",".join(map(str, c["periods"])), c["week_text"],
                      c["teacher"], c["location"], c["campus"], c["credits"]]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text); item.setToolTip(text); self.list.setItem(row, col, item)
        self.previous.setEnabled(week > 1); self.next.setEnabled(week < self.week.count() - 1)
        self.current.setEnabled(bool(start))
        self.start_note.setText("起始日期应为周一。" if self.use_start.isChecked() and not start
                                else "日期仅用于定位本周，不改变学校周次。")
        self._update_today()

    def _update_today(self):
        if not self.schedule: return
        start = self._start_date()
        if not start:
            self.today_label.setText("设置第一周周一后，这里显示今日课程。")
            return
        today = date.today(); week = (today - start).days // 7 + 1
        courses = sorted((c for c in self.schedule["courses"] if week in c["weeks"] and c["weekday"] == today.isoweekday()),
                         key=lambda c: min(c["periods"]))
        if not 1 <= week < self.week.count():
            self.today_label.setText(f"今日 {today:%m/%d} · 在课表周次范围外")
        else:
            self.today_label.setText(f"今日 {today:%m/%d} · 第 {week} 周 · " + ("；".join(
                f'{c["name"]}（{min(c["periods"])}–{max(c["periods"])}节，{c["location"] or "教室待定"}）'
                for c in courses) or "今天没有课程"))

    def details(self, row, col):
        item = self.grid.item(row, col)
        if not item: return
        texts = []
        for i in item.data(Qt.ItemDataRole.UserRole) or []:
            c = self.schedule["courses"][i]
            texts.append(f'{c["name"]}\n教师：{c["teacher"]}\n教室：{c["location"]}  {c["campus"]}\n'
                         f'{WEEKDAYS[c["weekday"] - 1]} · 第 {",".join(map(str, c["periods"]))} 节\n'
                         f'周次：{c["week_text"]}\n学分：{c["credits"]}\n教学班：{c["class_name"]}')
        box = QMessageBox(self); box.setWindowTitle("课程详情"); box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText("\n\n".join(texts)); box.exec()

    def stop(self):
        self.stopping = True; self.clock.stop()
        self.class_page.stop()
        if self.login_dialog: self.login_dialog.reject()
        if self.worker: self.worker.requestInterruption()
