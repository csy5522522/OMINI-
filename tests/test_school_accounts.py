from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import tempfile
import unittest

from accounts import AccountStore, SCHOOL_ACCOUNT_DOMAIN


class SchoolAccountTests(unittest.TestCase):
    def test_stable_school_identity_and_disabled_account(self):
        with tempfile.TemporaryDirectory() as directory:
            store=AccountStore(Path(directory)/'accounts.sqlite3')
            a=hashlib.sha256(b'verified-school-a').hexdigest();b=hashlib.sha256(b'verified-school-b').hexdigest()
            user=store.school_user(a)
            self.assertEqual(user['auth_provider'],'school')
            self.assertEqual(store.school_user(a)['id'],user['id'])
            self.assertNotEqual(store.school_user(b)['id'],user['id'])
            token=store.issue_session(user)
            self.assertEqual(store.session_user(token)['display_name'],user['display_name'])
            with self.assertRaises(ValueError):store.register(user['email'],'FixturePass-123')
            with self.assertRaises(ValueError):store.login(user['email'],'FixturePass-123')
            store.manage_user(user['id'],disabled=True)
            self.assertIsNone(store.session_user(token))
            with self.assertRaisesRegex(ValueError,'停用'):store.school_user(a)

    def test_simultaneous_first_scan_creates_one_account(self):
        with tempfile.TemporaryDirectory() as directory:
            store=AccountStore(Path(directory)/'accounts.sqlite3');key=hashlib.sha256(b'same-school-student').hexdigest()
            with ThreadPoolExecutor(2) as pool:
                users=list(pool.map(lambda _:store.school_user(key),range(2)))
            self.assertEqual(users[0]['id'],users[1]['id'])
            self.assertEqual(len(store.list_users()),1)
