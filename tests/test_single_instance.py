"""Prevent duplicate controllers before any player or watchdog is started."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from synchro.single_instance import ServiceInstance, claim_service


class SingleInstanceTests(unittest.TestCase):
    def test_existing_legacy_service_redirects_before_new_controller(self):
        instance = Mock(owned=True)
        with patch('synchro.single_instance.ServiceInstance', return_value=instance), \
                patch('synchro.single_instance.service_ready', side_effect=lambda port: port == 8765), \
                patch('synchro.single_instance.webbrowser.open') as browser:
            self.assertIsNone(claim_service(18777))
        instance.close.assert_called_once()
        browser.assert_called_once_with('http://127.0.0.1:8765/setup')

    def test_busy_mutex_uses_published_port(self):
        instance = Mock(owned=False)
        instance.existing_port.return_value = 18778
        with patch('synchro.single_instance.ServiceInstance', return_value=instance), \
                patch('synchro.single_instance.service_ready', return_value=False), \
                patch('synchro.single_instance.webbrowser.open') as browser:
            self.assertIsNone(claim_service(18777))
        instance.close.assert_called_once()
        browser.assert_called_once_with('http://127.0.0.1:18778/setup')

    @unittest.skipUnless(os.name == 'nt', 'Windows named mutex')
    def test_two_processes_cannot_own_one_controller(self):
        name = 'Local\\Synchro.Test.' + uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory) / 'service.json'
            script = '''import sys
from synchro.single_instance import ServiceInstance
instance = ServiceInstance(sys.argv[1], sys.argv[2])
instance.publish(18777)
print(instance.owned, flush=True)
sys.stdin.readline()
instance.close()
'''
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'server'))
            process = subprocess.Popen([sys.executable, '-c', script, name, str(record)],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                self.assertEqual(process.stdout.readline().strip(), 'True')
                contender = ServiceInstance(name, record)
                try:
                    self.assertFalse(contender.owned)
                    with self.assertRaises(RuntimeError):
                        contender.publish(18778)
                finally:
                    contender.close()
                self.assertEqual(json.loads(record.read_text(encoding='utf-8'))['port'], 18777)
            finally:
                process.communicate('\n', timeout=5)
            self.assertFalse(record.exists())
            replacement = ServiceInstance(name, record)
            try:
                self.assertTrue(replacement.owned)
            finally:
                replacement.close()
