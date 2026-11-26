"""Persistent check history.

Wraps the PostgreSQL `jobs` table and falls back to JSON files, so history –
and with it the record of who owns which check – survives a restart in both
deployments. Ownership matters: a report is served only to its owner (or to an
account allowed to see everything), and that decision needs the record.
"""

import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from checker import db, jsonstore

try:
    import fcntl
except ImportError:  # Windows local development; production image is Linux.
    fcntl = None

STORE_PATH = Path(__file__).parent.parent / 'memory' / 'jobs.json'
STORE_DIR = Path(__file__).parent.parent / 'memory' / 'jobs'
REPORTS_DIR = Path(__file__).parent.parent / 'reports'
_lock = jsonstore.lock
_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
# Путь файла проверки → её владелец: см. `_owners_unlocked`. Ключ – путь, а не
# id: в тестах каталог хранилища подменяется, и одинаковые id из разных
# каталогов не должны считаться одной проверкой.
_owner_cache = {}

_STAMP = '%d.%m.%Y %H:%M'
_STAMP_RE = re.compile(r'^\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}$')


def parse_created_at(value):
    """Parse a persisted job timestamp, or return None for legacy bad data."""
    if not isinstance(value, str) or not _STAMP_RE.fullmatch(value):
        return None
    try:
        return datetime.strptime(value, _STAMP)
    except ValueError:
        return None


def _job_path(job_id: str) -> Path:
    job_id = str(job_id or '')
    if not _ID_RE.fullmatch(job_id):
        raise ValueError('invalid job id')
    return STORE_DIR / f'{job_id}.json'


@contextmanager
def _process_lock():
    """Serialize JSON migration and writes across server processes."""
    path = STORE_DIR.parent / 'jobs.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _migrate_legacy_unlocked() -> None:
    """Move the old monolithic jobs.json to one atomic file per job."""
    if not STORE_PATH.exists():
        return
    legacy = jsonstore.read_json(STORE_PATH, None)
    if not isinstance(legacy, dict):
        return

    remaining = {}
    for job_id, data in legacy.items():
        try:
            path = _job_path(job_id)
        except ValueError:
            remaining[job_id] = data
            continue
        existing = jsonstore.read_json(path, None)
        if not isinstance(existing, dict):
            jsonstore.write_json(path, data)

    if remaining:
        jsonstore.write_json(STORE_PATH, remaining)
    else:
        jsonstore.unlink(STORE_PATH, missing_ok=True)


def _read_all_unlocked() -> dict:
    _migrate_legacy_unlocked()
    store = jsonstore.read_json(STORE_PATH, {})
    if not isinstance(store, dict):
        store = {}
    if STORE_DIR.exists():
        for path in STORE_DIR.glob('*.json'):
            data = jsonstore.read_json(path, None)
            if isinstance(data, dict):
                store[path.stem] = data
    return store


def _owners_unlocked() -> dict:
    """id проверки → её владелец.

    Владельца читаем повторно, только если файл проверки изменился.
    Так восстановление из дампа обновляет счётчики и права доступа.
    """
    _migrate_legacy_unlocked()
    owners = {}
    legacy = jsonstore.read_json(STORE_PATH, {})
    if isinstance(legacy, dict):
        for job_id, data in legacy.items():
            if isinstance(data, dict):
                owners[job_id] = data.get('owner', '')
    fresh = {}
    if STORE_DIR.exists():
        for path in STORE_DIR.glob('*.json'):
            key = str(path)
            stat = path.stat()
            stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
            cached = _owner_cache.get(key)
            if cached is None or cached[0] != stamp:
                data = jsonstore.read_json(path, None)
                if not isinstance(data, dict):
                    continue
                owner = data.get('owner', '')
            else:
                owner = cached[1]
            fresh[key] = (stamp, owner)
            owners[path.stem] = owner
    # Удалённые проверки не должны копиться в памяти процесса.
    _owner_cache.clear()
    _owner_cache.update(fresh)
    return owners


def save(job_id: str, data: dict) -> None:
    path = _job_path(job_id)
    if db.DB_ENABLED:
        db.jobs_save(job_id, data)
        return
    with _lock, _process_lock():
        _migrate_legacy_unlocked()
        jsonstore.write_json(path, data)
        _owner_cache.pop(str(path), None)


def load_all(owner=None) -> dict:
    if db.DB_ENABLED:
        return db.jobs_load_all(owner)
    with _lock, _process_lock():
        store = _read_all_unlocked()
    if owner is None:
        return store
    return {k: v for k, v in store.items() if v.get('owner', '') == owner}


def count(owner=None) -> int:
    """Сколько проверок в истории – всех или одного владельца.

    Отдельно от `load_all`: для одного числа в меню незачем читать и разбирать
    каждую проверку целиком.
    """
    if db.DB_ENABLED:
        return db.jobs_count(owner)
    with _lock, _process_lock():
        owners = _owners_unlocked()
    if owner is None:
        return len(owners)
    return sum(1 for value in owners.values() if value == owner)


def owners() -> dict:
    """Current ownership of report files, without loading all job summaries."""
    if db.DB_ENABLED:
        return db.job_owners()
    with _lock, _process_lock():
        return _owners_unlocked()


def get(job_id: str):
    if db.DB_ENABLED:
        return db.jobs_get(job_id)
    try:
        path = _job_path(job_id)
    except ValueError:
        return None
    with _lock, _process_lock():
        _migrate_legacy_unlocked()
        data = jsonstore.read_json(path, None)
    return data if isinstance(data, dict) else None


def delete(job_id: str) -> bool:
    if db.DB_ENABLED:
        return db.jobs_delete(job_id)
    try:
        path = _job_path(job_id)
    except ValueError:
        return False
    with _lock, _process_lock():
        _migrate_legacy_unlocked()
        if not path.exists():
            return False
        jsonstore.unlink(path)
        _owner_cache.pop(str(path), None)
        return True


def clear(owner=None) -> list:
    """Delete history. Returns the ids removed, so callers can drop the
    matching report files."""
    if db.DB_ENABLED:
        ids = list(load_all(owner).keys())
        db.jobs_clear(owner)
        return ids
    with _lock, _process_lock():
        store = _read_all_unlocked()
        ids = [jid for jid, data in store.items()
               if owner is None or data.get('owner', '') == owner]
        for job_id in ids:
            try:
                path = _job_path(job_id)
                jsonstore.unlink(path, missing_ok=True)
                _owner_cache.pop(str(path), None)
            except ValueError:
                pass
        legacy = jsonstore.read_json(STORE_PATH, {})
        if isinstance(legacy, dict):
            for job_id in ids:
                legacy.pop(job_id, None)
            if legacy:
                jsonstore.write_json(STORE_PATH, legacy)
            else:
                jsonstore.unlink(STORE_PATH, missing_ok=True)
    return ids


def expired(days: int) -> list:
    """Ids of checks older than `days`. `days <= 0` means keep forever."""
    if days <= 0:
        return []
    now = datetime.now()
    stale = []
    for jid, data in load_all().items():
        created = parse_created_at(data.get('created_at'))
        if created is None:
            continue
        age = now - created
        if age > timedelta(days=days):
            stale.append(jid)
    return stale
