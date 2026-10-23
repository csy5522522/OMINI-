"""Exercise the real Web service, Qt queue and bundled downloader in isolation."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import requests

ROOT = Path(__file__).resolve().parents[1]
FILE_BYTES = b"Omini Web integration fixture\n" * 18000


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        data = FILE_BYTES if self.path == "/sample.bin" else FILE_BYTES * 8
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            for offset in range(0, len(data), 16000):
                self.wfile.write(data[offset:offset + 16000])
                self.wfile.flush()
                if self.path != "/sample.bin":
                    time.sleep(.04)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


class WebServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.data = Path(cls.temp.name) / "data"
        cls.downloads = Path(cls.temp.name) / "downloads"
        cls.fixture = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.fixture.daemon_threads = True
        cls.fixture_thread = threading.Thread(target=cls.fixture.serve_forever, daemon=True)
        cls.fixture_thread.start()
        cls.source = f"http://127.0.0.1:{cls.fixture.server_port}"
        cls.start()

    @classmethod
    def start(cls):
        env = os.environ.copy()
        access_password = getattr(cls, "access_password", "OminiTestOnly-123456")
        env.update(PYTHONIOENCODING="utf-8", OMINI_WEB_PASSWORD=access_password)
        cls.process = subprocess.Popen(
            [sys.executable, str(ROOT / "web_server.py"), "--port", "0", "--data-dir", str(cls.data)],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        lines = []
        ready = threading.Event()
        def output():
            for line in cls.process.stdout:
                lines.append(line)
                match = re.search(r"Local URL: (http://\S+)", line)
                if match:
                    cls.url = match[1]
                    ready.set()
        threading.Thread(target=output, daemon=True).start()
        if not ready.wait(15):
            cls.process.kill()
            raise RuntimeError("Web service did not start: " + "".join(lines))
        cls.session = requests.Session()
        cls.session.trust_env = False
        if access_password:
            response = cls.session.post(cls.url + "/api/login", json={"password": access_password}, headers={"X-Omini-Client": "web"}, timeout=5)
            assert response.status_code == 200, response.text

    @classmethod
    def stop(cls):
        cls.session.close()
        cls.process.terminate()
        cls.process.wait(timeout=10)
        cls.process.stdout.close()

    @classmethod
    def tearDownClass(cls):
        cls.stop()
        cls.fixture.shutdown()
        cls.fixture.server_close()
        cls.fixture_thread.join(timeout=2)
        cls.temp.cleanup()

    def get(self, path, **kwargs):
        return self.session.get(self.url + path, timeout=8, **kwargs)

    def post(self, path, data, **kwargs):
        return self.session.post(self.url + path, json=data, headers={"X-Omini-Client": "web", **kwargs.pop("headers", {})}, timeout=8, **kwargs)

    def test_01_auth_and_request_boundaries(self):
        with requests.Session() as anonymous:
            anonymous.trust_env = False
            self.assertEqual(anonymous.get(self.url + "/api/state", timeout=4).status_code, 401)
            self.assertEqual(anonymous.get(self.url, timeout=4).status_code, 200)
            self.assertEqual(anonymous.get(self.url + "/api/state", timeout=4).status_code, 401)
        denied = self.session.post(self.url + "/api/settings", json={"concurrency": 1}, timeout=4)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.post("/api/settings", {"concurrency": 1}, headers={"Origin": "https://other.example"}).status_code, 403)
        self.assertEqual(self.get("/api/state", headers={"Host": "rebind.example"}).status_code, 403)
        self.assertEqual(self.post("/api/login", {"password": "wrong"}).status_code, 401)
        response = self.get("/")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(self.get("/favicon.ico").status_code, 200)
        self.assertEqual(self.get("/app.js").status_code, 200)
        self.assertEqual(self.get("/style.css").status_code, 200)
        for resource in ("/memos.css", "/memo-editor.js"):
            self.assertEqual(self.get(resource).status_code, 200)
        self.assertEqual(self.get("/web_server.py").status_code, 404)

    def test_02_download_center_removed(self):
        for path in ("/api/tasks", "/api/tasks/fixture/files", "/api/tasks/fixture/log", "/api/files/fixture/file"):
            self.assertEqual(self.get(path).status_code, 404)
            self.assertEqual(self.post(path, {}).status_code, 404)
        state = self.get("/api/state").json()
        self.assertNotIn("downloads", state["features"])
        for field in ("tasks", "engines", "download_dir", "concurrency"):
            self.assertNotIn(field, state)
        self.assertNotIn('id="page-downloads"', self.get("/").text)
        self.assertFalse((self.data / "tasks.json").exists())

    def test_04_import_har_and_cache_privacy(self):
        payload = {"xsxx": {"XH": "PRIVATE_STUDENT", "XM": "PRIVATE_STUDENT_NAME"}, "kbList": [
            {"kcmc": "数据结构 <script>", "xm": "示例教师", "cdmc": "A201", "xqj": "1", "jcs": "1-2,5", "zcd": "1-16周(单)", "xqh_id": "1"}]}
        har = {"log": {"entries": [{"request": {"url": "https://jw.hualixy.edu.cn/jwglxt/kbcx/xskbcx_cxXsgrkb.html", "postData": {"text": "xnm=2026&xqm=3"}, "headers": [{"name": "Cookie", "value": "PRIVATE_COOKIE"}]}, "response": {"status": 200, "content": {"encoding": "base64", "text": base64.b64encode(json.dumps(payload).encode()).decode()}}}]}}
        response = self.post("/api/timetable/import", {"content": json.dumps(har)})
        self.assertEqual(response.status_code, 200, response.text)
        schedule = response.json()["schedule"]
        self.assertEqual(schedule["courses"][0]["weeks"], list(range(1, 17, 2)))
        self.assertEqual(schedule["courses"][0]["periods"], [1, 2, 5])
        self.assertEqual(self.post("/api/timetable/semester", {"first_monday": "2026-09-07"}).status_code, 200)
        self.assertEqual(self.post("/api/timetable/semester", {"first_monday": "2026-09-08"}).status_code, 400)
        self.assertEqual(self.get("/api/timetable").json()["first_monday"], "2026-09-07")
        for file in self.data.rglob("*"):
            if file.is_file():
                raw = file.read_bytes()
                self.assertNotIn(b"PRIVATE_STUDENT", raw)
                self.assertNotIn(b"PRIVATE_COOKIE", raw)
        self.assertEqual(self.post("/api/timetable/import", {"content": "{}"}).status_code, 400)
        self.assertEqual(self.get("/api/timetable").json()["schedule"]["courses"], schedule["courses"])

    def test_05_week_validation_and_restart(self):
        for week in (-1, 61, True, "2"):
            self.assertEqual(self.post("/api/timetable/week", {"selected_week": week}).status_code, 400)
        for week in (0, 7):
            self.assertEqual(self.post("/api/timetable/week", {"selected_week": week}).status_code, 200)
            schedule = self.get("/api/timetable").json()
            self.assertEqual(schedule["selected_week"], week)
            type(self).stop(); type(self).start()
            self.assertEqual(self.get("/api/timetable").json(), schedule)

    def test_07_email_registration_respects_service_password(self):
        value = {"email": "member@example.test", "password": "EmailPassword-123"}
        with requests.Session() as client:
            client.trust_env = False
            url = self.url + "/api/auth/register"
            headers = {"X-Omini-Client": "desktop"}
            self.assertEqual(client.post(url, json=value, headers=headers, timeout=5).status_code, 401)
            response = client.post(url, json={**value, "access_password": "OminiTestOnly-123456"}, headers=headers, timeout=5)
            self.assertEqual(response.status_code, 201, response.text)
            session = client.get(self.url + "/api/auth/session", timeout=5).json()
            self.assertEqual(session["user"]["email"], value["email"])
            self.assertTrue(session["access_password_required"])


class EmailWebTests(unittest.TestCase):
    access_password = ""
    start = classmethod(WebServerTests.start.__func__)
    stop = classmethod(WebServerTests.stop.__func__)
    get = WebServerTests.get
    post = WebServerTests.post

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.data = Path(cls.temp.name) / "data"
        cls.downloads = Path(cls.temp.name) / "downloads"
        cls.start()

    @classmethod
    def tearDownClass(cls):
        cls.stop(); cls.temp.cleanup()

    def test_01_local_service_requires_email_session(self):
        self.assertEqual(self.get("/").status_code, 200)
        self.assertEqual(self.get("/api/state").status_code, 401)
        self.assertEqual(self.post("/api/login", {"password": ""}).status_code, 401)
        self.assertFalse(self.get("/api/auth/session").json()["authenticated"])

    def test_02_register_login_rotate_and_logout(self):
        credentials = {"email": " Member@Example.test ", "password": "EmailPassword-123"}
        for value in ({**credentials, "email": "bad email"}, {**credentials, "password": "short"}):
            self.assertEqual(self.post("/api/auth/register", value).status_code, 400)
        response = self.post("/api/auth/register", credentials)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["user"]["email"], "member@example.test")
        self.assertNotIn("password", response.text)
        self.assertIn("HttpOnly", response.headers["Set-Cookie"])
        token = self.session.cookies["omini_session"]
        self.assertEqual(self.get("/api/state").status_code, 200)
        self.assertEqual(self.post("/api/auth/register", credentials).status_code, 400)
        self.assertEqual(self.post("/api/auth/login", {**credentials, "password": "incorrect-password"}).status_code, 401)
        self.assertEqual(self.post("/api/auth/login", credentials).status_code, 200)
        self.assertEqual(self.get("/api/state", headers={"Cookie": "omini_session=" + token}).status_code, 401)
        token = self.session.cookies["omini_session"]
        self.assertEqual(self.post("/api/auth/logout", {}).status_code, 200)
        self.assertEqual(self.get("/api/state", headers={"Cookie": "omini_session=" + token}).status_code, 401)
        self.assertEqual(self.post("/api/auth/login", credentials).status_code, 200)
        self.assertNotIn(credentials["password"].encode(), (self.data / "accounts.sqlite3").read_bytes())

    def test_03_browser_and_desktop_use_same_durable_accounts(self):
        from account_client import AccountClient
        token = self.session.cookies["omini_session"]
        type(self).stop(); type(self).start()
        self.session.cookies.set("omini_session", token)
        self.assertEqual(self.get("/api/auth/session").json()["user"]["email"], "member@example.test")
        client = AccountClient(self.url)
        response = client.request("login", {"email": "member@example.test", "password": "EmailPassword-123"})
        self.assertEqual(response["user"]["email"], "member@example.test")
        self.assertTrue(client.request("session")["authenticated"])
        desktop_token = client.token
        client.request("logout", {})
        self.assertFalse(AccountClient(self.url, desktop_token).request("session")["authenticated"])
        self.assertEqual(self.get("/api/state").status_code, 200)

    def test_04_optional_features_persist_and_reenable(self):
        response = self.post("/api/settings", {"features": {"timetable": False}})
        self.assertEqual(response.status_code, 200, response.text)
        state = self.get("/api/state").json()
        self.assertEqual(state["features"], {"timetable": False, "notes": True, "ai_assistants": True})
        self.assertEqual(self.get("/api/timetable").status_code, 403)
        self.assertEqual(self.post("/api/tasks", {"type": "file", "source": "https://example.test/a"}).status_code, 404)
        for features in ([], {"bad": True}, {"timetable": "false"}, {"downloads": 1}):
            self.assertEqual(self.post("/api/settings", {"features": features}).status_code, 400)
        token = self.session.cookies["omini_session"]
        type(self).stop(); type(self).start(); self.session.cookies.set("omini_session", token)
        self.assertEqual(self.get("/api/state").json()["features"], state["features"])
        self.assertEqual(self.post("/api/settings", {"features": {"timetable": True}}).status_code, 200)
        self.assertEqual(self.get("/api/timetable").status_code, 200)
        self.assertNotIn("downloads", self.get("/api/state").json()["features"])

    def test_05_login_throttling(self):
        type(self).stop(); type(self).start()
        for _ in range(10):
            self.assertEqual(self.post("/api/auth/login", {"email": "missing@example.test", "password": "incorrect-password"}).status_code, 401)
        self.assertEqual(self.post("/api/auth/login", {"email": "missing@example.test", "password": "incorrect-password"}).status_code, 429)


if __name__ == "__main__":
    unittest.main()
