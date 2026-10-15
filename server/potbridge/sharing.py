"""Independent text/URL sharing: a single live clipboard value and a small explicit link inbox."""
from collections import OrderedDict
import json
import hashlib
from pathlib import Path
import subprocess
import threading
import time
from urllib.parse import urlsplit
import uuid


def plain_text(value):
    if not isinstance(value, str) or len(value.encode('utf-16-le')) > 32000 or '\0' in value:
        raise ValueError('Use plain text up to 16000 characters')
    return value


def web_url(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 33 for c in value):
        raise ValueError('Use an HTTP or HTTPS link')
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError()
        parsed.port
    except ValueError:
        raise ValueError('Use an HTTP or HTTPS link') from None
    return value


def open_chrome(url):
    import os
    candidates = [Path(os.environ.get(name, '')) / 'Google/Chrome/Application/chrome.exe'
                  for name in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA')]
    chrome = next((str(path) for path in candidates if path.is_file()), None)
    if chrome is None:
        raise OSError('Google Chrome is not installed')
    subprocess.Popen([chrome, '--new-tab', web_url(url)], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


class DeviceHub:
    def __init__(self, root, clipboard=None, adb=None, opener=open_chrome):
        self.root = Path(root)
        if clipboard is None:
            from .clipboard_windows import WindowsClipboard
            clipboard = WindowsClipboard()
        if adb is None:
            from .adb_clipboard import AdbClipboard
            adb = AdbClipboard(root)
        self.clipboard, self.adb, self.opener = clipboard, adb, opener
        self.origin = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.links = OrderedDict()
        self.receipts = OrderedDict()
        self.enabled, self.serial, self.connected = False, None, False
        self.wireless = None
        self.message, self.last_source, self.last_at = 'Автосинхронизация выключена', None, None
        self.settings = self.root / '.data/sync-settings.json'
        try:
            saved = json.loads(self.settings.read_text(encoding='utf-8'))
            self.enabled, self.serial = saved.get('enabled') is True, saved.get('device')
        except (OSError, ValueError):
            pass
        self.stopped = threading.Event()
        self.worker = threading.Thread(target=self._monitor, name='syncro-clipboard', daemon=True)
        self.worker.start()

    def status(self):
        with self.lock:
            return {'clipboard': {'enabled': self.enabled, 'connected': self.connected,
                    'message': self.message, 'lastSource': self.last_source, 'lastAt': self.last_at},
                    'links': list(self.links.values())}

    def mode(self, data):
        enabled = data.get('enabled')
        if type(enabled) is not bool:
            raise ValueError('enabled must be boolean')
        serial = data.get('device')
        if serial is not None and (not isinstance(serial, str) or len(serial) > 200):
            raise ValueError('Invalid device')
        with self.lock:
            self.enabled, self.serial = enabled, serial
            if not enabled:
                self.connected = False
            self.settings.parent.mkdir(parents=True, exist_ok=True)
            self.settings.write_text(json.dumps({'enabled': enabled, 'device': serial}), encoding='utf-8')
            self.message = 'Подключаем системный буфер…' if enabled else 'Автосинхронизация выключена'
        return self.status()

    def clipboard_write(self, data):
        value = plain_text(data.get('text'))
        self.clipboard.write(value, self.origin)
        with self.lock:
            self.last_source, self.last_at = 'phone', time.time()
        return {'ok': True}

    def clipboard_read(self):
        text, _, _ = self.clipboard.read()
        return {'text': plain_text(text or ''), 'origin': self.origin}

    def send_link(self, data):
        url = web_url(data.get('url'))
        title = data.get('title', '')
        target, request_id = data.get('target'), data.get('requestId')
        if target not in ('pc', 'phone') or not isinstance(title, str) or len(title) > 500:
            raise ValueError('Invalid link destination or title')
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
            raise ValueError('A requestId is required')
        with self.lock:
            if request_id in self.receipts:
                return self.receipts[request_id]
            identity = uuid.uuid4().hex
            if target == 'pc':
                self.opener(url)
            else:
                for old_id, link in list(self.links.items()):
                    if link['url'] == url:
                        del self.links[old_id]
                self.links[identity] = {'id': identity, 'url': url, 'title': title or urlsplit(url).hostname,
                                        'createdAt': time.time()}
                while len(self.links) > 20:
                    self.links.popitem(last=False)
            result = {'ok': True, 'id': identity, 'opened': target == 'pc'}
            self.receipts[request_id] = result
            while len(self.receipts) > 64:
                self.receipts.popitem(last=False)
            return result

    def acknowledge(self, data):
        with self.lock:
            self.links.pop(data.get('id'), None)
        return {'ok': True}

    def _monitor(self):
        sequence = self.clipboard.sequence()
        synced_digest = None
        next_connect = 0
        while not self.stopped.wait(.15):
            try:
                if not self.enabled:
                    if self.adb.alive():
                        self.adb.close()
                    self.connected = False
                    sequence = self.clipboard.sequence()
                    synced_digest = None
                    continue
                if not self.adb.alive():
                    self.connected = False
                    if time.monotonic() < next_connect:
                        continue
                    next_connect = time.monotonic() + 3
                    devices = self.adb.devices()
                    physical = [d for d in devices if not d['emulator']]
                    device = (next((d for d in devices if d['id'] == self.serial), None)
                              if self.serial else (physical[0] if len(physical) == 1 else None))
                    if device is None:
                        address = self.wireless.reconnect() if self.wireless else None
                        if address:
                            self.mode({'enabled': True, 'device': address})
                        self.message = 'Подключите телефон по USB или беспроводной отладке'
                        continue
                    self.adb.start(device['id'], self.origin)
                    if not self.enabled or self.stopped.is_set():
                        self.adb.close()
                        continue
                    # Starting/reconnecting never replays either device's old clipboard.
                    sequence = self.clipboard.sequence()
                    synced_digest = None
                    self.connected = True
                    self.message = 'Системный буфер подключён · ' + device['name']
                text = self.adb.read_latest()
                if text is not None:
                    text = plain_text(text)
                    digest = hashlib.sha256(text.encode('utf-8')).digest()
                    if digest != synced_digest:
                        sequence = self.clipboard.write(text, self.origin)
                        synced_digest = digest
                        self.last_source, self.last_at = 'phone', time.time()
                if self.clipboard.sequence() != sequence:
                    text, observed_sequence, origin = self.clipboard.read()
                    # Clipboard providers can briefly advertise a change before
                    # Unicode data is readable. Retry it instead of losing that copy.
                    if text is not None:
                        sequence = observed_sequence
                    if text is not None and origin != self.origin:
                        text = plain_text(text)
                        digest = hashlib.sha256(text.encode('utf-8')).digest()
                        if digest != synced_digest:
                            self.adb.write(text)
                            synced_digest = digest
                            self.last_source, self.last_at = 'pc', time.time()
            except Exception:
                self.message = 'Соединение с системным буфером прервано; повторяем подключение'
                self.connected = False
                self.adb.close()

    def close(self):
        self.stopped.set()
        self.worker.join(timeout=30)
        self.adb.close()
