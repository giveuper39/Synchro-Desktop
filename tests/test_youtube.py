"""Ownership, capability isolation and URL boundaries for the YouTube adapter."""
from pathlib import Path
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from synchro.browser_page import bookmarklet, connection_page
from synchro.models import PlayerUnavailable, SessionConflict
from synchro.youtube import YouTubeCoordinator, video_id, watch_url


IDENTIFIER = 'aqz-KE-bpKQ'
SECOND_ID = 'YE7VzlLtp-4'


class Media:
    def __init__(self):
        self.state = dict(id='chrome.exe', appId='chrome.exe', title='Big Buck Bunny',
                          positionMs=12000, durationMs=120000, paused=False, canPause=True, canSeek=True)
        self.calls = []

    def list_sessions(self):
        return [dict(self.state)]

    def command(self, identity, action, **values):
        self.calls.append((identity, action, values))
        if action == 'pause':
            self.state['paused'] = True
        elif action == 'play':
            self.state['paused'] = False
        elif action == 'seek':
            self.state['positionMs'] = values['positionMs']
        return dict(self.state)


class Browser:
    available = True

    def capture(self, media):
        if not self.available:
            return None
        return dict(videoId=IDENTIFIER, browser='chrome', windowId='123', pid=100,
                    url=watch_url(IDENTIFIER))


class YouTubeTests(unittest.TestCase):
    def setUp(self):
        self.media, self.browser, self.clock = Media(), Browser(), [10.0]
        self.coordinator = YouTubeCoordinator(Path('.'), self.media, self.browser,
                                              clock=lambda: self.clock[0], command_timeout=1)
        self.addCleanup(self.coordinator.close)

    def capture(self):
        source = self.coordinator.catalog()[0]
        return self.coordinator.takeover(source['id'])

    def event(self, state, **values):
        return dict(sessionId=state['sessionId'], sequence=1, videoId=IDENTIFIER,
                    title='Big Buck Bunny', positionMs=50000, durationMs=120000,
                    paused=False, rate=1, **values)

    def bridge(self):
        state = dict(videoId=IDENTIFIER, title='Big Buck Bunny', positionMs=12000,
                     durationMs=120000, paused=False, rate=1)
        auth = self.coordinator.register_bridge(dict(nonce='a' * 48, origin='https://www.youtube.com', video=state))
        return auth, state

    def call_with_page_ack(self, callback, auth, state):
        result, errors = [], []
        def worker():
            try:
                result.append(callback())
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            deadline = time.monotonic() + 2
            while thread.is_alive() and time.monotonic() < deadline:
                reply = self.coordinator.bridge_poll(dict(auth, video=dict(state)))
                for command in reply['commands']:
                    if command['action'] == 'pause':
                        state['paused'] = True
                    elif command['action'] == 'seek':
                        state.update(paused=True, positionMs=command['positionMs'], rate=command['rate'])
                    elif command['action'] == 'open':
                        state.update(paused=True, videoId=command['videoId'], title=command['title'],
                                     positionMs=command['positionMs'], rate=command['rate'])
                    self.coordinator.bridge_poll(dict(auth, video=dict(state), ack=command['id']))
                time.sleep(.01)
        finally:
            thread.join(2)
        self.assertFalse(thread.is_alive(), 'The page command was not acknowledged')
        if errors:
            raise errors[0]
        return result[0]

    def test_urls_accept_only_public_youtube_video_identity(self):
        for address in (f'https://www.youtube.com/watch?v={IDENTIFIER}&list=PL123',
                        f'https://youtu.be/{IDENTIFIER}', f'https://m.youtube.com/shorts/{IDENTIFIER}',
                        f'www.youtube.com/live/{IDENTIFIER}'):
            self.assertEqual(video_id(address), IDENTIFIER)
        for address in (f'https://www.youtube.com.evil.test/watch?v={IDENTIFIER}',
                        f'http://youtube.com/watch?v={IDENTIFIER}',
                        f'https://user:pass@youtube.com/watch?v={IDENTIFIER}',
                        f'https://youtube.com:123/watch?v={IDENTIFIER}',
                        'https://youtube.com/watch?v=invalid', None):
            self.assertIsNone(video_id(address))

    def test_capture_pauses_only_selected_system_session(self):
        state = self.capture()
        self.assertEqual(state['videoId'], IDENTIFIER)
        self.assertEqual(state['kind'], 'youtube')
        self.assertTrue(self.media.state['paused'])
        self.assertEqual(self.media.calls, [('chrome.exe', 'pause', {})])
        self.assertNotIn('url', self.coordinator.catalog()[0]['media'])

    def test_missing_url_is_explicitly_not_ready_and_does_not_pause(self):
        self.browser.available = False
        source = self.coordinator.catalog()[0]
        self.assertFalse(source['ready'])
        with self.assertRaises(PlayerUnavailable):
            self.coordinator.takeover(source['id'])
        self.assertEqual(self.media.calls, [])

    def test_pause_restores_position_but_older_events_cannot_seek_back(self):
        state = self.capture()
        event = self.event(state)
        event.update(sequence=3, paused=True)
        self.coordinator.progress(event)
        self.assertEqual(self.media.state['positionMs'], 50000)
        before = list(self.media.calls)
        stale = dict(event, sequence=2, positionMs=100)
        self.assertTrue(self.coordinator.progress(stale)['ignored'])
        self.assertEqual(self.media.calls, before)

    def test_manual_pc_play_or_seek_revokes_delayed_phone_events(self):
        state = self.capture()
        self.media.state.update(paused=False, positionMs=18000)
        with self.assertRaises(SessionConflict):
            self.coordinator.progress(self.event(state))
        self.assertTrue(self.coordinator.describe()['revoked'])
        self.assertEqual(self.media.state['positionMs'], 18000)

    def test_phone_disconnect_checkpoints_then_revokes(self):
        state = self.capture()
        self.coordinator.progress(self.event(state))
        self.clock[0] += 16
        self.coordinator.watchdog()
        self.assertEqual(self.media.state['positionMs'], 50000)
        self.assertTrue(self.coordinator.describe()['revoked'])
        self.assertEqual(self.coordinator.describe()['owner'], 'pc')

    def test_page_capability_cannot_control_another_page(self):
        auth, state = self.bridge()
        with self.assertRaises(SessionConflict):
            self.coordinator.bridge_poll(dict(auth, bridgeKey='wrong-key', video=state))
        with self.assertRaises(ValueError):
            self.coordinator.register_bridge(dict(nonce='b' * 48, origin='https://evil.test', video=state))
        with self.assertRaises(ValueError):
            self.coordinator.register_bridge(dict(nonce='a' * 48, origin='https://www.youtube.com', video=state))

    def test_page_commands_require_acknowledgement_and_return_actual_position(self):
        auth, page = self.bridge()
        source = next(s for s in self.coordinator.catalog() if s['id'].startswith('youtube:page:'))
        state = self.call_with_page_ack(lambda: self.coordinator.takeover(source['id']), auth, page)
        self.assertTrue(page['paused'])
        event = self.event(state)
        event.update(paused=True, rate=1.5)
        self.call_with_page_ack(lambda: self.coordinator.progress(event), auth, page)
        self.assertEqual((page['positionMs'], page['rate']), (50000, 1.5))
        self.assertEqual(self.media.calls, [], 'The page connector must not control another native session')

    def test_unacknowledged_page_does_not_publish_phone_ownership(self):
        auth, state = self.bridge()
        self.coordinator.command_timeout = .03
        source = next(s for s in self.coordinator.catalog() if s['id'].startswith('youtube:page:'))
        with self.assertRaises(PlayerUnavailable):
            self.coordinator.takeover(source['id'])
        self.assertIsNone(self.coordinator.describe()['sessionId'])

    def test_capable_page_returns_next_video_without_opening_another_browser(self):
        auth, page = self.bridge()
        page['canChangeVideo'] = True
        self.coordinator.bridge_poll(dict(auth, video=page))
        source = next(s for s in self.coordinator.catalog() if s['id'].startswith('youtube:page:'))
        state = self.call_with_page_ack(lambda: self.coordinator.takeover(source['id']), auth, page)
        event = self.event(state)
        event.update(videoId=SECOND_ID, title='Next video', positionMs=60000)
        self.coordinator.progress(event)
        self.assertEqual(page['videoId'], IDENTIFIER, 'PC stays on its paused page while Android is playing')
        event.update(sequence=2, paused=True)
        self.call_with_page_ack(lambda: self.coordinator.progress(event), auth, page)
        self.assertEqual((page['videoId'], page['positionMs'], page['paused']), (SECOND_ID, 60000, True))
        self.assertEqual(self.media.calls, [])

    def test_page_reports_pc_change_that_happened_after_server_precheck(self):
        auth, page = self.bridge()
        source = next(s for s in self.coordinator.catalog() if s['id'].startswith('youtube:page:'))
        state = self.call_with_page_ack(lambda: self.coordinator.takeover(source['id']), auth, page)
        event = self.event(state)
        event['paused'] = True
        errors = []
        def apply():
            try:
                self.coordinator.progress(event)
            except BaseException as error:
                errors.append(error)
        worker = threading.Thread(target=apply)
        worker.start()
        deadline = time.monotonic() + 2
        command = None
        while worker.is_alive() and time.monotonic() < deadline:
            commands = self.coordinator.bridge_poll(dict(auth, video=page))['commands']
            if commands:
                command = commands[0]
                break
            time.sleep(.01)
        self.assertIsNotNone(command)
        page.update(paused=False, positionMs=18000)
        self.coordinator.bridge_poll(dict(auth, video=page, ack=command['id'], error='pc_changed'))
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], SessionConflict)
        self.assertTrue(self.coordinator.describe()['revoked'])
        self.assertEqual(self.coordinator.describe()['sequence'], 0)

    def test_invalid_state_does_not_advance_sequence(self):
        state = self.capture()
        for changes in (dict(rate=float('nan')), dict(positionMs=True), dict(paused='false'),
                        dict(positionMs=150000), dict(videoId=SECOND_ID)):
            event = self.event(state)
            event.update(changes)
            with self.assertRaises(ValueError):
                self.coordinator.progress(event)
        self.assertEqual(self.coordinator.describe()['sequence'], 0)

    def test_bookmarklet_has_only_loopback_endpoint_and_no_pairing_key(self):
        source = bookmarklet('http://127.0.0.1:8765')
        self.assertTrue(source.startswith('javascript:'))
        self.assertNotIn('\n', source)
        with self.assertRaises(ValueError):
            bookmarklet('http://remote-computer:8765')
        page = connection_page().decode('utf-8')
        self.assertIn('event.source!==opener', page)
        self.assertIn('d.nonce!==nonce', page)
        self.assertIn('Подключить эту страницу', page)
        self.assertNotIn('pairing-key', page)


if __name__ == '__main__':
    unittest.main()
