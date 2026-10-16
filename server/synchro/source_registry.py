"""A selected desktop player, with bounded discovery and explicit playback ownership."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time
import shutil
import subprocess

from .models import PlayerUnavailable, SessionConflict
from .playlist import discover_playlist

FULL_CONTROLS = {'audioTracks': True, 'subtitles': True, 'subtitleDelay': True, 'rate': True}


class SourceRegistry:
    def __init__(self, root, potplayer, windows, vlc, mpv, youtube):
        self.root = Path(root)
        self.adapters = {'potplayer': potplayer, 'windows': windows, 'vlc': vlc, 'mpv': mpv}
        self.youtube = youtube
        self.selected = 'potplayer'
        self.settings = self.root / '.data/player-selection.json'
        try:
            value = json.loads(self.settings.read_text(encoding='utf-8')).get('selected')
            if value in self.adapters:
                self.selected = value
        except (OSError, ValueError):
            pass
        self.lock = threading.RLock()
        self.cache = []
        self.catalog_at = 0
        self.refreshing = False
        self._preview_key = None
        self._preview = None
        self.discovery = ThreadPoolExecutor(max_workers=1, thread_name_prefix='synchro-sources')

    @property
    def is_youtube(self):
        return self.selected not in self.adapters

    @property
    def capabilities(self):
        if self.is_youtube:
            return {'audioTracks': False, 'subtitles': False, 'subtitleDelay': False, 'rate': True}
        return dict(getattr(self.adapters[self.selected], 'capabilities', FULL_CONTROLS))

    def _adapter(self):
        if self.is_youtube:
            raise PlayerUnavailable('Выбран YouTube. Обновите приложение до 0.4.0')
        return self.adapters[self.selected]

    def read_basic(self):
        return self._adapter().read_basic()

    def capture(self):
        return self._adapter().capture()

    def pause(self):
        return self._adapter().pause()

    def restore(self, snapshot):
        return self._adapter().restore(snapshot)

    def playlist_selection(self, current, explicit=None):
        player = self._adapter()
        if explicit or self.selected == 'potplayer':
            return discover_playlist(current, explicit)
        if hasattr(player, 'playlist_selection'):
            return player.playlist_selection(current, explicit)
        return player.playlist()

    def _catalog(self):
        result = []
        names = {'potplayer': 'PotPlayer', 'windows': 'Медиаплеер Windows', 'vlc': 'VLC', 'mpv': 'mpv'}
        for identity, player in self.adapters.items():
            descriptor = {'id': identity, 'name': names[identity], 'kind': 'file',
                          'installed': True, 'ready': False,
                          'capabilities': dict(getattr(player, 'capabilities', FULL_CONTROLS))}
            try:
                if hasattr(player, 'find_executable') and not player.find_executable():
                    descriptor['installed'] = False
                    raise PlayerUnavailable('Установите плеер на ПК, затем откройте его через мастер Synchro')
                state = player.read_basic()
                descriptor.update(ready=True, detail='Позиция и пауза' if identity == 'windows' else 'Видео, позиция и дорожки',
                                  media={'title': state.path.stem, 'positionMs': state.position_ms,
                                         'durationMs': state.duration_ms, 'paused': state.paused})
            except Exception:
                descriptor['detail'] = ('Откройте локальное видео в плеере' if identity in ('potplayer', 'windows')
                                        else 'Откройте видео через мастер на ПК' if descriptor['installed']
                                        else 'Плеер не установлен на компьютере')
            result.append(descriptor)
        try:
            browsers = self.youtube.catalog()
            result.extend(browsers)
        except Exception:
            pass
        if not any(item.get('kind') == 'youtube' for item in result):
            result.append({'id': 'youtube', 'name': 'YouTube · любой браузер', 'kind': 'youtube',
                           'ready': False, 'installed': True,
                           'detail': 'Подключите вкладку в мастере на ПК'})
        with self.lock:
            self.cache, self.catalog_at, self.refreshing = result, time.monotonic(), False

    def catalog(self, wait=False):
        with self.lock:
            if not self.refreshing and (not self.cache or time.monotonic() - self.catalog_at > 4):
                self.refreshing = True
                future = self.discovery.submit(self._catalog)
            else:
                future = None
        if wait and future:
            future.result(timeout=25)
        with self.lock:
            return {'selected': self.selected, 'players': list(self.cache), 'discovering': self.refreshing}

    def select(self, identity):
        known = identity in self.adapters or any(item['id'] == identity for item in self.cache)
        if not isinstance(identity, str) or not known:
            raise ValueError('Выберите доступный источник из списка')
        with self.lock:
            # Reconnectable local adapters are remembered. Browser/page capabilities expire.
            self.settings.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.settings.with_suffix('.tmp')
            temporary.write_text(json.dumps({'selected': identity if identity in self.adapters else 'potplayer'}), encoding='utf-8')
            temporary.replace(self.settings)
            self.selected = identity
        return self.catalog()

    def open(self, identity, paths):
        if identity not in self.adapters:
            raise ValueError('Выберите локальный плеер')
        if identity == 'windows':
            if len(paths) != 1:
                raise ValueError('Выберите один файл для Медиаплеера Windows')
            self.adapters[identity].select_file(paths[0])
            self.adapters[identity].system_media.open_media_file(paths[0])
        elif hasattr(self.adapters[identity], 'launch'):
            self.adapters[identity].launch(paths)
        else:
            raise ValueError('Откройте видео в PotPlayer обычным способом')
        self.select(identity)
        with self.lock:
            self.catalog_at = 0

    def close(self):
        self.discovery.shutdown(wait=True)
        for player in self.adapters.values():
            player.close()

    def preview(self):
        """One optional in-memory frame from the selected local file; never pause playback."""
        identity = self.selected
        state = self.read_basic()
        key = (identity, str(state.path), state.path.stat().st_mtime_ns, state.position_ms // 10000)
        if key == self._preview_key and self._preview:
            return self._preview, identity
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            raise PlayerUnavailable('Preview is unavailable')
        result = subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-ss',str(state.position_ms/1000),
                                 '-i',str(state.path),'-frames:v','1','-vf','scale=640:-2','-f','image2pipe','-vcodec','mjpeg','pipe:1'],
                                stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=6,
                                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode or not result.stdout.startswith(b'\xff\xd8') or len(result.stdout)>2*1024*1024:
            raise PlayerUnavailable('Preview is unavailable')
        self._preview_key,self._preview = key,result.stdout
        return result.stdout,identity


class ViewingSources:
    """Source changes invalidate old sessions and never interrupt a phone that owns playback."""
    def __init__(self, local, registry, youtube):
        self.local, self.registry, self.youtube = local, registry, youtube
        self.token, self.library, self.lock = local.token, local.library, local.lock

    def _phone_owns(self):
        return (bool(self.local.session and self.local.session.owner == 'android' and not self.local.session.revoked)
                or self.youtube.describe().get('owner') == 'android')

    def select(self, data):
        with self.lock:
            identity = data.get('id')
            if identity != self.registry.selected and self._phone_owns():
                raise SessionConflict('Сначала поставьте просмотр на телефоне на паузу')
            if identity != self.registry.selected:
                self.registry.select(identity)
                self.local.session = None
            return self.registry.catalog()

    def describe(self, base_url):
        if self.registry.is_youtube:
            return self.youtube.describe(base_url)
        return self.local.describe(base_url)

    def takeover(self, base_url):
        if self.registry.is_youtube:
            raise PlayerUnavailable('YouTube открывается через встроенный плеер в Synchro 0.4.0')
        return self.local.takeover(base_url)

    def resume(self, data, base_url):
        return self.local.resume(data, base_url)

    def progress(self, data):
        if self.registry.is_youtube:
            raise SessionConflict('Источник просмотра изменён')
        return self.local.progress(data)

    def youtube_takeover(self, data):
        with self.lock:
            identity = data.get('sourceId', self.registry.selected)
            if identity != self.registry.selected:
                self.select({'id': identity})
            if not self.registry.is_youtube:
                raise ValueError('Выберите YouTube в списке источников')
            return self.youtube.takeover(identity)

    def youtube_progress(self, data):
        with self.lock:
            if not self.registry.is_youtube or self.youtube.describe().get('sourceId') != self.registry.selected:
                raise SessionConflict('Источник просмотра изменён')
            return self.youtube.progress(data)

    def watchdog(self):
        if not self.registry.is_youtube:
            self.local.watchdog()
        self.youtube.watchdog()
