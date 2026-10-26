"""Proxy isolation, native launch quoting, and real local gateway regressions."""
from __future__ import annotations

import base64
import copy
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import ai_assistants as core


class ProfileTests(unittest.TestCase):
    def test_proxy_modes_isolate_children_and_preserve_parent(self):
        base = {"PATH": "fixture", "http_proxy": "http://old:1", "HTTPS_PROXY": "http://old:2",
                "ALL_PROXY": "socks5://old:3", "no_proxy": "*"}
        original = dict(base)
        profile = {"mode": "custom", "proxy": "http://127.0.0.1:7890", "no_proxy": "school.local"}
        result = core.proxy_environment(profile, base)
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            self.assertEqual(result[key], profile["proxy"])
        self.assertIn("127.0.0.1", result["NO_PROXY"])
        self.assertIn("school.local", result["NO_PROXY"])
        self.assertNotIn("*", result["NO_PROXY"])
        self.assertEqual(base, original)
        direct = core.proxy_environment({"mode": "direct"}, base)
        self.assertFalse(any(k.lower() in {"http_proxy", "https_proxy", "all_proxy"} for k in direct))
        self.assertEqual(core.proxy_environment({"mode": "inherit"}, base), original)

    def test_proxy_validation_and_claude_http_requirement(self):
        for value in ("http://127.0.0.1:7890", "https://user:pass@proxy.local:8443", "socks5://[::1]:7891"):
            self.assertEqual(core.validate_proxy(value), value)
        for value in ("127.0.0.1:7890", "http://localhost", "http://localhost:0", "file:///tmp:80",
                      "http://host:65536", "http://host:80/path", "http://host:80#fragment"):
            with self.assertRaises(ValueError):
                core.validate_proxy(value)
        with self.assertRaisesRegex(ValueError, "HTTP/HTTPS"):
            core.validate_proxy("socks5://localhost:7890", assistant="claude")

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI")
    def test_encrypted_restart_preserves_keys_and_separate_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            store = core.AssistantStore(Path(directory))
            data = copy.deepcopy(store.data)
            data["profiles"]["claude"]["proxy"] = "http://member:private-pass@localhost:7890"
            data["profiles"]["gemini"]["mode"] = "direct"
            store.save(data)
            content = store.path.read_text()
            for secret in ("private-pass", data["pool"]["api_key"], data["pool"]["management_key"]):
                self.assertNotIn(secret, content)
            reloaded = core.AssistantStore(Path(directory))
            self.assertEqual(reloaded.data, data)

    def test_codex_config_is_separate_and_parses_as_toml(self):
        with tempfile.TemporaryDirectory(prefix="助手 & profile ") as directory:
            pool = core.default_settings()["pool"]
            pool["model"] = "fixture-model"
            home = core.pool_codex_home(Path(directory), pool)
            config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(config["model_provider"], "omini_pool")
            self.assertEqual(config["model_providers"]["omini_pool"]["base_url"], "http://127.0.0.1:8317/v1")
            self.assertNotIn(pool["api_key"], (home / "config.toml").read_text(encoding="utf-8"))

    def test_auth_import_converts_native_json_and_deduplicates_account(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "auth.json"
            claims = base64.urlsafe_b64encode(json.dumps({"email": "fixture@example.com", "exp": 2000000000}).encode()).decode().rstrip("=")
            data = {"tokens": {"access_token": "fixture-access", "refresh_token": "fixture-refresh",
                               "id_token": "header." + claims + ".sig", "account_id": "fixture-id"}}
            core.atomic_json(source, data)
            name, raw = core.normalize_codex_credential(source)
            normalized = json.loads(raw)
            self.assertEqual(normalized["type"], "codex")
            self.assertEqual(normalized["email"], "fixture@example.com")
            self.assertEqual(normalized["account_id"], "fixture-id")
            data["tokens"]["access_token"] = "changed-access"
            core.atomic_json(source, data)
            self.assertEqual(core.normalize_codex_credential(source)[0], name)
            for bad in ({"tokens": {}}, [], {"type": "claude", "access_token": "x", "refresh_token": "y"},
                        {"access_token": {}, "refresh_token": "y"}):
                core.atomic_json(source, bad)
                with self.assertRaises(ValueError):
                    core.normalize_codex_credential(source)

    def test_redaction_and_powershell_literal_quotes(self):
        result = core.redact('https://user:password@proxy:7890 access_token="private-token" Bearer abc.def key-secret', ["key-secret"])
        for value in ("user:password", "private-token", "abc.def", "key-secret"):
            self.assertNotIn(value, result)
        self.assertEqual(core.ps_quote("a'$(whoami)`b"), "'a''$(whoami)`b'")

    def test_installer_rejects_digest_before_writing_executable(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def raise_for_status(self): pass
            def iter_content(self, _): yield b"tampered archive"
        with tempfile.TemporaryDirectory() as directory, patch.object(core.requests.Session, "get", return_value=Response()):
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                core.install_engine(Path(directory))
            self.assertFalse((Path(directory) / "cli-proxy-api.exe").exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows native launcher")
    def test_actual_powershell_launch_preserves_arguments_and_proxy(self):
        with tempfile.TemporaryDirectory(prefix="助手 & ' $ launch ") as directory:
            root = Path(directory)
            script = root / "inspect child.py"
            result = root / "result.json"
            script.write_text("import os,sys,json;from pathlib import Path;Path(sys.argv[1]).write_text(json.dumps({'args':sys.argv[2:],'proxy':os.environ.get('HTTPS_PROXY'),'home':os.environ.get('CODEX_HOME'),'cwd':os.getcwd()}),encoding='utf-8')", encoding="utf-8")
            args = [str(script), str(result), 'literal "quoted" value', "a&b%PATH%$(whoami)", "中文 空格"]
            profile = core.default_settings()["profiles"]["codex"]
            profile.update(program=sys.executable, cwd=str(root), args=args)
            pool = core.default_settings()["pool"]
            pool["model"] = "fixture-model"
            real_popen = subprocess.Popen
            launched = []
            def hidden(command, **kwargs):
                command = [item for item in command if item != "-NoExit"]
                kwargs.update(creationflags=subprocess.CREATE_NO_WINDOW, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                process = real_popen(command, **kwargs)
                launched.append(process)
                return process
            with patch.object(core.subprocess, "Popen", side_effect=hidden):
                core.launch_assistant("codex", profile, root=root, pool=pool)
            stdout, stderr = launched[0].communicate(timeout=15)
            self.assertEqual(launched[0].returncode, 0, stdout + stderr)
            data = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(data["args"], args[2:])
            self.assertEqual(data["proxy"], profile["proxy"])
            self.assertEqual(Path(data["home"]), root / "codex-pool")
            self.assertEqual(list((root / "launchers").iterdir()), [])

    def test_desktop_pool_launch_passes_isolated_home_and_electron_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "Codex.exe"
            executable.write_bytes(b"fixture")
            profile = core.default_settings()["profiles"]["codex"]
            profile.update(program=str(executable), desktop=True, cwd=directory)
            pool = core.default_settings()["pool"]
            pool["model"] = "fixture-model"
            with patch.object(core.subprocess, "Popen") as launch:
                launch.return_value.pid = 123
                self.assertEqual(core.launch_assistant("codex", profile, root=root, pool=pool), 123)
            args, kwargs = launch.call_args
            self.assertIn("--proxy-server=http://127.0.0.1:7890", args[0])
            self.assertEqual(kwargs["env"]["OMINI_CODEX_POOL_KEY"], pool["api_key"])
            self.assertEqual(kwargs["env"]["CODEX_HOME"], str(root / "codex-pool"))


class ManagementHttpTests(unittest.TestCase):
    def test_network_check_uses_selected_proxy_and_treats_401_as_transport_only(self):
        seen = []
        class Proxy(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
            def log_message(self, *_): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            profile = core.default_settings()["profiles"]["codex"]
            profile["proxy"] = f"http://127.0.0.1:{server.server_port}"
            with patch.dict(core.ASSISTANTS, {"codex": ("Fixture", "http://fixture.invalid/check")}), \
                    patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": "*"}):
                result = core.test_connection("codex", profile)
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], 401)
            self.assertIn("另行验证", result["message"])
            self.assertEqual(seen, ["http://fixture.invalid/check"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_management_bypasses_inherited_proxy_and_rejects_redirects(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                if self.path.endswith("/redirect"):
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:1/credential-leak")
                    self.end_headers()
                else:
                    body = b'{"files":[]}'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            def log_message(self, *_): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            pool = core.default_settings()["pool"]
            pool["port"] = server.server_port
            with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}):
                client = core.PoolClient(pool)
                self.assertEqual(client.accounts(), [])
                with self.assertRaisesRegex(ValueError, "302"):
                    client.request("GET", "/redirect")
            self.assertEqual(len(seen), 2)
            self.assertEqual(seen[0], ("/v8/management/credentials", "Bearer " + pool["management_key"]))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


ENGINE = core.engine_path(Path(tempfile.gettempdir()) / "no-personal-ai-data")


@unittest.skipUnless(sys.platform == "win32" and ENGINE, "Pinned Windows CLIProxyAPI engine")
class LiveGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="助手 gateway ")
        self.root = Path(self.temporary.name)
        self.pool = core.default_settings()["pool"]
        self.pool["proxy"] = ""
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.pool["port"] = listener.getsockname()[1]
        self.config = core.pool_config(self.root, self.pool)
        self.config_path = self.root / "config.yaml"
        self.process = None
        self.log_handle = None
        self.client = core.PoolClient(self.pool)

    def start(self):
        core.atomic_json(self.config_path, self.config)
        if self.log_handle is None:
            self.log_handle = (self.root / "gateway.log").open("ab")
        self.process = subprocess.Popen([str(ENGINE), "--config", str(self.config_path), "--local-model"],
                                        cwd=self.root, stdout=self.log_handle, stderr=subprocess.STDOUT,
                                        creationflags=subprocess.CREATE_NO_WINDOW,
                                        env=core.proxy_environment({"mode": "direct"}))
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail((self.root / "gateway.log").read_text(encoding="utf-8", errors="replace"))
            try:
                self.client.accounts()
                return
            except ValueError:
                time.sleep(.1)
        self.fail("Local gateway did not become ready")

    def tearDown(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.communicate(timeout=5)
        if self.log_handle is not None:
            self.log_handle.close()
        self.temporary.cleanup()

    def test_real_import_status_models_restart_and_delete(self):
        self.start()
        self.assertEqual(self.client.accounts(), [])
        source = self.root / "fixture.json"
        core.atomic_json(source, {"type": "codex", "email": "fixture@example.com", "account_id": "fixture-member",
                                  "access_token": "fixture-access", "refresh_token": "fixture-refresh",
                                  "expired": "2099-01-01T00:00:00Z", "last_refresh": "2099-01-01T00:00:00Z"})
        self.client.import_account(source)
        rows = self.client.accounts()
        self.assertEqual(len(rows), 1)
        name = rows[0]["name"]
        self.assertTrue(self.client.models())
        self.client.set_disabled(name, True)
        self.assertTrue(self.client.accounts()[0]["disabled"])
        self.process.terminate()
        self.process.communicate(timeout=5)
        self.start()
        self.assertTrue(self.client.accounts()[0]["disabled"])
        self.client.remove(name)
        self.assertEqual(self.client.accounts(), [])

    def test_real_login_url_poll_cancel_without_opening_browser(self):
        self.start()
        data = self.client.begin_login()
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["url"].startswith("https://auth.openai.com/"))
        self.assertEqual(self.client.login_status(data["state"])["status"], "wait")
        self.assertTrue(self.client.cancel_login(data["state"])["cancelled"])
        self.assertEqual(self.client.accounts(), [])

    def test_real_round_robin_responses_and_session_affinity(self):
        seen = []
        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append(self.headers.get("Authorization"))
                body = json.dumps({"id": "chatcmpl-fixture", "object": "chat.completion", "created": 1700000000,
                                   "model": "fixture-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": "fixture reply"}, "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.config["api-keys"] = {"openai-compatibility": [{"name": "local-fixture",
            "base-url": f"http://127.0.0.1:{server.server_port}/v1", "keys": [{"api-key": "member-A"}, {"api-key": "member-B"}],
            "models": [{"name": "fixture-model", "alias": "fixture-model"}]}]}
        try:
            self.start()
            import requests
            with requests.Session() as session:
                session.trust_env = False
                for key in ("conversation-a", "conversation-b", "conversation-a"):
                    response = session.post(self.client.base + "/v1/responses", headers={
                        "Authorization": "Bearer " + self.pool["api_key"], "X-Session-ID": key},
                        json={"model": "fixture-model", "input": [{"role": "user", "content": "test"}]}, timeout=10)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertIn("fixture reply", response.text)
            self.assertEqual(len(seen), 3)
            self.assertNotEqual(seen[0], seen[1])
            self.assertEqual(seen[0], seen[2])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


class PageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        from ai_assistants_page import AiAssistantsPage
        self.temporary = tempfile.TemporaryDirectory(prefix="助手 page ")
        self.page = AiAssistantsPage(Path(self.temporary.name))

    def tearDown(self):
        self.page.stop()
        if self.page.worker:
            self.page.worker.wait(15000)
        self.qt.processEvents()
        self.page.deleteLater()
        self.qt.processEvents()
        self.temporary.cleanup()

    def test_page_profiles_save_independently_and_pool_is_opt_in(self):
        self.assertEqual(self.page.tabs.count(), 7)
        self.assertFalse(self.page.is_running)
        self.assertFalse(self.page.login_button.isEnabled())
        codex, gemini = self.page.profiles["codex"], self.page.profiles["gemini"]
        codex.proxy.setText("http://127.0.0.1:7897")
        self.page.save_profile("codex")
        self.assertEqual(self.page.store.data["profiles"]["codex"]["proxy"], "http://127.0.0.1:7897")
        self.assertEqual(gemini.proxy.text(), "http://127.0.0.1:7890")
        self.page.tabs.setCurrentIndex(4)
        self.assertFalse(self.page.is_running)

    @unittest.skipUnless(sys.platform == "win32" and ENGINE, "Pinned Windows CLIProxyAPI engine")
    def test_live_page_start_stop_cleanup_and_busy_shutdown(self):
        from PySide6.QtTest import QTest
        self.page.pool_proxy.clear()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.page.port.setValue(listener.getsockname()[1])
        self.page.start_pool()
        deadline = time.monotonic() + 15
        while (not self.page.ready or self.page.worker is not None) and time.monotonic() < deadline:
            QTest.qWait(25)
        self.assertTrue(self.page.ready, self.page.status.text() + self.page.log.toPlainText())
        self.assertIsNone(self.page.worker, "Wait for the startup worker before checking enabled controls")
        runtime = Path(self.page.runtime.name)
        self.assertTrue(self.page.login_button.isEnabled())
        self.page.tabs.setCurrentIndex(0)
        self.assertTrue(self.page.is_running)
        self.page.run_action(lambda worker: (time.sleep(.2), "finished")[-1], lambda _: self.fail("Result after stop"))
        self.page.stop()
        if self.page.worker:
            self.page.worker.wait(2000)
        self.qt.processEvents()
        self.assertFalse(self.page.is_running)
        self.assertFalse(runtime.exists())
        self.assertFalse((self.page.root / "pool.lock").exists())


if __name__ == "__main__":
    unittest.main()
