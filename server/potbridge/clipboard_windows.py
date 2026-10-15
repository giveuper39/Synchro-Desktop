"""One current Unicode clipboard value; received text is excluded from Windows history/cloud."""
import ctypes as ct
from ctypes import wintypes as wt
import threading
import time


class WindowsClipboard:
    def __init__(self):
        self.user = ct.WinDLL('user32', use_last_error=True)
        self.kernel = ct.WinDLL('kernel32', use_last_error=True)
        self.lock = threading.Lock()
        signatures = (
            (self.user, 'OpenClipboard', wt.BOOL, [wt.HWND]),
            (self.user, 'CloseClipboard', wt.BOOL, []),
            (self.user, 'EmptyClipboard', wt.BOOL, []),
            (self.user, 'GetClipboardData', wt.HANDLE, [wt.UINT]),
            (self.user, 'SetClipboardData', wt.HANDLE, [wt.UINT, wt.HANDLE]),
            (self.user, 'GetClipboardSequenceNumber', wt.DWORD, []),
            (self.user, 'RegisterClipboardFormatW', wt.UINT, [wt.LPCWSTR]),
            (self.user, 'CreateWindowExW', wt.HWND, [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
             ct.c_int, ct.c_int, ct.c_int, ct.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, ct.c_void_p]),
            (self.user, 'DestroyWindow', wt.BOOL, [wt.HWND]),
            (self.kernel, 'GlobalAlloc', wt.HGLOBAL, [wt.UINT, ct.c_size_t]),
            (self.kernel, 'GlobalLock', ct.c_void_p, [wt.HGLOBAL]),
            (self.kernel, 'GlobalUnlock', wt.BOOL, [wt.HGLOBAL]),
            (self.kernel, 'GlobalSize', ct.c_size_t, [wt.HGLOBAL]),
            (self.kernel, 'GlobalFree', wt.HGLOBAL, [wt.HGLOBAL]),
        )
        for library, name, result, args in signatures:
            function = getattr(library, name)
            function.restype, function.argtypes = result, args
        self.origin_format = self.user.RegisterClipboardFormatW('SyncroClipboardOrigin')
        self.private_formats = [self.user.RegisterClipboardFormatW(name) for name in (
            'ExcludeClipboardContentFromMonitorProcessing', 'CanIncludeInClipboardHistory',
            'CanUploadToCloudClipboard')]

    def sequence(self):
        return self.user.GetClipboardSequenceNumber()

    def _open(self, window=None):
        for pause in (0, .02, .05, .1, .2):
            if pause:
                time.sleep(pause)
            if self.user.OpenClipboard(window):
                return
        raise OSError('Clipboard is busy')

    def _bytes(self, format_id, maximum=131072):
        handle = self.user.GetClipboardData(format_id)
        if not handle:
            return None
        size = self.kernel.GlobalSize(handle)
        if not 0 < size <= maximum:
            return None
        pointer = self.kernel.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ct.string_at(pointer, size)
        finally:
            self.kernel.GlobalUnlock(handle)

    def read(self):
        with self.lock:
            self._open()
            try:
                data = self._bytes(13)  # CF_UNICODETEXT, no conversion of images/HTML/files.
                text = data.decode('utf-16-le').split('\0', 1)[0] if data else None
                marker = self._bytes(self.origin_format, 256)
                origin = marker.decode('ascii').rstrip('\0') if marker else None
                return text, self.sequence(), origin
            finally:
                self.user.CloseClipboard()

    def _allocate(self, data):
        handle = self.kernel.GlobalAlloc(0x42, len(data))  # GMEM_MOVEABLE | ZEROINIT
        if not handle:
            raise MemoryError('Clipboard allocation failed')
        pointer = self.kernel.GlobalLock(handle)
        if not pointer:
            self.kernel.GlobalFree(handle)
            raise MemoryError('Clipboard allocation failed')
        ct.memmove(pointer, data, len(data))
        self.kernel.GlobalUnlock(handle)
        return handle

    def write(self, text, origin):
        # Allocate everything before EmptyClipboard. Publish exclusion formats first.
        values = [(format_id, b'\0' * 4) for format_id in self.private_formats]
        values += [(self.origin_format, origin.encode('ascii') + b'\0'),
                   (13, (text + '\0').encode('utf-16-le'))]
        return self._write_values(values)

    def _write_values(self, values):
        blocks = []
        with self.lock:
            window = self.user.CreateWindowExW(0, 'STATIC', 'Syncro clipboard', 0,
                                               0, 0, 0, 0, wt.HWND(-3), None, None, None)
            if not window:
                raise OSError('Clipboard owner window is unavailable')
            opened = False
            try:
                for format_id, data in values:
                    blocks.append((format_id, self._allocate(data)))
                self._open(window)
                opened = True
                if not self.user.EmptyClipboard():
                    raise OSError('Clipboard could not be cleared')
                for index, (format_id, handle) in enumerate(blocks):
                    if not self.user.SetClipboardData(format_id, handle):
                        raise OSError('Clipboard write failed')
                    blocks[index] = (format_id, None)  # Windows now owns the allocation.
                return self.sequence()
            finally:
                if opened:
                    self.user.CloseClipboard()
                for _, handle in blocks:
                    if handle:
                        self.kernel.GlobalFree(handle)
                self.user.DestroyWindow(window)
