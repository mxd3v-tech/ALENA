from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from checker import accounts, db
from tests.test_regressions import _user, web


class SettingsInputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.inputs = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'input':
            self.inputs.append(dict(attrs))


class DefaultCriteriaTests(TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(db, 'DB_ENABLED', False))
        self.enterContext(patch.object(accounts, 'CONF_PATH', root / 'settings.json'))
        self.enterContext(patch.object(accounts, 'get_user', return_value=_user(role='admin')))
        self.enterContext(patch.object(web.job_store, 'count', return_value=0))
        self.enterContext(patch('checker.memory_store.load_store', return_value={}))
        self.client = web.app.test_client()
        with self.client.session_transaction() as session:
            session.update(login='teacher', account_id='teacher-test-id', csrf='test')

    def save(self, data):
        response = self.client.post('/admin/settings', data=data, headers={'X-CSRF-Token': 'test'})
        self.assertEqual(response.status_code, 302)

    def test_native_form_and_new_job_keep_subset_then_empty_selection(self):
        for selected in (['S1'], []):
            settings = SettingsInputs(self.client.get('/admin/settings').get_data(as_text=True)).inputs
            submitted = [i['value'] for i in settings if i.get('name') == 'gost_item'
                         and (i['type'] == 'hidden' or i['value'] in selected)]
            self.assertIn('', submitted)  # A native form can send an explicitly empty selection.
            self.save({'gost_item': submitted})
            self.save({'grade_scale': '5'})
            for path, cls in (('/new', 'gost-cb'), ('/admin/settings', 'gost-def')):
                inputs = SettingsInputs(self.client.get(path).get_data(as_text=True)).inputs
                self.assertEqual([i['value'] for i in inputs
                                  if i.get('class') == cls and 'checked' in i], selected)

    def test_upgrade_preserves_both_legacy_all_and_custom_selection(self):
        for old, expected in (([], web.ALL_CODES), (['S1'], ['S1'])):
            accounts.save_settings({'gost_schema': 4, 'default_gost': old})
            web._migrate_settings()
            web._migrate_settings()
            self.assertEqual(accounts.get_settings()['default_gost'], expected)


class ApiRoutingTests(TestCase):
    def test_old_summary_returns_all_available_groups(self):
        old = {'group': 'Г1', 'students': [{'group': 'Г1'}, {'group': 'Г2'}]}
        result = web._public_job({'summary': old})
        self.assertEqual(result['summary']['groups'], ['Г1', 'Г2'])
        self.assertNotIn('groups', old)

    def test_routing_errors_are_json_and_keep_http_headers(self):
        client = web.app.test_client()
        with patch.object(accounts, 'get_user', return_value=None):
            for method, target, code in [('get', '/api/v1/missing', 404),
                                         ('post', '/api/v1/health', 405)]:
                response = getattr(client, method)(target)
                self.assertEqual(response.status_code, code)
                self.assertEqual(response.json['status'], code)
                self.assertTrue(response.json['error'])
                if code == 405:
                    self.assertIn('GET', response.headers['Allow'])
            self.assertEqual(client.get('/missing').mimetype, 'text/html')
