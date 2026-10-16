"""Real HTTP image uploads/downloads against an isolated, in-memory clipboard."""
from io import BytesIO
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from synchro.clipboard_images import MAX_IMAGE_BYTES, image_content
from synchro.http_server import HandoffHTTPServer
from synchro.library import MediaLibrary
from synchro.sharing import DeviceHub


class MemoryClipboard:
    def __init__(self):
        self.content, self.number, self.origin = {'type': 'text', 'text': 'fixture text'}, 1, None

    def sequence(self):
        return self.number

    def read_content(self):
        return self.content, self.number, self.origin

    def write_content(self, content, origin):
        self.content, self.origin = dict(content), origin
        self.number += 1
        return self.number


class DisconnectedAdb:
    def alive(self):
        return False

    def close(self):
        pass


class ClipboardHTTPTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.clipboard = MemoryClipboard()
        self.hub = DeviceHub(self.root, self.clipboard, DisconnectedAdb(), lambda url: None)
        self.addCleanup(self.hub.close)
        self.token = 'test-only-image-http-token'
        manager = SimpleNamespace(library=MediaLibrary(ffprobe=False), token=self.token)
        self.server = HandoffHTTPServer(('127.0.0.1', 0), manager, hub=self.hub)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        with Image.new('RGBA', (8, 5), (20, 60, 140, 128)) as image:
            buffer = BytesIO()
            image.save(buffer, 'PNG')
        self.image = image_content(buffer.getvalue())['data']

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, method, path='/api/clipboard/image', payload=None, headers=None, authorized=True):
        request_headers = {'Authorization': 'Bearer ' + self.token} if authorized else {}
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        connection.request(method, path, body=payload, headers=request_headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_photo_upload_and_download_require_pairing_before_access(self):
        original = dict(self.clipboard.content)
        self.assertEqual(self.request('GET', authorized=False)[0], 401)
        self.assertEqual(self.request('POST', payload=self.image, headers={'Content-Type': 'image/png'},
                                      authorized=False)[0], 401)
        self.assertEqual(self.clipboard.content, original)
        self.hub.clipboard_image_write(self.image)
        status, _, body = self.request('GET', authorized=False)
        self.assertEqual(status, 401)
        self.assertNotIn(self.image, body)

    def test_binary_png_roundtrip_has_private_cache_headers_and_origin_marker(self):
        status, _, body = self.request('POST', payload=self.image, headers={'Content-Type': 'image/png'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'ok': True})
        status, headers, body = self.request('GET')
        self.assertEqual(status, 200)
        self.assertEqual(body, self.image)
        self.assertEqual(headers['Content-Type'], 'image/png')
        self.assertEqual(int(headers['Content-Length']), len(self.image))
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertRegex(headers['X-Synchro-Clipboard-Origin'], r'^[a-f0-9]{32}$')
        self.assertEqual(headers['X-Synchro-Clipboard-Origin'], self.hub.origin)
        status, _, description = self.request('GET', '/api/clipboard')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(description),
                         {'type': 'image', 'mime': 'image/png', 'origin': self.hub.origin})
        self.assertNotIn(self.image, description)

    def test_missing_photo_returns_json_bad_request_without_a_binary_marker(self):
        status, headers, body = self.request('GET')
        self.assertEqual(status, 400)
        self.assertTrue(headers['Content-Type'].startswith('application/json'))
        self.assertNotIn('X-Synchro-Clipboard-Origin', headers)
        self.assertIn('error', json.loads(body))

    def test_upload_rejects_unsupported_mime_and_invalid_png_without_replacing_clipboard(self):
        original = dict(self.clipboard.content)
        for mime in ('', 'text/plain', 'image/jpeg', 'application/octet-stream'):
            with self.subTest(mime=mime):
                status, _, _ = self.request('POST', payload=self.image, headers={'Content-Type': mime})
                self.assertEqual(status, 400)
                self.assertEqual(self.clipboard.content, original)
        status, _, body = self.request('POST', payload=b'not image bytes', headers={'Content-Type': 'image/png'})
        self.assertEqual(status, 400)
        self.assertIn('error', json.loads(body))
        self.assertEqual(self.clipboard.content, original)

    def test_upload_length_is_bounded_before_reading_an_oversized_body(self):
        original = dict(self.clipboard.content)
        for length in ('0', '-1', str(MAX_IMAGE_BYTES + 1), 'not-a-number'):
            with self.subTest(length=length):
                status, _, body = self.request('POST', payload=b'',
                    headers={'Content-Type': 'image/png', 'Content-Length': length})
                self.assertEqual(status, 400)
                self.assertIn('error', json.loads(body))
                self.assertEqual(self.clipboard.content, original)

    def test_image_mime_parameters_are_accepted_and_status_contains_only_its_type(self):
        status, _, _ = self.request('POST', payload=self.image,
                                    headers={'Content-Type': 'image/png; charset=binary'})
        self.assertEqual(status, 200)
        status, _, body = self.request('GET', '/api/sync')
        self.assertEqual(status, 200)
        clipboard = json.loads(body)['clipboard']
        self.assertEqual(clipboard['lastType'], 'image')
        self.assertEqual(clipboard['lastSource'], 'phone')
        self.assertNotIn('data', clipboard)
        self.assertEqual(list(self.root.rglob('*')), [])


if __name__ == '__main__':
    unittest.main()
