"""QR payload and private pairing page: no cloud generator or LAN secret exposure."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from synchro.pairing import pairing_uri, qr_svg, pairing_page, download_page


class PairingTests(unittest.TestCase):
    def test_uri_contains_address_key_version_and_unicode_computer_name(self):
        uri = pairing_uri('http://192.168.1.22:8765', 'test_key_which_is_not_a_real_secret', 'Мой компьютер')
        parsed = urlsplit(uri)
        self.assertEqual((parsed.scheme, parsed.netloc), ('synchro', 'pair'))
        fields = parse_qs(parsed.query)
        self.assertEqual(fields['v'], ['1'])
        self.assertEqual(fields['server'], ['http://192.168.1.22:8765'])
        self.assertEqual(fields['name'], ['Мой компьютер'])
        self.assertEqual(fields['token'], ['test_key_which_is_not_a_real_secret'])

    def test_qr_is_local_inline_vector_with_quiet_zone_and_no_external_assets(self):
        svg = qr_svg(pairing_uri('http://192.168.1.22:8765', 'demo_key_with_more_than_24_characters'))
        image = ET.fromstring(svg)
        self.assertEqual(image.tag, '{http://www.w3.org/2000/svg}svg')
        self.assertIn('viewBox', image.attrib)
        self.assertTrue(image.findall('{http://www.w3.org/2000/svg}path'))
        self.assertNotIn('<image', svg)
        self.assertNotIn('href=', svg)

    def test_page_has_separate_connect_and_install_qr_codes(self):
        with patch('synchro.pairing.qr_svg', return_value='<svg/>') as encoder:
            page = pairing_page('http://192.168.1.22:8765', 'test_key_which_is_not_a_real_secret')
        self.assertEqual(encoder.call_count, 2)
        self.assertTrue(encoder.call_args_list[0].args[0].startswith('synchro://pair?'))
        self.assertEqual(encoder.call_args_list[1].args[0], 'http://192.168.1.22:8765/synchro.apk')
        self.assertIn('<details>', page)

    def test_public_download_page_does_not_contain_pairing_credentials(self):
        self.assertNotIn('token', download_page())
        self.assertIn('/synchro.apk', download_page())


if __name__ == '__main__':
    unittest.main()
