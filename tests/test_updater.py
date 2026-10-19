"""Real local HTTP transfer, integrity failures, private auth and update UI actions."""
import dataclasses
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from updater import (GitHubUpdater, Installer, TokenStore, UpdateCancelled, UpdateError,
                     launch_installer, select_release, verify_installer, version_tuple)
from updater_page import UpdatePanel

PAYLOAD = b"MZ" + b"verified-installer-fixture" * 12000
HASH = hashlib.sha256(PAYLOAD).hexdigest()
NAME = "Omini-Toolbox-1.1.2-Setup.exe"


def release_data():
    return {"tag_name": "v1.1.2", "body": "检查更新与下载安装", "draft": False, "prerelease": False,
            "assets": [{"id": 1, "name": NAME, "state": "uploaded", "size": len(PAYLOAD), "digest": "sha256:" + HASH},
                       {"id": 2, "name": NAME + ".sha256", "state": "uploaded", "size": 100}]}


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.server.seen.append((self.path, self.headers.get("Authorization", "")))
        status = self.server.status
        if status:
            self.send_response(status)
            self.end_headers()
            return
        if self.path.endswith("/latest"):
            body = json.dumps(self.server.release).encode()
            content_type = "application/json"
        elif self.path.endswith("/assets/2"):
            body = (self.server.hash + "  " + NAME + "\n").encode()
            content_type = "application/octet-stream"
        elif self.path.endswith("/assets/1") and self.server.redirect:
            self.send_response(302)
            self.send_header("Location", self.server.redirect)
            self.end_headers()
            return
        else:
            body = self.server.payload
            content_type = "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionResetError, BrokenPipeError):
            pass


class UpdaterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        cls.qt.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="omini-update-test-")
        self.root = Path(self.temp.name)
        self.server = self.make_server()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.client = GitHubUpdater("1.1.1", "private-token-fixture", self.url)

    def make_server(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        server.daemon_threads = True
        server.seen = []
        server.status = None
        server.release = release_data()
        server.payload = PAYLOAD
        server.hash = HASH
        server.redirect = None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def tearDown(self):
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()
        self.qt.processEvents()

    def wait_panel(self, page):
        deadline = time.monotonic() + 8
        while page.worker and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertIsNone(page.worker)

    def test_semantic_versions_stable_only_and_no_downgrade(self):
        self.assertGreater(version_tuple("v1.1.10"), version_tuple("1.1.9"))
        with self.assertRaises(UpdateError):
            version_tuple("v1.2.3-rc1")
        for current in ("1.1.2", "2.0.0"):
            client = GitHubUpdater(current, api_root=self.url)
            try:
                self.assertIsNone(client.check())
            finally:
                client.close()
        data = release_data()
        data["prerelease"] = True
        with self.assertRaises(UpdateError):
            select_release(data)

    def test_authenticated_check_download_hash_and_cache(self):
        release = self.client.check()
        progress = []
        artifact = self.client.download(release, self.root, progress=lambda a, b: progress.append((a, b)))
        self.assertEqual(artifact.path.read_bytes(), PAYLOAD)
        self.assertEqual(progress[-1], (len(PAYLOAD), len(PAYLOAD)))
        self.assertTrue(all(auth == "Bearer private-token-fixture" for _, auth in self.server.seen))
        asset_calls = sum(path.endswith("/assets/1") for path, _ in self.server.seen)
        self.client.download(release, self.root)
        self.assertEqual(sum(path.endswith("/assets/1") for path, _ in self.server.seen), asset_calls)
        self.assertFalse(list(self.root.rglob("*.part")))

    def test_checksum_fallback_and_failures_never_create_executable(self):
        release = self.client.check()
        fallback = dataclasses.replace(release, digest="")
        self.assertEqual(self.client.download(fallback, self.root / "fallback").sha256, HASH)
        self.server.hash = "0" * 64
        with self.assertRaisesRegex(UpdateError, "不一致"):
            self.client.download(release, self.root / "bad-checksum")
        self.server.hash = HASH
        self.server.payload = b"MZ" + b"bad" * 100
        with self.assertRaisesRegex(UpdateError, "校验未通过"):
            self.client.download(release, self.root / "corrupt")
        self.assertFalse(list((self.root / "corrupt").rglob("*.exe")))
        self.assertFalse(list(self.root.rglob("*.part")))

    def test_cancel_during_transfer_cleans_partial_file(self):
        release = self.client.check()
        cancel = threading.Event()
        with self.assertRaises(UpdateCancelled):
            self.client.download(release, self.root, cancel, lambda *_: cancel.set())
        self.assertFalse(list(self.root.rglob("*.exe")))
        self.assertFalse(list(self.root.rglob("*.part")))

    def test_storage_redirect_strips_token_and_untrusted_redirect_is_blocked(self):
        storage = self.make_server()
        storage_url = f"http://localhost:{storage.server_port}/download"
        try:
            self.server.redirect = storage_url
            validate = self.client._valid_url
            with patch.object(self.client, "_valid_url", side_effect=lambda url: url == storage_url or validate(url)):
                self.client.download(self.client.check(), self.root)
            self.assertEqual(storage.seen, [("/download", "")])
            self.server.redirect = "https://example.com/untrusted.exe"
            with self.assertRaisesRegex(UpdateError, "官方分发域名"):
                self.client.download(self.client.check(), self.root / "untrusted")
        finally:
            storage.shutdown()
            storage.server_close()

    def test_private_auth_errors_do_not_include_token(self):
        for status in (401, 403, 404, 429):
            self.server.status = status
            with self.subTest(status=status), self.assertRaises(UpdateError) as context:
                self.client.check()
            self.assertNotIn("private-token-fixture", str(context.exception))

    def test_encrypted_token_restart_and_clear(self):
        path = self.root / "credentials.json"
        store = TokenStore(path)
        store.save("private-token-fixture", True)
        self.assertNotIn("private-token-fixture", path.read_text())
        self.assertEqual(TokenStore(path).token, "private-token-fixture")
        store.save("", False)
        self.assertFalse(path.exists())

    def test_tampered_installer_is_not_launched(self):
        artifact = self.client.download(self.client.check(), self.root)
        with patch("updater.subprocess.Popen") as launch:
            launch_installer(artifact)
            self.assertEqual(launch.call_args.args[0][0], str(artifact.path))
            self.assertEqual(launch.call_args.kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
            artifact.path.write_bytes(b"MZtampered")
            with self.assertRaises(UpdateError):
                launch_installer(artifact)
            self.assertEqual(launch.call_count, 1)

    def test_buttons_check_download_and_install_handoff(self):
        def local_client(version, token):
            return GitHubUpdater(version, token, self.url)
        page = UpdatePanel("1.1.1", self.root / "ui")
        page.show()
        requested = []
        page.install_requested.connect(requested.append)
        try:
            with patch("updater_page.GitHubUpdater", side_effect=local_client):
                page.token.setText("private-token-fixture")
                page.check_button.click()
                self.wait_panel(page)
                self.assertTrue(page.download_button.isEnabled())
                self.assertIn("1.1.2", page.status.text())
                page.download_button.click()
                self.wait_panel(page)
                self.assertTrue(page.install_button.isEnabled())
                page.install_button.click()
                self.wait_panel(page)
                self.assertEqual(len(requested), 1)
                verify_installer(requested[0])
        finally:
            page.stop()
            if page.worker:
                page.worker.wait(30000)
            page.close()
            page.deleteLater()


if __name__ == "__main__":
    unittest.main()
