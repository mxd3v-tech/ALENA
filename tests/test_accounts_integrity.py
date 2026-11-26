"""Account integrity across concurrent changes, old credentials and deleted logins."""
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from checker import accounts, db, job_store, jsonstore, memory_store, teams


class AccountIntegrityTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(folder)
        self.enterContext(patch.object(db, 'DB_ENABLED', False))
        self.enterContext(patch.multiple(accounts, USERS_PATH=self.root / 'users.json',
                                         LOG_PATH=self.root / 'log.json',
                                         CONF_PATH=self.root / 'settings.json'))
        self.enterContext(patch.multiple(job_store, STORE_DIR=self.root / 'jobs',
                                          STORE_PATH=self.root / 'jobs.json'))
        self.enterContext(patch.object(memory_store, 'STORE_PATH', self.root / 'store.json'))
        self.enterContext(patch.object(teams, 'STORE_PATH', self.root / 'teams.json'))

    def create(self, login='teacher', **kwargs):
        return accounts.create_user(login, 'Преподаватель', 'Teacher2026', **kwargs)

    def test_delete_retires_credentials_and_reserves_login(self):
        original = self.create()
        key = accounts.issue_api_key('teacher')
        self.assertTrue(accounts.delete_user('teacher'))
        self.assertIsNone(accounts.get_user('teacher'))
        self.assertIsNone(accounts.user_by_api_key(key))
        self.assertIsNone(accounts.authenticate('teacher', 'Teacher2026', '', '')[0])
        archived = accounts.load_users(include_deleted=True)['teacher']
        self.assertEqual(archived['state'], 'deleted')
        self.assertEqual(archived['account_id'], original['account_id'])
        self.assertNotEqual(archived['password_hash'], original['password_hash'])
        with self.assertRaises(accounts.LoginUnavailable):
            self.create()

    def test_orphaned_logins_can_never_inherit_historical_data(self):
        job_store.save('abc123', {'owner': 'previous'})
        memory_store.save_store({'key': {'owner': 'former'}})
        teams.create_team('Course', ['colleague'])
        accounts.record_event('alumnus', True, '', '')
        for login in ('previous', 'former', 'colleague', 'alumnus'):
            with self.subTest(login=login), self.assertRaises(accounts.LoginUnavailable):
                self.create(login)
        accounts.authenticate('newteacher', 'invalid', '', '')
        self.create('newteacher')

    def test_conflicting_creation_never_replaces_existing_identity(self):
        user = self.create()
        with self.assertRaises(accounts.LoginUnavailable):
            self.create()
        self.assertEqual(accounts.get_user('teacher'), user)

    def test_existing_keys_still_work_after_bootstrap_and_round_trip(self):
        user = self.create()
        user.pop('account_id')
        user['api_key'] = 'existing-private-key'
        jsonstore.write_json(accounts.USERS_PATH, {'teacher': user})
        accounts.bootstrap()
        upgraded = accounts.get_user('teacher')
        self.assertTrue(upgraded['account_id'])
        self.assertNotIn('existing-private-key', accounts.USERS_PATH.read_text())
        self.assertEqual(accounts.user_by_api_key('existing-private-key')['account_id'],
                         upgraded['account_id'])
        accounts.save_user(upgraded)
        self.assertEqual(accounts.get_user('teacher'), upgraded)
        self.assertIsNone(accounts.user_by_api_key(upgraded['api_key']))

    def test_digest_shaped_legacy_key_remains_a_bearer_key(self):
        user = self.create()
        user.pop('account_id')
        old_key = 'sha256:' + 'a' * 64
        user['api_key'] = old_key
        jsonstore.write_json(accounts.USERS_PATH, {'teacher': user})
        accounts.bootstrap()
        self.assertEqual(accounts.user_by_api_key(old_key)['login'], 'teacher')
        self.assertNotIn(old_key, accounts.USERS_PATH.read_text())

    def test_keys_are_never_stored_as_bearer_tokens(self):
        self.create()
        key = accounts.issue_api_key('teacher')
        self.assertTrue(accounts.user_by_api_key(key))
        self.assertNotIn(key, accounts.USERS_PATH.read_text())
        stored = accounts.get_user('teacher')['api_key']
        self.assertIsNone(accounts.user_by_api_key(stored))
        replacement = accounts.issue_api_key('teacher')
        self.assertIsNone(accounts.user_by_api_key(key))
        self.assertTrue(accounts.user_by_api_key(replacement))

    def test_reset_preserves_administrative_block(self):
        self.create()
        self.assertTrue(accounts.toggle_blocked('teacher'))
        self.assertTrue(accounts.set_password('teacher', 'Replacement2026', must_change=True))
        self.assertEqual(accounts.get_user('teacher')['state'], 'blocked')
        self.assertIsNone(accounts.authenticate('teacher', 'Replacement2026', '', '')[0])
        self.assertFalse(accounts.toggle_blocked('teacher'))
        self.assertEqual(accounts.get_user('teacher')['state'], 'pending')

    def test_current_password_cannot_satisfy_forced_change(self):
        self.create()
        with self.assertRaisesRegex(ValueError, 'отличаться'):
            accounts.set_password('teacher', 'Teacher2026', current='Teacher2026')
        self.assertTrue(accounts.get_user('teacher')['must_change'])
        self.assertFalse(accounts.set_password('teacher', 'Changed2027', current='not-correct'))
        self.assertTrue(accounts.set_password('teacher', 'Changed2027', current='Teacher2026'))
        self.assertFalse(accounts.get_user('teacher')['must_change'])

    def test_parallel_authentication_retains_all_attempts(self):
        self.create()
        accounts.save_settings({'lock_after_fails': 0})
        with patch.object(accounts, 'check_password_hash', return_value=False):
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: accounts.authenticate('teacher', 'bad', '', ''), range(8)))
        self.assertEqual(accounts.get_user('teacher')['fail_count'], 8)

    def test_bootstrap_requires_configured_password(self):
        with patch.dict(os.environ, AU_PASSWORD=''):
            with self.assertRaisesRegex(RuntimeError, 'AU_PASSWORD'):
                accounts.bootstrap()
        self.assertEqual(accounts.load_users(), {})
        with patch.dict(os.environ, AU_USERNAME='owner', AU_PASSWORD='Unique2026pw',
                        AU_API_KEY='private-bootstrap-key'):
            accounts.bootstrap()
        self.assertEqual(accounts.user_by_api_key('private-bootstrap-key')['login'], 'owner')
        self.assertNotIn('private-bootstrap-key', accounts.USERS_PATH.read_text())
