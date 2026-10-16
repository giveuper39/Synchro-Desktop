"""One live text/image clipboard value and a small explicit link inbox."""
from collections import OrderedDict
import json
from pathlib import Path
import subprocess
import threading
import time
from urllib.parse import urlsplit
import uuid

from .clipboard_images import MAX_IMAGE_BYTES, content_digest, image_content


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
        # Clipboard transactions share one digest, including explicit HTTP writes.
        # A separate lock keeps slow image transfers from blocking status reads.
        self.clipboard_lock = threading.RLock()
        self.synced_digest = None
        self.links = OrderedDict()
        self.receipts = OrderedDict()
        self.enabled, self.serial, self.connected = False, None, False
        self.connection_revision = 0
        self.wireless = None
        self.message = 'Автосинхронизация выключена'
        self.connection_message = 'Системный буфер подключён'
        self.last_source, self.last_type, self.last_at = None, None, None
        self.settings = self.root / '.data/sync-settings.json'
        try:
            saved = json.loads(self.settings.read_text(encoding='utf-8'))
            self.enabled, self.serial = saved.get('enabled') is True, saved.get('device')
        except (OSError, ValueError):
            pass
        self.stopped = threading.Event()
        self.worker = threading.Thread(target=self._monitor, name='synchro-clipboard', daemon=True)
        self.worker.start()

    def status(self):
        with self.lock:
            return {'clipboard': {'enabled': self.enabled, 'connected': self.connected,
                    'message': self.message, 'lastSource': self.last_source,
                    'lastType': self.last_type, 'lastAt': self.last_at,
                    'imagesSupported': getattr(self.adb, 'images_supported', None),
                    'bridgeError': getattr(self.adb, 'last_error', None)},
                    'links': list(self.links.values())}

    def mode(self, data):
        enabled = data.get('enabled')
        if type(enabled) is not bool:
            raise ValueError('enabled must be boolean')
        serial = data.get('device')
        if serial is not None and (not isinstance(serial, str) or len(serial) > 200):
            raise ValueError('Invalid device')
        with self.lock:
            self.connection_revision += 1
            self.enabled, self.serial = enabled, serial
            if not enabled:
                self.connected = False
            self._save_settings()
            self.message = 'Подключаем системный буфер…' if enabled else 'Автосинхронизация выключена'
        return self.status()

    def _save_settings(self):
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.settings.with_suffix('.tmp')
        temporary.write_text(json.dumps({'enabled': self.enabled, 'device': self.serial}), encoding='utf-8')
        temporary.replace(self.settings)

    def clipboard_write(self, data):
        return self._apply_phone_content({'type': 'text', 'text': plain_text(data.get('text'))})

    def clipboard_read(self):
        with self.clipboard_lock:
            content, _, _ = self._read_pc_content()
        if content is not None and content['type'] == 'image':
            # Image bytes use the binary endpoint; ordinary status never contains them.
            return {'type': 'image', 'mime': 'image/png', 'origin': self.origin}
        return {'type': 'text', 'text': content['text'] if content else '', 'origin': self.origin}

    def clipboard_image_read(self):
        with self.clipboard_lock:
            content, _, _ = self._read_pc_content()
        if content is None or content['type'] != 'image':
            raise ValueError('The clipboard does not contain an image')
        return content['data'], self.origin

    def clipboard_image_write(self, payload):
        return self._apply_phone_content(image_content(payload))

    @staticmethod
    def _content(content):
        """Validate the common adapter shape without decoding an image twice."""
        if content is None:
            return None
        if not isinstance(content, dict):
            raise ValueError('Unsupported clipboard content')
        if content.get('type') == 'text':
            return {'type': 'text', 'text': plain_text(content.get('text'))}
        if (content.get('type') == 'image' and content.get('mime') == 'image/png'
                and isinstance(content.get('data'), bytes)
                and 0 < len(content['data']) <= MAX_IMAGE_BYTES):
            return {'type': 'image', 'mime': 'image/png', 'data': content['data']}
        raise ValueError('Unsupported clipboard content')

    def _read_pc_content(self):
        if hasattr(self.clipboard, 'read_content'):
            content, sequence, origin = self.clipboard.read_content()
        else:
            text, sequence, origin = self.clipboard.read()
            content = {'type': 'text', 'text': text} if text is not None else None
        return self._content(content), sequence, origin

    def _write_pc_content(self, content):
        if hasattr(self.clipboard, 'write_content'):
            return self.clipboard.write_content(content, self.origin)
        if content['type'] == 'text':
            return self.clipboard.write(content['text'], self.origin)
        raise OSError('This clipboard adapter does not support images')

    def _read_phone_content(self):
        if hasattr(self.adb, 'read_latest_content'):
            return self._content(self.adb.read_latest_content())
        text = self.adb.read_latest()
        return self._content({'type': 'text', 'text': text}) if text is not None else None

    def _send_phone_content(self, content):
        if hasattr(self.adb, 'send_content'):
            return self.adb.send_content(content, self.origin)
        if hasattr(self.adb, 'write_content'):
            return self.adb.write_content(content, self.origin)
        if content['type'] == 'text':
            return self.adb.write(content['text'])
        raise OSError('This Android adapter does not support images')

    def _record_clipboard(self, source, kind):
        with self.lock:
            self.last_source, self.last_type, self.last_at = source, kind, time.time()

    def _apply_phone_content(self, content):
        with self.clipboard_lock:
            self._write_pc_content(content)
            self.synced_digest = content_digest(content)
            self._record_clipboard('phone', content['type'])
        return {'ok': True}

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
        next_connect = 0
        observed_revision = -1
        while not self.stopped.wait(.15):
            try:
                with self.lock:
                    enabled, serial, revision = self.enabled, self.serial, self.connection_revision
                if revision != observed_revision:
                    next_connect, observed_revision = 0, revision
                if not enabled:
                    if self.adb.alive():
                        self.adb.close()
                    self.connected = False
                    with self.clipboard_lock:
                        sequence = self.clipboard.sequence()
                        self.synced_digest = None
                    continue
                if self.adb.alive() and serial and getattr(self.adb, 'serial', serial) != serial:
                    self.adb.close()
                    self.connected = False
                if not self.adb.alive():
                    self.connected = False
                    if time.monotonic() < next_connect:
                        continue
                    next_connect = time.monotonic() + 3
                    devices = self.adb.devices()
                    physical = [d for d in devices if not d['emulator']]
                    device = (next((d for d in devices if d['id'] == serial), None)
                              if serial else (physical[0] if len(physical) == 1 else None))
                    if device is None:
                        address = self.wireless.reconnect() if self.wireless else None
                        if address:
                            device = next((d for d in devices if d['id'] == address),
                                          {'id': address, 'name': 'Ваш телефон'})
                        else:
                            with self.lock:
                                if self.enabled and revision == self.connection_revision:
                                    self.message = 'Подключите телефон по USB или беспроводной отладке'
                            continue
                    with self.lock:
                        if not self.enabled or revision != self.connection_revision:
                            continue
                        # Persist the usable ADB identity without re-enabling a
                        # mode the user may have disabled during discovery.
                        self.serial = device['id']
                        self._save_settings()
                    self.adb.start(device['id'], self.origin)
                    if not self.enabled or revision != self.connection_revision or self.stopped.is_set():
                        self.adb.close()
                        continue
                    # Starting/reconnecting never replays either device's old clipboard.
                    with self.clipboard_lock:
                        sequence = self.clipboard.sequence()
                        self.synced_digest = None
                    self.connected = True
                    self.message = 'Системный буфер подключён · ' + device['name']
                    if getattr(self.adb, 'images_supported', None) is False:
                        self.message += ' · Android не предоставил фоновый доступ к фото'
                    self.connection_message = self.message
                if getattr(self.adb, 'last_error', None):
                    self.message = 'Android не предоставил доступ к последней копии; попробуйте скопировать её снова'
                elif self.connected:
                    self.message = self.connection_message
                with self.clipboard_lock:
                    content = self._read_phone_content()
                    if content is not None:
                        digest = content_digest(content)
                        if digest != self.synced_digest:
                            sequence = self._write_pc_content(content)
                            self.synced_digest = digest
                            self._record_clipboard('phone', content['type'])
                    if self.clipboard.sequence() != sequence:
                        content, observed_sequence, origin = self._read_pc_content()
                        # Providers can advertise a change before its data is
                        # readable. Retry rather than losing that text or image.
                        if content is not None:
                            sequence = observed_sequence
                        if content is not None and origin != self.origin:
                            digest = content_digest(content)
                            if digest != self.synced_digest:
                                self._send_phone_content(content)
                                self.synced_digest = digest
                                self._record_clipboard('pc', content['type'])
            except Exception:
                self.message = 'Соединение с системным буфером прервано; повторяем подключение'
                self.connected = False
                self.adb.close()

    def close(self):
        self.stopped.set()
        self.worker.join(timeout=30)
        self.adb.close()
