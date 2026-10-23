"""Migration, encrypted recovery, corruption and plaintext-leak regression tests."""
import base64
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from memo_crypto import PREFIX, MemoCipher
from memo_store import MemoStore
from text_notes import TextNotesStore, read_text_file
from tests.test_memos import image_data


class MemoEncryptionTests(unittest.TestCase):
    def test_title_content_images_history_metadata_and_filenames_are_encrypted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); store = TextNotesStore(root)
            file = store.create("PRIVATE_TITLE_标题", "PRIVATE_CONTENT_正文")
            memo = store.for_path(file)
            asset = store.memos.put_asset("local", "PRIVATE_IMAGE_NAME.png", image_data())
            store.save(file, "PRIVATE_NEW_CONTENT", read_text_file(file)[1])
            self.assertEqual(store.memos.history("local", memo["id"])[1]["content"], "PRIVATE_CONTENT_正文")
            self.assertEqual(store.memos.asset("local", asset["id"])["data"], base64.b64decode(image_data()))
            store.trash(file)
            restarted = TextNotesStore(root)
            self.assertTrue(restarted.memos.get("local", memo["id"])["deleted"])
            for path in root.rglob("*"):
                self.assertNotIn("PRIVATE_", path.name)
                if path.is_file():
                    raw = path.read_bytes()
                    self.assertNotIn(b"PRIVATE_", raw)
                    self.assertNotIn(base64.b64decode(image_data()), raw)

    def test_legacy_database_and_txt_migrate_with_recoverable_encrypted_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); database = root / ".omini-memos.sqlite3"
            # Create the exact pre-encryption database schema, including immutable history.
            original = MemoStore(database)
            memo = original.save("local", {"title": "LEGACY_TITLE", "content": "LEGACY_BODY"})
            asset = original.put_asset("local", "LEGACY_IMAGE", image_data())
            with original.database.connect() as db:
                db.execute("DELETE FROM memo_storage_meta")
                db.execute("UPDATE memos SET title=?, content=?", ("LEGACY_TITLE", "LEGACY_BODY"))
                db.execute("UPDATE memo_versions SET title=?, content=?", ("LEGACY_TITLE", "LEGACY_BODY"))
                db.execute("UPDATE memo_assets SET name=?, data=?", ("LEGACY_IMAGE", base64.b64decode(image_data())))
                db.execute("INSERT INTO workspace_data VALUES (?,?,?)", ("local", "paths", json.dumps({memo["id"]: "LEGACY_TITLE.txt"})))
            (root / "LEGACY_TITLE.txt").write_text("LEGACY_BODY", encoding="utf-8")
            (root / ".trash").mkdir(); (root / ".trash/OLD_TITLE.txt").write_text("OLD_BODY", encoding="utf-8")
            store = TextNotesStore(root)
            self.assertEqual(read_text_file(store.files()[0])[0], "LEGACY_BODY")
            self.assertEqual(store.memos.history("local", memo["id"])[0]["content"], "LEGACY_BODY")
            self.assertEqual(store.memos.asset("local", asset["id"])["name"], "LEGACY_IMAGE")
            self.assertFalse(list(root.rglob("*.txt")))
            backups = list(root.glob(".memo-migration-*.enc"))
            recovered = [json.loads(store.memos.cipher.decrypt(line, "migration-backup"))
                         for line in backups[0].read_bytes().splitlines()]
            self.assertTrue(any(row["row"].get("content") == "LEGACY_BODY" for row in recovered))
            file_backups = list((root / ".migration-backup").glob("*.enc"))
            self.assertEqual(len(file_backups), 2)
            for path in root.rglob("*"):
                if path.is_file(): self.assertNotIn(b"LEGACY_", path.read_bytes())
            self.assertEqual(TextNotesStore(root).memos.get("local", memo["id"])["version"], 1)

    def test_missing_wrong_key_and_modified_ciphertext_preserve_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); store = TextNotesStore(root)
            path = store.create("important", "KEEP_THIS_DATA"); memo = store.for_path(path)
            key_path = store.memos.cipher.path; key = key_path.read_bytes(); database = store.memos.database.path
            before = database.read_bytes(); key_path.unlink()
            with self.assertRaisesRegex(ValueError, "密钥缺失"): TextNotesStore(root)
            self.assertFalse(key_path.exists()); self.assertEqual(database.read_bytes(), before)
            other = MemoCipher(root / "wrong.key"); key_path.write_bytes(other.path.read_bytes())
            with self.assertRaisesRegex(ValueError, "校验失败"): TextNotesStore(root)
            self.assertEqual(database.read_bytes(), before)
            key_path.write_bytes(key)
            with store.memos.database.connect() as db:
                db.execute("UPDATE memos SET content=? WHERE id=?", (PREFIX.decode()+"broken", memo["id"]))
            with self.assertRaisesRegex(ValueError, "校验失败"): store.memos.get("local", memo["id"])
            self.assertEqual(read_text_file(path)[0], "KEEP_THIS_DATA")

    def test_failed_file_migration_keeps_original_and_retry_imports_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); legacy = root / "legacy.txt"; legacy.write_text("original", encoding="utf-8")
            with patch("text_notes.TextNotesStore._write", side_effect=OSError("fixture")):
                with self.assertRaises(OSError): TextNotesStore(root)
            self.assertEqual(legacy.read_text(), "original")
            store = TextNotesStore(root)
            self.assertEqual(len(store.memos.listing("local")), 1)
            self.assertEqual(read_text_file(store.files()[0])[0], "original")


if __name__ == "__main__": unittest.main()
