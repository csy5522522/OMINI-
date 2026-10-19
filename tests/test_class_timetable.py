"""Shared login, class routing, preview ownership and actual local PDF downloads."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QUrl
from PySide6.QtNetwork import QNetworkCookie
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import app
from school_timetable import CLASS_PAGE_URL, HOST, LOGIN_URL, parse_schedule
from timetable_page import TimetablePage
from tests.test_timetable import payload


class ClassTimetableTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache = Path(self.temp.name) / "cache.json"
        self.cache.write_text(json.dumps(parse_schedule(payload(), "2026", "3")))
        self.original = self.cache.read_bytes()
        self.page = TimetablePage({}, lambda _: None, self.cache, lambda v: self.cache.write_text(json.dumps(v)))
        self.page.resize(1040, 800); self.page.show()
        self.class_page = self.page.class_page

    def tearDown(self):
        self.page.stop(); self.page.close(); self.page.deleteLater()
        QTest.qWait(30); self.temp.cleanup()

    def until(self, predicate):
        for _ in range(500):
            if predicate(): return
            QTest.qWait(10)
        self.assertTrue(predicate())

    def test_shared_login_and_independent_class_tab(self):
        self.assertTrue(self.class_page._ensure_browser())
        with patch("timetable_page.LOGIN_URL", "about:blank"):
            self.page.login()
        dialog = self.page.login_dialog
        self.assertIs(dialog.profile, self.class_page.profile)
        self.assertTrue(dialog.profile.isOffTheRecord())
        cookie = QNetworkCookie(b"JSESSIONID", b"SHARED_TOKEN"); cookie.setDomain(HOST); cookie.setPath("/jwglxt")
        self.class_page.profile.cookieStore().setCookie(cookie, QUrl("https://" + HOST))
        self.until(lambda: self.page.cookie.text() == "JSESSIONID=SHARED_TOKEN" and bool(dialog.cookies))
        dialog.reject(); QTest.qWait(20)
        self.assertIs(self.class_page.main_view.page().profile(), self.class_page.profile)
        self.page.tabs.setCurrentWidget(self.class_page)
        self.assertFalse(self.page.selectors_row.isVisible())
        self.assertFalse(self.page.import_button.isVisible())
        self.assertTrue(self.class_page.isVisible())
        self.page.tabs.setCurrentWidget(self.page.grid)
        self.assertTrue(self.page.selectors_row.isVisible())
        self.assertEqual(self.page.list.rowCount(), 2)
        self.assertEqual(self.cache.read_bytes(), self.original)

    def test_class_auth_callback_and_no_redirect_loop(self):
        self.assertTrue(self.class_page._ensure_browser())
        view = self.class_page.main_view
        with patch.object(view, "url", return_value=QUrl("https://" + HOST + "/jwglxt/xtgl/login_slogin.html")), \
             patch.object(view, "load") as load:
            self.class_page._loaded(view, True)
            self.assertEqual(load.call_args.args[0].toString(), LOGIN_URL)
            self.class_page._loaded(view, True); load.assert_called_once()
        with patch.object(view, "url", return_value=QUrl("https://" + HOST + "/jwglxt/xtgl/index_initMenu.html")), \
             patch.object(view, "load") as load:
            self.class_page._loaded(view, True)
            self.assertEqual(load.call_args.args[0].toString(), CLASS_PAGE_URL)
        with patch.object(view, "url", return_value=QUrl(CLASS_PAGE_URL)), patch.object(view, "load") as load:
            self.class_page._loaded(view, True); load.assert_not_called()
            self.assertIn("课表名称预览", self.class_page.status.text())
        self.assertEqual(self.cache.read_bytes(), self.original)

    def test_preview_pages_share_session_and_query_tab_stays_open(self):
        self.class_page._ensure_browser()
        request = Mock()
        self.class_page._new_window(request)
        self.assertEqual(self.class_page.browser_tabs.count(), 2)
        preview = self.class_page.browser_tabs.widget(1)
        self.assertIs(preview.page().profile(), self.class_page.profile)
        request.openIn.assert_called_once_with(preview.page())
        self.class_page.close_preview(0)
        self.assertEqual(self.class_page.browser_tabs.count(), 2)
        self.class_page.close_preview(1)
        self.assertEqual(self.class_page.browser_tabs.count(), 1)

    def test_school_pdf_export_is_saved_to_user_selected_path(self):
        data = b"%PDF-1.4\n% class timetable download fixture\n%%EOF\n"
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", 'attachment; filename="class.pdf"')
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            self.class_page._ensure_browser()
            target = Path(self.temp.name) / "班级课表.pdf"
            with patch("timetable_page.QFileDialog.getSaveFileName", return_value=(str(target), "")) as picker:
                self.class_page.main_view.load(QUrl(f"http://127.0.0.1:{server.server_port}/class.pdf"))
                self.until(lambda: target.exists() and target.read_bytes() == data)
                picker.assert_called_once()
            self.assertEqual(self.cache.read_bytes(), self.original)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_cancel_export_and_stop_browser(self):
        download = Mock(); download.suggestedFileName.return_value = "class.pdf"
        with patch("timetable_page.QFileDialog.getSaveFileName", return_value=("", "")):
            self.page._download_requested(download)
        download.cancel.assert_called_once(); download.accept.assert_not_called()
        self.class_page._ensure_browser(); self.class_page.stop()
        request = Mock(); self.class_page._new_window(request); request.openIn.assert_not_called()


if __name__ == "__main__": unittest.main()
