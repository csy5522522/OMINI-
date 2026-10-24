"""Local-to-account migration preserves all actions, originals and retry identity."""
import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memo_import import import_workspace, adopt_local_workspace
from text_notes import TextNotesStore, read_text_file
from tests.test_memos import image_data


class MemoImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.source = TextNotesStore(self.root / 'text-notes')
        self.target = TextNotesStore(self.root / 'account')

    def tearDown(self): self.temp.cleanup()

    def test_all_versions_images_same_title_cloud_note_and_repeated_login(self):
        path = self.source.create('同名标题', '本机第一稿')
        asset = self.source.memos.put_asset('local', 'image.png', image_data())
        self.source.save(path, '第二稿 ![image](omini-asset://' + asset['id'] + ')', read_text_file(path)[1])
        self.source.record(path, 'edit')  # Identical content is still an original history action.
        cloud = self.target.create('同名标题', '云端原文')
        self.assertEqual(import_workspace(self.source, self.target), 1)
        self.assertEqual(import_workspace(self.source, self.target), 0)
        self.assertEqual(len(self.target.files()), 2)
        self.assertEqual(read_text_file(cloud)[0], '云端原文')
        imported = next(m for m in self.target.memos.listing('local') if m['content'] != '云端原文')
        self.assertEqual(len(self.target.memos.history('local', imported['id'])), 3)
        self.assertEqual(self.target.memos.asset('local', asset['id'])['data'], base64.b64decode(image_data()))
        self.assertTrue(path.exists())
        self.source.save(path, '再次离线编辑', read_text_file(path)[1])
        self.assertEqual(import_workspace(self.source, self.target), 1)
        self.assertEqual(len(self.target.memos.history('local', imported['id'])), 4)

    def test_interrupted_mirror_import_retries_without_duplicate_note_or_history(self):
        self.source.create('重要备忘录', '保持内容')
        with patch.object(self.target, '_write', side_effect=OSError('fixture')):
            with self.assertRaises(OSError): import_workspace(self.source, self.target)
        self.assertEqual(import_workspace(self.source, self.target), 1)
        self.assertEqual(len(self.target.files()), 1)
        memo = self.target.memos.listing('local')[0]
        self.assertEqual(len(self.target.memos.history('local', memo['id'])), 1)
        self.assertEqual(read_text_file(self.target.files()[0])[0], '保持内容')

    def test_only_bound_account_adopts_guest_data_and_trash_is_preserved(self):
        path = self.source.create('原本机记录', '原始内容'); self.source.trash(path)
        self.assertEqual(adopt_local_workspace(self.root/'text-notes.json', {}, 'first', self.target), 1)
        other = TextNotesStore(self.root/'other-account')
        self.assertEqual(adopt_local_workspace(self.root/'text-notes.json', {}, 'second', other), 0)
        self.assertEqual(other.memos.listing('local'), [])
        self.assertTrue(self.target.memos.listing('local')[0]['deleted'])
        self.assertEqual(len(self.target.memos.history('local', self.target.memos.listing('local')[0]['id'])), 2)


if __name__ == '__main__': unittest.main()
