"""Durable credentials, session expiry, concurrent registration and desktop storage."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from accounts import AccountStore, normalize_email, SESSION_SECONDS, REMEMBERED_SESSION_SECONDS
from account_client import DesktopSession, service_url


class AccountsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = AccountStore(self.root / "accounts.sqlite3")

    def tearDown(self): self.temp.cleanup()

    def test_password_hashes_salts_and_normalized_duplicate(self):
        one = self.store.register(" Member@Example.test ", "password-123")
        self.store.register("two@example.test", "password-123")
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM users").fetchall()
        self.assertNotEqual(rows[0]["salt"], rows[1]["salt"])
        self.assertNotEqual(rows[0]["password_hash"], rows[1]["password_hash"])
        self.assertNotIn(b"password-123", self.store.path.read_bytes())
        with self.assertRaisesRegex(ValueError, "已注册"):
            self.store.register("member@example.test", "other-password")
        self.assertEqual(self.store.login("MEMBER@EXAMPLE.TEST", "password-123"), one)
        with self.assertRaisesRegex(ValueError, "邮箱或密码"):
            self.store.login("member@example.test", "wrong-password")

    def test_durable_session_expiry_revocation_and_per_account_limit(self):
        user = self.store.register("member@example.test", "password-123")
        token = self.store.issue_session(user)
        self.assertEqual(AccountStore(self.store.path).session_user(token), user)
        self.assertNotIn(token.encode(), self.store.path.read_bytes())
        with patch("accounts.time.time", return_value=10**12):
            self.assertIsNone(self.store.session_user(token))
        self.store.revoke_session(token); self.assertIsNone(self.store.session_user(token))
        tokens = [self.store.issue_session(user) for _ in range(22)]
        self.assertIsNone(self.store.session_user(tokens[0]))
        self.assertEqual(self.store.session_user(tokens[-1]), user)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sessions").fetchone()[0], 20)

    def test_registration_race_keeps_one_account(self):
        def register(_):
            try: return self.store.register("race@example.test", "password-123")
            except ValueError: return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(register, range(2)))
        self.assertEqual(sum(value is not None for value in results), 1)

    def test_email_and_service_address_validation(self):
        for email in (None, "member", "a@b", "a b@example.com", "a..b@example.com", "a@example.com\nInjected"):
            with self.assertRaises(ValueError): normalize_email(email)
        for url in ("file:///x", "http://user:secret@host", "http://host/path", "http://host:bad", "http://host:0", "http://host?token=secret"):
            with self.assertRaises(ValueError): service_url(url)
        self.assertEqual(service_url(" http://127.0.0.1:18765/ "), "http://127.0.0.1:18765")

    def test_windows_session_is_encrypted_scoped_and_clearable(self):
        store = DesktopSession(self.root / "session.bin")
        url, token = "http://127.0.0.1:18765", "private-session-token-123456"
        store.save(url, token)
        self.assertNotIn(token, store.path.read_text())
        self.assertEqual(store.load(url), token)
        self.assertEqual(store.load("http://127.0.0.1:18766"), "")
        store.save(url, ""); self.assertFalse(store.path.exists())
        store.path.write_text("damaged"); self.assertEqual(store.load(url), "")

    def test_remembered_session_survives_overnight_but_still_expires_and_revokes(self):
        user = self.store.register('remembered@example.test', 'password-123')
        with patch('accounts.time.time', return_value=100000):
            normal = self.store.issue_session(user)
            remembered = self.store.issue_session(user, remember=True)
        with patch('accounts.time.time', return_value=100000 + SESSION_SECONDS + 60):
            self.assertIsNone(self.store.session_user(normal))
            self.assertEqual(self.store.session_user(remembered), user)
            self.store.revoke_session(remembered)
            self.assertIsNone(self.store.session_user(remembered))
        with patch('accounts.time.time', return_value=100000):
            remembered = self.store.issue_session(user, remember=True)
        with patch('accounts.time.time', return_value=100000 + REMEMBERED_SESSION_SECONDS + 1):
            self.assertIsNone(self.store.session_user(remembered))

    def test_expired_credential_retains_encrypted_workspace_identity_until_logout(self):
        session = DesktopSession(self.root / 'session.bin')
        user = {'id': 'fixture-id', 'email': 'fixture@example.test'}
        session.save('https://fixture.example', '', user)
        self.assertEqual(session.user('https://fixture.example'), user)
        self.assertEqual(session.load('https://fixture.example'), '')
        self.assertNotIn(user['email'], session.path.read_text())
        session.save('https://fixture.example', '')
        self.assertFalse(session.path.exists())

    def test_remembered_client_renews_valid_session_without_replacing_token(self):
        user = self.store.register('renew@example.test', 'password-123')
        with patch('accounts.time.time', return_value=100000): token = self.store.issue_session(user)
        with patch('accounts.time.time', return_value=100001):
            self.assertEqual(self.store.session_user(token, remember=True), user)
        with patch('accounts.time.time', return_value=100000 + SESSION_SECONDS + 60):
            self.assertEqual(self.store.session_user(token), user)
        self.store.revoke_session(token)
        self.assertIsNone(self.store.session_user(token, remember=True))


if __name__ == "__main__": unittest.main()
