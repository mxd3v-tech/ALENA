import base64
import io
import hashlib
import re
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from checker import db, memory_store
from checker.reporter import generate_html_report, pdf_resource_fetcher


class AnonymousVersionsTests(unittest.TestCase):
    def setUp(self):
        self.root = TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.enterContext(patch.object(db, 'DB_ENABLED', False))
        self.enterContext(patch.object(memory_store, 'STORE_PATH',
                                       Path(self.root.name) / 'store.json'))

    def test_same_filename_in_two_unidentified_works_is_not_an_identity(self):
        a = {'filename': 'report.pdf', 'full_text': 'Первая работа'}
        b = dict(a, full_text='Другая работа')
        c = dict(a, student={'group': 'Г-2'})
        self.assertEqual(memory_store.add_reports([a, b, c, a], 'job', 'teacher'),
                         [1, 1, 1, 2])

    def test_legacy_entries_keep_public_keys_and_receive_their_own_versions(self):
        a = {'filename': 'A.pdf', 'full_text': 'Анонимный отчёт'}
        b = dict(a, filename='B.pdf')
        store = {}
        for i, work in enumerate((a, b, a), 1):
            _, entry = memory_store._entry_for(work, 'old', 'teacher')
            store[f'teacher|||v{i}'] = dict(entry, key_base='teacher||', version=i)
        memory_store.save_store(store)
        memory_store.migrate_anonymous_versions()
        memory_store.migrate_anonymous_versions()
        migrated = memory_store.load_store('teacher')
        self.assertEqual(list(migrated), list(store))
        self.assertEqual([migrated[k]['version'] for k in store], [1, 1, 2])
        self.assertEqual(memory_store.add_reports([b, a], 'new', 'teacher'), [2, 3])
        self.assertTrue(memory_store.delete_entry('teacher|||v2'))


class ReportResourcesTests(unittest.TestCase):
    @staticmethod
    def thumbnail(size=(96, 60)):
        with Image.new('RGB', size) as img, io.BytesIO() as out:
            img.save(out, 'JPEG')
            return 'data:image/jpeg;base64,' + base64.b64encode(out.getvalue()).decode()

    def test_only_stored_raster_thumbnails_reach_a_virtual_report(self):
        good = self.thumbnail()
        self.assertTrue(memory_store.valid_preview(good))
        entries = [good, 'relative.jpg', 'data:image/jpeg;base64,AAAA',
                   self.thumbnail((250, 80))]
        report = memory_store.to_virtual_report('test', {
            'image_data': [{'page': 1, 'hashes': ['00'], 'thumb': t} for t in entries],
        })
        self.assertEqual([i['thumb'] for i in report['precomputed_images']],
                         [good, '', '', ''])

    def test_labels_in_imported_metadata_are_literal_html_text(self):
        current = {'path': 'one', 'filename': 'one.pdf', 'student': {},
                   'images': [], 'full_text': 'Current work', 'gost_results': []}
        historical = memory_store.to_virtual_report(
            'two', {'filename': 'two.pdf', 'added_at': '<date>', 'version': '<edition>'})
        a, b = current['path'], historical['path']
        matrix = {'matrix': {a: {b: 1.0}, b: {a: 1.0}}, 'pairs': []}
        pair = {'report1': a, 'report2': b, 'page1': 1, 'page2': '<page>',
                'img1': '', 'img2': 'external-preview.jpg', 'distance': 0}
        out = generate_html_report([current], [historical], matrix, {'pairs': [pair]}, 0.6)
        for marker in ('date', 'edition', 'page'):
            self.assertIn(f'&lt;{marker}&gt;', out)
            self.assertNotIn(f'<{marker}>', out)
        self.assertNotIn('src="external-preview.jpg"', out)

    def test_pdf_fetcher_reads_embedded_thumbnail_only(self):
        try:
            fetch = pdf_resource_fetcher()
        except (ImportError, OSError):
            self.skipTest('WeasyPrint libraries are unavailable')
        url = self.thumbnail()
        response = fetch(url)
        if isinstance(response, dict):  # WeasyPrint 60
            stream = response['file_obj']
        else:
            stream = response
        self.addCleanup(stream.close)
        self.assertTrue(stream.read().startswith(b'\xff\xd8'))
        with self.assertRaises(ValueError):
            fetch('relative.jpg')


class ArchivedReportTests(unittest.TestCase):
    def setUp(self):
        from tests.test_regressions import web
        self.web = web
        directory = self.enterContext(TemporaryDirectory())
        self.path = Path(directory) / 'archived.html'
        self.enterContext(patch.object(web, 'REPORTS_DIR', self.path.parent))
        self.job = self.enterContext(patch.object(web, '_find_job', return_value={'status': 'done'}))
        self.enterContext(patch.object(web, '_may_view', return_value=True))
        self.enterContext(web.app.test_request_context())
        web.g.user = {'login': 'teacher'}

    def test_current_report_allows_exactly_the_two_shipped_scripts(self):
        html = generate_html_report([], [], {}, {})
        shipped = re.findall(r'<script>(.*?)</script>', html, re.S)
        self.assertEqual(len(shipped), 2)
        html = html.replace('<title>', '<script>/* unknown */</script><title>')
        self.path.write_text(html, encoding='utf-8')
        response = self.web._job_report('archived')
        self.addCleanup(response.close)
        response.direct_passthrough = False
        self.assertEqual(response.get_data(as_text=True), html)
        policy = response.headers['Content-Security-Policy']
        scripts = next(rule for rule in policy.split('; ') if rule.startswith('script-src '))
        self.assertCountEqual(scripts.split()[1:], [
            "'sha256-" + base64.b64encode(hashlib.sha256(s.encode()).digest()).decode() + "'"
            for s in shipped
        ])
        self.assertIn("form-action 'none'", policy)
        self.assertIn("connect-src 'none'", policy)
        self.assertIn("img-src data:", policy)

    def test_legacy_cards_remain_readable_with_unrecognized_controls(self):
        self.path.write_text('<div class="report-body">Обнаружено совпадение</div>', encoding='utf-8')
        response = self.web._job_report('archived')
        self.addCleanup(response.close)
        html = response.get_data(as_text=True)
        self.assertIn('Обнаружено совпадение', html)
        self.assertIn('.report-body,.section-body{display:block!important}', html)
        self.assertIn("default-src 'none'", response.headers['Content-Security-Policy'])

    def test_incomplete_jobs_cannot_open_or_export_partial_output(self):
        from werkzeug.exceptions import NotFound
        self.path.write_text('partially published file', encoding='utf-8')
        self.job.return_value = {'status': 'processing'}
        for method in (self.web._job_report, self.web._job_export):
            with self.assertRaises(NotFound):
                method('archived')
