from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from omini_api import omini_account_service as accounts
from omini_api import omini_email_receiver as receiver
from omini_api import omini_sites as sites
from omini_api import xhhai_key_manager as key_manager


class FixtureHandler(BaseHTTPRequestHandler):
    events: list[dict[str, object]] = []
    fail_updates = False
    mailbox_sequence = 0
    mailbox_lock = threading.Lock()

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def body(self) -> dict[str, object]:
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")

    def event(self, body: dict[str, object] | None = None) -> None:
        type(self).events.append({"method": self.command, "path": self.path, "body": body,
                                  "auth": self.headers.get("X-Session") or self.headers.get("Authorization"),
                                  "receiver_auth": self.headers.get("X-API-Key")})

    def send_json(self, payload: object, status: int = 200) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        self.event()
        if self.path == "/a/settings":
            self.send_json({"code": 0, "data": {"registration_enabled": True, "email_verify_enabled": False}})
        elif self.path == "/b/config":
            self.send_json({"status": "yes", "payload": {"signup": {"open": True, "verify": True}}})
        elif self.path == "/a/groups":
            self.send_json({"code": 0, "data": [{"id": 1, "name": "首个分组"}, {"id": 2, "name": "第二分组"}]})
        elif self.path == "/b/groups":
            self.send_json({"status": "yes", "payload": {"rows": [{"gid": "g1", "title": "B首组"}]}})
        elif self.path == "/a/balance":
            self.send_json({"code": 0, "data": {"wallet": {"remaining": 3.25}}})
        elif self.path == "/b/balance":
            self.send_json({"status": "yes", "payload": {"wallet": {"remaining": 2.5}}})
        elif self.path == "/api/v1/mailboxes/available-domains":
            self.send_json(["mail.test"])
        elif self.path == "/m/no-code/api":
            self.send_json({"email": "no***@mail.test", "emails": [{
                "id": "same-message", "subject": "no verification here", "received_at": "2026-06-14T00:00:00Z",
            }]})
        elif self.path.startswith("/m/"):
            sent = any(event["path"] == "/b/send" for event in type(self).events)
            emails = [
                {"id": "older", "code": "111111", "subject": "old", "received_at": "2026-06-13T00:00:00Z"},
                {"id": "msg-1", "code": "618204", "subject": "Your verification code", "received_at": "2026-06-14T00:00:00Z"},
            ] if sent else []
            self.send_json({"email": "omini1@mail.test", "emails": emails})
        elif self.path == "/missing-groups":
            self.send_json({"code": 0, "data": {}})
        else:
            self.send_json({"ok": True})

    def do_POST(self) -> None:
        body = self.body()
        self.event(body)
        if self.path == "/a/register":
            if body.get("email") == "fail@example.com":
                self.send_json({"message": "fixture rejected"}, 400)
            else:
                self.send_json({"code": 0, "data": {"access_token": "a-access", "user": {"email": body.get("email")}}})
        elif self.path in {"/b/send", "/b/send-no-mail"}:
            self.send_json({"status": "yes", "payload": {"sent": True}})
        elif self.path == "/missing-send":
            self.send_json({"status": "no", "error": "send failed"}, 504)
        elif self.path == "/b/join":
            self.send_json({"status": "yes", "payload": {"auth": {"access": "b-access"}}})
        elif self.path == "/a/keys":
            self.send_json({"code": 0, "data": {"secret": "sk-a", "id": 9}})
        elif self.path == "/b/keys":
            self.send_json({"status": "yes", "payload": {"credential": {"secret": "sk-b", "kid": "key-b"}}})
        elif self.path == "/api/v1/mailboxes":
            with type(self).mailbox_lock:
                type(self).mailbox_sequence += 1
                mailbox_no = type(self).mailbox_sequence
            self.send_json({"email": f"omini{mailbox_no}@mail.test", "token": f"token-{mailbox_no}"})
        else:
            self.send_json({"code": 0, "data": {}})

    def do_PATCH(self) -> None:
        body = self.body()
        self.event(body)
        if type(self).fail_updates:
            self.send_json({"status": "no", "error": "update failed"}, 500)
        else:
            self.send_json({"status": "yes", "payload": {"updated": True}})


class MultisiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        FixtureHandler.events.clear()
        FixtureHandler.fail_updates = False
        FixtureHandler.mailbox_sequence = 0
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def profile_a(self) -> dict[str, object]:
        return {
            "id": "a", "name": "A", "home_url": self.base, "api_base": self.base, "enabled": True,
            "verification": {"mode": "auto"}, "key_workflow": {"create_requires_group": True},
            "auth": {"header": "X-Session", "template": "Token {access_token}"},
            "envelope": {"code_path": "code", "success_values": [0], "data_path": "data", "message_path": "message"},
            "operations": {
                "settings": {"method": "GET", "path": "/a/settings", "response": {"registration_enabled": "registration_enabled", "email_verify_enabled": "email_verify_enabled"}},
                "register": {"method": "POST", "path": "/a/register", "body_map": {"email": "email", "password": "password", "verification_code": "code"}, "response": {"access_token": "access_token", "user": "user"}},
                "groups": {"method": "GET", "path": "/a/groups", "auth": True, "response": {"items": "$", "id": "id", "name": "name"}},
                "create_key": {"method": "POST", "path": "/a/keys", "auth": True, "body_map": {"key_name": "name", "group_id": "group"}, "response": {"api_key": "secret", "id": "id"}},
                "balance": {"method": "GET", "path": "/a/balance", "auth": True, "response": {"balance": "wallet.remaining"}},
            },
        }

    def profile_b(self) -> dict[str, object]:
        return {
            "id": "b", "name": "B", "home_url": self.base, "api_base": self.base, "enabled": True,
            "verification": {"mode": "auto"}, "key_workflow": {"create_requires_group": False},
            "auth": {"header": "Authorization", "template": "Bearer {access_token}"},
            "envelope": {"code_path": "status", "success_values": ["yes"], "data_path": "payload"},
            "operations": {
                "settings": {"method": "GET", "path": "/b/config", "response": {"registration_enabled": "signup.open", "email_verify_enabled": "signup.verify"}},
                "send_verification_code": {"method": "POST", "path": "/b/send", "body_map": {"email": "mail"}},
                "register": {"method": "POST", "path": "/b/join", "body_map": {"email": "userMail", "password": "secret", "verification_code": "verifyCode"}, "response": {"access_token": "auth.access"}},
                "groups": {"method": "GET", "path": "/b/groups", "auth": True, "response": {"items": "rows", "id": "gid", "name": "title"}},
                "create_key": {"method": "POST", "path": "/b/keys", "auth": True, "body_map": {"key_name": "label"}, "response": {"api_key": "credential.secret", "id": "credential.kid"}},
                "update_key_group": {"method": "PATCH", "path": "/b/keys/{key_id}", "auth": True, "body_map": {"group_id": "group"}},
                "balance": {"method": "GET", "path": "/b/balance", "auth": True, "response": {"balance": "wallet.remaining"}},
            },
        }

    def receiver_config(self) -> dict[str, object]:
        return {"api_base": self.base, "api_key": "receiver-secret", "domain": "mail.test", "prefix": "omini", "random_length": 8, "domain_level": 3, "poll_interval": 0.01, "timeout": 1}

    def test_direct_full_workflow(self) -> None:
        result = accounts.register_batch(self.profile_a(), count=1, email_domain="@example.com", output_dir=self.root,
                                         account_kind="codex", route="默认", use_proxy=False, concurrency=1, key_name="codex")
        self.assertEqual(result, (1, 0, 0))
        record = json.loads(next(self.root.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(record["password"], "1234578Cao")
        self.assertEqual(record["api_key"], "sk-a")
        self.assertEqual(record["group"]["id"], 1)
        events = [event for event in FixtureHandler.events if str(event["path"]).startswith("/a/")]
        self.assertEqual([event["path"] for event in events], ["/a/settings", "/a/register", "/a/balance", "/a/groups", "/a/keys"])
        self.assertEqual(events[1]["body"]["password"], "1234578Cao")
        self.assertFalse(any(event["path"] == "/api/v1/mailboxes" for event in FixtureHandler.events))

    def test_receiver_and_key_then_update_group(self) -> None:
        result = accounts.register_batch(self.profile_b(), count=1, email_domain="example.com", output_dir=self.root,
                                         account_kind="claude", route="A", use_proxy=False, concurrency=1,
                                         key_name="claude", email_mode="receiver", receiver_config=self.receiver_config())
        self.assertEqual(result, (1, 0, 0))
        paths = [event["path"] for event in FixtureHandler.events]
        self.assertEqual(paths, ["/b/config", "/api/v1/mailboxes", "/b/send", "/m/token-1/api",
                                 "/b/join", "/b/balance", "/b/keys", "/b/groups", "/b/keys/key-b"])
        join = next(event for event in FixtureHandler.events if event["path"] == "/b/join")
        self.assertEqual(join["body"]["verifyCode"], "618204")
        self.assertEqual(join["body"]["secret"], "1234578Cao")
        record_text = next(self.root.glob("*.json")).read_text(encoding="utf-8")
        self.assertNotIn("618204", record_text)
        self.assertEqual(json.loads(record_text)["provisioning"]["status"], "complete")

    def test_receiver_uses_concurrent_single_mailbox_requests(self) -> None:
        self.assertEqual(len(receiver.create_mailboxes(self.receiver_config(), 51)), 51)
        requests = [event for event in FixtureHandler.events if event["path"] == "/api/v1/mailboxes"]
        self.assertEqual(len(requests), 51)
        self.assertTrue(all(event["body"]["domain_level"] == 3 for event in requests))
        self.assertTrue(all(event["body"]["domain"] == "mail.test" for event in requests))
        self.assertTrue(all(event["receiver_auth"] == "receiver-secret" for event in requests))

        FixtureHandler.events.clear()
        config = dict(self.receiver_config())
        config["domain"] = ""
        receiver.create_mailbox(config)
        request = next(event for event in FixtureHandler.events if event["path"] == "/api/v1/mailboxes")
        self.assertNotIn("domain", request["body"])

    def test_receiver_missing_key_and_timeout(self) -> None:
        missing = dict(self.receiver_config())
        missing["api_key"] = ""
        with self.assertRaises(sites.SiteError):
            receiver.create_mailboxes(missing, 1)
        short = dict(self.receiver_config())
        short["poll_interval"] = 0.005
        short["timeout"] = 0.02
        with self.assertRaisesRegex(sites.SiteError, "未收到可识别验证码"):
            receiver.wait_for_code(short, "no-code")
        self.assertEqual(receiver.extract_code({"code": "", "body": "Your code is 472901"}), "472901")

    def test_real_receiver_delivery_test_and_v1_config_migration(self) -> None:
        result = accounts.test_receiver_delivery(self.profile_b(), self.receiver_config(), use_proxy=False)
        self.assertEqual(result.code_length, 6)
        self.assertEqual(result.subject, "Your verification code")
        self.assertEqual(
            [event["path"] for event in FixtureHandler.events],
            ["/b/config", "/api/v1/mailboxes", "/m/token-1/api", "/b/send", "/m/token-1/api"],
        )

        old_path = self.root / "receiver.json"
        old_path.write_text(json.dumps({
            "version": 1, "api_base": self.base, "api_key": "x", "poll_interval": 1, "timeout": 2,
        }), encoding="utf-8")
        migrated = receiver.load_receiver_config(old_path)
        self.assertEqual(migrated["domain_level"], 3)
        self.assertEqual(json.loads(old_path.read_text(encoding="utf-8"))["version"], 2)

    def test_send_and_receive_failures_have_distinct_stages(self) -> None:
        send_profile = self.profile_b()
        send_profile["operations"]["send_verification_code"]["path"] = "/missing-send"
        with self.assertRaisesRegex(sites.SiteError, "send_verification_code"):
            accounts.test_receiver_delivery(send_profile, self.receiver_config(), use_proxy=False)

        FixtureHandler.events.clear()
        wait_profile = self.profile_b()
        wait_profile["operations"]["send_verification_code"]["path"] = "/b/send-no-mail"
        config = dict(self.receiver_config())
        config["timeout"] = 0.02
        config["poll_interval"] = 0.005
        with self.assertRaisesRegex(sites.SiteError, "wait_verification_code"):
            accounts.test_receiver_delivery(wait_profile, config, use_proxy=False)

    def test_update_failure_preserves_key_and_retry_reuses_it(self) -> None:
        FixtureHandler.fail_updates = True
        result = accounts.register_batch(self.profile_b(), count=1, email_domain="example.com", output_dir=self.root,
                                         account_kind="codex", route="默认", use_proxy=False, concurrency=1,
                                         key_name="codex", email_mode="receiver", receiver_config=self.receiver_config())
        self.assertEqual(result, (0, 1, 0))
        path = next(self.root.glob("*.json"))
        partial = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(partial["api_key"], "sk-b")
        self.assertEqual(partial["provisioning"]["stage"], "update_key_group")

        FixtureHandler.events.clear()
        FixtureHandler.fail_updates = False
        self.assertEqual(accounts.retry_provision(self.profile_b(), path, key_name="codex",
                                                  receiver_config=self.receiver_config(), use_proxy=False), "complete")
        self.assertFalse(any(event["method"] == "POST" and event["path"] == "/b/keys" for event in FixtureHandler.events))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["group"]["id"], "g1")

    def test_failures_isolated_and_partial_saved(self) -> None:
        profile = self.profile_a()
        profile["operations"]["groups"] = {"method": "GET", "path": "/missing-groups", "auth": True, "response": {"items": "rows"}}
        result = accounts.register_batch(profile, count=0, email_domain="example.com", output_dir=self.root,
                                         account_kind="codex", route="默认", use_proxy=False, concurrency=2,
                                         email_rows=[("ok@example.com", None), ("fail@example.com", None)], key_name="codex")
        self.assertEqual(result, (0, 1, 1))
        records = [json.loads(path.read_text(encoding="utf-8")) for path in self.root.glob("*.json")]
        registered = next(item for item in records if item["email"] == "ok@example.com")
        self.assertEqual(registered["provisioning"]["status"], "partial")
        self.assertEqual(registered["registration"]["access_token"], "a-access")

    def test_balance_failure_does_not_block_key_and_group(self) -> None:
        profile = self.profile_a()
        profile["operations"]["balance"] = {
            "method": "GET", "path": "/bad-balance", "auth": True,
            "response": {"balance": "missing.amount"},
        }
        result = accounts.register_batch(
            profile, count=1, email_domain="example.com", output_dir=self.root,
            account_kind="codex", route="默认", use_proxy=False, concurrency=1, key_name="codex",
        )
        self.assertEqual(result, (0, 1, 0))
        record = json.loads(next(self.root.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(record["api_key"], "sk-a")
        self.assertEqual(record["group"]["id"], 1)
        self.assertEqual(record["balance_check"]["status"], "failed")
        self.assertEqual(record["provisioning"]["stages"]["balance"], "failed")
        paths = [event["path"] for event in FixtureHandler.events]
        self.assertEqual(paths, ["/a/settings", "/a/register", "/bad-balance", "/a/groups", "/a/keys"])

    def test_manual_compatibility_domain_and_proxy(self) -> None:
        path = self.root / "account.json"
        path.write_text(json.dumps({"schema_version": 2, "site": {"id": "a"}, "email": "x@example.com",
                                    "password": "1234578Cao", "registration": {"access_token": "access"},
                                    "api_key": "sk-old", "key": {"key": "sk-old"}, "group": {}, "key_history": []}), encoding="utf-8")
        self.assertEqual(accounts.add_key(self.profile_a(), path, group_id=1, group_name="首个分组", key_name="manual",
                                          include_existing=True, use_proxy=False), "created")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["key_history"][0]["api_key"], "sk-old")
        self.assertEqual(accounts.query_balance(self.profile_a(), path, use_proxy=False)[0], 3.25)
        self.assertEqual(accounts.normalize_email_domain("@Example.COM"), "example.com")
        local = accounts.random_email("example.com").split("@", 1)[0]
        self.assertTrue(local.isdigit() and 8 <= len(local) <= 12)

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
        sock.close()
        self.assertFalse(sites.resolve_proxy("自动", "127.0.0.1", closed_port).use_proxy)
        with self.assertRaises(sites.SiteError):
            sites.resolve_proxy("强制代理", "127.0.0.1", closed_port)

    def test_xhhai_verification_field_and_old_profile_migration(self) -> None:
        default_profile = sites.default_xhhai_profile()
        self.assertEqual(
            default_profile["operations"]["register"]["body_map"]["verification_code"],
            "verify_code",
        )
        default_profile["operations"]["register"]["body_map"]["verification_code"] = "email_code"
        store_path = self.root / "sites.json"
        store_path.write_text(json.dumps({"version": 1, "sites": [default_profile]}), encoding="utf-8")
        migrated = sites.load_site_store(store_path)
        self.assertEqual(
            migrated["sites"][0]["operations"]["register"]["body_map"]["verification_code"],
            "verify_code",
        )

    def test_keyless_account_can_query_balance(self) -> None:
        path = self.root / "keyless.json"
        path.write_text(json.dumps({
            "schema_version": 2, "site": {"id": "a"}, "email": "keyless@example.com",
            "password": "1234578Cao", "registration": {"access_token": "access"},
            "api_key": "", "key": {}, "group": {}, "key_history": [],
        }), encoding="utf-8")
        balance, refreshed = accounts.query_balance(self.profile_a(), path, use_proxy=False)
        self.assertEqual(balance, 3.25)
        self.assertFalse(refreshed)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["balance_initial"], 3.25)
        self.assertEqual(saved["balance_cache"]["balance"], 3.25)
        self.assertEqual([event["path"] for event in FixtureHandler.events], ["/a/balance"])

        account = key_manager.Account(path, "keyless@example.com", "", "access", "refresh", "1234578Cao")
        key_manager.save_account_tokens(account, {"access_token": "access-2", "refresh_token": "refresh-2"})
        refreshed = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(refreshed["balance_initial"], 3.25)


if __name__ == "__main__":
    unittest.main()
