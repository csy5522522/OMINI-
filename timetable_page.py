"""School timetable sidebar page with offline viewing and an isolated login browser."""
from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
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
    """Cookies are captured only from this explicit, off-the-record school session."""
    def __init__(self, parent=None, profile=None):
        super().__init__(parent)
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
        from PySide6.QtWebEngineWidgets import QWebEngineView
        self.setWindowTitle("学校统一身份认证 · 教务课表")
        self.resize(1000, 740)
        self.cookies = {}
        self.cookie_header = ""
        self._page_ready = False
        self._completed = False
        self._navigation = 0
        self._probing = False
        layout = QVBoxLayout(self)
        note = QLabel("在学校统一身份认证页面完成登录后，程序会自动进入课表、读取课程并更新本地课表。")
        note.setWordWrap(True); layout.addWidget(note)
        # View is destroyed before the profile; its page is owned by the view.
        self.web = QWebEngineView(self)
        self.profile = profile if profile is not None else QWebEngineProfile(self)
        self.web.setPage(QWebEnginePage(self.profile, self.web))
        store = self.profile.cookieStore()
        store.cookieAdded.connect(self._cookie_added)
        store.cookieRemoved.connect(self._cookie_removed)
        store.loadAllCookies()
        layout.addWidget(self.web, 1)
        self.message = QLabel("正在打开学校统一身份认证…"); self.message.setWordWrap(True); layout.addWidget(self.message)
        row = QHBoxLayout()
        retry = QPushButton("统一认证登录"); retry.clicked.connect(lambda: self.web.load(QUrl(LOGIN_URL)))
        timetable = QPushButton("进入课表"); timetable.clicked.connect(lambda: self.web.load(QUrl(PAGE_URL)))
        read = QPushButton("读取已登录课表"); read.setObjectName("primary"); read.clicked.connect(self._read)
        close = QPushButton("取消"); close.clicked.connect(self.reject)
        for button in (retry, timetable, read, close): row.addWidget(button)
        row.addStretch(); layout.addLayout(row)
        self.web.urlChanged.connect(self._url_changed)
        self.web.loadStarted.connect(lambda: self._url_changed(None))
        self.web.loadFinished.connect(self._load_finished)
        self.finished.connect(self._closed)
        self.web.load(QUrl(LOGIN_URL))

    def _closed(self, _result):
        self._completed = True

    def _url_changed(self, _url):
        self._navigation += 1
        self._page_ready = False
        self._probing = False

    def _is_timetable(self):
        url = self.web.url()
        return (url.scheme() == "https" and url.host().lower() == HOST
                and url.path() == QUrl(PAGE_URL).path())

    def _load_finished(self, ok):
        url = self.web.url()
        if not ok:
            self.message.setText("页面加载失败，请点击「统一认证登录」重试。")
        elif url.host().lower() == "sso.hualixy.edu.cn":
            self.message.setText("请在统一身份认证页面完成登录，学校会将会话返回教务系统。")
        elif url.host().lower() == HOST:
            if url.path().startswith("/jwglxt/xtgl/index_"):
                self.message.setText("已返回教务系统，正在进入课表…")
                self.web.load(QUrl(PAGE_URL))
            elif url.path() == "/jwglxt/xtgl/login_slogin.html":
                self.message.setText("当前是教务系统旧登录页，请点击「统一认证登录」使用学校统一入口。")
            elif self._is_timetable():
                self.message.setText("已进入课表，正在自动读取…")
                self._check_page_ready()
            else:
                self.message.setText("已返回教务系统，请点击「进入课表」，随后自动读取。")
        else:
            self.message.setText("请完成学校登录流程并返回教务系统。")

    @staticmethod
    def _key(cookie):
        return (cookie.domain().lstrip(".").lower(), cookie.path() or "/", bytes(cookie.name()))

    def _cookie_added(self, cookie):
        domain, path, name = self._key(cookie)
        if domain and (HOST == domain or HOST.endswith("." + domain)) and "/jwglxt/kbcx/".startswith(path):
            if cookie.isSessionCookie() or cookie.expirationDate().toSecsSinceEpoch() > QDateTime_now():
                self.cookies[(domain, path, name)] = bytes(cookie.value())
                self._maybe_complete()

    def _cookie_removed(self, cookie):
        self.cookies.pop(self._key(cookie), None)

    def _read(self):
        if not self._is_timetable():
            self.message.setText("请先完成统一认证登录并进入学校课表页面。")
            return
        self._check_page_ready()

    def _check_page_ready(self):
        if self._completed or self._probing or not self._is_timetable():
            return
        self._probing = True
        navigation = self._navigation
        # The genuine timetable includes both semester selectors; an HTTP-200
        # login form can otherwise masquerade as a successfully loaded page.
        self.web.page().runJavaScript(
            'Boolean(document.querySelector("select#xnm") && document.querySelector("select#xqm"))',
            lambda ready: self._page_checked(navigation, ready))

    def _page_checked(self, navigation, ready):
        if self._completed or navigation != self._navigation or not self._is_timetable():
            return
        self._probing = False
        self._page_ready = ready is True
        if not self._page_ready:
            self.message.setText("尚未加载到学生课表，请完成统一认证后点击「进入课表」。")
            return
        self._maybe_complete()

    def _maybe_complete(self):
        if self._completed or not self._page_ready or not self._is_timetable():
            return
        try:
            # Longer paths must precede less specific cookies with the same name.
            pairs = sorted(self.cookies.items(), key=lambda pair: -len(pair[0][1]))
            self.cookie_header = normalize_cookie("; ".join(
                name.decode("ascii") + "=" + value.decode("ascii") for (_, _, name), value in pairs))
        except (ValueError, UnicodeError):
            self.message.setText("课表已打开，正在等待教务登录会话…")
            return
        self._completed = True
        self.message.setText("登录成功，正在抓取并更新课表…")
        self.accept()


def QDateTime_now():
    from PySide6.QtCore import QDateTime
    return QDateTime.currentSecsSinceEpoch()


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
        self._browser_profile = None
        self._browser_cookies = {}
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
        self.import_button = QPushButton("导入 HAR"); self.import_button.clicked.connect(self.choose_har)
        self.login_button = QPushButton("登录并读取"); self.login_button.setObjectName("primary")
        self.login_button.clicked.connect(self.login)
        self.refresh_button = QPushButton("在线刷新"); self.refresh_button.clicked.connect(self.refresh)
        self.class_button = QPushButton("查询班级课表"); self.class_button.clicked.connect(self.open_class_query)
        self.web_button = QPushButton("打开教务网页"); self.web_button.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl(CLASS_PAGE_URL if self.tabs.currentWidget() is self.class_page else PAGE_URL)))
        for button in (self.import_button, self.login_button, self.refresh_button, self.class_button, self.web_button): actions.addWidget(button)
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
        self.week.currentIndexChanged.connect(self.render)
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
        self.status = QLabel("点击「导入 HAR」读取已导出的课表，或点击「登录并读取」获取课表。")
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
        self.class_page = ClassTimetablePage(self._ensure_browser_profile, self)
        self.tabs.addTab(self.class_page, "班级课表")
        self.session_box = QGroupBox("会话设置（可选）"); form = QFormLayout(self.session_box)
        self.cookie = QLineEdit(); self.cookie.setEchoMode(QLineEdit.EchoMode.Password)
        self.cookie.setPlaceholderText("也可粘贴当前学校教务系统的 Cookie 请求头")
        self.remember = QCheckBox("使用 Windows 本机加密保存会话")
        self.remember.setChecked(bool(self.config.get("session_blob")))
        form.addRow("教务会话", self.cookie); form.addRow("", self.remember)
        note = QLabel("登录并读取后会话仅用于学校课表接口。默认只在本次运行保留；会话过期时重新登录。")
        note.setWordWrap(True); note.setObjectName("muted"); form.addRow(note)
        layout.addWidget(self.session_box)
        self.session_box.hide()
        self.session_toggle.toggled.connect(self.session_box.setVisible)
        self.tabs.currentChanged.connect(self._tab_changed)

    def _ensure_browser_profile(self):
        if self._browser_profile is None:
            from PySide6.QtWebEngineCore import QWebEngineProfile
            from PySide6.QtNetwork import QNetworkCookie
            self._browser_profile = QWebEngineProfile()
            # Delete the profile on the next event loop, after its child pages are gone.
            self.destroyed.connect(self._browser_profile.deleteLater)
            self._browser_profile.downloadRequested.connect(self._download_requested)
            store = self._browser_profile.cookieStore()
            store.cookieAdded.connect(self._browser_cookie_added)
            store.cookieRemoved.connect(self._browser_cookie_removed)
            try:
                header = normalize_cookie(self.cookie.text()) if self.cookie.text().strip() else ""
            except ValueError:
                header = ""
            if header:
                for part in header.split(";"):
                    name, value = part.strip().split("=", 1)
                    cookie = QNetworkCookie(name.encode("ascii"), value.encode("ascii"))
                    cookie.setDomain(HOST); cookie.setPath("/jwglxt"); cookie.setSecure(True)
                    self._browser_profile.cookieStore().setCookie(cookie, QUrl("https://" + HOST))
        return self._browser_profile

    def _browser_cookie_added(self, cookie):
        domain, path, name = SchoolLoginDialog._key(cookie)
        if domain and (HOST == domain or HOST.endswith("." + domain)) and "/jwglxt/kbcx/".startswith(path):
            if cookie.isSessionCookie() or cookie.expirationDate().toSecsSinceEpoch() > QDateTime_now():
                self._browser_cookies[(domain, path, name)] = bytes(cookie.value())
                self._sync_browser_cookie()

    def _browser_cookie_removed(self, cookie):
        self._browser_cookies.pop(SchoolLoginDialog._key(cookie), None)
        self._sync_browser_cookie()

    def _sync_browser_cookie(self):
        try:
            pairs = sorted(self._browser_cookies.items(), key=lambda pair: -len(pair[0][1]))
            self.cookie.setText("; ".join(name.decode("ascii") + "=" + value.decode("ascii")
                                          for (_, _, name), value in pairs))
        except UnicodeError:
            pass

    def _download_requested(self, download):
        if self.stopping: download.cancel(); return
        name = Path(download.suggestedFileName() or "班级课表.pdf").name
        target, _ = QFileDialog.getSaveFileName(self, "保存学校课表导出文件", str(Path.home() / "Downloads" / name), "全部文件 (*)")
        if not target: download.cancel(); return
        destination = Path(target)
        download.setDownloadDirectory(str(destination.parent)); download.setDownloadFileName(destination.name)
        download.accept()

    def _tab_changed(self, _index):
        is_class = self.tabs.currentWidget() is self.class_page
        for widget in (self.import_button, self.login_button, self.refresh_button, self.session_toggle,
                       self.selectors_row, self.date_row, self.status, self.today_label): widget.setVisible(not is_class)
        self.session_box.setVisible(not is_class and self.session_toggle.isChecked())

    def open_class_query(self):
        self.tabs.setCurrentWidget(self.class_page)
        if not self.class_page.main_view: self.class_page.open_query()

    def _config(self):
        start = self.start.date()
        if self.use_start.isChecked() and start.dayOfWeek() != 1:
            raise ValueError("请将第一周起始日期设置为周一。")
        return {"year": self.year.value(), "term": self.term.currentData(),
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

    def save(self):
        try:
            self._save_config(); self.status.setText("课表设置已保存。")
            self.render()
        except (ValueError, RuntimeError) as error:
            self.status.setText(str(error))

    def choose_har(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入学校课表 HAR", str(Path.home() / "Downloads"), "浏览器网络记录 (*.har)")
        if path: self.load_har(path)

    def load_har(self, path):
        self._start(lambda cancelled: import_har(path), "正在导入课表…", importing=True)

    def login(self):
        if self.worker or self.stopping: return
        try:
            dialog = SchoolLoginDialog(self, profile=self._ensure_browser_profile())
        except (ImportError, ValueError, RuntimeError):
            self.status.setText("登录窗口组件缺失，请安装完整依赖或使用 HAR 导入。")
            return
        self.login_dialog = dialog
        dialog.finished.connect(self._login_finished)
        dialog.open()

    def _login_finished(self, result):
        dialog = self.login_dialog; self.login_dialog = None
        if result == QDialog.DialogCode.Accepted:
            self.cookie.setText(dialog.cookie_header)
            self.refresh()
        dialog.web.stop()
        dialog.deleteLater()

    def refresh(self):
        if self.worker or self.stopping: return
        try:
            cookie = normalize_cookie(self.cookie.text())
            year, term = str(self.year.value()), self.term.currentData()
        except ValueError as error:
            self.status.setText(str(error) + " 点击「登录并读取」可自动获取会话。")
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
        previous_week = self.week.currentData()
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
