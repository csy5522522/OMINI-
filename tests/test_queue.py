"""Button-level regressions in isolated state, using real download worker threads."""
import json
import tempfile
import threading
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

import app


class ControlledProcess:
    def __init__(self):
        self.release = threading.Event()
        self.code = None
        self.stdout = self.lines()

    def lines(self):
        yield "[download] 10.0% of 1MiB at 1MiB/s ETA 00:01\n"
        self.release.wait(10)
        # A late queued progress signal must not resurrect cleared rows.
        yield "[download] 80.0% of 1MiB\n"

    def complete(self, code=0):
        self.code = code
        self.release.set()

    def terminate(self):
        self.complete(-15)

    def kill(self):
        self.complete(-9)

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        self.release.wait(timeout or 10)
        return self.code if self.code is not None else 1


class QueueButtonsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="omini-queue-test-")
        self.root = Path(self.temp.name)
        self.patches = [patch.object(app, key, self.root / value) for key, value in {
            "APP_DIR": ".", "TASKS_FILE": "tasks.json", "SETTINGS_FILE": "settings.json",
            "LOG_DIR": "logs", "TEMP_COOKIE_DIR": "runtime-cookies",
        }.items()]
        self.processes = []
        def popen(*args, **kwargs):
            process = ControlledProcess()
            self.processes.append(process)
            return process
        self.patches += [patch("campus_page.wifi_ssids", return_value=()),
                         patch.object(app, "build_command", return_value=["test-engine"]),
                         patch.object(app.subprocess, "Popen", side_effect=popen)]
        for item in self.patches:
            item.start()
        app.ensure_app_dirs()
        history = [app.DownloadTask(id=f"old-{index}", type="video",
                    source=f"https://example.com/{index}", output_dir=str(self.root),
                    status="failed") for index in range(60)]
        app.save_json(app.TASKS_FILE, [asdict(task) for task in history])
        self.window = app.MainWindow()
        self.page = self.window.download_page
        self.manager = self.window.manager
        self.window.show()
        self.qt.processEvents()
        self.submit = next(button for button in self.page.tabs.widget(0).findChildren(QPushButton)
                           if button.text() == "加入下载队列")

    def until(self, predicate):
        deadline = time.monotonic() + 4
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate())

    def click(self, button):
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        self.qt.processEvents()

    def add_video(self):
        self.page.video_url.setText("复制打开抖音 https://v.douyin.com/tr651XRKHfk/ 更多文字")
        self.page.video_dir.edit.setText(str(self.root))
        self.click(self.submit)
        return self.manager.tasks[0]

    def tearDown(self):
        self.manager.clear_queue()
        self.until(lambda: not self.manager.workers)
        self.window.close()
        self.window.deleteLater()
        self.qt.processEvents()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_new_task_visible_selected_and_actions(self):
        self.page.table.selectRow(40)
        task = self.add_video()
        self.until(lambda: task.progress == 10)
        self.assertEqual(task.source, "https://v.douyin.com/tr651XRKHfk/")
        self.assertEqual(self.page.rows[task.id], 0)
        self.assertEqual(self.page.current_task_id, task.id)
        self.assertIn("[download]", self.page.log.toPlainText())
        self.assertIn(task.source, self.page.queue_feedback.text())
        for task_id, row in self.page.rows.items():
            self.assertEqual(self.page.table.item(row, 0).data(Qt.ItemDataRole.UserRole), task_id)
        self.click(self.page.pause_button)
        self.until(lambda: task.status == "paused" and not self.manager.workers)
        self.assertTrue(self.page.resume_button.isEnabled())
        self.click(self.page.resume_button)
        self.until(lambda: len(self.processes) == 2)
        self.click(self.page.cancel_button)
        self.until(lambda: task.status == "cancelled" and not self.manager.workers)
        self.page.table.clearSelection()
        self.assertFalse(self.page.pause_button.isEnabled())
        self.assertFalse(self.page.retry_button.isEnabled())
        self.assertFalse(self.page.open_button.isEnabled())

    def test_retry_failure_then_success_and_duplicate_guard(self):
        self.page.table.selectRow(0)
        task = self.manager.tasks[0]
        self.click(self.page.retry_button)
        self.until(lambda: bool(self.processes))
        self.processes[-1].complete(1)
        self.until(lambda: task.status == "failed" and not self.manager.workers)
        self.click(self.page.retry_button)
        self.until(lambda: len(self.processes) == 2)
        worker = self.manager.workers[task.id]
        # Model retry arriving before QThread.finished is delivered.
        task.status = "failed"
        self.manager.retry(task.id)
        self.assertIs(self.manager.workers[task.id], worker)
        self.assertEqual(len(self.processes), 2)
        self.processes[-1].complete()
        self.until(lambda: task.status == "completed" and not self.manager.workers)
        self.assertIn("文件已保存", self.page.queue_feedback.text())

    def test_bulk_clear_running_queued_history_and_restart(self):
        self.add_video()
        self.add_video()
        queued = self.add_video()
        self.until(lambda: len(self.processes) == 2)
        self.assertEqual(queued.status, "queued")
        media = self.root / "keep.mp4"
        media.write_bytes(b"existing downloaded media")
        removed = self.manager.tasks.copy()
        started = time.monotonic()
        self.click(self.page.clear_button)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(self.manager.tasks, [])
        self.assertEqual(self.page.table.rowCount(), 0)
        self.assertEqual(self.page.log.toPlainText(), "")
        self.assertFalse(self.page.clear_button.isEnabled())
        self.assertEqual(json.loads(app.TASKS_FILE.read_text(encoding="utf-8")), [])
        backup = next((app.APP_DIR / "queue-backups").glob("*.json"))
        self.assertEqual(len(json.loads(backup.read_text(encoding="utf-8"))), 63)
        self.assertEqual(media.read_bytes(), b"existing downloaded media")
        # Late updates should be harmless even when another task is submitted immediately.
        new_task = self.add_video()
        for task in removed:
            self.manager._on_progress(task.id, 99, "test", "test")
            self.page.update_row(task)
        self.until(lambda: len(self.processes) == 3)
        self.processes[-1].complete()
        self.until(lambda: new_task.status == "completed" and not self.manager.workers)
        self.assertEqual(self.page.table.rowCount(), 1)
        self.assertEqual(len(app.TaskManager(self.manager.settings).tasks), 1)

    def test_stop_before_process_spawn(self):
        task = app.DownloadTask(id="stopped", type="video", source="https://example.com", output_dir=str(self.root))
        worker = app.DownloadWorker(task)
        worker.request_stop()
        worker.run()
        self.assertEqual(self.processes, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
