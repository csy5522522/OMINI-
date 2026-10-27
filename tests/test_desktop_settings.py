"""Desktop feature lifecycle and real account/web service buttons in isolated state."""
from __future__ import annotations

import json
import os
from pathlib import Path
from text_notes import read_text_file
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QProcess, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QGroupBox

import app
from account_client import AccountClient
from feature_config import DESKTOP_FEATURES, saved_page


class DesktopSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="omini-settings-test-")
        self.root = Path(self.temp.name)
        self.patches = [patch.object(app, key, self.root / name) for key, name in {
            "APP_DIR": ".", "TASKS_FILE": "tasks.json", "SETTINGS_FILE": "settings.json",
            "LOG_DIR": "logs", "TEMP_COOKIE_DIR": "runtime-cookies",
        }.items()]
        self.patches.append(patch.dict(os.environ, {"OMINI_WEB_PASSWORD": ""}))
        self.patches.append(patch("campus_page.wifi_ssids", return_value=()))
        for item in self.patches: item.start()
        app.ensure_app_dirs()
        app.save_json(app.SETTINGS_FILE, {"features": dict.fromkeys(DESKTOP_FEATURES, False), "last_page": 8})
        self.window = app.MainWindow()

    def tearDown(self):
        if self.window:
            self.window.quitting = True; self.window.close()
            pages = [getattr(self.window, self.window.page_attribute(feature))
                     for feature in app.PAGE_FEATURES if feature]
            pages += [self.window.settings_page.account, self.window.cloud_sync]
            self.until(lambda: all(getattr(page, "worker", None) is None for page in pages))
            self.qt.processEvents(); self.window.deleteLater(); QTest.qWait(100)
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    def until(self, condition, seconds=12):
        end = time.monotonic() + seconds
        while not condition() and time.monotonic() < end: QTest.qWait(20)
        self.assertTrue(condition())

    def save(self):
        with patch("app.QMessageBox.information"):
            self.window.settings_page.save()
        self.qt.processEvents()

    def test_window_geometry_restores_from_device_local_file(self):
        self.window.resize(1150, 780)
        self.window.move(60, 70)
        self.window.save_window_layout()
        saved = app.load_json(self.root / 'window-layout.json', {})
        self.assertTrue(saved.get('geometry'))
        self.window.resize(1000, 700)
        self.window.restore_window_layout()
        self.assertEqual((self.window.width(), self.window.height()), (1150, 780))
        self.assertNotIn('geometry', app.load_json(app.SETTINGS_FILE, {}))

    def test_memo_account_switch_images_and_guest_data_are_isolated(self):
        import base64, io
        from PIL import Image
        panel = self.window.settings_page.account
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0)); port = listener.getsockname()[1]
        panel.address.setText(f"http://127.0.0.1:{port}")
        panel.start_service(); self.until(lambda: panel.service_ready)
        page = self.window.notes_page
        guest = page.store.create("本机原笔记", "仅保存在访客目录")
        page.refresh_files(guest.name)
        def register(email):
            panel.auth_tabs.setCurrentIndex(1)
            panel.email.setText(email); panel.password.setText("FixturePassword-123"); panel.confirm.setText("FixturePassword-123")
            panel.register_button.click(); self.until(lambda: panel.worker is None)
            self.assertEqual(panel.user["email"], email)
            self.until(lambda: page.worker is None)
        register("first@example.test")
        first_directory = page.store.directory
        self.assertNotEqual(first_directory, guest.parent)
        self.assertEqual(page.editor.toPlainText(), "仅保存在访客目录")
        self.assertEqual(len(AccountClient(panel.url, panel.token).api("/api/memos")["memos"]), 1)
        note = page.store.create("账号图片备忘录", "第一稿"); page.refresh_files(note.name)
        raw = io.BytesIO(); Image.new("RGB", (32, 20), "red").save(raw, "PNG")
        page.insert_raw_image(raw.getvalue(), "图片.png")
        self.assertIn("omini-asset://", page.editor.get_content())
        page.sync_now(); self.until(lambda: page.worker is None)
        client = AccountClient(panel.url, panel.token)
        remote = client.api("/api/memos")["memos"]
        self.assertEqual(len(remote), 2)
        image_memo = next(memo for memo in remote if memo['title'] == '账号图片备忘录')
        self.assertGreaterEqual(len(client.api(f"/api/memos/{image_memo['id']}/history")["versions"]), 2)
        panel.logout_button.click(); self.until(lambda: panel.worker is None)
        self.assertEqual(page.store.directory, guest.parent)
        self.assertEqual(read_text_file(guest)[0], "仅保存在访客目录")
        register("second@example.test")
        self.assertEqual(page.store.files(), [])
        self.assertEqual(AccountClient(panel.url, panel.token).api("/api/memos")["memos"], [])
        self.assertNotEqual(page.store.directory, first_directory)
        self.assertEqual(json.loads(page.config_path.read_text(encoding='utf-8'))["directory"], str(guest.parent))

    def test_legacy_disabled_features_keep_all_remaining_tools_available(self):
        self.assertEqual(self.window.nav.count(), 6)
        for index, feature in enumerate(app.PAGE_FEATURES):
            if feature:
                self.assertFalse(self.window.nav.item(index).isHidden())
                self.assertIsNotNone(getattr(self.window, self.window.page_attribute(feature)))
        self.window.open_ai_assistants(); self.assertEqual(self.window.nav.currentRow(), 5)
        self.window.open_account(); self.window.toggle_theme()
        self.assertEqual(self.window.nav.currentRow(), 4)
        self.assertNotIn("sync", self.window.feature_tray_actions)
        self.assertFalse(hasattr(self.window, "sync_page"))
        groups = {box.title() for box in self.window.settings_page.findChildren(QGroupBox)}
        self.assertFalse(groups & {"可选功能加载", "下载设置"})
        self.assertTrue(all(action.isVisible() for action in self.window.feature_tray_actions.values()))

    def test_old_navigation_and_account_preferences_skip_retired_tools(self):
        from account_sync import preferences
        self.assertEqual(saved_page({"last_feature": "sync", "last_page": 8}), 4)
        self.assertEqual(saved_page({"last_feature": "more"}), 4)
        self.assertEqual(saved_page({"last_page": 6}), 4)
        self.assertEqual(saved_page({"last_feature": "ai_assistants"}), 5)
        self.assertEqual(preferences({"features": {"sync": True, "more": False, "notes": False}}),
                         {"features": {"notes": False}})

    def test_auth_tabs_separate_fields_and_clear_passwords_when_switching(self):
        panel = self.window.settings_page.account
        self.assertIs(panel.email, panel.login_email)
        panel.email.setText("forms@example.test"); panel.password.setText("LoginPassword-123")
        panel.auth_tabs.setCurrentIndex(1)
        self.assertIs(panel.email, panel.register_email)
        self.assertEqual(panel.email.text(), "forms@example.test")
        self.assertEqual(panel.login_password.text(), "")
        panel.password.setText("RegisterPassword-123"); panel.confirm.setText("mismatch")
        panel.request("register")
        self.assertIsNone(panel.worker)
        self.assertIn("不一致", panel.status.text())
        panel.auth_tabs.setCurrentIndex(0)
        self.assertEqual(panel.register_password.text(), "")
        self.assertEqual(panel.confirm.text(), "")
        self.assertIs(panel.password, panel.login_password)

    def test_expired_session_keeps_account_notes_and_relogin_uploads_offline_edits(self):
        from accounts import AccountStore
        from account_panel import AccountPanel
        panel = self.window.settings_page.account
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0)); port = listener.getsockname()[1]
        panel.address.setText(f"http://127.0.0.1:{port}")
        panel.start_service(); self.until(lambda: panel.service_ready)
        panel.auth_tabs.setCurrentIndex(1)
        panel.email.setText("expiry@example.test"); panel.password.setText("FixturePassword-123"); panel.confirm.setText("FixturePassword-123")
        panel.register_button.click(); self.until(lambda: panel.worker is None)
        page = self.window.notes_page; self.until(lambda: page.worker is None)
        path = page.store.create("账号离线内容", "到期前"); page.refresh_files(path.name)
        page.sync_now(); self.until(lambda: page.worker is None)
        directory, context, user = page.store.directory, page.context, panel.user.copy()
        with AccountStore(self.root/'web/accounts.sqlite3').connect() as db:
            db.execute("UPDATE sessions SET expires_at=0")
        panel.request("session"); self.until(lambda: panel.worker is None)
        self.assertEqual(panel.user, user); self.assertFalse(panel.token)
        self.assertEqual(page.store.directory, directory); self.assertEqual(page.context, context)
        self.assertIn("登录已过期", panel.status.text())
        page.editor.setPlainText("凭据过期后继续写的内容"); self.assertTrue(page.flush())
        with patch('text_notes_page.MemoSyncWorker') as worker:
            page.sync_now(); worker.assert_not_called()
        restored = AccountPanel(self.root, self.window.settings, lambda value: None)
        self.assertEqual(restored.user, user); self.assertFalse(restored.token)
        restored.stop(); restored.deleteLater()
        panel.password.setText("FixturePassword-123"); panel.login_button.click()
        self.until(lambda: panel.worker is None)
        self.until(lambda: page.worker is not None)
        self.until(lambda: page.worker is None)
        self.assertEqual(page.store.directory, directory)
        remote = AccountClient(panel.url, panel.token).api('/api/memos')['memos']
        self.assertEqual(remote[0]['content'], "凭据过期后继续写的内容")

    def test_theme_save_preserves_download_preferences_and_live_notes(self):
        page = self.window.notes_page
        self.window.nav.setCurrentRow(3)
        path = page.store.create("保留笔记", "原始")
        page.refresh_files(path.name); page.editor.setPlainText("修改主题时保留编辑")
        self.window.settings.update(download_dir="fixture-download-folder", concurrency=4)
        self.window.settings_page.theme.setCurrentText("浅色"); self.save()
        self.assertIs(self.window.notes_page, page)
        self.assertEqual(page.editor.toPlainText(), "修改主题时保留编辑")
        saved = json.loads(app.SETTINGS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(saved["theme"], "浅色")
        self.assertEqual(saved["download_dir"], "fixture-download-folder")
        self.assertEqual(saved["concurrency"], 4)
        self.assertTrue(page.flush())
        self.assertEqual(read_text_file(path)[0], "修改主题时保留编辑")

    def test_cloud_feature_flags_leave_unsaved_editor_and_account_intact(self):
        import copy
        page = self.window.notes_page
        path = page.store.create("保存保护", "原文")
        page.refresh_files(path.name); page.editor.setPlainText("待保存的编辑")
        before = self.window.cloud_sync.snapshot()
        incoming = copy.deepcopy(before); incoming["preferences"]["features"]["notes"] = False
        panel = self.window.settings_page.account
        identity = (panel.user, panel.token, panel.store.path.exists())
        with patch.object(page.store, "save", side_effect=OSError("fixture disk failure")):
            self.window.cloud_sync.apply(incoming, before)
        self.assertIs(self.window.notes_page, page)
        self.assertFalse(page.stopping)
        self.assertTrue(page.dirty)
        self.assertEqual(page.editor.toPlainText(), "待保存的编辑")
        self.assertEqual(read_text_file(path)[0], "原文")
        self.assertFalse(self.window.nav.item(3).isHidden())
        self.assertEqual((panel.user, panel.token, panel.store.path.exists()), identity)
        self.assertTrue(page.flush())
        self.assertEqual(read_text_file(path)[0], "待保存的编辑")

    def test_cloud_feature_flags_preserve_ime_composition(self):
        import copy
        page = self.window.notes_page
        path = page.store.create("输入保护", "原文"); page.refresh_files(path.name)
        before = self.window.cloud_sync.snapshot()
        incoming = copy.deepcopy(before); incoming["preferences"]["features"]["notes"] = False
        with patch.object(page.editor, "composing", True):
            self.window.cloud_sync.apply(incoming, before)
        self.assertIs(self.window.notes_page, page)
        self.assertFalse(page.stopping)

    def test_real_web_start_register_login_logout_and_graceful_stop(self):
        panel = self.window.settings_page.account
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0)); port = listener.getsockname()[1]
        panel.address.setText(f"http://127.0.0.1:{port}")
        QTest.mouseClick(panel.start_button, Qt.MouseButton.LeftButton)
        self.until(lambda: panel.service_ready)
        panel.auth_tabs.setCurrentIndex(1)
        panel.email.setText("desktop@example.test")
        panel.password.setText("DesktopPassword-123"); panel.confirm.setText("DesktopPassword-123")
        QTest.mouseClick(panel.register_button, Qt.MouseButton.LeftButton)
        self.until(lambda: panel.worker is None)
        self.assertIn("已登录：desktop@example.test", panel.status.text())
        self.assertEqual(panel.password.text(), ""); self.assertEqual(panel.confirm.text(), "")
        self.assertEqual(panel.store.load(panel.url), panel.token)
        self.assertTrue(AccountClient(panel.url, panel.token).request("session")["authenticated"])
        QTest.mouseClick(panel.logout_button, Qt.MouseButton.LeftButton)
        self.until(lambda: panel.worker is None)
        self.assertFalse(panel.token); self.assertFalse(panel.store.path.exists())
        panel.password.setText("DesktopPassword-123")
        QTest.mouseClick(panel.login_button, Qt.MouseButton.LeftButton)
        self.until(lambda: panel.worker is None)
        self.assertIn("已登录", panel.status.text())
        token = panel.token
        panel.stop_service()
        self.until(lambda: panel.process.state() == QProcess.ProcessState.NotRunning)
        panel.start_service(); self.until(lambda: panel.service_ready)
        self.assertTrue(AccountClient(panel.url, token).request("session")["authenticated"])
        panel.stop_service(); self.until(lambda: panel.process.state() == QProcess.ProcessState.NotRunning)

    def test_retired_pages_are_not_imported_at_startup(self):
        source = """
import sys
from pathlib import Path
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
import app
qt = QApplication([])
root = Path(sys.argv[1])
for key, name in {'APP_DIR':'.','TASKS_FILE':'tasks.json','SETTINGS_FILE':'settings.json','LOG_DIR':'logs','TEMP_COOKIE_DIR':'runtime-cookies'}.items():
    setattr(app, key, root / name)
window = app.MainWindow()
assert window.nav.count() == 6
for name in ('brightdata_page','folder_sync_page','folder_sync','tailscale_sync','sync_self_test','omini_api_page'):
    assert name not in sys.modules, name
window.quitting = True
window.close()
"""
        result = subprocess.run([sys.executable, "-c", source, str(self.root)], capture_output=True, text=True,
                                timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__": unittest.main()
