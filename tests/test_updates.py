"""OTA accepts only stable releases and confines extraction to verified program files."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from potbridge.updates import release_info, unpack, version, Updates


class UpdateTests(unittest.TestCase):
    def release(self, **changes):
        asset = {'name': 'Syncro-Windows.zip', 'browser_download_url': 'https://github.com/owner/repo/releases/download/v0.4.0/Syncro-Windows.zip', 'size': 12, 'digest': 'sha256:' + 'a' * 64}
        asset.update(changes)
        return {'tag_name': 'v0.4.0', 'draft': False, 'prerelease': False, 'assets': [asset]}

    def info(self, data):
        return release_info('owner/repo', 'desktop', lambda *a, **k: io.BytesIO(json.dumps(data).encode()))

    def test_stable_semantic_version_not_lexical(self):
        self.assertGreater(version('v0.10.0'), version('0.9.9'))
        for value in ('latest', 'v1.0.0-rc1', '../v1.0.0'):
            with self.assertRaises(ValueError): version(value)

    def test_expected_asset_and_digest(self):
        self.assertEqual(self.info(self.release())['sha256'], 'a' * 64)
        for changes in ({'digest': None}, {'digest': ''}, {'browser_download_url': 'http://evil/update.zip'}, {'size': 0}, {'name': 'other.zip'}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError)):
                self.info(self.release(**changes))

    def test_preview_never_becomes_update(self):
        data = self.release(); data['prerelease'] = True
        with self.assertRaises(ValueError): self.info(data)

    def test_untrusted_archive_cannot_modify_user_data_or_escape(self):
        for name in ('Синхро/../../outside', 'Синхро/.data/pairing-key.txt', 'Синхро\\bad', 'C:/bad', '/absolute', 'other/file'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); archive = root / 'archive.zip'
                with zipfile.ZipFile(archive, 'w') as z: z.writestr(name, 'bad')
                with self.assertRaises(ValueError): unpack(archive, root / 'unpacked')

    def test_complete_distribution_extracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); archive = root / 'archive.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                for name in ('BUILD-MANIFEST.json', 'desktop-launcher.py', 'runtime/pythonw.exe', 'server/potbridge/__init__.py'):
                    z.writestr('Синхро/' + name, '{}')
            self.assertEqual(unpack(archive, root / 'unpacked'), root / 'unpacked/Синхро')
            with self.assertRaises(ValueError): Updates(root).install(lambda: None)
