"""Source/packaged update workflow verification using a real local HTTP server."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QScrollArea

from updater import GitHubUpdater, verify_installer
import updater_page


def run_update_test(app_module):
    qt = QApplication.instance() or QApplication([])
    qt.setQuitOnLastWindowClosed(False)
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        for name in ("msyh.ttc", "msyhbd.ttc"):
            path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
            if path.exists():
                QFontDatabase.addApplicationFont(str(path))
    version = app_module.APP_VERSION.split(".")
    next_version = ".".join(version[:2] + [str(int(version[2]) + 1)])
    payload = b"MZ" + b"update-download-fixture" * 12000
    digest = hashlib.sha256(payload).hexdigest()
    name = f"Omini-Toolbox-{next_version}-Setup.exe"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path.endswith("/latest"):
                body = json.dumps({"tag_name": "v" + next_version, "body": "示例更新说明\n\n新增检查更新、GitHub 下载与完整性校验。",
                    "assets": [{"id": 1, "name": name, "state": "uploaded", "size": len(payload), "digest": "sha256:" + digest}]}).encode()
                kind = "application/json"
            else:
                body = payload
                kind = "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    original_client = updater_page.GitHubUpdater
    updater_page.GitHubUpdater = lambda version, token: GitHubUpdater(version, token, f"http://127.0.0.1:{server.server_port}")
    originals = {key: getattr(app_module, key) for key in
                 ("APP_DIR", "TASKS_FILE", "SETTINGS_FILE", "LOG_DIR", "TEMP_COOKIE_DIR")}
    report = {}
    window = None

    def wait_for(condition, seconds=25):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            qt.processEvents()
            if condition():
                return
            time.sleep(0.01)
        raise RuntimeError("Update workflow timed out")

    try:
        with tempfile.TemporaryDirectory(prefix="omini-updater-release-") as directory:
            root = Path(directory)
            for key, relative in {"APP_DIR": ".", "TASKS_FILE": "tasks.json", "SETTINGS_FILE": "settings.json",
                                  "LOG_DIR": "logs", "TEMP_COOKIE_DIR": "cookies"}.items():
                setattr(app_module, key, root / relative)
            window = app_module.MainWindow()
            window.resize(1280, 980)
            window.nav.setCurrentRow(4)
            window.show()
            page = window.settings_page.updates
            report["settings_button"] = page.check_button.isEnabled()
            page.check_button.click()
            wait_for(lambda: page.worker is None)
            report["new_release_detected"] = page.release is not None and page.download_button.isEnabled()
            page.download_button.click()
            wait_for(lambda: page.worker is None)
            report["download_and_hash_verified"] = page.artifact is not None and page.install_button.isEnabled()
            verify_installer(page.artifact)
            image_path = os.environ.get("OMINI_SELF_TEST_IMAGE")
            if image_path:
                path = Path(image_path)
                qt.processEvents()
                report["dark_screenshot"] = window.grab().save(str(path))
                window.toggle_theme()
                qt.processEvents()
                report["light_screenshot"] = window.grab().save(str(path.with_name(path.stem + "-light.png")))
                window.resize(980, 680)
                window.settings_page.findChild(QScrollArea).ensureWidgetVisible(page.check_button)
                qt.processEvents()
                report["minimum_screenshot"] = window.grab().save(str(path.with_name(path.stem + "-minimum.png")))
            page.install_button.click()
            wait_for(lambda: window.pending_update is not None and not window.isVisible())
            wait_for(lambda: not window.sync_page.worker.isRunning())
            report["verified_install_handoff"] = window.pending_update.sha256 == digest
            report["shutdown_before_installer"] = True
    except Exception as error:
        report["error"] = str(error)
    finally:
        if window:
            window.pending_update = None
            window.quitting = True
            window.settings_page.updates.stop()
            worker = window.settings_page.updates.worker
            if worker:
                worker.wait(30000)
            window.sync_page.stop()
            window.sync_page.worker.wait(20000)
            window.close()
            qt.processEvents()
        updater_page.GitHubUpdater = original_client
        for key, value in originals.items():
            setattr(app_module, key, value)
        server.shutdown()
        server.server_close()
    log = os.environ.get("OMINI_SELF_TEST_LOG")
    if log:
        Path(log).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report and all(value is True for value in report.values()) else 1
