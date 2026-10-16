"""One receiver owns playback; explicit pause returns its position to PotPlayer.

Session IDs and monotonically increasing event numbers prevent a delayed
network packet from restoring an older episode. Native player operations are
serialized with state updates, including the watchdog's ownership checks.
"""
from dataclasses import dataclass, replace
import json
import math
from pathlib import Path
import threading
import time
import uuid

from .models import PlayerSnapshot, SessionConflict
from .playlist import discover_playlist


@dataclass
class ViewingSession:
    id: str
    items: list
    index: int
    snapshot: PlayerSnapshot
    playlist_source: str
    playlist_warning: str | None
    sequence: int = 0
    owner: str = 'android'
    revoked: bool = False
    last_seen: float = 0
    error: str | None = None
    pc_path: Path | None = None
    pc_position_ms: int = 0


def _integer(data, name, minimum, maximum):
    value = data.get(name)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be an integer in {minimum}..{maximum}')
    return value


def _boolean(data, name):
    value = data.get(name)
    if type(value) is not bool:
        raise ValueError(f'{name} must be a boolean')
    return value


class SessionManager:
    def __init__(self, player, library, token, data_dir, playlist=None, clock=time.monotonic):
        self.player = player
        self.library = library
        self.token = token
        self.data_dir = Path(data_dir)
        self.playlist = playlist
        self.clock = clock
        self.session: ViewingSession | None = None
        self.lock = threading.RLock()

    def _save(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        s = self.session
        journal = {'sessionId': s.id, 'sequence': s.sequence, 'owner': s.owner,
                   'index': s.index, 'path': str(s.snapshot.path),
                   'positionMs': s.snapshot.position_ms, 'audioIndex': s.snapshot.audio_index,
                   'subtitleIndex': s.snapshot.subtitle_index,
                   'subtitleEnabled': s.snapshot.subtitle_enabled,
                   'subtitleDelayMs': s.snapshot.subtitle_delay_ms}
        temporary = self.data_dir / 'last-session.tmp'
        temporary.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.data_dir / 'last-session.json')

    def _pc_changed(self, session):
        current = self.player.read_basic()
        return (not current.paused or current.path != session.pc_path
                or abs(current.position_ms - session.pc_position_ms) > 250)

    def _revoke(self, session):
        session.owner, session.revoked = 'pc', True
        self._save()

    def _describe(self, base_url):
        s = self.session
        if s is None:
            return {'sessionId': None, 'owner': 'pc'}
        snapshot = s.snapshot
        # Probe the playing item. Large queues do not require scanning every movie.
        items = [self.library.describe(item, base_url, self.token, index == s.index)
                 for index, item in enumerate(s.items)]
        metadata = self.library.probe(s.items[s.index])
        def label(collection, index):
            choices = metadata.get(collection, [])
            if 0 <= index < len(choices):
                return choices[index]['title'] or choices[index]['language']
            return ''
        return {'sessionId': s.id, 'sequence': s.sequence, 'owner': s.owner,
                'sourceId': getattr(self.player, 'selected', 'potplayer'),
                'capabilities': getattr(self.player, 'capabilities', {'audioTracks': True, 'subtitles': True, 'subtitleDelay': True, 'rate': True}),
                'revoked': s.revoked, 'index': s.index, 'items': items,
                'positionMs': snapshot.position_ms, 'rate': snapshot.rate,
                'audioIndex': snapshot.audio_index, 'subtitleIndex': snapshot.subtitle_index,
                'audioLabel': label('audioTracks', snapshot.audio_index),
                'subtitleLabel': label('subtitleTracks', snapshot.subtitle_index),
                'subtitleEnabled': snapshot.subtitle_enabled,
                'subtitleDelayMs': snapshot.subtitle_delay_ms,
                'playlistSource': s.playlist_source, 'playlistWarning': s.playlist_warning,
                'error': s.error}

    def describe(self, base_url):
        with self.lock:
            return self._describe(base_url)

    def resume(self, data, base_url):
        previous_id = data.get('sessionId')
        if not isinstance(previous_id, str) or not 1 <= len(previous_id) <= 256:
            raise ValueError('A previous sessionId is required')
        return self.takeover(base_url, previous_id)

    @staticmethod
    def _same_playback(first, second):
        return (first.path == second.path
                and abs(first.position_ms - second.position_ms) <= 250
                and first.audio_index == second.audio_index
                and first.subtitle_index == second.subtitle_index
                and first.subtitle_enabled == second.subtitle_enabled
                and first.subtitle_delay_ms == second.subtitle_delay_ms
                and abs(first.rate - second.rate) < .001)

    def takeover(self, base_url, resume_session_id=None):
        with self.lock:
            previous = self.session
            snapshot = self.player.capture()
            selection = (self.player.playlist_selection(snapshot.path, self.playlist)
                         if hasattr(self.player, 'playlist_selection') else discover_playlist(snapshot.path, self.playlist))
            items = [self.library.add(path) for path in selection.paths]
            index = next(i for i, item in enumerate(items) if item.path == snapshot.path.resolve())
            resume_in_place = bool(
                previous and previous.id == resume_session_id
                and previous.owner == 'pc' and not previous.revoked
                and previous.index == index
                and [item.id for item in previous.items] == [item.id for item in items]
                and self._same_playback(previous.snapshot, snapshot))
            if not resume_in_place:
                # Validate media before publishing a new session ID.
                self.library.probe(items[index])
                self.library.preload(items, index)
            self.session = ViewingSession(uuid.uuid4().hex, items, index, snapshot,
                                           selection.source, selection.warning,
                                           last_seen=self.clock(), pc_path=snapshot.path,
                                           pc_position_ms=snapshot.position_ms)
            self._save()
            response = self._describe(base_url)
            response['resumeInPlace'] = resume_in_place
            return response

    def progress(self, data):
        with self.lock:
            s = self.session
            if s is None or data.get('sessionId') != s.id or s.revoked:
                raise SessionConflict('This playback session is no longer active')
            sequence = _integer(data, 'sequence', 1, 2**53 - 1)
            if sequence <= s.sequence:
                return {'ok': True, 'ignored': True, 'sequence': s.sequence, 'owner': s.owner}
            # Do not wait for the next watchdog tick: Play/seek on the computer
            # takes priority over an Android event already travelling over Wi-Fi.
            if s.owner == 'android' and self._pc_changed(s):
                self._revoke(s)
                raise SessionConflict('Playback was resumed on the computer')
            index = _integer(data, 'index', 0, len(s.items) - 1)
            position = _integer(data, 'positionMs', 0, 2**31 - 1)
            audio = _integer(data, 'audioIndex', -1, 255)
            subtitle = _integer(data, 'subtitleIndex', -1, 255)
            enabled = _boolean(data, 'subtitleEnabled')
            paused = _boolean(data, 'paused')
            delay = _integer(data, 'subtitleDelayMs', -3600000, 3600000)
            rate = data.get('rate', s.snapshot.rate)
            if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or not .2 <= rate <= 12:
                raise ValueError('rate must be a finite number in 0.2..12')
            item = s.items[index]
            metadata = self.library.probe(item)
            if metadata['metadataAvailable']:
                if audio >= len(metadata['audioTracks']) or subtitle >= len(metadata['subtitleTracks']):
                    raise ValueError('The selected media track does not exist')
                duration = metadata['durationMs']
                if duration and position > duration + 2000:
                    raise ValueError('The requested position exceeds the media duration')
            else:
                duration = s.snapshot.duration_ms if index == s.index else 0
            desired = PlayerSnapshot(item.path, position, duration or 0, paused,
                                     audio, subtitle, enabled, delay, float(rate))
            if paused:
                # Retries while already paused must not interrupt a manually resumed PC.
                if s.owner == 'android':
                    self.player.restore(desired)
                elif desired != s.snapshot:
                    if self._pc_changed(s):
                        self._revoke(s)
                        raise SessionConflict('Playback was resumed on the computer')
                    self.player.restore(desired)
            elif s.owner == 'pc':
                if self._pc_changed(s):
                    self._revoke(s)
                    raise SessionConflict('Playback was changed on the computer; capture it again')
                self.player.pause()
            s.index, s.snapshot = index, desired
            s.sequence = sequence
            s.owner = 'pc' if paused else 'android'
            if paused:
                s.pc_path = desired.path
                s.pc_position_ms = desired.position_ms
            s.last_seen, s.error = self.clock(), None
            self._save()
            return {'ok': True, 'ignored': False, 'sequence': sequence, 'owner': s.owner}

    def watchdog(self, disconnect_seconds=15):
        """Yield to a manually resumed PC, or checkpoint a disconnected receiver."""
        with self.lock:
            s = self.session
            if not s or s.owner != 'android' or s.revoked:
                return
            if self._pc_changed(s):
                self._revoke(s)
            elif self.clock() - s.last_seen > disconnect_seconds:
                self.player.restore(replace(s.snapshot, paused=True))
                s.snapshot = replace(s.snapshot, paused=True)
                s.pc_path = s.snapshot.path
                s.pc_position_ms = s.snapshot.position_ms
                s.owner, s.revoked = 'pc', True
                self._save()
