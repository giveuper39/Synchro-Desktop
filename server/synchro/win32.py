"""Small, pointer-safe Win32 interface. No process injection or memory writes.

WM_USER protocol: PotPlayerControl/InternalSimpleCmd.h (public reference).
Menu checkmarks are used for tracks because this release has no track getter.
"""
import ctypes as ct
from ctypes import wintypes as wt
import os
import time

from .models import PlayerUnavailable

if os.name != 'nt':
    raise RuntimeError('The PotPlayer adapter requires Windows')

user32 = ct.WinDLL('user32', use_last_error=True)
kernel32 = ct.WinDLL('kernel32', use_last_error=True)
LRESULT = ct.c_ssize_t
WNDPROC = ct.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
ENUMPROC = ct.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


class WindowClass(ct.Structure):
    _fields_ = [('style', wt.UINT), ('procedure', WNDPROC),
                ('class_extra', ct.c_int), ('window_extra', ct.c_int),
                ('instance', wt.HINSTANCE), ('icon', wt.HICON),
                ('cursor', wt.HANDLE), ('background', wt.HBRUSH),
                ('menu_name', wt.LPCWSTR), ('class_name', wt.LPCWSTR)]


class CopyData(ct.Structure):
    _fields_ = [('kind', ct.c_size_t), ('size', wt.DWORD), ('data', ct.c_void_p)]


class MenuBarInfo(ct.Structure):
    _fields_ = [('size', wt.DWORD), ('rect', wt.RECT), ('menu', wt.HMENU),
                ('window', wt.HWND), ('flags', wt.DWORD)]


def _signature(name, result, *arguments):
    function = getattr(user32, name)
    function.restype = result
    function.argtypes = list(arguments)


_signature('DefWindowProcW', LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
_signature('RegisterClassW', wt.ATOM, ct.POINTER(WindowClass))
_signature('UnregisterClassW', wt.BOOL, wt.LPCWSTR, wt.HINSTANCE)
_signature('CreateWindowExW', wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR,
           wt.DWORD, ct.c_int, ct.c_int, ct.c_int, ct.c_int,
           wt.HWND, wt.HMENU, wt.HINSTANCE, ct.c_void_p)
_signature('DestroyWindow', wt.BOOL, wt.HWND)
_signature('EnumWindows', wt.BOOL, ENUMPROC, wt.LPARAM)
_signature('EnumChildWindows', wt.BOOL, wt.HWND, ENUMPROC, wt.LPARAM)
_signature('GetClassNameW', ct.c_int, wt.HWND, wt.LPWSTR, ct.c_int)
_signature('GetWindowTextW', ct.c_int, wt.HWND, wt.LPWSTR, ct.c_int)
_signature('GetWindowThreadProcessId', wt.DWORD, wt.HWND, ct.POINTER(wt.DWORD))
_signature('SendMessageTimeoutW', LRESULT, wt.HWND, wt.UINT, wt.WPARAM,
           wt.LPARAM, wt.UINT, wt.UINT, ct.POINTER(ct.c_size_t))
_signature('PostMessageW', wt.BOOL, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
_signature('GetMenuBarInfo', wt.BOOL, wt.HWND, ct.c_long, ct.c_long, ct.POINTER(MenuBarInfo))
_signature('GetMenuItemCount', ct.c_int, wt.HMENU)
_signature('GetMenuStringW', ct.c_int, wt.HMENU, wt.UINT, wt.LPWSTR, ct.c_int, wt.UINT)
_signature('GetMenuState', wt.UINT, wt.HMENU, wt.UINT, wt.UINT)
_signature('GetMenuItemID', wt.UINT, wt.HMENU, ct.c_int)
_signature('GetSubMenu', wt.HMENU, wt.HMENU, ct.c_int)
_signature('GetDlgItem', wt.HWND, wt.HWND, ct.c_int)
_signature('IsWindow', wt.BOOL, wt.HWND)
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE


def send(window, message, command=0, value=0, timeout_ms=1500):
    result = ct.c_size_t()
    if not user32.SendMessageTimeoutW(window, message, command, value, 2,
                                     timeout_ms, ct.byref(result)):
        raise PlayerUnavailable(f'PotPlayer did not answer message {message:#x}/{command:#x}')
    return result.value


def post(window, message, command=0, value=0):
    if not user32.PostMessageW(window, message, command, value):
        raise PlayerUnavailable('Cannot send a command to PotPlayer')


def class_name(window):
    buffer = ct.create_unicode_buffer(256)
    user32.GetClassNameW(window, buffer, len(buffer))
    return buffer.value


def process_id(window):
    result = wt.DWORD()
    user32.GetWindowThreadProcessId(window, ct.byref(result))
    return result.value


def windows_of_class(names, pid=None):
    windows = []
    @ENUMPROC
    def callback(window, _):
        if class_name(window) in names and (pid is None or process_id(window) == pid):
            windows.append(window)
        return True
    user32.EnumWindows(callback, 0)
    return windows


def wait_for(predicate, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.04)
    raise PlayerUnavailable('PotPlayer did not finish the requested operation')


def window_text(window):
    buffer = ct.create_unicode_buffer(4096)
    send(window, 0xD, len(buffer), ct.addressof(buffer))
    return buffer.value


def current_filename(window):
    """Receive UTF-8 through a private WM_COPYDATA window on this thread."""
    result = []
    @WNDPROC
    def callback(receiver, message, wp, lp):
        if message == 0x4A and lp:
            data = ct.cast(lp, ct.POINTER(CopyData)).contents
            if data.kind == 0x6020 and 0 < data.size < 1024 * 1024:
                payload = ct.string_at(data.data, data.size).rstrip(b'\0')
                try:
                    result.append(payload.decode('utf-8'))
                except UnicodeDecodeError:
                    result.append(payload.decode('cp' + str(kernel32.GetACP())))
            return 1
        return user32.DefWindowProcW(receiver, message, wp, lp)
    name = f'SynchroFilename_{os.getpid()}_{time.monotonic_ns()}'
    instance = kernel32.GetModuleHandleW(None)
    wc = WindowClass()
    wc.procedure, wc.instance, wc.class_name = callback, instance, name
    if not user32.RegisterClassW(ct.byref(wc)):
        raise PlayerUnavailable('Cannot create the IPC receiver class')
    receiver = user32.CreateWindowExW(0, name, '', 0, 0, 0, 0, 0,
                                     None, None, instance, None)
    try:
        if not receiver:
            raise PlayerUnavailable('Cannot create the IPC receiver window')
        send(window, 0x400, 0x6020, receiver)
        if not result or not result[-1]:
            raise PlayerUnavailable('PotPlayer has no open media file')
        return result[-1]
    finally:
        if receiver:
            user32.DestroyWindow(receiver)
        user32.UnregisterClassW(name, instance)
