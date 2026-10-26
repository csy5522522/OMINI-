"""API members: real HTTP probes, pinned gateway routing and encrypted sync."""
import copy
import json
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import ai_assistants as core
from api_members import (API_PROTOCOLS, api_providers, models_text, normalize_base_url,
                         parse_models, probe_api_member, validate_api_member)
from account_data import AccountDataStore, merge_documents, validate_resource
from account_sync import read_assistants, write_assistants
from database import Database
from tests import test_ai_assistants as gateway


def member(protocol="openai", base="http://127.0.0.1:1"):
    return validate_api_member({"label": "Fixture " + protocol, "protocol": protocol,
        "base_url": base, "api_key": "fixture-private-key", "proxy": "direct",
        "headers": {"X-Fixture": "fixture-header-secret"},
        "models": [{"name": "upstream-model", "alias": "fixture-" + protocol}]})


class Upstream:
    def __enter__(self):
        owner = self
        self.seen, self.status, self.html = [], 200, False
        class Handler(BaseHTTPRequestHandler):
            def send(self, value):
                body = b"<html>wrong endpoint</html>" if owner.html else json.dumps(value).encode()
                self.send_response(owner.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if owner.status == 302: self.send_header("Location", owner.base + "/leak")
                self.end_headers(); self.wfile.write(body)
            def do_GET(self):
                owner.seen.append((self.path, dict(self.headers), None))
                self.send({"models": [{"name": "models/upstream-model"}]} if "v1beta" in self.path or "/publishers/google" in self.path else {"data": [{"id": "upstream-model"}]})
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.seen.append((self.path, dict(self.headers), data))
                path = self.path.split("?")[0]
                if path.endswith("/chat/completions"):
                    self.send({"id": "chatcmpl-fixture", "object": "chat.completion", "created": 1700000000,
                        "model": "upstream-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": "fixture reply"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                elif path.endswith("/messages"):
                    self.send({"id": "msg_fixture", "content": [{"type": "text", "text": "OK"}]})
                elif ":generateContent" in self.path:
                    self.send({"candidates": [{"content": {"parts": [{"text": "OK"}]}}]})
                elif path.endswith("/interactions"):
                    self.send({"id": "interaction_fixture", "outputs": [{"type": "text", "text": "OK"}]})
                else:
                    self.send({"id": "resp_fixture", "object": "response", "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}]})
            def log_message(self, *_): pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)


class ApiMemberTests(unittest.TestCase):
    def test_protocols_emit_the_pinned_binary_group_schema_and_disabled_keys_are_omitted(self):
        pool = core.default_settings()["pool"]
        pool["strategy"] = "fill-first"
        for protocol in API_PROTOCOLS:
            pool["api_members"]["api-" + protocol] = member(protocol)
            pool["member_rules"]["api-" + protocol] = {"priority": 50, "weight": 3}
        providers = api_providers(pool)
        self.assertEqual(len(providers), 8)
        for entries in providers.values():
            self.assertEqual(entries[0]["keys"][0]["weight"], 3)
            self.assertEqual(entries[0]["priority"], 50)
            self.assertEqual(entries[0]["models"][0]["name"], "upstream-model")
        pool["api_members"]["api-openai"]["disabled"] = True
        self.assertEqual(api_providers(pool)["openai-compatibility"], [])
        pool["strategy"] = "balanced"
        self.assertEqual(api_providers(pool)["codex"][0]["priority"], 0)

    def test_model_aliases_and_endpoint_normalization(self):
        models = parse_models("upstream => public\nmodel-b\n")
        self.assertEqual(models_text(models), "upstream => public\nmodel-b")
        self.assertEqual(normalize_base_url("https://example.test/v1/responses/", "responses"), "https://example.test/v1")
        self.assertEqual(normalize_base_url("https://example.test/api/v1/messages", "anthropic"), "https://example.test/api")
        for protocol in ("openai", "responses", "xai", "meta"):
            self.assertEqual(normalize_base_url("https://example.test", protocol), "https://example.test/v1")

    def test_invalid_urls_headers_models_and_protocols_are_rejected(self):
        for changes in ({"base_url": "file:///x"}, {"base_url": "https://user:pass@host/v1"},
                        {"base_url": "https://host/v1?key=secret"}, {"api_key": "secret\r\nx: 1"},
                        {"headers": {"X-Test": "a\nsecret"}}, {"headers": {"Host": "other"}},
                        {"models": [{"name": "model bad"}]}, {"protocol": []}, {"models": []}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_api_member({**member(), **changes})

    def test_all_protocol_probes_use_exact_upstream_model_and_authentication(self):
        with Upstream() as fixture:
            for protocol in API_PROTOCOLS:
                with self.subTest(protocol=protocol):
                    value = member(protocol, fixture.base)
                    result = probe_api_member(value, "http://127.0.0.1:1")
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(result["models"], ["upstream-model"])
                    result = probe_api_member(value, generate=True)
                    self.assertTrue(result["ok"], result)
                    path, headers, data = fixture.seen[-1]
                    self.assertEqual(headers["X-Fixture"], "fixture-header-secret")
                    if protocol in ("gemini", "interactions", "vertex"):
                        self.assertEqual(headers["x-goog-api-key"], value["api_key"])
                    elif protocol == "anthropic":
                        self.assertEqual(headers["x-api-key"], value["api_key"])
                    else:
                        self.assertEqual(headers["Authorization"], "Bearer " + value["api_key"])
                    if protocol in ("gemini", "vertex"):
                        self.assertIn("upstream-model:generateContent", path)
                    else:
                        self.assertEqual(data["model"], "upstream-model")

    def test_probe_rejects_401_html_and_redirects_without_leaking_credentials(self):
        with Upstream() as fixture:
            value = member(base=fixture.base)
            for status in (401, 403, 404, 429, 500, 302):
                fixture.status = status
                result = probe_api_member(value)
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], status)
                self.assertNotIn(value["api_key"], result["message"])
            self.assertEqual(len(fixture.seen), 6)
            fixture.status, fixture.html = 200, True
            self.assertFalse(probe_api_member(value)["ok"])
            value["models"] = []
            fixture.html = False
            self.assertTrue(probe_api_member(value)["ok"])

    def test_api_credentials_are_atomic_and_survive_encrypted_desktop_and_cloud_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = core.AssistantStore(root / "desktop")
            data = copy.deepcopy(store.data)
            data["pool"]["api_members"]["api-one"] = member()
            store.save(data)
            self.assertNotIn("fixture-private-key", store.path.read_text())
            snapshot = read_assistants(store.root)
            write_assistants(root / "second", snapshot)
            self.assertEqual(read_assistants(root / "second")["pool"]["api_members"], snapshot["pool"]["api_members"])
            db = Database(root / "accounts.db", local=True)
            cloud = AccountDataStore(db, root / "cloud")
            baseline = cloud.exchange("alice", "assistants", 0, snapshot)
            local, remote = copy.deepcopy(snapshot), copy.deepcopy(snapshot)
            local["pool"]["api_members"]["api-one"]["api_key"] = "local-key"
            remote["pool"]["api_members"]["api-one"]["base_url"] = "https://remote.test/v1"
            cloud.exchange("alice", "assistants", baseline["revision"], remote)
            merged = cloud.exchange("alice", "assistants", baseline["revision"], local)
            self.assertEqual(merged["data"]["pool"]["api_members"]["api-one"], remote["pool"]["api_members"]["api-one"])
            self.assertIn("pool/api_members/api-one", merged["conflicts"])
            removed = copy.deepcopy(merged["data"]); removed["pool"]["api_members"] = {}
            cloud.exchange("alice", "assistants", merged["revision"], removed)
            stale = cloud.exchange("alice", "assistants", 0, snapshot)
            self.assertEqual(stale["data"]["pool"]["api_members"], {})

    def test_provider_failure_rolls_back_configuration_and_routing(self):
        pool = core.default_settings()["pool"]
        pool["api_members"]["api-one"] = member()
        calls = []
        previous = {"openai-compatibility": []}
        client = core.PoolClient(pool)
        def request(method, path, **kwargs):
            calls.append((method, path, kwargs.get("json")))
            if method == "GET":
                return {"files": []} if path == "/credentials" else {"api-keys": previous} if path == "/config" else {"strategy": "round-robin"}
            if method == "PATCH" and path == "/config/routing": raise ValueError("fixture failure")
            return {}
        with patch.object(client, "request", side_effect=request), self.assertRaises(ValueError):
            client.apply_scheduling(pool)
        self.assertEqual(calls[-1], ("PUT", "/config/api-keys", previous))


@unittest.skipUnless(sys.platform == "win32" and gateway.ENGINE, "Pinned Windows gateway")
class LiveApiMemberTests(unittest.TestCase):
    setUp = gateway.LiveGatewayTests.setUp
    start = gateway.LiveGatewayTests.start
    tearDown = gateway.LiveGatewayTests.tearDown

    def test_real_all_protocol_models_hot_updates_and_oauth_coexistence(self):
        self.start()
        source = self.root / "fixture.json"
        core.atomic_json(source, {"type": "codex", "access_token": "fixture-access", "refresh_token": "fixture-refresh", "account_id": "fixture-member"})
        self.client.import_account(source)
        for protocol in API_PROTOCOLS:
            self.pool["api_members"]["api-" + protocol] = member(protocol)
        self.client.apply_scheduling(self.pool)
        models = self.client.request("GET", "/models", management=False)["data"]
        self.assertTrue({"fixture-" + protocol for protocol in API_PROTOCOLS}.issubset({m["id"] for m in models}))
        self.assertEqual(len(self.client.accounts()), 1)
        self.assertEqual(len(self.client.request("GET", "/config")["api-keys"]), 8)
        self.pool["api_members"] = {}
        self.client.apply_scheduling(self.pool)
        self.assertFalse(any(m["id"].startswith("fixture-") for m in self.client.request("GET", "/models", management=False)["data"]))
        self.assertEqual(len(self.client.accounts()), 1)

    def test_real_alias_routing_weights_and_responses_translation(self):
        import requests
        with Upstream() as fixture:
            self.pool.update(strategy="weighted-round-robin", session_affinity=False)
            for index, weight in ((1, 1), (2, 3)):
                name = "api-" + str(index)
                value = member(base=fixture.base)
                value["api_key"] = "fixture-key-" + str(index)
                self.pool["api_members"][name] = value
                self.pool["member_rules"][name] = {"priority": 0, "weight": weight}
            self.config = core.pool_config(self.root, self.pool)
            self.client = core.PoolClient(self.pool)
            self.start()
            with requests.Session() as session:
                session.trust_env = False
                for _ in range(8):
                    response = session.post(self.client.base + "/v1/responses", headers={"Authorization": "Bearer " + self.pool["api_key"]},
                        json={"model": "fixture-openai", "input": [{"role": "user", "content": "test"}]}, timeout=10)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertIn("fixture reply", response.text)
            self.assertTrue(all(row[2]["model"] == "upstream-model" for row in fixture.seen))
            counts = Counter(row[1]["Authorization"] for row in fixture.seen)
            self.assertEqual(counts, {"Bearer fixture-key-1": 2, "Bearer fixture-key-2": 6})


class ApiMemberPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.qt = QApplication.instance() or QApplication([])

    def test_offline_crud_controls_and_restart_preserve_key_and_models(self):
        from ai_assistants_page import AiAssistantsPage, ApiMemberDialog
        with tempfile.TemporaryDirectory() as directory:
            page = AiAssistantsPage(Path(directory))
            try:
                self.assertTrue(page.save_api_member("api-one", member()))
                page.table.selectRow(0)
                self.assertTrue(page.edit_api_button.isEnabled())
                self.assertTrue(page.check_api_button.isEnabled())
                self.assertFalse(page.quota_button.isEnabled())
                page.toggle_account()
                self.assertTrue(page.store.data["pool"]["api_members"]["api-one"]["disabled"])
                dialog = ApiMemberDialog(page, member("responses"))
                self.assertEqual(dialog.member_value()["models"][0]["alias"], "fixture-responses")
                dialog.deleteLater()
                page.save_session(); page.stop()
                reloaded = AiAssistantsPage(Path(directory))
                try:
                    self.assertEqual(len(reloaded.accounts), 1)
                    self.assertTrue(reloaded.accounts[0]["api_member"])
                    reloaded.remove_api_member("api-one")
                    self.assertEqual(reloaded.accounts, [])
                finally:
                    reloaded.stop(); reloaded.deleteLater()
            finally:
                page.stop(); page.deleteLater(); self.qt.processEvents()

    def test_real_line_check_and_model_validation_persist_and_configuration_changes_invalidate(self):
        from PySide6.QtTest import QTest
        from ai_assistants_page import AiAssistantsPage, ApiMemberDialog
        def wait_idle(page):
            deadline = time.monotonic() + 5
            while page.worker is not None and time.monotonic() < deadline: QTest.qWait(10)
            self.assertIsNone(page.worker)
        with Upstream() as fixture, tempfile.TemporaryDirectory() as directory:
            page = AiAssistantsPage(Path(directory))
            try:
                page.pool_proxy.clear()
                page.save_api_member("api-one", member(base=fixture.base))
                page.table.selectRow(0)
                page.check_api_member()
                wait_idle(page)
                self.assertIn("通过", page.table.item(0, 9).text())
                self.assertIsNone(fixture.seen[-1][2], "Checking the line only reads models")
                page.check_api_member(True)
                wait_idle(page)
                self.assertEqual(fixture.seen[-1][2]["model"], "upstream-model")
                self.assertIn("模型调用通过", page.table.item(0, 9).toolTip())
                dialog = ApiMemberDialog(page, member(base=fixture.base))
                dialog.probe(False)
                self.assertFalse(dialog.api_key.isEnabled())
                wait_idle(page)
                self.assertTrue(dialog.api_key.isEnabled())
                self.assertEqual(dialog.member_value()["models"], [{"name": "upstream-model", "alias": "fixture-openai"}])
                dialog.reject(); dialog.deleteLater()
                page.stop()
                restarted = AiAssistantsPage(Path(directory))
                try:
                    self.assertIn("通过", restarted.table.item(0, 9).text())
                    data = copy.deepcopy(restarted.store.data)
                    data["pool"]["api_members"]["api-one"]["api_key"] = "rotated-fixture-key"
                    restarted.store.save(data)
                    restarted.apply_cloud()
                    self.assertEqual(restarted.table.item(0, 9).text(), "未检查")
                finally:
                    restarted.stop(); restarted.deleteLater()
            finally:
                page.stop(); page.deleteLater(); self.qt.processEvents()
