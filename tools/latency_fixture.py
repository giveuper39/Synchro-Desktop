"""Isolated slow-PC fixture for Android instrumentation; never controls PotPlayer.

Supply two short generated MKV files with audio. The journal belongs in .data,
not beside source files. Bind to loopback and use adb reverse, or explicitly
choose a LAN interface for a dedicated test emulator.
"""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from potbridge.models import PlayerSnapshot
from potbridge.library import MediaLibrary
from potbridge.sessions import SessionManager
from potbridge.http_server import HandoffHTTPServer, HandoffHandler


class SlowPlayer:
    def __init__(self, paths):
        self.paths = paths
        self.state = PlayerSnapshot(paths[0], 72000, 180000, True, 0, 0, False, 0)

    def capture(self):
        time.sleep(.65)
        self.state = replace(self.state, paused=True)
        return self.state

    def read_basic(self):
        return self.state

    def pause(self):
        self.state = replace(self.state, paused=True)

    def restore(self, snapshot):
        time.sleep(.8)
        self.state = replace(snapshot, paused=True)
        return self.state


class FixtureHandler(HandoffHandler):
    def do_POST(self):
        if self.path == '/test/pc' and self._authorized():
            changes = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            with self.server.manager.lock:
                player = self.server.manager.player
                fields = {key: value for key, value in changes.items()
                          if key in {'position_ms', 'audio_index', 'subtitle_delay_ms'}}
                if 'index' in changes:
                    fields['path'] = player.paths[changes['index']]
                player.state = replace(player.state, **fields)
            self.close_connection = True
            self._reply(200, {'ok': True})
            return
        super().do_POST()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--media-dir', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--token', required=True, help='Disposable test credential, not the real pairing key')
    parser.add_argument('--bind', default='127.0.0.1')
    args = parser.parse_args()
    files = sorted(args.media_dir.resolve().glob('*.mkv'))
    if len(files) != 2:
        parser.error('Provide a fixture directory with exactly two generated MKV files')
    manager = SessionManager(SlowPlayer(files), MediaLibrary(), args.token, args.data_dir)
    server = HandoffHTTPServer((args.bind, 18765), manager)
    server.RequestHandlerClass = FixtureHandler
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
