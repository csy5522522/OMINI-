"""Announcement validation, durable edits and real HTTP administrator boundaries."""
from pathlib import Path
import tempfile
import unittest

import requests
from account_client import AccountClient
from announcements import AnnouncementStore, AnnouncementConflict
from database import Database
import tests.test_memos as fixtures


class AnnouncementStorageTests(unittest.TestCase):
    def test_persistence_validation_and_stale_edit_delete(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "site.sqlite")
            store = AnnouncementStore(database)
            for value in ({"title": "", "content": "body"}, {"title": "title", "content": []},
                          {"title": "a" * 121, "content": "body"}, {"title": "a", "content": "b" * 20001}):
                with self.assertRaises(ValueError): store.save(value)
            original = store.save({"title": " 标题 ", "content": "正文\n第二行"})["announcement"]
            self.assertEqual(AnnouncementStore(database).listing(), [original])
            updated = store.save({**original, "title": "已修改"})["announcement"]
            self.assertEqual(updated["version"], 2)
            with self.assertRaises(AnnouncementConflict): store.save({**original, "content": "旧编辑"})
            with self.assertRaises(AnnouncementConflict): store.save({**original, "action": "delete"})
            self.assertEqual(store.listing(), [updated])
            store.save({**updated, "action": "delete"})
            self.assertEqual(AnnouncementStore(database).listing(), [])


class AnnouncementHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): fixtures.MemoHTTPTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls): fixtures.MemoHTTPTests.tearDownClass.__func__(cls)

    def test_public_read_and_administrator_only_crud(self):
        headers = {"X-Omini-Client": "web"}
        guest = requests.Session(); guest.trust_env = False
        self.addCleanup(guest.close)
        endpoint = self.url + "/api/admin/announcements"
        self.assertEqual(guest.get(self.url + "/api/announcements").status_code, 200)
        self.assertEqual(guest.post(endpoint, json={"title": "guest", "content": "body"}, headers=headers).status_code, 401)
        user = AccountClient(self.url)
        user.request("register", {"email": "reader@example.test", "password": "FixturePass-123"})
        guest.cookies.set("omini_session", user.token)
        self.assertEqual(guest.get(endpoint, headers=headers).status_code, 403)
        for action in ("save", "delete"):
            self.assertEqual(guest.post(endpoint, json={"action": action}, headers=headers).status_code, 403)
        admin = AccountClient(self.url)
        admin.request("login", {"email": "admin", "password": "admin123"})
        item = admin.api("/api/admin/announcements", {"title": "公开公告", "content": "<img src=x onerror=alert(1)>\n第二行"})["announcement"]
        self.assertIn(item, guest.get(self.url + "/api/announcements").json()["announcements"])
        updated = admin.api("/api/admin/announcements", {**item, "title": "更新公告"})["announcement"]
        with self.assertRaises(ValueError): admin.api("/api/admin/announcements", {**item, "action": "delete"})
        admin.api("/api/admin/announcements", {**updated, "action": "delete"})
        self.assertEqual(guest.get(self.url + "/api/announcements").json()["announcements"], [])

    def test_web_retired_assistants_and_selected_desktop_sync(self):
        client = AccountClient(self.url)
        client.request("register", {"email": "resources@example.test", "password": "FixturePass-123"})
        self.assertEqual(set(client.api("/api/sync?resources=preferences,schedule")["resources"]), {"preferences", "schedule"})
        self.assertIn("assistants", client.api("/api/sync")["resources"])
        with requests.Session() as web:
            web.trust_env = False; web.cookies.set("omini_session", client.token)
            self.assertEqual(set(web.get(self.url + "/api/sync").json()["resources"]), {"preferences", "schedule"})
            self.assertEqual(web.get(self.url + "/api/sync?resources=assistants").status_code, 400)
            self.assertEqual(web.post(self.url + "/api/sync/assistants", json={"revision": 0, "data": {}},
                                     headers={"X-Omini-Client": "web"}).status_code, 403)
        self.assertNotIn("ai_assistants", client.api("/api/state")["features"])
