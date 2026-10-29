"""Real multi-device HTTP sync, encrypted persistence and credential deletion."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from account_client import AccountClient, DesktopSession
from account_data import AccountDataStore, validate_resource
from account_sync import DesktopAccountSync, SyncWorker, read_assistants, write_assistants, restore_conflict_fields
from assistant_data import OperationHistory
from database import Database
import tests.test_memos as memo_fixtures


def credential(identity="one"):
    return {"type": "codex", "email": identity + "@example.test", "access_token": "access-private-" + identity,
            "refresh_token": "refresh-private-" + identity, "disabled": False}


def schedule(title="Math"):
    return {"schema": 1, "year": "2026", "term": "3", "updated_at": "2026-10-22T10:00:00",
            "period_times": {}, "courses": [{"name": title, "weekday": 1, "periods": [1, 2], "weeks": [1, 2],
                "teacher": "Teacher", "location": "A101", "week_text": "1-2", "campus": "", "credits": "", "class_name": ""}]}


class AutomaticSyncTests(unittest.TestCase):
    def test_automatic_snapshot_does_not_read_or_save_assistant_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            page = SimpleNamespace(save_session=lambda: self.fail("Automatic sync saved assistant state"))
            state = SimpleNamespace(root=Path(temporary), window=SimpleNamespace(settings={}, ai_assistants_page=page))
            with patch("account_sync.read_assistants", side_effect=AssertionError("History was read")):
                self.assertEqual(set(DesktopAccountSync.snapshot(state)), {"preferences", "schedule"})
            state.window.ai_assistants_page = None
            with patch("account_sync.read_assistants", return_value={"members": {}}) as read:
                self.assertIn("assistants", DesktopAccountSync.snapshot(state, include_assistants=True))
                read.assert_called_once()

    def test_worker_requests_only_its_snapshot_resources(self):
        for resources in (("preferences", "schedule"), ("preferences", "schedule", "assistants")):
            snapshot = {name: {"fixture": name} for name in resources}
            ack = {name: {"revision": 2, "data": value} for name, value in snapshot.items()}
            worker = SyncWorker("http://127.0.0.1:1", "fixture", snapshot, ack, 1)
            with patch.object(AccountClient, "api", return_value={"resources": {name: {"unchanged": True} for name in resources}}) as api:
                worker.run()
            self.assertFalse(worker.error)
            self.assertEqual(set(worker.result["resources"]), set(resources))
            self.assertEqual(api.call_count, 1)
            self.assertIn("resources=" + ",".join(resources), api.call_args.args[0])

    def test_old_ack_migrates_once_and_background_avoids_assistant_file(self):
        from assistant_data import read_protected, write_protected
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = SimpleNamespace(root=root)
            legacy = {"preferences": {"revision": 3, "data": {}}, "assistants": {"revision": 7, "data": {"members": {}}}}
            write_protected(root / "sync.json", legacy)
            self.assertNotIn("assistants", DesktopAccountSync.sync_ack(state))
            with patch("account_sync.read_protected", wraps=read_protected) as read:
                self.assertNotIn("assistants", DesktopAccountSync.sync_ack(state))
                self.assertEqual([call.args[0].name for call in read.call_args_list], ["sync-auto.json"])
            self.assertEqual(DesktopAccountSync.sync_ack(state, True)["assistants"]["revision"], 7)


class AccountDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = Database(self.root / "accounts.db", local=True)
        self.store = AccountDataStore(self.database, self.root)

    def tearDown(self): self.temp.cleanup()

    def test_encryption_restart_owner_isolation_and_missing_key(self):
        raw = {"members": {"codex-one.json": credential()}}
        self.store.exchange("alice", "assistants", 0, raw)
        self.assertNotIn(b"access-private-one", self.database.path.read_bytes())
        restarted = AccountDataStore(self.database, self.root)
        self.assertEqual(restarted.get("alice", "assistants")["data"], raw)
        self.assertEqual(restarted.get("bob", "assistants")["revision"], 0)
        (self.root / "account-sync.key").unlink()
        with self.assertRaisesRegex(ValueError, "密钥缺失"):
            AccountDataStore(self.database, self.root)

    def test_three_way_merge_offline_conflict_and_atomic_credential(self):
        first = self.store.exchange("alice", "assistants", 0, {"members": {"codex-one.json": credential()}})
        remote = copy.deepcopy(first["data"]); remote["members"]["codex-two.json"] = credential("two")
        self.store.exchange("alice", "assistants", 1, remote)
        local = copy.deepcopy(first["data"]); local["pool"] = {"model": "fixture-model"}
        merged = self.store.exchange("alice", "assistants", 1, local)
        self.assertEqual(set(merged["data"]["members"]), {"codex-one.json", "codex-two.json"})
        current = copy.deepcopy(merged["data"]); current["members"]["codex-one.json"]["disabled"] = True
        self.store.exchange("alice", "assistants", merged["revision"], current)
        local = copy.deepcopy(merged["data"]); local["members"]["codex-one.json"]["access_token"] = "offline-refreshed-access"
        result = self.store.exchange("alice", "assistants", merged["revision"], local)
        self.assertTrue(result["data"]["members"]["codex-one.json"]["disabled"])
        self.assertEqual(result["data"]["members"]["codex-one.json"]["access_token"], "access-private-one")
        backup = self.store.conflicts("alice", result["conflict_id"])
        self.assertEqual(backup["incoming"]["members"]["codex-one.json"]["access_token"], "offline-refreshed-access")
        with self.assertRaises(ValueError): self.store.conflicts("bob", result["conflict_id"])

    def test_concurrent_additions_and_tombstones(self):
        self.store.exchange("alice", "assistants", 0, {"members": {}})
        def add(index):
            return self.store.exchange("alice", "assistants", 1, {"members": {f"codex-{index}.json": credential(str(index))}})
        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(add, range(6)))
        previous = self.store.get("alice", "assistants")
        self.assertEqual(len(previous["data"]["members"]), 6)
        self.store.exchange("alice", "assistants", previous["revision"], {"members": {}})
        stale = self.store.exchange("alice", "assistants", previous["revision"], previous["data"])
        self.assertEqual(stale["data"]["members"], {})
        fresh_device = self.store.exchange("alice", "assistants", 0, previous["data"])
        self.assertEqual(fresh_device["data"]["members"], {})
        self.assertTrue(fresh_device["conflict_id"])

    def test_credential_paths_and_machine_configuration_validation(self):
        for name in ("../outside.json", "C:\\private.json", ".hidden.json"):
            with self.assertRaises(ValueError): validate_resource("assistants", {"members": {name: credential()}})
        for data in ({"pool": {"management_key": "private"}}, {"profiles": {"codex": {"program": "C:/app.exe"}}},
                     {"pool": {"member_rules": {"x": {"weight": 0}}}}, {"members": {"one.json": {"type": "codex"}}}):
            with self.assertRaises(ValueError): validate_resource("assistants", data)
        with self.assertRaises(ValueError): validate_resource("schedule", {"first_monday": "2026-10-22"})

    def test_local_materialization_preserves_machine_keys_paths_and_history(self):
        root = self.root / "assistant"
        from ai_assistants import AssistantStore
        store = AssistantStore(root)
        store.data["profiles"]["codex"]["program"] = "C:/local/codex.exe"
        store.save(store.data)
        original_key = store.data["pool"]["management_key"]
        OperationHistory(root).append("fixture action")
        raw = read_assistants(root)
        raw["members"] = {"codex-one.json": credential()}
        write_assistants(root, raw)
        loaded = read_assistants(root)
        self.assertEqual(loaded, raw)
        self.assertEqual(AssistantStore(root).data["pool"]["management_key"], original_key)
        self.assertEqual(AssistantStore(root).data["profiles"]["codex"]["program"], "C:/local/codex.exe")
        loaded["members"] = {}
        write_assistants(root, loaded)
        self.assertFalse((root / "auths/codex-one.json").exists())
        backups = list((root / "backups/credentials").glob("*.json"))
        self.assertTrue(backups)
        self.assertNotIn("access-private", backups[0].read_text())

    def test_offline_worker_preserves_input_and_cached_user_identity(self):
        snapshot = {"preferences": {"theme": "深色"}, "schedule": {}, "assistants": {"members": {}}}
        worker = SyncWorker("http://127.0.0.1:1", "fixture-session", snapshot, {}, 1)
        with patch.object(AccountClient, "api", side_effect=ValueError("fixture offline")):
            worker.run()
        self.assertEqual(worker.snapshot, snapshot)
        self.assertTrue(worker.error)
        session = DesktopSession(self.root / "session.bin")
        user = {"id": "alice", "email": "alice@example.test"}
        session.save("http://127.0.0.1:1", "fixture-private-session", user)
        self.assertEqual(session.user("http://127.0.0.1:1"), user)
        self.assertIsNone(session.user("http://127.0.0.1:2"))

    def test_conflict_recovery_preserves_independent_current_fields(self):
        current = {"pool": {"model": "remote", "strategy": "balanced"}, "members": {"remote.json": credential()}}
        incoming = {"pool": {"model": "offline", "strategy": "round-robin"}, "members": {}}
        result = restore_conflict_fields(current, incoming, ["pool/model"])
        self.assertEqual(result["pool"], {"model": "offline", "strategy": "balanced"})
        self.assertEqual(result["members"], current["members"])


class AccountSyncHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): memo_fixtures.MemoHTTPTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls): memo_fixtures.MemoHTTPTests.tearDownClass.__func__(cls)

    def test_desktop_desktop_web_schedule_settings_members_and_deletion(self):
        a = AccountClient(self.url)
        a.request("register", {"email": "sync@example.test", "password": "FixturePass-123"})
        b = AccountClient(self.url)
        b.request("login", {"email": "sync@example.test", "password": "FixturePass-123"})
        other = AccountClient(self.url)
        other.request("register", {"email": "other@example.test", "password": "FixturePass-123"})
        a.api("/api/sync/preferences", {"revision": 0, "data": {"theme": "浅色", "concurrency": 3, "features": {"notes": True}}})
        a.api("/api/sync/schedule", {"revision": 0, "data": {"schedule": schedule(), "first_monday": "2026-09-07"}})
        a.api("/api/sync/assistants", {"revision": 0, "data": {"members": {"codex-one.json": credential()}}})
        remote = b.api("/api/sync")["resources"]
        self.assertEqual(b.api("/api/state")["theme"], "浅色")
        self.assertEqual(b.api("/api/timetable")["schedule"]["courses"][0]["name"], "Math")
        self.assertEqual(remote["assistants"]["data"]["members"]["codex-one.json"], credential())
        from urllib.parse import quote
        known = quote(json.dumps({"assistants": remote["assistants"]["revision"]}), safe="")
        unchanged = b.api("/api/sync?revisions=" + known)["resources"]["assistants"]
        self.assertTrue(unchanged["unchanged"])
        self.assertNotIn("data", unchanged)
        self.assertEqual(other.api("/api/sync")["resources"]["assistants"]["revision"], 0)
        # Exercise the same cookie/browser API used by the Web application.
        import requests
        web = requests.Session(); web.trust_env = False
        web.cookies.set("omini_session", b.token)
        response = web.post(self.url + "/api/timetable/semester", json={"first_monday": "2026-09-14"}, headers={"X-Omini-Client": "web"}, timeout=8)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(a.api("/api/sync")["resources"]["schedule"]["data"]["first_monday"], "2026-09-14")
        web.close()
        b.api("/api/sync/assistants", {"revision": remote["assistants"]["revision"], "data": {"members": {}}})
        self.assertEqual(a.api("/api/sync")["resources"]["assistants"]["data"]["members"], {})
        a.api("/api/settings", {"features": {"notes": False, "ai_assistants": False}})
        self.assertIn("assistants", b.api("/api/sync")["resources"])
        with self.assertRaises(ValueError): AccountClient(self.url).api("/api/sync")


if __name__ == "__main__": unittest.main()
