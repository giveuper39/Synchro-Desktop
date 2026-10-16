"""The Windows file picker; invoked only by a click in the local desktop wizard."""
import ctypes as ct
from ctypes import wintypes as wt
from pathlib import Path
import threading

_dialog = threading.Lock()


def choose_media():
    class OpenFileName(ct.Structure):
        _fields_ = [('size', wt.DWORD), ('owner', wt.HWND), ('instance', wt.HINSTANCE),
                    ('filter', wt.LPCWSTR), ('customFilter', wt.LPWSTR), ('maxCustomFilter', wt.DWORD),
                    ('filterIndex', wt.DWORD), ('file', wt.LPWSTR), ('maxFile', wt.DWORD),
                    ('fileTitle', wt.LPWSTR), ('maxFileTitle', wt.DWORD), ('initialDir', wt.LPCWSTR),
                    ('title', wt.LPCWSTR), ('flags', wt.DWORD), ('fileOffset', wt.WORD),
                    ('extensionOffset', wt.WORD), ('defaultExtension', wt.LPCWSTR),
                    ('customData', wt.LPARAM), ('hook', ct.c_void_p), ('template', wt.LPCWSTR),
                    ('reserved', ct.c_void_p), ('reservedInt', wt.DWORD), ('flagsEx', wt.DWORD)]
    with _dialog:
        buffer = ct.create_unicode_buffer(65536)
        dialog = OpenFileName()
        dialog.size, dialog.file, dialog.maxFile = ct.sizeof(dialog), ct.cast(buffer, wt.LPWSTR), len(buffer)
        dialog.filter = 'Видео и аудио\0*.mkv;*.mp4;*.avi;*.mov;*.webm;*.m4v;*.mp3;*.flac;*.m4a\0Все файлы\0*.*\0\0'
        dialog.title = 'Synchro · Выберите видео'
        dialog.flags = 0x80000 | 0x1000 | 0x200 | 0x8  # Explorer, file exists, multiselect, no cwd change.
        api = ct.WinDLL('comdlg32', use_last_error=True)
        api.GetOpenFileNameW.argtypes, api.GetOpenFileNameW.restype = [ct.POINTER(OpenFileName)], wt.BOOL
        if not api.GetOpenFileNameW(ct.byref(dialog)):
            return []
        values = buffer[:].split('\0')
        values = values[:values.index('')]
        paths = [Path(values[0])] if len(values) == 1 else [Path(values[0]) / name for name in values[1:]]
        return [path.resolve() for path in paths if path.is_file()]
