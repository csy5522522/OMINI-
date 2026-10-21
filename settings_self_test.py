"""Isolated account/feature checks for source and the frozen Windows application."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import tempfile
import time
import traceback
from unittest.mock import patch

from PySide6.QtCore import QProcess
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from account_client import AccountClient
from feature_config import DESKTOP_FEATURES


def run_settings_test(app_module):
    report = {"ok": False}
    window = None
    patches = []
    qt = QApplication.instance() or QApplication([])
    destination = Path.cwd() / "verification"
    destination.mkdir(exist_ok=True)

    def until(predicate, message, timeout=15):
        end = time.monotonic() + timeout
        while not predicate() and time.monotonic() < end: QTest.qWait(20)
        if not predicate(): raise AssertionError(message)

    try:
        with tempfile.TemporaryDirectory(prefix="omini-account-feature-check-") as directory:
            root = Path(directory)
            patches = [patch.object(app_module, key, root / name) for key, name in {
                "APP_DIR": ".", "TASKS_FILE": "tasks.json", "SETTINGS_FILE": "settings.json",
                "LOG_DIR": "logs", "TEMP_COOKIE_DIR": "runtime-cookies",
            }.items()]
            patches.append(patch.dict("os.environ", {"OMINI_WEB_PASSWORD": ""}))
            for item in patches: item.start()
            app_module.ensure_app_dirs()
            app_module.save_json(app_module.SETTINGS_FILE,
                                 {"features": dict.fromkeys(DESKTOP_FEATURES, False), "last_page": 8})
            window = app_module.MainWindow(); window.show()
            assert window.nav.currentRow() == 4
            assert window.ai_assistants_page is None and window.sync_page is None
            assert all(window.nav.item(index).isHidden() for index in range(window.nav.count()) if index != 4)
            report["disabled_startup"] = True
            checks = window.settings_page.feature_checks
            checks["notes"].setChecked(True)
            with patch.object(app_module.QMessageBox, "information"): window.settings_page.save()
            page = window.notes_page
            file = page.store.create("功能开关验证", "initial")
            page.refresh_files(file.name); page.editor.setPlainText("关闭功能后保留笔记")
            checks["notes"].setChecked(False)
            with patch.object(app_module.QMessageBox, "information"): window.settings_page.save()
            assert window.notes_page is None
            assert file.read_text(encoding="utf-8") == "关闭功能后保留笔记"
            checks["notes"].setChecked(True)
            with patch.object(app_module.QMessageBox, "information"): window.settings_page.save()
            assert window.notes_page.editor.toPlainText() == "关闭功能后保留笔记"
            report["feature_reload_preserves_data"] = True

            panel = window.settings_page.account
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0)); port = listener.getsockname()[1]
            panel.address.setText(f"http://127.0.0.1:{port}")
            panel.start_service()
            until(lambda: panel.service_ready, "Bundled web service did not become ready: " + panel.output)
            client = AccountClient(panel.url)
            # Confirm packaged HTML/CSS/JS resources before exercising the buttons.
            import requests
            with requests.Session() as http:
                http.trust_env = False
                for path in ("/", "/app.js", "/style.css", "/favicon.ico"):
                    assert http.get(panel.url + path, timeout=5).status_code == 200, path
            panel.email.setText("frozen-check@example.test")
            panel.password.setText("FixturePassword-123"); panel.confirm.setText("FixturePassword-123")
            panel.register_button.click(); until(lambda: panel.worker is None, "Registration did not finish")
            assert panel.user and panel.user["email"] == "frozen-check@example.test", panel.status.text()
            assert panel.store.load(panel.url) == panel.token
            token = panel.token
            assert AccountClient(panel.url, token).request("session")["authenticated"]
            report["bundled_web_and_registration"] = True
            page = window.notes_page
            until(lambda: page.worker is None and page.context is not None, "Memo account did not become ready")
            memo_file = page.store.create("超级备忘录验收", "桌面第一版")
            page.refresh_files(memo_file.name)
            import io
            from PIL import Image
            picture = io.BytesIO(); Image.new("RGB", (320, 90), "#39816c").save(picture, "PNG")
            page.insert_raw_image(picture.getvalue(), "验收图片.png")
            assert "omini-asset://" in page.editor.toPlainText()
            page.sync_now(); until(lambda: page.worker is None, "Memo image upload timed out")
            api_client = AccountClient(panel.url, panel.token)
            memo = api_client.api("/api/memos")["memos"][0]
            assert "omini-asset://" in memo["content"] and memo["version"] == 2
            updated = api_client.api("/api/memos", {**memo, "content": memo["content"] + "\n网页补充", "base_version": memo["version"]})["memo"]
            page.sync_now(); until(lambda: page.worker is None, "Memo download timed out")
            assert "网页补充" in page.editor.toPlainText()
            restored = api_client.api(f"/api/memos/{memo['id']}/restore", {"version": 2, "base_version": updated["version"]})["memo"]
            page.sync_now(); until(lambda: page.worker is None, "Memo rollback download timed out")
            assert "网页补充" not in page.editor.toPlainText()
            assert len(api_client.api(f"/api/memos/{memo['id']}/history")["versions"]) == 4
            report["memo_images_sync_and_rollback"] = True
            window.nav.setCurrentRow(3); qt.processEvents()
            assert window.grab().save(str(destination / f"super-memo-desktop-{app_module.APP_VERSION}.png"))
            panel.stop_service(); until(lambda: panel.process.state() == QProcess.ProcessState.NotRunning, "Web stop timed out")
            panel.start_service(); until(lambda: panel.service_ready, "Web restart timed out")
            until(lambda: panel.worker is None, "Session restore did not finish")
            assert AccountClient(panel.url, token).request("session")["authenticated"]
            panel.logout_button.click(); until(lambda: panel.worker is None, "Logout timed out")
            assert not AccountClient(panel.url, token).request("session")["authenticated"]
            assert not panel.store.path.exists()
            until(lambda: page.worker is None and page.context is None, "Memo logout did not restore local workspace")
            assert file.read_text(encoding="utf-8") == "关闭功能后保留笔记"
            report["durable_session_and_logout"] = True
            window.settings["theme"] = "浅色"; window.apply_theme(); window.open_account()
            qt.processEvents()
            screenshot = destination / f"settings-desktop-{app_module.APP_VERSION}.png"
            assert window.grab().save(str(screenshot))
            report["screenshot"] = str(screenshot)
            panel.stop_service(); until(lambda: panel.process.state() == QProcess.ProcessState.NotRunning, "Final stop timed out")
            window.quitting = True; window.close()
            until(lambda: not window.isVisible(), "Desktop did not exit")
            window.deleteLater(); window = None; QTest.qWait(100)
            report["ok"] = True
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        if window:
            window.quitting = True; window.close(); QTest.qWait(100)
        for item in reversed(patches): item.stop()
    (destination / f"settings-self-test-{app_module.APP_VERSION}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1
