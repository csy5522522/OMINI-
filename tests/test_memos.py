"""Real HTTP, image/version isolation, offline sync and concurrent-edit recovery."""
import base64
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from PIL import Image
from account_client import AccountClient
from memo_store import MemoStore, MemoConflict
from memo_sync import sync_memos
from text_notes import TextNotesStore, read_text_file
from unittest.mock import patch


def image_data():
    stream = io.BytesIO(); Image.new("RGB", (40, 30), "#45917a").save(stream, "PNG")
    return base64.b64encode(stream.getvalue()).decode()


class MemoStorageTests(unittest.TestCase):
    def test_reusing_trashed_title_keeps_both_memos_and_restore_preserves_new_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TextNotesStore(Path(directory))
            first = store.create("同名记录", "旧记录")
            old = store.for_path(first); store.trash(first)
            second = store.create("同名记录", "新记录")
            self.assertNotEqual(old["id"], store.for_path(second)["id"])
            _, restored = store.write_snapshot({**old, "deleted": False}, "restore")
            self.assertNotEqual(restored, second)
            self.assertEqual({read_text_file(p)[0] for p in store.files()}, {"旧记录", "新记录"})

    def test_versions_rollback_deletion_and_owner_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MemoStore(Path(directory) / "memos.db")
            a = store.save("alice", {"title": "第一稿", "content": "初始"})
            b = store.save("alice", {"id": a["id"], "title": "第二稿", "content": "写入动作", "base_version": 1})
            with self.assertRaises(MemoConflict): store.save("alice", {"id": a["id"], "title": "旧编辑", "base_version": 1})
            with self.assertRaises(ValueError): store.history("bob", a["id"])
            rolled = store.restore("alice", a["id"], 1, 2)
            self.assertEqual((rolled["content"], rolled["version"]), ("初始", 3))
            self.assertEqual([v["action"] for v in store.history("alice", a["id"])], ["rollback", "edit", "create"])
            deleted = store.save("alice", {**rolled, "deleted": True, "base_version": 3}, action="delete")
            self.assertTrue(deleted["deleted"])
            self.assertEqual(store.listing("alice", include_deleted=False), [])
            restored = store.restore("alice", a["id"], 2, 4)
            self.assertEqual(restored["content"], b["content"])
            asset = store.put_asset("alice", "图片.png", image_data())
            self.assertEqual(store.asset("alice", asset["id"])["mime"], "image/png")
            with self.assertRaises(ValueError): store.asset("bob", asset["id"])
            with self.assertRaises(ValueError): store.put_asset("alice", "bad.svg", base64.b64encode(b'<svg onload="alert(1)"/>').decode())


class MemoHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.root = Path(cls.temp.name)
        env = os.environ.copy(); env.update(OMINI_ADMIN_PASSWORD="admin123", OMINI_WEB_PASSWORD="", PYTHONIOENCODING="utf-8")
        cls.process = subprocess.Popen([sys.executable, "web_server.py", "--port", "0", "--data-dir", str(cls.root / "server")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        event = threading.Event(); lines = []
        def output():
            for line in cls.process.stdout:
                lines.append(line)
                match = re.search(r"Local URL: (http://\S+)", line)
                if match: cls.url = match[1]; event.set()
        threading.Thread(target=output, daemon=True).start()
        if not event.wait(20): cls.process.kill(); raise RuntimeError(''.join(lines))

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate(); cls.process.wait(timeout=15); cls.process.stdout.close(); cls.temp.cleanup()

    def client(self, name):
        client = AccountClient(self.url)
        client.request("register", {"email": name + "@example.test", "password": "FixturePass-123"})
        return client

    def test_background_sync_does_not_replace_active_document_and_applies_after_switch(self):
        client = self.client('active-editor')
        with tempfile.TemporaryDirectory() as directory:
            store = TextNotesStore(Path(directory))
            path = store.create('正在编辑', '本机正文')
            memo_id = store.for_path(path)['id']
            sync_memos(directory, client.url, client.token)
            remote = next(m for m in client.api('/api/memos')['memos'] if m['id'] == memo_id)
            client.api('/api/memos', {**remote, 'content': '云端新正文', 'base_version': remote['version']})
            report = sync_memos(directory, client.url, client.token, protected=lambda key: key == memo_id)
            self.assertEqual(report['deferred'], 1)
            self.assertEqual(read_text_file(path)[0], '本机正文')
            sync_memos(directory, client.url, client.token)
            self.assertEqual(read_text_file(path)[0], '云端新正文')

    def test_local_save_during_cloud_download_survives_and_syncs_as_conflict(self):
        client = self.client('concurrent-local-save')
        with tempfile.TemporaryDirectory() as directory:
            store = TextNotesStore(Path(directory))
            path = store.create('并行编辑', '第一版')
            memo_id = store.for_path(path)['id']
            sync_memos(directory, client.url, client.token)
            remote = next(m for m in client.api('/api/memos')['memos'] if m['id'] == memo_id)
            client.api('/api/memos', {**remote, 'content': '云端修改', 'base_version': remote['version']})
            real_api = AccountClient.api
            def delayed(api, route, value=None):
                result = real_api(api, route, value)
                if route == f'/api/memos/{memo_id}/history':
                    store.save(path, '同步途中继续输入', read_text_file(path)[1])
                return result
            with patch.object(AccountClient, 'api', delayed):
                report = sync_memos(directory, client.url, client.token)
            self.assertEqual(report['deferred'], 1)
            self.assertEqual(read_text_file(path)[0], '同步途中继续输入')
            sync_memos(directory, client.url, client.token)
            contents = {m['content'] for m in client.api('/api/memos')['memos']}
            self.assertTrue({'云端修改', '同步途中继续输入'} <= contents)

    def test_operation_retries_preserve_history_and_session_over_http(self):
        client = self.client("response-loss")
        token = client.token
        payload = {"id": "c" * 32, "title": "HTTP fixture", "content": "first", "operation_id": "d" * 64}
        first = client.api("/api/memos", payload)["memo"]
        client.api("/api/memos", {**first, "content": "newer", "base_version": 1})
        self.assertEqual(client.api("/api/memos", payload)["memo"], first)
        self.assertEqual(len(client.api(f"/api/memos/{first['id']}/history")["versions"]), 2)
        with tempfile.TemporaryDirectory() as directory:
            store = TextNotesStore(Path(directory))
            path = store.create("retry fixture", "first action")
            store.save(path, "second action", read_text_file(path)[1])
            real_api = client.api
            writes = 0
            def delayed(path, value=None):
                nonlocal writes
                result = real_api(path, value)
                if path == "/api/memos" and value is not None:
                    writes += 1
                    if writes == 2: raise ValueError("Fixture: committed response lost")
                return result
            with patch("memo_sync.AccountClient", return_value=client), patch.object(client, "api", side_effect=delayed):
                with self.assertRaises(ValueError): sync_memos(directory, self.url, token)
                self.assertEqual(sync_memos(directory, self.url, token)["conflicts"], 0)
            memos = client.api("/api/memos")["memos"]
            retry = next(m for m in memos if m["title"] == "retry fixture")
            self.assertEqual(retry["content"], "second action")
            self.assertEqual(len(client.api(f"/api/memos/{retry['id']}/history")["versions"]), 2)
        self.assertEqual(client.token, token)
        self.assertTrue(client.request("session")["authenticated"])

    def test_01_api_versions_images_and_personal_feature_settings(self):
        alice, bob = self.client("alice"), self.client("bob")
        asset = alice.api("/api/memos/assets", {"name": "图片.png", "data": image_data()})
        memo = alice.api("/api/memos", {"title": "文字与图片", "content": f"![图片](omini-asset://{asset['id']})"})["memo"]
        self.assertEqual(bob.api("/api/memos")["memos"], [])
        for path in (f"/api/memos/{memo['id']}/history", "/api/memos/assets/" + asset["id"]):
            with self.assertRaises(ValueError): bob.api(path)
        alice.api("/api/settings", {"features": {"notes": False, "timetable": False}})
        self.assertTrue(bob.api("/api/state")["features"]["notes"])
        self.assertEqual(len(alice.api("/api/memos")["memos"]), 1)
        import requests
        with requests.Session() as web:
            web.trust_env = False
            web.cookies.set("omini_session", alice.token)
            self.assertEqual(web.get(self.url + "/api/memos", headers={"X-Omini-Client": "web"}, timeout=8).status_code, 403)
        alice.api("/api/settings", {"features": {"notes": True}})
        self.assertEqual(len(alice.api("/api/memos")["memos"]), 1)
        with self.assertRaises(ValueError): alice.api("/api/admin/users")

    def test_02_sync_every_action_images_and_conflict_preservation(self):
        client = self.client("sync")
        one = TextNotesStore(self.root / "client-one"); two = TextNotesStore(self.root / "client-two")
        path = one.create("同账号同步", "第一稿")
        from text_notes import read_text_file
        asset = one.memos.put_asset(one.owner, "图片.png", image_data())
        one.save(path, f"第二稿\n![图片](omini-asset://{asset['id']})", read_text_file(path)[1])
        sync_memos(one.directory, self.url, client.token)
        remote = client.api("/api/memos")["memos"][0]
        self.assertEqual(len(client.api(f"/api/memos/{remote['id']}/history")["versions"]), 2)
        sync_memos(two.directory, self.url, client.token)
        self.assertIn("第二稿", read_text_file(two.files()[0])[0])
        self.assertEqual(len(two.memos.history(two.owner, remote["id"])), 2)
        self.assertEqual(two.memos.asset(two.owner, asset["id"])["mime"], "image/png")
        one.save(path, "桌面离线修改", read_text_file(path)[1])
        cloud = client.api("/api/memos", {"id": remote["id"], "title": "同账号同步", "content": "网页更新", "base_version": remote["version"]})["memo"]
        result = sync_memos(one.directory, self.url, client.token)
        self.assertEqual(result["conflicts"], 1)
        texts = {m["content"] for m in client.api("/api/memos")["memos"]}
        self.assertEqual(texts, {"桌面离线修改", "网页更新"})
        self.assertEqual({read_text_file(p)[0] for p in one.files()}, texts)
        rolled = client.api(f"/api/memos/{cloud['id']}/restore", {"version": 1, "base_version": cloud["version"]})["memo"]
        sync_memos(two.directory, self.url, client.token)
        self.assertEqual(two.memos.get(two.owner, cloud["id"])["content"], rolled["content"])
        sync_memos(two.directory, self.url, client.token)
        self.assertEqual(len(client.api(f"/api/memos/{cloud['id']}/history")["versions"]), rolled["version"])

    def test_03_admin_disable_reset_and_hashed_passwords(self):
        user = self.client("managed")
        administrator = AccountClient(self.url); administrator.request("login", {"email": "admin", "password": "admin123"})
        users = administrator.api("/api/admin/users")["users"]
        self.assertNotIn("password", json.dumps(users))
        target = next(row for row in users if row["email"] == "managed@example.test")
        administrator.api("/api/admin/user", {"id": target["id"], "disabled": True})
        self.assertFalse(user.request("session")["authenticated"])
        with self.assertRaises(ValueError): user.request("login", {"email": target["email"], "password": "FixturePass-123"})
        administrator.api("/api/admin/user", {"id": target["id"], "disabled": False, "password": "NewPassword-123"})
        user.request("login", {"email": target["email"], "password": "NewPassword-123"})
        self.assertTrue(user.request("session")["authenticated"])
