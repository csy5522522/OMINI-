"""Class timetable queries open in the installed system browser."""
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from school_timetable import CLASS_PAGE_URL, LOGIN_URL


class ClassTimetablePage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.stopping = False
        layout = QVBoxLayout(self); layout.setContentsMargins(12, 16, 12, 12)
        title = QLabel("班级课表 · 使用系统浏览器"); title.setObjectName("sectionTitle"); layout.addWidget(title)
        note = QLabel("在学校页面选择学年、学期、校区、年级、学院、专业和班级后查询。\n\n"
                      "请先完成统一认证，再打开班级查询。课表预览、PDF 导出和打印均在系统浏览器中进行。\n\n"
                      "个人课表可通过「导入 HAR」保存到工具箱，离线查看每周课程。")
        note.setWordWrap(True); layout.addWidget(note)
        row = QHBoxLayout()
        self.login_button = QPushButton("统一认证登录"); self.login_button.clicked.connect(self.login)
        self.open_button = QPushButton("打开班级查询"); self.open_button.setObjectName("primary")
        self.open_button.clicked.connect(self.open_query)
        row.addWidget(self.login_button); row.addWidget(self.open_button); row.addStretch(); layout.addLayout(row)
        self.status = QLabel("网页使用默认浏览器打开。")
        self.status.setWordWrap(True); self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status); layout.addStretch()

    def open_url(self, url):
        if self.stopping: return
        if QDesktopServices.openUrl(QUrl(url)):
            self.status.setText("已交给系统浏览器，查询结果与 PDF 可在浏览器中查看或保存。")
        else:
            self.status.setText("系统浏览器启动失败，请手动打开：" + url)

    def open_query(self):
        self.open_url(CLASS_PAGE_URL)

    def login(self):
        self.open_url(LOGIN_URL)

    def stop(self):
        self.stopping = True
