"""Regression: nearby menu actions must not be counted as audio/subtitle tracks."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from potbridge.track_menus import extract_tracks


def item(command, checked=False, children=None):
    result = {'command': command, 'checked': checked}
    if children is not None:
        result['children'] = children
    return result


class TrackMenuTests(unittest.TestCase):
    def test_actions_do_not_shift_ordinals_or_shadow_audio_group(self):
        menu = [item(-1, children=[item(45249), item(29199), item(29198),
                                  item(29000), item(29001, True)]),
                item(-1, children=[item(45050), item(45300)]),
                item(-1, children=[item(45400, True), item(45401), item(45402)]),
                item(10126, True), item(10265, True)]
        parsed = extract_tracks(menu)
        self.assertEqual(parsed.audio_index, 0)
        self.assertEqual(parsed.audio_commands, (45400, 45401, 45402))
        self.assertEqual(parsed.subtitle_index, 1)
        self.assertEqual(parsed.subtitle_commands, (29000, 29001))
        self.assertTrue(parsed.subtitle_enabled)
        self.assertTrue(parsed.keyframe_seek)

    def test_missing_seek_preference_remains_unknown(self):
        self.assertIsNone(extract_tracks([]).keyframe_seek)
        self.assertEqual(extract_tracks([]).audio_index, -1)


if __name__ == '__main__':
    unittest.main()
