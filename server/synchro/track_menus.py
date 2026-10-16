"""Interpret dynamic track menu entries independently of Win32 transport."""
from dataclasses import dataclass


@dataclass(frozen=True)
class TrackSettings:
    audio_index: int
    subtitle_index: int
    subtitle_enabled: bool
    audio_commands: tuple[int, ...]
    subtitle_commands: tuple[int, ...]
    keyframe_seek: bool | None


def walk(items):
    for item in items:
        yield item
        yield from walk(item.get('children', []))


def extract_tracks(items):
    audio, subtitles = [], []
    enabled, keyframe_seek = False, None
    for item in walk(items):
        if item['command'] == 10126:
            enabled = item['checked']
        if item['command'] == 10265:
            keyframe_seek = item['checked']
        children = item.get('children', [])
        # 45249 swaps subtitles; 29198/29199 disable/cycle rather than select.
        audio_group = [x for x in children if 45400 <= x['command'] < 45600]
        subtitle_group = [x for x in children if 29000 <= x['command'] < 29198]
        if audio_group and not audio:
            audio = audio_group
        if subtitle_group and not subtitles:
            subtitles = subtitle_group
    def selected(choices):
        return next((index for index, item in enumerate(choices) if item['checked']), -1)
    return TrackSettings(selected(audio), selected(subtitles), enabled,
                         tuple(x['command'] for x in audio),
                         tuple(x['command'] for x in subtitles), keyframe_seek)
