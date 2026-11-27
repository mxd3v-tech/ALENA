import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class SuiteIsolationTests(unittest.TestCase):
    def test_import_ignores_external_database_and_temp_directory(self):
        # Discovery may import storage modules before it reaches the tests package.
        probe = textwrap.dedent("""
            import os
            from pathlib import Path
            from unittest.mock import patch

            from checker import db

            with patch.object(db, '_get_pool', side_effect=AssertionError('external DB')) as pool:
                from tests import DATA_DIR
                from tests.test_regressions import web
                pool.assert_not_called()

            assert not db.DB_ENABLED, 'PostgreSQL enabled after test module import'
            assert not db.DATABASE_URL, 'storage retained the external DSN'
            assert not os.environ.get('DATABASE_URL'), 'subprocesses inherit the external DSN'
            tmp_root = Path(os.environ['AU_TMP_DIR']).resolve()
            assert tmp_root.is_relative_to(DATA_DIR.resolve()), 'temp directory outside test sandbox'
            assert Path(web.TMP_ROOT).resolve() == tmp_root, 'app uses a different temp directory'
        """)
        with tempfile.TemporaryDirectory() as external:
            stale_upload = Path(external) / 'alena_up_existing'
            stale_upload.mkdir()
            os.utime(stale_upload, (1, 1))
            env = dict(os.environ, DATABASE_URL='postgresql://example.invalid/test',
                       AU_TMP_DIR=external, TMPDIR=external)
            run = subprocess.run(
                [sys.executable, '-c', probe],
                cwd=Path(__file__).resolve().parents[1], env=env,
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue(stale_upload.is_dir(), 'startup removed an external upload')
