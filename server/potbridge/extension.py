"""Only this project's Chrome extension may pair through its loopback endpoint."""
import base64
import hashlib
import json
from pathlib import Path


def extension_id():
    manifest = Path(__file__).resolve().parents[2] / 'chrome-extension/manifest.json'
    key = json.loads(manifest.read_text(encoding='utf-8-sig'))['key']
    digest = hashlib.sha256(base64.b64decode(key)).digest()[:16]
    return ''.join(chr(97 + part) for byte in digest for part in (byte >> 4, byte & 15))
