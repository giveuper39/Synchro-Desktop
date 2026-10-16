"""Loopback-only browser integration fixture. No real players, clipboard or private keys."""
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from synchro.extension_setup import ExtensionSetup
from synchro.http_server import HandoffHandler, HandoffHTTPServer
from synchro.library import MediaLibrary
from synchro.youtube import YouTubeCoordinator


def main():
    port = 18766
    youtube = YouTubeCoordinator(ROOT, SimpleNamespace(list_sessions=lambda: []))
    manager = SimpleNamespace(token='synthetic-browser-fixture-only', library=MediaLibrary(ffprobe=False),
                              describe=youtube.describe, youtube_takeover=lambda data: youtube.takeover(data['sourceId']),
                              youtube_progress=youtube.progress)
    links = []
    def send_link(data):
        links.append(data)
        return dict(ok=True)
    server = HandoffHTTPServer(('127.0.0.1', port), manager, hub=SimpleNamespace(send_link=send_link))
    server.youtube = youtube
    server.sources = SimpleNamespace(catalog=lambda: dict(selected='potplayer', players=youtube.catalog()))
    server.extension = ExtensionSetup(ROOT, ROOT / '.data/browser-fixture/install')
    server.extension.prepare()
    class Handler(HandoffHandler):
        def do_GET(self):
            if self.path == '/test/links' and self._authorized():
                self._reply(200, dict(links=links))
            else:
                super().do_GET()

        def do_POST(self):
            self.close_connection = True
            if self.path == '/test/reset' and self._authorized():
                with youtube.changed:
                    youtube.bridges.clear()
                    youtube.sources.clear()
                    youtube.session = None
                    youtube.changed.notify_all()
                links.clear()
                self._reply(200, dict(ok=True))
            elif self.path == '/test/shutdown' and self._authorized():
                self._reply(200, dict(ok=True))
                threading.Thread(target=server.shutdown, daemon=True).start()
            else:
                super().do_POST()
    server.RequestHandlerClass = Handler
    try:
        server.serve_forever(poll_interval=.1)
    finally:
        youtube.close()
        server.server_close()


if __name__ == '__main__':
    main()
