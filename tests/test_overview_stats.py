import json
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from tests.test_regressions import web, _user


class OverviewTests(unittest.TestCase):
    def test_scored_works_and_partial_jobs_have_separate_totals(self):
        records = {
            'abc001': {'status': 'done', 'total': 5, 'summary': {
                'gost': 0, 'students': [{'gost': 0}, {'gost': None}], 'clean': 0,
                'matches_total': 620, 'matches': [{}] * 500}},
            'abc002': {'status': 'done', 'total': 1, 'summary': {
                'gost': 100, 'students': [{'gost': 100}], 'clean': 1, 'matches': [{}]}},
            'abc003': {'status': 'cancelled', 'total': 12, 'done_files': 2},
            'abc004': {'status': 'processing', 'total': 25},
            'abc005': {'status': 'done', 'total': 2, 'summary': {
                'gost': None, 'students': [{'gost': None}, {'gost': None}], 'clean': 0}},
        }
        with patch.object(web, '_all_jobs', return_value=records):
            stats = web._overview_stats(_user())
        self.assertEqual(stats['avg_gost'], 50)
        self.assertEqual(stats['scored'], 2)
        self.assertEqual(stats['clean_pct'], 50)
        self.assertEqual(stats['total_files'], 10)
        self.assertEqual(stats['flagged'], 621)

    def test_legacy_empty_criteria_are_recognised_without_dropping_measured_zero(self):
        def old_summary(fails):
            return {'gost': 0, 'clean': int(not fails),
                    'students': [{'gost': 0, 'fails': fails}]}
        saved = {'summary': old_summary([])}
        restored = web._public_job(saved)['summary']
        self.assertIsNone(restored['gost'])
        self.assertIsNone(restored['students'][0]['gost'])
        self.assertEqual(restored['clean'], 0)
        self.assertEqual(saved['summary'], old_summary([]))
        measured = old_summary(['F1'])
        self.assertEqual(web._public_job({'summary': measured})['summary'], dict(measured, groups=[]))

    def test_browser_receives_the_daily_series_and_only_existing_report_links(self):
        class Elements(HTMLParser):
            series = None

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if attrs.get('id') == 'line-chart':
                    self.series = json.loads(attrs['data-series'])
                if tag == 'a' and attrs.get('href', '').startswith('/report/'):
                    links.append(attrs['href'])

        day = '02.10.2026'
        records = {f'abc00{i}': dict(created_at=day + ' 12:00', status=s, total=1)
                   for i, s in enumerate(['done', 'processing', 'cancelled', 'error', 'done'])}
        with (tempfile.TemporaryDirectory() as dest,
              web.app.test_request_context('/overview'),
              patch.object(web, 'REPORTS_DIR', Path(dest)),
              patch.object(web, '_all_jobs', return_value=records),
              patch.object(web.job_store, 'count', return_value=len(records))):
            (Path(dest) / 'abc000.html').touch()
            web.g.user = _user()
            html = web.overview()
        links = []
        parser = Elements()
        parser.feed(html)
        self.assertEqual(parser.series, [[day, 5]])
        self.assertEqual(links, ['/report/abc000'])
