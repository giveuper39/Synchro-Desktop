"""Local-file mpv adapter using bounded Windows JSON IPC named pipes.

The pipe is created only by a separate mpv instance explicitly opened through
Synchro. Per-command overlapped I/O has a deadline, including a stalled peer.
No TCP control socket, volume commands or image-setting writes are used.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import threading
import time

from .models import PlayerSnapshot, PlayerUnavailable
from .playlist import PlaylistSelection
from .local_media import local_media_path as _local_file


class NamedPipeClient:
    """One request per connection; events cannot be mistaken for responses."""
    def __init__(self, name: str, timeout: float = 2):
        self.name, self.timeout = name, timeout

    def command(self, command):
        if os.name != 'nt':
            raise PlayerUnavailable('Управление mpv в этой сборке доступно в Windows')
        import ctypes
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            _fields_ = [('internal', ctypes.c_size_t), ('internal_high', ctypes.c_size_t),
                        ('offset', wintypes.DWORD), ('offset_high', wintypes.DWORD),
                        ('event', wintypes.HANDLE)]

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        kernel.WaitNamedPipeW.restype = wintypes.BOOL
        kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateEventW.restype = wintypes.HANDLE
        for name in ('ReadFile', 'WriteFile'):
            function = getattr(kernel, name)
            function.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(Overlapped)]
            function.restype = wintypes.BOOL
        kernel.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(Overlapped),
                                              ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
        kernel.GetOverlappedResult.restype = wintypes.BOOL
        kernel.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(Overlapped)]
        kernel.CancelIoEx.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        deadline = time.monotonic() + self.timeout
        def remaining():
            return max(1, min(0xFFFFFFFE, math.ceil((deadline - time.monotonic()) * 1000)))

        handle = kernel.CreateFileW(self.name, 0xC0000000, 0, None, 3, 0x40000000, None)
        invalid = ctypes.c_void_p(-1).value
        if handle == invalid and ctypes.get_last_error() == 231:
            if kernel.WaitNamedPipeW(self.name, remaining()):
                handle = kernel.CreateFileW(self.name, 0xC0000000, 0, None, 3, 0x40000000, None)
        if handle == invalid:
            raise PlayerUnavailable('mpv не подключён. Откройте локальный файл через Synchro')

        def transfer(buffer, writing=False):
            event = kernel.CreateEventW(None, True, False, None)
            if not event:
                raise PlayerUnavailable('Windows не создала канал управления mpv')
            pending = Overlapped(event=event)
            count = wintypes.DWORD()
            try:
                operation = kernel.WriteFile if writing else kernel.ReadFile
                ok = operation(handle, buffer, len(buffer) if writing else len(buffer) - 1,
                               ctypes.byref(count), ctypes.byref(pending))
                if not ok:
                    if ctypes.get_last_error() != 997:
                        raise PlayerUnavailable('Соединение с mpv прервано')
                    if kernel.WaitForSingleObject(event, remaining()) != 0:
                        kernel.CancelIoEx(handle, ctypes.byref(pending))
                        # Cancel completion must finish before the stack-owned
                        # OVERLAPPED and its buffer are released.
                        kernel.GetOverlappedResult(handle, ctypes.byref(pending), ctypes.byref(count), True)
                        raise PlayerUnavailable('mpv не ответил за отведённое время')
                    if not kernel.GetOverlappedResult(handle, ctypes.byref(pending), ctypes.byref(count), False):
                        raise PlayerUnavailable('Соединение с mpv прервано')
                return count.value
            finally:
                kernel.CloseHandle(event)

        try:
            request_id = secrets.randbits(48)
            payload = (json.dumps({'command': command, 'request_id': request_id},
                                  ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
            if len(payload) > 131072:
                raise ValueError('mpv IPC request is too large')
            output = ctypes.create_string_buffer(payload, len(payload))
            if transfer(output, writing=True) != len(payload):
                raise PlayerUnavailable('mpv не принял команду целиком')
            received = bytearray()
            while time.monotonic() < deadline:
                block = ctypes.create_string_buffer(8193)
                count = transfer(block)
                if count == 0:
                    break
                received.extend(block.raw[:count])
                if len(received) > 2_000_000:
                    raise PlayerUnavailable('mpv вернул слишком большой ответ')
                while b'\n' in received:
                    line, _, remainder = received.partition(b'\n')
                    received = bytearray(remainder)
                    try:
                        response = json.loads(line)
                    except (ValueError, UnicodeError):
                        raise PlayerUnavailable('mpv вернул некорректный ответ') from None
                    if not isinstance(response, dict) or response.get('request_id') != request_id:
                        continue
                    if response.get('error') != 'success':
                        raise PlayerUnavailable('mpv не смог выполнить команду воспроизведения')
                    return response.get('data')
            raise PlayerUnavailable('mpv не ответил за отведённое время')
        finally:
            kernel.CloseHandle(handle)


class MpvPlayer:
    def __init__(self, root: Path, executable: Path | None = None):
        self.root = Path(root).resolve()
        self.executable = Path(executable) if executable else self.find_executable()
        self.state_path = self.root / '.data' / 'player-connections' / 'mpv.json'
        self.pipe_name = None
        self._process = None
        self.lock = threading.RLock()
        try:
            data = json.loads(self.state_path.read_text(encoding='utf-8'))
            suffix = data['pipe'].removeprefix('\\\\.\\pipe\\synchro-')
            if len(suffix) == 32 and all(char in '0123456789abcdef' for char in suffix):
                self.pipe_name = '\\\\.\\pipe\\synchro-' + suffix
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

    @staticmethod
    def find_executable() -> Path | None:
        candidates = [Path(folder) / sub / 'mpv.exe'
                      for folder in (os.environ.get('ProgramFiles', 'C:/Program Files'),
                                     os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)'))
                      for sub in ('mpv', 'mpv.net')]
        found = shutil.which('mpv')
        if found:
            candidates.append(Path(found))
        return next((path for path in candidates if path.is_file()), None)

    def close(self):
        # IPC connections are scoped to commands. Keep the user's movie open.
        pass

    def _command(self, *arguments):
        if not self.pipe_name:
            raise PlayerUnavailable('Откройте локальное видео в mpv через выбор источника Synchro')
        return NamedPipeClient(self.pipe_name).command(list(arguments))

    def _get(self, name):
        return self._command('get_property', name)

    def _set(self, name, value):
        return self._command('set_property', name, value)

    @staticmethod
    def _number(value):
        try:
            number = float(value)
            if not math.isfinite(number):
                raise ValueError('Non-finite state')
            return number
        except (TypeError, ValueError):
            raise PlayerUnavailable('mpv вернул некорректное состояние воспроизведения') from None

    @staticmethod
    def _tracks(tracks, kind):
        if not isinstance(tracks, list):
            raise PlayerUnavailable('mpv не предоставил список дорожек')
        return [track for track in tracks if isinstance(track, dict)
                and track.get('type') == kind and type(track.get('id')) is int
                and not track.get('external')]

    def read_basic(self):
        with self.lock:
            path = _local_file(self._get('path'))
            if path is None:
                raise PlayerUnavailable('В mpv должен быть открыт доступный локальный медиафайл')
            position = max(0, round(self._number(self._get('time-pos')) * 1000))
            duration = max(0, round(self._number(self._get('duration')) * 1000))
            paused = self._get('pause')
            if type(paused) is not bool:
                raise PlayerUnavailable('mpv не предоставил состояние паузы')
            rate = self._number(self._get('speed'))
            return PlayerSnapshot(path, position, duration, paused, rate=rate)

    def _wait(self, predicate, message, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except PlayerUnavailable:
                if self._process is not None and self._process.poll() is not None:
                    break
            time.sleep(.05)
        raise PlayerUnavailable(message)

    def pause(self):
        with self.lock:
            if self._get('pause') is True:
                return
            self._set('pause', True)
            self._wait(lambda: self._get('pause') is True, 'mpv не подтвердил паузу')

    def capture(self):
        with self.lock:
            before = self.read_basic()
            self.pause()
            current = self.read_basic()
            tracks = self._get('track-list')
            audio, subtitles = self._tracks(tracks, 'audio'), self._tracks(tracks, 'sub')
            if any(track.get('external') and track.get('selected')
                   for track in tracks if isinstance(track, dict) and track.get('type') in {'audio', 'sub'}):
                raise PlayerUnavailable('Для передачи mpv используйте озвучку и субтитры внутри видеофайла')
            audio_index = next((i for i, track in enumerate(audio) if track.get('selected')), -1)
            subtitle_index = next((i for i, track in enumerate(subtitles) if track.get('selected')), -1)
            enabled = subtitle_index >= 0 and self._get('sub-visibility') is True
            delay = round(self._number(self._get('sub-delay')) * 1000)
            if before.path != current.path or _local_file(self._get('path')) != current.path:
                raise PlayerUnavailable('Файл mpv изменился во время передачи; повторите')
            if not current.paused or self._get('pause') is not True:
                raise PlayerUnavailable('mpv возобновил просмотр во время передачи; повторите')
            return PlayerSnapshot(current.path, current.position_ms, current.duration_ms, True,
                                  audio_index, subtitle_index, enabled, delay, current.rate)

    def playlist(self) -> PlaylistSelection:
        with self.lock:
            current = self.read_basic().path
            entries = self._get('playlist')
            if not isinstance(entries, list):
                raise PlayerUnavailable('mpv не предоставил текущую очередь')
            paths, omitted = [], 0
            for entry in entries:
                path = _local_file(entry.get('filename')) if isinstance(entry, dict) else None
                if path is None:
                    omitted += 1
                else:
                    paths.append(path)
            if current not in paths:
                paths.append(current)
            warning = f'Пропущено недоступных файлов и сетевых потоков: {omitted}.' if omitted else None
            return PlaylistSelection(paths, 'mpv-live', warning)

    def launch(self, paths: list[Path]):
        chosen = [_local_file(str(path)) for path in paths]
        if not chosen or len(chosen) > 1000 or any(path is None for path in chosen):
            raise ValueError('Выберите от 1 до 1000 доступных локальных медиафайлов')
        with self.lock:
            try:
                connected = isinstance(self._get('mpv-version'), str)
            except PlayerUnavailable:
                connected = False
            if connected:
                self._command('loadfile', str(chosen[0]), 'replace')
                for path in chosen[1:]:
                    self._command('loadfile', str(path), 'append')
                self._set('pause', False)
            else:
                if self.executable is None or not self.executable.is_file():
                    raise PlayerUnavailable('mpv не установлен. Выберите сборку на mpv.io/installation')
                self.pipe_name = '\\\\.\\pipe\\synchro-' + secrets.token_hex(16)
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                self.state_path.parent.chmod(0o700)
                temporary = self.state_path.with_suffix('.tmp')
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                    json.dump({'pipe': self.pipe_name}, output)
                temporary.replace(self.state_path)
                args = [str(self.executable), '--no-config', '--idle=yes',
                        '--sub-auto=no', '--audio-file-auto=no',
                        f'--input-ipc-server={self.pipe_name}', '--', *map(str, chosen)]
                try:
                    self._process = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                except OSError:
                    raise PlayerUnavailable('Не удалось запустить mpv') from None
            self._wait(lambda: self.read_basic().path == chosen[0],
                       'mpv не открыл выбранный файл', timeout=12)
            return self.read_basic()

    def restore(self, requested: PlayerSnapshot):
        path = _local_file(str(requested.path))
        if path is None or not math.isfinite(requested.rate) or not .2 <= requested.rate <= 12:
            raise ValueError('Некорректное состояние локального файла для mpv')
        with self.lock:
            current = self.read_basic()
            self.pause()
            if current.path != path:
                entries = self._get('playlist')
                if not isinstance(entries, list):
                    raise PlayerUnavailable('mpv не предоставил текущую очередь')
                index = next((i for i, entry in enumerate(entries)
                              if isinstance(entry, dict) and _local_file(entry.get('filename')) == path), None)
                if index is not None:
                    self._set('playlist-pos', index)
                else:
                    self._command('loadfile', str(path), 'replace')
                self._wait(lambda: self.read_basic().path == path,
                           'mpv не переключился на нужный файл')
                self.pause()
            def tracks_ready():
                available = self._get('track-list')
                return (requested.audio_index < len(self._tracks(available, 'audio'))
                        and (not requested.subtitle_enabled
                             or 0 <= requested.subtitle_index < len(self._tracks(available, 'sub'))))
            self._wait(tracks_ready, 'mpv не загрузил нужные дорожки выбранного файла')
            tracks = self._get('track-list')
            audio, subtitles = self._tracks(tracks, 'audio'), self._tracks(tracks, 'sub')
            if requested.audio_index >= len(audio) or requested.audio_index < -1:
                raise PlayerUnavailable('Нужная озвучка отсутствует в файле mpv')
            if requested.subtitle_enabled and not 0 <= requested.subtitle_index < len(subtitles):
                raise PlayerUnavailable('Нужные встроенные субтитры отсутствуют в файле mpv')
            if requested.audio_index >= 0:
                self._set('aid', audio[requested.audio_index]['id'])
            if 0 <= requested.subtitle_index < len(subtitles):
                self._set('sid', subtitles[requested.subtitle_index]['id'])
            elif not requested.subtitle_enabled:
                self._set('sid', 'no')
            self._set('sub-visibility', requested.subtitle_enabled)
            self._set('sub-delay', requested.subtitle_delay_ms / 1000)
            self._set('speed', requested.rate)
            self._command('seek', max(0, requested.position_ms) / 1000, 'absolute+exact')
            self.pause()
            def matches():
                actual = self.capture()
                return (actual.path == path and actual.paused
                        and abs(actual.position_ms - requested.position_ms) <= 250
                        and (requested.audio_index < 0 or actual.audio_index == requested.audio_index)
                        and actual.subtitle_enabled == requested.subtitle_enabled
                        and (not requested.subtitle_enabled or actual.subtitle_index == requested.subtitle_index)
                        and actual.subtitle_delay_ms == requested.subtitle_delay_ms
                        and abs(actual.rate - requested.rate) <= .001)
            self._wait(matches, 'mpv не подтвердил позицию, озвучку и субтитры')
            return self.capture()
