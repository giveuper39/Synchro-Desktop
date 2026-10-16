"""Android's native ADB QR pairing with local mDNS discovery; no typed ports/codes required."""
import json
from pathlib import Path
import secrets
import shlex
import subprocess
import threading
import time
import uuid
from urllib.parse import urlsplit

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

    def _install_device(self, requested, paired):
        """ADB may publish the QR-connected phone under its mDNS name, not IP:port."""
        devices = [d for d in self.hub.adb.devices() if not d['emulator']]
        if any(d['id'] == requested for d in devices):
            return requested
        if requested != paired:
            raise ValueError('Телефон не подключён. Включите беспроводную отладку и повторите подключение')
        result = self._run('-s', requested, 'get-state', timeout=4)
        if result.returncode == 0 and result.stdout.strip() == 'device':
            return requested
        host = urlsplit('http://' + requested).hostname
        with self.lock:
            records = dict(self.records)
        for device in devices:
            name = device['id'].rstrip('.')
            if not name.endswith('.local'):
                name += '.local'
            record = records.get(name + '.')
            if record and record['kind'] == CONNECT and record['host'] == host:
                return device['id']
            if self.known_serial:
                serial = self._run('-s', device['id'], 'shell', 'getprop ro.serialno', timeout=4)
                if serial.returncode == 0 and serial.stdout.strip() == self.known_serial:
                    return device['id']
        raise ValueError('Телефон отключился. Включите беспроводную отладку и подключите его снова')

    def finish(self, device):
        with self.lock:
            if self.state['stage'] == 'installing':
                return dict(self.state)
            paired = self.state.get('device')
            device = device or paired
            if not isinstance(device, str) or not 1 <= len(device) <= 256 or any(ord(c) < 32 for c in device):
                raise ValueError('Выберите подключённый телефон')
            self.generation += 1
            generation = self.generation
            self._message('installing', 'Проверяем подключение телефона…', device=device, phase='checking')
        def install():
            try:
                selected = self._install_device(device, paired)
                apk = self.root / 'dist/Synchro-Android.apk'
                if not apk.is_file():
                    raise ValueError('APK отсутствует на компьютере. Обновите переносимую сборку Synchro')
                self._message('installing', 'Передаём APK на телефон. Дождитесь окончания установки…',
                              generation=generation, phase='installing', device=selected)
                result = self._run('-s', selected, 'install', '-r', str(apk), timeout=120)
                if result.returncode or 'Success' not in result.stdout:
                    reason = result.stdout + (getattr(result, 'stderr', '') or '')
                    if 'INSTALL_FAILED_UPDATE_INCOMPATIBLE' in reason:
                        raise ValueError('Android отклонил обновление: у установленного приложения другая подпись')
                    if 'INSTALL_FAILED_USER_RESTRICTED' in reason:
                        raise ValueError('Android запретил установку. Подтвердите её на телефоне или скачайте APK вручную')
                    if 'INSTALL_FAILED_VERSION_DOWNGRADE' in reason:
                        raise ValueError('На телефоне уже установлена более новая версия Synchro')
                    raise ValueError('Android не установил APK. Проверьте экран телефона или скачайте APK вручную')
                self._message('installing', 'Приложение установлено. Подключаем к компьютеру…',
                              generation=generation, phase='connecting')
                # Quote for Android's POSIX shell. URLs contain '&' and must stay one argument.
                uri = pairing_uri(self.address, self.token)
                command = ('am start -n dev.potplayer.handoff/.MainActivity '
                           '-a android.intent.action.VIEW -d ' + shlex.quote(uri))
                if self._run('-s', selected, 'shell', command).returncode:
                    raise OSError('Launch failed')
                serial = self._run('-s', selected, 'shell', 'getprop ro.serialno').stdout.strip()
                if serial and len(serial) < 100 and all(c.isalnum() or c in '_-' for c in serial):
                    self.known_serial = serial
                    self.settings.parent.mkdir(parents=True, exist_ok=True)
                    self.settings.write_text(json.dumps({'serial': serial}), encoding='utf-8')
                self.hub.mode({'enabled': True, 'device': selected})
                self._message('done', 'Готово. Synchro подключён, автоматический буфер включён', generation=generation)
            except ValueError as error:
                self._message('error', str(error), generation=generation, retryable=True)
            except subprocess.TimeoutExpired:
                self._message('error', 'Телефон не ответил вовремя. Повторите установку или скачайте APK вручную',
                              generation=generation, retryable=True)
            except Exception:
                self._message('error', 'Не удалось подключить приложение. Проверьте телефон и повторите установку',
                              generation=generation, retryable=True)
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
                try:
                    return self._install_device(address, address)
                except (ValueError, OSError, subprocess.SubprocessError):
                    return None
        return None

    def close(self):
        self.closed.set()
        self.generation += 1
        if self.browser:
            self.browser.cancel()
        if self.zeroconf:
            self.zeroconf.close()
