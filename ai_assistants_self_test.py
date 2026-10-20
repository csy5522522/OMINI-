"""Isolated full-window verification, also runnable inside the frozen desktop app."""
from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication


def run_ai_assistants_test(app_module) -> int:
    report = {"ok": False}
    window = None
    try:
        qt = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory(prefix="omini-ai-desktop-check-") as directory:
            root = Path(directory)
            patches = [patch.object(app_module, key, root / value) for key, value in {
                "APP_DIR": ".", "TASKS_FILE": "tasks.json", "SETTINGS_FILE": "settings.json",
                "LOG_DIR": "logs", "TEMP_COOKIE_DIR": "runtime-cookies",
            }.items()]
            for item in patches:
                item.start()
            try:
                app_module.ensure_app_dirs()
                app_module.save_json(app_module.SETTINGS_FILE, {"theme": "深色", "omini_api_workspace": str(root / "api")})
                window = app_module.MainWindow()
                window.show()
                window.nav.setCurrentRow(8)
                qt.processEvents()
                page = window.ai_assistants_page
                assert window.stack.currentWidget() is page
                assert "AI 编程助手" in window.nav.item(8).text()
                assert page.tabs.count() == 6
                assert not page.is_running
                page.profiles["claude"].proxy.setText("http://127.0.0.1:7899")
                page.save_profile("claude")
                assert page.profiles["codex"].proxy.text() == "http://127.0.0.1:7890"
                page.tabs.setCurrentIndex(4)
                page.pool_proxy.clear()
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    page.port.setValue(listener.getsockname()[1])
                page.start_pool()
                deadline = time.monotonic() + 20
                while not page.ready and time.monotonic() < deadline:
                    QTest.qWait(30)
                assert page.ready, page.status.text() + page.log.toPlainText()
                runtime = Path(page.runtime.name)
                assert page.login_button.isEnabled()
                with patch.object(app_module.QSystemTrayIcon, "isSystemTrayAvailable", return_value=True), \
                        patch.object(window.tray, "showMessage"):
                    window.close()
                    qt.processEvents()
                    assert not window.isVisible()
                    assert page.is_running
                    window.show()
                window.nav.setCurrentRow(0)
                assert page.is_running
                window.nav.setCurrentRow(8)
                capture = os.environ.get("OMINI_AI_CAPTURE_DIR")
                if capture:
                    capture_path = Path(capture)
                    capture_path.mkdir(parents=True, exist_ok=True)
                    for mode, name in (("深色", "dark"), ("浅色", "light")):
                        window.settings["theme"] = mode
                        window.apply_theme()
                        QTest.qWait(100)
                        assert window.grab().save(str(capture_path / f"ai-assistants-{name}.png"))
                    page.tabs.setCurrentIndex(0)
                    QTest.qWait(100)
                    assert window.grab().save(str(capture_path / "ai-assistants-profile.png"))
                    page.tabs.setCurrentIndex(4)
                    window.resize(980, 680)
                    QTest.qWait(100)
                    assert window.grab().save(str(capture_path / "ai-assistants-compact.png"))
                page.stop_pool()
                assert not page.is_running
                assert not runtime.exists()
                assert not (page.root / "pool.lock").exists()
                assert (page.root / "settings.json").exists()
                window.quitting = True
                window.close()
                qt.processEvents()
                report = {"ok": True, "version": app_module.APP_VERSION, "tabs": page.tabs.count(),
                          "independent_proxy_profiles": True, "gateway_start_stop": True,
                          "background_page_switch": True, "tray_background": True, "runtime_cleanup": True}
            finally:
                if window is not None:
                    window.ai_assistants_page.stop()
                    if window.ai_assistants_page.worker:
                        window.ai_assistants_page.worker.wait(15000)
                    window.quitting = True
                    window.close()
                    window.deleteLater()
                    qt.processEvents()
                for item in reversed(patches):
                    item.stop()
    except Exception as error:
        report["error"] = str(error)
    if os.environ.get("OMINI_SELF_TEST_LOG"):
        target = Path(os.environ["OMINI_SELF_TEST_LOG"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report["ok"] else 1
