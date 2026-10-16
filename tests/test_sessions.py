"""Ownership, failed restores, and out-of-order network event integration tests."""
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from synchro.library import MediaLibrary
from synchro.models import PlayerSnapshot, PlayerUnavailable, SessionConflict
from synchro.playlist import PlaylistSelection
from synchro.sessions import SessionManager


class FakePlayer:
    def __init__(self, path):
        self.snapshot = PlayerSnapshot(path, 12345, 120000, True, 0, 0, True, 500)
        self.restores = []
        self.fail_restore = False
        self.pause_count = 0

    def capture(self):
        return self.snapshot

    def read_basic(self):
        return self.snapshot

    def pause(self):
        self.pause_count += 1
        self.snapshot = replace(self.snapshot, paused=True)

    def restore(self, snapshot):
        if self.fail_restore:
            raise PlayerUnavailable('Busy')
        self.restores.append(snapshot)
        self.snapshot = replace(snapshot, paused=True)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.first, self.second = self.root / 'episode1.mkv', self.root / 'episode2.mkv'
        for path in (self.first, self.second):
            path.write_bytes(b'fixture')
        self.clock = [10.0]
        self.player = FakePlayer(self.first)
        self.manager = SessionManager(self.player, MediaLibrary(ffprobe=False), 'test-pairing-key',
                                      self.root / 'private', clock=lambda: self.clock[0])
        self.selection = PlaylistSelection([self.first, self.second], 'saved-playlist')
        with patch('synchro.sessions.discover_playlist', return_value=self.selection):
            self.state = self.manager.takeover('http://computer:8765')

    def event(self, **changes):
        data = {'sessionId': self.state['sessionId'], 'sequence': 1, 'index': 0,
                'positionMs': 45000, 'paused': False, 'audioIndex': 0,
                'subtitleIndex': 0, 'subtitleEnabled': True, 'subtitleDelayMs': 500}
        data.update(changes)
        return data

    def test_streaming_progress_does_not_run_the_paused_pc(self):
        self.manager.progress(self.event())
        self.assertEqual(self.player.restores, [])
        self.assertTrue(self.player.snapshot.paused)
        self.assertEqual(self.manager.session.snapshot.position_ms, 45000)

    def test_pause_returns_next_episode_tracks_and_delay_to_pc(self):
        self.manager.progress(self.event(index=1, positionMs=70000, paused=True,
                                         audioIndex=2, subtitleDelayMs=-750))
        restored = self.player.restores[-1]
        self.assertEqual((restored.path, restored.position_ms, restored.audio_index,
                          restored.subtitle_delay_ms), (self.second, 70000, 2, -750))
        self.assertTrue(restored.paused)
        self.assertEqual(self.manager.session.owner, 'pc')
        self.assertNotIn('volume', self.manager.describe('http://computer:8765'))

    def test_delayed_packet_cannot_restore_previous_episode(self):
        self.manager.progress(self.event(sequence=3, index=1, paused=True))
        answer = self.manager.progress(self.event(sequence=2, index=0, paused=True))
        self.assertTrue(answer['ignored'])
        self.assertEqual(len(self.player.restores), 1)
        self.assertEqual(self.player.restores[-1].path, self.second)

    def test_failed_pause_can_be_retried_with_the_same_event_number(self):
        self.player.fail_restore = True
        with self.assertRaises(PlayerUnavailable):
            self.manager.progress(self.event(paused=True))
        self.assertEqual(self.manager.session.sequence, 0)
        self.player.fail_restore = False
        self.manager.progress(self.event(paused=True))
        self.assertEqual(self.manager.session.sequence, 1)

    def test_resume_phone_pauses_the_computer(self):
        self.manager.progress(self.event(paused=True))
        self.manager.progress(self.event(sequence=2, paused=False))
        self.assertEqual(self.player.pause_count, 1)
        self.assertEqual(self.manager.session.owner, 'android')

    def test_manually_resumed_pc_revokes_the_old_receiver(self):
        self.player.snapshot = replace(self.player.snapshot, paused=False)
        self.manager.watchdog()
        with self.assertRaises(SessionConflict):
            self.manager.progress(self.event(paused=True))
        self.assertEqual(self.player.restores, [])

    def test_disconnect_checkpoints_the_last_confirmed_position(self):
        self.manager.progress(self.event(index=1, positionMs=80000))
        self.clock[0] += 16
        self.manager.watchdog()
        self.assertEqual(self.player.restores[-1].path, self.second)
        self.assertEqual(self.player.restores[-1].position_ms, 80000)
        self.assertEqual(self.manager.session.owner, 'pc')
        self.assertTrue(self.manager.session.revoked)
        with self.assertRaises(SessionConflict):
            self.manager.progress(self.event(sequence=2, paused=False))

    def test_pc_resume_between_watchdog_ticks_rejects_delayed_pause(self):
        self.player.snapshot = replace(self.player.snapshot, position_ms=60000, paused=False)
        with self.assertRaises(SessionConflict):
            self.manager.progress(self.event(paused=True))
        self.assertEqual(self.player.restores, [])
        self.assertEqual(self.player.snapshot.position_ms, 60000)
        self.assertTrue(self.manager.session.revoked)

    def test_manually_opened_paused_file_is_not_overwritten_by_receiver(self):
        self.player.snapshot = replace(self.player.snapshot, path=self.second)
        with self.assertRaises(SessionConflict):
            self.manager.progress(self.event(paused=True))
        self.assertEqual(self.player.restores, [])

    def test_phone_must_capture_again_after_pc_resumes_from_handoff(self):
        self.manager.progress(self.event(paused=True))
        self.player.snapshot = replace(self.player.snapshot, paused=False, position_ms=80000)
        with self.assertRaises(SessionConflict):
            self.manager.progress(self.event(sequence=2, paused=False))
        self.assertEqual(self.player.pause_count, 0)

    def test_paused_phone_setting_change_cannot_replace_a_new_pc_file(self):
        self.manager.progress(self.event(paused=True))
        self.player.snapshot = replace(self.player.snapshot, path=self.second)
        with self.assertRaises(SessionConflict):
            self.manager.progress(self.event(sequence=2, paused=True, subtitleDelayMs=900))
        self.assertEqual(len(self.player.restores), 1)

    def test_manual_seek_on_paused_pc_takes_priority_over_phone_progress(self):
        self.player.snapshot = replace(self.player.snapshot, position_ms=90000)
        with self.assertRaises(SessionConflict):
            self.manager.progress(self.event(paused=True))
        self.assertEqual(self.player.restores, [])

    def test_reconnect_invalidates_previous_session_events(self):
        old = self.event()
        with patch('synchro.sessions.discover_playlist', return_value=self.selection):
            self.manager.takeover('http://computer:8765')
        with self.assertRaises(SessionConflict):
            self.manager.progress(old)

    def test_invalid_values_do_not_touch_the_player(self):
        for changes in ({'index': 2}, {'positionMs': -1}, {'paused': 'false'},
                        {'rate': float('nan')}, {'audioIndex': True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.manager.progress(self.event(**changes))
        self.assertEqual(self.player.restores, [])
        self.assertEqual(self.manager.session.sequence, 0)

    def test_resume_unchanged_pc_allows_buffer_reuse_and_rotates_session_id(self):
        self.manager.progress(self.event(paused=True, audioIndex=1, subtitleDelayMs=-300))
        with patch('synchro.sessions.discover_playlist', return_value=self.selection), \
                patch.object(self.manager.library, 'preload') as preload:
            resumed = self.manager.resume({'sessionId': self.state['sessionId']}, 'http://computer:8765')
        self.assertTrue(resumed['resumeInPlace'])
        self.assertNotEqual(resumed['sessionId'], self.state['sessionId'])
        self.assertEqual((resumed['positionMs'], resumed['audioIndex'], resumed['subtitleDelayMs']),
                         (45000, 1, -300))
        self.assertEqual(resumed['owner'], 'android')
        preload.assert_not_called()
        with self.assertRaises(SessionConflict):
            self.manager.progress(self.event(sequence=2, paused=True, positionMs=90000))

    def _resume_after_pc_edit(self, **changes):
        self.manager.progress(self.event(paused=True))
        self.player.snapshot = replace(self.player.snapshot, **changes)
        with patch('synchro.sessions.discover_playlist', return_value=self.selection):
            return self.manager.resume({'sessionId': self.state['sessionId']}, 'http://computer:8765')

    def test_resume_after_pc_seek_uses_the_new_pc_position(self):
        resumed = self._resume_after_pc_edit(position_ms=85000)
        self.assertFalse(resumed['resumeInPlace'])
        self.assertEqual(resumed['positionMs'], 85000)

    def test_resume_after_pc_episode_change_uses_the_new_file(self):
        resumed = self._resume_after_pc_edit(path=self.second, position_ms=5000)
        self.assertFalse(resumed['resumeInPlace'])
        self.assertEqual((resumed['index'], resumed['positionMs']), (1, 5000))

    def test_resume_detects_pc_audio_subtitle_delay_and_speed_edits(self):
        original = self.player.snapshot
        for edit in ({'audio_index': 1}, {'subtitle_index': 1}, {'subtitle_enabled': False},
                     {'subtitle_delay_ms': -900}, {'rate': 1.5}):
            with self.subTest(edit=edit):
                self.player.snapshot = original
                with patch('synchro.sessions.discover_playlist', return_value=self.selection):
                    self.state = self.manager.takeover('http://computer:8765')
                self.assertFalse(self._resume_after_pc_edit(**edit)['resumeInPlace'])

    def test_resume_detects_saved_playlist_reorder(self):
        self.manager.progress(self.event(paused=True))
        reordered = PlaylistSelection([self.second, self.first], 'saved-playlist')
        with patch('synchro.sessions.discover_playlist', return_value=reordered):
            resumed = self.manager.resume({'sessionId': self.state['sessionId']}, 'http://computer:8765')
        self.assertFalse(resumed['resumeInPlace'])
        self.assertEqual(resumed['index'], 1)

    def test_unknown_or_unconfirmed_pause_cannot_reuse_the_buffer(self):
        with patch('synchro.sessions.discover_playlist', return_value=self.selection):
            resumed = self.manager.resume({'sessionId': self.state['sessionId']}, 'http://computer:8765')
        self.assertFalse(resumed['resumeInPlace'])
        self.manager.progress(dict(self.event(paused=True), sessionId=resumed['sessionId']))
        with patch('synchro.sessions.discover_playlist', return_value=self.selection):
            unknown = self.manager.resume({'sessionId': 'obsolete'}, 'http://computer:8765')
        self.assertFalse(unknown['resumeInPlace'])

    def test_resume_rejects_missing_or_non_string_session_ids(self):
        for value in (None, 4, True, '', 'x' * 257):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.manager.resume({'sessionId': value}, 'http://computer:8765')


if __name__ == '__main__':
    unittest.main()
