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

    def test_01_api_versions_images_and_personal_feature_settings(self):
        alice, bob = self.client("alice"), self.client("bob")
        asset = alice.api("/api/memos/assets", {"name": "图片.png", "data": image_data()})
        memo = alice.api("/api/memos", {"title": "文字与图片", "content": f"![图片](omini-asset://{asset['id']})"})["memo"]
        self.assertEqual(bob.api("/api/memos")["memos"], [])
        for path in (f"/api/memos/{memo['id']}/history", "/api/memos/assets/" + asset["id"]):
            with self.assertRaises(ValueError): bob.api(path)
        alice.api("/api/settings", {"features": {"notes": False, "timetable": False}})
        self.assertTrue(bob.api("/api/state")["features"]["notes"])
        with self.assertRaises(ValueError): alice.api("/api/memos")
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
