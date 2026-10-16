"""Latest-only ADB clipboard transport. Text stays in memory; images use one temporary slot."""
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading

NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class AdbClipboard:
    def __init__(self, root):
        self.root = Path(root)
        bundled = self.root / '.tools/android-sdk/platform-tools/adb.exe'
        portable = self.root / 'runtime/adb/adb.exe'
        self.adb = str(portable) if portable.is_file() else (str(bundled) if bundled.is_file() else shutil.which('adb'))
        self.process = None
        self.events = queue.Queue(maxsize=1)
        self.serial = None
        self.origin = None
        self.send_lock = threading.RLock()

    def devices(self):
        if not self.adb:
            return []
        result = subprocess.run([self.adb, 'devices', '-l'], capture_output=True, text=True,
                                encoding='utf-8', timeout=6, creationflags=NO_WINDOW)
        devices = []
        for line in result.stdout.splitlines()[1:]:
            match = re.fullmatch(r'(.+?)\s+(device|offline|unauthorized)(?:\s+(.*))?', line.strip())
            if not match or match[2] != 'device':
                continue
            identity, metadata = match[1], (match[3] or '').split()
            fields = [identity, 'device', *metadata]
            model = next((part[6:] for part in fields if part.startswith('model:')), fields[0])
            devices.append({'id': fields[0], 'name': model.replace('_', ' '),
                            'emulator': fields[0].startswith('emulator-')})
        return devices

    def start(self, serial, origin):
        self.close()
        if not isinstance(origin, str) or not re.fullmatch('[a-f0-9]{32}', origin):
            raise ValueError('Invalid clipboard origin')
        events = queue.Queue(maxsize=1)
        self.events = events
        bridge = self.root / 'dist/Synchro-Clipboard.jar'
        if not bridge.is_file() or not self.adb:
            raise OSError('Clipboard bridge is not installed')
        subprocess.run([self.adb, '-s', serial, 'push', str(bridge),
                        '/data/local/tmp/Synchro-Clipboard.jar'], capture_output=True,
                       check=True, timeout=12, creationflags=NO_WINDOW)
        command = ('CLASSPATH=/data/local/tmp/Synchro-Clipboard.jar app_process / '
                   'dev.synchro.bridge.ClipboardBridge ' + origin)
        self.process = subprocess.Popen([self.adb, '-s', serial, 'shell', command],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
                                        bufsize=1, creationflags=NO_WINDOW)
        self.serial, self.origin = serial, origin
        startup = queue.Queue(maxsize=1)
        process = self.process
        def receive():
            from .clipboard_images import MAX_EVENT_CHARACTERS
            while True:
                line = process.stdout.readline(MAX_EVENT_CHARACTERS + 1)
                if not line:
                    break
                if len(line) > MAX_EVENT_CHARACTERS:
                    process.terminate()
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get('type') == 'ready':
                    startup.put(True)
                elif event.get('type') in {'clipboard', 'image'}:
                    # A disconnected/slow consumer retains only the newest value.
                    try:
                        events.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        events.put_nowait(event)
                    except queue.Full:
                        pass
            try:
                startup.put_nowait(False)
            except queue.Full:
                pass
        threading.Thread(target=receive, name='synchro-adb-reader', daemon=True).start()
        if not startup.get(timeout=8):
            self.close()
            raise OSError('Android clipboard permission is unavailable')

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def read_latest(self):
        try:
            return self.events.get_nowait().get('text')
        except queue.Empty:
            return None

    def read_latest_content(self):
        from .clipboard_images import event_content
        try:
            return event_content(self.events.get_nowait())
        except queue.Empty:
            return None

    def write(self, text):
        with self.send_lock:
            if not self.alive():
                raise OSError('Android disconnected')
            self.process.stdin.write(json.dumps({'text': text}, ensure_ascii=False) + '\n')
            self.process.stdin.flush()

    def send_content(self, content, origin=None):
        if content.get('type') == 'text':
            return self.write(content['text'])
        from .clipboard_images import image_content
        if content.get('type') != 'image' or content.get('mime') != 'image/png':
            raise ValueError('Unsupported clipboard content')
        image = image_content(content['data'])
        marker = origin or self.origin
        if not isinstance(marker, str) or not re.fullmatch('[a-f0-9]{32}', marker):
            raise ValueError('Invalid clipboard origin')
        with self.send_lock:
            if not self.alive() or not self.serial:
                raise OSError('Android disconnected')
            directory = self.root / '.data' / 'clipboard'
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)
            current = directory / 'current.png'
            descriptor = os.open(current, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(descriptor, 'wb') as output:
                    output.write(image['data'])
                base = [self.adb, '-s', self.serial]
                remote = '/sdcard/Android/data/dev.potplayer.handoff/files/clipboard'
                subprocess.run(base + ['shell', 'mkdir', '-p', remote], capture_output=True,
                               check=True, timeout=8, creationflags=NO_WINDOW)
                subprocess.run(base + ['push', str(current), remote + '/from-pc.png'],
                               capture_output=True, check=True, timeout=25, creationflags=NO_WINDOW)
                result = subprocess.run(base + ['shell', 'am', 'broadcast', '--receiver-foreground',
                    '-a', 'dev.potplayer.handoff.APPLY_CLIPBOARD_IMAGE', '-n',
                    'dev.potplayer.handoff/dev.synchro.app.ClipboardImageReceiver',
                    '--es', 'origin', marker], capture_output=True, text=True,
                    encoding='utf-8', timeout=15, creationflags=NO_WINDOW)
                if result.returncode or not re.search(r'Broadcast completed: result=-1\b', result.stdout):
                    raise OSError('Android did not accept the clipboard image')
            except subprocess.SubprocessError:
                raise OSError('Clipboard image transfer failed') from None
            finally:
                current.unlink(missing_ok=True)

    def write_content(self, content, origin=None):
        return self.send_content(content, origin)

    def close(self):
        process, self.process = self.process, None
        if process:
            try:
                process.stdin.close()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()
        self.serial, self.origin = None, None
        while not self.events.empty():
            try:
                self.events.get_nowait()
            except queue.Empty:
                break
