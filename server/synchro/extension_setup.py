"""Prepare one stable extension directory and the browser's one-time installation screen.

Chrome on personal Windows PCs requires a Web Store install or an explicit
developer-mode load. No browser policy, profile preference or signature is edited.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time


class ExtensionSetup:
    def __init__(self, root, destination=None, clock=time.monotonic):
        self.root, self.clock = Path(root), clock
        local = Path(os.environ.get('LOCALAPPDATA', self.root / '.data'))
        self.directory = Path(destination) if destination else local / 'Synchro/browser-extension'
        self.settings = self.root / '.data/extension-setup.json'
        self.version = json.loads((self.root / 'chrome-extension/manifest.json').read_text(encoding='utf-8-sig'))['version']
        self.clients, self.lock = {}, threading.RLock()
        self.prepared = False

    def prepare(self):
        """Refresh program files without replacing the user's extension installation path."""
        if self.directory.is_symlink():
            raise ValueError('The extension directory must not be a symbolic link')
        self.directory.mkdir(parents=True, exist_ok=True)
        source = self.root / 'chrome-extension'
        files = [p for p in source.iterdir() if p.is_file() and
                 (p.suffix in ('.js', '.html', '.css', '.png', '.svg') or p.name == 'manifest.json')]
        # Publish the manifest last; it describes the complete set of runtime files.
        for path in sorted(files, key=lambda p: p.name == 'manifest.json'):
            target = self.directory / path.name
            if path.is_symlink() or target.is_symlink():
                raise ValueError('Extension files must not be symbolic links')
            payload = path.read_bytes()
            if not target.is_file() or target.read_bytes() != payload:
                temporary = target.with_name(target.name + '.pending')
                temporary.write_bytes(payload)
                temporary.replace(target)
        self.prepared = True

    def first_start(self):
        try:
            if json.loads(self.settings.read_text(encoding='utf-8')).get('installationShown'):
                return False
        except (OSError, ValueError):
            pass
        return True

    def mark_shown(self):
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.settings.with_suffix('.tmp')
        temporary.write_text(json.dumps({'installationShown': True}), encoding='utf-8')
        temporary.replace(self.settings)

    def heartbeat(self, client, version, count):
        if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version):
            raise ValueError('Invalid extension version')
        with self.lock:
            now = self.clock()
            self.clients = {key: value for key, value in self.clients.items() if now - value[0] < 70}
            self.clients[client] = (now, version, count)

    def status(self):
        from .extension import extension_id
        with self.lock:
            clients = [value for value in self.clients.values() if self.clock() - value[0] < 70]
            return dict(connected=bool(clients), youtubeTabs=sum(value[2] for value in clients),
                        version=self.version, installedVersions=sorted({value[1] for value in clients}),
                        folder=str(self.directory), prepared=self.prepared,
                        extensionId=extension_id(),
                        message='Браузер подключён' if clients else 'Подключите расширение Synchro')

    def open(self, action):
        if action == 'folder':
            os.startfile(self.directory)
        elif action == 'extensions':
            candidates = [Path(os.environ.get(name, '')) / 'Google/Chrome/Application/chrome.exe'
                          for name in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA')]
            chrome = next((path for path in candidates if path.is_file()), None)
            if chrome is None:
                raise ValueError('Откройте chrome://extensions в Chrome')
            subprocess.Popen([str(chrome), '--new-tab', 'chrome://extensions/'],
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        else:
            raise ValueError('Unknown browser setup action')
        return dict(ok=True)
