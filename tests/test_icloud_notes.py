"""Notes parsing, encrypted persistence, offline editing and isolated web sessions."""
import base64
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zlib

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import app
from icloud_notes import NotesStore, decode_note_body, import_notes_har


def varint(value):
    data = bytearray()
    while value > 127:
        data.append((value & 127) | 128); value >>= 7
    return bytes(data + bytes([value]))


def message(number, value):
    return varint(number << 3 | 2) + varint(len(value)) + value


def body(text, compressor=zlib.compress):
    data = message(2, message(3, message(2, text.encode())))
    return base64.b64encode(compressor(data)).decode()


def field(value, kind="STRING"):
    return {"value": value, "type": kind}


def encoded(text):
    return field(base64.b64encode(text.encode()).decode(), "ENCRYPTED_BYTES")


def response(records, encoding=None, host="p123-ckdatabasews.icloud.com.cn"):
    text = json.dumps({"records": records})
    content = {"text": base64.b64encode(text.encode()).decode() if encoding else text}
    if encoding: content["encoding"] = encoding
    return {"request": {"url": f"https://{host}/database/1/com.apple.notes/production/private/records/query",
                        "headers": [{"name": "Cookie", "value": "PRIVATE_COOKIE"}]},
            "response": {"status": 200, "content": content}}


def fixture():
    return [{"recordName": "folder", "recordType": "Folder", "fields": {"TitleEncrypted": encoded("工作")}},
            {"recordName": "note", "recordType": "Note", "fields": {
                "TitleEncrypted": encoded("测试标题"), "SnippetEncrypted": encoded("摘要"),
                "TextDataEncrypted": field(body("完整正文\n第二行 ☀"), "ENCRYPTED_BYTES"),
                "Folder": field({"recordName": "folder"}, "REFERENCE")}},
            {"recordName": "locked", "recordType": "PasswordProtectedNote", "fields": {
                "TitleEncrypted": encoded("锁定标题"), "TextDataEncrypted": field(body("PRIVATE_LOCKED_BODY"), "ENCRYPTED_BYTES")}},
            {"recordName": "pin", "recordType": "Note_UserSpecific", "fields": {
                "Note": field({"recordName": "note"}, "REFERENCE"), "IsPinned": field(1, "INT64")}}]


class NotesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)

    def tearDown(self):
        self.qt.processEvents(); self.temp.cleanup()

    def har(self, entries):
        path = self.root / "notes.har"
        path.write_text(json.dumps({"log": {"entries": entries}}), encoding="utf-8")
        return path

    def snapshot(self):
        return import_notes_har(self.har([response(fixture())]))

    def test_full_body_zlib_gzip_and_invalid(self):
        for compressor in (zlib.compress, gzip.compress):
            self.assertEqual(decode_note_body(body("多行\n正文", compressor)), "多行\n正文")
        for invalid in ("?", "", base64.b64encode(b"xBAD").decode()):
            with self.assertRaises((ValueError, zlib.error)):
                decode_note_body(invalid)

    def test_partial_changes_base64_locked_and_host_boundary(self):
        entries = [response(fixture(), "base64"), response([
            {"recordName": "note", "recordType": "Note", "fields": {"TitleEncrypted": encoded("新标题")}}]),
            response([{"recordName": "bad", "recordType": "Note"}], host="p123-ckdatabasews.icloud.com.cn.attacker.test")]
        snapshot = import_notes_har(self.har(entries))
        self.assertEqual(len(snapshot["notes"]), 2)
        note = next(n for n in snapshot["notes"] if n["id"] == "icloud:note")
        self.assertEqual(note["title"], "新标题"); self.assertEqual(note["body"], "完整正文\n第二行 ☀")
        self.assertTrue(note["pinned"]); self.assertEqual(note["folder"], "工作")
        locked = next(n for n in snapshot["notes"] if n["locked"])
        self.assertEqual(locked["body"], "")

    def test_tombstone_and_missing_body(self):
        records = fixture()
        records[1]["fields"].pop("TextDataEncrypted")
        path = self.har([response(records), response([{"recordName": "locked", "deleted": True}])])
        notes = import_notes_har(path)["notes"]
        self.assertEqual(len(notes), 1); self.assertEqual(notes[0]["body"], "摘要")
        self.assertFalse(notes[0]["complete"])

    def test_encrypted_restart_and_merge_keeps_local_edits(self):
        path = self.root / "cache.json"; store = NotesStore(path)
        snapshot = self.snapshot(); store.merge(snapshot)
        persisted = path.read_text()
        for private in ("测试标题", "完整正文", "PRIVATE_COOKIE", "PRIVATE_LOCKED_BODY"):
            self.assertNotIn(private, persisted)
        store = NotesStore(path)
        self.assertEqual(store.data["notes"][0]["body"], "完整正文\n第二行 ☀")
        store.data["notes"][0].update(body="本地修改", edited=True); store.save()
        self.assertEqual(store.merge(snapshot), (1, 1))
        self.assertEqual(NotesStore(path).data["notes"][0]["body"], "本地修改")

    def test_corrupt_cache_is_preserved(self):
        path = self.root / "cache.json"; path.write_text("bad data")
        store = NotesStore(path); self.assertTrue(store.error)
        with self.assertRaises(ValueError): store.merge(self.snapshot())
        self.assertEqual(path.read_text(), "bad data")

    def test_later_summary_import_preserves_cached_full_body(self):
        store = NotesStore(self.root / "cache.json"); store.merge(self.snapshot())
        records = fixture(); records[1]["fields"].pop("TextDataEncrypted")
        store.merge(import_notes_har(self.har([response(records)])))
        note = next(n for n in NotesStore(store.path).data["notes"] if n["id"] == "icloud:note")
        self.assertTrue(note["complete"]); self.assertEqual(note["body"], "完整正文\n第二行 ☀")

    def test_main_window_theme_button_persistence_and_settings_sync(self):
        patches = [patch.object(app, name, self.root / target) for name, target in {
            "APP_DIR": ".", "SETTINGS_FILE": "settings.json", "TASKS_FILE": "tasks.json",
            "LOG_DIR": "logs", "TEMP_COOKIE_DIR": "runtime"}.items()]
        for item in patches: item.start()
        try:
            with patch("campus_page.wifi_ssids", return_value=()):
                window = app.MainWindow(); window.show(); QTest.qWait(30)
                window.nav.setCurrentRow(3); self.assertIs(window.stack.currentWidget(), window.notes_page)
                QTest.mouseClick(window.theme_button, Qt.MouseButton.LeftButton)
                self.assertEqual(window.settings["theme"], "浅色")
                self.assertEqual(window.settings_page.theme.currentText(), "浅色")
                self.assertFalse(window.notes_page.dark)
                self.assertEqual(json.loads(app.SETTINGS_FILE.read_text(encoding="utf-8"))["theme"], "浅色")
                window.quitting = True; window.close(); QTest.qWait(50); window.deleteLater()
                window = app.MainWindow()
                self.assertEqual(window.settings["theme"], "浅色")
                self.assertIn("黑夜", window.theme_button.text())
                window.quitting = True; window.close(); QTest.qWait(80); window.deleteLater()
        finally:
            for item in reversed(patches): item.stop()


if __name__ == "__main__": unittest.main()
