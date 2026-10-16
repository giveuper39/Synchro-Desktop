"""Portable Windows entry point. Open the local wizard and keep its service in the background."""
from pathlib import Path
import ctypes
import json
import os
import sys
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'server'))


def main():
    try:
        settings = json.loads((ROOT / '.data/service.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        settings = {}
    port = int(settings.get('port', 8765))
    if not 1 <= port <= 65535:
        raise ValueError('Invalid service port')
    local_url = f'http://127.0.0.1:{port}'
    try:
        with urllib.request.urlopen(local_url + '/api/health', timeout=1) as response:
            # Health requires a key, so a 401 is handled below using its Server header.
            if json.load(response).get('ok'):
                webbrowser.open(local_url + '/setup')
                return
    except urllib.error.HTTPError as error:
        if error.code == 401 and error.headers.get('Server', '').startswith(('Synchro/', 'Syncro/', 'PotPlayerHandoff/')):
            webbrowser.open(local_url + '/setup')
            return
    except (OSError, ValueError):
        pass
    data = ROOT / '.data'
    data.mkdir(exist_ok=True)
    sys.stdout = open(data / 'server.log', 'a', encoding='utf-8', buffering=1)
    sys.stderr = sys.stdout
    sys.argv = ['Synchro', '--port', str(port), '--bind', settings.get('bind', '0.0.0.0')]
    if not os.environ.get('CI'):
        sys.argv.append('--open')
    if settings.get('advertiseHost'):
        sys.argv += ['--advertise-host', settings['advertiseHost']]
    try:
        from synchro.__main__ import main as serve
        serve()
    except Exception:
        import traceback
        traceback.print_exc()
        ctypes.windll.user32.MessageBoxW(None,
            'Не удалось запустить Synchro. Подробности сохранены в .data/server.log рядом с приложением.',
            'Synchro', 0x10)


if __name__ == '__main__':
    main()
