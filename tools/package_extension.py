"""Pack only extension runtime files, with manifest.json at the archive root."""
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parents[1]
(root / 'dist').mkdir(exist_ok=True)
with zipfile.ZipFile(root / 'dist/chrome-extension.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
    for path in (root / 'chrome-extension').rglob('*'):
        if path.is_file() and path.name != 'package.json' and not path.name.endswith('.test.mjs'):
            archive.write(path, path.relative_to(root / 'chrome-extension'))
