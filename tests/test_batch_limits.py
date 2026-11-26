import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from checker import reporter
from tests.test_regressions import web


class ArchiveBatchLimitTests(unittest.TestCase):
    def make_upload(self, loose=False):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for i in range(2):
            with zipfile.ZipFile(root / f'archive{i}.zip', 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(f'folder{i}/report.pdf', b'x' * 1000)
                zf.writestr('readme.txt', b'ignored')
        if loose:
            (root / 'report.pdf').write_bytes(b'y' * 1000)
        return root

    def test_launch_rejects_combined_archives_and_cleans_upload(self):
        for files, size, loose in [(1, 2000, False), (2, 1999, False),
                                   (2, 3000, True), (3, 2999, True)]:
            with self.subTest(files=files, size=size, loose=loose):
                root = self.make_upload(loose)
                with (patch.object(web, 'ZIP_MAX_FILES', files),
                      patch.object(web, 'ZIP_MAX_TOTAL', size)):
                    job_id, _error, status = web._launch(str(root), 0.6, {'login': 'test'})
                self.assertIsNone(job_id)
                self.assertEqual(status, 413)
                self.assertFalse(root.exists())

    def test_worker_unpacks_boundary_batch_preserving_same_named_files(self):
        root = self.make_upload(loose=True)
        with (patch.object(web, 'ZIP_MAX_FILES', 3),
              patch.object(web, 'ZIP_MAX_TOTAL', 3000)):
            self.assertEqual(web._prepare_batch(str(root)), 3)
            self.assertEqual(len(list(root.glob('*.zip'))), 2)
            paths, _, failures = web._collect_docs('abc123', str(root))
        self.assertFalse(failures)
        self.assertEqual({p.name for p in root.iterdir()},
                         {'report.pdf', 'report_2.pdf', 'report_3.pdf'})
        self.assertCountEqual([Path(p).read_bytes() for p in paths],
                              [b'x' * 1000, b'x' * 1000, b'y' * 1000])


class MatrixReportLimitsTests(unittest.TestCase):
    def test_small_matrix_and_limit_boundary(self):
        reports = [{'path': f'curr{i}'} for i in range(reporter.MATRIX_MAX_REPORTS)]
        matrix = {r['path']: {r['path']: 1.0} for r in reports}
        rendered = reporter._render_matrix(reports, [], {'matrix': matrix}, 0.6)
        self.assertIn('<table class="matrix-table">', rendered)
        self.assertEqual(rendered.count('<td '), len(reports) ** 2)

    def test_large_batch_has_bounded_matrix_output(self):
        reports = [{'path': f'curr{i}'} for i in range(5000)]
        # The fallback needs only the number of works, even for dense matrices.
        with patch.object(reporter, '_short_name', side_effect=AssertionError('unexpected header')):
            rendered = reporter._render_matrix(reports, [], {'matrix': {'curr0': {}}}, 0.6)
        self.assertIn('5000', rendered)
        self.assertNotIn('<td ', rendered)

    def test_archived_sources_still_reach_summary_and_cards(self):
        new = {'path': 'current'}
        history = [{'path': f'old{i}', 'is_historical': True, 'filename': f'archive-{i}.pdf'}
                   for i in range(reporter.MATRIX_MAX_REPORTS)]
        scores = {r['path']: 0.5 for r in history}
        scores[history[-1]['path']] = 0.75
        matrix = {path: {'current': score} for path, score in scores.items()}
        matrix['current'] = scores

        html = reporter.generate_html_report([new], history, {'matrix': matrix}, {}, 0.6)
        self.assertNotIn('<table class="matrix-table">', html)
        self.assertIn(f'Матрица для {len(history) + 1} работ', html)
        self.assertIn('href="#report-summary"', html)
        self.assertIn('id="report-summary"', html)
        self.assertGreaterEqual(html.count(Path(history[-1]['filename']).stem), 2)
        self.assertIn('75%', html)
