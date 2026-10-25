"""Data-preserving regressions from the October 2026 audit; temporary fixtures only."""
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import app
from memo_store import MemoStore
from memo_sync import sync_memos
from text_notes import TextNotesStore, read_text_file


class LocalClient:
    server = None
    drop_at = 0
    writes = 0
    legacy = False

    def __init__(self, *args): pass

    def api(self, path, value=None):
        if path == '/api/memos' and value is None:
            return {'memos': self.server.listing('fixture')}
        if path == '/api/memos':
            value = dict(value)
            if self.legacy: value.pop('operation_id', None)
            result = self.server.save('fixture', value, action=value.get('action', 'edit'))
            type(self).writes += 1
            if self.writes == self.drop_at:
                raise ValueError('Fixture: response lost after commit')
            return {'memo': result}
        if path.endswith('/history'):
            return {'versions': self.server.history('fixture', path.split('/')[3])}
        raise AssertionError(path)


class MemoAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='omini-audit-regression-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = TextNotesStore(self.root / 'client')
        LocalClient.server = MemoStore(self.root / 'server.sqlite3')
        LocalClient.drop_at = LocalClient.writes = 0
        LocalClient.legacy = False
        self.client = patch('memo_sync.AccountClient', LocalClient)
        self.client.start(); self.addCleanup(self.client.stop)

    def sync(self):
        return sync_memos(self.store.directory, 'http://fixture.test', 'fixture')

    def test_long_duplicate_titles_preserve_both_mirrors_and_cloud(self):
        for n in (1, 2, 3):
            LocalClient.server.save('fixture', {'id': str(n) * 32, 'title': 'a' * 160,
                'content': f'unique {n}'})
        before = {m['id']: m['content'] for m in LocalClient.server.listing('fixture')}
        self.sync(); self.sync()
        store = TextNotesStore(self.store.directory)
        self.assertEqual(len(store.files()), 3)
        self.assertEqual(len(set(store.mapping().values())), 3)
        for memo_id, name in store.mapping().items():
            self.assertEqual(read_text_file(store.directory / name)[0], before[memo_id])
        self.assertEqual({m['id']: m['content'] for m in LocalClient.server.listing('fixture')}, before)

    def test_windows_title_normalization_never_overwrites_another_memo(self):
        titles = ['shared?title', 'shared*title', '.', '..']
        for n, title in enumerate(titles, 1):
            LocalClient.server.save('fixture', {'id': str(n) * 32, 'title': title, 'content': title})
        self.sync(); self.sync()
        self.assertEqual(len(self.store.files()), 4)
        self.assertEqual({read_text_file(p)[0] for p in self.store.files()}, set(titles))

    def test_rejected_save_and_create_leave_no_partial_data(self):
        path = self.store.create('fixture', 'ORIGINAL')
        raw, digest = path.read_bytes(), read_text_file(path)[1]
        with self.assertRaises(ValueError):
            self.store.save(path, 'x' * (2 * 1024 * 1024 + 1), digest)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(self.store.for_path(path)['content'], 'ORIGINAL')
        with self.assertRaises(ValueError):
            self.store.create('oversized', 'x' * (2 * 1024 * 1024 + 1))
        self.assertEqual(len(self.store.files()), 1)
        self.store.save(path, 'corrected', digest)

    def test_database_failure_rolls_back_mirror_and_history(self):
        path = self.store.create('fixture', 'ORIGINAL')
        raw, digest = path.read_bytes(), read_text_file(path)[1]
        with patch.object(self.store.memos, 'set_data', side_effect=OSError('fixture disk failure')):
            with self.assertRaises(OSError): self.store.save(path, 'new', digest)
        self.assertEqual(path.read_bytes(), raw)
        memo = self.store.for_path(path)
        self.assertEqual(memo['content'], 'ORIGINAL')
        self.assertEqual(len(self.store.memos.history('local', memo['id'])), 1)

    def test_old_shared_mapping_is_repaired_without_changing_key_or_versions(self):
        values = [{'id': str(n) * 32, 'title': 'a' * 160, 'content': f'original {n}', 'deleted': False} for n in (1, 2)]
        path = self.store.target('a' * 160)
        for value in values:
            self.store.memos.save('local', value)
        self.store._write(path, values[-1]['title'], values[-1]['content'])
        original_raw = path.read_bytes()
        self.store.memos.set_data('local', 'paths', {v['id']: path.name for v in values})
        key = self.store.memos.cipher.path.read_bytes()
        repaired = TextNotesStore(self.store.directory)
        self.assertEqual(repaired.memos.cipher.path.read_bytes(), key)
        self.assertEqual(len(repaired.files()), 2)
        for value in values:
            self.assertEqual(repaired.memos.get('local', value['id'])['version'], 1)
            self.assertEqual(read_text_file(repaired.directory / repaired.mapping()[value['id']])[0], value['content'])
        self.assertTrue(any(p.read_bytes() == original_raw for p in (repaired.directory / '.collision-backup').glob('*.enc')))
        self.assertEqual(TextNotesStore(self.store.directory).mapping(), repaired.mapping())

    def test_legacy_migration_and_import_keep_colliding_titles_separate(self):
        from memo_import import import_workspace
        legacy = self.root / 'legacy'; legacy.mkdir()
        (legacy / ('a' * 160 + '1.txt')).write_text('first', encoding='utf-8')
        (legacy / ('a' * 160 + '2.txt')).write_text('second', encoding='utf-8')
        migrated = TextNotesStore(legacy)
        self.assertEqual({read_text_file(p)[0] for p in migrated.files()}, {'first', 'second'})
        self.store.create('a' * 160, 'cloud original')
        import_workspace(migrated, self.store)
        import_workspace(migrated, self.store)
        self.assertEqual({read_text_file(p)[0] for p in self.store.files()}, {'first', 'second', 'cloud original'})

    def test_old_interrupted_import_reallocates_an_occupied_planned_path(self):
        import uuid
        from memo_import import import_workspace
        source = TextNotesStore(self.root / 'source')
        original = source.create('a' * 160, 'imported')
        memo = source.for_path(original)
        cloud = self.store.create('a' * 160, 'cloud original')
        target_id = uuid.uuid5(uuid.NAMESPACE_URL, 'omini-local-memo:' + memo['id']).hex
        self.store.memos.save('local', {**memo, 'id': target_id, 'base_version': 0})
        self.store.memos.set_data('local', 'local_imports', {memo['id']: {'id': target_id, 'version': 0,
            'pending': 1, 'target_version': 1, 'path': cloud.name}})
        self.assertEqual(import_workspace(source, self.store), 1)
        self.assertEqual(read_text_file(cloud)[0], 'cloud original')
        self.assertEqual(len(self.store.files()), 2)
        self.assertEqual(len(self.store.memos.history('local', target_id)), 1)

    def test_rename_and_delete_mapping_failures_keep_original_file_and_action(self):
        path = self.store.create('fixture', 'ORIGINAL')
        raw = path.read_bytes()
        target = self.store.target('renamed')
        for operation in (lambda: self.store.rename(path, target), lambda: self.store.trash(path)):
            with patch.object(self.store.memos, 'set_data', side_effect=OSError('fixture disk failure')):
                with self.assertRaises(OSError): operation()
            self.assertEqual(path.read_bytes(), raw)
            self.assertFalse(target.exists())
            memo = self.store.for_path(path)
            self.assertEqual((memo['title'], memo['deleted'], memo['version']), ('fixture', False, 1))

    def test_committed_edit_then_concurrent_cloud_edit_is_adopted_without_copy(self):
        path = self.store.create('fixture', 'committed')
        LocalClient.drop_at = 1
        with self.assertRaises(ValueError): self.sync()
        remote = LocalClient.server.listing('fixture')[0]
        LocalClient.server.save('fixture', {**remote, 'content': 'new cloud edit', 'base_version': remote['version']})
        result = self.sync()
        self.assertEqual(result['conflicts'], 0)
        self.assertEqual(len(LocalClient.server.listing('fixture')), 1)
        self.assertEqual(read_text_file(path)[0], 'new cloud edit')

    def test_server_operation_replay_returns_original_version_and_isolates_accounts(self):
        payload = {'id': 'a' * 32, 'title': 'fixture', 'content': 'first', 'operation_id': 'b' * 64}
        first = LocalClient.server.save('fixture', payload)
        LocalClient.server.save('fixture', {**first, 'content': 'later', 'base_version': 1})
        self.assertEqual(LocalClient.server.save('fixture', payload), first)
        self.assertEqual(LocalClient.server.get('fixture', payload['id'])['content'], 'later')
        with self.assertRaises(ValueError): LocalClient.server.save('fixture', {**payload, 'content': 'changed'})
        other = LocalClient.server.save('other', payload)
        self.assertEqual(other['version'], 1)
        self.assertEqual(len(LocalClient.server.history('fixture', payload['id'])), 2)

    def test_response_loss_retries_each_action_without_duplicates(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                LocalClient.legacy = legacy
                path = self.store.create(f'fixture {legacy}', 'first')
                self.store.save(path, 'second', read_text_file(path)[1])
                LocalClient.drop_at = LocalClient.writes + 2
                with self.assertRaises(ValueError): self.sync()
                result = self.sync(); self.sync()
                self.assertEqual(result['conflicts'], 0)
                memo = next(m for m in LocalClient.server.listing('fixture') if m['title'] == f'fixture {legacy}')
                self.assertEqual(memo['content'], 'second')
                self.assertEqual(len(LocalClient.server.history('fixture', memo['id'])), 2)
        self.assertEqual(len(LocalClient.server.listing('fixture')), 2)

    def test_conflict_copy_retry_preserves_both_edits_once(self):
        path = self.store.create('fixture', 'base'); self.sync()
        memo = self.store.for_path(path)
        self.store.save(path, 'offline first', read_text_file(path)[1])
        self.store.save(path, 'offline second', read_text_file(path)[1])
        remote = LocalClient.server.get('fixture', memo['id'])
        LocalClient.server.save('fixture', {**remote, 'content': 'remote edit', 'base_version': remote['version']})
        LocalClient.drop_at = LocalClient.writes + 2
        with self.assertRaises(ValueError): self.sync()
        self.sync(); self.sync()
        rows = LocalClient.server.listing('fixture')
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['content'] for r in rows}, {'remote edit', 'offline second'})
        conflict = next(r for r in rows if r['id'] != memo['id'])
        self.assertEqual(len(LocalClient.server.history('fixture', conflict['id'])), 2)


class QueueAuditTests(unittest.TestCase):
    def test_bad_queue_rows_are_isolated_with_original_backup(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            good = asdict(app.DownloadTask('good', 'file', 'https://fixture.test/file', str(root)))
            raw = json.dumps([{'id': 'bad'}, good, None, {**good, 'id': 'invalid', 'progress': 'bad'}]).encode()
            (root / 'tasks.json').write_bytes(raw)
            with patch.object(app, 'APP_DIR', root), patch.object(app, 'TASKS_FILE', root / 'tasks.json'):
                manager = app.TaskManager({})
                self.assertEqual([t.id for t in manager.tasks], ['good'])
                manager.save_tasks()
            self.assertTrue(any(p.read_bytes() == raw for p in (root / 'queue-backups').glob('*.json')))
            self.assertEqual(json.loads((root / 'tasks.json').read_text(encoding='utf-8'))[0]['id'], 'good')

    def test_progress_reports_optional_speed_and_eta(self):
        worker = app.DownloadWorker(app.DownloadTask('fixture', 'file', 'fixture', '.'))
        values = []
        worker.progress.connect(lambda *args: values.append(args[1:]))
        for line in ('[download] 42.0% of 100MiB at 1.23MiB/s ETA 00:47',
                     '[download] 50.0% of 100MiB at Unknown speed',
                     '[download] 60% of 100MiB',
                     '[#fixture 42MiB/100MiB(42%) CN:1 DL:2MiB ETA:29s]'):
            worker._parse_progress(line)
        self.assertEqual(values[0], (42.0, '1.23MiB/s', '00:47'))
        self.assertEqual(values[1], (50.0, 'Unknown', '—'))
        self.assertEqual(values[2], (60.0, '—', '—'))
        self.assertEqual(values[3], (42.0, '2MiB', '29s'))


if __name__ == '__main__': unittest.main()
