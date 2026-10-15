"""Fail a tag build if the source version disagrees with the published tag."""
import re
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
tag = sys.argv[1]
if not re.fullmatch(r'v\d+\.\d+\.\d+', tag):
    raise SystemExit('Use a stable vMAJOR.MINOR.PATCH tag')
if (root / 'server').is_dir():
    sys.path.insert(0, str(root / 'server'))
    from potbridge import __version__
    actual = __version__
else:
    actual = re.search(r"versionName '([^']+)'", (root / 'android/app/build.gradle').read_text(encoding='utf-8')).group(1)
if tag != 'v' + actual:
    raise SystemExit('Bump the source version before creating its tag')
print('Source version matches ' + tag)
