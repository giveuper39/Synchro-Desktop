"""Android's native ADB QR pairing with local mDNS discovery; no typed ports/codes required."""
import json
from pathlib import Path
import secrets
import shlex
import subprocess
import threading
import time
import uuid

from .pairing import pairing_uri, qr_svg

PAIRING = '_adb-tls-pairing._tcp.local.'
CONNECT = '_adb-tls-connect._tcp.local.'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class WirelessSetup:
    def __init__(self, root, hub, address, token):
        self.root, self.hub, self.address, self.token = Path(root), hub, address, token
        self.lock = threading.RLock()
        self.records = {}
        self.zeroconf = self.browser = None
        self.closed = threading.Event()
        self.state = {'stage': 'idle', 'message': 'Подключите телефон по QR-коду'}
        self.settings = self.root / '.data/wireless-device.json'
        try:
            self.known_serial = json.loads(self.settings.read_text(encoding='utf-8')).get('serial')
        except (ValueError, OSError):
            self.known_serial = None
        self.generation = 0

    def _run(self, *args, input=None, timeout=15):
        if not self.hub.adb.adb:
            raise OSError('ADB component is missing')
        return subprocess.run([self.hub.adb.adb, *args], input=input, text=True,
                              encoding='utf-8', errors='replace', capture_output=True,
                              timeout=timeout, creationflags=NO_WINDOW)

    def _discover(self):
        if self.zeroconf is not None:
            return
        from zeroconf import Zeroconf, ServiceBrowser, IPVersion
        host = self.address.split('://', 1)[1].split(':')[0]
        self.zeroconf = Zeroconf(interfaces=[host], ip_version=IPVersion.V4Only)
        self.browser = ServiceBrowser(self.zeroconf, [PAIRING, CONNECT], self)

    def add_service(self, zeroconf, kind, name):
        from zeroconf import IPVersion
        info = zeroconf.get_service_info(kind, name, timeout=1500)
        if info:
            addresses = info.parsed_addresses(IPVersion.V4Only)
            if addresses:
                with self.lock:
                    self.records[name] = {'kind': kind, 'host': addresses[0], 'port': info.port}

    def update_service(self, zeroconf, kind, name):
        self.add_service(zeroconf, kind, name)

    def remove_service(self, zeroconf, kind, name):
        with self.lock:
            self.records.pop(name, None)

    def status(self):
        with self.lock:
            return dict(self.state)

    def begin(self):
        with self.lock:
            if self.state['stage'] == 'installing':
                return dict(self.state)
            self._discover()
            self.generation += 1
            generation = self.generation
            service, password = 'synchro-' + uuid.uuid4().hex[:16], secrets.token_urlsafe(18)
            payload = f'WIFI:T:ADB;S:{service};P:{password};;'
            self.state = {'stage': 'scan', 'message': 'Сканируйте код в настройках беспроводной отладки',
                          'qrSvg': qr_svg(payload), 'expiresAt': time.time() + 300,
                          'devices': [d for d in self.hub.adb.devices() if not d['emulator']]}
        def pair():
            try:
                deadline = time.monotonic() + 300
                while not self.closed.wait(.3) and generation == self.generation:
                    if time.monotonic() > deadline:
                        self._message('expired', 'QR-код истёк. Обновите его и отсканируйте снова', generation=generation)
                        return
                    with self.lock:
                        match = next((record for name, record in self.records.items()
                                      if name == service + '.' + PAIRING), None)
                    if match is None:
                        continue
                    self._message('pairing', 'Телефон найден. Выполняем защищённое сопряжение…', generation=generation)
                    result = self._run('pair', f"{match['host']}:{match['port']}", input=password+'\n')
                    if result.returncode:
                        raise OSError('Pairing failed')
                    for attempt in range(40):
                        with self.lock:
                            connection = next((record for record in self.records.values()
                                               if record['kind'] == CONNECT and record['host'] == match['host']), None)
                        if connection:
                            device = f"{connection['host']}:{connection['port']}"
                            if self._run('connect', device).returncode == 0:
                                self._message('paired', 'Телефон подключён. Можно установить Synchro', device=device, generation=generation)
                                return
                        if self.closed.wait(.3):
                            return
                    raise OSError('Connect service was not discovered')
            except Exception:
                self._message('error', 'Не удалось завершить сопряжение. Проверьте Wi-Fi и обновите QR-код', generation=generation)
        threading.Thread(target=pair, name='synchro-qr-pair', daemon=True).start()
        return self.status()

    def _message(self, stage, message, generation=None, **fields):
        with self.lock:
            if generation is not None and generation != self.generation:
                return
            self.state.update(stage=stage, message=message, **fields)

    def finish(self, device):
        with self.lock:
            if self.state['stage'] == 'installing':
                return dict(self.state)
        devices = self.hub.adb.devices()
        if not any(d['id'] == device and not d['emulator'] for d in devices):
            raise ValueError('Выберите подключённый телефон')
        with self.lock:
            self.generation += 1
            generation = self.generation
            self._message('installing', 'Устанавливаем и подключаем приложение…', device=device)
        def install():
            try:
                apk = self.root / 'dist/Synchro-Android.apk'
                result = self._run('-s', device, 'install', '-r', str(apk), timeout=120)
                if result.returncode or 'Success' not in result.stdout:
                    raise OSError('Install failed')
                # Quote for Android's POSIX shell. URLs contain '&' and must stay one argument.
                uri = pairing_uri(self.address, self.token)
                command = ('am start -n dev.potplayer.handoff/.MainActivity '
                           '-a android.intent.action.VIEW -d ' + shlex.quote(uri))
                if self._run('-s', device, 'shell', command).returncode:
                    raise OSError('Launch failed')
                serial = self._run('-s', device, 'shell', 'getprop ro.serialno').stdout.strip()
                if serial and len(serial) < 100 and all(c.isalnum() or c in '_-' for c in serial):
                    self.known_serial = serial
                    self.settings.parent.mkdir(parents=True, exist_ok=True)
                    self.settings.write_text(json.dumps({'serial': serial}), encoding='utf-8')
                self.hub.mode({'enabled': True, 'device': device})
                self._message('done', 'Готово. Synchro подключён, автоматический буфер включён', generation=generation)
            except Exception:
                self._message('error', 'Не удалось установить приложение. Можно скачать APK и установить его вручную', generation=generation)
        threading.Thread(target=install, name='synchro-phone-install', daemon=True).start()
        return self.status()

    def reconnect(self):
        if not self.known_serial:
            return None
        with self.lock:
            self._discover()
            record = next((record for name, record in self.records.items()
                           if record['kind'] == CONNECT and name.startswith('adb-' + self.known_serial + '-')), None)
        if record:
            address = f"{record['host']}:{record['port']}"
            result = self._run('connect', address, timeout=6)
            if result.returncode == 0:
                return address
        return None

    def close(self):
        self.closed.set()
        self.generation += 1
        if self.browser:
            self.browser.cancel()
        if self.zeroconf:
            self.zeroconf.close()
