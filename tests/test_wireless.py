"""Official Android QR format, exact service matching, and safe one-click installation."""
from pathlib import Path
import shlex
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from synchro.wireless import WirelessSetup, PAIRING, CONNECT
from synchro.pairing import pairing_uri


class WirelessTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.hub = SimpleNamespace(adb=SimpleNamespace(adb='adb', devices=lambda: [
            {'id': '192.168.1.22:40000', 'name': 'Android', 'emulator': False}]), mode=Mock())
        self.setup = WirelessSetup(directory.name, self.hub, 'http://192.168.1.2:8765',
                                   'test_key_not_used_for_any_real_device')
        self.addCleanup(self.setup.close)

    def await_state(self, stage):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            if self.setup.status()['stage'] == stage:
                return
            time.sleep(.025)
        self.fail(self.setup.status()['message'])

    def test_qr_pairing_discovers_only_the_service_from_that_qr(self):
        payloads = []
        with patch.object(self.setup, '_discover'), patch('synchro.wireless.qr_svg', side_effect=lambda value: payloads.append(value) or '<svg/>'):
            self.setup.begin()
        parts = dict(part.split(':', 1) for part in payloads[0].split(';') if ':' in part)
        self.assertEqual(parts['WIFI'], 'T:ADB')
        service = parts['S']
        self.assertTrue(service.startswith('synchro-'))
        run = Mock(return_value=SimpleNamespace(returncode=0, stdout='connected'))
        with patch.object(self.setup, '_run', run):
            self.setup.records['wrong.' + PAIRING] = {'kind': PAIRING, 'host': '192.168.1.22', 'port': 39000}
            time.sleep(.35)
            run.assert_not_called()
            self.setup.records[service + '.' + PAIRING] = {'kind': PAIRING, 'host': '192.168.1.22', 'port': 39000}
            self.setup.records['test.' + CONNECT] = {'kind': CONNECT, 'host': '192.168.1.22', 'port': 40000}
            self.await_state('paired')
        self.assertEqual(run.call_args_list[0].args, ('pair', '192.168.1.22:39000'))
        self.assertEqual(run.call_args_list[0].kwargs['input'], parts['P'] + '\n')

    def test_install_quotes_the_pairing_uri_as_one_android_shell_argument(self):
        def command(*args, **kwargs):
            if args[-1] == 'getprop ro.serialno':
                return SimpleNamespace(returncode=0, stdout='TEST_SERIAL')
            return SimpleNamespace(returncode=0, stdout='Success')
        with patch.object(self.setup, '_run', side_effect=command) as run:
            self.setup.finish('192.168.1.22:40000')
            self.await_state('done')
        shell = next(call.args[-1] for call in run.call_args_list if 'am start' in call.args[-1])
        self.assertEqual(shlex.split(shell)[-1], pairing_uri(self.setup.address, self.setup.token))
        self.hub.mode.assert_called_once_with({'enabled': True, 'device': '192.168.1.22:40000'})
        self.assertNotIn(self.setup.token, self.setup.settings.read_text())

    def test_unconnected_device_cannot_be_selected_for_installation(self):
        with self.assertRaises(ValueError):
            self.setup.finish('other-device')

    def test_old_qr_generation_cannot_change_new_wizard_state(self):
        self.setup.generation = 2
        self.setup._message('error', 'old error', generation=1)
        self.assertEqual(self.setup.status()['stage'], 'idle')
