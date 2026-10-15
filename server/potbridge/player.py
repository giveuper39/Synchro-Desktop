"""Serialized PotPlayer adapter; all native calls stay on one worker thread."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import os
import subprocess
import time

from .models import PlayerSnapshot, PlayerUnavailable
from . import menus, win32 as win


class WindowsPlayer:
    def __init__(self, executable=None, pid=None):
        self.executable = Path(executable or Path(os.environ.get('ProgramFiles', 'C:/Program Files'))
                               / 'PotPlayer' / 'PotPlayerMini64.exe')
        self.pid = pid
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='potplayer')

    def close(self):
        self.worker.shutdown(wait=True)

    def _call(self, method, *args):
        # Native sends and waits have their own bounds. An outer Future timeout
        # would release the session lock while the worker still mutates PotPlayer.
        return self.worker.submit(method, *args).result()

    def _window(self):
        windows = win.windows_of_class({'PotPlayer64', 'PotPlayer'}, self.pid)
        if len(windows) != 1:
            raise PlayerUnavailable('Open one PotPlayer instance with a local video file')
        return windows[0]

    @staticmethod
    def _basic(window):
        path = Path(win.current_filename(window))
        if not path.is_file():
            raise PlayerUnavailable('Only accessible local video files are supported')
        position = win.send(window, 0x400, 0x5004)
        duration = win.send(window, 0x400, 0x5002)
        status = win.send(window, 0x400, 0x5006)
        speed = win.send(window, 0x400, 0x5015)
        return PlayerSnapshot(path, position, duration, status != 2,
                              rate=speed / 1000 if 200 <= speed <= 12000 else 1.0)

    def read_basic(self):
        return self._call(lambda: self._basic(self._window()))

    def pause(self):
        return self._call(lambda: self._pause(self._window()))

    @staticmethod
    def _pause(window, settle=False):
        # An established, paused file needs no repeated Pause commands or delay.
        # A newly opened file can still report its predecessor's cached status.
        if not settle and win.send(window, 0x400, 0x5006) == 1:
            return
        # GET_PLAY_STATUS is cached. During file loading, Pause may arrive
        # before the real engine enters Running and therefore do nothing.
        # Pause=1 is idempotent (unlike Toggle=0), so bounded retries are safe.
        deadline = time.monotonic() + 8
        stable_since = None
        while time.monotonic() < deadline:
            win.send(window, 0x400, 0x5007, 1)
            if win.send(window, 0x400, 0x5006) == 1:
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= .3:
                    return
            else:
                stable_since = None
            time.sleep(.1)
        raise PlayerUnavailable('PotPlayer did not remain paused')

    def capture(self):
        """Pause at the captured position. No volume, layout or video-filter writes."""
        return self._call(self._capture)

    def _capture(self):
        window = self._window()
        before = self._basic(window)
        tracks = menus.read_tracks(window)
        delay = menus.subtitle_delay(window)
        self._pause(window)
        paused = self._basic(window)
        if paused.path != before.path:
            raise PlayerUnavailable('The media changed during handoff; try again')
        if not paused.paused:
            raise PlayerUnavailable('PotPlayer did not pause')
        return replace(paused, audio_index=tracks.audio_index,
                       subtitle_index=tracks.subtitle_index,
                       subtitle_enabled=tracks.subtitle_enabled,
                       subtitle_delay_ms=delay)

    def restore(self, snapshot):
        return self._call(self._restore, snapshot)

    def _restore(self, requested):
        window = self._window()
        before = self._basic(window)
        self._pause(window)
        opening_file = before.path != requested.path
        if opening_file:
            if not self.executable.is_file():
                raise PlayerUnavailable('Set --player-exe to your PotPlayer executable')
            subprocess.Popen([str(self.executable), str(requested.path), '/current',
                              f'/seek={requested.position_ms / 1000:.3f}'],
                             creationflags=subprocess.CREATE_NO_WINDOW)
            def opened():
                return Path(win.current_filename(window)) == requested.path
            win.wait_for(opened, 8)
            # File opened does not yet guarantee that the splitter populated its tracks.
            time.sleep(.15)
        for attempt in range(3):
            self._pause(window, settle=opening_file)
            track_state = menus.apply_tracks(window, requested.audio_index, requested.subtitle_index,
                                              requested.subtitle_enabled)
            menus.subtitle_delay(window, requested.subtitle_delay_ms)
            # Older builds report zero for GET_SPEED; do not send an unsupported setter.
            speed = win.send(window, 0x400, 0x5015)
            if 200 <= speed <= 12000:
                win.send(window, 0x400, 0x5016, round(requested.rate * 1000))
            menus.exact_seek(window, requested.position_ms, track_state.keyframe_seek)
            self._pause(window, settle=opening_file)
            # Opening a file can finish after its filename becomes visible.
            # Re-read every required setting before acknowledging the handoff.
            observed_tracks = menus.read_tracks(window)
            observed_delay = menus.subtitle_delay(window)
            result = self._basic(window)
            if (result.path == requested.path and result.paused
                    and abs(result.position_ms - requested.position_ms) <= 250
                    and (requested.audio_index < 0 or observed_tracks.audio_index == requested.audio_index)
                    and (requested.subtitle_index < 0 or observed_tracks.subtitle_index == requested.subtitle_index)
                    and observed_tracks.subtitle_enabled == requested.subtitle_enabled
                    and observed_delay == requested.subtitle_delay_ms):
                return result
            time.sleep(.3)
        raise PlayerUnavailable('PotPlayer did not confirm all restored playback settings')
