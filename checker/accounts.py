"""Accounts, permissions, login journal and system settings.

Two roles:
  admin   – sees every check and the whole fingerprint base, manages accounts,
            teacher groups and system settings.
  teacher – isolated by default: own checks, own fingerprint base. Plagiarism
            is searched only inside that base; группа преподавателей
            (checker/teams.py) расширяет её на коллег по группе.

Backed by PostgreSQL when DATABASE_URL is set, otherwise by JSON files next to
the fingerprint store (same fallback strategy as checker/memory_store.py).
"""

import hashlib
import os
import re
import secrets
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

from checker import db, jsonstore

DATA_DIR    = Path(__file__).parent.parent / 'memory'
USERS_PATH  = DATA_DIR / 'users.json'
LOG_PATH    = DATA_DIR / 'login_log.json'
CONF_PATH   = DATA_DIR / 'settings.json'

_lock = jsonstore.lock

ROLES = {'admin': 'Администратор', 'teacher': 'Преподаватель'}

STATES = {
    'active':  'Активен',
    'blocked': 'Заблокирован',
    'pending': 'Ждёт смены пароля',
}

# Permission flags, in the order the admin UI shows them.
# Названия описывают ровно то, что проверяется в коде. «run_checks» закрывает
# запуск новых проверок; уже готовые отчёты владелец открывает и без него –
# иначе снятие права отрезало бы преподавателя от собственной истории.
PERMISSIONS = [
    ('run_checks',  'Запускать новые проверки'),
    ('delete_own',  'Удалять свои проверки'),
    ('delete_all',  'Удалять данные всех преподавателей'),
    ('manage_base', 'Удалять записи из базы отпечатков'),
    ('see_all',     'Видеть проверки других преподавателей'),
    ('use_api',     'Пользоваться API по ключу'),
]

_DEFAULT_PERMS = {
    'admin':   {'run_checks': True, 'delete_own': True, 'delete_all': True,
                'manage_base': True, 'see_all': True, 'use_api': True},
    'teacher': {'run_checks': True, 'delete_own': True, 'delete_all': False,
                'manage_base': False, 'see_all': False, 'use_api': False},
}

DEFAULT_SETTINGS = {
    'default_threshold':       0.6,
    'default_gost':            [],      # empty list = all checks
    'gost_weights':            {},      # код критерия → 0…100; нет ключа = 100
    'grade_scale':             100,     # 100 = оценка в процентах, иначе баллы
    'retention_days':          0,       # 0 = keep reports forever
    'pw_min_len':              10,
    'pw_require_change_first': True,
    'pw_expire_days':          0,
    'lock_after_fails':        5,
    'lock_minutes':            15,
}

_STAMP = '%d.%m.%Y %H:%M'
_STAMP_SEC = '%d.%m.%Y %H:%M:%S'


def default_perms(role: str) -> dict:
    return dict(_DEFAULT_PERMS.get(role, _DEFAULT_PERMS['teacher']))


# JSON fallback helpers

def _read_json(path: Path, fallback):
    return jsonstore.read_json(path, fallback)


def _write_json(path: Path, value) -> None:
    jsonstore.write_json(path, value)


# Accounts

def load_users(include_deleted: bool = False) -> dict:
    users = db.users_load_all() if db.DB_ENABLED else _read_json(USERS_PATH, {})
    if include_deleted:
        return users
    return {k: v for k, v in users.items() if v.get('state') != 'deleted'}


def get_user(login: str):
    if not login:
        return None
    return load_users().get(login)


def _api_digest(key: str) -> str:
    return 'sha256:' + hashlib.sha256(key.encode('utf-8')).hexdigest()


def normalize_user(user: dict) -> dict:
    """Upgrade legacy records, including plaintext API credentials in old dumps."""
    user = dict(user)
    if not user.get('state'):
        user['state'] = 'pending' if user.get('must_change') else 'active'
    legacy = not user.get('account_id')
    if legacy:
        user['account_id'] = secrets.token_hex(16)
    key = user.get('api_key')
    if key and (legacy or not re.fullmatch(r'sha256:[0-9a-f]{64}', key)):
        user['api_key'] = _api_digest(key)
    if user.get('state') == 'deleted':
        user.update(password_hash='!', api_key=None, perms={}, role='teacher')
    return user


def save_user(user: dict) -> None:
    """Restore a complete, validated account record."""
    user = normalize_user(user)
    if db.DB_ENABLED:
        db.users_save(user)
        return
    with _lock:
        users = _read_json(USERS_PATH, {})
        users[user['login']] = user
        _write_json(USERS_PATH, users)


@contextmanager
def _edit_user(login: str):
    """Keep lookup, validation and update of a single account atomic."""
    if db.DB_ENABLED:
        with db.users_edit(login) as user:
            yield user
        return
    with _lock:
        users = _read_json(USERS_PATH, {})
        user = users.get(login)
        before = user.copy() if user else None
        yield user
        if user != before:
            _write_json(USERS_PATH, users)


def _insert_user(user: dict) -> bool:
    if db.DB_ENABLED:
        return db.users_save(user, new=True)
    with _lock:
        users = _read_json(USERS_PATH, {})
        if user['login'] in users:
            return False
        users[user['login']] = user
        _write_json(USERS_PATH, users)
        return True


def _archived(login: str, account_id: str) -> dict:
    return {'login': login, 'account_id': account_id, 'state': 'deleted',
            'fio': '', 'role': 'teacher', 'password_hash': '!', 'api_key': None,
            'perms': {}, 'must_change': False, 'fail_count': 0}


def reserve_logins(logins) -> None:
    """Preserve ownership when importing historical rows without user records."""
    with nullcontext() if db.DB_ENABLED else _lock:
        users = load_users(include_deleted=True)
        missing = {login for login in logins if login and login not in users}
        if not missing:
            return
        archived = {login: _archived(login, secrets.token_hex(16)) for login in missing}
        if db.DB_ENABLED:
            for user in archived.values():
                db.users_save(user, new=True)
        else:
            users.update(archived)
            _write_json(USERS_PATH, users)


def delete_user(login: str) -> bool:
    with _edit_user(login) as user:
        if not user or user.get('state') == 'deleted':
            return False
        archived = _archived(login, user['account_id'])
        user.clear()
        user.update(archived)
    return True


def login_in_history(login: str) -> bool:
    """Legacy versions physically removed accounts but kept their ownership."""
    if db.DB_ENABLED:
        return db.users_login_in_history(login)
    from checker import job_store, memory_store, teams
    return bool(job_store.count(login) or memory_store.load_store(login)
                or teams.teams_of(login)
                or any(e.get('reason') != 'учётной записи не существует'
                       for e in recent_logins(None, login)))


class LoginUnavailable(ValueError):
    """An existing account or historical record owns this login."""


def create_user(login: str, fio: str, password: str, role: str = 'teacher',
                email: str = '', perms=None, must_change: bool = True,
                api_key: str = '') -> dict:
    now = datetime.now().strftime(_STAMP)
    user = {
        'login':         login,
        'account_id':    secrets.token_hex(16),
        'fio':           fio,
        'email':         email,
        'role':          role,
        'state':         'pending' if must_change else 'active',
        'password_hash': generate_password_hash(password),
        'perms':         perms if perms is not None else default_perms(role),
        'api_key':       _api_digest(api_key) if api_key else None,
        'must_change':   must_change,
        'created_at':    now,
        'pw_changed_at': now,
        'last_login':    '',
        'fail_count':    0,
        'locked_until':  '',
    }
    with db.transaction() if db.DB_ENABLED else _lock:
        if login_in_history(login) or not _insert_user(user):
            raise LoginUnavailable(f'Логин {login} уже занят или закреплён за архивом')
    return user


def set_password(login: str, password: str, must_change: bool = False, *,
                 current: str | None = None) -> bool:
    with _edit_user(login) as user:
        if not user or user['state'] == 'deleted':
            return False
        if current is not None:
            if user['state'] == 'blocked' or not check_password_hash(user['password_hash'], current):
                return False
            if password == current:
                raise ValueError('Новый пароль должен отличаться от текущего')
        user.update(password_hash=generate_password_hash(password),
                    must_change=must_change, pw_changed_at=datetime.now().strftime(_STAMP),
                    fail_count=0, locked_until='')
        if user['state'] != 'blocked':
            user['state'] = 'pending' if must_change else 'active'
    return True


def set_permissions(login: str, role: str, perms: dict) -> bool:
    with _edit_user(login) as user:
        if not user or user['state'] == 'deleted':
            return False
        user.update(role=role, perms=perms)
    return True


def toggle_blocked(login: str):
    with _edit_user(login) as user:
        if not user or user['state'] == 'deleted':
            return None
        blocking = user['state'] != 'blocked'
        if blocking:
            user['state'] = 'blocked'
        else:
            user['state'] = 'pending' if user.get('must_change') else 'active'
            user.update(fail_count=0, locked_until='')
        return blocking


def issue_api_key(login: str):
    """Only the issuing response receives the bearer credential."""
    with _edit_user(login) as user:
        if not user or user['state'] == 'deleted':
            return None
        key = secrets.token_hex(24)
        user['api_key'] = _api_digest(key)
        return key


def user_by_api_key(key: str):
    if not key:
        return None
    digest = _api_digest(key)
    for user in load_users().values():
        stored = user.get('api_key')
        if stored and secrets.compare_digest(stored, digest):
            return user
    return None


def can(user, flag: str) -> bool:
    """Permission check. Admins always keep account management; every other
    flag is read from the account so it can be revoked individually."""
    if not user:
        return False
    return bool((user.get('perms') or {}).get(flag, False))


def password_expired(user: dict, conf: dict = None) -> bool:
    """Истёк ли срок действия пароля по настройке `pw_expire_days`.

    Отсчёт от последней смены пароля; у записей, заведённых до появления
    отметки, – от даты создания. 0 в настройке означает «не требовать смены».
    """
    conf = conf if conf is not None else get_settings()
    try:
        days = int(conf.get('pw_expire_days', 0))
    except (TypeError, ValueError):
        return False
    if days <= 0:
        return False
    stamp = user.get('pw_changed_at') or user.get('created_at') or ''
    if not stamp:
        return False
    try:
        changed = datetime.strptime(stamp, _STAMP)
    except ValueError:
        return False
    return datetime.now() - changed > timedelta(days=days)


def password_problem(password: str) -> str:
    """Return a human-readable reason the password is unacceptable, or ''."""
    conf = get_settings()
    min_len = int(conf.get('pw_min_len', 10))
    if len(password) < min_len:
        return f'Пароль короче {min_len} символов'
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        return 'Пароль должен содержать и буквы, и цифры'
    return ''


# Login journal

def record_event(login: str, ok: bool, ip: str, ua: str, reason: str = '') -> None:
    event = {
        'ts':     datetime.now().strftime(_STAMP_SEC),
        'login':  login,
        'ok':     ok,
        'ip':     ip,
        'ua':     ua,
        'reason': reason,
    }
    if db.DB_ENABLED:
        db.log_add(event)
        return
    with _lock:
        log = _read_json(LOG_PATH, [])
        log.insert(0, event)
        _write_json(LOG_PATH, log)


def add_events(events: list) -> int:
    """Записать готовые события журнала, как есть – с их собственным временем.

    Нужно загрузке дампа: record_event() проставил бы текущую дату и журнал
    перестал бы соответствовать перенесённой истории.
    """
    if not events:
        return 0
    if db.DB_ENABLED:
        for event in events:
            db.log_add(event)
        return len(events)
    with _lock:
        log = _read_json(LOG_PATH, [])
        log = list(reversed(events)) + log
        _write_json(LOG_PATH, log)
    return len(events)


def clear_events() -> None:
    """Стереть журнал входов целиком. Нужно восстановлению из дампа: иначе
    перенесённые записи ложились поверх имеющихся и журнал удваивался."""
    if db.DB_ENABLED:
        db.log_clear()
        return
    with _lock:
        _write_json(LOG_PATH, [])


def recent_logins(limit: int = 200, login=None) -> list:
    if db.DB_ENABLED:
        return db.log_recent(limit, login)
    log = _read_json(LOG_PATH, [])
    if login is not None:
        log = [e for e in log if e.get('login') == login]
    return log[:limit]


# Authentication

def _locked_for(user: dict) -> int:
    """Minutes of lockout still to run, 0 when the account is not locked."""
    until = user.get('locked_until') or ''
    if not until:
        return 0
    try:
        left = datetime.fromisoformat(until) - datetime.now()
    except ValueError:
        return 0
    return max(0, int(left.total_seconds() // 60) + 1) if left.total_seconds() > 0 else 0


def authenticate(login: str, password: str, ip: str, ua: str):
    """Check credentials and write the attempt to the journal.

    Returns (user, error_message). user is None whenever error_message is set.
    """
    with db.transaction() if db.DB_ENABLED else _lock:
        conf = get_settings()
        with _edit_user(login) as user:
            result, error, reason = _check_login(user, password, conf)
        record_event(login, result is not None, ip, ua, reason)
    return result, error


def _check_login(user, password: str, conf: dict):
    if not user or user['state'] == 'deleted':
        return None, 'Неверный логин или пароль', 'учётной записи не существует'

    if user.get('state') == 'blocked':
        return None, 'Учётная запись заблокирована. Обратитесь к администратору.', 'учётная запись заблокирована'

    left = _locked_for(user)
    if left:
        return None, f'Слишком много неудачных попыток. Вход откроется через {left} мин.', 'вход временно закрыт'

    if not check_password_hash(user['password_hash'], password):
        user['fail_count'] = int(user.get('fail_count', 0)) + 1
        limit = int(conf.get('lock_after_fails', 5))
        reason = 'неверный пароль'
        if limit and user['fail_count'] >= limit:
            minutes = int(conf.get('lock_minutes', 15))
            user['locked_until'] = (datetime.now() + timedelta(minutes=minutes)).isoformat()
            user['fail_count'] = 0
            reason = f'неверный пароль, вход закрыт на {minutes} мин.'
        return None, 'Неверный логин или пароль', reason

    user['fail_count'] = 0
    user['locked_until'] = ''
    user['last_login'] = datetime.now().strftime(_STAMP)
    # Срок действия пароля вышел – вход разрешён, но следующим экраном будет
    # смена пароля (login_required не пускает дальше с must_change).
    expired = password_expired(user, conf)
    if expired:
        user['must_change'] = True
    return user, '', ('срок действия пароля истёк, требуется смена' if expired else '')


# System settings

def get_settings() -> dict:
    stored = db.settings_load() if db.DB_ENABLED else _read_json(CONF_PATH, {})
    conf = dict(DEFAULT_SETTINGS)
    conf.update(stored or {})
    return conf


def save_settings(conf: dict) -> None:
    with nullcontext() if db.DB_ENABLED else _lock:
        merged = get_settings()
        merged.update(conf)
        if db.DB_ENABLED:
            db.settings_save(merged)
        else:
            _write_json(CONF_PATH, merged)


# First run

def bootstrap() -> None:
    """Create the first administrator when the account list is empty, using
    AU_USERNAME / AU_PASSWORD so existing installations keep working."""
    with db.transaction() if db.DB_ENABLED else _lock:
        if db.DB_ENABLED:
            with db._conn() as conn:
                conn.execute('LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE')
        users = load_users(include_deleted=True)
        if users:
            upgraded = {login: normalize_user(user) for login, user in users.items()}
            if upgraded != users:
                if db.DB_ENABLED:
                    for login, user in upgraded.items():
                        if user != users[login]:
                            db.users_save(user)
                else:
                    _write_json(USERS_PATH, upgraded)
            return

        login = os.environ.get('AU_USERNAME', 'admin').strip() or 'admin'
        password = os.environ.get('AU_PASSWORD', '')
        problem = password_problem(password)
        if password in ('admin', 'changeme') or problem:
            raise RuntimeError('Для первого запуска задайте AU_PASSWORD в окружении. '
                               + (problem or 'Выберите собственный пароль.'))
        create_user(login=login,
                    fio=os.environ.get('AU_FIO', 'Администратор системы'),
                    password=password, role='admin',
                    must_change=bool(get_settings().get('pw_require_change_first')),
                    api_key=os.environ.get('AU_API_KEY', '').strip())
