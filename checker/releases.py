"""Локальная история выпусков и ручная проверка стабильной версии."""
import json
import re
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

CHANGELOG = json.loads(Path(__file__).with_name('changelog.json').read_text(encoding='utf-8'))
APP_VERSION = CHANGELOG[0]['version']
RELEASES_URL = 'https://github.com/mxd3v-tech/ALENA/releases'


def version_parts(version):
    if not isinstance(version, str) or not re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}', version):
        raise ValueError('Ожидается номер версии X.Y.Z')
    return tuple(map(int, version.split('.')))


def check_update():
    request = Request('https://api.github.com/repos/mxd3v-tech/ALENA/releases/latest',
                      headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'ALENA'})
    try:
        with urlopen(request, timeout=5) as response:
            release = json.load(response)
        tag = release.get('tag_name') if isinstance(release, dict) else None
        latest = tag.removeprefix('v') if isinstance(tag, str) else ''
        available = version_parts(latest)
    except HTTPError as exc:
        exc.close()
        if exc.code == 404:
            return {'message': 'Публичный стабильный выпуск на GitHub не найден.'}
        raise
    if available > version_parts(APP_VERSION):
        return {'message': f'Доступна версия {latest}.', 'url': f'{RELEASES_URL}/tag/{tag}'}
    return {'message': f'Более новой версии нет. Последний выпуск на GitHub — {latest}.'}


if __name__ == '__main__':
    release = CHANGELOG[0]
    print('### Что нового\n')
    for change in release['changes']:
        print(f'- {change}')
    if release.get('commits'):
        print('\n### История коммитов\n')
        for commit in release['commits']:
            sha = commit['sha']
            print(f"- {commit['description']} [{sha[:7]}](https://github.com/mxd3v-tech/ALENA/commit/{sha})")
