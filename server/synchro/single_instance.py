"""One desktop controller per Windows logon, across folders and listening ports."""
import atexit
import ctypes as ct
from ctypes import wintypes as wt
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request
import webbrowser


def service_ready(port):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{int(port)}/api/health', timeout=.7) as response:
            return response.headers.get('Server', '').startswith(('Synchro/', 'Syncro/', 'PotPlayerHandoff/'))
    except urllib.error.HTTPError as error:
        return error.code == 401 and error.headers.get('Server', '').startswith(('Synchro/', 'Syncro/', 'PotPlayerHandoff/'))
    except (OSError, ValueError):
        return False


class ServiceInstance:
    def __init__(self, name='Local\\Synchro.Desktop.Controller.v1', record=None):
        self.record = Path(record) if record else Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'synchro/service.json'
        self.handle = None
        self.owned = False
        self.api = None
        if os.name == 'nt':
            self.api = ct.WinDLL('kernel32', use_last_error=True)
            self.api.CreateMutexW.argtypes, self.api.CreateMutexW.restype = [ct.c_void_p, wt.BOOL, wt.LPCWSTR], wt.HANDLE
            self.api.WaitForSingleObject.argtypes, self.api.WaitForSingleObject.restype = [wt.HANDLE, wt.DWORD], wt.DWORD
            self.api.ReleaseMutex.argtypes, self.api.ReleaseMutex.restype = [wt.HANDLE], wt.BOOL
            self.api.CloseHandle.argtypes, self.api.CloseHandle.restype = [wt.HANDLE], wt.BOOL
            self.handle = self.api.CreateMutexW(None, False, name)
            if not self.handle:
                raise OSError(ct.get_last_error(), 'Cannot create the Synchro service lock')
            self.owned = self.api.WaitForSingleObject(self.handle, 0) in (0, 0x80)
        else:
            self.owned = True

    def publish(self, port):
        if not self.owned:
            raise RuntimeError('Only the running controller can publish its address')
        self.record.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.record.with_suffix('.tmp')
        temporary.write_text(json.dumps({'pid': os.getpid(), 'port': int(port)}), encoding='utf-8')
        temporary.replace(self.record)

    def existing_port(self, fallback=8765):
        for attempt in range(10):
            try:
                port = int(json.loads(self.record.read_text(encoding='utf-8'))['port'])
                if 1 <= port <= 65535 and service_ready(port):
                    return port
            except (OSError, ValueError, KeyError, TypeError):
                pass
            if service_ready(fallback):
                return fallback
            time.sleep(.15)
        return fallback

    def close(self):
        if self.owned:
            try:
                data = json.loads(self.record.read_text(encoding='utf-8'))
                if data.get('pid') == os.getpid():
                    self.record.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
            if self.api and self.handle:
                self.api.ReleaseMutex(self.handle)
            self.owned = False
        if self.api and self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def claim_service(port, open_browser=True):
    instance = ServiceInstance()
    # An older installation may not yet own the mutex. Detect it before creating
    # any player, watchdog or second HTTP listener.
    existing = next((candidate for candidate in dict.fromkeys((port, 8765)) if service_ready(candidate)), None)
    if not instance.owned or existing:
        current = existing or instance.existing_port(port)
        instance.close()
        if open_browser:
            webbrowser.open(f'http://127.0.0.1:{current}/setup')
        return None
    atexit.register(instance.close)
    return instance
