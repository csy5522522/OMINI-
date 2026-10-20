"""TXT persistence, external edits, encoding imports and folder switching."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from text_notes import TextNotesStore, read_text_file
from text_notes_page import TextNotesPage


class TextNotesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.page = TextNotesPage(self.root / 'text-notes.json')

    def tearDown(self):
        self.page.dirty = False; self.page.stop(); self.page.close(); self.page.deleteLater()
        self.qt.processEvents(); self.temp.cleanup()

    def test_auto_save_plain_txt_search_trash_and_restart(self):
        path = self.page.store.create('我的笔记', '初始文本')
        self.page.refresh_files(path.name)
        self.page.editor.setPlainText('离线内容 ☀\n第二行')
        QTest.qWait(950)
        self.assertFalse(self.page.dirty)
        self.assertEqual(path.read_text(encoding='utf-8'), '离线内容 ☀\n第二行')
        self.page.search.setText('第二行'); self.assertFalse(self.page.notes.item(0).isHidden())
        self.page.search.setText('没有此词'); self.assertTrue(self.page.notes.item(0).isHidden())
        self.page.search.clear()
        restored = TextNotesPage(self.page.config_path)
        self.assertEqual(restored.editor.toPlainText(), '离线内容 ☀\n第二行')
        restored.stop(); restored.deleteLater()
        self.page.delete_note()
        self.assertFalse(path.exists()); self.assertEqual(self.page.notes.count(), 0)
        self.assertEqual(next((path.parent/'.trash').glob('*.txt')).read_text(encoding='utf-8'), '离线内容 ☀\n第二行')

    def test_import_gb18030_and_utf16_copies_without_overwriting(self):
        for encoding in ('gb18030', 'utf-16'):
            source = self.root / '来源.txt'
            raw = '中文记录\n第二行'.encode(encoding); source.write_bytes(raw)
            self.assertTrue(self.page.import_file(source))
            self.assertEqual(self.page.editor.toPlainText(), '中文记录\n第二行')
            self.assertEqual(self.page.current_path.read_bytes(), '中文记录\n第二行'.encode('utf-8'))
            self.assertEqual(source.read_bytes(), raw)
        self.assertEqual(len(self.page.store.files()), 2)

    def test_external_changes_block_auto_save_and_switch(self):
        first = self.page.store.create('one', 'original')
        second = self.page.store.create('two', 'other')
        self.page.refresh_files(first.name)
        self.page.editor.setPlainText('my editing')
        first.write_text('external editor', encoding='utf-8')
        self.assertFalse(self.page.flush())
        self.page.notes.setCurrentRow(1)
        self.assertEqual(self.page.current_path, first)
        self.assertEqual(self.page.editor.toPlainText(), 'my editing')
        self.assertEqual(first.read_text(), 'external editor')
        with patch('text_notes_page.QFileDialog.getSaveFileName', return_value=(str(self.root/'copy.txt'), '')):
            self.page.save_as()
        self.assertEqual((self.root/'copy.txt').read_text(), 'my editing')
        self.assertEqual(second.read_text(), 'other')

    def test_failed_write_keeps_unsaved_editor(self):
        path = self.page.store.create('test', 'old')
        self.page.refresh_files(path.name); self.page.editor.setPlainText('new')
        with patch('text_notes.os.replace', side_effect=PermissionError('readonly')):
            self.assertFalse(self.page.flush())
        self.assertTrue(self.page.dirty); self.assertEqual(path.read_text(), 'old')
        self.assertTrue(self.page.flush()); self.assertEqual(path.read_text(), 'new')

    def test_select_folder_persists_and_preserves_legacy_cache(self):
        old = self.root/'icloud-notes.json'; old.write_text('legacy encrypted sentinel')
        directory = self.root/'other folder'; directory.mkdir()
        (directory/'外部.txt').write_text('other directory', encoding='utf-8')
        with patch('text_notes_page.QFileDialog.getExistingDirectory', return_value=str(directory)):
            self.page.choose_directory()
        self.assertEqual(self.page.editor.toPlainText(), 'other directory')
        self.assertEqual(json.loads(self.page.config_path.read_text(encoding='utf-8'))['directory'], str(directory.resolve()))
        self.assertEqual(old.read_text(), 'legacy encrypted sentinel')

    def test_rename_collision_and_non_text_import_preserve_files(self):
        one = self.page.store.create('one', 'first'); two = self.page.store.create('two', 'second')
        self.page.refresh_files(one.name)
        with patch('text_notes_page.QInputDialog.getText', return_value=('two', True)):
            self.page.rename_note()
        self.assertEqual(one.read_text(), 'first'); self.assertEqual(two.read_text(), 'second')
        binary = self.root/'binary.txt'; binary.write_bytes(b'\x00\x01\x02')
        self.assertFalse(self.page.import_file(binary)); self.assertEqual(len(self.page.store.files()), 2)


if __name__ == '__main__': unittest.main()
