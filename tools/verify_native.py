"""Exercise native handoff on the open paused file and restore it in finally.

Run deliberately with --exercise. This briefly changes position, tracks and
subtitle delay; it never changes volume. Results go to the ignored .data folder.
"""
import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from synchro.library import MediaLibrary
from synchro.player import WindowsPlayer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exercise', action='store_true', required=True)
    args = parser.parse_args()
    player = WindowsPlayer()
    original = player.read_basic()
    if not original.paused:
        player.close()
        raise SystemExit('Pause PotPlayer before running this test')
    before = player.capture()
    evidence = {'before': asdict(before)}
    library = MediaLibrary()
    tracks = library.probe(library.add(before.path))
    try:
        requested = replace(before, position_ms=min(before.position_ms + 10000, before.duration_ms - 3000),
                            audio_index=1 if len(tracks['audioTracks']) > 1 else before.audio_index,
                            subtitle_index=1 if len(tracks['subtitleTracks']) > 1 else before.subtitle_index,
                            subtitle_enabled=True if tracks['subtitleTracks'] else False,
                            subtitle_delay_ms=750)
        player.restore(requested)
        observed = player.capture()
        evidence['requested'], evidence['observed'] = asdict(requested), asdict(observed)
        if abs(observed.position_ms - requested.position_ms) > 1000:
            raise AssertionError('Position was not applied')
        if (observed.audio_index, observed.subtitle_index, observed.subtitle_enabled, observed.subtitle_delay_ms) != (
                requested.audio_index, requested.subtitle_index, requested.subtitle_enabled, requested.subtitle_delay_ms):
            raise AssertionError('Tracks or subtitle delay were not applied')
        evidence['exercisePassed'] = True
    finally:
        try:
            player.restore(before)
            restored = player.capture()
            evidence['restored'] = asdict(restored)
            evidence['restorePassed'] = (abs(restored.position_ms - before.position_ms) <= 1000
                                         and restored.audio_index == before.audio_index
                                         and restored.subtitle_index == before.subtitle_index
                                         and restored.subtitle_enabled == before.subtitle_enabled
                                         and restored.subtitle_delay_ms == before.subtitle_delay_ms)
        finally:
            player.close()
            target = ROOT / '.data' / 'native-validation.json'
            target.parent.mkdir(exist_ok=True)
            target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    if not evidence['restorePassed']:
        raise AssertionError('The original state was not confirmed after cleanup')
    print('Native seek, audio, subtitle selection and nonzero subtitle delay: PASS; original state restored')


if __name__ == '__main__':
    main()
