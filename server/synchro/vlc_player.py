"""A separate VLC instance controlled through an authenticated loopback API.

VLC's bundled HTTP modules supply the protocol implementation. Our tiny HTTP
root adds selected track IDs and precise time, absent from stock status.json.
Only explicitly chosen local files are opened; volume and image settings never
cross the adapter boundary.
"""
from __future__ import annotations

import base64
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler

from .models import PlayerSnapshot, PlayerUnavailable
from .playlist import PlaylistSelection
from .local_media import local_media_path as _local_file


def _secret_blob(payload: bytes, decrypt: bool = False) -> bytes:
    """Use Windows per-user DPAPI for the persisted HTTP password."""
    if os.name != 'nt':
        return payload
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(payload)
    source = Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    method = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    method.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                       ctypes.POINTER(Blob)]
    method.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not method(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError('Windows could not protect the VLC connection')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


class VlcPlayer:
    def __init__(self, root: Path, executable: Path | None = None):
        self.root = Path(root).resolve()
        self.executable = Path(executable) if executable else self.find_executable()
        self.data_dir = self.root / '.data' / 'player-connections'
        self.state_path = self.data_dir / 'vlc.json'
        self.port = None
        self._password = None
        self._process = None
        self.lock = threading.RLock()
        # Private loopback requests must not be routed through system proxies.
        self._http = build_opener(ProxyHandler({}))
        self._load_connection()

    @staticmethod
    def find_executable() -> Path | None:
        candidates = [Path(folder) / 'VideoLAN' / 'VLC' / 'vlc.exe'
                      for folder in (os.environ.get('ProgramFiles', 'C:/Program Files'),
                                     os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)'))]
        found = shutil.which('vlc')
        if found:
            candidates.append(Path(found))
        return next((path for path in candidates if path.is_file()), None)

    def close(self):
        # The user may continue watching after the service exits.
        self._http.close()

    def _load_connection(self):
        try:
            data = json.loads(self.state_path.read_text(encoding='utf-8'))
            port = data['port']
            if type(port) is not int or not 1024 <= port <= 65535:
                return
            password = _secret_blob(base64.b64decode(data['password']), decrypt=True).decode('ascii')
            if len(password) != 64 or not password.isalnum():
                return
            self.port, self._password = port, password
        except (OSError, ValueError, KeyError, TypeError, UnicodeError):
            pass

    def _save_connection(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.chmod(0o700)
        data = {'port': self.port,
                'password': base64.b64encode(_secret_blob(self._password.encode('ascii'))).decode('ascii')}
        temporary = self.state_path.with_suffix('.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(data, output)
        temporary.replace(self.state_path)

    def _request(self, resource='status', **parameters):
        if not self.port or not self._password:
            raise PlayerUnavailable('Откройте локальное видео в VLC через выбор источника Synchro')
        query = '?' + urlencode(parameters) if parameters else ''
        request = Request(f'http://127.0.0.1:{self.port}/requests/{resource}.json{query}')
        credentials = base64.b64encode((':' + self._password).encode('ascii')).decode('ascii')
        request.add_header('Authorization', 'Basic ' + credentials)
        try:
            with self._http.open(request, timeout=2) as response:
                payload = response.read(2_000_001)
            if len(payload) > 2_000_000:
                raise ValueError('Response too large')
            data = json.loads(payload)
            if not isinstance(data, dict):
                raise ValueError('Expected object')
            return data
        except (OSError, ValueError):
            # Never include an authenticated request, media URI or password.
            raise PlayerUnavailable('VLC не отвечает; откройте файл через Synchro') from None

    @staticmethod
    def _tracks(status, name):
        tracks = status.get(name, [])
        if tracks == {}:  # dkjson serializes an empty Lua table as an object.
            return []
        if not isinstance(tracks, list):
            raise PlayerUnavailable('VLC вернул некорректный список дорожек')
        return [track for track in tracks if isinstance(track, dict)
                and type(track.get('id')) is int and track['id'] >= 0]

    @classmethod
    def _snapshot(cls, status):
        if status.get('protocol') != 1:
            raise PlayerUnavailable('Этот экземпляр VLC открыт без адаптера Synchro')
        path = _local_file(status.get('uri'))
        if path is None or status.get('state') not in {'playing', 'paused'}:
            raise PlayerUnavailable('В VLC должен быть открыт доступный локальный медиафайл')
        audio = cls._tracks(status, 'audioTracks')
        subtitles = cls._tracks(status, 'subtitleTracks')
        def index(tracks, selected):
            return next((i for i, track in enumerate(tracks) if track['id'] == selected), -1)
        audio_index = index(audio, status.get('audioId'))
        subtitle_index = index(subtitles, status.get('subtitleId'))
        try:
            position = float(status.get('timeMs', 0))
            duration = float(status.get('durationMs', 0))
            delay = float(status.get('subtitleDelayMs', 0))
            rate = float(status.get('rate', 1))
            if not all(math.isfinite(value) for value in (position, duration, delay, rate)):
                raise ValueError('Non-finite playback state')
        except (ValueError, TypeError):
            raise PlayerUnavailable('VLC вернул некорректную позицию воспроизведения') from None
        return PlayerSnapshot(path, max(0, round(position)), max(0, round(duration)),
                              status['state'] == 'paused', audio_index, subtitle_index,
                              subtitle_index >= 0, round(delay), rate)

    def read_basic(self):
        with self.lock:
            return self._snapshot(self._request())

    def _wait(self, predicate, message, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                status = self._request()
                if predicate(status):
                    return status
            except PlayerUnavailable:
                if self._process is not None and self._process.poll() is not None:
                    break
            time.sleep(.05)
        raise PlayerUnavailable(message)

    def pause(self):
        with self.lock:
            current = self._snapshot(self._request())
            if current.paused:
                return
            self._request(command='pl_forcepause')
            self._wait(lambda data: data.get('state') == 'paused', 'VLC не подтвердил паузу')

    def capture(self):
        with self.lock:
            before = self.read_basic()
            self.pause()
            captured = self.read_basic()
            if before.path != captured.path:
                raise PlayerUnavailable('Файл VLC изменился во время передачи; повторите')
            if not captured.paused:
                raise PlayerUnavailable('VLC возобновил просмотр во время передачи; повторите')
            return captured

    def playlist(self) -> PlaylistSelection:
        with self.lock:
            current = self.read_basic().path
            paths, omitted = [], 0
            def visit(node):
                nonlocal omitted
                if not isinstance(node, dict):
                    return
                if node.get('type') == 'leaf':
                    path = _local_file(node.get('uri'))
                    if path is None:
                        omitted += 1
                    else:
                        paths.append(path)
                children = node.get('children', [])
                for child in children if isinstance(children, list) else []:
                    visit(child)
            visit(self._request('playlist'))
            if current not in paths:
                paths.append(current)
            warning = f'Пропущено недоступных файлов и сетевых потоков: {omitted}.' if omitted else None
            return PlaylistSelection(paths, 'vlc-live', warning)

    def launch(self, paths: list[Path]):
        chosen = [_local_file(str(path)) for path in paths]
        if not chosen or len(chosen) > 1000 or any(path is None for path in chosen):
            raise ValueError('Выберите от 1 до 1000 доступных локальных медиафайлов')
        with self.lock:
            try:
                connected = self._request().get('protocol') == 1
            except PlayerUnavailable:
                connected = False
            if connected:
                self._request(command='pl_empty')
                for path in chosen:
                    self._request(command='in_enqueue', input=path.as_uri())
                self._request(command='pl_play')
            else:
                if self.executable is None or not self.executable.is_file():
                    raise PlayerUnavailable('VLC не установлен. Установите его с videolan.org')
                source = self.root / 'player-integrations' / 'vlc'
                if not (source / 'requests' / 'status.json').is_file():
                    raise PlayerUnavailable('В комплекте Synchro отсутствует адаптер VLC')
                with socket.socket() as reservation:
                    reservation.bind(('127.0.0.1', 0))
                    self.port = reservation.getsockname()[1]
                self._password = secrets.token_hex(32)
                self._save_connection()
                args = [str(self.executable), '--no-one-instance', '--no-media-library',
                        '--no-qt-privacy-ask', '--no-qt-updates-notif',
                        '--no-sub-autodetect-file', '--extraintf=http',
                        '--http-host=127.0.0.1', f'--http-port={self.port}',
                        f'--http-password={self._password}', f'--http-src={source}',
                        f'--config={self.data_dir / "vlcrc"}', '--', *map(str, chosen)]
                try:
                    self._process = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                except OSError:
                    raise PlayerUnavailable('Не удалось запустить VLC') from None
            status = self._wait(lambda data: _local_file(data.get('uri')) == chosen[0]
                                and data.get('state') in {'playing', 'paused'},
                                'VLC не открыл выбранный файл', timeout=12)
            return self._snapshot(status)

    def restore(self, requested: PlayerSnapshot):
        path = _local_file(str(requested.path))
        if path is None or not math.isfinite(requested.rate) or not .2 <= requested.rate <= 12:
            raise ValueError('Некорректное состояние локального файла для VLC')
        with self.lock:
            current = self.read_basic()
            self.pause()
            if current.path != path:
                # Prefer the existing queue entry; do not duplicate the next episode.
                def find(node):
                    if not isinstance(node, dict):
                        return None
                    if node.get('type') == 'leaf' and _local_file(node.get('uri')) == path:
                        return node.get('id')
                    children = node.get('children', [])
                    for child in children if isinstance(children, list) else []:
                        found = find(child)
                        if found is not None:
                            return found
                    return None
                item_id = find(self._request('playlist'))
                if item_id is None:
                    self._request(command='in_play', input=path.as_uri())
                else:
                    self._request(command='pl_play', id=item_id)
                self._wait(lambda data: _local_file(data.get('uri')) == path
                           and data.get('state') in {'playing', 'paused'},
                           'VLC не переключился на нужный файл')
                self.pause()
            def tracks_ready(data):
                return (_local_file(data.get('uri')) == path
                        and requested.audio_index < len(self._tracks(data, 'audioTracks'))
                        and (not requested.subtitle_enabled
                             or 0 <= requested.subtitle_index < len(self._tracks(data, 'subtitleTracks'))))
            status = self._wait(tracks_ready, 'VLC не загрузил нужные дорожки выбранного файла')
            audio, subtitles = self._tracks(status, 'audioTracks'), self._tracks(status, 'subtitleTracks')
            if requested.audio_index >= len(audio) or requested.audio_index < -1:
                raise PlayerUnavailable('Нужная озвучка отсутствует в файле VLC')
            if requested.subtitle_enabled and not 0 <= requested.subtitle_index < len(subtitles):
                raise PlayerUnavailable('Нужные встроенные субтитры отсутствуют в файле VLC')
            if requested.audio_index >= 0:
                self._request(command='audio_track', val=audio[requested.audio_index]['id'])
            selected_subtitle = subtitles[requested.subtitle_index]['id'] if requested.subtitle_enabled else -1
            self._request(command='subtitle_track', val=selected_subtitle)
            self._request(command='subdelay', val=f'{requested.subtitle_delay_ms / 1000:.3f}')
            self._request(command='rate', val=f'{requested.rate:.6f}')
            # Stock HTTP seek truncates fractional seconds; our template writes
            # VLC's native microsecond time property instead.
            self._request(command='synchro_seek', ms=max(0, requested.position_ms))
            self.pause()
            def matches(data):
                actual = self._snapshot(data)
                return (actual.path == path and actual.paused
                        and abs(actual.position_ms - requested.position_ms) <= 250
                        and (requested.audio_index < 0 or actual.audio_index == requested.audio_index)
                        and actual.subtitle_enabled == requested.subtitle_enabled
                        and (not requested.subtitle_enabled or actual.subtitle_index == requested.subtitle_index)
                        and actual.subtitle_delay_ms == requested.subtitle_delay_ms
                        and abs(actual.rate - requested.rate) <= .001)
            result = self._wait(matches, 'VLC не подтвердил позицию, озвучку и субтитры')
            return self._snapshot(result)
