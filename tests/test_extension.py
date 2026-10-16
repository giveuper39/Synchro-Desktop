"""Extension discovery, document lifetimes, command acks and stable install files."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from synchro.extension_setup import ExtensionSetup
from synchro.models import PlayerUnavailable
from synchro.youtube import YouTubeCoordinator


CLIENT = '12345678-1234-1234-1234-123456789abc'


def snapshot():
    return dict(clientId=CLIENT, version='0.4.4', waitMs=0, pages=[dict(
        tabId=7, documentId='document-one', origin='https://www.youtube.com', acks=[],
        video=dict(videoId='abcdefghijk', title='Own fixture', positionMs=12000,
                   durationMs=180000, paused=False, rate=1, canChangeVideo=False))])


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.clock = [10.0]
        self.coordinator = YouTubeCoordinator(Path('.'), SimpleNamespace(list_sessions=lambda: []),
                                               clock=lambda: self.clock[0], command_timeout=.3)

    def test_snapshot_discovers_all_pages_and_keeps_identity_during_spa_navigation(self):
        data = snapshot()
        self.coordinator.extension_sync(data)
        first = self.coordinator.catalog()[0]['id']
        data['pages'][0]['video']['videoId'] = 'lmnopqrstuv'
        self.coordinator.extension_sync(data)
        self.assertEqual(self.coordinator.catalog()[0]['id'], first)
        self.assertEqual(self.coordinator.catalog()[0]['media']['videoId'], 'lmnopqrstuv')
        other = deepcopy(data); other['clientId'] = 'a' * 36
        self.coordinator.extension_sync(other)
        self.assertEqual(len(self.coordinator.catalog()), 2, 'Profiles cannot share a page binding')
        data['pages'] = []; self.coordinator.extension_sync(data)
        self.assertEqual(len(self.coordinator.catalog()), 1)

    def test_invalid_batch_cannot_remove_or_change_connected_pages(self):
        data = snapshot(); self.coordinator.extension_sync(data)
        before = self.coordinator.catalog()
        for change in ({'origin': 'https://evil.test'}, {'tabId': True}, {'documentId': '../replacement'},
                       {'acks': [{'id': 'a' * 32, 'error': 0}]}):
            bad = deepcopy(data); bad['pages'].append(dict(bad['pages'][0], **change))
            with self.assertRaises(ValueError):
                self.coordinator.extension_sync(bad)
            self.assertEqual(self.coordinator.catalog(), before)

    def _takeover_thread(self, identity):
        result, errors = [], []
        def run():
            try:
                result.append(self.coordinator.takeover(identity))
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=run); thread.start()
        return thread, result, errors

    def _commands(self, data):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            commands = self.coordinator.extension_sync(data)['pages'][0]['commands']
            if commands:
                return commands
            time.sleep(.005)
        self.fail('No command queued')

    def test_takeover_and_return_use_acknowledged_existing_page(self):
        data = snapshot(); self.coordinator.extension_sync(data)
        identity = self.coordinator.catalog()[0]['id']
        thread, result, errors = self._takeover_thread(identity)
        command = self._commands(data)[0]
        self.assertEqual(command['action'], 'pause')
        data['pages'][0]['video']['paused'] = True
        data['pages'][0]['acks'] = [dict(id=command['id'], error=False)]
        self.coordinator.extension_sync(data); thread.join(1)
        self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
        self.assertEqual(result[0]['owner'], 'android')
        event = dict(sessionId=result[0]['sessionId'], sequence=1, videoId='abcdefghijk', title='Own fixture',
                     positionMs=50000, durationMs=180000, paused=True, rate=1.25)
        completed, failures = [], []
        def restore():
            try: completed.append(self.coordinator.progress(event))
            except BaseException as error: failures.append(error)
        thread = threading.Thread(target=restore); thread.start()
        command = self._commands(data)[0]
        self.assertEqual((command['action'], command['positionMs']), ('seek', 50000))
        data['pages'][0]['video'].update(positionMs=50000, rate=1.25)
        data['pages'][0]['acks'] = [dict(id=command['id'], error=False)]
        self.coordinator.extension_sync(data); thread.join(1)
        self.assertEqual(failures, []); self.assertTrue(completed)
        self.assertEqual(self.coordinator.describe()['owner'], 'pc')

    def test_document_replacement_cancels_old_commands_instead_of_controlling_new_page(self):
        data = snapshot(); self.coordinator.extension_sync(data)
        identity = self.coordinator.catalog()[0]['id']
        thread, result, errors = self._takeover_thread(identity)
        self._commands(data)
        data['pages'][0]['documentId'] = 'new-document'
        reply = self.coordinator.extension_sync(data)
        thread.join(1)
        self.assertEqual(reply['pages'][0]['commands'], [])
        self.assertFalse(result); self.assertIsInstance(errors[0], PlayerUnavailable)
        self.assertNotEqual(self.coordinator.catalog()[0]['id'], identity)


class ExtensionInstallTests(unittest.TestCase):
    def test_preparation_is_stable_preserves_unknown_files_and_prompts_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / 'app/chrome-extension'; source.mkdir(parents=True)
            (source / 'manifest.json').write_text(json.dumps({'version': '0.4.4'}))
            (source / 'background.js').write_text('first')
            (source / 'private.test.mjs').write_text('not runtime')
            destination = root / 'local/Synchro/browser-extension'
            setup = ExtensionSetup(source.parent, destination, clock=lambda: 10)
            setup.prepare(); self.assertTrue(setup.first_start()); setup.mark_shown()
            self.assertFalse(setup.first_start())
            self.assertFalse((destination / 'private.test.mjs').exists())
            (destination / 'user-note.txt').write_text('keep')
            (source / 'background.js').write_text('second'); setup.prepare()
            self.assertEqual((destination / 'background.js').read_text(), 'second')
            self.assertEqual((destination / 'user-note.txt').read_text(), 'keep')
            self.assertFalse(setup.status()['connected'])
            setup.heartbeat(CLIENT, '0.4.4', 2)
            self.assertTrue(setup.status()['connected']); self.assertEqual(setup.status()['youtubeTabs'], 2)


if __name__ == '__main__':
    unittest.main()
