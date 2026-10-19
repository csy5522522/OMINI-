"""End-to-end tests with real, isolated Syncthing peers and native Qt controls."""
from datetime import datetime
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from folder_sync import SyncError, SyncManager, engine_path, next_due
from folder_sync_page import DeviceDialog, FolderDialog, FolderSyncPage


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class SyncIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not engine_path().is_file():
            raise unittest.SkipTest("Fetch the bundled engine before integration tests")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="omini-sync-test-")
        self.root = Path(self.temp.name)
        self.a_port, self.b_port = free_port(), free_port()
        self.a = SyncManager(self.root / "config-a", local_only=True,
                             listen_addresses=[f"tcp://127.0.0.1:{self.a_port}"])
        self.b = SyncManager(self.root / "config-b", local_only=True,
                             listen_addresses=[f"tcp://127.0.0.1:{self.b_port}"])
        try:
            self.a.start()
            self.b.start()
            self.a.device(self.b.device_id, "电脑 B", [f"127.0.0.1:{self.b_port}"])
            self.b.device(self.a.device_id, "电脑 A", [f"tcp://127.0.0.1:{self.a_port}"])
            self.left = self.root / "左边文件夹"
            self.right = self.root / "另一台电脑"
            self.left.mkdir()
            self.right.mkdir()
        except Exception:
            self.a.close()
            self.b.close()
            self.temp.cleanup()
            raise

    def tearDown(self):
        self.a.close()
        self.b.close()
        self.temp.cleanup()

    def wait_until(self, condition, seconds=40):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.2)
        self.fail("Timed out waiting for real peer synchronization")

    def add_folders(self, mode="auto"):
        for manager, peer, path in ((self.a, self.b, self.left), (self.b, self.a, self.right)):
            manager.folder({"id": "test-share", "path": str(path), "devices": [peer.device_id], "mode": mode})

    @staticmethod
    def read(path):
        try:
            return path.read_bytes()
        except (FileNotFoundError, PermissionError):
            return None

    def scan(self, manager):
        manager.api("POST", "db/scan", params={"folder": "test-share"}, timeout=30)

    def test_real_bidirectional_watch_transfer_delete_versions_and_restart(self):
        payload = bytes(range(256)) * 8192
        (self.left / "中文 文档.bin").write_bytes(payload)
        (self.right / "from-b.txt").write_text("B 的初始文件", encoding="utf-8")
        self.add_folders()
        self.wait_until(lambda: self.read(self.right / "中文 文档.bin") == payload)
        self.wait_until(lambda: self.read(self.left / "from-b.txt") == "B 的初始文件".encode())
        # No REST scan: exercise the native filesystem watcher in both directions.
        (self.right / "from-b.txt").write_text("B 修改后的文件", encoding="utf-8")
        self.wait_until(lambda: self.read(self.left / "from-b.txt") == "B 修改后的文件".encode())
        (self.left / "中文 文档.bin").unlink()
        self.wait_until(lambda: not (self.right / "中文 文档.bin").exists())
        self.assertTrue(any(p.is_file() for p in (self.right / ".stversions").rglob("*")))
        self.assertIn(".stversions", self.b.api("GET", "config/folders")[0].get("versioning", {}).get("fsPath", ".stversions") or ".stversions")
        own_id = self.a.device_id
        self.a.stop()
        self.a.start()
        self.assertEqual(self.a.device_id, own_id)
        self.assertEqual(self.a.snapshot()["folders"][0]["policy"]["mode"], "auto")
        # Removal only affects configuration, leaving user files intact.
        self.a.remove_folder("test-share")
        self.assertTrue((self.left / "from-b.txt").exists())
        self.assertEqual(self.a.api("GET", "config/folders"), [])

    def test_manual_pending_peer_completion_and_recovered_pause(self):
        (self.left / "manual.txt").write_text("manual", encoding="utf-8")
        self.add_folders("manual")
        self.assertTrue(self.a.api("GET", "config/folders")[0]["paused"])
        self.assertFalse((self.right / "manual.txt").exists())
        self.a.sync_now("test-share")
        self.wait_until(lambda: self.a.snapshot()["devices"][0]["connected"])
        snapshot = self.a.snapshot()
        self.assertNotEqual(snapshot["folders"][0]["status"], "已同步")
        self.assertIn("test-share", self.a.active)
        self.b.sync_now("test-share")
        self.wait_until(lambda: self.read(self.right / "manual.txt") == b"manual")
        # Poll genuine completion; a synthetic later timestamp avoids a 20s grace wait.
        def finished():
            self.a.snapshot(time.time() + 25)
            self.b.snapshot(time.time() + 25)
            return not self.a.active and not self.b.active
        self.wait_until(finished)
        self.assertTrue(self.a.api("GET", "config/folders")[0]["paused"])
        self.a.sync_now("test-share")
        self.a.stop()
        self.a.start()
        self.assertTrue(self.a.api("GET", "config/folders")[0]["paused"])
        self.assertEqual(self.a.active, {})

    def test_interval_missed_run_suspension_and_folder_validation(self):
        self.add_folders("interval")
        policy = self.a.settings["policies"]["test-share"]
        due = policy["next_run"]
        self.a.snapshot(due - 1)
        self.assertNotIn("test-share", self.a.active)
        self.a.snapshot(due + 3600)
        self.assertIn("test-share", self.a.active)
        self.assertGreater(policy["next_run"], due + 3600)
        self.a.pause("test-share")
        self.a.snapshot(policy["next_run"] + 10)
        self.assertNotIn("test-share", self.a.active)
        self.assertTrue(policy["suspended"])
        for values in (
            {"id": "../bad", "path": str(self.left), "devices": [self.b.device_id]},
            {"id": "nested", "path": str(self.left), "devices": [self.b.device_id]},
            {"id": "internal", "path": str(self.a.home), "devices": [self.b.device_id]},
            {"id": "empty", "path": "", "devices": [self.b.device_id]},
            {"id": "peerless", "path": str(self.root), "devices": []},
            {"id": "test-share", "path": str(self.left), "devices": [self.b.device_id]},
        ):
            with self.subTest(values=values), self.assertRaises(SyncError):
                self.a.folder(values)
        with self.assertRaises(SyncError):
            self.a.device(self.a.device_id, "self")
        with self.assertRaises(SyncError):
            self.a.device("bad-id", "bad")
        duplicate = SyncManager(self.a.directory, local_only=True)
        try:
            with self.assertRaises(SyncError):
                duplicate.start()
        finally:
            duplicate.close()

    def test_offline_conflicting_edits_preserve_both_versions(self):
        self.add_folders()
        (self.left / "conflict.txt").write_bytes(b"original")
        self.scan(self.a)
        self.wait_until(lambda: self.read(self.right / "conflict.txt") == b"original")
        # Pause peer connections while indexing concurrent changes on both devices.
        self.a.api("PATCH", "config/devices/" + self.b.device_id, {"paused": True})
        self.wait_until(lambda: not self.a.api("GET", "system/connections")["connections"].get(self.b.device_id, {}).get("connected"))
        (self.left / "conflict.txt").write_bytes(b"edited by A")
        (self.right / "conflict.txt").write_bytes(b"edited by B")
        self.scan(self.a)
        self.scan(self.b)
        self.a.api("PATCH", "config/devices/" + self.b.device_id, {"paused": False})
        self.wait_until(lambda: bool(list(self.left.glob("*sync-conflict*")) or list(self.right.glob("*sync-conflict*"))))
        copies = [p.read_bytes() for path in (self.left, self.right) for p in path.glob("*conflict*.txt")]
        self.assertIn(b"edited by A", copies)
        self.assertIn(b"edited by B", copies)


class SchedulingTest(unittest.TestCase):
    def test_daily_local_time_and_interval(self):
        now = datetime(2026, 10, 19, 19, 45).timestamp()
        policy = {"mode": "daily", "time": "20:00"}
        self.assertEqual(datetime.fromtimestamp(next_due(policy, now)), datetime(2026, 10, 19, 20))
        self.assertEqual(datetime.fromtimestamp(next_due(policy, now + 3600)), datetime(2026, 10, 20, 20))
        self.assertEqual(next_due({"mode": "interval", "minutes": 5}, now), now + 300)

    def test_corrupt_settings_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("{bad settings}")
            with self.assertRaises(SyncError):
                SyncManager(directory)
            self.assertEqual(path.read_text(), "{bad settings}")


class SyncUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        cls.qt.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="omini-sync-ui-")
        self.root = Path(self.temp.name)
        manager = SyncManager(self.root / "sync", local_only=True, listen_addresses=["tcp://127.0.0.1:0"])
        self.page = FolderSyncPage(manager.directory, manager=manager)
        self.page.resize(1060, 790)
        self.page.show()

    def tearDown(self):
        self.page.stop()
        self.assertTrue(self.page.worker.wait(20000))
        self.page.close()
        self.page.deleteLater()
        self.qt.processEvents()
        self.temp.cleanup()

    def wait_until(self, condition, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.qt.processEvents()
            if condition():
                return
            QTest.qWait(20)
        self.fail("Timed out waiting for native UI")

    def test_native_start_pair_folder_copy_pause_and_remove(self):
        QTest.mouseClick(self.page.start_button, Qt.MouseButton.LeftButton)
        self.wait_until(lambda: bool(self.page.my_id.text()))
        QTest.mouseClick(self.page.copy_id_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.qt.clipboard().text(), self.page.my_id.text())
        # A second real identity checks ID validation without starting another daemon.
        other = SyncManager(self.root / "other", local_only=True, listen_addresses=["tcp://127.0.0.1:0"])
        try:
            other.start()
            values = (other.device_id, "远程测试电脑", ["tcp://127.0.0.1:1"])
            with patch.object(DeviceDialog, "exec", return_value=QDialog.DialogCode.Accepted), patch.object(DeviceDialog, "values", return_value=values):
                self.page.add_device()
            self.wait_until(lambda: self.page.device_table.rowCount() == 1)
            target = self.root / "data"
            target.mkdir()
            values = {"id": "ui-share", "path": str(target), "devices": [other.device_id], "mode": "manual"}
            with patch.object(FolderDialog, "exec", return_value=QDialog.DialogCode.Accepted), patch.object(FolderDialog, "values", return_value=values):
                QTest.mouseClick(self.page.add_folder_button, Qt.MouseButton.LeftButton)
            self.wait_until(lambda: self.page.folder_table.rowCount() == 1)
            self.page.folder_table.selectRow(0)
            QTest.mouseClick(self.page.copy_folder_button, Qt.MouseButton.LeftButton)
            self.assertEqual(self.qt.clipboard().text(), "ui-share")
            QTest.mouseClick(self.page.sync_button, Qt.MouseButton.LeftButton)
            self.wait_until(lambda: self.page.snapshot_data["folders"][0]["status"] == "等待其他电脑")
            QTest.mouseClick(self.page.pause_button, Qt.MouseButton.LeftButton)
            self.wait_until(lambda: self.page.snapshot_data["folders"][0]["policy"].get("suspended"))
            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                self.page.remove_folder_button.click()
            self.wait_until(lambda: self.page.folder_table.rowCount() == 0)
            self.assertTrue(target.is_dir())
        finally:
            other.close()

    def test_dialog_modes_and_local_path(self):
        dialog = FolderDialog([{"deviceID": "example", "name": "另一台电脑"}])
        self.assertEqual(dialog.values()["mode"], "auto")
        self.assertFalse(dialog.minutes.isEnabled())
        dialog.mode.setCurrentIndex(dialog.mode.findData("interval"))
        self.assertTrue(dialog.minutes.isEnabled())
        dialog.mode.setCurrentIndex(dialog.mode.findData("daily"))
        self.assertTrue(dialog.clock.isEnabled())
        dialog.close()


if __name__ == "__main__":
    unittest.main()
