"""Track and subtitle-delay access for PotPlayer 1.7.22318.

These operations briefly open a native menu/dialog. They run only at handoff
or when a receiver applies changed track settings, never on the heartbeat.
"""
from . import win32 as win
from .models import PlayerUnavailable
from .track_menus import extract_tracks


def _menu_items(owner, menu, index=0, depth=0):
    win.send(owner, 0x117, menu, index)  # WM_INITMENUPOPUP updates checkmarks.
    count = win.user32.GetMenuItemCount(menu)
    if not 0 <= count <= 1000 or depth > 8:
        raise PlayerUnavailable('Unexpected PotPlayer menu structure')
    items = []
    for position in range(count):
        label = win.ct.create_unicode_buffer(2048)
        win.user32.GetMenuStringW(menu, position, label, len(label), 0x400)
        flags = win.user32.GetMenuState(menu, position, 0x400)
        command = win.user32.GetMenuItemID(menu, position)
        item = {'label': label.value, 'command': command, 'checked': bool(flags & 8)}
        submenu = win.user32.GetSubMenu(menu, position)
        text = label.value.replace('&', '').lower()
        if submenu and (depth or any(word in text for word in
                         ('audio', 'subtitle', 'playback', 'аудио', 'субтитр', 'воспроизведение'))):
            item['children'] = _menu_items(owner, submenu, position, depth + 1)
        items.append(item)
    return items


def read_tracks(owner):
    pid = win.process_id(owner)
    if win.windows_of_class({'#32768', '#32770'}, pid):
        raise PlayerUnavailable('Close the open PotPlayer menu/dialog and try again')
    win.post(owner, 0x7B, owner, 0x00010001)  # WM_CONTEXTMENU at (1, 1).
    try:
        popup = win.wait_for(lambda: win.windows_of_class({'#32768'}, pid))[0]
        info = win.MenuBarInfo()
        info.size = win.ct.sizeof(info)
        if not win.user32.GetMenuBarInfo(popup, -4, 0, win.ct.byref(info)) or not info.menu:
            raise PlayerUnavailable('Cannot inspect the PotPlayer context menu')
        items = _menu_items(owner, info.menu)
    finally:
        win.post(owner, 0x1F)  # WM_CANCELMODE, never WM_CLOSE on the main window.
        win.wait_for(lambda: not win.windows_of_class({'#32768'}, pid))
    return extract_tracks(items)


def apply_tracks(owner, audio_index, subtitle_index, subtitle_enabled):
    current = read_tracks(owner)
    choices = (
            ('audio', audio_index, current.audio_index, current.audio_commands),
            ('subtitle', subtitle_index, current.subtitle_index, current.subtitle_commands))
    for label, requested, selected, commands in choices:
        if requested < -1 or requested >= len(commands):
            raise PlayerUnavailable(f'The requested {label} track is unavailable in PotPlayer')
    def await_state(predicate):
        def confirmed():
            observed = read_tracks(owner)
            return observed if predicate(observed) else None
        return win.wait_for(confirmed, 6)
    # Stream changes are asynchronous. Wait for the audio splitter before
    # selecting subtitles; otherwise its completion can undo that selection.
    if audio_index >= 0 and current.audio_index != audio_index:
        win.send(owner, 0x111, current.audio_commands[audio_index])
        current = await_state(lambda value: value.audio_index == audio_index)
    if subtitle_index >= 0 and current.subtitle_index != subtitle_index:
        win.send(owner, 0x111, current.subtitle_commands[subtitle_index])
        current = await_state(lambda value: value.subtitle_index == subtitle_index)
    if subtitle_enabled != current.subtitle_enabled:
        win.send(owner, 0x111, 10126)
        current = await_state(lambda value: value.subtitle_enabled == subtitle_enabled)
    return current


def exact_seek(owner, position_ms, keyframe_seek):
    """Temporarily bypass fast keyframe jumps, then restore that preference."""
    if keyframe_seek is None:
        raise PlayerUnavailable('Cannot determine PotPlayer seek mode in this UI language')
    changed = False
    try:
        if keyframe_seek:
            win.send(owner, 0x111, 10265)
            changed = True
        win.send(owner, 0x400, 0x5005, position_ms)
        win.wait_for(lambda: abs(win.send(owner, 0x400, 0x5004) - position_ms) <= 250, 8)
        # The initial query can still show the previous frame while an asynchronous
        # seek is pending. Confirm again after the decoder has processed the request.
        import time
        time.sleep(.15)
        win.wait_for(lambda: abs(win.send(owner, 0x400, 0x5004) - position_ms) <= 250, 8)
    finally:
        if changed:
            win.send(owner, 0x111, 10265)


def subtitle_delay(owner, new_value=None):
    """Read/cancel, or apply an absolute signed millisecond value."""
    pid = win.process_id(owner)
    if win.windows_of_class({'#32770', '#32768'}, pid):
        raise PlayerUnavailable('Close the open PotPlayer menu/dialog and try again')
    dialog = None
    win.post(owner, 0x111, 10865)
    try:
        dialog = win.wait_for(lambda: win.windows_of_class({'#32770'}, pid))[0]
        edit = win.user32.GetDlgItem(dialog, 3069)
        if not edit or win.class_name(edit) != 'Edit':
            raise PlayerUnavailable('This PotPlayer subtitle dialog is unsupported')
        current = int(win.window_text(edit))
        if new_value is None or current == new_value:
            return current
        text = win.ct.create_unicode_buffer(str(new_value))
        win.send(edit, 0xC, 0, win.ct.addressof(text))  # WM_SETTEXT is system-marshalled.
        win.post(dialog, 0x111, 1)  # IDOK, after validating the exact edit control.
        win.wait_for(lambda: not win.user32.IsWindow(dialog))
        dialog = None
        return new_value
    finally:
        if dialog and win.user32.IsWindow(dialog):
            win.post(dialog, 0x111, 2)  # IDCANCEL also on a failed read.
            win.wait_for(lambda: not win.user32.IsWindow(dialog))
