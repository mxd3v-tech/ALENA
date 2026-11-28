import errno
import io
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tests.test_regressions import _user, web


class UploadIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(web, 'TMP_ROOT', self.root))
        self.enterContext(patch.object(web, 'uploads', {}))
        self.enterContext(patch.object(web.accounts, 'get_user', return_value=_user()))
        self.enterContext(patch.object(web.accounts, 'get_settings', return_value={}))

    def post(self, url, data=None):
        client = web.app.test_client()
        with client.session_transaction() as session:
            session.update(login='teacher', account_id='teacher-test-id', csrf='test-csrf')
        return client.post(url, data=data, headers={'X-CSRF-Token': 'test-csrf'})

    def start(self):
        return self.post('/upload/start').get_json()['upload_id']

    def part(self, uid, offset, data):
        return self.post('/upload/part', {
            'upload_id': uid, 'idx': '0', 'name': 'report.pdf', 'offset': str(offset),
            'chunk': (io.BytesIO(data), 'part'),
        })

    def test_both_single_request_endpoints_apply_combined_file_limit(self):
        def upload(route, size):
            files = [(io.BytesIO(b'1234'), 'one.pdf'),
                     (io.BytesIO(b'x' * size), 'two.pdf')]
            return self.post(route, {'files': files})

        with (patch.object(web, 'UPLOAD_MAX_TOTAL', 7),
              patch.object(web, '_launch', return_value=('abcdef', None, 200)) as launch):
            for route in ('/upload', '/api/v1/jobs'):
                with self.subTest(route=route):
                    launch.reset_mock()
                    before = set(Path(self.root).iterdir())
                    self.assertEqual(upload(route, 4).status_code, 413)
                    self.assertEqual(set(Path(self.root).iterdir()), before)
                    launch.assert_not_called()
                    self.assertIn('job_id', upload(route, 3).get_json())
                    launch.assert_called_once()

    def test_overlapping_retry_verifies_existing_bytes_and_appends_missing_suffix(self):
        uid = self.start()
        path = Path(web.uploads[uid]['dir']) / 'report.pdf'
        with patch.object(web, 'UPLOAD_MAX_TOTAL', 6):
            self.assertEqual(self.part(uid, 0, b'abc').get_json()['bytes'], 3)
            self.assertEqual(self.part(uid, 1, b'XXde').status_code, 409)
            self.assertEqual(self.part(uid, 5, b'f').status_code, 409)
            self.assertEqual(self.part(uid, 1, b'bcdefg').status_code, 413)
            self.assertEqual(self.part(uid, 1, b'bcdef').get_json()['bytes'], 6)
            self.assertTrue(self.part(uid, 1, b'bcdef').get_json()['repeat'])
        self.assertEqual(path.read_bytes(), b'abcdef')

    def test_partial_write_rolls_back_before_retry(self):
        uid = self.start()
        path = Path(web.uploads[uid]['dir']) / 'report.pdf'
        self.part(uid, 0, b'abc')

        def disk_full(src, out, length):
            out.write(src.read(1))
            raise OSError(errno.ENOSPC, 'No space left on device')

        with patch.object(web.shutil, 'copyfileobj', side_effect=disk_full):
            self.assertEqual(self.part(uid, 3, b'def').status_code, 500)
        self.assertEqual(path.read_bytes(), b'abc')
        self.assertEqual(web.uploads[uid]['bytes'], 3)
        self.assertEqual(self.part(uid, 3, b'def').get_json()['bytes'], 6)
        self.assertEqual(path.read_bytes(), b'abcdef')

    def test_concurrent_retries_append_the_range_once(self):
        uid = self.start()
        path = Path(web.uploads[uid]['dir']) / 'report.pdf'
        self.part(uid, 0, b'abc')
        start = threading.Barrier(2)

        def retry():
            start.wait(5)
            return self.part(uid, 3, b'def').get_json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(retry) for _ in range(2)]
            self.assertCountEqual([f.result(5)['repeat'] for f in results], [False, True])
        self.assertEqual(path.read_bytes(), b'abcdef')
        self.assertEqual(web.uploads[uid]['bytes'], 6)

    def test_finish_waits_for_the_writer_and_exclusively_claims_the_directory(self):
        uid = self.start()
        slot = web.uploads[uid]
        path = Path(slot['dir']) / 'report.pdf'
        writing, release = threading.Event(), threading.Event()
        launched, finish = threading.Event(), threading.Event()
        copy = web.shutil.copyfileobj

        def slow_write(src, dst, length):
            writing.set()
            self.assertTrue(release.wait(5))
            copy(src, dst, length)

        def slow_launch(*args, **kwargs):
            self.assertEqual(path.read_bytes(), b'abc')
            launched.set()
            self.assertTrue(finish.wait(5))
            return 'abcdef', None, 200

        data = {'upload_id': uid}
        with (ThreadPoolExecutor(max_workers=2) as pool,
              patch.object(web.shutil, 'copyfileobj', side_effect=slow_write),
              patch.object(web, '_launch', side_effect=slow_launch) as launch):
            writer = pool.submit(self.part, uid, 0, b'abc')
            self.assertTrue(writing.wait(5))
            slot['at'] -= 2 * web.UPLOAD_TTL
            web._sweep_uploads()
            self.assertIn(uid, web.uploads)
            closing = pool.submit(self.post, '/upload/finish', data)
            release.set()
            self.assertEqual(writer.result(5).status_code, 200)
            self.assertTrue(launched.wait(5))
            try:
                for endpoint in ('/upload/finish', '/upload/cancel'):
                    self.assertEqual(self.post(endpoint, data).status_code, 404)
                self.assertEqual(self.part(uid, 3, b'def').status_code, 404)
                self.assertTrue(path.exists())
            finally:
                finish.set()
            self.assertEqual(closing.result(5).get_json(), {'job_id': 'abcdef'})
            launch.assert_called_once()
