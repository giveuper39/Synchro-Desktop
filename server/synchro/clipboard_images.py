"""Bounded image clipboard conversion using Pillow, with no image history.

Wire content is canonical PNG. Windows consumers receive PNG, CF_DIBV5 with
alpha and a legacy CF_DIB fallback. Only in-memory decoded pixels are used;
URI text, files copied in Explorer and network image URLs are never fetched.
"""
from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import struct
import warnings

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 64_000_000
MAX_DIB_BYTES = MAX_IMAGE_PIXELS * 4 + 1024 * 1024
MAX_EVENT_CHARACTERS = ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 4096
_IMAGE_FORMATS = ('PNG', 'JPEG', 'WEBP', 'GIF', 'BMP', 'DIB', 'TIFF', 'AVIF')


def _rgba(payload: bytes, maximum=MAX_IMAGE_BYTES) -> Image.Image:
    if not isinstance(payload, bytes) or not 0 < len(payload) <= maximum:
        raise ValueError('Clipboard image exceeds the 32 MB transfer limit')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload), formats=_IMAGE_FORMATS) as source:
                width, height = source.size
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise ValueError('Clipboard image exceeds the 64 megapixel limit')
                # Animated images use their first frame, matching a still-image
                # clipboard. EXIF orientation is applied before metadata removal.
                source.seek(0)
                image = ImageOps.exif_transpose(source).convert('RGBA')
                profile = source.info.get('icc_profile')
                if isinstance(profile, bytes) and len(profile) <= 1024 * 1024:
                    try:
                        converted = ImageCms.profileToProfile(
                            image, ImageCms.ImageCmsProfile(BytesIO(profile)),
                            ImageCms.createProfile('sRGB'), outputMode='RGBA')
                    except (OSError, ValueError, ImageCms.PyCMSError):
                        pass  # Invalid optional profiles do not hide a valid image.
                    else:
                        image.close()
                        image = converted
                image.info.clear()
                return image
    except (OSError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise ValueError('Clipboard does not contain a supported image') from None


def normalize_png(payload: bytes, *, dib=False) -> bytes:
    """Validate dimensions, decode once, strip metadata and encode lossless PNG."""
    image = _rgba(payload, MAX_DIB_BYTES if dib else MAX_IMAGE_BYTES)
    try:
        output = BytesIO()
        image.save(output, 'PNG', compress_level=6)
        normalized = output.getvalue()
        if len(normalized) > MAX_IMAGE_BYTES:
            raise ValueError('Clipboard image exceeds the 32 MB transfer limit')
        return normalized
    finally:
        image.close()


def image_content(payload: bytes, *, dib=False) -> dict:
    return {'type': 'image', 'mime': 'image/png', 'data': normalize_png(payload, dib=dib)}


def windows_image_formats(png: bytes) -> tuple[bytes, bytes, bytes]:
    """Produce PNG, alpha-preserving DIBV5 and compatible 24-bit DIB bytes."""
    canonical = normalize_png(png)
    image = _rgba(canonical)
    try:
        width, height = image.size
        # This is the documented Windows container header, not an image codec.
        # Pillow provides the BGRA pixel encoding and BMP compatibility encoding.
        header = bytearray(124)
        struct.pack_into('<IiiHHIIiiII', header, 0,
                         124, width, -height, 1, 32, 3, width * height * 4, 0, 0, 0, 0)
        struct.pack_into('<IIIII', header, 40,
                         0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000, 0x73524742)
        struct.pack_into('<I', header, 108, 4)  # LCS_GM_IMAGES.
        dib_v5 = bytes(header) + image.tobytes('raw', 'BGRA')
        background = Image.new('RGB', image.size, 'white')
        background.paste(image, mask=image.getchannel('A'))
        legacy = BytesIO()
        background.save(legacy, 'BMP')
        background.close()
        return canonical, dib_v5, legacy.getvalue()[14:]
    finally:
        image.close()


def content_digest(content: dict) -> bytes:
    """Type-separated digest for the latest value; callers keep no text history."""
    if content.get('type') == 'text' and isinstance(content.get('text'), str):
        return hashlib.sha256(b'text\0' + content['text'].encode('utf-8')).digest()
    if content.get('type') == 'image' and content.get('mime') == 'image/png':
        return hashlib.sha256(b'image/png\0' + content['data']).digest()
    raise ValueError('Unsupported clipboard content')


def event_content(event: dict) -> dict | None:
    """Decode the shell bridge's bounded event into the common in-memory value."""
    kind = event.get('type')
    if kind == 'clipboard':  # Original text protocol remains compatible.
        text = event.get('text')
        if not isinstance(text, str) or '\0' in text or len(text.encode('utf-16-le')) > 32000:
            raise ValueError('Invalid clipboard text')
        return {'type': 'text', 'text': text}
    if kind == 'image':
        encoded = event.get('data')
        mime = event.get('mime')
        if (not isinstance(mime, str) or not mime.startswith('image/')
                or not isinstance(encoded, str) or len(encoded) > MAX_EVENT_CHARACTERS - 4096):
            raise ValueError('Invalid clipboard image event')
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (ValueError, UnicodeError):
            raise ValueError('Invalid clipboard image encoding') from None
        return image_content(payload)
    return None
