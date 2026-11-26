import errno
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from checker import accounts, db, job_store, memory_store
from tests.test_regressions import web, _user


class JobLifecycleTests(TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.reports = self.root / 'reports'
        self.reports.mkdir()
        self.enterContext(patch.object(web, 'REPORTS_DIR', self.reports))
        self.enterContext(patch.object(web, 'jobs', {}))
        self.enterContext(patch.object(db, 'DB_ENABLED', False))
        self.enterContext(patch.object(job_store, 'STORE_DIR', self.root / 'jobs'))
        self.enterContext(patch.object(job_store, 'STORE_PATH', self.root / 'jobs.json'))
        self.enterContext(patch.object(accounts, 'USERS_PATH', self.root / 'users.json'))
        self.enterContext(patch.object(memory_store, 'STORE_PATH', self.root / 'fingerprints.json'))
        self.upload = self.root / 'upload'
        self.upload.mkdir()
        (self.upload / 'work.pdf').touch()

    def launch(self):
        return web._launch(str(self.upload), 0.6, _user(), use_memory=False)

    def test_initial_write_failure_releases_upload_and_live_state(self):
        with (patch.object(job_store.jsonstore, 'write_json',
                           side_effect=OSError(errno.ENOSPC, 'disk full')),
              patch.object(threading.Thread, 'start') as start):
            job_id, error, status = self.launch()
        self.assertIsNone(job_id)
        self.assertTrue(error)
        self.assertEqual(status, 500)
        start.assert_not_called()
        self.assertFalse(self.upload.exists())
        self.assertFalse(web.jobs)
        self.assertFalse(job_store.load_all())

    def test_failed_thread_start_releases_id_and_upload(self):
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError('thread limit')):
            self.assertEqual(self.launch()[2], 500)
        self.assertFalse(self.upload.exists())
        self.assertFalse(web.jobs)
        self.assertFalse(job_store.load_all())

    def test_collision_preserves_existing_record_and_orphan_report(self):
        existing, orphan, fresh = (s * 32 for s in 'abc')
        old = {'owner': 'another', 'status': 'done'}
        job_store.save(existing, old)
        report = self.reports / f'{orphan}.html'
        report.write_text('earlier report')
        with (patch.object(web.uuid, 'uuid4', side_effect=[
                SimpleNamespace(hex=key) for key in (existing, orphan, fresh)]),
              patch.object(threading.Thread, 'start')):
            self.assertEqual(self.launch(), (fresh, None, 200))
        self.assertEqual(job_store.get(existing), old)
        self.assertIsNone(job_store.get(orphan))
        self.assertEqual(report.read_text(), 'earlier report')
        self.assertEqual(job_store.get(fresh)['owner'], 'teacher')
        self.assertEqual(list(web.jobs), [fresh])

    def test_error_is_evicted_and_readable_from_storage(self):
        with patch.object(threading.Thread, 'start'):
            job_id, _, _ = self.launch()
        with patch.object(web, '_collect_docs', side_effect=ValueError('broken')):
            web._process_job(job_id, str(self.upload), 0.6, 'teacher')
        self.assertNotIn(job_id, web.jobs)
        self.assertEqual(web._find_job(job_id)['status'], 'error')
        self.assertFalse(self.upload.exists())

    def test_cancel_accepted_while_rendering_discards_result(self):
        with patch.object(threading.Thread, 'start'):
            job_id, _, _ = self.launch()
        rendering, resume = threading.Event(), threading.Event()

        def render(*args, **kwargs):
            rendering.set()
            if not resume.wait(5):
                raise RuntimeError('test renderer timed out')
            return '<html>ready</html>'

        work = {'path': 'work.pdf', 'filename': 'work.pdf', 'full_text': 'text'}
        with (patch.object(web, 'extract_report', return_value=work),
              patch.object(web, 'check_gost', return_value=[]),
              patch.object(web, 'generate_html_report', side_effect=render),
              patch.object(accounts, 'user_by_api_key', return_value=_user(use_api=True))):
            worker = threading.Thread(target=web._process_job,
                                      args=(job_id, str(self.upload), 0.6, 'teacher'),
                                      kwargs={'use_memory': False})
            worker.start()
            try:
                self.assertTrue(rendering.wait(5))
                client = web.app.test_client()
                url = f'/api/v1/jobs/{job_id}/cancel'
                response = client.post(url, headers={'X-API-Key': 'test'})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json['ok'])
            finally:
                resume.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(client.post(url, headers={'X-API-Key': 'test'}).status_code, 409)

        self.assertNotIn(job_id, web.jobs)
        self.assertEqual(job_store.get(job_id)['status'], 'cancelled')
        self.assertFalse(memory_store.load_store())
        self.assertFalse(list(self.reports.iterdir()))
        self.assertFalse(self.upload.exists())
