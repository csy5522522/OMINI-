"""School class selectors and previews using the shared temporary login session."""
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QTabBar, QTabWidget, QVBoxLayout, QWidget
from school_timetable import CLASS_PAGE_URL, HOST, LOGIN_URL


class ClassTimetablePage(QWidget):
    def __init__(self, profile_factory, parent=None):
        super().__init__(parent)
        self.profile_factory = profile_factory
        self.profile = None
        self.main_view = None
        self.views = []
        self.login_attempted = False
        self.stopping = False
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 4, 0, 0)
        self.status = QLabel("按学年、学期、校区、年级、学院、专业和班级查询学校开放的班级课表。")
        self.status.setWordWrap(True); layout.addWidget(self.status)
        toolbar = QHBoxLayout()
        self.open_button = QPushButton("打开班级查询"); self.open_button.setObjectName("primary")
        self.open_button.clicked.connect(self.open_query)
        self.login_button = QPushButton("统一认证登录"); self.login_button.clicked.connect(self.login)
        self.back_button = QPushButton("返回查询"); self.back_button.clicked.connect(self.return_to_query)
        self.reload_button = QPushButton("刷新页面"); self.reload_button.clicked.connect(self.reload)
        for button in (self.open_button, self.login_button, self.back_button, self.reload_button): toolbar.addWidget(button)
        toolbar.addStretch(); layout.addLayout(toolbar)
        self.browser_tabs = QTabWidget(); self.browser_tabs.setTabsClosable(True)
        self.browser_tabs.tabCloseRequested.connect(self.close_preview)
        layout.addWidget(self.browser_tabs, 1)
        self.empty = QLabel("点击「打开班级查询」，登录后在学校页面选择班级并查询。\n点击查询结果中的课表名称打开预览；PDF 导出时可选择本地保存位置。")
        self.empty.setWordWrap(True); layout.addWidget(self.empty)
        self.back_button.setEnabled(False); self.reload_button.setEnabled(False)

    def _ensure_browser(self):
        if self.stopping: return False
        if self.main_view: return True
        try:
            self.profile = self.profile_factory()
            self.main_view = self._make_view("班级查询")
            self.browser_tabs.tabBar().setTabButton(0, QTabBar.ButtonPosition.RightSide, None)
            self.empty.hide()
            self.back_button.setEnabled(True); self.reload_button.setEnabled(True)
            return True
        except (ImportError, RuntimeError, ValueError):
            self.status.setText("班级查询浏览器初始化失败，请检查安装文件后重试。")
            return False

    def _make_view(self, title):
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
        from PySide6.QtWebEngineWidgets import QWebEngineView
        view = QWebEngineView(self.browser_tabs)
        page = QWebEnginePage(self.profile, view); view.setPage(page)
        for attribute in (QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows,
                          QWebEngineSettings.WebAttribute.PluginsEnabled, QWebEngineSettings.WebAttribute.PdfViewerEnabled):
            page.settings().setAttribute(attribute, True)
        page.newWindowRequested.connect(self._new_window)
        page.printRequested.connect(lambda: self.status.setText("请使用学校页面的 PDF 导出按钮下载课表。"))
        view.loadFinished.connect(lambda ok, target=view: self._loaded(target, ok))
        view.titleChanged.connect(lambda text, target=view: self._title_changed(target, text))
        self.views.append(view); self.browser_tabs.addTab(view, title)
        return view

    def _new_window(self, request):
        if self.stopping: return
        view = self._make_view("课表预览")
        self.browser_tabs.setCurrentWidget(view); request.openIn(view.page())

    def _title_changed(self, view, title):
        index = self.browser_tabs.indexOf(view)
        if index > 0 and title:
            self.browser_tabs.setTabText(index, title[:24]); self.browser_tabs.setTabToolTip(index, title)

    def _loaded(self, view, ok):
        if self.stopping: return
        if not ok:
            self.status.setText("学校页面加载失败，请检查网络后刷新。"); return
        if view is not self.main_view: return
        url = view.url()
        if url.host().lower() == "sso.hualixy.edu.cn":
            self.status.setText("请在下方统一认证页面完成登录，返回教务系统后自动打开班级查询。")
        elif url.host().lower() == HOST:
            if url.path().startswith("/jwglxt/xtgl/index_"):
                self.status.setText("已返回教务系统，正在打开班级查询…"); view.load(QUrl(CLASS_PAGE_URL))
            elif url.path() == "/jwglxt/xtgl/login_slogin.html":
                if not self.login_attempted:
                    self.login_attempted = True
                    self.status.setText("教务会话需要登录，正在打开学校统一认证…"); view.load(QUrl(LOGIN_URL))
                else:
                    self.status.setText("会话尚未生效，请点击「统一认证登录」完成学校登录。")
            elif url.path() == QUrl(CLASS_PAGE_URL).path():
                self.login_attempted = False
                self.status.setText("选择学校页面中的学年、学期和班级后点击「查询」，再点击课表名称预览；可使用学校 PDF 导出。")

    def open_query(self):
        if self._ensure_browser():
            self.browser_tabs.setCurrentWidget(self.main_view); self.main_view.load(QUrl(CLASS_PAGE_URL))

    def login(self):
        if self._ensure_browser():
            self.login_attempted = True
            self.browser_tabs.setCurrentWidget(self.main_view); self.main_view.load(QUrl(LOGIN_URL))

    def return_to_query(self):
        if not self.main_view: return
        self.browser_tabs.setCurrentWidget(self.main_view)
        if self.main_view.url().path() != QUrl(CLASS_PAGE_URL).path(): self.main_view.load(QUrl(CLASS_PAGE_URL))

    def reload(self):
        view = self.browser_tabs.currentWidget()
        if view: view.reload()

    def close_preview(self, index):
        if index <= 0: return
        view = self.browser_tabs.widget(index)
        self.browser_tabs.removeTab(index); self.views.remove(view)
        view.stop(); view.deleteLater()

    def stop(self):
        self.stopping = True
        for view in self.views: view.stop()
