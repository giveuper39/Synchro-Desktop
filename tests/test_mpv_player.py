"""mpv native-ID mapping, handoff verification, and bounded IPC."""
from copy import deepcopy
from dataclasses import replace
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from synchro.models import PlayerUnavailable
from synchro.mpv_player import MpvPlayer, NamedPipeClient


class MemoryMpv(MpvPlayer):
    def __init__(self, root):
        super().__init__(root)
        self.first, self.second = root / 'first.mkv', root / 'second.mkv'
        for path in (self.first, self.second):
            path.touch()
        self.properties = {'path': str(self.first), 'time-pos': 13.875, 'duration': 60,
                           'pause': False, 'speed': 1, 'sub-visibility': True, 'sub-delay': -.3,
                           'track-list': [{'id': 2, 'type': 'audio', 'selected': False},
                                          {'id': 9, 'type': 'audio', 'selected': True},
                                          {'id': 1, 'type': 'sub', 'selected': True},
                                          {'id': 4, 'type': 'sub', 'selected': False}],
                           'playlist': [{'filename': str(self.first)}, {'filename': str(self.second)}]}
        self.commands = []
        self.ignore_speed = False

    def _command(self, *arguments):
        if arguments[0] == 'get_property':
            return deepcopy(self.properties[arguments[1]])
        self.commands.append(arguments)
        if arguments[0] == 'set_property':
            name, value = arguments[1:]
            if name == 'playlist-pos':
                self.properties['path'] = self.properties['playlist'][value]['filename']
                self.properties['time-pos'] = 0
            elif name in {'aid', 'sid'}:
                kind = 'audio' if name == 'aid' else 'sub'
                for track in self.properties['track-list']:
                    if track['type'] == kind:
                        track['selected'] = track['id'] == value
            elif not (name == 'speed' and self.ignore_speed):
                self.properties[name] = value
        elif arguments[0] == 'seek':
            self.properties['time-pos'] = arguments[1]

    def _wait(self, predicate, message, timeout=8):
        if not predicate():
            raise PlayerUnavailable(message)


class MpvTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.player = MemoryMpv(self.root)

    def tearDown(self):
        self.player.close()
        self.directory.cleanup()

    def test_capture_uses_native_selection_and_pauses_without_volume_changes(self):
        result = self.player.capture()
        self.assertEqual((result.audio_index, result.subtitle_index, result.position_ms,
                          result.subtitle_delay_ms, result.paused), (1, 0, 13875, -300, True))
        self.assertEqual(self.player.commands, [('set_property', 'pause', True)])

    def test_restore_uses_existing_playlist_position_and_exact_seek(self):
        target = replace(self.player.capture(), path=self.player.second, position_ms=29671,
                         audio_index=0, subtitle_index=1, subtitle_delay_ms=650, rate=1.4)
        result = self.player.restore(target)
        self.assertEqual((result.path, result.position_ms, result.audio_index, result.subtitle_index,
                          result.subtitle_delay_ms, result.rate),
                         (self.player.second, 29671, 0, 1, 650, 1.4))
        self.assertIn(('set_property', 'playlist-pos', 1), self.player.commands)
        self.assertIn(('set_property', 'aid', 2), self.player.commands)
        self.assertIn(('set_property', 'sid', 4), self.player.commands)
        self.assertIn(('seek', 29.671, 'absolute+exact'), self.player.commands)
        self.assertFalse(any(command[0] == 'loadfile' or 'volume' in command
                             for command in self.player.commands))

    def test_hidden_selected_subtitle_stays_disabled(self):
        target = replace(self.player.capture(), subtitle_enabled=False)
        result = self.player.restore(target)
        self.assertFalse(result.subtitle_enabled)
        self.assertEqual(result.subtitle_index, 0)

    def test_selected_external_track_is_rejected_instead_of_using_wrong_ordinal(self):
        self.player.properties['track-list'][2]['external'] = True
        with self.assertRaises(PlayerUnavailable):
            self.player.capture()

    def test_missing_required_track_and_unconfirmed_speed_are_rejected(self):
        target = replace(self.player.capture(), audio_index=99)
        with self.assertRaises(PlayerUnavailable):
            self.player.restore(target)
        target = replace(target, audio_index=1, rate=1.5)
        self.player.ignore_speed = True
        with self.assertRaises(PlayerUnavailable):
            self.player.restore(target)

    def test_live_queue_excludes_urls_and_reports_omissions(self):
        self.player.properties['playlist'].append({'filename': 'https://example.com/live'})
        selection = self.player.playlist()
        self.assertEqual(selection.paths, [self.player.first, self.player.second])
        self.assertEqual(selection.source, 'mpv-live')
        self.assertIn('1', selection.warning)

    def test_nonfinite_timeline_and_relative_protocols_are_rejected(self):
        self.player.properties['time-pos'] = float('inf')
        with self.assertRaises(PlayerUnavailable):
            self.player.read_basic()
        self.player.properties['path'] = 'https://example.com/video'
        with self.assertRaises(PlayerUnavailable):
            self.player.read_basic()

    def test_capture_rejects_playback_resumed_during_handoff(self):
        with patch.object(self.player, 'pause', return_value=None):
            with self.assertRaises(PlayerUnavailable):
                self.player.capture()

    @unittest.skipUnless(os.name == 'nt', 'Windows named pipe deadlines')
    def test_a_nonresponsive_local_pipe_cannot_hang_the_player_worker(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                           wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                           wintypes.DWORD, ctypes.c_void_p]
        kernel.CreateNamedPipeW.restype = wintypes.HANDLE
        kernel.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        kernel.ConnectNamedPipe.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        name = '\\\\.\\pipe\\synchro-test-' + secrets.token_hex(16)
        handle = kernel.CreateNamedPipeW(name, 3, 0, 1, 4096, 4096, 0, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        release = threading.Event()
        def silent_server():
            try:
                kernel.ConnectNamedPipe(handle, None)
                release.wait(2)
            finally:
                kernel.CloseHandle(handle)
        thread = threading.Thread(target=silent_server, daemon=True)
        thread.start()
        began = time.monotonic()
        try:
            with self.assertRaises(PlayerUnavailable):
                NamedPipeClient(name, timeout=.15).command(['get_property', 'pause'])
            self.assertLess(time.monotonic() - began, .8)
        finally:
            release.set()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
