"""Набор тестов проекта. Запуск: ``python -m unittest discover -s tests -t .``

При импорте app.py приложение обновляет настройки и удаляет старые каталоги
загрузок ещё до первого setUp. Поэтому весь набор использует JSON-хранилища
и каталог загрузок внутри одного временного каталога.

Каталог живёт до конца процесса: history-тесты и тесты хранилищ пишут в него
по-настоящему, а изоляцию друг от друга обеспечивают сами.
"""

import atexit
import os
import tempfile
from pathlib import Path

_SANDBOX = tempfile.TemporaryDirectory(prefix='alena_tests_')
atexit.register(_SANDBOX.cleanup)

DATA_DIR = Path(_SANDBOX.name)
os.environ.pop('DATABASE_URL', None)
os.environ['AU_TMP_DIR'] = str(DATA_DIR / 'tmp')

from checker import accounts, db, job_store, memory_store, teams

# При обнаружении тестов checker.db мог уже прочитать окружение.
db.DATABASE_URL = ''
db.DB_ENABLED = False

accounts.DATA_DIR   = DATA_DIR
accounts.USERS_PATH = DATA_DIR / 'users.json'
accounts.LOG_PATH   = DATA_DIR / 'login_log.json'
accounts.CONF_PATH  = DATA_DIR / 'settings.json'

memory_store.STORE_PATH = DATA_DIR / 'store.json'
teams.STORE_PATH        = DATA_DIR / 'teams.json'
job_store.STORE_PATH    = DATA_DIR / 'jobs.json'
job_store.STORE_DIR     = DATA_DIR / 'jobs'   # рядом с ним лежит и jobs.lock
job_store.REPORTS_DIR   = DATA_DIR / 'reports'
