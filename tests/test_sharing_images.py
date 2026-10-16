"""Text and photos share one latest-only slot across the two clipboard adapters."""
from collections import deque
from io import BytesIO
from pathlib import Path
import sys
import json
import tempfile
import time
import threading
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from synchro.clipboard_images import image_content
from synchro.sharing import DeviceHub


def photo(color):
    with Image.new('RGBA', (6, 4), color) as image:
        buffer = BytesIO()
        image.save(buffer, 'PNG')
    return image_content(buffer.getvalue())


def text(value):
    return {'type': 'text', 'text': value}


class ImageClipboard:
    def __init__(self):
        self.content = photo((25, 50, 100, 128))
        self.number, self.origin = 1, None
        self.writes = 0
        self.strip_origin = False

    def sequence(self):
        return self.number

    def read_content(self):
        return self.content, self.number, self.origin

    def write_content(self, content, origin):
        self.content = dict(content)
        self.origin = None if self.strip_origin else origin
        self.number += 1
        self.writes += 1
        return self.number


class ImageAdb:
    def __init__(self):
        self.running = False
        self.incoming = deque(maxlen=1)
        self.outgoing = []

    def devices(self):
        return [{'id': 'phone', 'name': 'Test Android', 'emulator': False}]

    def start(self, serial, origin):
        self.incoming.clear()
        self.running = True

    def alive(self):
        return self.running

    def close(self):
        self.running = False
        self.incoming.clear()

    def read_latest_content(self):
        return self.incoming.pop() if self.incoming else None

    def send_content(self, content, origin=None):
        self.outgoing.append(dict(content))


class SharingImageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.clipboard, self.adb = ImageClipboard(), ImageAdb()
        self.hub = DeviceHub(self.root, self.clipboard, self.adb, lambda url: None)
        self.addCleanup(self.hub.close)

    def await_state(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.025)
        self.fail('Sharing state timed out')

    def connect(self):
        self.hub.mode({'enabled': True})
        self.await_state(lambda: self.hub.connected)

    def test_connection_does_not_replay_existing_images(self):
        existing = self.clipboard.content
        self.adb.incoming.append(photo('red'))
        self.connect()
        time.sleep(.2)
        self.assertEqual(self.clipboard.content, existing)
        self.assertEqual(self.clipboard.writes, 0)
        self.assertEqual(self.adb.outgoing, [])
        self.assertIsNone(self.hub.status()['clipboard']['lastType'])

    def test_newest_phone_photo_replaces_text_then_text_replaces_photo(self):
        self.connect()
        image = photo('green')
        self.adb.incoming.extend([text('superseded phone copy'), image])
        self.await_state(lambda: self.clipboard.content == image)
        self.assertEqual(self.hub.status()['clipboard']['lastType'], 'image')
        self.adb.incoming.append(text('new phone text'))
        self.await_state(lambda: self.clipboard.content == text('new phone text'))
        self.assertEqual(self.hub.status()['clipboard']['lastType'], 'text')
        self.assertEqual(self.hub.status()['clipboard']['lastSource'], 'phone')
        self.assertEqual(self.adb.outgoing, [])

    def test_pc_photo_and_text_use_the_same_latest_slot(self):
        self.connect()
        first, latest = photo('red'), photo('blue')
        self.clipboard.write_content(first, None)
        self.await_state(lambda: self.adb.outgoing == [first])
        self.clipboard.write_content(text('temporary text'), None)
        self.clipboard.write_content(latest, None)
        self.await_state(lambda: self.adb.outgoing == [first, latest])
        self.clipboard.write_content(text('latest PC text'), None)
        self.await_state(lambda: self.adb.outgoing[-1] == text('latest PC text'))
        self.assertEqual(len(self.adb.outgoing), 3)
        self.assertEqual(self.hub.status()['clipboard']['lastSource'], 'pc')
        self.assertEqual(self.hub.status()['clipboard']['lastType'], 'text')

    def test_photo_does_not_echo_when_windows_discards_origin_metadata(self):
        self.connect()
        self.clipboard.strip_origin = True
        original = self.clipboard.write_content

        def delayed_sequence(content, origin):
            return original(content, origin) - 1

        image = photo('yellow')
        with patch.object(self.clipboard, 'write_content', side_effect=delayed_sequence):
            self.adb.incoming.append(image)
            self.await_state(lambda: self.clipboard.content == image)
            time.sleep(.35)
        self.assertEqual(self.adb.outgoing, [])

    def test_android_reflection_of_pc_photo_does_not_rewrite_windows_clipboard(self):
        self.connect()
        image = photo('purple')
        self.clipboard.write_content(image, None)
        self.await_state(lambda: self.adb.outgoing == [image])
        before = self.clipboard.writes
        self.adb.incoming.append(image)
        time.sleep(.35)
        self.assertEqual(self.clipboard.writes, before)
        self.assertEqual(self.adb.outgoing, [image])

    def test_explicit_image_api_has_binary_payload_only_in_image_read(self):
        image = photo('cyan')
        self.assertEqual(self.hub.clipboard_image_write(image['data']), {'ok': True})
        self.assertEqual(self.hub.clipboard_image_read(), (image['data'], self.hub.origin))
        self.assertEqual(self.hub.clipboard_read(),
                         {'type': 'image', 'mime': 'image/png', 'origin': self.hub.origin})
        self.assertEqual(self.hub.status()['clipboard']['lastType'], 'image')
        self.assertNotIn('data', self.hub.clipboard_read())
        self.assertNotIn(image['data'], repr(self.hub.status()).encode('utf-8'))
        self.hub.mode({'enabled': False})
        self.assertEqual([path for path in self.root.rglob('*') if path.is_file()], [self.hub.settings])
        self.assertNotIn('data', self.hub.settings.read_text(encoding='utf-8'))

    def test_explicit_text_replaces_image_and_image_read_rejects_text(self):
        self.hub.clipboard_image_write(photo('orange')['data'])
        self.assertEqual(self.hub.clipboard_write({'text': 'replacement'}), {'ok': True})
        self.assertEqual(self.hub.clipboard_read(),
                         {'type': 'text', 'text': 'replacement', 'origin': self.hub.origin})
        self.assertEqual(self.hub.status()['clipboard']['lastType'], 'text')
        with self.assertRaises(ValueError):
            self.hub.clipboard_image_read()

    def test_invalid_image_does_not_replace_the_current_value(self):
        existing = self.clipboard.content
        with self.assertRaises(ValueError):
            self.hub.clipboard_image_write(b'not an image')
        self.assertEqual(self.clipboard.content, existing)
        self.assertEqual(self.clipboard.writes, 0)

    def test_explicit_phone_photo_does_not_echo_after_origin_is_stripped(self):
        self.connect()
        self.clipboard.strip_origin = True
        self.hub.clipboard_image_write(photo('white')['data'])
        time.sleep(.35)
        self.assertEqual(self.adb.outgoing, [])

    def test_retry_when_image_provider_is_not_yet_readable(self):
        self.connect()
        original = self.clipboard.read_content
        calls = [0]

        def deferred_read():
            calls[0] += 1
            if calls[0] == 1:
                return None, self.clipboard.number, None
            return original()

        image = photo('black')
        with patch.object(self.clipboard, 'read_content', side_effect=deferred_read):
            self.clipboard.write_content(image, None)
            self.await_state(lambda: self.adb.outgoing == [image])
        self.assertGreaterEqual(calls[0], 2)

    def test_reconnect_does_not_replay_photo_copied_while_disabled(self):
        self.connect()
        self.hub.mode({'enabled': False})
        self.await_state(lambda: not self.adb.running)
        image = photo('magenta')
        self.clipboard.write_content(image, None)
        self.adb.incoming.append(photo('lime'))
        self.connect()
        time.sleep(.2)
        self.assertEqual(self.clipboard.content, image)
        self.assertEqual(self.adb.outgoing, [])

    def test_reconnected_route_starts_without_waiting_for_a_different_adb_alias(self):
        self.hub.wireless = type('Wireless', (), {'reconnect': lambda _: '192.168.1.22:42000'})()
        self.hub.mode({'enabled': True, 'device': '192.168.1.22:41000'})
        self.await_state(lambda: self.hub.connected)
        self.assertEqual(self.hub.serial, '192.168.1.22:42000')
        image = photo('olive')
        self.adb.incoming.append(image)
        self.await_state(lambda: self.clipboard.content == image)
        self.assertEqual(self.hub.status()['clipboard']['lastSource'], 'phone')

    def test_disable_during_reconnect_is_never_overwritten(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def reconnect(_):
            started.set()
            release.wait(2)
            return '192.168.1.22:42000'
        self.hub.wireless = type('Wireless', (), {'reconnect': reconnect})()
        self.hub.mode({'enabled': True, 'device': 'old-route'})
        self.assertTrue(started.wait(2))
        self.hub.mode({'enabled': False})
        release.set()
        time.sleep(.3)
        self.assertFalse(self.hub.enabled)
        self.assertFalse(self.adb.running)
        self.assertFalse(json.loads(self.hub.settings.read_text(encoding='utf-8'))['enabled'])

    def test_selection_during_reconnect_is_not_replaced_by_its_old_result(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def reconnect(_):
            started.set()
            release.wait(2)
            return 'obsolete-route'
        self.hub.wireless = type('Wireless', (), {'reconnect': reconnect})()
        self.hub.mode({'enabled': True, 'device': 'old-route'})
        self.assertTrue(started.wait(2))
        self.hub.mode({'enabled': True, 'device': 'phone'})
        release.set()
        self.await_state(lambda: self.hub.connected)
        self.assertEqual(self.hub.serial, 'phone')


if __name__ == '__main__':
    unittest.main()
