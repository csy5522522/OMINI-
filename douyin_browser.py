"""Import public Douyin image URLs after viewing them in the system browser."""
import json
import re
from urllib.parse import urlparse

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout

DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

def media_info(source, data):
    """Validate browser results before using them as downloader input."""
    match = re.fullmatch(r"https://(?:www\.)?douyin\.com/note/(\d+)(?:\?.*)?", data.get("url", ""))
    if not match:
        raise ValueError("解析结果不属于抖音图文页面")
    post_id = match.group(1)
    source_id = re.search(r"/(?:share/)?note/(\d+)", source)
    if source_id and source_id.group(1) != post_id:
        raise ValueError("页面作品与本次下载任务不一致")
    title = (data.get("title") or f"抖音图文 {post_id}")[:100]
    entries = []
    for index, url in enumerate(dict.fromkeys(data.get("images") or []), 1):
        parsed = urlparse(url)
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".douyinpic.com"):
            raise ValueError("图文图片地址不是抖音公开媒体域名")
        entries.append({"id": f"{post_id}-{index:03}", "title": f"{title} [{post_id}] - 图片{index:03}",
                        "formats": [{"url": url, "ext": "jpg", "format_id": "image"}],
                        "extractor": "douyin:browser", "extractor_key": "OminiDouyinNote", "webpage_url": source,
                        "http_headers": {"User-Agent": DESKTOP_UA, "Referer": data["url"]}})
    if not entries:
        raise ValueError("页面尚未加载图文图片，请完成页面验证后点击重新解析")
    return {"_type": "playlist", "id": post_id, "title": title, "entries": entries, "webpage_url": source,
            "extractor": "douyin:browser", "extractor_key": "OminiDouyinNote"}


class DouyinBrowserDialog(QDialog):
    media_ready = Signal(object)

    def __init__(self, source, parent=None):
        super().__init__(parent)
        self.source = source
        self.setWindowTitle("抖音图文 · 导入图片地址")
        self.resize(760, 500)
        layout = QVBoxLayout(self)
        note = QLabel("在系统浏览器打开作品。若普通下载解析失败，可右键复制公开图片地址，每行粘贴一张，再加入下载队列。")
        note.setWordWrap(True); layout.addWidget(note)
        self.page_url = QLineEdit(source)
        self.page_url.setPlaceholderText("完整作品链接：https://www.douyin.com/note/作品编号")
        layout.addWidget(self.page_url)
        self.images = QPlainTextEdit()
        self.images.setPlaceholderText("图片地址，每行一个 https://…douyinpic.com/…")
        layout.addWidget(self.images, 1)
        self.hint = QLabel("需要完整的 note 作品链接；短分享链接可在浏览器打开后复制地址栏。")
        self.hint.setTextFormat(Qt.TextFormat.PlainText); self.hint.setWordWrap(True); layout.addWidget(self.hint)
        row = QHBoxLayout()
        browser = QPushButton("打开系统浏览器"); browser.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(source)))
        read = QPushButton("加入图片下载"); read.setObjectName("primary"); read.clicked.connect(self.read_media)
        close = QPushButton("关闭"); close.clicked.connect(self.reject)
        row.addWidget(browser); row.addStretch(); row.addWidget(read); row.addWidget(close); layout.addLayout(row)

    def read_media(self):
        try:
            info = media_info(self.source, {"url": self.page_url.text().strip(),
                                           "images": [line.strip() for line in self.images.toPlainText().splitlines() if line.strip()]})
        except (ValueError, TypeError, AttributeError) as error:
            self.hint.setText(str(error)); return
        self.media_ready.emit(info)
        self.accept()
