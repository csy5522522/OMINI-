"""Native document editing and compatibility of image objects with TXT snapshots."""
import base64
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import threading
import time

from PIL import Image
from PySide6.QtCore import QMimeData, Qt
from PySide6.QtGui import QInputMethodEvent, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from memo_editor import MemoEditor, IMAGE_OBJECT
from text_notes_page import TextNotesPage
from text_notes import read_text_file


class MemoEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.page = TextNotesPage(Path(self.temp.name) / 'notes.json')
        self.page.resize(1120, 800)
        self.page.show()
        stream = io.BytesIO(); Image.new('RGB', (1600, 500), '#45917a').save(stream, 'PNG')
        self.raw = stream.getvalue()
        self.asset = self.page.store.memos.put_asset('local', '图片 😀.png', base64.b64encode(self.raw).decode())
        self.token = f"![图片 😀.png](omini-asset://{self.asset['id']})"

    def tearDown(self):
        self.page.dirty = False; self.page.stop(); self.page.close(); self.page.deleteLater()
        self.qt.processEvents(); self.temp.cleanup()

    def load(self, content):
        path = self.page.store.create('我的世界', content)
        self.page.refresh_files(path.name)
        return path

    def test_lossless_text_image_tokens_whitespace_and_copy(self):
        content = '\n前文 😀\r\n\r\n' + self.token + '\n后文\u00a0文本\n\n'
        self.load(content)
        editor = self.page.editor
        self.assertEqual(editor.get_content(), content)
        self.assertIn('\ufffc', editor.toPlainText())
        self.assertNotIn('omini-asset://', editor.toPlainText())
        editor.selectAll()
        self.assertEqual(editor.createMimeDataFromSelection().text(), content)
        self.assertFalse(hasattr(self.page, 'preview'))
        self.assertIsNotNone(editor.document().documentLayout().handlerForObject(IMAGE_OBJECT))
        self.qt.processEvents()
        self.assertFalse(editor.image_for('memo://' + self.asset['id']).isNull())

    def test_inline_insert_delete_undo_redo_export_and_resize(self):
        path = self.load('前文\n\n后文\n')
        editor = self.page.editor
        cursor = editor.textCursor(); cursor.setPosition(4); editor.setTextCursor(cursor)
        self.page.insert_raw_image(self.raw, self.asset['name'])
        expected = '前文\n\n' + self.token + '后文\n'
        self.assertEqual(editor.get_content(), expected)
        self.assertEqual(read_text_file(path)[0], expected)
        position = editor.textCursor().position()
        QTest.keyClicks(editor, ' after')
        self.assertEqual(editor.get_content(), '前文\n\n' + self.token + ' after后文\n')
        editor.undo(); self.assertEqual(editor.get_content(), expected)
        editor.undo(); self.assertEqual(editor.get_content(), '前文\n\n后文\n')
        editor.redo(); self.assertEqual(editor.get_content(), expected)
        self.assertTrue(self.page.flush())
        editor.document().setModified(False)
        saved_position = editor.textCursor().position()
        self.page.resize(600, 760); self.qt.processEvents()
        self.assertEqual(editor.get_content(), expected)
        self.assertEqual(editor.textCursor().position(), saved_position)
        self.assertFalse(self.page.dirty)
        destination = Path(self.temp.name) / 'export.txt'
        with patch('text_notes_page.QFileDialog.getSaveFileName', return_value=(str(destination), '')):
            self.page.save_as()
        self.assertEqual(destination.read_text(encoding='utf-8'), expected)
        cursor = editor.textCursor(); cursor.setPosition(position - 1); cursor.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor)
        editor.setTextCursor(cursor); QTest.keyClick(editor, Qt.Key.Key_Backspace)
        self.assertNotIn('omini-asset://', editor.get_content())
        editor.undo(); self.assertEqual(editor.get_content(), expected)

    def test_composition_defers_save_and_plain_paste_escapes_html(self):
        path = self.load('初稿')
        editor = self.page.editor
        preedit = QInputMethodEvent('中文输入', [])
        QApplication.sendEvent(editor, preedit)
        editor.insertPlainText('new')
        self.assertFalse(self.page.flush()); self.assertEqual(read_text_file(path)[0], '初稿')
        commit = QInputMethodEvent(); commit.setCommitString('中文')
        QApplication.sendEvent(editor, commit)
        source = QMimeData(); source.setText('<img src=x onerror=alert(1)>\n' + self.token)
        editor.insertFromMimeData(source)
        self.assertIn('<img src=x onerror=alert(1)>', editor.get_content())
        self.assertTrue(self.page.flush()); self.assertIn(self.token, read_text_file(path)[0])

    def test_trash_is_read_only_and_restore_keeps_history(self):
        path = self.load('正文\n' + self.token)
        with patch('text_notes_page.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes): self.page.delete_note()
        self.assertFalse(path.exists())
        self.page.show_trash()
        self.assertEqual(self.page.notes.count(), 1)
        self.assertTrue(self.page.editor.isReadOnly()); self.assertTrue(self.page.restore_button.isVisible())
        before = len(self.page.store.memos.history('local', self.page.current_memo['id']))
        self.page.restore_note()
        self.assertFalse(self.page.trash_mode); self.assertFalse(self.page.editor.isReadOnly())
        self.assertEqual(read_text_file(self.page.current_path)[0], '正文\n' + self.token)
        self.assertEqual(len(self.page.store.memos.history('local', self.page.current_memo['id'])), before + 1)

    def test_title_autosave_and_collision_preserve_edit(self):
        path = self.load('原文')
        self.page.filename.setText('新标题'); self.page.title_edited()
        self.assertTrue(self.page.flush()); self.assertFalse(path.exists())
        self.assertEqual(self.page.current_memo["title"], '新标题')
        self.page.store.create('已有标题', '别的笔记')
        self.page.filename.setText('已有标题'); self.page.title_edited()
        self.assertFalse(self.page.flush()); self.assertTrue(self.page.dirty)
        self.assertEqual(read_text_file(self.page.current_path)[0], '原文')

    def test_keyboard_save_preserves_caret_without_waiting_for_autosave(self):
        path = self.load('原文')
        self.page.activateWindow(); self.page.editor.setFocus(); QTest.qWait(100)
        self.page.editor.insertPlainText('立即保存')
        cursor = self.page.editor.textCursor().position()
        self.assertTrue(self.page.dirty)
        QTest.keyClick(self.page.editor, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
        self.assertFalse(self.page.dirty)
        self.assertEqual(read_text_file(path)[0], self.page.editor.get_content())
        self.assertEqual(self.page.editor.textCursor().position(), cursor)

    def test_slow_sync_keeps_typing_composition_undo_and_encrypted_local_saves(self):
        path = self.load('初稿')
        page = self.page
        page.account = SimpleNamespace(url='https://fixture.test', token='fixture', user={'id': 'fixture'})
        page.context = (page.account.url, 'fixture')
        started, release = threading.Event(), threading.Event()
        def slow_sync(directory, url, token, *, protected):
            self.assertTrue(protected(page.current_memo['id']))
            self.assertEqual(read_text_file(path)[0], '本机先保存')
            started.set()
            if not release.wait(8): raise RuntimeError('Fixture timed out')
            return {'count': 1, 'conflicts': 0}
        page.editor.selectAll(); page.editor.insertPlainText('本机先保存')
        with patch('memo_sync.sync_memos', side_effect=slow_sync), patch.object(page.editor, 'load_content', wraps=page.editor.load_content) as load:
            try:
                page.sync_now()
                self.assertTrue(started.wait(3))
                self.assertTrue(page.isEnabled()); self.assertTrue(page.editor.isEnabled())
                page.editor.insertPlainText('继续输入')
                self.assertTrue(page.flush())
                self.assertEqual(read_text_file(path)[0], '本机先保存继续输入')
                for file in page.store.directory.rglob('*'):
                    if file.is_file(): self.assertNotIn('继续输入'.encode(), file.read_bytes())
                QApplication.sendEvent(page.editor, QInputMethodEvent('中文候选', []))
                position = page.editor.textCursor().position()
                release.set()
                deadline = time.monotonic() + 5
                while page.worker and time.monotonic() < deadline: QTest.qWait(10)
                self.assertIsNone(page.worker)
                self.assertTrue(page.editor.composing)
                self.assertEqual(page.editor.textCursor().position(), position)
                self.assertTrue(page.editor.document().isUndoAvailable())
                load.assert_not_called()
            finally:
                release.set()
                if page.worker: page.worker.wait(5000)
                self.qt.processEvents()
                QApplication.sendEvent(page.editor, QInputMethodEvent())


if __name__ == '__main__': unittest.main()
