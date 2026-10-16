"""Authenticated JSON control API and seekable, byte-for-byte HTTP file streaming."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import socket
import json
import mimetypes
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit

from . import __version__
from .models import PlayerUnavailable, SessionConflict
from .pairing import pairing_page, download_page


class RangeNotSatisfiable(ValueError):
    pass


def byte_range(header, size):
    """Return an inclusive single range, including suffix/open-ended requests."""
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', header or '')
    if not match or not any(match.groups()) or size <= 0:
        raise RangeNotSatisfiable('Unsupported byte range')
    first, last = match.groups()
    if not first:
        length = int(last)
        if length == 0:
            raise RangeNotSatisfiable('Empty suffix range')
        return max(0, size - length), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        raise RangeNotSatisfiable('Byte range outside the file')
    return start, end


class HandoffHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != 'nt'

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, address, manager, receiver_apk=None, pairing_host=None, hub=None):
        self.manager = manager
        self.receiver_apk = Path(receiver_apk) if receiver_apk else None
        self.pairing_host = pairing_host
        self.hub = hub
        super().__init__(address, HandoffHandler)


class HandoffHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = f'Synchro/{__version__}'

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def handle(self):
        # A player may abort a range transfer or reset an idle keep-alive
        # socket on pause/seek. This also covers Windows 10053 and 10054.
        try:
            super().handle()
        except (ConnectionError, TimeoutError):
            self.close_connection = True

    def finish(self):
        try:
            super().finish()
        except (ConnectionError, TimeoutError):
            pass

    def log_message(self, *args):
        # Media URLs contain a pairing secret; never log URLs or request headers.
        pass

    def _base_url(self):
        host = self.headers.get('Host')
        if not host or '/' in host or len(host) > 255:
            raise ValueError('A valid Host header is required')
        return 'http://' + host

    def _authorized(self):
        received = self.headers.get('Authorization', '').encode('utf-8')
        expected = ('Bearer ' + self.server.manager.token).encode('utf-8')
        return secrets.compare_digest(received, expected)

    def _hub(self):
        if self.server.hub is None:
            raise PlayerUnavailable('Запустите обновлённый сервис «Synchro»')
        return self.server.hub

    def _extension(self, preflight=False):
        from .extension import extension_id
        identity = extension_id()
        local = (self.client_address[0] in ('127.0.0.1', '::1')
                 and re.fullmatch(r'(127\.0\.0\.1|localhost)(:\d{1,5})?', self.headers.get('Host', ''), re.I))
        origin = self.headers.get('Origin')
        return bool(local and origin in (None, 'chrome-extension://' + identity)
                    and (origin == 'chrome-extension://' + identity if preflight
                         else (self.headers.get('X-Synchro-Extension')
                               or self.headers.get('X-Syncro-Extension')) == identity))

    def _local(self):
        if not (self.client_address[0] in ('127.0.0.1', '::1')
                and re.fullmatch(r'(127\.0\.0\.1|localhost)(:\d{1,5})?', self.headers.get('Host', ''), re.I)):
            return False
        return self.headers.get('Origin') in (None, 'http://' + self.headers.get('Host'))

    def do_OPTIONS(self):
        if not self._extension(preflight=True):
            self._reply(403, {'error': 'Origin is not allowed'})
            return
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', self.headers['Origin'])
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type, X-Synchro-Extension, X-Syncro-Extension')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _reply(self, status, body, content_type='application/json; charset=utf-8', headers=None):
        payload = json.dumps(body, ensure_ascii=False).encode('utf-8') if isinstance(body, dict) else body
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        if self.headers.get('Origin', '').startswith('chrome-extension://') and self._extension():
            self.send_header('Access-Control-Allow-Origin', self.headers['Origin'])
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(payload)

    def _error(self, error):
        if isinstance(error, SessionConflict):
            status, message = 409, str(error)
        elif isinstance(error, PlayerUnavailable):
            status, message = 503, str(error)
        elif isinstance(error, (ValueError, KeyError, TypeError)):
            status, message = 400, str(error)
        else:
            # Filesystem/subprocess errors can contain private computer paths.
            status, message = 500, 'The computer could not complete this operation'
        self._reply(status, {'error': message})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            path = urlsplit(self.path).path
            if path == '/':
                self._reply(200, download_page().encode('utf-8'), 'text/html; charset=utf-8')
            elif path == '/setup' and self._local():
                from .setup_page import setup_page
                self._reply(200, setup_page().encode('utf-8'), 'text/html; charset=utf-8')
            elif path == '/local/setup/status' and self._local():
                self._reply(200, self._hub().wireless.status())
            elif path == '/local/updates/status' and self._local():
                self._reply(200, self.server.updates.status())
            elif path == '/browser/connect' and self._local():
                from .browser_page import connection_page
                page = connection_page()
                self._reply(200, page if isinstance(page, bytes) else page.encode('utf-8'), 'text/html; charset=utf-8')
            elif path == '/local/browser/bookmarklet' and self._local():
                from .browser_page import bookmarklet
                self._reply(200, {'bookmarklet': bookmarklet(f'http://127.0.0.1:{self.server.server_port}')})
            elif path == '/local/players' and self._local():
                self._reply(200, self.server.sources.catalog())
            elif path == '/ui/Manrope.ttf':
                self._file(Path(__file__).resolve().parents[2] / 'assets/Manrope.ttf', 'font/ttf')
            elif (path == '/pair' and self.client_address[0] in ('127.0.0.1', '::1')
                  and self.headers.get('Host', '').split(':')[0].lower() in ('localhost', '127.0.0.1')):
                self._pair_page()
            elif path in ('/synchro.apk', '/receiver.apk', '/syncro.apk') and self.server.receiver_apk and self.server.receiver_apk.is_file():
                self._file(self.server.receiver_apk, 'application/vnd.android.package-archive')
            elif path == '/chrome-extension.zip' and self.server.receiver_apk:
                self._file(self.server.receiver_apk.parent / 'chrome-extension.zip', 'application/zip')
            elif path.startswith('/media/'):
                parts = path.split('/')
                if len(parts) != 4 or not secrets.compare_digest(parts[2], self.server.manager.token):
                    self._reply(401, {'error': 'Pairing key required'})
                    return
                item = self.server.manager.library.get(parts[3])
                if item is None:
                    self._reply(404, {'error': 'Media is not in the granted playlist'})
                    return
                self._file(item.path)
            elif path.startswith('/api/'):
                if not self._authorized():
                    self._reply(401, {'error': 'Pairing key required'})
                elif path == '/api/session':
                    self._reply(200, self.server.manager.describe(self._base_url()))
                elif path == '/api/health':
                    self._reply(200, {'ok': True, 'version': __version__})
                elif path == '/api/sync':
                    self._reply(200, self._hub().status())
                elif path == '/api/clipboard':
                    self._reply(200, self._hub().clipboard_read())
                elif path == '/api/clipboard/image':
                    data, origin = self._hub().clipboard_image_read()
                    self._reply(200, data, 'image/png', {'X-Synchro-Clipboard-Origin': origin})
                elif path == '/api/devices':
                    self._reply(200, {'devices': self._hub().adb.devices()})
                elif path == '/api/players':
                    if hasattr(self.server, 'sources'):
                        self._reply(200, self.server.sources.catalog())
                    else:
                        # Older isolated fixtures and a configured single adapter still work.
                        self._reply(200, {'selected':'potplayer', 'players':[{'id':'potplayer','name':'PotPlayer','kind':'file','ready':True,'detail':'Видео с компьютера'}]})
                elif path == '/api/players/preview':
                    picture, source = self.server.sources.preview()
                    self._reply(200, picture, 'image/jpeg', {'X-Synchro-Source': source})
                elif path == '/api/youtube/session':
                    self._reply(200, self.server.youtube.describe())
                else:
                    self._reply(404, {'error': 'Unknown API endpoint'})
            else:
                self._reply(404, {'error': 'Not found'})
        except (ConnectionError, TimeoutError):
            self.close_connection = True
            return
        except Exception as error:
            self._error(error)

    def _pair_page(self):
        host = self.server.pairing_host or '127.0.0.1'
        port = self.server.server_port
        page = pairing_page(f'http://{host}:{port}', self.server.manager.token)
        self._reply(200, page.encode('utf-8'), 'text/html; charset=utf-8')

    def do_POST(self):
        # Close rejected POSTs so an unread request body cannot be interpreted as a new request.
        self.close_connection = True
        path = urlsplit(self.path).path
        if path.startswith('/local/browser/') or path in ('/local/players/open', '/local/players/select'):
            if not self._local():
                self._reply(403, {'error': 'Open this connection on the computer'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16384:
                    raise ValueError('Invalid local request')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('JSON object required')
                if path == '/local/browser/register':
                    result = self.server.youtube.register_bridge(data)
                elif path == '/local/browser/poll':
                    result = self.server.youtube.bridge_poll(data)
                elif path == '/local/browser/disconnect':
                    result = self.server.youtube.disconnect_bridge(data)
                elif path == '/local/players/select':
                    result = self.server.manager.select(data)
                elif path == '/local/players/open':
                    from .file_dialog import choose_media
                    if self.server.manager._phone_owns():
                        raise SessionConflict('Сначала поставьте просмотр на телефоне на паузу')
                    paths = choose_media()
                    if paths:
                        with self.server.manager.lock:
                            if self.server.manager._phone_owns():
                                raise SessionConflict('Сначала поставьте просмотр на телефоне на паузу')
                            self.server.sources.open(data.get('id'), paths)
                    result = {'ok': True, 'cancelled': not bool(paths)}
                else:
                    self._reply(404, {'error':'Unknown browser endpoint'})
                    return
                self._reply(200, result)
            except Exception as error:
                self._error(error)
            return
        if path.startswith('/local/updates/'):
            if not self._local():
                self._reply(403, {'error': 'Open updates on this computer'})
                return
            try:
                if path == '/local/updates/check':
                    result = self.server.updates.check()
                elif path == '/local/updates/install':
                    if self.server.manager.describe(self._base_url()).get('owner') == 'android':
                        raise SessionConflict('Остановите просмотр на телефоне перед обновлением ПК')
                    result = self.server.updates.install(self.server.shutdown)
                else:
                    self._reply(404, {'error': 'Unknown update endpoint'})
                    return
                self._reply(200, result)
            except Exception as error:
                self._error(error)
            return
        if path.startswith('/local/setup/'):
            if not self._local():
                self._reply(403, {'error': 'Open the setup wizard on the computer'})
                return
            try:
                if path == '/local/setup/start':
                    result = self._hub().wireless.begin()
                elif path == '/local/setup/install':
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length < 1024:
                        raise ValueError('Invalid setup request')
                    data = json.loads(self.rfile.read(length))
                    result = self._hub().wireless.finish(data.get('device'))
                else:
                    self._reply(404, {'error': 'Unknown setup endpoint'})
                    return
                self._reply(200, result)
            except Exception as error:
                self._error(error)
            return
        if urlsplit(self.path).path == '/api/extension/connect':
            if not self._extension():
                self._reply(403, {'error': 'Pair from the local Synchro extension'})
                return
            self._reply(200, {'token': self.server.manager.token})
            return
        if not self._authorized():
            self._reply(401, {'error': 'Pairing key required'})
            return
        if path == '/api/clipboard/image':
            try:
                from .clipboard_images import MAX_IMAGE_BYTES
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_IMAGE_BYTES or self.headers.get('Content-Type', '').split(';')[0] != 'image/png':
                    raise ValueError('Use a PNG image up to 32 MB')
                payload = self.rfile.read(length)
                if len(payload) != length:
                    raise ValueError('Incomplete image upload')
                self._reply(200, self._hub().clipboard_image_write(payload))
            except (ConnectionError, TimeoutError):
                self.close_connection = True
            except Exception as error:
                self._error(error)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 65536:
                raise ValueError('JSON body must contain 1..65536 bytes')
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError('Incomplete request body')
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError('JSON body must be an object')
            path = urlsplit(self.path).path
            if path == '/api/takeover':
                result = self.server.manager.takeover(self._base_url())
            elif path == '/api/resume':
                result = self.server.manager.resume(data, self._base_url())
            elif path == '/api/progress':
                result = self.server.manager.progress(data)
            elif path == '/api/players/select':
                result = self.server.manager.select(data)
            elif path == '/api/youtube/takeover':
                result = self.server.manager.youtube_takeover(data)
            elif path == '/api/youtube/progress':
                result = self.server.manager.youtube_progress(data)
            elif path == '/api/clipboard':
                result = self._hub().clipboard_write(data)
            elif path == '/api/clipboard/mode':
                result = self._hub().mode(data)
            elif path == '/api/links':
                result = self._hub().send_link(data)
            elif path == '/api/links/ack':
                result = self._hub().acknowledge(data)
            elif path == '/api/setup/open':
                self._hub().opener(f'http://127.0.0.1:{self.server.server_port}/setup')
                result = {'ok': True}
            else:
                self._reply(404, {'error': 'Unknown API endpoint'})
                return
            self._reply(200, result)
        except (ConnectionError, TimeoutError):
            self.close_connection = True
            return
        except Exception as error:
            self._error(error)

    def _file(self, path, content_type=None):
        # Open before stat to keep Content-Length consistent if a path is replaced.
        import os
        with path.open('rb') as file:
            size = os.fstat(file.fileno()).st_size
            header = self.headers.get('Range')
            try:
                start, end = byte_range(header, size) if header else (0, size - 1)
            except RangeNotSatisfiable:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            self.send_response(206 if header else 200)
            self.send_header('Content-Type', content_type or mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
            self.send_header('Content-Length', str(max(0, end - start + 1)))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Cache-Control', 'private, no-store')
            if header:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.end_headers()
            if self.command == 'HEAD':
                return
            file.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = file.read(min(1024 * 1024, remaining))
                if not chunk:
                    self.close_connection = True
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
