"""
Persistent fingerprint store for cross-session plagiarism detection.

Backed by PostgreSQL when DATABASE_URL is set, otherwise by a JSON file:
  { "<owner>|<name>|<group>|v<N>": { ...entry... }, ... }

Ключ начинается с владельца: базы двух преподавателей не пересекаются даже при
полном тёзке в одной группе (см. _work_key).

Each entry stores: student info, normalized text, image hashes + thumbnails.
PIL images are NOT stored, only compact hashes (144 bits × 3 crops) and a
small JPEG thumbnail per image.
"""

import base64
import hashlib
import io
import json
from datetime import datetime
from pathlib import Path

from PIL import Image

from checker import db, jsonstore

STORE_PATH = Path(__file__).parent.parent / 'memory' / 'store.json'
_lock = jsonstore.lock


def _read_all() -> dict:
    return jsonstore.read_json(STORE_PATH, {})


def _owners(owner):
    """None → вся база; логин или список логинов → множество владельцев."""
    if owner is None:
        return None
    return {owner} if isinstance(owner, str) else set(owner)


def load_store(owner=None) -> dict:
    """Fingerprints visible to `owner`, or the whole base when owner is None.

    `owner` – логин или список логинов. Преподаватель вне групп видит только
    себя; состоящий в группе видит ещё и коллег по ней, потому что база группы
    общая (см. checker/teams.py).
    """
    owners = _owners(owner)
    if db.DB_ENABLED:
        return db.fp_load_all(None if owners is None else sorted(owners))
    store = _read_all()
    if owners is None:
        return store
    return {k: v for k, v in store.items() if v.get('owner', '') in owners}


def save_store(store: dict) -> None:
    """Persist the given entries. Entries outside `store` are left untouched,
    so saving one teacher's slice never disturbs another's."""
    if db.DB_ENABLED:
        db.fp_upsert_many(store)
        return
    with _lock:
        full = _read_all()
        full.update(store)
        jsonstore.write_json(STORE_PATH, full)


def clear_store(owner=None) -> int:
    """Delete the whole base, or just one owner's entries. Returns the count."""
    if db.DB_ENABLED:
        return db.fp_clear(owner)
    with _lock:
        store = _read_all()
        if owner is None:
            count = len(store)
            jsonstore.unlink(STORE_PATH, missing_ok=True)
            return count
        doomed = [k for k, v in store.items() if v.get('owner', '') == owner]
        for k in doomed:
            del store[k]
        jsonstore.write_json(STORE_PATH, store)
    return len(doomed)


def delete_entry(entry_key: str) -> bool:
    """Remove a single entry by key. Thread-safe. Returns True if it existed."""
    if db.DB_ENABLED:
        return db.fp_delete(entry_key)
    with _lock:
        store = _read_all()
        if entry_key not in store:
            return False
        del store[entry_key]
        jsonstore.write_json(STORE_PATH, store)
    return True


def get_summary(store: dict) -> list:
    """Lightweight list for the UI, no text or hashes included."""
    result = []
    for key, v in store.items():
        result.append({
            'key':         key,
            'version':     v.get('version', 1),
            'filename':    v.get('filename', ''),
            'student':     v.get('student', {}),
            'pages_count': v.get('pages_count', 0),
            'image_count': len(v.get('image_data', [])),
            'added_at':    v.get('added_at', ''),
            'job_id':      v.get('job_id', ''),
            'owner':       v.get('owner', ''),
        })
    def _parse_dt(s):
        try:
            return datetime.strptime(s, '%d.%m.%Y %H:%M')
        except Exception:
            return datetime.min

    result.sort(key=lambda x: _parse_dt(x['added_at']), reverse=True)
    return result


NO_STUDENT = '|'


def student_id(student: dict) -> str:
    """Личность студента без владельца: «фио|группа» в нижнем регистре.

    По ней шаг 4 конвейера отсеивает из истории самого проверяемого студента.
    Ключ записи для этого не годится: в общей базе группы преподавателей ту же
    работу мог сохранить коллега, под своим владельцем и своим ключом, – и
    студента обвинило бы в списывании у самого себя.

    Одна группа без имени не идентифицирует человека: все работы группы иначе
    считались бы работами одного студента и не сравнивались между собой.
    """
    s = student or {}
    name = str(s.get('name') or '').strip().lower()
    group = str(s.get('group') or '').strip().lower()
    return f'{name}|{group}' if name else NO_STUDENT


def _work_key(entry: dict) -> str:
    owner = entry.get('owner', '')
    student = entry.get('student') or {}
    identity = student_id(student)
    if identity != NO_STUDENT:
        return f'{owner}|{identity}'

    # A filename such as "report.pdf" can occur in every submission. Only an
    # exact repeat of the same anonymous work shares its version sequence.
    from checker.text_plagiarism import text_fingerprint
    signature = [
        (student.get('group') or '').strip().casefold(),
        (entry.get('filename') or '').strip().casefold(),
        entry.get('text_hash') or text_fingerprint(entry.get('normalized_text', '')),
        entry.get('pages_count', 0),
        [(img.get('page'), img['hashes']) for img in entry.get('image_data', [])
         if isinstance(img, dict) and img.get('hashes')],
    ]
    digest = hashlib.sha256(json.dumps(signature, separators=(',', ':')).encode())
    return f'{owner}||anon:{digest.hexdigest()}'


def migrate_anonymous_versions() -> None:
    """Split old anonymous buckets, preserving public entry keys and URLs."""
    with (db.transaction() if db.DB_ENABLED else _lock) as conn:
        if db.DB_ENABLED:
            conn.execute("LOCK TABLE fingerprints IN SHARE ROW EXCLUSIVE MODE")
            # Current stores don't need to materialize historical images and
            # text at each startup just to discover there is no migration.
            legacy = conn.execute(
                "SELECT 1 FROM fingerprints WHERE "
                "COALESCE(btrim(student->>'name'), '') = '' AND "
                "NOT starts_with(key_base, COALESCE(owner, '') || '||anon:') LIMIT 1")
            if legacy.fetchone() is None:
                return
        store = load_store()
        old = []
        used = {}
        for key, entry in store.items():
            base = entry.get('key_base', '')
            if (student_id(entry.get('student')) == NO_STUDENT
                    and not base.startswith(f"{entry.get('owner', '')}||anon:")):
                old.append((key, entry))
            else:
                used.setdefault(base, set()).add(entry.get('version', 1))
        changed = {}
        next_free = {}
        for key, entry in sorted(old, key=lambda row: (row[1].get('version', 1), row[0])):
            base = _work_key(entry)
            version = next_free.get(base, 1)
            occupied = used.get(base, ())
            while version in occupied:
                version += 1
            next_free[base] = version + 1
            changed[key] = dict(entry, key_base=base, version=version)
        if changed:
            save_store(changed)


_PREVIEW_PREFIX = 'data:image/jpeg;base64,'
# Reports create thumbnails no larger than 200 x 160, stored ones are 120 x 96.
_MAX_PREVIEW_URI = 256 * 1024


def _preview_image(uri):
    if (not isinstance(uri, str) or len(uri) > _MAX_PREVIEW_URI
            or not uri.startswith(_PREVIEW_PREFIX)):
        raise ValueError('Invalid thumbnail')
    raw = base64.b64decode(uri[len(_PREVIEW_PREFIX):], validate=True)
    with Image.open(io.BytesIO(raw), formats=('JPEG',)) as img:
        if img.width > 200 or img.height > 160:
            raise ValueError('Oversized thumbnail')
        return img.convert('RGB')


def valid_preview(uri) -> bool:
    try:
        with _preview_image(uri):
            return True
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return False


def _shrink_thumb(data_uri: str) -> str:
    if not data_uri:
        return ''
    try:
        with _preview_image(data_uri) as img:
            img.thumbnail((120, 96))
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=55)
        return _PREVIEW_PREFIX + base64.b64encode(buf.getvalue()).decode()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return ''


def _entry_for(report: dict, job_id: str, owner: str) -> tuple:
    """(key_base, запись без номера версии) для одной проверенной работы."""
    from checker.text_plagiarism import normalize_text, text_fingerprint

    image_data = []
    for img_info in report.get('images', []):
        hashes = img_info.get('hashes')
        if not hashes:
            continue
        image_data.append({
            'page':   img_info.get('page', 0),
            'hashes': [str(h) for h in hashes],
            'thumb':  _shrink_thumb(img_info.get('thumb', '')),
            'is_ui':  img_info.get('is_ui', False),
        })

    normalized_text = normalize_text(report.get('full_text', ''))
    entry = {
        'filename':        report.get('filename', ''),
        'student':         report.get('student', {}),
        'normalized_text': normalized_text,
        'text_hash':       text_fingerprint(normalized_text),
        'image_data':      image_data,
        'pages_count':     report.get('pages_count', 0),
        'added_at':        datetime.now().strftime('%d.%m.%Y %H:%M'),
        'job_id':          job_id,
        'owner':           owner,
    }
    key_base = _work_key(entry)
    entry['key_base'] = key_base
    return key_base, entry


def add_reports(reports: list, job_id: str, owner: str = '') -> list:
    """Записать партию работ в базу отпечатков и вернуть присвоенные номера
    версий (v1, v2, …) в порядке работ.

    Номер версии выбирается и запись сохраняется одним неделимым действием.
    Раньше вызывающий читал базу, считал `max(версий)+1` по своему снимку и
    сохранял: две проверки одного преподавателя, идущие рядом, выбирали один и
    тот же номер, и отпечаток первой затирался отпечатком второй.

    The JSON backend reads and replaces ``store.json`` once for the whole job,
    while the lock still makes version selection atomic against another job.
    """
    prepared = [_entry_for(report, job_id, owner) for report in reports]
    if not prepared:
        return []

    if db.DB_ENABLED:
        return db.fp_insert_batch(prepared)

    with _lock:
        store = _read_all()
        latest = {}
        for value in store.values():
            key_base = value.get('key_base', '')
            latest[key_base] = max(latest.get(key_base, 0),
                                   value.get('version', 0))

        versions = []
        for key_base, entry in prepared:
            version = latest.get(key_base, 0) + 1
            latest[key_base] = version
            store[f'{key_base}|v{version}'] = dict(entry, version=version)
            versions.append(version)
        jsonstore.write_json(STORE_PATH, store)
    return versions


def to_virtual_report(entry_key: str, entry: dict) -> dict:
    """
    Convert a store entry into a report dict compatible with all checker modules.
    The 'path' is a virtual memory:// URI used as a stable unique key.
    """
    from checker.text_plagiarism import text_fingerprint

    precomputed_images = []
    for item in entry.get('image_data', []) or []:
        if not isinstance(item, dict):
            continue
        hashes = item.get('hashes') or []
        if not isinstance(hashes, (list, tuple)) or not hashes:
            continue
        precomputed_images.append({
            'page':   item.get('page', 0),
            # Keep compact strings in memory. The image checker converts one
            # historical report at a time and drops the ImageHash objects.
            'hashes': [str(value) for value in hashes],
            'thumb':  item['thumb'] if valid_preview(item.get('thumb')) else '',
            'is_ui':  item.get('is_ui', False),
        })

    normalized_text = entry.get('normalized_text', '')
    return {
        'path':               f'memory://{entry_key}',
        'filename':           entry.get('filename', ''),
        'student':            entry.get('student', {}),
        'full_text':          '',
        'normalized_text':    normalized_text,
        'text_hash':          (entry.get('text_hash')
                               or text_fingerprint(normalized_text)),
        'images':             [],
        'precomputed_images': precomputed_images,
        'is_historical':      True,
        'historical_date':    entry.get('added_at', ''),
        'historical_version': entry.get('version', 1),
        'gost_results':       [],
        'pages_count':        entry.get('pages_count', 0),
        'font_info':          {},
        'margin_info':        {},
        'is_scanned':         False,
        'error':              None,
    }
