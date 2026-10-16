"""Stable GitHub releases, verified downloads, and an out-of-process portable updater."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.request
import zipfile

from . import __version__

VERSION = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)$')


def version(value):
    match = VERSION.fullmatch(value)
    if not match:
        raise ValueError('Only stable vMAJOR.MINOR.PATCH releases are supported')
    return tuple(map(int, match.groups()))


def release_info(repository, platform, fetch=None):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid GitHub repository')
    request = urllib.request.Request(f'https://api.github.com/repos/{repository}/releases/latest',
                                    headers={'User-Agent': 'Synchro-Updater', 'Accept': 'application/vnd.github+json'})
    with (fetch or urllib.request.urlopen)(request, timeout=20) as response:
        release = json.load(response)
    tag = release['tag_name']
    version(tag)
    if release.get('draft') or release.get('prerelease'):
        raise ValueError('A stable published release is required')
    names = ('Synchro-Windows.zip', 'Syncro-Windows.zip') if platform == 'desktop' else ('Synchro-Android.apk', 'Syncro-Android.apk')
    asset = next((item for name in names for item in release['assets'] if item['name'] == name), None)
    if not asset:
        raise ValueError('Release does not contain the expected build')
    prefix = f'https://github.com/{repository}/releases/download/{tag}/'
    if not asset['browser_download_url'].startswith(prefix):
        raise ValueError('Unexpected release download address')
    digest = asset.get('digest') or ''
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
        raise ValueError('Release asset SHA-256 is missing')
    if not 0 < asset['size'] < 600 * 1024 * 1024:
        raise ValueError('Unexpected release size')
    return {'tag': tag, 'url': asset['browser_download_url'], 'sha256': digest[7:],
            'size': asset['size'], 'releaseUrl': f'https://github.com/{repository}/releases/tag/{tag}'}


def unpack(archive, destination):
    """Extract only a portable distribution; never allow a release to overwrite user state."""
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        if sum(item.file_size for item in entries) > 1500 * 1024 * 1024:
            raise ValueError('Unpacked release is too large')
        for item in entries:
            parts = PurePosixPath(item.filename).parts
            if (not parts or parts[0] != 'Synchro' or any(p.lower() in ('..', '.data') for p in parts)
                    or '\\' in item.filename or ':' in item.filename
                    or (item.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError('Unsafe release archive path')
        source.extractall(destination)
    result = destination / 'Synchro'
    for required in ('BUILD-MANIFEST.json', 'desktop-launcher.py', 'runtime/pythonw.exe', 'server/synchro/__init__.py'):
        if not (result / required).is_file():
            raise ValueError('Incomplete portable release')
    return result


class Updates:
    def __init__(self, root, port=8765):
        self.root = Path(root)
        self.port = port
        self.lock = threading.Lock()
        self.state = {'stage': 'idle', 'message': 'Проверить обновления'}
        self.release = None

    def status(self):
        with self.lock:
            return dict(self.state, current=__version__, portable=(self.root / 'BUILD-MANIFEST.json').is_file())

    def check(self):
        with self.lock:
            if self.state['stage'] in ('checking', 'downloading', 'restarting'):
                return self.status_unlocked()
            self.state = {'stage': 'checking', 'message': 'Проверяем GitHub Releases…'}
        def work():
            try:
                config = json.loads((self.root / 'release-config.json').read_text(encoding='utf-8'))
                release = release_info(config['desktop'], 'desktop')
                available = version(release['tag']) > version(__version__)
                with self.lock:
                    self.release = release if available else None
                    self.state = {'stage': 'available' if available else 'current',
                                  'message': 'Доступна ' + release['tag'] if available else 'Установлена актуальная версия',
                                  'latest': release['tag'], 'releaseUrl': release['releaseUrl']}
            except Exception:
                with self.lock:
                    self.state = {'stage': 'error', 'message': 'Не удалось проверить релиз. Проверьте интернет и повторите.'}
        threading.Thread(target=work, name='synchro-update-check', daemon=True).start()
        return self.status()

    def status_unlocked(self):
        return dict(self.state, current=__version__, portable=(self.root / 'BUILD-MANIFEST.json').is_file())

    def install(self, shutdown):
        with self.lock:
            if not (self.root / 'BUILD-MANIFEST.json').is_file():
                raise ValueError('Автоустановка доступна в переносимой сборке Windows. Исходники обновляются через Git.')
            if self.state['stage'] in ('downloading', 'restarting'):
                return self.status_unlocked()
            if not self.release:
                raise ValueError('Сначала проверьте обновления')
            release = dict(self.release)
            self.state = {'stage': 'downloading', 'message': 'Скачиваем и проверяем новую версию…'}
        def work():
            try:
                stage = Path(tempfile.mkdtemp(prefix='synchro-update-'))
                target = stage / 'update.zip'
                digest = hashlib.sha256()
                count = 0
                with urllib.request.urlopen(release['url'], timeout=30) as response, target.open('wb') as output:
                    while block := response.read(1024 * 1024):
                        count += len(block)
                        if count > release['size']:
                            raise ValueError('Download exceeds release size')
                        digest.update(block)
                        output.write(block)
                if count != release['size'] or digest.hexdigest() != release['sha256']:
                    raise ValueError('Release checksum mismatch')
                source = unpack(target, stage / 'unpacked')
                manifest = json.loads((source / 'BUILD-MANIFEST.json').read_text(encoding='utf-8'))
                if version(manifest['version']) != version(release['tag']):
                    raise ValueError('Release tag and build disagree')
                script = stage / 'apply.ps1'
                shutil.copyfile(self.root / 'tools/apply-update.ps1', script)
                plan = stage / 'plan.json'
                plan.write_text(json.dumps({'root': str(self.root.resolve()), 'source': str(source.resolve()),
                                            'pid': os.getpid(), 'version': manifest['version'], 'port': self.port}, ensure_ascii=False), encoding='utf-8')
                subprocess.Popen(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                  '-File', str(script), '-PlanPath', str(plan)],
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), close_fds=True)
                with self.lock:
                    self.state = {'stage': 'restarting', 'message': 'Перезапускаем Synchro. Страница подключится снова…'}
                shutdown()
            except Exception:
                with self.lock:
                    self.state = {'stage': 'error', 'message': 'Обновление не установлено. Текущая версия сохранена; можно повторить.'}
        threading.Thread(target=work, name='synchro-update-install', daemon=True).start()
        return self.status()
