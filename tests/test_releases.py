import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError

from checker import accounts, releases
from tests.test_regressions import _user, web


class ReleaseTests(TestCase):
    def test_release_history_is_newest_first(self):
        versions = [releases.version_parts(entry['version']) for entry in releases.CHANGELOG]
        self.assertEqual(versions, sorted(set(versions), reverse=True))

    def test_notice_is_remembered_per_account_across_sessions(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(web.db, 'DB_ENABLED', False))
        self.enterContext(patch.object(accounts, 'USERS_PATH', root / 'users.json'))
        self.enterContext(patch.object(web.job_store, 'count', return_value=0))
        accounts.save_user(_user())
        accounts.save_user(_user('other'))

        def visit(login):
            client = web.app.test_client()
            with client.session_transaction() as session:
                session.update(login=login, account_id=login + '-test-id', csrf='test')
            return client

        client = visit('teacher')
        marker = b'id="release-notice"'
        self.assertIn(marker, client.get('/changelog').data)
        self.assertEqual(client.post(f'/changelog/{releases.APP_VERSION}/seen',
                                     data={'csrf_token': 'test'}).status_code, 200)
        self.assertNotIn(marker, visit('teacher').get('/changelog').data)
        self.assertIn(marker, visit('other').get('/changelog').data)
        with patch.object(releases, 'CHANGELOG', [dict(version='2.0.0', changes=['Новое'])]):
            self.assertIn(marker, client.get('/changelog').data)

    def test_admin_checks_latest_release(self):
        client = web.app.test_client()
        with client.session_transaction() as session:
            session.update(login='teacher', account_id='teacher-test-id', csrf='test')
        url = '/admin/settings/check-update'
        headers = {'X-CSRF-Token': 'test'}
        fetch = self.enterContext(patch.object(releases, 'urlopen'))
        user = self.enterContext(patch.object(accounts, 'get_user', return_value=_user()))
        with patch.object(web.job_store, 'count', return_value=0):
            page = client.get('/changelog')
            self.assertEqual(page.status_code, 200)
            self.assertIn(releases.CHANGELOG[0]['changes'][0], page.get_data(as_text=True))
            self.assertIn('href="/changelog"', page.get_data(as_text=True))
        self.assertEqual(client.post(url, headers=headers).status_code, 403)
        user.return_value = _user(role='admin')
        self.assertGreaterEqual(client.post(url).status_code, 400)
        fetch.assert_not_called()
        with patch.object(releases, 'APP_VERSION', '1.9.0'):
            for tag, newer in [('v1.10.0', True), ('v1.9.0', False), ('v1.8.9', False)]:
                fetch.return_value = io.BytesIO(json.dumps({'tag_name': tag}).encode())
                response = client.post(url, headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual('url' in response.json, newer)
            fetch.return_value = io.BytesIO(b'{}')
            self.assertEqual(client.post(url, headers=headers).status_code, 502)
            fetch.side_effect = HTTPError('', 404, '', {}, None)
            self.assertIn('не найден', client.post(url, headers=headers).json['message'])
            fetch.side_effect = TimeoutError()
            self.assertEqual(client.post(url, headers=headers).status_code, 502)
