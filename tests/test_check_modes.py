import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf
from PIL import Image

from checker import job_store, memory_store
from tests.test_regressions import _user, web


def submission(text):
    with pymupdf.open() as document, io.BytesIO() as picture:
        page = document.new_page()
        page.insert_textbox((40, 40, 540, 350), text)
        with Image.new('RGB', (100, 100), 'blue') as image:
            image.save(picture, format='PNG')
        page.insert_image((40, 400, 140, 500), stream=picture.getvalue())
        return document.tobytes()


class CheckModeTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(web, 'TMP_ROOT', str(root)))
        self.enterContext(patch.object(web, 'REPORTS_DIR', root))
        self.enterContext(patch.object(web, 'jobs', {}))
        self.enterContext(patch.object(web, 'uploads', {}))
        self.enterContext(patch.object(web.accounts, 'USERS_PATH', root / 'users.json'))
        self.enterContext(patch.object(job_store, 'STORE_DIR', root / 'jobs'))
        self.enterContext(patch.object(job_store, 'STORE_PATH', root / 'jobs.json'))
        self.enterContext(patch.object(memory_store, 'STORE_PATH', root / 'fingerprints.json'))
        user = _user(use_api=True)
        self.enterContext(patch.object(web.accounts, 'get_user', return_value=user))
        self.enterContext(patch.object(web.accounts, 'user_by_api_key', return_value=user))
        self.client = web.app.test_client()
        with self.client.session_transaction() as session:
            session.update(login='teacher', account_id=user['account_id'], csrf='test')
        self.headers = {'X-CSRF-Token': 'test'}

    def finished(self, job_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with web.jobs_lock:
                if job_id not in web.jobs:
                    break
            time.sleep(0.01)
        job = self.client.get('/status/' + job_id).get_json()
        self.assertEqual(job['status'], 'done', job.get('error'))
        return job

    def test_gost_only_archives_text_and_images_for_a_later_full_check(self):
        pdf = submission(' '.join(f'word{i}' for i in range(80)))
        upload = self.client.post('/upload/start', headers=self.headers).json['upload_id']
        for index, (name, content) in enumerate((
                ('Иванов Иван.pdf', pdf), ('Петров Пётр.pdf', pdf),
                ('Скан.pdf', submission('')))):
            part = self.client.post('/upload/part', headers=self.headers, data={
                'upload_id': upload, 'idx': str(index), 'name': name, 'offset': '0',
                'chunk': (io.BytesIO(content), 'part'),
            })
            self.assertEqual(part.status_code, 200)
        with (patch.object(memory_store, 'load_store') as archive,
              patch.object(web, 'check_text_plagiarism') as text_check,
              patch.object(web, 'check_image_plagiarism') as image_check):
            response = self.client.post('/upload/finish', headers=self.headers, data={
                'upload_id': upload, 'mode': 'gost_only', 'gost': 'S1', 'scale': '5',
                'threshold': '0', 'use_memory': '1',
            })
            first_id = response.json['job_id']
            job = self.finished(first_id)
            archive.assert_not_called()
            text_check.assert_not_called()
            image_check.assert_not_called()
        self.assertEqual(job['mode'], 'gost_only')
        for field in ('threshold', 'text_pairs', 'img_pairs'):
            self.assertIsNone(job[field])
        summary = job['summary']
        for field in ('threshold', 'plag', 'plag_flagged', 'matches_total'):
            self.assertIsNone(summary[field])
        self.assertEqual(summary['matches'], [])
        self.assertEqual(summary['no_text'], 1)
        for row in summary['students']:
            self.assertIsNone(row['plag'])
            self.assertIsNone(row['plag_flagged'])
            if row['no_text']:
                self.assertIsNone(row['grade'])
            else:
                self.assertEqual(row['grade']['scale'], 5)
                self.assertIsNotNone(row['gost'])
        with self.client.get('/report/' + first_id) as report:
            html = report.get_data(as_text=True)
        self.assertIn('Только ГОСТ', html)
        self.assertIn('Рекомендуемая оценка за оформление', html)
        self.assertIn('выставьте оценку вручную', html)
        for label in ('Матрица схожести', 'Заимствование текста', 'Заимствование изображений',
                      'Порог заимствования', 'Схожесть:', 'Дубли изображений'):
            self.assertNotIn(label, html)
        entries = memory_store.load_store('teacher')
        self.assertEqual(len(entries), 3)
        self.assertTrue(all(e['image_data'] for e in entries.values()))
        self.assertEqual(sum(bool(e['text_hash']) for e in entries.values()), 2)

        result = self.client.post('/api/v1/jobs', headers={'X-API-Key': 'test'}, data={
            'files': (io.BytesIO(pdf), 'Сидоров Сергей.pdf'), 'gost': 'S1'})
        self.assertEqual(result.status_code, 201)
        full = self.finished(result.json['job_id'])
        self.assertEqual(full['mode'], 'full')
        self.assertEqual(full['text_pairs'], 2)
        self.assertGreaterEqual(full['img_pairs'], 2)
        self.assertEqual(full['summary']['plag'], 100)
        self.assertTrue(all(not m['b_new'] for m in full['summary']['matches']))

    def test_single_request_uploads_use_the_requested_mode_and_default_criteria(self):
        pdf = submission(' '.join(['sample'] * 40))
        for url in ('/upload', '/api/v1/jobs'):
            with self.subTest(url=url):
                headers = self.headers if url == '/upload' else {'X-API-Key': 'test'}
                response = self.client.post(url, headers=headers, data={
                    'files': (io.BytesIO(pdf), 'report.pdf'), 'mode': 'gost_only'})
                self.assertEqual(response.status_code, 200 if url == '/upload' else 201)
                job = self.finished(response.json['job_id'])
                self.assertEqual(job['mode'], 'gost_only')
                self.assertIsNone(job['threshold'])
                self.assertIsNotNone(job['summary']['students'][0]['gost'])

    def test_invalid_modes_and_empty_gost_only_selection_reject_before_launch(self):
        upload = self.client.post('/upload/start', headers=self.headers).json['upload_id']
        with patch.object(web, '_launch') as launch:
            for url in ('/upload', '/upload/finish', '/api/v1/jobs'):
                for options in ({'mode': 'other'}, {'mode': 'gost_only', 'gost': ''},
                                {'mode': 'gost_only', 'gost': 'unknown'}):
                    with self.subTest(url=url, options=options):
                        res = self.client.post(url, headers=self.headers,
                                               data=dict(options, upload_id=upload))
                        self.assertEqual(res.status_code, 400)
                        self.assertTrue(res.json['error'])
            launch.assert_not_called()
        self.assertEqual(self.client.post('/upload/cancel', headers=self.headers,
                                          data={'upload_id': upload}).status_code, 200)
        self.assertEqual(web._public_job({'status': 'done'})['mode'], 'full')
