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
        env.update(PYTHONIOENCODING="utf-8", OMINI_WEB_PASSWORD="OminiTestOnly-123456")
        cls.process = subprocess.Popen(
            [sys.executable, str(ROOT / "web_server.py"), "--port", "0", "--data-dir", str(cls.data), "--download-dir", str(cls.downloads)],
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
        response = cls.session.post(cls.url + "/api/login", json={"password": "OminiTestOnly-123456"}, headers={"X-Omini-Client": "web"}, timeout=5)
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

    def wait_status(self, task_id, wanted):
        end = time.monotonic() + 25
        while time.monotonic() < end:
            tasks = self.get("/api/state").json()["tasks"]
            task = next(t for t in tasks if t["id"] == task_id)
            if task["status"] in wanted:
                return task
            time.sleep(.1)
        self.fail(f"Task did not reach {wanted}; last status {task}")

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
        self.assertEqual(self.get("/web_server.py").status_code, 404)

    def test_02_real_download_and_retrieve(self):
        response = self.post("/api/tasks", {"type": "file", "source": self.source + "/sample.bin", "options": {"filename": "sample.bin"}})
        self.assertEqual(response.status_code, 201, response.text)
        task_id = response.json()["task"]["id"]
        task = self.wait_status(task_id, {"completed", "failed"})
        self.assertEqual(task["status"], "completed", self.get(f"/api/tasks/{task_id}/log").text)
        files = self.get(f"/api/tasks/{task_id}/files").json()["files"]
        self.assertEqual([f["name"] for f in files], ["sample.bin"])
        downloaded = self.get(files[0]["url"])
        self.assertEqual(hashlib.sha256(downloaded.content).digest(), hashlib.sha256(FILE_BYTES).digest())
        self.assertIn("attachment;", downloaded.headers["Content-Disposition"])
        # Path traversal, guessed tasks and symlinks cannot expose service data.
        self.assertIn(self.get(f"/api/files/{task_id}/%2e%2e/%2e%2e/data/settings.json").status_code, {400, 404})
        self.assertEqual(self.get("/api/tasks/not-a-task/files").status_code, 400)
        self.assertTrue(Path(task["output_dir"]).is_relative_to(self.downloads))

    def test_03_pause_resume_cancel_and_queue(self):
        self.assertEqual(self.post("/api/settings", {"concurrency": 1}).status_code, 200)
        first = self.post("/api/tasks", {"type": "file", "source": self.source + "/slow.bin"}).json()["task"]["id"]
        self.wait_status(first, {"running"})
        second = self.post("/api/tasks", {"type": "file", "source": self.source + "/queued.bin"}).json()["task"]["id"]
        self.assertEqual(self.wait_status(second, {"queued"})["status"], "queued")
        self.assertEqual(self.post(f"/api/tasks/{second}/pause", {}).status_code, 200)
        self.assertEqual(self.wait_status(second, {"paused"})["status"], "paused")
        self.post(f"/api/tasks/{first}/pause", {})
        self.assertEqual(self.wait_status(first, {"paused"})["status"], "paused")
        self.post(f"/api/tasks/{first}/resume", {})
        self.wait_status(first, {"running"})
        self.post(f"/api/tasks/{first}/cancel", {})
        self.assertEqual(self.wait_status(first, {"cancelled"})["status"], "cancelled")
        self.post(f"/api/tasks/{second}/cancel", {})

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
                raw = file.read_text(encoding="utf-8")
                self.assertNotIn("PRIVATE_STUDENT", raw)
                self.assertNotIn("PRIVATE_COOKIE", raw)
        self.assertEqual(self.post("/api/timetable/import", {"content": "{}"}).status_code, 400)
        self.assertEqual(self.get("/api/timetable").json()["schedule"]["courses"], schedule["courses"])

    def test_05_input_validation(self):
        for source in ("file:///C:/Windows/win.ini", "--help", "javascript:alert(1)"):
            self.assertEqual(self.post("/api/tasks", {"type": "file", "source": source}).status_code, 400)
        for filename in ("../../escape.bin", "C:\\escape.bin", "bad\nfile"):
            self.assertEqual(self.post("/api/tasks", {"type": "file", "source": self.source + "/sample.bin", "options": {"filename": filename}}).status_code, 400)
        for concurrency in (0, 6, True, "2"):
            self.assertEqual(self.post("/api/settings", {"concurrency": concurrency}).status_code, 400)
        self.assertEqual(self.post("/api/timetable/refresh", {"cookie": "", "year": "2026", "term": "3"}).status_code, 400)

    def test_06_restart_restores_queue_and_schedule(self):
        before = self.get("/api/state").json()["tasks"]
        schedule = self.get("/api/timetable").json()
        type(self).stop()
        type(self).start()
        self.assertEqual(self.get("/api/state").json()["tasks"], before)
        self.assertEqual(self.get("/api/timetable").json(), schedule)


if __name__ == "__main__":
    unittest.main()
