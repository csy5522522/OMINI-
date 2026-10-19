"""Timetable data, session boundaries, offline persistence and Qt integration."""
import base64
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs

from PySide6.QtCore import QDate, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

import app
import school_timetable as protocol
from timetable_page import TimetablePage, SchoolLoginDialog


def payload(week="1-16周(单)"):
    return {"xsxx": {"XH": "PRIVATE_STUDENT", "XM": "PRIVATE_NAME"}, "kbList": [
        {"kcmc": "数据库原理", "xm": "张老师", "cdmc": "4-507", "xqmc": "广州校区",
         "xqj": "1", "jcs": "1-2", "zcd": week, "xf": "3", "xqh_id": "1"},
        {"kcmc": "网络实验", "xm": "李老师", "cdmc": "实验室", "xqj": "3", "jcs": "5-6",
         "zcd": "2-16周(双)", "xqh_id": "1"}]}


def entry(path, value, year="2026", term="3", encoded=False):
    text = json.dumps(value, ensure_ascii=False)
    content = {"text": base64.b64encode(text.encode()).decode() if encoded else text}
    if encoded: content["encoding"] = "base64"
    return {"request": {"url": protocol.BASE_URL + path, "headers": [{"name": "Cookie", "value": "PRIVATE_SESSION"}],
                        "postData": {"text": f"xnm={year}&xqm={term}&xqh_id=1"}},
            "response": {"status": 200, "content": content}}


class TimetableProtocolTest(unittest.TestCase):
    def test_mixed_parity_and_non_contiguous_ranges(self):
        self.assertEqual(protocol.number_ranges("1-9周(单),2-8周(双),第15周", 60, True), list(range(1, 10)) + [15])
        self.assertEqual(protocol.number_ranges("1–5周，7~9周", 60, True), [1, 2, 3, 4, 5, 7, 8, 9])
        for value in ("", "0周", "9-2周", "61周"):
            with self.assertRaises(ValueError): protocol.number_ranges(value, 60, True)

    def test_import_latest_query_base64_times_and_no_identity(self):
        records = [entry(protocol.COURSE_PATH, payload(), "2025"),
                   entry(protocol.COURSE_PATH, payload("1-5周,7-9周"), encoded=True),
                   entry(protocol.PERIOD_PATH, [{"jcmc": "1", "qssj": "08:30", "jssj": "09:15"}]),
                   entry(protocol.PERIOD_PATH, [{"jcmc": "1", "qssj": "00:00", "jssj": "01:00"}], "2025")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.har"
            path.write_text(json.dumps({"log": {"entries": records}}), encoding="utf-8-sig")
            value = protocol.import_har(path)
        self.assertEqual((value["year"], value["term"]), ("2026", "3"))
        self.assertEqual(value["courses"][0]["weeks"], [1, 2, 3, 4, 5, 7, 8, 9])
        self.assertEqual(value["period_times"]["1"]["start"], "08:30")
        self.assertNotIn("PRIVATE_", json.dumps(value))
        self.assertEqual(protocol.validate_cache(value), value)

    def test_non_school_and_missing_response_are_rejected(self):
        row = entry(protocol.COURSE_PATH, payload())
        row["request"]["url"] = "https://example.com" + protocol.COURSE_PATH
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.har"
            path.write_text(json.dumps({"log": {"entries": [row]}}))
            with self.assertRaisesRegex(ValueError, "没有学校课表响应"): protocol.import_har(path)

    def test_bad_courses_and_empty_semester(self):
        for key, value in [("xqj", "8"), ("jcs", "0"), ("zcd", "")]:
            data = payload(); data["kbList"][0][key] = value
            with self.assertRaises(ValueError): protocol.parse_schedule(data, "2026", "3")
        self.assertEqual(protocol.parse_schedule({"kbList": []}, "2026", "12")["courses"], [])

    def test_cookie_header_validation(self):
        self.assertEqual(protocol.normalize_cookie("Cookie: JSESSIONID=TOKEN; route=node"), "JSESSIONID=TOKEN; route=node")
        for value in ("", "a=", "a=x\r\nInjected: secret", "wrong", "a=测试"):
            with self.assertRaises(ValueError): protocol.normalize_cookie(value)

    def test_no_cookie_redirect_to_another_host_or_plain_http(self):
        handler = protocol.SchoolRedirectHandler()
        for target in ("https://other.example/login", "http://jw.hualixy.edu.cn/login", "https://jw.hualixy.edu.cn:8443/login"):
            with self.assertRaises(ValueError): handler.redirect_request(None, None, 302, "", {}, target)

    def test_live_post_uses_semester_and_detects_login_html(self):
        class Opener:
            def __init__(self): self.requests = []
            def open(self, request, timeout):
                self.requests.append(request)
                if len(self.requests) == 1: return io.BytesIO(json.dumps(payload()).encode())
                return io.BytesIO(b'[{"jcmc":"1","qssj":"08:30","jssj":"09:15"}]')
        opener = Opener()
        value = protocol.fetch_schedule("JSESSIONID=TOKEN", "2026", "3", opener=opener)
        self.assertEqual(len(value["courses"]), 2)
        self.assertEqual(parse_qs(opener.requests[0].data.decode())["xqm"], ["3"])
        self.assertTrue(all(request.host == protocol.HOST for request in opener.requests))
        bad = type("Bad", (), {"open": lambda *args, **kwargs: io.BytesIO(b"<html>login</html>")})()
        with self.assertRaisesRegex(ValueError, "登录"): protocol.fetch_schedule("a=b", "2026", "3", opener=bad)

    def test_auth_and_optional_times_failures(self):
        class Opener:
            def __init__(self, status): self.status = status; self.count = 0
            def open(self, request, timeout):
                self.count += 1
                if self.status == 401 or self.count > 1: raise HTTPError(request.full_url, self.status, "", {}, None)
                return io.BytesIO(json.dumps(payload()).encode())
        with self.assertRaisesRegex(ValueError, "会话已过期"):
            protocol.fetch_schedule("a=b", "2026", "3", opener=Opener(401))
        value = protocol.fetch_schedule("a=b", "2026", "3", opener=Opener(500))
        self.assertEqual(len(value["courses"]), 2); self.assertIn("warning", value)


class TimetableUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.qt = QApplication.instance() or QApplication([])

    def until(self, predicate):
        for _ in range(400):
            if predicate(): return
            QTest.qWait(10)
        self.assertTrue(predicate())

    def test_offline_import_week_filter_times_and_encrypted_session(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache.json"; saved = []
            har = Path(directory) / "input.har"
            har.write_text(json.dumps({"log": {"entries": [entry(protocol.COURSE_PATH, payload()),
                entry(protocol.PERIOD_PATH, [{"jcmc": "1", "qssj": "08:30", "jssj": "09:15"}])]}}))
            page = TimetablePage({}, lambda settings: saved.append(json.loads(json.dumps(settings))), cache,
                                 lambda value: cache.write_text(json.dumps(value)))
            page.resize(1060, 860); page.show()
            page.load_har(har); self.until(lambda: page.worker is None)
            self.assertEqual(page.list.rowCount(), 2)
            self.assertIn("08:30", page.grid.verticalHeaderItem(0).text())
            page.week.setCurrentIndex(page.week.findData(1))
            self.assertEqual(page.list.rowCount(), 1); self.assertEqual(page.grid.rowSpan(0, 0), 2)
            page.week.setCurrentIndex(page.week.findData(2))
            self.assertEqual(page.list.item(0, 0).text(), "网络实验")
            self.assertEqual(page.grid.rowSpan(0, 0), 1, "Old merged course cells remained after changing week")
            page.use_start.setChecked(True); page.start.setDate(QDate(2026, 10, 5))
            page.cookie.setText("JSESSIONID=PRIVATE_TOKEN"); page.remember.setChecked(True); page.save()
            config = saved[-1]["school_timetable"]
            self.assertEqual(unprotect(config["session_blob"]), "JSESSIONID=PRIVATE_TOKEN")
            self.assertNotIn("PRIVATE_TOKEN", json.dumps(saved)); self.assertNotIn("PRIVATE_", cache.read_text())
            offline = TimetablePage(saved[-1], lambda _: None, cache, lambda _: None)
            self.assertEqual(offline.list.rowCount(), 2); self.assertTrue(offline.use_start.isChecked())
            page.remember.setChecked(False); page.save(); self.assertEqual(saved[-1]["school_timetable"]["session_blob"], "")
            page.stop(); offline.stop(); page.close(); offline.close()
            page.deleteLater(); offline.deleteLater(); self.qt.processEvents()

    def test_failed_refresh_preserves_offline_course_data(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache.json"
            cache.write_text(json.dumps(protocol.parse_schedule(payload(), "2026", "3")))
            page = TimetablePage({}, lambda _: None, cache, lambda value: cache.write_text(json.dumps(value)))
            previous = cache.read_bytes(); page.cookie.setText("a=b")
            with patch("timetable_page.fetch_schedule", side_effect=ValueError("登录会话已过期")):
                page.refresh(); self.until(lambda: page.worker is None)
            self.assertEqual(page.list.rowCount(), 2); self.assertEqual(cache.read_bytes(), previous)
            self.assertIn("登录会话已过期", page.status.text())
            page.stop(); page.deleteLater(); self.qt.processEvents()

    def test_sidebar_mapping_and_exit_during_timetable_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patches = [patch.object(app, key, root / name) for key, name in {
                "APP_DIR": ".", "SETTINGS_FILE": "settings.json", "TASKS_FILE": "tasks.json",
                "LOG_DIR": "logs", "TEMP_COOKIE_DIR": "cookies"}.items()]
            for p in patches: p.start()
            try:
                with patch("campus_page.wifi_ssids", return_value=()):
                    window = app.MainWindow(); window.show(); window.nav.setCurrentRow(2)
                    page = window.timetable_page
                    self.assertIs(window.stack.currentWidget(), page)
                    window.nav.setCurrentRow(4); self.assertIs(window.stack.currentWidget(), window.settings_page)
                    def operation(cancelled):
                        for _ in range(200):
                            if cancelled(): break
                            time.sleep(.01)
                        return protocol.parse_schedule(payload(), "2026", "3")
                    page._start(operation, "读取中")
                    window.quitting = True; window.close()
                    self.until(lambda: page.worker is None and not window.isVisible())
                    self.assertFalse(page.clock.isActive())
                    self.assertFalse((root / "school-timetable.json").exists())
                    window.deleteLater(); self.qt.processEvents()
            finally:
                for p in reversed(patches): p.stop()

    def test_login_profile_is_ephemeral_and_cookie_scoped(self):
        # Load a local blank page rather than contacting the school in a regression test.
        from PySide6.QtNetwork import QNetworkCookie
        with patch("timetable_page.LOGIN_URL", "about:blank"):
            dialog = SchoolLoginDialog()
        self.assertTrue(dialog.profile.isOffTheRecord())
        for domain in ("jw.hualixy.edu.cn", "evil.example"):
            cookie = QNetworkCookie(b"session", b"TOKEN"); cookie.setDomain(domain); cookie.setPath("/jwglxt")
            dialog._cookie_added(cookie)
        self.assertEqual(len(dialog.cookies), 1)
        dialog.web.stop(); dialog.reject(); dialog.deleteLater(); self.qt.processEvents()

    def test_sso_entry_callback_and_cookie_domains(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtNetwork import QNetworkCookie
        from PySide6.QtWidgets import QPushButton
        with patch("timetable_page.LOGIN_URL", "about:blank"):
            dialog = SchoolLoginDialog()
        # The login button uses the CAS service callback, not the old login form.
        button = next(b for b in dialog.findChildren(QPushButton) if b.text() == "统一认证登录")
        with patch.object(dialog.web, "load") as load:
            button.click()
            self.assertEqual(load.call_args.args[0].toString(), protocol.LOGIN_URL)
            self.assertIn("jw.hualixy.edu.cn%2Fsso%2Fddlogin", protocol.LOGIN_URL)
        for domain in ("sso.hualixy.edu.cn", "jw.hualixy.edu.cn", ".hualixy.edu.cn"):
            cookie = QNetworkCookie(domain.encode(), b"TOKEN"); cookie.setDomain(domain); cookie.setPath("/")
            dialog._cookie_added(cookie)
        domains = {key[0] for key in dialog.cookies}
        self.assertEqual(domains, {"jw.hualixy.edu.cn", "hualixy.edu.cn"})
        with patch.object(dialog.web, "url", return_value=QUrl(protocol.BASE_URL + "/jwglxt/xtgl/index_initMenu.html")), \
             patch.object(dialog.web, "load") as load:
            dialog._load_finished(True)
            self.assertEqual(load.call_args.args[0].toString(), protocol.PAGE_URL)
        with patch.object(dialog.web, "url", return_value=QUrl(protocol.LOGIN_URL)), patch.object(dialog, "accept") as accept:
            dialog._read(); accept.assert_not_called()
        dialog.web.stop(); dialog.reject(); dialog.deleteLater(); self.qt.processEvents()


    def test_auto_capture_waits_for_cookie_and_runs_once(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtNetwork import QNetworkCookie
        for cookie_first in (True, False):
            with self.subTest(cookie_first=cookie_first), patch("timetable_page.LOGIN_URL", "about:blank"):
                dialog = SchoolLoginDialog()
            callbacks = []
            cookie = QNetworkCookie(b"JSESSIONID", b"TOKEN"); cookie.setDomain(protocol.HOST); cookie.setPath("/jwglxt")
            with patch.object(dialog.web, "url", return_value=QUrl(protocol.PAGE_URL)), \
                 patch.object(dialog.web.page(), "runJavaScript", side_effect=lambda script, callback: callbacks.append(callback)), \
                 patch.object(dialog, "accept") as accept:
                if cookie_first: dialog._cookie_added(cookie)
                dialog._load_finished(True); dialog._load_finished(True)
                self.assertEqual(len(callbacks), 1, "Duplicate loads started concurrent page checks")
                accept.assert_not_called()
                callbacks[0](True)
                if not cookie_first:
                    accept.assert_not_called(); dialog._cookie_added(cookie)
                self.assertEqual(dialog.cookie_header, "JSESSIONID=TOKEN")
                dialog._cookie_added(cookie); dialog._load_finished(True)
                accept.assert_called_once()
            dialog.web.stop(); dialog.reject(); dialog.deleteLater(); self.qt.processEvents()

    def test_login_html_and_stale_navigation_never_trigger_capture(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtNetwork import QNetworkCookie
        with patch("timetable_page.LOGIN_URL", "about:blank"):
            dialog = SchoolLoginDialog()
        callbacks = []
        cookie = QNetworkCookie(b"JSESSIONID", b"TOKEN"); cookie.setDomain(protocol.HOST); cookie.setPath("/")
        with patch.object(dialog.web, "url", return_value=QUrl(protocol.PAGE_URL)), \
             patch.object(dialog.web.page(), "runJavaScript", side_effect=lambda script, callback: callbacks.append(callback)), \
             patch.object(dialog, "accept") as accept:
            dialog._cookie_added(cookie)
            dialog._load_finished(False); self.assertEqual(callbacks, [])
            dialog._load_finished(True); callbacks.pop()(False)
            accept.assert_not_called()
            dialog._load_finished(True)
            dialog._url_changed(QUrl(protocol.LOGIN_URL))
            callbacks.pop()(True); accept.assert_not_called()
            dialog._load_finished(True)
            dialog.reject(); callbacks.pop()(True); accept.assert_not_called()
        dialog.web.stop(); dialog.deleteLater(); self.qt.processEvents()

    def test_auto_login_completion_fetches_and_persists_course_data(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtNetwork import QNetworkCookie
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache.json"
            page = TimetablePage({}, lambda _: None, cache, lambda value: cache.write_text(json.dumps(value)))
            with patch("timetable_page.LOGIN_URL", "about:blank"):
                page.login()
            dialog = page.login_dialog
            cookie = QNetworkCookie(b"JSESSIONID", b"TOKEN"); cookie.setDomain(protocol.HOST); cookie.setPath("/jwglxt")
            callbacks = []
            with patch.object(dialog.web, "url", return_value=QUrl(protocol.PAGE_URL)), \
                 patch.object(dialog.web.page(), "runJavaScript", side_effect=lambda script, callback: callbacks.append(callback)), \
                 patch("timetable_page.fetch_schedule", return_value=protocol.parse_schedule(payload(), "2026", "3")) as fetch:
                dialog._cookie_added(cookie)
                dialog._load_finished(True); callbacks.pop()(True)
                self.until(lambda: page.worker is None and page.schedule is not None)
                fetch.assert_called_once()
                self.assertEqual(fetch.call_args.args[0], "JSESSIONID=TOKEN")
                self.assertIsNone(page.login_dialog)
                self.assertEqual(page.list.rowCount(), 2)
                self.assertEqual(len(json.loads(cache.read_text())["courses"]), 2)
            page.stop(); page.deleteLater(); self.qt.processEvents()

    def test_real_browser_page_selectors_trigger_automatic_fetch(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtNetwork import QNetworkCookie
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache.json"
            page = TimetablePage({}, lambda _: None, cache, lambda value: cache.write_text(json.dumps(value)))
            with patch("timetable_page.LOGIN_URL", "about:blank"):
                page.login()
            dialog = page.login_dialog
            cookie = QNetworkCookie(b"JSESSIONID", b"TOKEN"); cookie.setDomain(protocol.HOST); cookie.setPath("/jwglxt")
            with patch("timetable_page.fetch_schedule", return_value=protocol.parse_schedule(payload(), "2026", "3")) as fetch:
                dialog.profile.cookieStore().setCookie(cookie, QUrl(protocol.BASE_URL))
                dialog.web.setHtml('<!doctype html><select id="xnm"><option>2026</option></select>'
                                   '<select id="xqm"><option>3</option></select>', QUrl(protocol.PAGE_URL))
                self.until(lambda: page.schedule is not None and page.worker is None)
                fetch.assert_called_once()
                self.assertEqual(len(json.loads(cache.read_text())["courses"]), 2)
            page.stop(); page.deleteLater(); self.qt.processEvents()


def unprotect(blob):
    from campus_network import unprotect_password
    return unprotect_password(blob)


if __name__ == "__main__": unittest.main()
