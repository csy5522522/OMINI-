"""Regression coverage for the reported restart data loss and action history."""
from __future__ import annotations

import copy
import json
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ai_assistants import AssistantStore, atomic_json, engine_path
from ai_assistants_page import AiAssistantsPage
from assistant_data import (AssistantState, OperationHistory, backup_credentials,
                            decode_protected, encode_protected, local_members)
from codex_quota import parse_codex_quota


def credentials(root, count=3):
    for index in range(count):
        atomic_json(root / "auths" / f"codex-{index}.json", {
            "type": "codex", "email": f"member{index}@example.com", "account_id": f"fixture-{index}",
            "access_token": f"private-access-{index}", "refresh_token": f"private-refresh-{index}",
            "expired": "2099-01-01T00:00:00Z", "last_refresh": "2099-01-01T00:00:00Z",
        })


def quota():
    return parse_codex_quota({"plan_type": "plus", "rate_limit": {
        "primary_window": {"used_percent": 23, "limit_window_seconds": 18000},
        "secondary_window": {"used_percent": 42, "limit_window_seconds": 604800},
    }})


class DurableStorageTests(unittest.TestCase):
    def test_settings_recover_from_backup_and_preserve_damaged_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AssistantStore(root)
            saved = copy.deepcopy(store.data)
            saved["pool"]["model"] = "saved-model"
            store.save(saved)
            store.path.write_bytes(b'{"interrupted":')
            recovered = AssistantStore(root)
            self.assertEqual(recovered.data, saved)
            self.assertIn("自动备份", recovered.error)
            recovered.save(saved)
            self.assertEqual(next((root / "recovery").iterdir()).read_bytes(), b'{"interrupted":')
            self.assertEqual(AssistantStore(root).data, saved)

    def test_unreadable_config_is_not_overwritten_with_generated_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "settings.json").write_bytes(b"damaged-personal-config")
            store = AssistantStore(root)
            with self.assertRaises(ValueError):
                store.save(store.data)
            self.assertEqual(store.path.read_bytes(), b"damaged-personal-config")

    def test_schema_recovery_retains_the_valid_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AssistantStore(root)
            store.save(store.data)
            valid = store.path.with_name("settings.json.bak").read_bytes()
            damaged = encode_protected({"pool": "damaged-schema", "profiles": {}})
            store.path.write_bytes(damaged)
            recovered = AssistantStore(root)
            self.assertIn("自动备份", recovered.error)
            recovered.save(recovered.data)
            self.assertEqual(recovered.path.with_name("settings.json.bak").read_bytes(), valid)
            self.assertEqual(next((root / "recovery").iterdir()).read_bytes(), damaged)
            self.assertEqual(AssistantStore(root).data, recovered.data)

    def test_history_is_encrypted_and_partial_final_line_does_not_erase_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            history = OperationHistory(Path(directory))
            history.append("fixture-action-before-restart", "service.start")
            path = next(history.root.iterdir())
            self.assertNotIn(b"fixture-action-before-restart", path.read_bytes())
            with path.open("ab") as stream:
                stream.write(b'{"partial":')
            reloaded = OperationHistory(Path(directory))
            reloaded.append("second-action", "service.stop")
            self.assertEqual([row["event"] for row in reloaded.records()], ["service.start", "service.stop"])
            self.assertTrue(all(row["time"] for row in reloaded.records()))

    def test_credential_backup_is_encrypted_deduplicated_and_leaves_originals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credentials(root)
            originals = {path.name: path.read_bytes() for path in (root / "auths").iterdir()}
            target = backup_credentials(root)
            self.assertEqual(target, backup_credentials(root))
            self.assertEqual(len(list(target.parent.iterdir())), 1)
            self.assertNotIn(b"private-access", target.read_bytes())
            self.assertEqual(set(decode_protected(target.read_bytes())["files"]), set(originals))
            self.assertEqual({path.name: path.read_bytes() for path in (root / "auths").iterdir()}, originals)
            rows, errors = local_members(root)
            self.assertEqual((len(rows), errors), (3, 0))
            self.assertNotIn("private-access", json.dumps(rows))
            self.assertTrue(all(row["offline"] for row in rows))

    def test_state_recovers_quota_snapshot_from_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            state = AssistantState(Path(directory))
            saved = copy.deepcopy(state.data)
            saved["quotas"] = {"member.json": quota()}
            state.save(saved)
            state.path.write_bytes(b"interrupted-write")
            restored = AssistantState(Path(directory))
            self.assertEqual(restored.data["quotas"], saved["quotas"])
            restored.save(restored.data)
            self.assertEqual(decode_protected(restored.path.read_bytes())["quotas"], saved["quotas"])


class RestartPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def close_page(self, page):
        page.stop()
        if page.worker:
            page.worker.wait(15000)
        self.qt.processEvents()
        page.deleteLater()
        self.qt.processEvents()

    def test_legacy_members_and_autosaved_actions_restore_without_starting_service(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credentials(root)
            store = AssistantStore(root)
            original_keys = copy.deepcopy(store.data["pool"])
            store.save(store.data)
            page = AiAssistantsPage(root)
            try:
                self.assertEqual(page.table.rowCount(), 3)
                self.assertFalse(page.is_running)
                page.tabs.setCurrentIndex(4)
                page.table.selectRow(2)
                page.model.addItems(["fixture-model", "second-model"])
                page.model.setCurrentText("second-model")
                page.profiles["claude"].proxy.setText("http://127.0.0.1:7899")
                page.profiles["gemini"].proxy.setText("unfinished-proxy-draft")
                page.strategy.setCurrentIndex(page.strategy.findData("weighted-round-robin"))
                page.quotas[page.accounts[2]["name"]] = quota()
                page.set_accounts(page.accounts)
                page.report("已完成 fixture-action；Bearer private-token", "fixture.action")
                QTest.qWait(900)  # No Save button: edits must persist during normal operation.
                self.assertEqual(AssistantStore(root).data["profiles"]["claude"]["proxy"], "http://127.0.0.1:7899")
            finally:
                self.close_page(page)
            restored = AiAssistantsPage(root)
            try:
                self.assertEqual(restored.table.rowCount(), 3)
                self.assertEqual(restored.selected_account()["name"], "codex-2.json")
                self.assertEqual(restored.tabs.currentIndex(), 4)
                self.assertEqual(restored.model.currentText(), "second-model")
                self.assertEqual(restored.model.count(), 2)
                self.assertEqual(restored.profiles["gemini"].proxy.text(), "unfinished-proxy-draft")
                self.assertEqual(restored.strategy.currentData(), "weighted-round-robin")
                self.assertIn("上次", restored.table.item(2, 2).text())
                self.assertIn("77%", restored.table.item(2, 2).text())
                self.assertIn("fixture-action", restored.history_view.toPlainText())
                self.assertNotIn("private-token", restored.history_view.toPlainText())
                self.assertIn("本地已保存", restored.accounts[0]["status"])
                for key in ("api_key", "management_key"):
                    self.assertEqual(restored.store.data["pool"][key], original_keys[key])
                self.assertFalse(restored.is_running)
            finally:
                self.close_page(restored)

    def test_older_state_draft_never_overwrites_newer_saved_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = AiAssistantsPage(root)
            page.strategy.setCurrentIndex(page.strategy.findData("round-robin"))
            self.close_page(page)
            store = AssistantStore(root)
            newer = copy.deepcopy(store.data)
            newer["pool"].update(strategy="fill-first", model="newer-saved-model")
            store.save(newer)
            restored = AiAssistantsPage(root)
            try:
                self.assertEqual(restored.strategy.currentData(), "fill-first")
                self.assertEqual(restored.model.currentText(), "newer-saved-model")
                restored.save_session()
                self.assertEqual(AssistantStore(root).data["pool"]["strategy"], "fill-first")
            finally:
                self.close_page(restored)

    def test_empty_refresh_never_erases_persisted_quotas_or_auth_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credentials(root, 1)
            page = AiAssistantsPage(root)
            try:
                page.quotas["codex-0.json"] = quota()
                page.set_accounts([])
                self.assertEqual(len(AssistantState(root).data["quotas"]), 1)
                page.refresh_accounts()
                self.assertEqual(page.table.rowCount(), 1)
                self.assertIn("77%", page.table.item(0, 2).text())
            finally:
                self.close_page(page)

    def test_state_write_failure_is_reported_in_operation_history(self):
        with tempfile.TemporaryDirectory() as directory:
            page = AiAssistantsPage(Path(directory))
            try:
                with patch.object(page.state_store, "save", side_effect=OSError("fixture disk full")):
                    self.assertFalse(page.save_session())
                self.assertIn("操作状态保存失败", page.history_view.toPlainText())
                self.assertIn("fixture disk full", page.status.text())
            finally:
                self.close_page(page)

    def test_native_gateway_resumes_after_app_exit_but_respects_manual_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if not engine_path(root):
                self.skipTest("Native gateway fixture is not installed")
            credentials(root, 2)
            page = AiAssistantsPage(root)
            try:
                page.pool_proxy.clear()
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    page.port.setValue(listener.getsockname()[1])
                page.start_pool()
                self.wait_ready(page)
                page.quotas["codex-0.json"] = quota()
            finally:
                self.close_page(page)
            self.assertTrue(AssistantState(root).data["service_wanted"])
            restored = AiAssistantsPage(root)
            try:
                self.assertEqual(restored.table.rowCount(), 2)
                self.wait_ready(restored)
                self.assertIn("77%", restored.table.item(0, 2).text())
                restored.stop_pool()
                self.assertFalse(restored.service_wanted)
                self.assertEqual(restored.table.rowCount(), 2)
            finally:
                self.close_page(restored)
            stopped = AiAssistantsPage(root)
            try:
                QTest.qWait(800)
                self.assertFalse(stopped.is_running)
                self.assertFalse(stopped.service_wanted)
                self.assertEqual(stopped.table.rowCount(), 2)
                self.assertIn("手动停止", stopped.history_view.toPlainText())
            finally:
                self.close_page(stopped)

    def wait_ready(self, page):
        deadline = time.monotonic() + 25
        while (not page.ready or page.worker) and time.monotonic() < deadline:
            QTest.qWait(30)
        self.assertTrue(page.ready, page.status.text() + page.log.toPlainText())
        self.assertIsNone(page.worker)
