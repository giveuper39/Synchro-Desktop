"""Windows adapter contract: scoped files, verified state and safe fallback."""
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from synchro.models import PlayerUnavailable
from synchro.windows_media import SystemMedia, WindowsMediaPlayer, WINDOWS_MEDIA_APP


class FakeSystemMedia:
    def __init__(self, path):
        self.path = path
        self.files = [path]
        self.calls = []
        self.state = dict(id=WINDOWS_MEDIA_APP, appId=WINDOWS_MEDIA_APP, title=path.stem,
                          positionMs=12000, durationMs=120000, paused=False,
                          canPause=True, canSeek=True)
        self.ignore_paused_seek = False

    def list_sessions(self):
        return [dict(self.state)]

    def open_media_files(self):
        return list(self.files)

    def open_media_file(self, path):
        self.calls.append(('open', path))
        self.path, self.files = path, [path]
        self.state.update(title=path.stem, positionMs=0, paused=False)

    def command(self, identity, action, **values):
        self.calls.append((action, values))
        if action in {'pause', 'play'}:
            self.state['paused'] = action == 'pause'
        elif not (self.ignore_paused_seek and self.state['paused']):
            self.state['positionMs'] = values['positionMs']
        return dict(self.state)


class WindowsMediaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.first = self.folder / 'Episode 2.mkv'
        self.second = self.folder / 'Episode 10.mkv'
        for path in (self.first, self.second):
            path.write_bytes(b'own media fixture')
        self.system = FakeSystemMedia(self.first)
        self.player = WindowsMediaPlayer(self.folder, self.system)

    def test_capture_pauses_and_reads_the_actual_local_file(self):
        state = self.player.capture()
        self.assertEqual(state.path, self.first)
        self.assertTrue(state.paused)
        self.assertEqual(state.position_ms, 12000)
        self.assertEqual(self.system.calls, [('pause', {})])
        self.assertEqual((state.audio_index, state.subtitle_index), (-1, -1))

    def test_already_paused_capture_does_not_toggle_playback(self):
        self.system.state['paused'] = True
        self.player.capture()
        self.assertEqual(self.system.calls, [])

    def test_title_matches_current_file_among_preloaded_handles(self):
        self.system.files.append(self.second)
        self.assertEqual(self.player.read_basic().path, self.first)

    def test_ambiguous_file_is_rejected_instead_of_choosing_another_video(self):
        self.system.files.append(self.second)
        self.system.state['title'] = 'Ambiguous title'
        with self.assertRaises(PlayerUnavailable):
            self.player.read_basic()
        self.player.select_file(self.second)
        self.assertEqual(self.player.read_basic().path, self.second)

    def test_explicit_file_fallback_must_match_the_current_title(self):
        self.player.select_file(self.first)
        self.system.files = []
        self.assertEqual(self.player.read_basic().path, self.first)
        self.system.state['title'] = self.second.stem
        with self.assertRaises(PlayerUnavailable):
            self.player.read_basic()

    def test_other_apps_are_never_implicitly_selected(self):
        self.system.state['appId'] = 'chrome.exe'
        with self.assertRaises(PlayerUnavailable):
            self.player.capture()
        self.assertEqual(self.system.calls, [])

    def test_restoring_a_later_episode_opens_it_seeks_and_leaves_it_paused(self):
        requested = replace(self.player.capture(), path=self.second, position_ms=34000)
        result = self.player.restore(requested)
        self.assertEqual((result.path, result.position_ms, result.paused),
                         (self.second, 34000, True))
        self.assertIn(('open', self.second), self.system.calls)
        self.assertNotIn(('play', {}), self.system.calls)

    def test_ignored_paused_seek_has_bounded_running_seek_then_pause(self):
        self.system.ignore_paused_seek = True
        requested = replace(self.player.capture(), position_ms=34000)
        clock = [0.]
        with patch('synchro.windows_media.time.monotonic', side_effect=lambda: clock[0]), \
                patch('synchro.windows_media.time.sleep', side_effect=lambda value: clock.__setitem__(0, clock[0] + value)):
            result = self.player.restore(requested)
        self.assertTrue(result.paused)
        self.assertEqual(result.position_ms, 34000)
        self.assertIn(('play', {}), self.system.calls)
        self.assertEqual(self.system.calls[-1], ('pause', {}))

    def test_folder_playlist_is_natural_order_and_explains_missing_queue_and_tracks(self):
        (self.folder / 'not-media.txt').write_text('ignored', encoding='utf-8')
        selection = self.player.playlist_selection(self.first)
        self.assertEqual(selection.paths, [self.first, self.second])
        self.assertEqual(selection.source, 'directory')
        self.assertIn('субтитры', selection.warning)
        self.assertFalse(self.player.capabilities['subtitles'])

    def test_generic_commands_reject_invalid_positions_before_native_call(self):
        media = SystemMedia(self.folder)
        with patch.object(media, '_request') as request:
            for position in (True, -1, 1.5, '15'):
                with self.assertRaises(ValueError):
                    media.command('chrome.exe', 'seek', positionMs=position)
            with self.assertRaises(ValueError):
                media.command('chrome.exe', 'toggle')
            request.assert_not_called()
            media.command('chrome.exe', 'seek', positionMs=35000)
            request.assert_called_once_with('seek', sessionId='chrome.exe', positionMs=35000)


if __name__ == '__main__':
    unittest.main()
