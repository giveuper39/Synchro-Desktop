"""Paused-file fast path retains the loading-file stability check."""
import os
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
if os.name == 'nt':
    from synchro.player import WindowsPlayer


@unittest.skipUnless(os.name == 'nt', 'Windows native adapter')
class PlayerPauseTests(unittest.TestCase):
    def test_established_paused_file_returns_without_commands_or_sleep(self):
        with patch('synchro.player.win.send', return_value=1) as send, \
                patch('synchro.player.time.sleep') as sleep:
            WindowsPlayer._pause(123)
        send.assert_called_once_with(123, 0x400, 0x5006)
        sleep.assert_not_called()

    def test_loading_file_waits_for_stable_pause_even_with_cached_paused_status(self):
        clock = [0.0]
        def advance(seconds):
            clock[0] += seconds
        with patch('synchro.player.win.send', return_value=1) as send, \
                patch('synchro.player.time.monotonic', side_effect=lambda: clock[0]), \
                patch('synchro.player.time.sleep', side_effect=advance):
            WindowsPlayer._pause(123, settle=True)
        self.assertGreaterEqual(clock[0], .3)
        self.assertTrue(any(call.args == (123, 0x400, 0x5007, 1) for call in send.call_args_list))
