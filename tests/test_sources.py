"""Selected source ownership, adapter playlists and real HTTP access boundaries."""
from dataclasses import replace
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from synchro.http_server import HandoffHTTPServer
from synchro.library import MediaLibrary
from synchro.models import PlayerSnapshot, SessionConflict
from synchro.playlist import PlaylistSelection
from synchro.sessions import SessionManager
from synchro.source_registry import SourceRegistry, ViewingSources


class MemoryPlayer:
    def __init__(self, path):
        self.snapshot = PlayerSnapshot(path, 12000, 180000, True)
        self.restores = []

    def read_basic(self):
        return self.snapshot

    def capture(self):
        self.snapshot = replace(self.snapshot, paused=True)
        return self.snapshot

    def pause(self):
        self.snapshot = replace(self.snapshot, paused=True)

    def restore(self, snapshot):
        self.restores.append(snapshot)
        self.snapshot = replace(snapshot, paused=True)

    def close(self):
        pass


class MemoryYouTube:
    def __init__(self):
        self.owner, self.source_id = 'pc', 'youtube:test-browser'
        self.events = []

    def catalog(self):
        return [{'id': self.source_id, 'kind': 'youtube', 'name': 'YouTube · Test browser',
                 'ready': True, 'installed': True}]

    def describe(self, base_url=None):
        return {'owner': self.owner, 'sourceId': self.source_id}

    def progress(self, data):
        self.events.append(data)
        return {'ok': True}


class SourcesFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.first, self.second = self.root / 'Episode 01.mkv', self.root / 'Episode 02.mkv'
        for path in (self.first, self.second):
            path.write_bytes(b'not-private-test-fixture')
        self.players = {name: MemoryPlayer(self.first) for name in ('potplayer', 'windows', 'vlc', 'mpv')}
        self.windows_selection = PlaylistSelection([self.first, self.second], 'windows-test-queue')
        self.vlc_selection = PlaylistSelection([self.second, self.first], 'vlc-test-queue')
        self.players['windows'].playlist_selection = Mock(return_value=self.windows_selection)
        self.players['vlc'].playlist = Mock(return_value=self.vlc_selection)
        self.youtube = MemoryYouTube()
        self.registry = SourceRegistry(self.root, *(self.players[name] for name in
            ('potplayer', 'windows', 'vlc', 'mpv')), self.youtube)
        self.addCleanup(self.registry.close)
        self.registry.catalog(wait=True)
        self.local = SessionManager(self.registry, MediaLibrary(ffprobe=False),
                                    'test-only-source-token', self.root / 'session')
        self.sources = ViewingSources(self.local, self.registry, self.youtube)
        self.sources.select({'id': 'windows'})

    def capture(self):
        return self.sources.takeover('http://test-computer:8765')

    @staticmethod
    def pause_event(state):
        return {'sessionId': state['sessionId'], 'sequence': 1, 'index': 0,
                'positionMs': 16000, 'paused': True, 'audioIndex': -1,
                'subtitleIndex': -1, 'subtitleEnabled': False, 'subtitleDelayMs': 0}


class SourceRegistryTests(SourcesFixture):
    def test_source_cannot_change_while_local_video_is_playing_on_android(self):
        state = self.capture()
        session = self.local.session
        with self.assertRaises(SessionConflict):
            self.sources.select({'id': 'vlc'})
        self.assertEqual(self.registry.selected, 'windows')
        self.assertIs(self.local.session, session)
        self.assertEqual(self.local.session.id, state['sessionId'])
        self.assertEqual(self.players['vlc'].restores, [])

    def test_source_cannot_change_while_youtube_is_playing_on_android(self):
        self.sources.select({'id': self.youtube.source_id})
        self.youtube.owner = 'android'
        with self.assertRaises(SessionConflict):
            self.sources.select({'id': 'windows'})
        self.assertEqual(self.registry.selected, self.youtube.source_id)
        self.assertEqual(self.youtube.events, [])

    def test_refreshing_selected_source_does_not_interrupt_android_owner(self):
        self.capture()
        session = self.local.session
        result = self.sources.select({'id': 'windows'})
        self.assertEqual(result['selected'], 'windows')
        self.assertIs(self.local.session, session)
        self.assertEqual(self.local.session.owner, 'android')

    def test_paused_source_switch_invalidates_delayed_phone_events(self):
        state = self.capture()
        event = self.pause_event(state)
        self.sources.progress(event)
        self.assertEqual(self.local.session.owner, 'pc')
        self.sources.select({'id': 'vlc'})
        self.assertIsNone(self.local.session)
        with self.assertRaises(SessionConflict):
            self.sources.progress(dict(event, sequence=2))
        self.sources.select({'id': 'windows'})
        with self.assertRaises(SessionConflict):
            self.sources.progress(dict(event, sequence=3))
        self.assertEqual(len(self.players['windows'].restores), 1)
        self.assertEqual(self.players['vlc'].restores, [])

    def test_invalid_source_preserves_the_paused_session(self):
        state = self.capture()
        self.sources.progress(self.pause_event(state))
        session = self.local.session
        with self.assertRaises(ValueError):
            self.sources.select({'id': 'unknown-player'})
        self.assertEqual(self.registry.selected, 'windows')
        self.assertIs(self.local.session, session)

    def test_windows_native_queue_does_not_read_potplayer_playlist_registry(self):
        with patch('synchro.source_registry.discover_playlist') as potplayer_discovery:
            result = self.registry.playlist_selection(self.first)
        self.assertIs(result, self.windows_selection)
        self.players['windows'].playlist_selection.assert_called_once_with(self.first, None)
        potplayer_discovery.assert_not_called()

    def test_vlc_native_queue_does_not_read_potplayer_playlist_registry(self):
        self.sources.select({'id': 'vlc'})
        with patch('synchro.source_registry.discover_playlist') as potplayer_discovery:
            result = self.registry.playlist_selection(self.first)
        self.assertIs(result, self.vlc_selection)
        self.players['vlc'].playlist.assert_called_once_with()
        potplayer_discovery.assert_not_called()

    def test_catalog_contains_media_title_but_no_local_path_or_private_error(self):
        self.players['mpv'].read_basic = Mock(side_effect=OSError(str(self.root / 'private.json')))
        self.registry.catalog_at = 0
        result = self.registry.catalog(wait=True)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn('private.json', serialized)
        self.assertNotIn('path', serialized)
        windows = next(item for item in result['players'] if item['id'] == 'windows')
        self.assertEqual(windows['media']['title'], 'Episode 01')
        self.assertTrue(windows['ready'])

    def test_only_local_source_selection_is_restored_after_restart(self):
        self.sources.select({'id': 'vlc'})
        self.assertEqual(json.loads(self.registry.settings.read_text(encoding='utf-8')), {'selected': 'vlc'})
        self.sources.select({'id': self.youtube.source_id})
        self.assertEqual(json.loads(self.registry.settings.read_text(encoding='utf-8')), {'selected': 'potplayer'})


class SourceHTTPTests(SourcesFixture):
    def setUp(self):
        super().setUp()
        self.server = HandoffHTTPServer(('127.0.0.1', 0), self.sources)
        self.server.sources = self.registry
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, method, path, data=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        payload = json.dumps(data).encode('utf-8') if data is not None else None
        request_headers = {'Content-Type': 'application/json'} if data is not None else {}
        request_headers.update(headers or {})
        connection.request(method, path, body=payload, headers=request_headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_player_catalog_and_remote_selection_require_pairing(self):
        self.assertEqual(self.request('GET', '/api/players')[0], 401)
        self.assertEqual(self.request('POST', '/api/players/select', {'id': 'vlc'})[0], 401)
        self.assertEqual(self.registry.selected, 'windows')
        headers = {'Authorization': 'Bearer ' + self.local.token}
        status, _, body = self.request('GET', '/api/players', headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['selected'], 'windows')
        status, _, body = self.request('POST', '/api/players/select', {'id': 'vlc'}, headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['selected'], 'vlc')

    def test_remote_source_switch_reports_playback_ownership_conflict(self):
        self.capture()
        status, _, _ = self.request('POST', '/api/players/select', {'id': 'vlc'},
                                    {'Authorization': 'Bearer ' + self.local.token})
        self.assertEqual(status, 409)
        self.assertEqual(self.registry.selected, 'windows')

    def test_local_selection_rejects_foreign_host_and_origin_before_mutation(self):
        for headers in ({'Host': 'foreign.example'}, {'Origin': 'https://foreign.example'},
                        {'Origin': 'http://127.0.0.1:1'}, {'Host': '127.0.0.1.evil.example'}):
            with self.subTest(headers=headers):
                status, _, body = self.request('POST', '/local/players/select', {'id': 'vlc'}, headers)
                self.assertEqual(status, 403)
                self.assertEqual(self.registry.selected, 'windows')
                self.assertNotIn(self.local.token.encode(), body)

    def test_local_selection_accepts_same_origin_desktop_wizard(self):
        origin = f'http://127.0.0.1:{self.server.server_port}'
        status, _, body = self.request('POST', '/local/players/select', {'id': 'vlc'}, {'Origin': origin})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['selected'], 'vlc')


if __name__ == '__main__':
    unittest.main()
