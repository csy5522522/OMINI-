"""Isolated full-window verification, also runnable inside the frozen desktop app."""
from __future__ import annotations

import json
import os
import socket
import tempfile
import time
import traceback
from pathlib import Path
from unittest.mock import patch
from ai_assistants import PoolClient, atomic_json
from codex_quota import parse_codex_quota
from ai_assistants_page import AiAssistantsPage
from assistant_data import AssistantState

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
                assert page.tabs.count() == 7
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
                while (not page.ready or page.worker) and time.monotonic() < deadline:
                    QTest.qWait(30)
                assert page.ready, page.status.text() + page.log.toPlainText()
                assert page.worker is None, "Gateway startup worker has not finished."
                client = PoolClient(page.active_pool)
                for index in range(3):
                    credential = root / f'fixture-{index}.json'
                    atomic_json(credential, {"type":"codex", "email":f"member-{index+1}@example.com",
                        "account_id":f"fixture-member-{index}", "access_token":f"fixture-access-{index}",
                        "refresh_token":f"fixture-refresh-{index}", "expired":"2099-01-01T00:00:00Z",
                        "last_refresh":"2099-01-01T00:00:00Z"})
                    client.import_account(credential)
                page.set_accounts(client.accounts()); page.table.selectRow(0)
                assert page.table.columnCount() == 7 and page.strategy.count() == 4
                def fixture_quota(row):
                    index = next(i for i, item in enumerate(page.accounts) if item['name'] == row['name'])
                    return parse_codex_quota({"plan_type":"plus", "rate_limit": {
                        "primary_window":{"used_percent":index*20+10,"limit_window_seconds":18000,"reset_after_seconds":12000},
                        "secondary_window":{"used_percent":index*15+20,"limit_window_seconds":604800,"reset_after_seconds":300000}}})
                with patch.object(PoolClient, 'quota', side_effect=fixture_quota):
                    page.query_quotas(True)
                    deadline = time.monotonic()+10
                    while page.worker and time.monotonic()<deadline: QTest.qWait(20)
                assert page.worker is None and len(page.quotas) == 3
                assert all('剩余' in page.table.item(i, 2).text() for i in range(3))
                page.strategy.setCurrentIndex(page.strategy.findData('balanced'))
                page.apply_schedule()
                deadline = time.monotonic()+10
                while page.worker and time.monotonic()<deadline: QTest.qWait(20)
                assert page.worker is None
                routing = client.request('GET', '/config/routing')
                assert routing['strategy'] == 'round-robin' and not routing['session-affinity']
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
                page.model.setCurrentText("fixture-persisted-model")
                page.stop()  # Full app exit preserves the user's last running intent.
                assert not page.is_running
                assert not runtime.exists()
                assert not (page.root / "pool.lock").exists()
                assert (page.root / "settings.json").exists()
                assert AssistantState(page.root).data["service_wanted"]
                restarted = AiAssistantsPage(page.root)
                try:
                    assert restarted.table.rowCount() == 3 and not restarted.is_running
                    assert all("上次" in restarted.table.item(i, 2).text() for i in range(3))
                    assert restarted.model.currentText() == "fixture-persisted-model"
                    assert restarted.strategy.currentData() == "balanced"
                    assert "额度查询完成" in restarted.history_view.toPlainText()
                    deadline = time.monotonic() + 20
                    while (not restarted.ready or restarted.worker) and time.monotonic() < deadline:
                        QTest.qWait(30)
                    assert restarted.ready, restarted.log.toPlainText()
                    restarted.stop_pool()
                    assert not AssistantState(page.root).data["service_wanted"]
                    assert restarted.table.rowCount() == 3
                finally:
                    restarted.stop()
                    if restarted.worker: restarted.worker.wait(15000)
                    restarted.deleteLater(); qt.processEvents()
                stopped = AiAssistantsPage(page.root)
                try:
                    QTest.qWait(700)
                    assert not stopped.is_running and stopped.table.rowCount() == 3
                    assert "手动停止" in stopped.history_view.toPlainText()
                    if capture:
                        stopped.resize(1060, 760)
                        stopped.show(); QTest.qWait(100)
                        assert stopped.grab().save(str(capture_path / "ai-assistants-restored-offline.png"))
                        stopped.tabs.setCurrentIndex(5); QTest.qWait(100)
                        assert stopped.grab().save(str(capture_path / "ai-assistants-history.png"))
                finally:
                    stopped.stop(); stopped.close(); stopped.deleteLater(); qt.processEvents()
                window.quitting = True
                window.close()
                qt.processEvents()
                report = {"ok": True, "version": app_module.APP_VERSION, "tabs": page.tabs.count(),
                          "independent_proxy_profiles": True, "gateway_start_stop": True,
                          "background_page_switch": True, "tray_background": True, "runtime_cleanup": True,
                          "quota_columns_and_batch_query": True, "live_balanced_scheduling": True,
                          "offline_members_after_restart": True, "persistent_quotas_and_history": True,
                          "resume_running_service": True, "manual_stop_preserved": True}
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
        report["error"] = str(error) or type(error).__name__
        report["traceback"] = traceback.format_exc()
    if os.environ.get("OMINI_SELF_TEST_LOG"):
        target = Path(os.environ["OMINI_SELF_TEST_LOG"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report["ok"] else 1
