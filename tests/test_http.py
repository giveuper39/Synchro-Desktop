"""Exercise real HTTP responses, authorization and seekable file bytes."""
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import socket
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from synchro.http_server import HandoffHTTPServer, HandoffHandler
from synchro.library import MediaLibrary


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        path = Path(self.temporary.name) / 'video.mkv'
        self.content = bytes(range(256)) * 32
        path.write_bytes(self.content)
        library = MediaLibrary(ffprobe=False)
        self.item = library.add(path)
        self.token = 'a-test-secret-with-24-characters'
        self.manager = SimpleNamespace(library=library, token=self.token,
                                       describe=lambda _: {'owner': 'pc'})
        self.server = HandoffHTTPServer(('127.0.0.1', 0), self.manager)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_original_file_is_streamed_without_transcoding(self):
        status, headers, data = self.request('GET', f'/media/{self.token}/{self.item.id}')
        self.assertEqual(status, 200)
        self.assertEqual(data, self.content)
        self.assertEqual(headers['Accept-Ranges'], 'bytes')

    def test_seek_suffix_and_open_ended_ranges(self):
        for header, expected in [('bytes=5-35', self.content[5:36]),
                                 ('bytes=-12', self.content[-12:]),
                                 ('bytes=8000-', self.content[8000:])]:
            with self.subTest(header=header):
                status, headers, data = self.request('GET', f'/media/{self.token}/{self.item.id}', {'Range': header})
                self.assertEqual(status, 206)
                self.assertEqual(data, expected)
                self.assertEqual(int(headers['Content-Length']), len(expected))

    def test_bad_ranges_have_valid_416_response(self):
        for header in ('bytes=99999-', 'bytes=8-2', 'bytes=-0', 'bytes=0-1,4-5'):
            status, headers, body = self.request('GET', f'/media/{self.token}/{self.item.id}', {'Range': header})
            self.assertEqual(status, 416)
            self.assertEqual(headers['Content-Range'], f'bytes */{len(self.content)}')
            self.assertEqual(body, b'')

    def test_head_does_not_send_video_bytes(self):
        status, headers, data = self.request('HEAD', f'/media/{self.token}/{self.item.id}')
        self.assertEqual(status, 200)
        self.assertEqual(int(headers['Content-Length']), len(self.content))
        self.assertEqual(data, b'')

    def test_unauthorized_api_and_media_are_rejected(self):
        self.assertEqual(self.request('GET', '/api/session')[0], 401)
        self.assertEqual(self.request('GET', f'/media/wrong/{self.item.id}')[0], 401)
        self.assertEqual(self.request('GET', f'/media/{self.token}/../../secret')[0], 401)
        status, _, data = self.request('GET', '/api/session', {'Authorization': 'Bearer ' + self.token})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {'owner': 'pc'})

    def test_ungranted_file_ids_cannot_be_read(self):
        self.assertEqual(self.request('GET', f'/media/{self.token}/ungranted')[0], 404)

    def test_pairing_qr_is_not_disclosed_through_a_foreign_host_header(self):
        self.assertEqual(self.request('GET', '/pair', {'Host':'other-lan-device:8765'})[0], 404)
        status, _, page = self.request('GET', '/pair')
        self.assertEqual(status, 200)
        self.assertEqual(page.count(b'<svg'), 2)

    def _reset_connection(self, path, after_body=False):
        completed = threading.Event()
        class ObservedHandler(HandoffHandler):
            def finish(handler):
                try:
                    super().finish()
                finally:
                    completed.set()
        self.server.RequestHandlerClass = ObservedHandler
        self.server.handle_error = Mock()
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        connection.request('GET', path, headers={'Authorization': 'Bearer ' + self.token})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        if after_body:
            response.read()
        # Force RST rather than a graceful FIN, as Android does when replacing
        # a buffered media request. Exercise the real server request thread.
        connection.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        connection.close()
        response.close()
        self.assertTrue(completed.wait(3), 'Server did not release the disconnected client')
        self.server.handle_error.assert_not_called()
        self.assertEqual(self.request('GET', '/api/health', {'Authorization': 'Bearer '+self.token})[0], 200)

    def test_reset_idle_keepalive_does_not_print_a_traceback(self):
        self._reset_connection('/api/health', after_body=True)

    def test_abort_during_video_stream_does_not_send_a_second_error_response(self):
        self.item.path.write_bytes(self.content * 2048)
        self._reset_connection(f'/media/{self.token}/{self.item.id}')

    def test_new_clipboard_and_sync_routes_require_authentication(self):
        for path in ('/api/sync', '/api/clipboard', '/api/devices'):
            self.assertEqual(self.request('GET', path)[0], 401)

    def test_extension_pairing_requires_own_id_loopback_host_and_no_foreign_origin(self):
        from synchro.extension import extension_id
        own = extension_id()
        headers = {'X-Syncro-Extension': own}
        status, _, body = self.request('POST', '/api/extension/connect', headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['token'], self.token)
        for changed in ({}, {'X-Syncro-Extension': 'wrong'},
                        dict(headers, Origin='https://example.com'), dict(headers, Host='foreign.example')):
            with self.subTest(headers=changed):
                status, _, body = self.request('POST', '/api/extension/connect', changed)
                self.assertEqual(status, 403)
                self.assertNotIn(self.token.encode(), body)

    def test_only_own_extension_origin_gets_cors_preflight(self):
        from synchro.extension import extension_id
        origin = 'chrome-extension://' + extension_id()
        status, headers, _ = self.request('OPTIONS', '/api/links', {'Origin': origin})
        self.assertEqual(status, 204)
        self.assertEqual(headers['Access-Control-Allow-Origin'], origin)
        status, headers, _ = self.request('OPTIONS', '/api/links', {'Origin': 'https://example.com'})
        self.assertEqual(status, 403)
        self.assertNotIn('Access-Control-Allow-Origin', headers)

    def test_background_setup_is_not_startable_from_a_foreign_web_origin(self):
        for headers in ({'Origin': 'https://example.com'}, {'Host': 'foreign.example'}):
            status, _, _ = self.request('POST', '/local/setup/start', headers)
            self.assertEqual(status, 403)

    def test_qr_wizard_page_is_local_and_contains_no_pairing_key(self):
        status, _, body = self.request('GET', '/setup')
        self.assertEqual(status, 200)
        self.assertNotIn(self.token.encode(), body)
        self.assertIn('/local/setup/start', body.decode())
        self.assertEqual(self.request('GET', '/setup', {'Host': 'foreign.example'})[0], 404)

    def test_update_check_is_local_and_foreign_origin_is_rejected(self):
        self.server.updates = SimpleNamespace(check=Mock(return_value={'stage': 'checking'}))
        for headers in ({'Origin': 'https://example.com'}, {'Host': 'foreign.example'}):
            self.assertEqual(self.request('POST', '/local/updates/check', headers)[0], 403)
        self.server.updates.check.assert_not_called()
        self.assertEqual(self.request('POST', '/local/updates/check')[0], 200)
        self.server.updates.check.assert_called_once()

    def test_windows_update_cannot_interrupt_phone_viewing(self):
        self.server.updates = SimpleNamespace(install=Mock(return_value={'stage': 'downloading'}))
        self.manager.describe = lambda _: {'owner': 'android'}
        self.assertEqual(self.request('POST', '/local/updates/install')[0], 409)
        self.server.updates.install.assert_not_called()
        self.manager.describe = lambda _: {'owner': 'pc'}
        self.assertEqual(self.request('POST', '/local/updates/install')[0], 200)
        self.server.updates.install.assert_called_once()


if __name__ == '__main__':
    unittest.main()
