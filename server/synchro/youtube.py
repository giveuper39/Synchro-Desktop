"""YouTube ownership through Windows media sessions or one explicitly connected page.

Windows does not publish a media session's URL. Accessibility is therefore only
an optional shortcut for the selected YouTube tab. The page connector is the
browser-independent path; it reads no other tabs, history, cookies or account
data. Its capability keys authorize only that page, never the application API.
"""
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid

from .models import PlayerUnavailable, SessionConflict


VIDEO_ID = re.compile(r'[A-Za-z0-9_-]{11}\Z')
YOUTUBE_ORIGINS = frozenset(('https://youtube.com', 'https://www.youtube.com', 'https://m.youtube.com'))
BROWSER_HINTS = ('chrome', 'chromium', 'firefox', 'msedge', 'opera', 'brave', 'vivaldi',
                 'waterfox', 'librewolf', 'floorp', 'zenbrowser', 'yandex')


def video_id(address):
    """Accept public YouTube watch/shorts/live URLs, never arbitrary destinations."""
    if not isinstance(address, str) or len(address) > 8192:
        return None
    try:
        parsed = urlsplit(address if '://' in address else 'https://' + address)
        if (parsed.scheme != 'https' or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.hostname not in
                ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be')):
            return None
        if parsed.hostname == 'youtu.be':
            candidate = parsed.path.strip('/')
        elif parsed.path == '/watch':
            candidate = parse_qs(parsed.query).get('v', [''])[0]
        else:
            parts = parsed.path.strip('/').split('/')
            candidate = parts[1] if len(parts) == 2 and parts[0] in ('shorts', 'live', 'embed') else ''
        return candidate if VIDEO_ID.fullmatch(candidate) else None
    except ValueError:
        return None


def watch_url(identifier, position_ms=0):
    if not isinstance(identifier, str) or not VIDEO_ID.fullmatch(identifier):
        raise ValueError('A valid YouTube videoId is required')
    return f'https://www.youtube.com/watch?v={identifier}&t={max(0, int(position_ms)) // 1000}s&autoplay=0'


def _integer(data, name, minimum=0, maximum=2**31 - 1):
    value = data.get(name)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be an integer in {minimum}..{maximum}')
    return value


def _video(data):
    if not isinstance(data, dict):
        raise ValueError('Video state is required')
    identifier = data.get('videoId')
    if not isinstance(identifier, str) or not VIDEO_ID.fullmatch(identifier):
        raise ValueError('A valid YouTube videoId is required')
    title = data.get('title', '')
    if not isinstance(title, str) or len(title) > 500 or any(ord(c) < 32 and c not in '\t\n' for c in title):
        raise ValueError('A valid video title is required')
    if type(data.get('paused')) is not bool:
        raise ValueError('paused must be a boolean')
    rate = data.get('rate', 1.0)
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or not .25 <= rate <= 4:
        raise ValueError('rate must be a finite number in 0.25..4')
    position, duration = _integer(data, 'positionMs'), _integer(data, 'durationMs')
    if duration and position > duration + 2000:
        raise ValueError('The requested position exceeds the video duration')
    return dict(videoId=identifier, title=title.strip() or 'YouTube', positionMs=position,
                durationMs=duration, paused=data['paused'], rate=float(rate),
                canChangeVideo=data.get('canChangeVideo') is True)


class BrowserAccess:
    """A short-lived, bounded Windows UI Automation query for one media title."""
    def __init__(self, root):
        self.root = Path(root)

    def _run(self, data):
        if os.name != 'nt':
            return {}
        shell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        script = self.root / 'player-integrations/windows/BrowserAccess.ps1'
        if not script.is_file():
            return {}
        try:
            result = subprocess.run([str(shell), '-NoProfile', '-NonInteractive', '-STA',
                                     '-ExecutionPolicy', 'Bypass', '-File', str(script)],
                                    input=json.dumps(data), encoding='utf-8', capture_output=True,
                                    timeout=6, creationflags=subprocess.CREATE_NO_WINDOW)
            response = json.loads(result.stdout)
            if isinstance(response, dict) and not response.get('error'):
                return response
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        return {}

    def capture(self, media):
        result = self._run(dict(action='inspect', title=media.get('title', ''), appId=media.get('appId', '')))
        identifier = video_id(result.get('url'))
        if not identifier:
            return None
        return dict(result, videoId=identifier)

    def navigate(self, binding, identifier, position_ms):
        response = self._run(dict(action='navigate', windowId=binding['windowId'], pid=binding['pid'],
                                  expectedUrl=binding['url'], url=watch_url(identifier, position_ms)))
        if not response.get('ok'):
            raise PlayerUnavailable('Выберите исходную вкладку YouTube в том же окне браузера и повторите')


@dataclass
class PageBridge:
    id: str
    key: str
    origin: str
    nonce: str
    video: dict
    last_seen: float
    commands: list = field(default_factory=list)
    acknowledgements: dict = field(default_factory=dict)
    system_id: str | None = None
    extension_page: tuple | None = None


@dataclass
class YouTubeSession:
    id: str
    source_id: str
    video: dict
    pc_video: dict
    last_seen: float
    sequence: int = 0
    owner: str = 'android'
    revoked: bool = False
    error: str | None = None


class YouTubeCoordinator:
    def __init__(self, root, media, browser=None, clock=time.monotonic, command_timeout=6):
        self.media = media
        self.browser = browser if browser is not None else BrowserAccess(root)
        self.clock = clock
        self.command_timeout = command_timeout
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.bridges = {}
        self.sources = {}
        self.session = None
        self._bindings = {}

    def _media(self):
        try:
            return self.media.list_sessions()
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return []

    def catalog(self):
        with self.lock:
            active = {}
            result = []
            now = self.clock()
            connected = [b for b in self.bridges.values() if now - b.last_seen <= 12]
            connected_videos = {b.video['videoId'] for b in connected}
            for state in self._media():
                app = state.get('appId', '').lower()
                if not any(hint in app for hint in BROWSER_HINTS) and 'youtube' not in state.get('title', '').lower():
                    continue
                # An explicit page connection is stronger evidence than title
                # matching and avoids repeatedly querying its accessibility tree.
                if any(b.video['title'].strip().lower() == state['title'].strip().lower() for b in connected):
                    continue
                identity = 'youtube:system:' + hashlib.sha256(state['id'].encode()).hexdigest()[:20]
                cached = self._bindings.get(identity)
                if cached and cached[0] == state['title'] and now - cached[1] < 4:
                    binding = cached[2]
                else:
                    binding = self.browser.capture(state)
                    self._bindings[identity] = (state['title'], now, binding)
                if binding and binding['videoId'] in connected_videos:
                    continue
                ready = bool(binding and state.get('canPause') and state.get('canSeek'))
                video = dict(videoId=binding['videoId'] if binding else None, title=state['title'],
                             positionMs=state['positionMs'], durationMs=state['durationMs'],
                             paused=state['paused'], rate=1.0, canChangeVideo=False)
                active[identity] = dict(kind='system', systemId=state['id'], binding=binding, video=video)
                result.append(dict(id=identity, name='YouTube · ' + (binding.get('browser') if binding else state['appId']),
                                   kind='youtube', installed=True, ready=ready,
                                   detail='Открытая вкладка YouTube' if ready else
                                   'Установите расширение Synchro: оно подключит YouTube автоматически', media=video))
            for bridge in connected:
                identity = 'youtube:page:' + bridge.id
                active[identity] = dict(kind='page', bridge=bridge, video=dict(bridge.video))
                result.append(dict(id=identity, name='YouTube · ' + ('расширение Synchro' if bridge.extension_page else 'подключённая страница'), kind='youtube',
                                   installed=True, ready=True, detail=('Открытая вкладка · ' if bridge.extension_page else 'Связь с выбранной страницей · ') + bridge.origin,
                                   media=dict(bridge.video)))
            # Keep the active binding while Android owns playback even if a
            # platform briefly omits a paused session from its discovery list.
            if self.session and self.session.source_id not in active and self.session.source_id in self.sources:
                active[self.session.source_id] = self.sources[self.session.source_id]
            self.sources = active
            return result

    def register_bridge(self, data):
        origin, nonce = data.get('origin'), data.get('nonce')
        if origin not in YOUTUBE_ORIGINS:
            raise ValueError('Only an explicitly selected YouTube page can connect')
        if not isinstance(nonce, str) or not re.fullmatch(r'[A-Za-z0-9_-]{24,128}', nonce):
            raise ValueError('A page handshake nonce is required')
        state = _video(data.get('video'))
        with self.lock:
            for bridge in self.bridges.values():
                if secrets.compare_digest(bridge.nonce, nonce):
                    raise ValueError('This page connection has already been confirmed')
            # Bound memory without storing a browsing history or replaying pages.
            for identity in [i for i, b in self.bridges.items() if self.clock() - b.last_seen > 60]:
                del self.bridges[identity]
            if len(self.bridges) >= 12:
                raise PlayerUnavailable('Закройте неиспользуемые окна подключения YouTube')
            bridge = PageBridge(uuid.uuid4().hex, secrets.token_urlsafe(24), origin, nonce,
                                state, self.clock())
            candidates = [s for s in self._media() if s.get('title', '').strip().lower() == state['title'].strip().lower()]
            if len(candidates) == 1:
                bridge.system_id = candidates[0]['id']
            self.bridges[bridge.id] = bridge
            return dict(bridgeId=bridge.id, bridgeKey=bridge.key)

    def _bridge(self, data):
        bridge = self.bridges.get(data.get('bridgeId'))
        key = data.get('bridgeKey')
        if not bridge or not isinstance(key, str) or not secrets.compare_digest(key, bridge.key):
            raise SessionConflict('This page connection is no longer active')
        return bridge

    def bridge_poll(self, data):
        state = _video(data.get('video'))
        wait_ms = data.get('waitMs', 0)
        if type(wait_ms) is not int or not 0 <= wait_ms <= 1000:
            raise ValueError('waitMs must be an integer in 0..1000')
        with self.changed:
            bridge = self._bridge(data)
            bridge.video, bridge.last_seen = state, self.clock()
            acknowledgement = data.get('ack')
            if acknowledgement is not None:
                if not isinstance(acknowledgement, str) or len(acknowledgement) > 64:
                    raise ValueError('Invalid page acknowledgement')
                if any(c['id'] == acknowledgement for c in bridge.commands):
                    bridge.commands = [c for c in bridge.commands if c['id'] != acknowledgement]
                    bridge.acknowledgements[acknowledgement] = ('pc_changed' if data.get('error') == 'pc_changed'
                                                               else bool(data.get('error')))
                    if len(bridge.acknowledgements) > 32:
                        del bridge.acknowledgements[next(iter(bridge.acknowledgements))]
            if acknowledgement is not None:
                self.changed.notify_all()
            if wait_ms and not bridge.commands:
                self.changed.wait(wait_ms / 1000)
                if bridge.id not in self.bridges:
                    raise SessionConflict('This page connection is no longer active')
            return dict(commands=[dict(c) for c in bridge.commands], connected=True)

    def disconnect_bridge(self, data):
        with self.changed:
            bridge = self._bridge(data)
            del self.bridges[bridge.id]
            self.changed.notify_all()
            return dict(ok=True)

    def extension_sync(self, data):
        """One complete snapshot per browser profile, using the existing page command queue."""
        client = data.get('clientId')
        if not isinstance(client, str) or not re.fullmatch(r'[a-f0-9-]{36}', client):
            raise ValueError('A browser profile identity is required')
        pages, wait_ms = data.get('pages'), data.get('waitMs', 0)
        if not isinstance(pages, list) or len(pages) > 12:
            raise ValueError('Use at most 12 YouTube pages')
        if type(wait_ms) is not int or not 0 <= wait_ms <= 1000:
            raise ValueError('Invalid browser polling interval')
        checked, identities = [], set()
        # Validate the complete snapshot before removing or updating any page.
        for page in pages:
            if not isinstance(page, dict) or page.get('origin') not in YOUTUBE_ORIGINS:
                raise ValueError('Only YouTube pages can connect automatically')
            tab, document = page.get('tabId'), page.get('documentId')
            if type(tab) is not int or not 0 <= tab < 2**31 or not isinstance(document, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', document):
                raise ValueError('A browser tab and document identity are required')
            identity = (client, tab, document)
            if identity in identities:
                raise ValueError('Duplicate browser document')
            identities.add(identity)
            acks = page.get('acks', [])
            if not isinstance(acks, list) or len(acks) > 8:
                raise ValueError('Invalid command acknowledgements')
            for ack in acks:
                if not isinstance(ack, dict) or not isinstance(ack.get('id'), str) or not re.fullmatch(r'[a-f0-9]{32}', ack['id']) or (type(ack.get('error')) is not bool and ack.get('error') != 'pc_changed'):
                    raise ValueError('Invalid command acknowledgement')
            checked.append((identity, page['origin'], _video(page.get('video')), acks))
        with self.changed:
            now = self.clock()
            for identity, bridge in list(self.bridges.items()):
                if ((bridge.extension_page and bridge.extension_page[0] == client and bridge.extension_page not in identities)
                        or now - bridge.last_seen > 60):
                    del self.bridges[identity]
            current = {b.extension_page: b for b in self.bridges.values() if b.extension_page}
            if len(self.bridges) + sum(identity not in current for identity in identities) > 48:
                raise PlayerUnavailable('Too many connected YouTube pages')
            connected = []
            for identity, origin, video, acks in checked:
                bridge = current.get(identity)
                if bridge is None:
                    bridge = PageBridge(uuid.uuid4().hex, secrets.token_urlsafe(24), origin,
                                        secrets.token_hex(24), video, now, extension_page=identity)
                    self.bridges[bridge.id] = bridge
                bridge.video, bridge.last_seen = video, now
                for ack in acks:
                    if any(command['id'] == ack['id'] for command in bridge.commands):
                        bridge.commands = [command for command in bridge.commands if command['id'] != ack['id']]
                        bridge.acknowledgements[ack['id']] = ack['error']
                connected.append(bridge)
            self.changed.notify_all()
            if connected and wait_ms and not any(b.commands for b in connected):
                self.changed.wait(wait_ms / 1000)
            return dict(pages=[dict(tabId=b.extension_page[1], documentId=b.extension_page[2],
                                    commands=[dict(command) for command in b.commands])
                               for b in connected if b.id in self.bridges])

    def _request(self, bridge, action, **values):
        if bridge.id not in self.bridges or self.clock() - bridge.last_seen > 12:
            raise PlayerUnavailable('Нет связи с вкладкой YouTube. Проверьте расширение Synchro')
        command = dict(id=uuid.uuid4().hex, action=action, **values)
        if len(bridge.commands) >= 8:
            raise PlayerUnavailable('Страница YouTube не отвечает')
        bridge.commands.append(command)
        self.changed.notify_all()
        deadline = time.monotonic() + self.command_timeout
        while command['id'] not in bridge.acknowledgements:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or bridge.id not in self.bridges:
                bridge.commands = [c for c in bridge.commands if c['id'] != command['id']]
                raise PlayerUnavailable('Страница YouTube не подтвердила команду. Проверьте подключение браузера')
            self.changed.wait(remaining)
        failed = bridge.acknowledgements.pop(command['id'])
        if failed == 'pc_changed':
            raise SessionConflict('Playback was changed on the computer')
        if failed:
            raise PlayerUnavailable('Не удалось применить команду на странице YouTube')
        return dict(bridge.video)

    def _source(self, identity):
        source = self.sources.get(identity)
        if not source:
            raise PlayerUnavailable('Выбранная страница YouTube больше не открыта')
        return source

    def _native_state(self, source):
        states = [state for state in self._media() if state['id'] == source['systemId']]
        if not states:
            raise PlayerUnavailable('Медиасессия YouTube закрыта')
        return states[0]

    def _native_pause(self, source, position=None):
        # WinRT commands acknowledge dispatch, not the browser's completed
        # transition. A stale paused cache immediately after Play is common.
        self.media.command(source['systemId'], 'pause')
        if position is not None:
            self.media.command(source['systemId'], 'seek', positionMs=position)
        deadline, stable_since = time.monotonic() + self.command_timeout, None
        while time.monotonic() < deadline:
            state = self._native_state(source)
            correct = state['paused'] and (position is None or abs(state['positionMs'] - position) <= 1500)
            if correct:
                stable_since = time.monotonic() if stable_since is None else stable_since
                if time.monotonic() - stable_since >= .25:
                    return state
            else:
                stable_since = None
            time.sleep(.05)
        raise PlayerUnavailable('Браузер не подтвердил паузу и позицию YouTube')

    def _current(self, source):
        if source['kind'] == 'page':
            bridge = source['bridge']
            if bridge.id not in self.bridges or self.clock() - bridge.last_seen > 12:
                raise PlayerUnavailable('Вкладка YouTube больше не подключена')
            return dict(bridge.video)
        current = self._native_state(source)
        state = dict(source['video'])
        if current['title'] != state['title']:
            # Never assume another title in the same browser is the old video.
            raise SessionConflict('В браузере выбрано другое видео. Подключите его снова')
        state.update(positionMs=current['positionMs'], durationMs=current['durationMs'], paused=current['paused'])
        return state

    def takeover(self, source_id):
        with self.changed:
            self._bindings.pop(source_id, None)
            self.catalog()
            source = self._source(source_id)
            if not source['video'].get('videoId'):
                raise PlayerUnavailable('Подключите браузер через расширение Synchro')
            if source['kind'] == 'page':
                snapshot = self._request(source['bridge'], 'pause')
            else:
                result = self._native_pause(source)
                snapshot = dict(source['video'], positionMs=result['positionMs'], durationMs=result['durationMs'], paused=result['paused'])
            if not snapshot['paused']:
                raise PlayerUnavailable('Браузер не подтвердил паузу YouTube')
            self.session = YouTubeSession(uuid.uuid4().hex, source_id, dict(snapshot), dict(snapshot), self.clock())
            return self.describe()

    def describe(self, base_url=None):
        with self.lock:
            s = self.session
            if s is None:
                return dict(sessionId=None, owner='pc', kind='youtube')
            return dict(sessionId=s.id, sourceId=s.source_id, kind='youtube', sequence=s.sequence,
                        owner=s.owner, revoked=s.revoked, error=s.error, **s.video)

    def _pc_changed(self, session, source):
        current = self._current(source)
        return (not current['paused'] or current['videoId'] != session.pc_video['videoId']
                or abs(current['positionMs'] - session.pc_video['positionMs']) > 1500)

    def _revoke(self, session):
        session.owner, session.revoked = 'pc', True

    def _restore(self, source, desired, expected=None):
        if desired['videoId'] != self._current(source)['videoId']:
            if source['kind'] != 'page' or not source['bridge'].video.get('canChangeVideo'):
                raise PlayerUnavailable('Этот браузер может вернуть позицию только текущего ролика. Подключите страницу закладкой «Synchro»')
            actual = self._request(source['bridge'], 'open', videoId=desired['videoId'],
                                   positionMs=desired['positionMs'], title=desired['title'], rate=desired['rate'],
                                   expected=expected)
            if not actual['paused'] or actual['videoId'] != desired['videoId']:
                raise PlayerUnavailable('Страница YouTube не подтвердила новый ролик на паузе')
            return actual
        if source['kind'] == 'page':
            return self._request(source['bridge'], 'seek', positionMs=desired['positionMs'],
                                 rate=desired['rate'], expected=expected)
        if desired['rate'] != 1.0:
            raise ValueError('Этот браузер передаёт скорость только при подключении страницы закладкой «Synchro»')
        result = self._native_pause(source, desired['positionMs'])
        return dict(desired, paused=True, positionMs=result['positionMs'])

    def progress(self, data):
        with self.changed:
            s = self.session
            if s is None or data.get('sessionId') != s.id or s.revoked:
                raise SessionConflict('This YouTube session is no longer active')
            sequence = _integer(data, 'sequence', 1, 2**53 - 1)
            if sequence <= s.sequence:
                return dict(ok=True, ignored=True, sequence=s.sequence, owner=s.owner)
            desired = _video(dict(s.video, **{key: data[key] for key in
                                  ('videoId', 'title', 'positionMs', 'durationMs', 'paused', 'rate') if key in data}))
            source = self._source(s.source_id)
            try:
                changed = self._pc_changed(s, source)
            except SessionConflict:
                self._revoke(s)
                raise
            if changed:
                self._revoke(s)
                raise SessionConflict('Playback was changed on the computer')
            if desired['videoId'] != s.video['videoId'] and not s.video.get('canChangeVideo'):
                raise ValueError('This source can return only the originally selected YouTube video')
            if desired['paused']:
                if s.owner == 'android' or desired != s.video:
                    try:
                        actual = self._restore(source, desired, s.pc_video)
                    except SessionConflict:
                        self._revoke(s)
                        raise
                    s.pc_video = dict(actual, paused=True)
            elif s.owner == 'pc':
                if source['kind'] == 'page':
                    try:
                        s.pc_video = self._request(source['bridge'], 'pause', expected=s.pc_video)
                    except SessionConflict:
                        self._revoke(s)
                        raise
                else:
                    self._native_pause(source)
            s.video, s.sequence, s.last_seen = desired, sequence, self.clock()
            s.owner, s.error = ('pc' if desired['paused'] else 'android'), None
            return dict(ok=True, ignored=False, sequence=sequence, owner=s.owner)

    def watchdog(self, disconnect_seconds=15):
        with self.changed:
            s = self.session
            if not s or s.owner != 'android' or s.revoked:
                return
            try:
                source = self._source(s.source_id)
                if self._pc_changed(s, source):
                    self._revoke(s)
                elif self.clock() - s.last_seen > disconnect_seconds:
                    actual = self._restore(source, dict(s.video, paused=True), s.pc_video)
                    s.video['paused'] = True
                    s.pc_video = dict(actual, paused=True)
                    self._revoke(s)
            except (PlayerUnavailable, SessionConflict):
                s.error = 'Связь с выбранной страницей YouTube прервана'
                self._revoke(s)

    def close(self):
        with self.changed:
            self.bridges.clear()
            self.changed.notify_all()
