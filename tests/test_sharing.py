"""Latest-only clipboard, echo suppression and explicit URL transfer."""
from collections import deque
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from potbridge.sharing import DeviceHub, plain_text, web_url


class FakeClipboard:
    def __init__(self):
        self.text, self.number, self.origin = 'old desktop clipboard', 1, None
    def sequence(self):
        return self.number
    def read(self):
        return self.text, self.number, self.origin
    def write(self, text, origin):
        self.text, self.origin = text, origin
        self.number += 1
        return self.number


class FakeAdb:
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
    def read_latest(self):
        return self.incoming.pop() if self.incoming else None
    def write(self, text):
        self.outgoing.append(text)


class SharingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.clipboard, self.adb = FakeClipboard(), FakeAdb()
        self.opened = []
        self.hub = DeviceHub(Path(directory.name), self.clipboard, self.adb, self.opened.append)
        self.addCleanup(self.hub.close)

    def await_state(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.025)
        self.fail('Sharing state timed out')

    def test_connect_does_not_replay_old_clipboard_and_new_copies_replace_it(self):
        self.hub.mode({'enabled': True})
        self.await_state(lambda: self.hub.connected)
        self.assertEqual(self.adb.outgoing, [])
        self.assertEqual(self.clipboard.text, 'old desktop clipboard')
        self.adb.incoming.extend(['first phone copy', 'latest phone copy'])
        self.await_state(lambda: self.clipboard.text == 'latest phone copy')
        self.assertEqual(self.adb.outgoing, [])  # No reflection back to Android.
        self.assertNotIn('latest phone copy', str(self.hub.status()))

    def test_pc_copy_is_forwarded_and_disable_stops_forwarding(self):
        self.hub.mode({'enabled': True})
        self.await_state(lambda: self.hub.connected)
        self.clipboard.write('new desktop copy', None)
        self.await_state(lambda: self.adb.outgoing == ['new desktop copy'])
        self.hub.mode({'enabled': False})
        self.await_state(lambda: not self.adb.running)
        self.clipboard.write('copy while disabled', None)
        time.sleep(.2)
        self.assertEqual(self.adb.outgoing, ['new desktop copy'])

    def test_text_is_not_written_to_settings_or_status(self):
        self.hub.clipboard_write({'text': 'private sample text'})
        self.hub.mode({'enabled': False})
        self.assertNotIn('private sample text', self.hub.settings.read_text())
        self.assertNotIn('private sample text', str(self.hub.status()))

    def test_retry_when_provider_advertises_text_before_it_can_be_read(self):
        self.hub.mode({'enabled': True})
        self.await_state(lambda: self.hub.connected)
        original = self.clipboard.read
        calls = [0]
        def delayed_read():
            calls[0] += 1
            return (None, self.clipboard.number, None) if calls[0] == 1 else original()
        with patch.object(self.clipboard, 'read', side_effect=delayed_read):
            self.clipboard.write('deferred text', None)
            self.await_state(lambda: self.adb.outgoing == ['deferred text'])

    def test_no_echo_if_a_clipboard_provider_strips_custom_origin_format(self):
        self.hub.mode({'enabled': True})
        self.await_state(lambda: self.hub.connected)
        original = self.clipboard.write
        def lossy_write(text, origin):
            return original(text, None) - 1
        with patch.object(self.clipboard, 'write', side_effect=lossy_write):
            self.adb.incoming.append('phone value')
            self.await_state(lambda: self.clipboard.text == 'phone value')
            time.sleep(.35)
            self.assertEqual(self.adb.outgoing, [])

    def test_links_to_pc_are_explicit_and_retries_do_not_open_twice(self):
        request = {'url': 'https://example.com/page', 'target': 'pc', 'title': 'Example', 'requestId': 'one'}
        self.assertEqual(self.hub.send_link(request), self.hub.send_link(request))
        self.assertEqual(self.opened, ['https://example.com/page'])

    def test_phone_inbox_is_bounded_deduplicated_and_consumed(self):
        for index in range(25):
            self.hub.send_link({'url': f'https://example.com/{index}', 'target': 'phone', 'requestId': str(index)})
        self.assertEqual(len(self.hub.status()['links']), 20)
        latest = self.hub.send_link({'url': 'https://example.com/24', 'target': 'phone', 'requestId': 'again'})
        self.assertEqual(len(self.hub.status()['links']), 20)
        self.hub.acknowledge({'id': latest['id']})
        self.assertEqual(len(self.hub.status()['links']), 19)

    def test_rejects_executable_urls_credentials_and_clipboard_overflow(self):
        for url in ('javascript:alert(1)', 'file:///private', 'https://u:p@example.com/', 'https://example.com/\r\nx', 'https://example.com:99999'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                web_url(url)
        for text in ('x\0y', '🙂' * 8001, None):
            with self.subTest(), self.assertRaises(ValueError):
                plain_text(text)


if __name__ == '__main__':
    unittest.main()
