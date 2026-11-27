import io
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import imagehash
import pymupdf
from PIL import Image

import tests  # Initialize the sandbox even for top-level unittest discovery.
from checker import accounts, db, job_store, memory_store, sqlmigrate, teams
from checker.image_plagiarism import check_image_plagiarism
from checker.reporter import _anchor, _report_anchors, generate_html_report
from checker.text_plagiarism import (
    check_text_plagiarism,
    normalize_text,
    text_fingerprint,
)
from checker.memory_store import NO_STUDENT, student_id


# The web tests supply their own users and need no bootstrap administrator.
with patch.object(accounts, 'bootstrap'):
    import app as web


def _user(login='teacher', role='teacher', **overrides):
    perms = {
        'run_checks': True,
        'delete_own': True,
        'delete_all': False,
        'manage_base': False,
        'see_all': False,
        'use_api': False,
    }
    perms.update(overrides)
    return {
        'login': login,
        'account_id': login + '-test-id',
        'fio': login.title(),
        'role': role,
        'state': 'active',
        'must_change': False,
        'perms': perms,
    }


class ClearAuthorizationTests(unittest.TestCase):
    def setUp(self):
        web.app.config.update(TESTING=True)
        with web.jobs_lock:
            web.jobs.clear()
        self.client = web.app.test_client()
        with self.client.session_transaction() as session:
            session['login'] = 'teacher'
            session['account_id'] = 'teacher-test-id'
            session['csrf'] = 'test-csrf'
        self.headers = {'X-CSRF-Token': 'test-csrf'}

    def _as(self, user):
        return patch.object(web.accounts, 'get_user', return_value=user)

    def test_polling_after_session_expiry_returns_json_while_navigation_opens_login(self):
        with self._as(None):
            for path in ('/jobs', '/jobs/active', '/status/abcdef'):
                with self.subTest(path=path):
                    res = self.client.get(path, headers={'Accept': 'application/json'})
                    self.assertEqual(res.status_code, 401)
                    self.assertTrue(res.get_json()['error'])
            self.assertEqual(self.client.get('/new').status_code, 302)

    def test_password_change_required_rejects_ajax_action_and_poll(self):
        with (self._as(dict(_user(), must_change=True)), patch.object(web, '_memory_delete') as delete):
            response = self.client.post('/memory/delete', json={'key': 'student'}, headers=self.headers)
            self.assertEqual(response.status_code, 403)
            self.assertIn('пароль', response.get_json()['error'])
            delete.assert_not_called()
            status = self.client.get('/status/abcdef', headers={'Accept': 'application/json'})
            self.assertEqual(status.status_code, 403)
            self.assertIn('пароль', status.get_json()['error'])
            self.assertEqual(self.client.get('/new').status_code, 302)

    def test_blocked_account_cannot_delete_with_an_open_pages_token(self):
        with (self._as(dict(_user(), state='blocked')), patch.object(web, '_memory_delete') as delete):
            response = self.client.post('/memory/delete', json={'key': 'student'}, headers=self.headers)
            self.assertEqual(response.status_code, 403)
            self.assertTrue(response.get_json()['error'])
            delete.assert_not_called()

    def test_html_clear_is_always_scoped_to_caller(self):
        user = _user(see_all=True)
        with (self._as(user),
              patch.object(web.job_store, 'load_all', return_value={}),
              patch.object(web.job_store, 'clear', return_value=[]) as clear_jobs,
              patch('checker.memory_store.clear_store', return_value=0) as clear_store):
            response = self.client.post('/jobs/clear', headers=self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['scope'], 'own')
        clear_jobs.assert_called_once_with('teacher')
        clear_store.assert_called_once_with('teacher')

    def test_api_requires_explicit_scope(self):
        with (self._as(_user()),
              patch.object(web.job_store, 'clear') as clear_jobs):
            response = self.client.delete('/api/v1/jobs', headers=self.headers)

        self.assertEqual(response.status_code, 400)
        clear_jobs.assert_not_called()

    def test_teacher_cannot_clear_all_even_with_visibility_and_permission(self):
        user = _user(see_all=True, delete_all=True)
        with (self._as(user),
              patch.object(web.job_store, 'clear') as clear_jobs):
            response = self.client.delete(
                '/api/v1/jobs?scope=all&confirm_login=teacher',
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 403)
        clear_jobs.assert_not_called()

    def test_admin_clear_all_requires_separate_permission(self):
        admin = _user(login='admin', role='admin', see_all=True)
        with self.client.session_transaction() as session:
            session['login'] = 'admin'
            session['account_id'] = 'admin-test-id'
        with (self._as(admin),
              patch.object(web.job_store, 'clear') as clear_jobs):
            response = self.client.delete(
                '/api/v1/jobs?scope=all&confirm_login=admin',
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 403)
        clear_jobs.assert_not_called()

    def test_admin_clear_all_requires_matching_login(self):
        admin = _user(login='admin', role='admin', delete_all=True)
        with self.client.session_transaction() as session:
            session['login'] = 'admin'
            session['account_id'] = 'admin-test-id'
        with (self._as(admin),
              patch.object(web.job_store, 'clear') as clear_jobs):
            response = self.client.delete(
                '/api/v1/jobs?scope=all&confirm_login=wrong',
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 400)
        clear_jobs.assert_not_called()

    def test_admin_clear_all_passes_global_scope_only_after_confirmation(self):
        admin = _user(login='admin', role='admin', delete_all=True)
        with self.client.session_transaction() as session:
            session['login'] = 'admin'
            session['account_id'] = 'admin-test-id'
        with (self._as(admin),
              patch.object(web.job_store, 'load_all', return_value={}),
              patch.object(web.job_store, 'clear', return_value=[]) as clear_jobs,
              patch('checker.memory_store.clear_store', return_value=0) as clear_store):
            response = self.client.delete(
                '/api/v1/jobs?scope=all&confirm_login=admin',
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['scope'], 'all')
        clear_jobs.assert_called_once_with(None)
        clear_store.assert_called_once_with(None)

    def test_separate_html_action_requires_typed_admin_login(self):
        admin = _user(login='admin', role='admin', delete_all=True)
        with self.client.session_transaction() as session:
            session['login'] = 'admin'
            session['account_id'] = 'admin-test-id'
        with (self._as(admin),
              patch.object(web.job_store, 'load_all', return_value={}),
              patch.object(web.job_store, 'clear', return_value=[]) as clear_jobs,
              patch('checker.memory_store.clear_store', return_value=0)):
            rejected = self.client.post(
                '/jobs/clear/all', json={'confirm_login': 'wrong'},
                headers=self.headers,
            )
            accepted = self.client.post(
                '/jobs/clear/all', json={'confirm_login': 'admin'},
                headers=self.headers,
            )

        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(accepted.status_code, 200)
        clear_jobs.assert_called_once_with(None)

    def test_bulk_clear_rejects_processing_jobs_before_mutation(self):
        running = {'abcdef': {
            'owner': 'teacher',
            'status': 'processing',
            'beat': web.time.time(),
        }}
        with (self._as(_user()),
              patch.object(web.job_store, 'load_all', return_value=running),
              patch.object(web.job_store, 'clear') as clear_jobs,
              patch('checker.memory_store.clear_store') as clear_store):
            response = self.client.post('/jobs/clear', headers=self.headers)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['processing'], 1)
        clear_jobs.assert_not_called()
        clear_store.assert_not_called()

    def test_bulk_clear_holds_job_lock_through_storage_mutation(self):
        def clear_jobs(owner):
            acquired = web.jobs_lock.acquire(blocking=False)
            if acquired:
                web.jobs_lock.release()
            self.assertFalse(acquired)
            return []

        with (self._as(_user()),
              patch.object(web.job_store, 'load_all', return_value={}),
              patch.object(web.job_store, 'clear', side_effect=clear_jobs),
              patch('checker.memory_store.clear_store', return_value=0)):
            response = self.client.post('/jobs/clear', headers=self.headers)

        self.assertEqual(response.status_code, 200)

    def test_stale_processing_record_does_not_block_clear_forever(self):
        stale = {'abcdef': {
            'owner': 'teacher',
            'status': 'processing',
            'beat': 0,
        }}
        with (self._as(_user()),
              patch.object(web.job_store, 'load_all', return_value=stale),
              patch.object(web.job_store, 'save') as save_job,
              patch.object(web.job_store, 'clear', return_value=[]),
              patch('checker.memory_store.clear_store', return_value=0)):
            response = self.client.post('/jobs/clear', headers=self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(save_job.call_args.args[1]['status'], 'error')

    def test_delayed_stale_read_cannot_recreate_cleared_job(self):
        stale = {
            'owner': 'teacher',
            'status': 'processing',
            'beat': 0,
        }
        with (patch.object(web.job_store, 'get', return_value=None),
              patch.object(web.job_store, 'save') as save_job):
            marked = web._mark_if_stale('abcdef', stale)

        self.assertEqual(marked['status'], 'error')
        save_job.assert_not_called()

    def test_job_snapshot_is_saved_under_bulk_operation_lock(self):
        with web.jobs_lock:
            web.jobs['abcdef'] = {
                'owner': 'teacher',
                'status': 'processing',
                'beat': web.time.time(),
            }

        def save_job(job_id, data):
            acquired = web.jobs_lock.acquire(blocking=False)
            if acquired:
                web.jobs_lock.release()
            self.assertFalse(acquired)

        with patch.object(web.job_store, 'save', side_effect=save_job):
            web._update('abcdef', _force=True, progress=50)

    def test_sql_restore_invalidates_completed_jobs_in_both_modes(self):
        admin = _user(login='admin', role='admin', delete_all=True)
        with self.client.session_transaction() as session:
            session['login'] = 'admin'
            session['account_id'] = 'admin-test-id'
        stats = {name: 0 for name in sqlmigrate.TABLES}
        stats['deleted_users'] = 0
        stats['cleared_jobs'] = 0
        with (self._as(admin),
              patch.object(web.job_store, 'load_all', return_value={}),
              patch.object(web.sqlmigrate, 'staged_rows', return_value=nullcontext({})),
              patch.object(web, '_stage_removed_reports'),
              patch.object(web.sqlmigrate, 'restore', return_value=stats),
              patch.object(web.accounts, 'record_event')):
            for replace in ('', 'on'):
                with self.subTest(replace=replace):
                    with web.jobs_lock:
                        web.jobs['abcdef'] = {'owner': 'admin', 'status': 'done'}
                    response = self.client.post(
                        '/admin/migration/import',
                        data={'replace': replace, 'confirm_login': 'admin',
                              'dump': (io.BytesIO(b'-- dump'), 'backup.sql')},
                        headers=self.headers,
                    )
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(web.jobs, {})

    def test_empty_exported_fingerprint_base_can_be_imported(self):
        admin = _user(role='admin')
        content = b'{"count":0,"entries":{}}'
        with (self._as(admin), patch.object(web.accounts, 'record_event') as record):
            response = self.client.post('/admin/migration/import', headers=self.headers,
                                        data={'dump': (io.BytesIO(content), 'base.json')})
        self.assertEqual(response.status_code, 302)
        record.assert_called_once()
        with self.client.session_transaction() as session:
            self.assertIn('отпечатков: 0', session['_flashes'][-1][1])

    def test_explicit_api_key_takes_precedence_over_browser_session(self):
        admin = _user(login='admin', role='admin', delete_all=True, use_api=True)
        with (self._as(_user()),
              patch.object(web.accounts, 'user_by_api_key', return_value=admin),
              patch.object(web.job_store, 'load_all', return_value={}),
              patch.object(web.job_store, 'clear', return_value=[]) as clear_jobs,
              patch('checker.memory_store.clear_store', return_value=0)):
            response = self.client.delete(
                '/api/v1/jobs?scope=all&confirm_login=admin',
                headers={'X-API-Key': 'admin-key'},
            )

        self.assertEqual(response.status_code, 200)
        clear_jobs.assert_called_once_with(None)

    def test_invalid_explicit_api_key_does_not_fall_back_to_session(self):
        with (self._as(_user()),
              patch.object(web.accounts, 'user_by_api_key', return_value=None),
              patch.object(web.job_store, 'clear') as clear_jobs):
            response = self.client.delete(
                '/api/v1/jobs?scope=own',
                headers={'X-API-Key': 'invalid'},
            )

        self.assertEqual(response.status_code, 401)
        clear_jobs.assert_not_called()

    def test_session_cannot_follow_a_login_to_a_different_account(self):
        with self.client.session_transaction() as session:
            session['account_id'] = 'former-account'
        with self._as(_user()):
            response = self.client.get('/api/v1/jobs')
        self.assertEqual(response.status_code, 401)
        with self.client.session_transaction() as session:
            self.assertNotIn('login', session)

    def test_old_session_without_an_identity_requires_login_again(self):
        with self.client.session_transaction() as session:
            session.pop('account_id')
        with self._as(_user()):
            response = self.client.get('/api/v1/jobs')
        self.assertEqual(response.status_code, 401)

    def test_missing_api_credentials_return_json_401_before_csrf(self):
        anonymous = web.app.test_client()

        response = anonymous.delete('/api/v1/jobs?scope=own')

        self.assertEqual(response.status_code, 401)

    def test_full_restore_requires_global_permission_and_typed_login(self):
        admin = _user(login='admin', role='admin')
        with self.client.session_transaction() as session:
            session['login'] = 'admin'
            session['account_id'] = 'admin-test-id'
        with (self._as(admin),
              patch.object(web.sqlmigrate, 'restore') as restore):
            no_permission = self.client.post(
                '/admin/migration/import',
                data={'replace': 'on', 'confirm_login': 'admin'},
                headers=self.headers,
            )

        allowed = _user(login='admin', role='admin', delete_all=True)
        with (self._as(allowed),
              patch.object(web.sqlmigrate, 'restore') as restore_with_right):
            wrong_login = self.client.post(
                '/admin/migration/import',
                data={'replace': 'on', 'confirm_login': 'wrong'},
                headers=self.headers,
            )

        self.assertEqual(no_permission.status_code, 302)
        self.assertEqual(wrong_login.status_code, 302)
        restore.assert_not_called()
        restore_with_right.assert_not_called()

    def test_see_all_does_not_allow_deleting_one_foreign_job(self):
        foreign = {'owner': 'other', 'status': 'done'}
        with (self._as(_user(see_all=True)),
              patch.object(web, '_find_job', return_value=foreign),
              patch.object(web.job_store, 'delete') as delete_job):
            response = self.client.post(
                '/jobs/abcdef/delete', headers=self.headers)

        self.assertEqual(response.status_code, 403)
        delete_job.assert_not_called()

    def test_see_all_does_not_allow_stopping_foreign_job(self):
        foreign = {'owner': 'other', 'status': 'processing'}
        with (self._as(_user(see_all=True)),
              patch.object(web, '_find_job', return_value=foreign)):
            response = self.client.post(
                '/jobs/abcdef/cancel', headers=self.headers)

        self.assertEqual(response.status_code, 403)


class ReportStorageIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.reports = self.root / 'reports'
        self.reports.mkdir()
        self.enterContext(patch.object(web, 'REPORTS_DIR', self.reports))

    def test_report_files_follow_the_storage_commit_after_restart(self):
        old = {'abc123': ['teacher', 'old-id'], 'abcdef': ['teacher', 'old-id']}
        new = {'abc123': ['teacher', 'new-id']}
        for recovered in (old, new):
            with self.subTest(recovered=recovered):
                for jid in old:
                    (self.reports / f'{jid}.html').write_text(jid)
                stage = self.reports / '.alena-restore-test'
                stage.mkdir()
                web._stage_removed_reports(stage, old, new, replace=True)
                self.assertFalse(list(self.reports.glob('*.html')))
                with patch.object(web, '_report_owners', return_value=recovered):
                    web._finish_report_cleanup(stage)
                remaining = {p.stem for p in self.reports.glob('*.html')}
                self.assertEqual(remaining, set(old) if recovered == old else set())
                self.assertFalse(stage.exists())

    def test_publication_failure_rolls_back_fingerprints_and_hides_report(self):
        self.enterContext(patch.object(db, 'DB_ENABLED', False))
        self.enterContext(patch.object(accounts, 'USERS_PATH', self.root / 'users.json'))
        self.enterContext(patch.object(memory_store, 'STORE_PATH', self.root / 'store.json'))
        self.enterContext(patch.dict(web.jobs, {'abc123': {'status': 'processing'}}))
        work = {'filename': 'report.pdf', 'full_text': 'Исходный текст'}
        with (patch.object(job_store, 'save', side_effect=OSError('disk full')),
              self.assertRaises(OSError)):
            web._publish_job('abc123', 'teacher', '<p>Готово</p>', [work], {'status': 'done'})
        self.assertFalse(memory_store.load_store())
        self.assertEqual(web.jobs['abc123']['status'], 'processing')
        self.assertFalse(list(self.reports.iterdir()))

    def test_newly_imported_job_cannot_claim_a_leftover_html_file(self):
        orphan = self.reports / 'abcdef.html'
        orphan.write_text('Старый отчёт')
        stage = self.reports / '.alena-restore-test'
        stage.mkdir()
        owners = {'abcdef': ['teacher', 'new-id']}
        web._stage_removed_reports(stage, {}, owners, replace=False)
        self.assertFalse(orphan.exists())
        with patch.object(web, '_report_owners', return_value=owners):
            web._finish_report_cleanup(stage)
        self.assertFalse(orphan.exists())


class BatchHistoryTests(unittest.TestCase):
    def test_archive_copy_is_detected_when_its_author_is_in_the_batch(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(db, 'DB_ENABLED', False))
        self.enterContext(patch.object(accounts, 'USERS_PATH', root / 'users.json'))
        self.enterContext(patch.object(memory_store, 'STORE_PATH', root / 'store.json'))
        self.enterContext(patch.object(job_store, 'STORE_DIR', root / 'jobs'))
        self.enterContext(patch.object(job_store, 'STORE_PATH', root / 'jobs.json'))
        self.enterContext(patch.object(web, 'REPORTS_DIR', root))
        self.enterContext(patch.object(teams, 'visible_owners', return_value=['teacher']))
        self.enterContext(patch.object(web, 'jobs', {}))

        def pdf(prefix, picture=False):
            with pymupdf.open() as doc:
                page = doc.new_page()
                words = ' '.join(f'{prefix}{i}' for i in range(80))
                page.insert_textbox((40, 40, 550, 350), words)
                if picture:
                    with Image.new('RGB', (100, 100), 'blue') as img, io.BytesIO() as buf:
                        img.save(buf, format='PNG')
                        page.insert_image((40, 400, 140, 500), stream=buf.getvalue())
                return doc.tobytes()

        def process(job_id, files, use_memory=True):
            uploads = root / job_id
            uploads.mkdir()
            for name, data in files:
                (uploads / (name + '.pdf')).write_bytes(data)
            web.jobs[job_id] = {'status': 'processing', 'owner': 'teacher'}
            web._process_job(job_id, str(uploads), 0.6, 'teacher', use_memory=use_memory)
            self.assertNotIn(job_id, web.jobs)
            saved = job_store.get(job_id)
            self.assertEqual(saved['status'], 'done', saved.get('error'))

        ivanov = 'Иванов Иван Иванович'
        petrov = 'Петров Пётр Петрович'
        original = pdf('original', picture=True)
        with patch.object(memory_store, 'load_store') as read_archive:
            process('abc001', [(ivanov, original)], use_memory=False)
            read_archive.assert_not_called()
        process('abc002', [(ivanov, pdf('revised')), (petrov, original)])

        with patch.object(accounts, 'user_by_api_key', return_value=_user(use_api=True)):
            response = web.app.test_client().get('/api/v1/jobs/abc002',
                                                 headers={'X-API-Key': 'test'})
        self.assertEqual(response.status_code, 200)
        job = response.get_json()
        self.assertEqual((job['text_pairs'], job['img_pairs']), (1, 1))
        scores = {s['fio']: s['plag'] for s in job['summary']['students']}
        self.assertEqual(scores, {ivanov: 0, petrov: 100})
        for match in job['summary']['matches']:
            self.assertEqual((match['a_fio'], match['b_fio'], match['b_new']),
                             (petrov, ivanov, False))


class AnonymousHistoryTests(unittest.TestCase):
    TEXT = ' '.join(f'слово{i}пример' for i in range(45))

    @classmethod
    def _current(cls, path='/tmp/current.pdf', filename='otchet.pdf'):
        return {
            'path': path,
            'filename': filename,
            'student': {},
            'full_text': cls.TEXT,
            'images': [],
        }

    @classmethod
    def _historical(cls, filename='otchet.pdf'):
        normalized = normalize_text(cls.TEXT)
        return {
            'path': 'memory://teacher||v1',
            'filename': filename,
            'student': {},
            'full_text': '',
            'normalized_text': normalized,
            'text_hash': text_fingerprint(normalized),
            'is_historical': True,
            'precomputed_images': [],
        }

    def test_same_anonymous_file_is_not_compared_with_its_history(self):
        current = self._current()
        historical = self._historical()

        result = check_text_plagiarism([current, historical], threshold=0.6)

        self.assertEqual(result['pairs'], [])
        self.assertNotIn(historical['path'], result['matrix'][current['path']])

    def test_renamed_anonymous_copy_is_still_detected(self):
        result = check_text_plagiarism(
            [self._current(), self._historical(filename='copy.pdf')],
            threshold=0.6,
        )

        self.assertEqual(len(result['pairs']), 1)
        self.assertEqual(result['pairs'][0]['similarity'], 1.0)

    def test_two_anonymous_files_in_current_batch_are_still_compared(self):
        first = self._current(path='/tmp/first.pdf')
        second = self._current(path='/tmp/second.pdf')

        result = check_text_plagiarism([first, second], threshold=0.6)

        self.assertEqual(len(result['pairs']), 1)

    def test_group_without_name_is_not_a_student_identity(self):
        self.assertEqual(student_id({'group': 'ИС-21'}), NO_STUDENT)
        first = self._current(path='/tmp/first.pdf', filename='first.pdf')
        second = self._current(path='/tmp/second.pdf', filename='second.pdf')
        first['student'] = {'group': 'ИС-21'}
        second['student'] = {'group': 'ИС-21'}

        result = check_text_plagiarism([first, second], threshold=0.6)

        self.assertEqual(len(result['pairs']), 1)

    def test_group_only_same_file_uses_anonymous_history_rule(self):
        current = self._current()
        historical = self._historical()
        current['student'] = {'group': 'ИС-21'}
        historical['student'] = {'group': 'ИС-21'}

        result = check_text_plagiarism([current, historical], threshold=0.6)

        self.assertEqual(result['pairs'], [])

    def test_fingerprint_is_persisted_and_restored_for_old_entries(self):
        _, entry = memory_store._entry_for(self._current(), 'abcdef', 'teacher')
        expected = text_fingerprint(entry['normalized_text'])
        self.assertEqual(entry['text_hash'], expected)

        legacy = dict(entry)
        legacy.pop('text_hash')
        virtual = memory_store.to_virtual_report('teacher||v1', legacy)
        self.assertEqual(virtual['text_hash'], expected)

    def test_sql_dump_keeps_text_fingerprint(self):
        _, entry = memory_store._entry_for(self._current(), 'abcdef', 'teacher')
        entry['version'] = 1
        store = {'teacher||v1': entry}
        with (patch.object(accounts, 'load_users', return_value={}),
              patch.object(job_store, 'load_all', return_value={}),
              patch.object(memory_store, 'load_store', return_value=store),
              patch.object(accounts, 'recent_logins', return_value=[]),
              patch.object(accounts, 'get_settings', return_value={}),
              patch.object(teams, 'load_teams', return_value={})):
            parsed = sqlmigrate.parse(sqlmigrate.dump())

        self.assertEqual(
            parsed['fingerprints'][0]['text_hash'], entry['text_hash'])

    def test_same_anonymous_history_is_also_skipped_for_images(self):
        phash = imagehash.hex_to_hash('0' * 36)
        current = self._current()
        current['images'] = [{
            'page': 1,
            'hashes': [phash, phash, phash],
            'thumb': '',
        }]
        historical = self._historical()
        historical['precomputed_images'] = [{
            'page': 1,
            'hashes': [phash, phash, phash],
            'thumb': '',
        }]

        self.assertEqual(
            check_image_plagiarism([current, historical])['pairs'], [])

        historical['filename'] = 'copy.pdf'
        self.assertEqual(
            len(check_image_plagiarism([current, historical])['pairs']), 1)

    def test_anonymous_scan_uses_exact_image_fingerprint(self):
        phash = imagehash.hex_to_hash('0' * 36)
        current = self._current()
        current['full_text'] = ''
        current['images'] = [{
            'page': 1,
            'hashes': [phash, phash, phash],
            'thumb': '',
        }]
        historical = self._historical()
        historical['normalized_text'] = ''
        historical['text_hash'] = ''
        historical['precomputed_images'] = [{
            'page': 1,
            'hashes': [phash, phash, phash],
            'thumb': '',
        }]

        self.assertEqual(
            check_image_plagiarism([current, historical])['pairs'], [])

    def test_legacy_admin_dump_does_not_gain_delete_all(self):
        rows = {name: [] for name in sqlmigrate.TABLES}
        rows['users'] = [{
            'login': 'admin',
            'role': 'admin',
            'password_hash': 'hash',
            'perms': {},
        }]
        with patch.object(accounts, 'save_user') as save_user:
            sqlmigrate.restore(rows)

        restored = save_user.call_args.args[0]
        self.assertFalse(restored['perms']['delete_all'])


class ReportAnchorTests(unittest.TestCase):
    def test_cyrillic_and_long_names_receive_unique_ordinal_anchors(self):
        reports = [
            {'path': '/tmp/Иванов Иван Иванович.pdf'},
            {'path': '/tmp/Петров Пётр Петрович.pdf'},
            {'path': '/tmp/' + 'очень-длинное-имя-' * 3 + '.pdf'},
        ]

        anchors = _report_anchors(reports, 'abcdef1234')

        self.assertEqual(
            [_anchor(report, anchors) for report in reports],
            ['r_abcdef1234_1', 'r_abcdef1234_2', 'r_abcdef1234_3'],
        )

    def test_summary_links_resolve_to_distinct_cards(self):
        reports = [
            {
                'path': '/tmp/Иванов.pdf',
                'filename': 'Иванов.pdf',
                'student': {'name': 'Иванов Иван Иванович', 'group': ''},
                'gost_results': [],
                'error': 'test error',
            },
            {
                'path': '/tmp/Петров.pdf',
                'filename': 'Петров.pdf',
                'student': {'name': 'Петров Пётр Петрович', 'group': ''},
                'gost_results': [],
                'error': 'test error',
            },
        ]
        p1, p2 = (report['path'] for report in reports)
        text = {
            'matrix': {p1: {p1: 1.0, p2: 0.9}, p2: {p1: 0.9, p2: 1.0}},
            'pairs': [],
            'no_text': [],
        }

        html = generate_html_report(
            reports, [], text, {'pairs': []}, threshold=0.6,
            job_id='abcdef1234',
        )

        for index in (1, 2):
            anchor = f'r_abcdef1234_{index}'
            self.assertEqual(html.count(f'id="{anchor}"'), 1)
            self.assertIn(f'href="#{anchor}"', html)

    def test_zero_threshold_handles_zero_similarity_to_history(self):
        current = {
            'path': '/tmp/current.pdf',
            'filename': 'current.pdf',
            'student': {},
            'gost_results': [],
            'full_text': '',
            'images': [],
            'error': None,
        }
        historical = {
            'path': 'memory://other',
            'filename': 'other.pdf',
            'student': {},
            'is_historical': True,
            'historical_version': 1,
            'historical_date': '01.01.2026 00:00',
            'precomputed_images': [],
        }
        p1, p2 = current['path'], historical['path']
        text = {
            'matrix': {p1: {p1: 1.0, p2: 0.0},
                       p2: {p1: 0.0, p2: 1.0}},
            'pairs': [],
            'no_text': [],
        }

        html = generate_html_report(
            [current], [historical], text, {'pairs': []},
            threshold=0.0, job_id='abcdef1234',
        )

        self.assertIn('id="r_abcdef1234_1"', html)


class JobDateTests(unittest.TestCase):
    def test_retention_expires_a_job_one_minute_past_the_limit(self):
        now = datetime(2026, 2, 1, 12, 0)
        records = {key: {'created_at': (now - age).strftime('%d.%m.%Y %H:%M')}
                   for key, age in [('expired', timedelta(days=30, minutes=1)),
                                    ('keep', timedelta(days=30))]}
        with (patch.object(job_store, 'datetime', wraps=datetime) as clock,
              patch.object(job_store, 'load_all', return_value=records)):
            clock.now.return_value = now
            self.assertEqual(job_store.expired(30), ['expired'])

    def test_parser_returns_real_datetime_or_none(self):
        parsed = job_store.parse_created_at('01.12.2026 08:15')
        self.assertEqual((parsed.year, parsed.month, parsed.day), (2026, 12, 1))
        self.assertIsNone(job_store.parse_created_at(None))
        self.assertIsNone(job_store.parse_created_at('31.02.2026 08:15'))
        self.assertIsNone(job_store.parse_created_at('1.2.2026 8:15'))
        self.assertIsNone(job_store.parse_created_at('01.02.2026'))
        self.assertEqual(
            job_store.parse_created_at('01.01.0100 00:00').year, 100)
        self.assertIsNone(job_store.parse_created_at('01.01.0000 00:00'))

    def test_overview_recent_is_chronological_across_months(self):
        records = {
            'jan': {'created_at': '31.01.2026 12:00', 'status': 'done'},
            'feb': {'created_at': '02.02.2026 12:00', 'status': 'done'},
            'dec': {'created_at': '01.12.2026 12:00', 'status': 'done'},
            'bad': {'created_at': None, 'status': 'done'},
        }

        with patch.object(web, '_all_jobs', return_value=records):
            stats = web._overview_stats(_user())

        self.assertEqual(
            [job['job_id'] for job in stats['recent'][:3]],
            ['dec', 'feb', 'jan'],
        )
        self.assertEqual(
            [day for day, _ in stats['series']],
            ['31.01.2026', '02.02.2026', '01.12.2026'],
        )

    def test_retention_ignores_bad_timestamp_without_aborting(self):
        records = {
            'old': {'created_at': '01.01.2000 00:00'},
            'bad': {'created_at': None},
        }
        with patch.object(job_store, 'load_all', return_value=records):
            self.assertEqual(job_store.expired(1), ['old'])


class ChecksCountTests(unittest.TestCase):
    """Число проверок у пункта «Проверки»: приходит с любой страницей и
    считается хранилищем, а не чтением всей истории."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        root = Path(self._dir.name)
        for item in (patch.object(db, 'DB_ENABLED', False),
                     patch.object(job_store, 'STORE_PATH', root / 'jobs.json'),
                     patch.object(job_store, 'STORE_DIR', root / 'jobs')):
            item.start()
            self.addCleanup(item.stop)

    def test_count_is_scoped_to_owner_and_follows_deletion(self):
        job_store.save('one', {'owner': 'teacher', 'status': 'done'})
        job_store.save('two', {'owner': 'teacher', 'status': 'done'})
        job_store.save('three', {'owner': 'other', 'status': 'done'})

        self.assertEqual(job_store.count(), 3)
        self.assertEqual(job_store.count('teacher'), 2)
        self.assertEqual(job_store.count('other'), 1)

        job_store.delete('two')
        # Запомненный владелец не должен пережить саму проверку.
        self.assertEqual(job_store.count('teacher'), 1)
        self.assertEqual(job_store.count(), 2)

    def test_repeated_count_does_not_reread_the_checks(self):
        """Владелец у проверки не меняется, поэтому её файл читается один раз –
        иначе значок в меню обходил бы всю историю на каждой странице."""
        job_store.save('one', {'owner': 'teacher', 'status': 'done'})
        self.assertEqual(job_store.count('teacher'), 1)

        real_read = job_store.jsonstore.read_json
        reads = []

        def counting_read(path, default=None):
            reads.append(Path(path).name)
            return real_read(path, default)

        with patch.object(job_store.jsonstore, 'read_json', counting_read):
            self.assertEqual(job_store.count('teacher'), 1)
        self.assertNotIn('one.json', reads)

        job_store.save('two', {'owner': 'teacher', 'status': 'done'})
        self.assertEqual(job_store.count('teacher'), 2)

    def test_badge_is_rendered_on_pages_other_than_checks(self):
        web.app.config.update(TESTING=True)
        client = web.app.test_client()
        with client.session_transaction() as session:
            session['login'] = 'teacher'
            session['account_id'] = 'teacher-test-id'

        with (patch.object(web.accounts, 'get_user', return_value=_user()),
              patch.object(web.job_store, 'count', return_value=7) as count,
              patch.object(web.job_store, 'load_all',
                           side_effect=AssertionError('история прочитана целиком')),
              patch('checker.memory_store.load_store', return_value={})):
            page = client.get('/base')

        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="nav-count"', html)
        self.assertIn('>7</span>', html)
        count.assert_called_once_with('teacher')

    def test_badge_is_hidden_when_there_are_no_checks(self):
        web.app.config.update(TESTING=True)
        client = web.app.test_client()
        with client.session_transaction() as session:
            session['login'] = 'teacher'
            session['account_id'] = 'teacher-test-id'

        with (patch.object(web.accounts, 'get_user', return_value=_user()),
              patch.object(web.job_store, 'count', return_value=0),
              patch('checker.memory_store.load_store', return_value={})):
            html = client.get('/base').get_data(as_text=True)

        self.assertIn('id="nav-count" hidden', html)


if __name__ == '__main__':
    unittest.main()
