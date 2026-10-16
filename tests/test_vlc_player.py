"""VLC live-queue handoff and explicit track-ID mapping."""
from copy import deepcopy
from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from synchro.local_media import local_media_path
from synchro.models import PlayerUnavailable
from synchro.vlc_player import VlcPlayer, _secret_blob


class MemoryVlc(VlcPlayer):
    def __init__(self, root):
        super().__init__(root)
        self.first, self.second = root / 'first.mkv', root / 'second.mkv'
        for path in (self.first, self.second):
            path.touch()
        self.status = {'protocol': 1, 'uri': self.first.as_uri(), 'state': 'playing',
                       'timeMs': 14501, 'durationMs': 60000, 'rate': 1,
                       'audioId': 8, 'subtitleId': 42, 'subtitleDelayMs': -250,
                       'audioTracks': [{'id': 3}, {'id': 8}],
                       'subtitleTracks': [{'id': 42}, {'id': 51}]}
        self.queue = {'type': 'node', 'children': [
            {'type': 'leaf', 'id': '7', 'uri': self.first.as_uri()},
            {'type': 'node', 'children': [{'type': 'leaf', 'id': '9', 'uri': self.second.as_uri()}]}]}
        self.commands = []
        self.ignore_subtitle = False

    def _request(self, resource='status', **parameters):
        if resource == 'playlist':
            return deepcopy(self.queue)
        if parameters:
            self.commands.append(parameters)
            command = parameters['command']
            if command == 'pl_forcepause':
                self.status['state'] = 'paused'
            elif command == 'pl_play':
                self.status.update(uri=self.second.as_uri(), state='playing', timeMs=0)
            elif command == 'audio_track':
                self.status['audioId'] = parameters['val']
            elif command == 'subtitle_track' and not self.ignore_subtitle:
                self.status['subtitleId'] = parameters['val']
            elif command == 'subdelay':
                self.status['subtitleDelayMs'] = round(float(parameters['val']) * 1000)
            elif command == 'rate':
                self.status['rate'] = float(parameters['val'])
            elif command == 'synchro_seek':
                self.status['timeMs'] = parameters['ms']
        return deepcopy(self.status)

    def _wait(self, predicate, message, timeout=8):
        if not predicate(self.status):
            raise PlayerUnavailable(message)
        return deepcopy(self.status)


class VlcTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.player = MemoryVlc(self.root)

    def tearDown(self):
        self.player.close()
        self.directory.cleanup()

    def test_capture_pauses_and_maps_non_contiguous_native_ids_to_ordinals(self):
        captured = self.player.capture()
        self.assertTrue(captured.paused)
        self.assertEqual((captured.position_ms, captured.audio_index, captured.subtitle_index,
                          captured.subtitle_delay_ms), (14501, 1, 0, -250))
        self.assertEqual(self.player.commands, [{'command': 'pl_forcepause'}])

    def test_restore_next_queue_entry_preserves_queue_and_restores_precise_settings(self):
        target = replace(self.player.capture(), path=self.player.second, position_ms=27381,
                         audio_index=0, subtitle_index=1, subtitle_delay_ms=420, rate=1.3)
        restored = self.player.restore(target)
        self.assertEqual((restored.path, restored.position_ms, restored.audio_index,
                          restored.subtitle_index, restored.subtitle_delay_ms, restored.rate),
                         (self.player.second, 27381, 0, 1, 420, 1.3))
        self.assertIn({'command': 'pl_play', 'id': '9'}, self.player.commands)
        self.assertIn({'command': 'audio_track', 'val': 3}, self.player.commands)
        self.assertIn({'command': 'subtitle_track', 'val': 51}, self.player.commands)
        self.assertIn({'command': 'synchro_seek', 'ms': 27381}, self.player.commands)
        self.assertFalse(any(item['command'] in {'volume', 'aspectratio', 'in_play'}
                             for item in self.player.commands))

    def test_disabled_subtitles_use_disable_id_and_do_not_require_track(self):
        target = replace(self.player.capture(), subtitle_enabled=False, subtitle_index=-1)
        restored = self.player.restore(target)
        self.assertFalse(restored.subtitle_enabled)
        self.assertIn({'command': 'subtitle_track', 'val': -1}, self.player.commands)

    def test_unconfirmed_track_change_is_not_acknowledged(self):
        target = replace(self.player.capture(), subtitle_index=1)
        self.player.ignore_subtitle = True
        with self.assertRaises(PlayerUnavailable):
            self.player.restore(target)

    def test_live_playlist_preserves_order_and_warns_about_unstreamable_entries(self):
        self.player.queue['children'].insert(1, {'type': 'leaf', 'uri': 'https://example.com/live'})
        self.player.queue['children'].append({'type': 'leaf', 'uri': (self.root / 'missing.mkv').as_uri()})
        queue = self.player.playlist()
        self.assertEqual(queue.paths, [self.player.first, self.player.second])
        self.assertEqual(queue.source, 'vlc-live')
        self.assertIn('2', queue.warning)

    def test_wrong_http_surface_and_nonfinite_state_are_rejected(self):
        with self.assertRaises(PlayerUnavailable):
            VlcPlayer._snapshot({'state': 'paused'})
        self.player.status['timeMs'] = float('nan')
        with self.assertRaises(PlayerUnavailable):
            self.player.read_basic()

    def test_existing_pause_does_not_send_a_toggle_or_pause_command(self):
        self.player.status['state'] = 'paused'
        self.player.pause()
        self.assertEqual(self.player.commands, [])

    def test_capture_rejects_playback_resumed_during_handoff(self):
        with patch.object(self.player, 'pause', return_value=None):
            with self.assertRaises(PlayerUnavailable):
                self.player.capture()

    def test_local_uri_decodes_unicode_and_does_not_accept_remote_urls(self):
        path = self.root / 'Серия с пробелом.mkv'
        path.touch()
        self.assertEqual(local_media_path(path.as_uri()), path)
        self.assertIsNone(local_media_path('https://example.com/file.mkv'))
        self.assertIsNone(local_media_path('file://other-host/shared.mkv'))
        self.assertIsNone(local_media_path('x\0y'))

    @unittest.skipUnless(os.name == 'nt', 'Windows per-user credential protection')
    def test_saved_password_is_dpapi_protected_and_reconnect_survives_restart(self):
        self.player.port, self.player._password = 24231, 'a' * 64
        self.player._save_connection()
        self.assertNotIn('a' * 64, self.player.state_path.read_text(encoding='utf-8'))
        reopened = VlcPlayer(self.root)
        try:
            self.assertEqual((reopened.port, reopened._password), (24231, 'a' * 64))
        finally:
            reopened.close()


if __name__ == '__main__':
    unittest.main()
