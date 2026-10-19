"""Verify source or packaged sync UI against two real, isolated local peers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import tempfile
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from folder_sync import SyncManager
import folder_sync_page


def run_sync_test(app_module):
    qt = QApplication.instance() or QApplication([])
    qt.setQuitOnLastWindowClosed(False)
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        for name in ("msyh.ttc", "msyhbd.ttc"):
            font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
            if font.is_file():
                QFontDatabase.addApplicationFont(str(font))
    originals = {key: getattr(app_module, key) for key in
                 ("APP_DIR", "TASKS_FILE", "SETTINGS_FILE", "LOG_DIR", "TEMP_COOKIE_DIR")}
    original_manager = folder_sync_page.SyncManager
    original_tailscale = folder_sync_page.read_status
    report = {}
    window = None
    peer = None

    def port():
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    def wait_for(condition, timeout=40):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            qt.processEvents()
            if condition():
                return
            time.sleep(0.02)
        raise RuntimeError("Sync UI fixture timed out")

    try:
        with tempfile.TemporaryDirectory(prefix="omini-sync-release-") as directory:
            root = Path(directory)
            for key, relative in {"APP_DIR": ".", "TASKS_FILE": "tasks.json", "SETTINGS_FILE": "settings.json",
                                  "LOG_DIR": "logs", "TEMP_COOKIE_DIR": "cookies"}.items():
                setattr(app_module, key, root / relative)
            local_port, peer_port = port(), port()
            folder_sync_page.SyncManager = lambda path: SyncManager(
                path, local_only=True, listen_addresses=[f"tcp://127.0.0.1:{local_port}"])
            inventory = {"state": "Running", "self": {"ips": ["100.100.10.1"]}, "peers": [
                {"name": "我的另一台电脑", "ips": ["100.100.10.2"], "dns": "my-computer.example.ts.net", "online": True, "os": "windows"}]}
            folder_sync_page.read_status = lambda: inventory
            window = app_module.MainWindow()
            window.resize(1400, 900)
            window.nav.setCurrentRow(6)
            window.show()
            page = window.sync_page
            report["sidebar"] = window.stack.currentWidget() is page
            page.start_button.click()
            wait_for(lambda: bool(page.my_id.text()))
            report["device_identity"] = len(page.my_id.text()) == 63
            page.read_tailscale_button.click()
            wait_for(lambda: page.tailscale_address.text() == "100.100.10.1")
            report["optional_tailscale_inventory"] = True
            peer = SyncManager(root / "peer-config", local_only=True, listen_addresses=[f"tcp://127.0.0.1:{peer_port}"])
            peer.start()
            peer.device(page.my_id.text(), "本机测试", [f"tcp://127.0.0.1:{local_port}"])
            page.submit("device", peer.device_id, "另一台测试电脑", [f"127.0.0.1:{peer_port}"])
            wait_for(lambda: len(page.snapshot_data["devices"]) == 1)
            report["bare_address_normalization"] = page.snapshot_data["devices"][0]["addresses"] == [f"tcp://127.0.0.1:{peer_port}"]
            local_path, remote_path = root / "我的同步文件", root / "另一台电脑的文件"
            local_path.mkdir()
            remote_path.mkdir()
            (local_path / "同步测试.txt").write_text("来自本机的实际传输\n", encoding="utf-8")
            peer.folder({"id": "release-share", "label": "工作资料", "path": str(remote_path),
                         "devices": [page.my_id.text()], "mode": "auto"})
            page.submit("folder", {"id": "release-share", "label": "工作资料", "path": str(local_path),
                                   "devices": [peer.device_id], "mode": "auto"})
            wait_for(lambda: (remote_path / "同步测试.txt").exists())
            report["real_peer_transfer"] = (remote_path / "同步测试.txt").read_text(encoding="utf-8") == "来自本机的实际传输\n"
            page.submit("refresh")
            wait_for(lambda: page.folder_table.rowCount() == 1 and page.snapshot_data["folders"][0]["progress"] == "100.0%")
            page.folder_table.selectRow(0)
            page.copy_folder_button.click()
            report["copy_share_id"] = qt.clipboard().text() == "release-share"
            report["online_peer"] = page.snapshot_data["devices"][0]["connected"] is True
            report["tray_background"] = page.background_enabled is True
            image_path = os.environ.get("OMINI_SELF_TEST_IMAGE")
            if image_path:
                path = Path(image_path)
                qt.processEvents()
                report["dark_screenshot"] = window.grab().save(str(path))
                window.toggle_theme()
                qt.processEvents()
                report["light_screenshot"] = window.grab().save(str(path.with_name(path.stem + "-light.png")))
                window.resize(980, 680)
                qt.processEvents()
                report["minimum_screenshot"] = window.grab().save(str(path.with_name(path.stem + "-minimum.png")))
                dialog = folder_sync_page.DeviceDialog(parent=window, inventory=inventory)
                dialog.device_id.setText(peer.device_id)
                dialog.choose_tailscale(1)
                dialog.show()
                qt.processEvents()
                report["tailscale_dialog_screenshot"] = dialog.grab().save(str(path.with_name(path.stem + "-tailscale.png")))
                dialog.close()
                dialog.deleteLater()
            page.pause_button.click()
            wait_for(lambda: page.snapshot_data["folders"][0]["policy"].get("suspended"))
            report["pause_button"] = True
            peer.close()
            peer = None
            window.quitting = True
            window.close()
            wait_for(lambda: not window.isVisible() and not page.worker.isRunning(), timeout=25)
            report["clean_shutdown"] = True
    except Exception as error:
        report["error"] = str(error)
    finally:
        if peer:
            peer.close()
        if window:
            window.quitting = True
            window.sync_page.stop()
            window.sync_page.worker.wait(20000)
            window.close()
            qt.processEvents()
        folder_sync_page.SyncManager = original_manager
        folder_sync_page.read_status = original_tailscale
        for key, value in originals.items():
            setattr(app_module, key, value)
    path = os.environ.get("OMINI_SELF_TEST_LOG")
    if path:
        Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report and all(v is True for v in report.values()) else 1
