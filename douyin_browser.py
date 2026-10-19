"""Read public media from a temporary Douyin browser, without exporting cookies."""
import json
import re
from urllib.parse import urlparse

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout

DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

MEDIA_SCRIPT = r"""(() => {
  if (!/^https:\/\/(?:www\.)?douyin\.com\/note\/\d+/.test(location.href)) return '';
  const images = [...new Set([...document.querySelectorAll('main img')]
    .filter(i => i.naturalWidth > 200 && i.naturalHeight > 200 && /[?&]biz_tag=aweme_images(?:&|$)/.test(i.currentSrc || i.src))
    .map(i => i.currentSrc || i.src))];
  return JSON.stringify({url:location.href,title:document.title.replace(/\s*-\s*抖音$/, ''),images});
})()"""


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
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
        from PySide6.QtWebEngineWidgets import QWebEngineView
        self.source = source
        self.finished_reading = False
        self.closing = False
        self.pending = False
        self.setWindowTitle("抖音图文解析 · 临时浏览器")
        self.resize(1100, 780)
        layout = QVBoxLayout(self)
        self.hint = QLabel("正在读取图文图片。若网站显示登录或验证，请自行在页面完成，再点击重新解析。不会读取 Chrome/Edge 登录数据库。")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.web = QWebEngineView(self)
        self.profile = QWebEngineProfile(self)  # Unnamed/off-the-record, memory-only cookies.
        self.profile.setHttpUserAgent(DESKTOP_UA)
        self.web.setPage(QWebEnginePage(self.profile, self.web))
        layout.addWidget(self.web, 1)
        button = QPushButton("重新解析已加载图片")
        button.clicked.connect(self.read_media)
        layout.addWidget(button)
        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self.read_media)
        self.timer.start()
        self.attempts = 0
        self.web.load(QUrl(source))

    def read_media(self):
        if self.closing or self.pending or self.finished_reading:
            return
        self.pending = True
        self.web.page().runJavaScript(MEDIA_SCRIPT, self._received)

    def _received(self, result):
        self.pending = False
        if self.closing or self.finished_reading or not self.isVisible():
            return
        self.attempts += 1
        try:
            info = media_info(self.source, json.loads(result or "{}"))
        except (ValueError, TypeError, AttributeError):
            if self.attempts >= 30:
                self.timer.stop()
                self.hint.setText("页面未返回图文图片。请在页面完成登录或验证，确认能看到作品后点击重新解析；关闭窗口保留失败任务供重试。")
            return
        # Wait until the image list is stable, to avoid accepting the first image
        # while the rest of a carousel is still being rendered.
        signature = tuple(entry["formats"][0]["url"] for entry in info["entries"])
        if signature != getattr(self, "last_signature", None):
            self.last_signature = signature
            return
        self.finished_reading = True
        self.timer.stop()
        self.media_ready.emit(info)
        self.accept()

    def done(self, result):
        self.closing = True  # JavaScript callbacks may arrive after Qt deletes the dialog.
        self.timer.stop()
        self.web.stop()
        super().done(result)
