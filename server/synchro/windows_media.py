"""Windows media sessions and the current Windows Media Player adapter.

GSMTC is the public Windows media-control API, also used by browser media keys.
It deliberately exposes no file path or audio/subtitle track selection. For the
Windows player only, a small read-only helper resolves its currently open disk
handles. We never consult a playback history or scan unrelated processes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from .models import PlayerSnapshot, PlayerUnavailable
from .playlist import MEDIA_EXTENSIONS, PlaylistSelection, _natural_key, discover_playlist


WINDOWS_MEDIA_APP = 'Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic'


class SystemMedia:
    """Serialized, persistent WinRT host; importing this module is portable."""

    def __init__(self, root):
        self.root = Path(root)
        self._process = None
        self._responses = queue.Queue()
        self._lock = threading.Lock()
        self._sequence = 0

    def _start(self):
        if self._process is not None and self._process.poll() is None:
            return
        if os.name != 'nt':
            raise PlayerUnavailable('Windows media sessions require Windows 10 or later')
        bridge = self.root / 'player-integrations' / 'windows' / 'MediaBridge.ps1'
        if not bridge.is_file():
            raise PlayerUnavailable('The Windows media bridge is missing from this installation')
        powershell = Path(os.environ.get('SystemRoot', 'C:/Windows')) / (
            'System32/WindowsPowerShell/v1.0/powershell.exe')
        self._responses = responses = queue.Queue()
        self._process = process = subprocess.Popen(
            [str(powershell), '-NoLogo', '-NoProfile', '-NonInteractive',
             '-ExecutionPolicy', 'Bypass', '-File', str(bridge)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', bufsize=1,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

        def read():
            try:
                for line in process.stdout:
                    try:
                        responses.put(json.loads(line))
                    except (ValueError, TypeError):
                        continue
            finally:
                responses.put(None)

        threading.Thread(target=read, name='windows-media-response', daemon=True).start()

    def _request(self, action, **values):
        with self._lock:
            self._start()
            self._sequence += 1
            request = {'requestId': self._sequence, 'action': action, **values}
            try:
                self._process.stdin.write(json.dumps(request, ensure_ascii=False) + '\n')
                self._process.stdin.flush()
                response = self._responses.get(timeout=20)
            except (OSError, queue.Empty) as error:
                self._stop()
                raise PlayerUnavailable('Windows media control did not respond') from error
            if response is None or response.get('requestId') != self._sequence:
                self._stop()
                raise PlayerUnavailable('Windows media control stopped unexpectedly')
            if response.get('error'):
                raise PlayerUnavailable(response['error'])
            return response.get('result')

    def list_sessions(self):
        return self._request('sessions') or []

    def command(self, session_id, action, **values):
        if action not in {'pause', 'play', 'seek'}:
            raise ValueError('Supported media commands are pause, play and seek')
        if not isinstance(session_id, str) or not session_id:
            raise ValueError('A selected Windows media session is required')
        if action == 'seek':
            position = values.get('positionMs')
            if type(position) is not int or not 0 <= position <= 2**53 - 1:
                raise ValueError('positionMs must be a nonnegative integer')
            values = {'positionMs': position}
        else:
            values = {}
        return self._request(action, sessionId=session_id, **values)

    def open_media_files(self):
        """Only accessible disk paths held by the installed MediaPlayer.exe."""
        values = self._request('files') or []
        if isinstance(values, str):
            values = [values]
        return [Path(value).resolve() for value in values
                if Path(value).suffix.casefold() in MEDIA_EXTENSIONS]

    def open_media_file(self, path):
        path = Path(path).resolve()
        if not path.is_file() or path.suffix.casefold() not in MEDIA_EXTENSIONS:
            raise PlayerUnavailable('Select an accessible local media file')
        return self._request('open', path=str(path))

    def _stop(self):
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.stdin.close()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=2)

    def close(self):
        with self._lock:
            self._stop()


class WindowsMediaPlayer:
    """Handoff local files without changing volume or taking over other apps.

    The adapter intentionally reports unsupported audio/subtitle settings as
    unknown. GSMTC cannot retrieve or set them, so callers must show that limit.
    """

    capabilities = {'audioTracks': False, 'subtitles': False, 'subtitleDelay': False,
                    'rate': False, 'playlist': 'folder'}

    def __init__(self, root, system_media=None, session_id=None, path=None):
        self.system_media = system_media or SystemMedia(root)
        self._owns_system = system_media is None
        self.session_id = session_id
        self.selected_path = Path(path).resolve() if path else None
        self._lock = threading.RLock()

    def _session(self):
        sessions = [item for item in self.system_media.list_sessions()
                    if item['appId'].casefold() == WINDOWS_MEDIA_APP.casefold()]
        if self.session_id is not None:
            sessions = [item for item in sessions if item['id'] == self.session_id]
        if len(sessions) != 1:
            raise PlayerUnavailable('Open one Windows Media Player instance with a local file')
        self.session_id = sessions[0]['id']
        return sessions[0]

    @staticmethod
    def _title_matches(path, title):
        normalized = str(title or '').strip().casefold()
        return normalized in {path.name.casefold(), path.stem.casefold()}

    def _path(self, session):
        files = list(dict.fromkeys(self.system_media.open_media_files()))
        matching = [path for path in files if self._title_matches(path, session['title'])]
        if len(matching) == 1:
            return matching[0]
        if len(files) == 1:
            return files[0]
        if self.selected_path in files:
            return self.selected_path
        if (not files and self.selected_path is not None and self.selected_path.is_file()
                and self._title_matches(self.selected_path, session['title'])):
            # An explicitly chosen file is the fallback on systems that deny
            # handle inspection. Matching the current title prevents silent
            # reuse when the user opens a different episode on the PC.
            return self.selected_path
        raise PlayerUnavailable('The current Windows Media Player file is ambiguous; select it explicitly')

    def select_file(self, path):
        path = Path(path).resolve()
        if not path.is_file() or path.suffix.casefold() not in MEDIA_EXTENSIONS:
            raise PlayerUnavailable('Select an accessible local media file')
        with self._lock:
            self.selected_path = path

    def _basic(self):
        session = self._session()
        return PlayerSnapshot(self._path(session), session['positionMs'],
                              session['durationMs'], session['paused'])

    def read_basic(self):
        with self._lock:
            return self._basic()

    def _pause(self, settle=False):
        session = self._session()
        if session['paused'] and not settle:
            return
        if not session['paused'] and not session['canPause']:
            raise PlayerUnavailable('Windows Media Player cannot pause this media')
        self.system_media.command(session['id'], 'pause')
        deadline = time.monotonic() + 3
        stable_since = None
        while time.monotonic() < deadline:
            if self._session()['paused']:
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= .25:
                    return
            else:
                stable_since = None
                self.system_media.command(session['id'], 'pause')
            time.sleep(.05)
        raise PlayerUnavailable('Windows Media Player did not confirm pause')

    def pause(self):
        with self._lock:
            self._pause()

    def capture(self):
        with self._lock:
            before = self._basic()
            self._pause()
            result = self._basic()
            if result.path != before.path:
                raise PlayerUnavailable('The Windows media file changed during handoff')
            return result

    def restore(self, requested):
        with self._lock:
            current = self._basic()
            self._pause()
            if current.path != requested.path:
                self.system_media.open_media_file(requested.path)
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    try:
                        current = self._basic()
                        if current.path == requested.path and current.duration_ms > 0:
                            break
                    except PlayerUnavailable:
                        pass
                    time.sleep(.1)
                else:
                    raise PlayerUnavailable('Windows Media Player did not open the next file')
            self._pause()
            session = self._session()
            if not session['canSeek']:
                raise PlayerUnavailable('Windows Media Player cannot seek in this media')
            for attempt in range(3):
                self.system_media.command(session['id'], 'seek', positionMs=requested.position_ms)
                self._pause()
                deadline = time.monotonic() + .4
                while time.monotonic() < deadline:
                    result = self._basic()
                    if (result.path == requested.path and result.paused
                            and abs(result.position_ms - requested.position_ms) <= 500):
                        return result
                    time.sleep(.05)
                if attempt == 0:
                    # Current Media Player can acknowledge a paused seek while
                    # keeping the decoder at its old position after file-open.
                    # A bounded running seek primes that decoder; always pause
                    # again in finally, including error/timeout paths.
                    try:
                        self.system_media.command(session['id'], 'play')
                        self.system_media.command(session['id'], 'seek',
                                                  positionMs=requested.position_ms)
                        deadline = time.monotonic() + 1
                        while time.monotonic() < deadline:
                            state = self._session()
                            if abs(state['positionMs'] - requested.position_ms) <= 350:
                                break
                            time.sleep(.03)
                    finally:
                        # GSMTC may briefly retain the pre-play paused status.
                        # Always send Pause after our own Play, then confirm a
                        # stable stopped timeline rather than trusting a cache.
                        self._pause(settle=True)
                    result = self._basic()
                    if (result.path == requested.path and result.paused
                            and abs(result.position_ms - requested.position_ms) <= 500):
                        return result
            raise PlayerUnavailable('Windows Media Player did not confirm the returned position')

    def playlist_selection(self, current_file, explicit_playlist=None):
        if explicit_playlist is not None:
            return discover_playlist(current_file, explicit_playlist)
        current_file = Path(current_file).resolve()
        try:
            paths = sorted((path.resolve() for path in current_file.parent.iterdir()
                            if path.is_file() and path.suffix.casefold() in MEDIA_EXTENSIONS),
                           key=_natural_key)
        except OSError:
            paths = [current_file]
        if current_file not in paths:
            paths.append(current_file)
        return PlaylistSelection(paths, 'directory',
            'Windows Media Player не предоставляет свою очередь: файлы упорядочены по имени. '
            'Его Windows API не позволяет синхронизировать озвучку, субтитры и их задержку.')

    def close(self):
        if self._owns_system:
            self.system_media.close()
