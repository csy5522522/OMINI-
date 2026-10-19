"""Integration regressions for embedded GUI, subprocess dispatch and persistence."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from omini_api import runtime
from omini_api_page import OminiApiPage


class ApiRuntimeTests(unittest.TestCase):
    def test_worker_runs_from_a_data_directory_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix="omini api dispatch ") as directory:
            environment = os.environ.copy()
            environment["OMINI_API_TOOLBOX_HOME"] = directory
            result = subprocess.run(runtime.base_command() + ["--worker", "--help"],
                                    cwd=directory, env=environment, capture_output=True,
                                    text=True, encoding="utf-8", timeout=15,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("local-proxy", result.stdout)
            self.assertIn("account-balances", result.stdout)

    def test_frozen_workers_use_the_api_dispatcher(self):
        with patch.object(sys, "frozen", True, create=True):
            self.assertEqual(runtime.base_command(), [str(Path(sys.executable).resolve()), "--omini-api"])

    def test_explicit_data_directory_has_priority_over_legacy_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(runtime.initial_workspace(directory), Path(directory).resolve())

    @unittest.skipUnless(sys.platform == "win32", "Windows parent watchdog")
    def test_embedded_process_saves_and_exits_when_its_parent_exits(self):
        with tempfile.TemporaryDirectory(prefix="omini-api-owner-") as directory:
            root = Path(directory)
            control = root / "control"
            control.mkdir()
            parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                      creationflags=subprocess.CREATE_NO_WINDOW)
            environment = os.environ.copy()
            environment["OMINI_API_TOOLBOX_HOME"] = str(root / "data")
            environment["OMINI_API_PARENT_PID"] = str(parent.pid)
            child = subprocess.Popen(runtime.base_command() + ["--embed", str(control)], env=environment,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = time.monotonic() + 15
                while not (control / "ready.json").exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue((control / "ready.json").exists())
                parent.terminate()
                parent.wait(timeout=5)
                output, _ = child.communicate(timeout=10)
                self.assertEqual(child.returncode, 0, output.decode("utf-8", "replace"))
                self.assertTrue((root / "data" / "omini_api_toolbox_settings.json").is_file())
            finally:
                for process in (child, parent):
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)


@unittest.skipUnless(sys.platform == "win32", "Native embedding runs on Windows")
class ApiPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="omini-api-integration-")
        self.workspace = Path(self.temporary.name)
        self.saved = []
        self.page = OminiApiPage({"omini_api_workspace": str(self.workspace)},
                                lambda settings: self.saved.append(dict(settings)))
        self.page.resize(1100, 800)
        self.page.show()

    def tearDown(self):
        self.page.stop()
        self.page.close()
        self.page.deleteLater()
        self.qt.processEvents()
        self.temporary.cleanup()

    def until(self, predicate, seconds=18):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(50)
        self.assertTrue(predicate(), self.page.diagnostic + self.page.status.text())

    def test_native_embed_theme_restart_and_settings(self):
        self.assertFalse(self.page.is_running)
        self.page.ensure_started()
        self.until(lambda: len(self.page.runtime_state.get("tabs", [])) == 7)
        self.assertIsNotNone(self.page.container)
        self.assertEqual(self.page.runtime_state["tabs"],
                         ["Codex注册", "网站搜索", "号池账号", "号池网站", "Claude 独立", "设置与分组", "日志"])
        first_pid = self.page.process.processId()
        self.page.ensure_started()
        self.assertEqual(self.page.process.processId(), first_pid)
        self.page.apply_theme(False)
        self.until(lambda: self.page.runtime_state.get("dark") is False)
        self.assertFalse(self.page.diagnostic, self.page.diagnostic)
        self.page.stop()
        self.assertFalse(self.page.is_running)
        settings_file = self.workspace / "omini_api_toolbox_settings.json"
        self.assertIsInstance(json.loads(settings_file.read_text(encoding="utf-8"))["values"], dict)
        self.assertTrue((self.workspace / "omini_email_receiver.json").is_file())
        self.page.restart()
        self.until(lambda: len(self.page.runtime_state.get("tabs", [])) == 7)
        self.assertNotEqual(self.page.process.processId(), first_pid)
        self.assertFalse(self.page.diagnostic, self.page.diagnostic)

    def test_start_failure_keeps_the_page_usable(self):
        occupied = self.workspace / "file.txt"
        occupied.write_text("fixture", encoding="utf-8")
        self.page.workspace = occupied
        self.page.ensure_started()
        self.assertFalse(self.page.is_running)
        self.assertIn("启动失败", self.page.status.text())
        self.page.workspace = self.workspace
        self.page.ensure_started()
        self.until(lambda: len(self.page.runtime_state.get("tabs", [])) == 7)
