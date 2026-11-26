"""Portable, non-executable SQL backups for both ALENA storage backends."""

import io
import json
import os
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from checker import accounts, db, job_store, jsonstore, memory_store, teams
from checker.text_plagiarism import text_fingerprint

TABLES = ('users', 'jobs', 'fingerprints', 'login_events', 'settings', 'teams')
_JOB_COLS = ('job_id', 'data')
_FP_COLS = ('entry_key', 'key_base', 'version', 'filename', 'student',
            'normalized_text', 'text_hash', 'image_data', 'pages_count', 'job_id',
            'added_at', 'owner')
_EVENT_COLS = ('ts', 'login', 'ok', 'ip', 'ua', 'reason')
_COLUMNS = {'users': db.USER_COLS, 'teams': db.TEAM_COLS, 'jobs': _JOB_COLS,
            'fingerprints': _FP_COLS, 'login_events': _EVENT_COLS,
            'settings': ('key', 'value')}
_ORDER = tuple(_COLUMNS)


def transaction():
    return (db.transaction() if db.DB_ENABLED
            else jsonstore.transaction(accounts.USERS_PATH.parent))


def recover_json():
    if not db.DB_ENABLED:
        jsonstore.recover(accounts.USERS_PATH.parent)


def _lit(value) -> str:
    if value is None:
        return 'NULL'
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, allow_nan=False)
        return "'" + text.replace("'", "''") + "'::jsonb"
    return "'" + value.replace("'", "''") + "'"


def counts() -> dict:
    with (db.transaction(snapshot=True) if db.DB_ENABLED else jsonstore.lock):
        if db.DB_ENABLED:
            with db._conn() as conn:
                return {t: conn.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
                        for t in TABLES if t != 'settings'}
        return {
            'users': len(accounts.load_users(include_deleted=True)),
            'jobs': len(job_store.load_all()),
            'fingerprints': len(memory_store.load_store()),
            'login_events': len(accounts.recent_logins(None)),
            'teams': len(teams.load_teams()),
        }


def _export_rows(table):
    if table == 'settings':
        yield {'key': 'app', 'value': accounts.get_settings()}
        return
    if db.DB_ENABLED:
        order = ' ORDER BY id' if table == 'login_events' else ''
        cols = _COLUMNS[table]
        with db._conn() as conn, conn.cursor(name='alena_export') as cur:
            cur.execute(f'SELECT {", ".join(cols)} FROM {table}{order}')
            for row in cur:
                yield dict(zip(cols, row))
        return
    if table == 'users':
        yield from accounts.load_users(include_deleted=True).values()
    elif table == 'teams':
        yield from teams.load_teams().values()
    elif table == 'jobs':
        for key, data in job_store.load_all().items():
            yield {'job_id': key, 'data': data}
    elif table == 'fingerprints':
        for key, data in memory_store.load_store().items():
            yield dict(data, entry_key=key)
    elif table == 'login_events':
        yield from reversed(accounts.recent_logins(None))


def write_dump(out):
    """Write an ordinary SQL file, keeping the same snapshot from start to end."""
    snapshot = db.transaction(snapshot=True) if db.DB_ENABLED else jsonstore.lock
    with snapshot:
        out.write('-- АЛЁНА – дамп базы данных\n')
        out.write(f'-- Сформирован: {datetime.now():%d.%m.%Y %H:%M}\n')
        out.write('-- Готовые HTML-отчёты (reports/) переносятся отдельно.\nBEGIN;\n')
        for stmt in db.SCHEMA:
            out.write(f'{stmt.strip()};\n')
        for table in _ORDER:
            out.write(f'DELETE FROM {table};\n')
        # Counts verify completeness even if a line disappeared mid-file.
        numbers = {}
        for table in _ORDER:
            cols = _COLUMNS[table]
            numbers[table] = 0
            for rec in _export_rows(table):
                if table == 'users':
                    rec = accounts.normalize_user(rec)
                values = ', '.join(_lit(rec.get(c)) for c in cols)
                out.write(f'INSERT INTO {table} ({", ".join(cols)}) VALUES ({values});\n')
                numbers[table] += 1
        out.write('-- ALENA rows ' + json.dumps(numbers) + '\nCOMMIT;\n')


def dump() -> str:
    out = io.StringIO()
    write_dump(out)
    return out.getvalue()


_INSERT = re.compile(r'INSERT\s+INTO\s+(\w+)\s*\(([^)]*)\)\s*VALUES\s*\((.*)\)',
                     re.IGNORECASE | re.DOTALL)
_OLD_COUNTS = re.compile(r'-- Учётных записей: (\d+), проверок: (\d+), отпечатков: (\d+), '
                         r'записей журнала: (\d+)(?:, групп преподавателей: (\d+))?')
_TOKEN = re.compile(r"'|--|;")
_VALUE = re.compile(r"""\s*(?:'([^']*(?:''[^']*)*)'(?:::(jsonb?))?|(NULL|TRUE|FALSE|-?\d+(?:\.\d+)?(?:e[+-]?\d+)?))\s*(,|$)""",
                    re.IGNORECASE | re.DOTALL)


def _statements(stream, meta):
    """Consume text or UTF-8 binary lines; retain at most one SQL statement."""
    if isinstance(stream, str):
        stream = io.StringIO(stream)
    buf = io.StringIO()
    quoted = False
    for line in stream:
        if isinstance(line, bytes):
            line = line.decode('utf-8')
        if not quoted and line.startswith('--'):
            if line.startswith('-- ALENA rows '):
                meta['counts'] = json.loads(line[len('-- ALENA rows '):])
            elif match := _OLD_COUNTS.fullmatch(line.strip()):
                names = ('users', 'jobs', 'fingerprints', 'login_events', 'teams')
                meta['counts'] = {name: int(n) for name, n in zip(names, match.groups())
                                  if n is not None}
                meta['legacy_counts'] = True
            continue
        start = 0
        for token in _TOKEN.finditer(line):
            s = token.group()
            if s == "'":
                quoted = not quoted
            elif quoted:
                continue
            elif s == '--':
                buf.write(line[start:token.start()] + '\n')
                start = len(line)
                break
            else:
                buf.write(line[start:token.start()])
                stmt = buf.getvalue().strip()
                if stmt:
                    yield stmt
                buf = io.StringIO()
                start = token.end()
        buf.write(line[start:])
    if quoted or buf.getvalue().strip():
        raise ValueError('Оборванная инструкция SQL')


def _values(raw):
    pos = 0
    while pos < len(raw):
        match = _VALUE.match(raw, pos)
        if not match:
            raise ValueError('Недопустимый литерал SQL')
        string, cast, bare, sep = match.groups()
        if bare is not None:
            yield json.loads(bare.lower())
        else:
            string = string.replace("''", "'")
            yield json.loads(string) if cast else string
        pos = match.end()
        if sep and pos == len(raw):
            raise ValueError('Пропущено значение SQL')


def _records(stream, meta):
    started, committed = False, False
    clears = set()
    for stmt in _statements(stream, meta):
        if committed:
            raise ValueError('После COMMIT найдены дополнительные инструкции')
        if stmt.upper() == 'BEGIN':
            if started:
                raise ValueError('Повторное начало дампа')
            started = True
            continue
        if stmt.upper() == 'COMMIT':
            committed = True
            continue
        if delete := re.fullmatch(r'DELETE\s+FROM\s+(\w+)', stmt, re.I):
            clears.add(delete.group(1).lower())
            continue
        if stmt.split(None, 1)[0].upper() in ('CREATE', 'ALTER', 'SET'):
            continue
        match = _INSERT.fullmatch(stmt)
        if not match or match[1].lower() not in TABLES:
            meta['skipped'] += 1
            continue
        table = match[1].lower()
        cols = [c.strip().strip('"') for c in match[2].split(',')]
        vals = list(_values(match[3]))
        if len(set(cols)) != len(cols) or len(vals) != len(cols) or set(cols) - set(_COLUMNS[table]):
            raise ValueError(f'Некорректные столбцы таблицы {table}')
        yield table, dict(zip(cols, vals))
    if started and not committed:
        raise ValueError('Дамп оборван до COMMIT')
    # Pre-teams exports used five tables and did not count teams in the header.
    required = set(TABLES)
    if meta.get('legacy_counts') and 'teams' not in meta['counts']:
        required.remove('teams')
    meta['complete'] = started and committed and required <= clears


def parse(text) -> dict:
    rows = {name: [] for name in TABLES}
    meta = {'skipped': 0}
    for table, rec in _records(text, meta):
        rows[table].append(rec)
    return dict(rows, **meta)


def _require(condition, context):
    if not condition:
        raise ValueError(f'Некорректные данные: {context}')


def _integer(value):
    return type(value) is int and value >= 0


def _pg_integer(value):
    return _integer(value) and value <= 2**31 - 1


def _percentage(value):
    return type(value) in (int, float) and 0 <= value <= 100


def _strings(values):
    return isinstance(values, list) and all(isinstance(s, str) for s in values)


def _check_summary(summary):
    for key in ('gost', 'grade', 'grade_score', 'scale', 'clean',
                'matches_total', 'no_text'):
        value = summary.get(key)
        _require(value is None or type(value) in (int, float), f'jobs.summary.{key}')
    for key in ('plag', 'threshold'):
        _require(summary.get(key) is None or _percentage(summary[key]), f'jobs.summary.{key}')
    _require(type(summary.get('plag_flagged', False)) is bool, 'jobs.summary.plag_flagged')
    if 'clean' in summary:
        _require(_integer(summary['clean']), 'jobs.summary.clean')
    _require(_strings(summary.get('groups', [])), 'jobs.summary.groups')
    fails = summary.get('fail_counts', [])
    _require(isinstance(fails, list) and all(isinstance(pair, list) and len(pair) == 2
             and isinstance(pair[0], str) and _integer(pair[1]) for pair in fails),
             'jobs.summary.fail_counts')
    for field in ('students', 'matches'):
        entries = summary.get(field, [])
        _require(isinstance(entries, list) and all(isinstance(row, dict) for row in entries),
                 f'jobs.summary.{field}')
    for student in summary.get('students', []):
        _require(isinstance(student.get('fio'), str) and _strings(student.get('fails', [])),
                 'jobs.summary.students')
        _require(isinstance(student.get('filename', ''), str), 'jobs.students.filename')
        _require('work_id' not in student or _integer(student['work_id']), 'jobs.students.work_id')
        _require(student.get('gost') is None or _integer(student['gost']), 'jobs.students.gost')
        _require(student.get('plag') is None or _percentage(student['plag']), 'jobs.students.plag')
        _require(type(student.get('plag_flagged', False)) is bool, 'jobs.students.plag_flagged')
        grade = student.get('grade')
        if grade is not None:
            _require(isinstance(grade, dict) and all(grade.get(k) is None or
                     type(grade[k]) in (int, float) for k in ('pct', 'score', 'scale')),
                     'jobs.students.grade')
            _require(isinstance(grade.get('lost', []), list) and
                     all(isinstance(row, dict) and type(row.get('weight')) in (int, float)
                         for row in grade.get('lost', [])), 'jobs.students.grade.lost')
        flaws = student.get('flaws', [])
        _require(isinstance(flaws, list) and all(isinstance(row, dict) for row in flaws),
                 'jobs.students.flaws')
    for match in summary.get('matches', []):
        _require(match.get('pct') is None or _percentage(match['pct']), 'jobs.matches.pct')
        for field in ('pages', 'b_pages'):
            pages = match.get(field, [])
            _require(isinstance(pages, list) and all(_integer(n) for n in pages),
                     f'jobs.matches.{field}')
        for field in ('a_id', 'b_id'):
            _require(field not in match or _integer(match[field]), f'jobs.matches.{field}')


def _validate(table, rec):
    _require(isinstance(rec, dict) and not set(rec) - set(_COLUMNS[table]), table)
    key = _COLUMNS[table][0]
    _require(isinstance(rec.get(key), str) and rec[key], f'{table}.{key}')
    if table == 'jobs':
        _require(re.fullmatch('[0-9a-f]{6,40}', rec[key]), 'jobs.job_id')
        job = rec.get('data')
        _require(isinstance(job, dict) and job.get('status') in
                 ('done', 'error', 'processing', 'cancelled'), 'jobs.status')
        for field in ('owner', 'owner_fio', 'step', 'created_at', 'error'):
            _require(job.get(field) is None or isinstance(job[field], str), f'jobs.{field}')
        for field in ('progress', 'total', 'done_files', 'text_pairs', 'img_pairs'):
            _require(field not in job or _integer(job[field]), f'jobs.{field}')
        _require('threshold' not in job or _percentage(job['threshold']), 'jobs.threshold')
        _require(job.get('beat') is None or type(job['beat']) in (int, float), 'jobs.beat')
        _require(job.get('summary') is None or isinstance(job['summary'], dict), 'jobs.summary')
        if job.get('summary') is not None:
            _check_summary(job['summary'])
        _require('job_id' not in job, 'jobs.data.job_id')
    elif table == 'users':
        _require(rec.get('role') in ('teacher', 'admin') and
                 rec.get('state', 'active') in ('active', 'pending', 'blocked', 'deleted'),
                 'users.role/state')
        _require(isinstance(rec.get('password_hash'), str) and
                 (rec['password_hash'] or rec.get('state') == 'deleted'), 'users.password_hash')
        _require('fio' not in rec or isinstance(rec['fio'], str), 'users.fio')
        perms = rec.get('perms')
        _require(perms is None or (isinstance(perms, dict) and
                                   all(type(v) is bool for v in perms.values())), 'users.perms')
        _require(rec.get('fail_count') is None or _pg_integer(rec['fail_count']), 'users.fail_count')
        _require(rec.get('must_change') is None or type(rec['must_change']) is bool, 'users.must_change')
        for field in set(rec) - {'fail_count', 'perms', 'must_change'}:
            _require(rec[field] is None or isinstance(rec[field], str), f'users.{field}')
        if rec.get('account_id'):
            _require(re.fullmatch('[0-9a-f]{32}', rec['account_id']), 'users.account_id')
    elif table == 'fingerprints':
        for field in ('owner', 'filename', 'key_base', 'normalized_text', 'text_hash',
                      'job_id', 'added_at'):
            _require(rec.get(field) is None or isinstance(rec[field], str), f'fingerprints.{field}')
        _require(rec.get('key_base') and _pg_integer(rec.get('version')) and rec['version'] > 0,
                 'fingerprints.version')
        _require(rec.get('pages_count') is None or _pg_integer(rec['pages_count']), 'fingerprints.pages_count')
        if rec.get('owner'):
            prefix = rec['owner'] + '|'
            _require(rec[key].startswith(prefix) and rec['key_base'].startswith(prefix),
                     'владелец отпечатка не совпадает с его ключом')
        if rec.get('text_hash'):
            _require(rec['text_hash'] == text_fingerprint(rec.get('normalized_text') or ''),
                     'fingerprints.text_hash')
        student = rec.get('student')
        _require(student is None or (isinstance(student, dict) and
                                    all(isinstance(student.get(k, ''), str) for k in ('name', 'group'))),
                 'fingerprints.student')
        if rec[key] != f"{rec['key_base']}|v{rec['version']}":
            # Rekeying legacy anonymous submissions kept their public URLs,
            # including the old shared version number.
            prefix = (rec.get('owner') or '') + '||'
            _require(memory_store.student_id(student) == memory_store.NO_STUDENT
                     and rec['key_base'].startswith(prefix + 'anon:')
                     and rec[key].startswith(prefix)
                     and re.search(r'\|v[1-9]\d*$', rec[key]),
                     'идентификатор отпечатка и его версия не совпадают')
        _require(rec.get('image_data') is None or isinstance(rec['image_data'], list), 'fingerprints.image_data')
        for image in rec.get('image_data') or ():
            _require(isinstance(image, dict) and _integer(image.get('page', 0)) and
                     type(image.get('is_ui', False)) is bool and _strings(image.get('hashes')) and
                     all(re.fullmatch('[0-9a-fA-F]{36}', h) for h in image['hashes']) and
                     (not image.get('thumb') or memory_store.valid_preview(image['thumb'])),
                     'fingerprints.image_data')
    elif table == 'teams':
        _require(isinstance(rec.get('name'), str) and _strings(rec.get('members')), 'teams')
        _require(rec.get('created_at') is None or isinstance(rec['created_at'], str), 'teams.created_at')
    elif table == 'login_events':
        _require(type(rec.get('ok')) is bool, 'login_events.ok')
        for field in set(rec) - {'ok'}:
            _require(rec[field] is None or isinstance(rec[field], str), f'login_events.{field}')
    elif table == 'settings':
        conf = rec.get('value')
        _require(rec['key'] == 'app' and isinstance(conf, dict), 'settings')
        for field, default in accounts.DEFAULT_SETTINGS.items():
            if field in conf:
                valid = type(conf[field]) is type(default)
                if isinstance(default, float):
                    valid = type(conf[field]) in (int, float) and 0 <= conf[field] <= 1
                _require(valid, f'settings.{field}')
        bounds = {'grade_scale': (2, 100), 'retention_days': (0, 3650),
                  'pw_min_len': (6, 64), 'pw_expire_days': (0, 1095),
                  'lock_after_fails': (0, 20), 'lock_minutes': (1, 1440)}
        for field, (lo, hi) in bounds.items():
            if field in conf:
                _require(lo <= conf[field] <= hi, f'settings.{field}')
        _require(_strings(conf.get('default_gost', [])) and
                 all(_integer(v) and v <= 100 for v in conf.get('gost_weights', {}).values()),
                 'settings.gost')
    # PostgreSQL TEXT and JSONB cannot store NUL. Also reject non-finite JSON numbers.
    data = json.dumps(rec, ensure_ascii=False, allow_nan=False)
    _require(re.search(r'(?<!\\)(?:\\\\)*\\u0000', data) is None, table)
    return data


class _Staged:
    """Validated rows on a private temporary SQLite database, replayable by table."""
    def __init__(self, spool):
        self.spool = spool
        spool.execute('CREATE TABLE rows_data (tbl TEXT, ident TEXT, data TEXT, UNIQUE(tbl,ident))')
        self.counts = dict.fromkeys(TABLES, 0)
        self.referenced_logins = set()

    def add(self, table, rec):
        data = _validate(table, rec)
        ident = None if table == 'login_events' else rec[_COLUMNS[table][0]]
        try:
            self.spool.execute('INSERT INTO rows_data VALUES (?,?,?)', (table, ident, data))
        except sqlite3.IntegrityError as exc:
            raise ValueError(f'Повторяющийся ключ в таблице {table}') from exc
        self.counts[table] += 1
        if table == 'jobs':
            self.referenced_logins.add(rec['data'].get('owner'))
        elif table == 'fingerprints':
            self.referenced_logins.add(rec.get('owner'))
        elif table == 'teams':
            self.referenced_logins.update(rec['members'])
        elif table == 'login_events' and rec.get('reason') != 'учётной записи не существует':
            self.referenced_logins.add(rec.get('login'))

    def rows(self, table):
        cursor = self.spool.execute('SELECT data FROM rows_data WHERE tbl=? ORDER BY rowid', (table,))
        return (json.loads(data) for data, in cursor)

    def keys(self, table):
        return {k for k, in self.spool.execute('SELECT ident FROM rows_data WHERE tbl=?', (table,))}


@contextmanager
def staged_rows(source, replace=False):
    """Parse and validate before acquiring application locks or changing live data."""
    if isinstance(source, dict):
        meta = source
        records = ((t, rec) for t in TABLES for rec in source.get(t, ()))
    else:
        meta = {'skipped': 0}
        records = _records(source, meta)
    tmp_root = os.environ.get('AU_TMP_DIR', '').strip() or None
    if tmp_root:
        Path(tmp_root).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='alena_import_', dir=tmp_root) as temp:
        spool = sqlite3.connect(Path(temp) / 'rows.sqlite')
        try:
            stage = _Staged(spool)
            for table, rec in records:
                stage.add(table, rec)
            _require(not meta.get('skipped'), 'в дампе есть неизвестные инструкции')
            _require(any(stage.counts.values()), 'в дампе нет данных')
            numbers = meta.get('counts')
            if numbers is not None:
                _require(isinstance(numbers, dict) and numbers and
                         all(t in TABLES and _integer(n) and stage.counts[t] == n
                             for t, n in numbers.items()), 'не совпадает число строк в дампе')
                if meta.get('legacy_counts'):
                    required = set(TABLES) - {'settings', 'teams'}
                    _require(required <= numbers.keys(), 'неполный заголовок дампа')
                else:
                    _require(numbers.keys() == set(TABLES), 'неполный заголовок дампа')
            stage.complete = bool(meta.get('complete') and numbers
                                  and stage.counts['settings'] == 1 and stage.counts['users'])
            if replace:
                _require(stage.complete, 'для полной замены нужен полный дамп АЛЁНЫ')
            yield stage
        finally:
            spool.close()


def restore_file(stream, replace=False, keep_login='', protected_job_ids=()):
    with staged_rows(stream, replace=replace) as rows:
        return restore(rows, replace, keep_login, protected_job_ids)


def restore(rows, replace=False, keep_login='', protected_job_ids=()):
    if not isinstance(rows, _Staged):
        with staged_rows(rows, replace=replace) as stage:
            return restore(stage, replace, keep_login, protected_job_ids)
    if replace:
        _require(rows.complete, 'для полной замены нужен полный дамп АЛЁНЫ')
    protected = set(protected_job_ids)
    _require(not (protected and (replace or protected & rows.keys('jobs'))),
             'дамп затрагивает выполняющуюся проверку')
    try:
        with transaction():
            if db.DB_ENABLED:
                with db._conn() as conn:
                    conn.execute('LOCK TABLE users, fingerprints, jobs, teams, settings, login_events '
                                 'IN SHARE ROW EXCLUSIVE MODE')
            return _apply(rows, replace, keep_login)
    finally:
        if not db.DB_ENABLED:
            with jsonstore.lock:
                job_store._owner_cache.clear()


def _apply(stage, replace, keep_login):
    stats = dict(stage.counts, deleted_users=0, cleared_jobs=0)
    current = accounts.load_users(include_deleted=True)
    incoming = stage.keys('users')
    if keep_login:
        _require(keep_login in current and current[keep_login]['role'] == 'admin',
                 'учётная запись импортирующего администратора')
    if stage.complete and not replace:
        live = {login for login, user in current.items() if user.get('state') != 'deleted'}
        conflicts = (stage.referenced_logins - incoming) & live
        if conflicts:
            raise ValueError('История в дампе относится к отсутствующей учётной записи, '
                             'а локальный логин уже занят: ' + ', '.join(sorted(conflicts)))
    # Resolve existing identities before performing any destructive writes.
    protected_collision = bool(keep_login and replace and keep_login not in incoming)
    for user in stage.rows('users'):
        login = user['login']
        old = current.get(login)
        if old:
            aid = user.get('account_id')
            same = (aid == old.get('account_id') if aid
                    else old['password_hash'] == user['password_hash'])
            if login == keep_login:
                protected_collision = not same
            elif not replace:
                _require(same and (old['state'] != 'deleted' or user.get('state') == 'deleted'),
                         f'логин {login} принадлежит другой учётной записи')
        elif not replace:
            _require(not accounts.login_in_history(login), f'логин {login} зарезервирован историей')
    if protected_collision:
        _require(keep_login not in stage.referenced_logins,
                 'логин администратора связан в дампе с другой учётной записью. '
                 'Войдите под отдельным локальным администратором для переноса')
    if replace:
        memory_store.clear_store(None)
        for key in teams.load_teams():
            teams.delete_team(key)
        stats['cleared_jobs'] = len(job_store.clear(None))
        accounts.clear_events()
        remove = current.keys() - incoming - {keep_login}
        if db.DB_ENABLED:
            for login in remove:
                db.users_delete(login)
        elif remove:
            jsonstore.write_json(accounts.USERS_PATH, {k: u for k, u in current.items() if k not in remove})
        stats['deleted_users'] = len(remove)

    for user in stage.rows('users'):
        login = user['login']
        if login == keep_login:
            stats['users'] -= 1
            continue
        old = current.get(login)
        legacy = not user.get('account_id')
        if old and legacy and old['password_hash'] == user['password_hash']:
            user = accounts.normalize_user(user)
            user['account_id'] = old.get('account_id') or user['account_id']
        if user.get('perms') is None or (not user['perms'] and legacy):
            user['perms'] = accounts.default_perms(user['role'])
            user['perms']['delete_all'] = False
        user.setdefault('fio', login)
        user.setdefault('state', 'active')
        user['must_change'] = bool(user.get('must_change'))
        user['fail_count'] = user.get('fail_count') or 0
        accounts.save_user(user)

    for team in stage.rows('teams'):
        teams.save_team(team)
    for job in stage.rows('jobs'):
        job_store.save(job['job_id'], job['data'])

    store = {}
    for entry in stage.rows('fingerprints'):
        key = entry.pop('entry_key')
        for field in ('filename', 'job_id', 'added_at', 'owner'):
            entry[field] = entry.get(field) or ''
        entry['pages_count'] = entry.get('pages_count') or 0
        entry['student'] = entry.get('student') or {}
        entry['normalized_text'] = entry.get('normalized_text') or ''
        entry['text_hash'] = entry.get('text_hash') or text_fingerprint(entry['normalized_text'])
        entry['image_data'] = entry.get('image_data') or []
        store[key] = entry
        if db.DB_ENABLED and len(store) >= 100:
            memory_store.save_store(store)
            store.clear()
    if store:
        memory_store.save_store(store)
    if stage.counts['fingerprints']:
        memory_store.migrate_anonymous_versions()
    events = []
    for event in stage.rows('login_events'):
        if db.DB_ENABLED:
            db.log_add(event)
        else:
            events.append(event)
    if events:
        accounts.add_events(events)
    if stage.referenced_logins:
        accounts.reserve_logins({login for login in stage.referenced_logins if login})
    for rec in stage.rows('settings'):
        if replace:
            if db.DB_ENABLED:
                db.settings_save(rec['value'])
            else:
                jsonstore.write_json(accounts.CONF_PATH, rec['value'])
        else:
            accounts.save_settings(rec['value'])
    return stats
