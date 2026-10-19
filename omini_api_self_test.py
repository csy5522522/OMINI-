"""Exercise the desktop API page in source mode and in a frozen executable."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from omini_api.runtime import base_command


def _check_balance_worker(root: Path) -> None:
    class BalanceFixture(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/balance"
            assert self.headers.get("Authorization") == "Bearer fixture-token"
            body = json.dumps({"code": 0, "data": {"balance": 3.25}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), BalanceFixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    data = root / "worker fixture"
    data.mkdir()
    base = f"http://127.0.0.1:{server.server_port}"
    profile = {"id": "worker-fixture", "name": "Fixture", "home_url": base, "api_base": base,
               "auth": {"header": "Authorization", "template": "Bearer {access_token}"},
               "envelope": {"code_path": "code", "success_values": [0], "data_path": "data"},
               "operations": {"balance": {"method": "GET", "path": "/balance", "auth": True,
                                            "response": {"balance": "balance"}}}}
    (data / "omini_sites.json").write_text(json.dumps({"version": 1, "sites": [profile]}), encoding="utf-8")
    account = data / "xhhai_codex_account_fixture.json"
    account.write_text(json.dumps({"email": "fixture@example.com", "site": {"id": "worker-fixture"},
                                   "registration": {"access_token": "fixture-token"}}), encoding="utf-8")
    environment = os.environ.copy()
    environment["OMINI_API_TOOLBOX_HOME"] = str(data)
    try:
        worker = subprocess.run(base_command() + ["--worker", "account-balances", "--accounts", str(account),
                                                 "--proxy-mode", "直连"],
                                cwd=str(data), env=environment, capture_output=True,
                                text=True, encoding="utf-8", timeout=25,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert worker.returncode == 0, worker.stderr + worker.stdout
        assert "remaining=3.250000" in worker.stdout, worker.stdout
        saved = json.loads(account.read_text(encoding="utf-8"))
        assert saved["balance_cache"]["balance"] == 3.25
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _write_report(report: dict) -> None:
    if os.environ.get("OMINI_SELF_TEST_LOG"):
        report_path = Path(os.environ["OMINI_SELF_TEST_LOG"])
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))


def run_api_test(app_module) -> int:
    try:
        return _run_api_test(app_module)
    except Exception as error:
        _write_report({"ok": False, "error": str(error)})
        return 1


def _run_api_test(app_module) -> int:
    qt = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(prefix="omini-api-desktop-check-") as directory:
        root = Path(directory)
        patches = [patch.object(app_module, key, root / name) for key, name in {
            "APP_DIR": ".", "TASKS_FILE": "tasks.json", "SETTINGS_FILE": "settings.json",
            "LOG_DIR": "logs", "TEMP_COOKIE_DIR": "runtime-cookies",
        }.items()]
        for item in patches:
            item.start()
        window = None
        try:
            app_module.ensure_app_dirs()
            app_module.save_json(app_module.SETTINGS_FILE, {
                "omini_api_workspace": str(root / "api data"), "theme": "深色",
            })
            window = app_module.MainWindow()
            window.show()
            api = window.omini_api_page
            assert not api.is_running, "API must start only when its page opens"
            window.nav.setCurrentRow(7)

            def until(predicate):
                deadline = time.monotonic() + 25
                while not predicate() and time.monotonic() < deadline:
                    QTest.qWait(50)
                assert predicate(), api.diagnostic + api.status.text()

            until(lambda: len(api.runtime_state.get("tabs", [])) == 7)
            assert window.stack.currentWidget() is api
            assert api.container is not None
            assert not api.diagnostic, api.diagnostic
            process_id = int(api.process.processId())
            window.nav.setCurrentRow(0)
            QTest.qWait(200)
            window.nav.setCurrentRow(7)
            assert int(api.process.processId()) == process_id
            window.toggle_theme()
            until(lambda: api.runtime_state.get("dark") is False)
            window.toggle_theme()
            until(lambda: api.runtime_state.get("dark") is True)
            window.resize(1440, 960)
            QTest.qWait(300)
            image_path = Path(os.environ.get("OMINI_API_TEST_SCREENSHOT", str(root / "desktop.png")))
            image_path.parent.mkdir(parents=True, exist_ok=True)
            assert qt.primaryScreen().grabWindow(int(window.winId())).save(str(image_path))
            _check_balance_worker(root)
            tabs = api.runtime_state["tabs"]
            window.quit_from_tray()
            until(lambda: not api.is_running)
            saved = root / "api data" / "omini_api_toolbox_settings.json"
            assert saved.is_file(), "API settings must be saved on desktop shutdown"
            report = {"ok": True, "tabs": tabs, "theme_switch": True,
                      "navigation_preserves_process": True, "worker_dispatch": True,
                      "settings_saved": True, "process_stopped": True,
                      "screenshot": str(image_path)}
            _write_report(report)
            return 0
        finally:
            if window is not None:
                window.omini_api_page.stop()
                window.quitting = True
                window.close()
                qt.processEvents()
            for item in reversed(patches):
                item.stop()
