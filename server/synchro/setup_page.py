"""Local desktop interface with guarded pairing APIs."""
from pathlib import Path


def setup_page():
    return (Path(__file__).with_name('ui') / 'setup.html').read_text(encoding='utf-8')
