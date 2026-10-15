"""ADB transport for the tiny shell bridge. No clipboard text is written to disk or logs."""
import json
from pathlib import Path
import queue
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

    def devices(self):
        if not self.adb:
            return []
        result = subprocess.run([self.adb, 'devices', '-l'], capture_output=True, text=True,
                                encoding='utf-8', timeout=6, creationflags=NO_WINDOW)
        devices = []
        for line in result.stdout.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 2 or fields[1] != 'device':
                continue
            model = next((part[6:] for part in fields if part.startswith('model:')), fields[0])
            devices.append({'id': fields[0], 'name': model.replace('_', ' '),
                            'emulator': fields[0].startswith('emulator-')})
        return devices

    def start(self, serial, origin):
        self.close()
        events = queue.Queue(maxsize=1)
        self.events = events
        bridge = self.root / 'dist/syncro-clipboard.jar'
        if not bridge.is_file() or not self.adb:
            raise OSError('Clipboard bridge is not installed')
        subprocess.run([self.adb, '-s', serial, 'push', str(bridge),
                        '/data/local/tmp/syncro-clipboard.jar'], capture_output=True,
                       check=True, timeout=12, creationflags=NO_WINDOW)
        command = ('CLASSPATH=/data/local/tmp/syncro-clipboard.jar app_process / '
                   'dev.syncro.bridge.ClipboardBridge ' + origin)
        self.process = subprocess.Popen([self.adb, '-s', serial, 'shell', command],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
                                        bufsize=1, creationflags=NO_WINDOW)
        startup = queue.Queue(maxsize=1)
        process = self.process
        def receive():
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('type') == 'ready':
                    startup.put(True)
                elif event.get('type') == 'clipboard':
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
        threading.Thread(target=receive, name='syncro-adb-reader', daemon=True).start()
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

    def write(self, text):
        if not self.alive():
            raise OSError('Android disconnected')
        self.process.stdin.write(json.dumps({'text': text}, ensure_ascii=False) + '\n')
        self.process.stdin.flush()

    def close(self):
        process, self.process = self.process, None
        if process:
            try:
                process.stdin.close()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()
        while not self.events.empty():
            try:
                self.events.get_nowait()
            except queue.Empty:
                break
