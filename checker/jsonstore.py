"""Atomic JSON documents and a recoverable transaction for multi-file updates."""

import json
import os
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_RETRIES = 3
_RETRY_PAUSE = 0.05
# All stores share this lock. A reader sees the entire old or new migration.
lock = threading.RLock()
_journal = None


def read_json(path: Path, fallback):
    with lock:
        for attempt in range(_RETRIES):
            try:
                with Path(path).open(encoding='utf-8') as stream:
                    value = json.load(stream)
                expected = dict if fallback is None else type(fallback)
                if not isinstance(value, expected):
                    raise ValueError(f'Неверный тип данных в {path}')
                return value
            except FileNotFoundError:
                return fallback
            except (OSError, ValueError):
                if attempt + 1 == _RETRIES:
                    raise
                time.sleep(_RETRY_PAUSE)


def _remember(path: Path):
    """Save the pre-transaction inode before its first replacement/deletion."""
    if _journal is None:
        return
    rel = path.resolve().relative_to(_journal.parent)
    before = _journal / 'before' / rel
    absent = _journal / 'absent' / rel
    if before.exists() or absent.exists():
        return
    if path.exists():
        before.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(path, before)
        except OSError:
            # Some shared folders do not support hard links. A partial copy
            # must never become a rollback image.
            with tempfile.NamedTemporaryFile(dir=_journal, delete=False) as tmp:
                with path.open('rb') as src:
                    shutil.copyfileobj(src, tmp)
            os.replace(tmp.name, before)
    else:
        absent.parent.mkdir(parents=True, exist_ok=True)
        absent.touch()


def _finish(journal):
    # The rename is the commit point. A crash during garbage collection
    # cannot turn a completed commit into an incomplete rollback.
    garbage = journal.with_name('.transaction-finished')
    journal.replace(garbage)
    shutil.rmtree(garbage, ignore_errors=True)


def recover(root: Path):
    with lock:
        root = Path(root)
        garbage = root / '.transaction-finished'
        if garbage.exists():
            shutil.rmtree(garbage)
        journal = root / '.transaction'
        if not journal.exists():
            return
        for marker in (journal / 'absent').rglob('*'):
            if marker.is_file():
                (root / marker.relative_to(journal / 'absent')).unlink(missing_ok=True)
        for before in (journal / 'before').rglob('*'):
            if before.is_file():
                path = root / before.relative_to(journal / 'before')
                # Each move is atomic. Recovery itself can be interrupted
                # and resumed because finished files are already restored.
                before.replace(path)
        _finish(journal)


@contextmanager
def transaction(root: Path):
    global _journal
    with lock:
        if _journal is not None:
            yield
            return
        root = Path(root).resolve()
        recover(root)
        root.mkdir(exist_ok=True, parents=True)
        journal = root / '.transaction'
        journal.mkdir(mode=0o700)
        _journal = journal
        try:
            yield
            _finish(journal)
        except BaseException:
            recover(root)
            raise
        finally:
            _journal = None


def unlink(path: Path, missing_ok=False):
    with lock:
        path = Path(path)
        _remember(path)
        path.unlink(missing_ok=missing_ok)


def write_json(path: Path, value) -> None:
    with lock:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump(value, out, ensure_ascii=False, indent=2, allow_nan=False)
                out.flush()
                os.fsync(out.fileno())
            _remember(path)
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)
