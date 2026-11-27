import io
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from checker import accounts, db, job_store, jsonstore, memory_store, sqlmigrate, teams


class MigrationIntegrityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for module, attr, new in [
            (db, 'DB_ENABLED', False),
            (accounts, 'USERS_PATH', self.root / 'users.json'),
            (accounts, 'LOG_PATH', self.root / 'log.json'),
            (accounts, 'CONF_PATH', self.root / 'settings.json'),
            (job_store, 'STORE_DIR', self.root / 'jobs'),
            (job_store, 'STORE_PATH', self.root / 'jobs.json'),
            (teams, 'STORE_PATH', self.root / 'teams.json'),
            (memory_store, 'STORE_PATH', self.root / 'store.json'),
        ]:
            context = patch.object(module, attr, new)
            context.start()
            self.addCleanup(context.stop)
        accounts.save_user({'login': 'admin', 'fio': 'Админ', 'role': 'admin', 'state': 'active',
                            'password_hash': 'original', 'perms': {}, 'fail_count': 0})
        job_store.save('abc123', {'owner': 'admin', 'status': 'done'})
        accounts.record_event('admin', True, '', '', 'вход')
        accounts.save_settings({'retention_days': 90})

    def test_empty_truncated_and_missing_rows_cannot_replace_a_database(self):
        backup = sqlmigrate.dump()
        dropped = '\n'.join(s for s in backup.splitlines() if not s.startswith('INSERT INTO jobs'))
        for broken in ('', backup.split('COMMIT;')[0], dropped,
                       backup.replace('DELETE FROM teams;', ''),
                       re.sub(r'-- ALENA rows .*', '-- ALENA rows {"users": 1}', backup),
                       backup.replace("'abc123'", "'invalid/job'")):
            with self.subTest(length=len(broken)), self.assertRaises(ValueError):
                sqlmigrate.restore_file(io.BytesIO(broken.encode()), replace=True)
            self.assertEqual(job_store.get('abc123')['status'], 'done')
            self.assertEqual(accounts.recent_logins()[0]['reason'], 'вход')

    def test_pre_teams_count_header_and_configured_spool_directory(self):
        # Older releases placed counts before BEGIN, with settings implicit.
        backup = ('-- Учётных записей: 1, проверок: 1, отпечатков: 0, записей журнала: 1\n'
                  + re.sub(r'-- ALENA rows .*\n', '', sqlmigrate.dump())
                  .replace('DELETE FROM teams;', ''))
        tmp_root = self.root / 'import-data'
        with patch.dict(os.environ, AU_TMP_DIR=str(tmp_root)):
            with sqlmigrate.staged_rows(io.BytesIO(backup.encode()), replace=True) as stage:
                self.assertEqual(len(list(tmp_root.glob('alena_import_*/rows.sqlite'))), 1)
                sqlmigrate.restore(stage, replace=True)
        self.assertFalse(list(tmp_root.iterdir()))
        self.assertEqual(job_store.count(), 1)
        self.assertEqual(accounts.get_user('admin')['perms'], {})

    def test_backup_round_trips_fractional_and_legacy_similarity_scores(self):
        current = {'threshold': 60.25, 'plag': 60.5, 'plag_flagged': True,
                   'students': [{'fio': 'Иванов', 'plag': 60.5, 'plag_flagged': True},
                                {'fio': 'Петров', 'plag': 59.6, 'plag_flagged': False}],
                   'matches': [{'pct': 60.5}, {'pct': None}]}
        legacy = {'threshold': 60, 'plag': 80, 'students': [{'fio': 'Иванов', 'plag': 80}],
                  'matches': [{'pct': 80}]}
        for job_id, digest in [('70c2e', current), ('70c2e6ba74ee476687593db2ece46355', legacy)]:
            job_store.save(job_id, {'owner': 'admin', 'status': 'done',
                                    'threshold': digest['threshold'], 'summary': digest})
        saved = job_store.load_all()
        backup = sqlmigrate.dump()
        job_store.clear()
        sqlmigrate.restore_file(io.StringIO(backup), replace=True)
        self.assertEqual(job_store.load_all(), saved)

    def test_invalid_similarity_fields_are_rejected(self):
        for data, field in [
            ({'threshold': 100.01}, 'jobs.threshold'),
            ({'summary': {'plag': -1}}, 'jobs.summary.plag'),
            ({'summary': {'threshold': True}}, 'jobs.summary.threshold'),
            ({'summary': {'plag_flagged': 1}}, 'jobs.summary.plag_flagged'),
            ({'summary': {'students': [{'fio': 'A', 'plag': '60'}]}}, 'jobs.students.plag'),
            ({'summary': {'students': [{'fio': 'A', 'plag_flagged': 'false'}]}}, 'jobs.students.plag_flagged'),
            ({'summary': {'matches': [{'pct': False}]}}, 'jobs.matches.pct'),
        ]:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, re.escape(field)):
                sqlmigrate.restore({'jobs': [{'job_id': 'fedcba', 'data': dict(data, status='done')}]})

    def test_history_without_account_is_archived_unless_login_was_unknown(self):
        def event(login, reason):
            return {'ts': '01.01.2025', 'login': login, 'ok': False, 'reason': reason}
        sqlmigrate.restore({'login_events': [event('alumnus', 'пароль неверен'),
                                            event('typo', 'учётной записи не существует')]})
        users = accounts.load_users(include_deleted=True)
        self.assertEqual(users['alumnus']['state'], 'deleted')
        self.assertNotIn('typo', users)

    def test_invalid_late_row_cannot_partially_merge(self):
        backup = sqlmigrate.dump().replace("'abc123'", "'fedcba'")
        backup = backup.replace("90,", '"invalid",', 1)
        with self.assertRaises(ValueError):
            sqlmigrate.restore_file(io.StringIO(backup))
        self.assertIsNone(job_store.get('fedcba'))

    def test_rekeyed_anonymous_entries_retain_public_id_across_two_restores(self):
        _, entry = memory_store._entry_for({'filename': 'scan.pdf'}, 'abc123', 'admin')
        public = 'admin|||v7'
        sqlmigrate.restore({'fingerprints': [dict(entry, entry_key=public, key_base='admin||', version=7)]})
        normalized = memory_store.load_store()[public]
        self.assertEqual(normalized['version'], 1)
        sqlmigrate.restore_file(io.StringIO(sqlmigrate.dump()), replace=True)
        self.assertEqual(memory_store.load_store()[public], normalized)

    def test_named_fingerprint_version_and_key_must_agree(self):
        base, entry = memory_store._entry_for({'student': {'name': 'Иванов'}}, 'abc123', 'admin')
        with self.assertRaisesRegex(ValueError, 'версия'):
            sqlmigrate.restore({'fingerprints': [dict(entry, entry_key=base + '|v2', version=1)]})
        self.assertEqual(memory_store.load_store(), {})

    def test_foreign_identity_cannot_take_over_protected_admin_or_history(self):
        backup = sqlmigrate.dump()
        admin = accounts.get_user('admin')
        backup = backup.replace(admin['account_id'], 'f' * 32)
        with self.assertRaisesRegex(ValueError, 'локальным администратором'):
            sqlmigrate.restore_file(io.StringIO(backup), replace=True, keep_login='admin')
        self.assertEqual(accounts.get_user('admin'), admin)

    def test_full_merge_rejects_orphan_history_for_occupied_login(self):
        job_store.save('fedcba', {'owner': 'teacher', 'status': 'done'})
        backup = sqlmigrate.dump()
        job_store.clear('teacher')
        accounts.save_user({'login': 'teacher', 'role': 'teacher', 'password_hash': 'other'})
        with self.assertRaisesRegex(ValueError, 'локальный логин уже занят'):
            sqlmigrate.restore_file(io.StringIO(backup), keep_login='admin')
        self.assertIsNone(job_store.get('fedcba'))
        self.assertEqual(accounts.get_user('teacher')['password_hash'], 'other')

    def test_legacy_credential_is_preserved_when_restoring_local_admin(self):
        rows = sqlmigrate.parse(sqlmigrate.dump())
        incoming = rows['users'][0]
        incoming.pop('account_id')
        incoming['state'] = 'blocked'
        before = accounts.get_user('admin')
        sqlmigrate.restore(rows, replace=True, keep_login='admin')
        self.assertEqual(accounts.get_user('admin'), before)

    def test_failure_in_final_settings_write_rolls_back_all_previous_tables(self):
        backup = sqlmigrate.dump().replace('abc123', 'fedcba')
        prior = accounts.load_users(include_deleted=True)
        original_write = jsonstore.write_json

        def fail_settings(path, value):
            if path == accounts.CONF_PATH:
                raise OSError('disk full')
            original_write(path, value)

        with patch.object(jsonstore, 'write_json', side_effect=fail_settings), self.assertRaises(OSError):
            sqlmigrate.restore_file(io.StringIO(backup), replace=True)
        self.assertEqual(accounts.load_users(include_deleted=True), prior)
        self.assertIsNone(job_store.get('fedcba'))
        self.assertEqual(job_store.get('abc123')['status'], 'done')
        self.assertEqual(accounts.recent_logins()[0]['reason'], 'вход')

    def test_export_observes_one_snapshot_across_tables(self):
        started = threading.Event()
        allowed = threading.Event()

        def publish():
            allowed.wait(3)
            started.set()
            with sqlmigrate.transaction():
                job_store.save('fedcba', {'owner': 'admin', 'status': 'done'})
                accounts.save_settings({'retention_days': 365})

        class SlowOutput(io.StringIO):
            def write(self, data):
                if data.startswith('INSERT INTO jobs'):
                    allowed.set()
                    started.wait(3)
                return super().write(data)

        thread = threading.Thread(target=publish)
        thread.start()
        out = SlowOutput()
        try:
            sqlmigrate.write_dump(out)
        finally:
            allowed.set()
            thread.join(3)
        rows = sqlmigrate.parse(out.getvalue())
        self.assertEqual([r['job_id'] for r in rows['jobs']], ['abc123'])
        self.assertEqual(rows['settings'][0]['value']['retention_days'], 90)
        self.assertEqual(accounts.get_settings()['retention_days'], 365)

    def test_full_log_survives_round_trip_and_next_login(self):
        event = {'ts': '01.01.2025 12:00:00', 'login': 'admin', 'ok': True,
                 'reason': "note '--';\n-- part of the quoted string"}
        accounts.add_events([event] * 2100)
        out = io.StringIO()
        sqlmigrate.write_dump(out)
        sqlmigrate.restore_file(io.BytesIO(out.getvalue().encode()), replace=True, keep_login='admin')
        accounts.record_event('admin', True, '', '')
        events = accounts.recent_logins(None)
        self.assertEqual(len(events), 2102)
        self.assertEqual(events[1]['reason'], event['reason'])
        self.assertEqual(events[-1]['reason'], 'вход')

    def test_process_death_rolls_back_file_replacement_deletion_and_creation(self):
        jsonstore.write_json(self.root / 'before.json', {'old': 1})
        jsonstore.write_json(self.root / 'removed.json', {'old': 2})
        code = """from pathlib import Path
from checker import jsonstore
import os, sys
root = Path(sys.argv[1])
with jsonstore.transaction(root):
    jsonstore.write_json(root / 'before.json', {'new': True})
    jsonstore.unlink(root / 'removed.json')
    jsonstore.write_json(root / 'new.json', {})
    os._exit(7)
"""
        run = subprocess.run([sys.executable, '-c', code, str(self.root)], check=False)
        self.assertEqual(run.returncode, 7)
        sqlmigrate.recover_json()
        self.assertEqual(jsonstore.read_json(self.root / 'before.json', {}), {'old': 1})
        self.assertEqual(jsonstore.read_json(self.root / 'removed.json', {}), {'old': 2})
        self.assertFalse((self.root / 'new.json').exists())
        self.assertFalse((self.root / '.transaction').exists())
