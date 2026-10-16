"""Image clipboard limits, actual bitmap formats, and latest-only ADB transport."""
import base64
from io import BytesIO, StringIO
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from PIL import Image, PngImagePlugin

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from synchro import clipboard_images as images
from synchro.adb_clipboard import AdbClipboard
from synchro.clipboard_windows import WindowsClipboard


def encoded_image(mode='RGBA', size=(7, 5), color=(20, 100, 200, 90), format='PNG'):
    output = BytesIO()
    with Image.new(mode, size, color) as image:
        image.save(output, format)
    return output.getvalue()


class ImageCodecTests(unittest.TestCase):
    def test_dib_v5_roundtrip_preserves_pixels_and_transparency(self):
        content = images.image_content(encoded_image())
        png, dib_v5, dib = images.windows_image_formats(content['data'])
        self.assertEqual(images.normalize_png(dib_v5, dib=True), png)
        with Image.open(BytesIO(dib)) as legacy:
            self.assertEqual(legacy.size, (7, 5))
            self.assertEqual(legacy.mode, 'RGB')

    def test_equivalent_png_encodings_have_one_canonical_digest(self):
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text('private-note', 'metadata must not be transmitted')
        output = BytesIO()
        with Image.new('RGB', (3, 4), 'red') as image:
            image.save(output, 'PNG', pnginfo=metadata, compress_level=0)
        first = images.image_content(output.getvalue())
        second = images.image_content(encoded_image('RGB', (3, 4), 'red'))
        self.assertEqual(images.content_digest(first), images.content_digest(second))
        with Image.open(BytesIO(first['data'])) as image:
            self.assertNotIn('private-note', image.info)

    def test_jpeg_exif_orientation_is_applied_before_metadata_is_removed(self):
        output = BytesIO()
        with Image.new('RGB', (3, 5), 'blue') as image:
            exif = Image.Exif()
            exif[274] = 6
            exif[315] = 'private camera owner'
            image.save(output, 'JPEG', exif=exif)
        with Image.open(BytesIO(images.normalize_png(output.getvalue()))) as normalized:
            self.assertEqual(normalized.size, (5, 3))
            self.assertNotIn('exif', normalized.info)

    def test_limits_and_non_image_payloads_are_rejected_before_full_decode(self):
        with patch.object(images, 'MAX_IMAGE_PIXELS', 4):
            with self.assertRaises(ValueError):
                images.normalize_png(encoded_image())
        with patch.object(images, 'MAX_IMAGE_BYTES', 8):
            with self.assertRaises(ValueError):
                images.normalize_png(encoded_image())
        for payload in (b'not an image', b'<svg><image href="https://example.com/private"/></svg>'):
            with self.assertRaises(ValueError):
                images.normalize_png(payload)

    def test_wire_event_accepts_real_image_bytes_and_preserves_original_text_protocol(self):
        png = images.normalize_png(encoded_image())
        event = {'type': 'image', 'mime': 'image/png', 'data': base64.b64encode(png).decode('ascii')}
        self.assertEqual(images.event_content(event), {'type': 'image', 'mime': 'image/png', 'data': png})
        self.assertEqual(images.event_content({'type': 'clipboard', 'text': 'последнее копирование'}),
                         {'type': 'text', 'text': 'последнее копирование'})
        for event in ({'type': 'image', 'mime': 'text/plain', 'data': 'YWJj'},
                      {'type': 'image', 'mime': 'image/png', 'data': '**bad**'},
                      {'type': 'clipboard', 'text': '\0'},
                      {'type': 'clipboard', 'text': '😀' * 8001}):
            with self.assertRaises(ValueError):
                images.event_content(event)


class WindowsImageFormatTests(unittest.TestCase):
    def setUp(self):
        self.clipboard = WindowsClipboard.__new__(WindowsClipboard)
        self.clipboard.lock = threading.Lock()
        self.clipboard.origin_format, self.clipboard.png_format = 500, 501
        self.clipboard.private_formats = [600, 601, 602]
        self.clipboard._write_values = Mock(return_value=101)

    def test_image_write_publishes_history_exclusions_first_and_all_bitmap_representations(self):
        png = images.normalize_png(encoded_image())
        sequence = self.clipboard.write_content({'type': 'image', 'mime': 'image/png', 'data': png}, 'origin')
        self.assertEqual(sequence, 101)
        values = self.clipboard._write_values.call_args.args[0]
        self.assertEqual(values[:3], [(600, b'\0' * 4), (601, b'\0' * 4), (602, b'\0' * 4)])
        self.assertEqual([format_id for format_id, _ in values], [600, 601, 602, 500, 501, 17, 8])
        self.assertEqual(images.normalize_png(dict(values)[17], dib=True), png)

    def test_real_image_format_takes_precedence_over_an_image_url_in_text(self):
        png = images.normalize_png(encoded_image())
        self.clipboard.user = Mock()
        self.clipboard._open = Mock()
        self.clipboard.sequence = Mock(return_value=12)
        self.clipboard._bytes = lambda format_id, maximum=0: {
            501: png, 500: b'origin\0',
            13: 'https://example.com/image.png\0'.encode('utf-16-le')}.get(format_id)
        content, sequence, origin = self.clipboard.read_content()
        self.assertEqual(content['type'], 'image')
        self.assertEqual((sequence, origin), (12, 'origin'))
        self.clipboard.user.CloseClipboard.assert_called_once()

    def test_legacy_text_write_remains_available(self):
        self.clipboard.write = Mock(return_value=45)
        self.assertEqual(self.clipboard.write_content({'type': 'text', 'text': 'new copy'}, 'origin'), 45)
        self.clipboard.write.assert_called_once_with('new copy', 'origin')


class AdbImageTransportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.adb = AdbClipboard(self.root)
        self.adb.adb = 'adb.exe'
        self.adb.serial, self.adb.origin = 'test-device', 'a' * 32
        self.adb.process = Mock()
        self.adb.process.poll.return_value = None
        self.adb.process.stdin = StringIO()

    def tearDown(self):
        self.directory.cleanup()

    def test_image_uses_only_one_temporary_slot_and_acknowledged_system_clipboard_broadcast(self):
        content = images.image_content(encoded_image())
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            if 'push' in command:
                local = Path(command[command.index('push') + 1])
                self.assertEqual(local.read_bytes(), content['data'])
            return Mock(returncode=0, stdout='Broadcast completed: result=-1\n')
        with patch('synchro.adb_clipboard.subprocess.run', side_effect=run):
            self.adb.send_content(content, 'b' * 32)
        self.assertEqual(len(calls), 3)
        self.assertIn('dev.potplayer.handoff/dev.synchro.app.ClipboardImageReceiver', calls[-1])
        self.assertEqual(calls[-1][-3:], ['--es', 'origin', 'b' * 32])
        self.assertFalse((self.root / '.data/clipboard/current.png').exists())
        self.assertEqual(self.adb.process.stdin.getvalue(), '')

    def test_receiver_rejection_is_reported_and_temporary_image_is_deleted(self):
        with patch('synchro.adb_clipboard.subprocess.run',
                   return_value=Mock(returncode=0, stdout='Broadcast completed: result=0\n')):
            with self.assertRaises(OSError):
                self.adb.send_content(images.image_content(encoded_image()))
        self.assertFalse((self.root / '.data/clipboard/current.png').exists())

    def test_text_still_uses_private_stdio_and_images_decode_only_from_image_events(self):
        self.adb.send_content({'type': 'text', 'text': 'latest text'})
        self.assertEqual(json.loads(self.adb.process.stdin.getvalue()), {'text': 'latest text'})
        self.adb.events.put({'type': 'clipboard', 'text': 'content://provider/image/1'})
        self.assertEqual(self.adb.read_latest_content(),
                         {'type': 'text', 'text': 'content://provider/image/1'})
        self.assertIsNone(self.adb.read_latest_content())


if __name__ == '__main__':
    unittest.main()
