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
from PySide6.QtWidgets import QApplication

import app
from account_client import AccountClient
from feature_config import DESKTOP_FEATURES


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
        for item in self.patches: item.start()
        app.ensure_app_dirs()
        app.save_json(app.SETTINGS_FILE, {"features": dict.fromkeys(DESKTOP_FEATURES, False), "last_page": 8})
        self.window = app.MainWindow()

    def tearDown(self):
        if self.window:
            self.window.quitting = True; self.window.close()
            self.until(lambda: self.window.settings_page.account.worker is None)
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

    def test_memo_account_switch_images_and_guest_data_are_isolated(self):
        import base64, io
        from PIL import Image
        panel = self.window.settings_page.account
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0)); port = listener.getsockname()[1]
        panel.address.setText(f"http://127.0.0.1:{port}")
        panel.start_service(); self.until(lambda: panel.service_ready)
        self.window.settings_page.feature_checks["notes"].setChecked(True); self.save()
        page = self.window.notes_page
        guest = page.store.create("本机原笔记", "仅保存在访客目录")
        page.refresh_files(guest.name)
        def register(email):
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

    def test_all_disabled_keeps_settings_account_theme_and_tray_usable(self):
        self.assertEqual(self.window.nav.currentRow(), 4)
        self.assertIs(self.window.stack.currentWidget(), self.window.settings_page)
        for index, feature in enumerate(app.PAGE_FEATURES):
            if feature:
                self.assertTrue(self.window.nav.item(index).isHidden())
                self.assertIsNone(getattr(self.window, self.window.page_attribute(feature)))
        self.window.open_ai_assistants(); self.assertEqual(self.window.nav.currentRow(), 4)
        self.window.open_folder_sync(); self.window.login_campus()
        self.window.open_account(); self.window.toggle_theme()
        self.assertTrue(all(not action.isVisible() for action in self.window.feature_tray_actions.values()))

    def test_expired_session_keeps_account_notes_and_relogin_uploads_offline_edits(self):
        from accounts import AccountStore
        from account_panel import AccountPanel
        panel = self.window.settings_page.account
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0)); port = listener.getsockname()[1]
        panel.address.setText(f"http://127.0.0.1:{port}")
        panel.start_service(); self.until(lambda: panel.service_ready)
        self.window.settings_page.feature_checks["notes"].setChecked(True); self.save()
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

    def test_enable_disable_restore_notes_and_navigation(self):
        checks = self.window.settings_page.feature_checks
        checks["notes"].setChecked(True); self.save()
        page = self.window.notes_page
        self.assertFalse(self.window.nav.item(3).isHidden())
        self.window.nav.setCurrentRow(3)
        self.assertIs(self.window.stack.currentWidget(), page)
        path = page.store.create("保留笔记", "原始")
        page.refresh_files(path.name); page.editor.setPlainText("关闭功能后继续保留")
        checks["notes"].setChecked(False); self.save()
        self.assertIsNone(self.window.notes_page)
        self.assertTrue(self.window.nav.item(3).isHidden())
        self.assertEqual(self.window.nav.currentRow(), 4)
        self.assertEqual(read_text_file(path)[0], "关闭功能后继续保留")
        checks["notes"].setChecked(True); self.save()
        self.assertIsNot(self.window.notes_page, page)
        self.assertEqual(self.window.notes_page.editor.toPlainText(), "关闭功能后继续保留")
        self.assertTrue(json.loads(app.SETTINGS_FILE.read_text(encoding="utf-8"))["features"]["notes"])

    def test_failed_note_save_preserves_feature_and_edit(self):
        checks = self.window.settings_page.feature_checks
        checks["notes"].setChecked(True); self.save()
        page = self.window.notes_page
        checks["notes"].setChecked(False)
        with patch.object(page, "flush", return_value=False), patch("app.QMessageBox.warning"):
            self.save()
        self.assertIs(self.window.notes_page, page)
        self.assertTrue(self.window.settings["features"]["notes"])

    def test_real_web_start_register_login_logout_and_graceful_stop(self):
        panel = self.window.settings_page.account
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0)); port = listener.getsockname()[1]
        panel.address.setText(f"http://127.0.0.1:{port}")
        QTest.mouseClick(panel.start_button, Qt.MouseButton.LeftButton)
        self.until(lambda: panel.service_ready)
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

    def test_disabled_pages_are_not_imported_at_startup(self):
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
assert window.nav.currentRow() == 4
for name in ('campus_page','timetable_page','text_notes_page','brightdata_page','folder_sync_page','omini_api_page','ai_assistants_page'):
    assert name not in sys.modules, name
window.quitting = True
window.close()
"""
        result = subprocess.run([sys.executable, "-c", source, str(self.root)], capture_output=True, text=True,
                                timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__": unittest.main()
