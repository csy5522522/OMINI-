"""Protocol, schedule and UI regressions; no real account authentication."""
import json
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote
from unittest.mock import patch

from PySide6.QtCore import Qt, QTimer
from PySide6.QtQml import QJSEngine
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import app
import campus_network as network
from campus_page import LoginSchedule


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.clock = 100
        self.now = datetime(2026, 10, 11, 9, 0)
        self.config = dict(ssid="LEDE1", wifi_auto=True, interval_enabled=True,
                           interval_minutes=5, daily_enabled=False)
        self.schedule = LoginSchedule(self.config, lambda: self.clock)

    def test_connect_startup_reconnect_and_other_wifi(self):
        self.assertEqual(self.schedule.reason(("OTHER",), self.now), "")
        self.assertEqual(self.schedule.reason(("LEDE1",), self.now), "连接目标 Wi-Fi")
        self.assertEqual(self.schedule.reason(("LEDE1",), self.now), "")
        self.schedule.reason((), self.now)
        self.assertEqual(self.schedule.reason(("LEDE1",), self.now), "连接目标 Wi-Fi")

    def test_interval_and_daily_once_per_day(self):
        self.config.update(wifi_auto=False, daily_enabled=True, daily_time="10:00")
        self.assertEqual(self.schedule.reason(("LEDE1",), self.now), "")
        self.clock += 300
        self.assertEqual(self.schedule.reason(("LEDE1",), self.now), "间隔定时检查")
        self.now = self.now.replace(hour=10)
        self.assertEqual(self.schedule.reason(("LEDE1",), self.now), "每日定时登录")
        self.assertEqual(self.schedule.reason(("LEDE1",), self.now), "")
        self.assertEqual(self.schedule.reason(("LEDE1",), self.now.replace(day=12)), "每日定时登录")

    def test_retry_cooldown_and_stop_after_three_failures(self):
        self.schedule.reason(("LEDE1",), self.now)
        for _ in range(3):
            self.schedule.completed(False)
            self.assertEqual(self.schedule.reason(("LEDE1",), self.now), "")
            self.clock += 60
            reason = self.schedule.reason(("LEDE1",), self.now)
        self.assertEqual(reason, "")
        self.clock += 1000
        self.assertEqual(self.schedule.reason(("LEDE1",), self.now), "")
        self.schedule.reason((), self.now)
        self.assertEqual(self.schedule.reason(("LEDE1",), self.now), "连接目标 Wi-Fi")

    def test_no_automatic_actions_when_disabled(self):
        self.config.update(wifi_auto=False, interval_enabled=False)
        self.schedule.completed(False)
        self.clock += 1000
        self.assertEqual(self.schedule.reason(("LEDE1",), self.now), "")


class ProtocolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        cls.modulus = "94dd2a8675fb779e6b9f7103698634cd400f27a154afa67af6166a43fc26417222a79506d34cacc7641946abda1785b7acf9910ad6a0978c91ec84d40b71d2891379af19ffb333e7517e390bd26ac312fe940c340466b4a5d4af1d65c3b5944078f96a1a51a5a53e4bc302818b7c9f63c4a1b07bd7d874cef1c3d4b2f5eb7871"

    def test_password_dpapi_roundtrip(self):
        password = "test-password-中文-!@#"
        blob = network.protect_password(password)
        self.assertNotIn(password, blob)
        self.assertEqual(network.unprotect_password(blob), password)
        self.assertEqual(network.unprotect_password(""), "")

    def test_portal_chinese_messages_support_utf8_and_gbk(self):
        message = "获取用户信息失败，用户可能已经下线"
        self.assertEqual(network.decode_portal_content(message.encode("utf-8")), message)
        self.assertEqual(network.decode_portal_content(message.encode("gbk")), message)
        self.assertEqual(network.decode_portal_content(message.encode("gbk"), "gbk"), message)

    def test_rsa_matches_actual_portal_javascript(self):
        source = Path("tests/fixtures/portal-security.js").read_text(encoding="utf-8")
        engine = QJSEngine()
        engine.evaluate("var window = this;")
        result = engine.evaluate(source)
        self.assertFalse(result.isError(), result.toString())
        for password in ("abc123!@#", "x" * 150, ""):
            plaintext = password + ">0123456789abcdef"
            script = ("RSAUtils.setMaxDigits(400); RSAUtils.encryptedString("
                      "RSAUtils.getKeyPair('10001', '', " + json.dumps(self.modulus) + "), "
                      + json.dumps(plaintext) + ".split('').reverse().join(''))")
            expected = engine.evaluate(script)
            self.assertFalse(expected.isError(), expected.toString())
            self.assertEqual(network.encrypt_portal_password(password, "0123456789abcdef", "10001", self.modulus), expected.toString())

    def test_full_http_flow_fresh_query_rsa_double_encoding_and_online_skip(self):
        captured = []
        query = "wlanuserip=FRESH_IP&mac=FRESH_MAC&url=a%2Fb"
        modulus = self.modulus
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                if self.path.startswith("/eportal/index.jsp?"):
                    self.send_response(200); self.end_headers(); self.wfile.write(b"portal")
                else:
                    self.send_response(302); self.send_header("Location", "/eportal/index.jsp?" + query); self.end_headers()
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                method = parse_qs(self.path.split("?", 1)[1])["method"][0]
                captured.append((method, body))
                result = {"result": "success"}
                if method == "pageInfo":
                    result = dict(passwordEncrypt="true", publicKeyExponent="10001", publicKeyModulus=modulus)
                elif method == "login": result["userIndex"] = "FRESH_SESSION"
                elif method == "getOnlineUserInfo" and parse_qs(body).get("userIndex") == ["STALE_SESSION"]:
                    result["result"] = "fail"
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps(result).encode())
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        try:
            with patch.object(network, "PROBE_URL", origin + "/probe"):
                client = network.PortalClient(origin + "/eportal/success.jsp?userIndex=STALE_SESSION&keepaliveInterval=0")
                self.assertIn("登录成功", client.login("test+user", "test-password"))
                page_fields = parse_qs(captured[1][1])
                self.assertEqual(page_fields["queryString"][0], query)
                login_fields = {key: unquote(value[0]) for key, value in parse_qs(captured[2][1]).items()}
                self.assertEqual(login_fields["queryString"], query)
                self.assertEqual(login_fields["userId"], "test+user")
                self.assertEqual(login_fields["password"], network.encrypt_portal_password("test-password", "FRESH_MAC", "10001", modulus))
                self.assertNotIn("test-password", captured[2][1])
                self.assertEqual(client.browser_url, origin + "/eportal/success.jsp?userIndex=FRESH_SESSION&keepaliveInterval=0")
                self.assertIn("已在线", client.login("test+user", "test-password"))
                self.assertEqual([m for m, _ in captured], ["getOnlineUserInfo", "pageInfo", "login", "getOnlineUserInfo"])
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_online_probe_skips_password_and_login(self):
        client = network.PortalClient()
        with patch.object(client, "fetch", return_value=(network.PROBE_URL, "Microsoft Connect Test")), patch.object(client, "call") as call:
            self.assertIn("已连通", client.login("", ""))
            call.assert_not_called()

    def test_success_page_session_is_checked_and_browser_url_preserved(self):
        url = "http://10.24.127.3/eportal/success.jsp?userIndex=EXISTING_SESSION&keepaliveInterval=0"
        client = network.PortalClient(url)
        self.assertEqual(client.browser_url, url)
        self.assertEqual(client.login_url, "http://10.24.127.3/eportal/index.jsp")
        with patch.object(client, "call", return_value={"result": "success"}) as call, patch.object(client, "discover") as discover:
            self.assertIn("已在线", client.login("test-user", "test-password"))
            call.assert_called_once_with("getOnlineUserInfo", {"userIndex": "EXISTING_SESSION"})
            discover.assert_not_called()

    def test_expired_session_request_error_falls_back_to_live_discovery(self):
        client = network.PortalClient("http://10.24.127.3/eportal/success.jsp?userIndex=OLD&keepaliveInterval=0")
        with patch.object(client, "call", side_effect=RuntimeError("timeout")), patch.object(client, "discover", return_value=("", True)):
            self.assertIn("已连通", client.login("test-user", "test-password"))
            self.assertEqual(client.user_index, "")
            self.assertEqual(client.browser_url, "http://10.24.127.3/eportal/success.jsp?userIndex=OLD&keepaliveInterval=0")


class CampusUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def test_sidebar_save_and_manual_button(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            patches = [patch.object(app, key, root / name) for key, name in {
                "APP_DIR": ".", "TASKS_FILE": "tasks.json", "SETTINGS_FILE": "settings.json",
                "LOG_DIR": "logs", "TEMP_COOKIE_DIR": "cookies",
            }.items()]
            for p in patches: p.start()
            try:
                with patch("campus_page.wifi_ssids", return_value=("LEDE1",)), patch.object(network.PortalClient, "login", return_value="校园网登录成功。") as login:
                    window = app.MainWindow(); window.show()
                    page = window.campus_page
                    window.nav.setCurrentRow(1)
                    self.assertIs(window.stack.currentWidget(), page)
                    page.portal.setText("http://10.24.127.3/eportal/success.jsp?userIndex=CONFIGURED_SESSION&keepaliveInterval=0")
                    page.username.setText("test-user"); page.password.setText("test-secret")
                    page.remember.setChecked(True); page.wifi_auto.setChecked(False); page.interval_enabled.setChecked(False)
                    QTest.mouseClick(page.save_button, Qt.MouseButton.LeftButton)
                    self.until(lambda: page.worker is None)
                    persisted = app.SETTINGS_FILE.read_text(encoding="utf-8")
                    self.assertEqual(page.client.user_index, "CONFIGURED_SESSION", "Initial Wi-Fi detection discarded supplied session")
                    self.assertNotIn("test-secret", persisted)
                    config = json.loads(persisted)["campus_network"]
                    self.assertEqual(network.unprotect_password(config["password_blob"]), "test-secret")
                    QTest.mouseClick(page.login_button, Qt.MouseButton.LeftButton)
                    self.until(lambda: page.worker is None)
                    login.assert_called_once()
                    self.assertIn("登录成功", page.log.toPlainText())
                    page.client.user_index = "LATEST_SESSION"
                    with patch("campus_page.QDesktopServices.openUrl", return_value=True) as open_url:
                        QTest.mouseClick(page.web_button, Qt.MouseButton.LeftButton)
                        self.assertEqual(open_url.call_args.args[0].toString(),
                                         "http://10.24.127.3/eportal/success.jsp?userIndex=LATEST_SESSION&keepaliveInterval=0")
                    page.remember.setChecked(False); page.save()
                    self.until(lambda: page.worker is None)
                    self.assertEqual(json.loads(app.SETTINGS_FILE.read_text(encoding="utf-8"))["campus_network"]["password_blob"], "")
                    page.password.clear(); page.username.clear()
                    page.portal.setText(network.DEFAULT_PORTAL)
                    page.wifi_auto.setChecked(True); page.interval_enabled.setChecked(True)
                    page.log.clear(); page.append_log("配置示例：填写账号和密码后保存，即可启用自动登录。")
                    QTest.qWait(100)
                    window.grab().save(str(Path(f"artifacts/verification/campus-{app.APP_VERSION}.png").resolve()))
                    # Hiding the last window must preserve polling; tray quit must
                    # explicitly stop QApplication even when the window is hidden.
                    page.config["background"] = True
                    with patch.object(app.QSystemTrayIcon, "isSystemTrayAvailable", return_value=True), patch.object(window.tray, "showMessage"):
                        window.close()
                    self.assertFalse(window.isVisible())
                    self.assertTrue(page.timer.isActive())
                    timeout = QTimer(); timeout.setSingleShot(True)
                    expired = []
                    timeout.timeout.connect(lambda: (expired.append(True), self.qt.quit()))
                    timeout.start(1500)
                    QTimer.singleShot(0, window.quit_from_tray)
                    self.qt.exec()
                    timeout.stop()
                    self.assertFalse(expired, "Tray quit left the hidden application running")
                    self.assertFalse(page.timer.isActive())
                    window.deleteLater(); self.qt.processEvents()
            finally:
                for p in reversed(patches): p.stop()

    def until(self, predicate):
        for _ in range(400):
            if predicate(): return
            QTest.qWait(10)
        self.assertTrue(predicate())


if __name__ == "__main__": unittest.main()
