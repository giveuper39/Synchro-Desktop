"""Portable runtime with replaceable Python libraries, official ADB binaries and no user state."""
from pathlib import Path
import hashlib
import importlib.metadata
import json
import shutil
import sys
import urllib.request
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from potbridge import __version__
PYTHON_VERSION = '3.14.8'
PYTHON_SHA256 = 'a93abe456ab01bd96d7a085b3cdb6566b3063f4241360d114142fbdb07f0a310'


def main():
    parent = ROOT / '.data' / ('windows-package-' + uuid.uuid4().hex)
    stage = parent / 'Синхро'
    runtime = stage / 'runtime'
    runtime.mkdir(parents=True)
    archive = ROOT / '.tools/downloads' / f'python-{PYTHON_VERSION}-embed-amd64.zip'
    url = f'https://www.python.org/ftp/python/{PYTHON_VERSION}/{archive.name}'
    if not archive.is_file():
        archive.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=30) as response:
            archive.write_bytes(response.read())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != PYTHON_SHA256:
        raise ValueError('Official Python archive checksum does not match')
    with zipfile.ZipFile(archive) as source:
        if any('..' in Path(name).parts or Path(name).is_absolute() for name in source.namelist()):
            raise ValueError('Unexpected archive path')
        source.extractall(runtime)
    (runtime / 'python314._pth').write_text('python314.zip\n.\nLib/site-packages\n../server\nimport site\n', encoding='utf-8')
    site = runtime / 'Lib/site-packages'
    site.mkdir(parents=True)
    versions = {}
    for package in ('qrcode', 'zeroconf', 'ifaddr'):
        distribution = importlib.metadata.distribution(package)
        versions[package] = distribution.version
        shutil.copytree(Path(distribution.locate_file(package)), site / package,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        metadata = next(entry.parts[0] for entry in distribution.files if entry.parts[0].endswith('.dist-info'))
        shutil.copytree(Path(distribution.locate_file(metadata)), site / metadata)
    adb = ROOT / '.tools/android-sdk/platform-tools'
    (runtime / 'adb').mkdir()
    for name in ('adb.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll', 'NOTICE.txt'):
        shutil.copyfile(adb / name, runtime / 'adb' / name)
    shutil.copytree(ROOT / 'server', stage / 'server',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.egg-info'))
    shutil.copytree(ROOT / 'chrome-extension', stage / 'chrome-extension',
                    ignore=shutil.ignore_patterns('*.test.mjs', 'package.json'))
    (stage / 'dist').mkdir()
    for name in ('potplayer-receiver.apk', 'syncro-clipboard.jar', 'chrome-extension.zip'):
        shutil.copyfile(ROOT / 'dist' / name, stage / 'dist' / name)
    for name in ('desktop-launcher.py', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'release-config.json'):
        shutil.copyfile(ROOT / name, stage / name)
    (stage / 'tools').mkdir()
    shutil.copyfile(ROOT / 'tools/apply-update.ps1', stage / 'tools/apply-update.ps1')
    (stage / 'Запустить Синхро.cmd').write_text('@echo off\r\ncd /d "%~dp0"\r\nstart "" "runtime\\pythonw.exe" "desktop-launcher.py"\r\n', encoding='ascii')
    (stage / 'ПРОЧИТАЙТЕ.txt').write_text(
        'СИНХРО\n\nРаспакуйте всю папку и запустите «Запустить Синхро.cmd».\n'
        'Откроется мастер: системный QR → установка приложения → готово.\n'
        'Для обычного видео можно выбрать QR приложения без беспроводной отладки.\n\n'
        'Буфер: только последнее копирование; история текстов Синхро не сохраняется.\n'
        'Просмотр: видео продолжится в системном мини-плеере при сворачивании.\n'
        'Вкладки: Opera → Поделиться → Синхро; Chrome → расширение из chrome-extension.\n'
        'Расширение: chrome://extensions → Режим разработчика → Загрузить распакованное.\n\n'
        'Python и ADB уже включены. PotPlayer установите отдельно для функции видео.\n'
        'ffprobe необязателен; он улучшает метаданные дорожек и сопоставление озвучек между сериями.\n'
        'Библиотеки Python расположены отдельно в runtime/Lib/site-packages и могут заменяться.\n'
        'Лицензии и ссылки на исходники: THIRD_PARTY_NOTICES.md, runtime/LICENSE.txt и runtime/adb/NOTICE.txt.\n', encoding='utf-8')
    sources = stage / 'third-party-sources'
    sources.mkdir()
    with urllib.request.urlopen('https://pypi.org/pypi/zeroconf/0.151.5/json', timeout=20) as response:
        metadata = json.load(response)
    source = next(item for item in metadata['urls'] if item['packagetype'] == 'sdist')
    with urllib.request.urlopen(source['url'], timeout=30) as response:
        payload = response.read()
    if hashlib.sha256(payload).hexdigest() != source['digests']['sha256']:
        raise ValueError('zeroconf source checksum mismatch')
    (sources / source['filename']).write_bytes(payload)
    (stage / 'BUILD-MANIFEST.json').write_text(json.dumps({
        'app': 'Синхро', 'version': __version__, 'python': PYTHON_VERSION,
        'pythonSource': url, 'pythonSha256': PYTHON_SHA256, 'packages': versions,
        'adbSource': 'https://android.googlesource.com/platform/packages/modules/adb/',
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    target = ROOT / f'dist/Синхро-Windows-{__version__}.zip'
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as output:
        for path in sorted(stage.rglob('*')):
            if path.is_file() and '.data' not in path.relative_to(stage).parts:
                output.write(path, Path('Синхро') / path.relative_to(stage))
    (ROOT / '.data/windows-stage.txt').write_text(str(stage), encoding='utf-8')
    print(json.dumps({'package': str(target), 'bytes': target.stat().st_size,
                      'stage': str(stage)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
