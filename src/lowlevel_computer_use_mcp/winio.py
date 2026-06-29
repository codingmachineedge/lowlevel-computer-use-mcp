"""Win32 background input + per-window capture + hidden-desktop helpers.

This module makes it possible to drive a target window *without* bringing it to
the foreground:

  * Mouse input  - PostMessage WM_*BUTTON* to the deepest child control at a point
  * Keyboard     - PostMessage WM_CHAR / WM_KEYDOWN / WM_KEYUP, or WM_SETTEXT
  * Capture      - PrintWindow (PW_RENDERFULLCONTENT) renders a window into a DC
                   even when it is occluded, unfocused, or on another desktop
  * Headless GUI - CreateDesktop + CreateProcess(lpDesktop=...) runs a real GUI
                   app on an off-screen desktop that never touches the visible one

Everything is implemented with ctypes against user32/gdi32/kernel32 so behaviour
is predictable on 64-bit Python. All functions raise WinIOError with an
actionable message on failure.

Caveats (inherent to message-based injection, documented for honesty):
  * Apps that read raw input / DirectInput / check physical key state
    (GetAsyncKeyState) may ignore posted messages. Modifier combos (Ctrl+C) are
    unreliable via messages; prefer WM_SETTEXT / app-specific messages, or use
    foreground input for those apps.
  * PrintWindow works for most GDI and Chromium/DWM windows with
    PW_RENDERFULLCONTENT, but a few GPU-exclusive surfaces render black.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------- #
# ctypes setup
# --------------------------------------------------------------------------- #
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WPARAM = ctypes.c_size_t  # unsigned, pointer-sized
LPARAM = ctypes.c_ssize_t  # signed, pointer-sized
LRESULT = ctypes.c_ssize_t
HWND = wintypes.HWND
HDESK = wintypes.HANDLE

# Window messages
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0201, 0x0202, 0x0203
WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK = 0x0204, 0x0205, 0x0206
WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MBUTTONDBLCLK = 0x0207, 0x0208, 0x0209
WM_MOUSEWHEEL = 0x020A
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_CHAR = 0x0102
WM_SETTEXT, WM_GETTEXT, WM_GETTEXTLENGTH = 0x000C, 0x000D, 0x000E

MK_LBUTTON, MK_RBUTTON, MK_MBUTTON = 0x0001, 0x0002, 0x0010

# ChildWindowFromPointEx flags
CWP_SKIPINVISIBLE = 0x0001
CWP_SKIPTRANSPARENT = 0x0004

# PrintWindow flags
PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002

# Desktop / process
GENERIC_ALL = 0x10000000
DESKTOP_CREATEWINDOW = 0x0002
STARTF_USESHOWWINDOW = 0x00000001
CREATE_NEW_CONSOLE = 0x00000010
SW_SHOWNORMAL = 1


class WinIOError(RuntimeError):
    pass


def _check(value: Any, what: str) -> Any:
    if not value:
        err = ctypes.get_last_error()
        raise WinIOError(f"{what} failed (GetLastError={err}: {ctypes.FormatError(err).strip()})")
    return value


# argtypes / restypes
user32.PostMessageW.argtypes = [HWND, wintypes.UINT, WPARAM, LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.SendMessageW.argtypes = [HWND, wintypes.UINT, WPARAM, LPARAM]
user32.SendMessageW.restype = LRESULT
user32.SendMessageTimeoutW.argtypes = [
    HWND, wintypes.UINT, WPARAM, LPARAM, wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)
]
user32.SendMessageTimeoutW.restype = LRESULT
user32.PrintWindow.argtypes = [HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClientRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowDC.argtypes = [HWND]
user32.GetWindowDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [HWND, wintypes.HDC]
user32.ClientToScreen.argtypes = [HWND, ctypes.POINTER(wintypes.POINT)]
user32.ScreenToClient.argtypes = [HWND, ctypes.POINTER(wintypes.POINT)]
user32.MapWindowPoints.argtypes = [HWND, HWND, ctypes.POINTER(wintypes.POINT), wintypes.UINT]
user32.ChildWindowFromPointEx.argtypes = [HWND, wintypes.POINT, wintypes.UINT]
user32.ChildWindowFromPointEx.restype = HWND
user32.GetClassNameW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindow.argtypes = [HWND]
user32.IsWindowVisible.argtypes = [HWND]
user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
user32.VkKeyScanW.restype = ctypes.c_short

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.GetDIBits.argtypes = [
    wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
    ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
]
gdi32.GetDIBits.restype = ctypes.c_int


# --------------------------------------------------------------------------- #
# Window discovery
# --------------------------------------------------------------------------- #
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, HWND, LPARAM)


def _get_window_text(hwnd: int) -> str:
    length = user32.SendMessageW(hwnd, WM_GETTEXTLENGTH, 0, 0)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.SendMessageW(hwnd, WM_GETTEXT, length + 1, ctypes.addressof(buf))
    return buf.value


def _get_class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def find_top_window(title: Optional[str], handle: Optional[int]) -> int:
    """Resolve a top-level window handle by exact handle or title substring."""
    if handle is not None:
        if not user32.IsWindow(handle):
            raise WinIOError(f"No window with handle {handle}.")
        return int(handle)
    if not title:
        raise WinIOError("Provide a window title or handle.")
    title_l = title.lower()
    matches: list[int] = []

    @WNDENUMPROC
    def cb(hwnd, _lparam):
        text = _get_window_text(hwnd)
        if text and title_l in text.lower():
            matches.append(int(hwnd))
        return True

    user32.EnumWindows(cb, 0)
    if not matches:
        raise WinIOError(f"No top-level window whose title contains '{title}'.")
    return matches[0]


def list_child_windows(hwnd: int) -> list[dict[str, Any]]:
    """Enumerate child controls of a window with class, text and client rect."""
    children: list[dict[str, Any]] = []
    top_rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(top_rect))

    @WNDENUMPROC
    def cb(child, _lparam):
        r = wintypes.RECT()
        user32.GetWindowRect(child, ctypes.byref(r))
        children.append(
            {
                "handle": int(child),
                "class": _get_class_name(child),
                "text": _get_window_text(child),
                # rect relative to the top-level window's top-left
                "left": r.left - top_rect.left,
                "top": r.top - top_rect.top,
                "width": r.right - r.left,
                "height": r.bottom - r.top,
                "visible": bool(user32.IsWindowVisible(child)),
            }
        )
        return True

    user32.EnumChildWindows(hwnd, cb, 0)
    return children


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", HWND),
        ("hwndFocus", HWND),
        ("hwndCapture", HWND),
        ("hwndMenuOwner", HWND),
        ("hwndMoveSize", HWND),
        ("hwndCaret", HWND),
        ("rcCaret", wintypes.RECT),
    ]


user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.GetGUIThreadInfo.restype = wintypes.BOOL


def focused_control(hwnd: int) -> int:
    """Return the focused control within the window's UI thread, or hwnd itself."""
    tid = user32.GetWindowThreadProcessId(hwnd, None)
    gui = GUITHREADINFO()
    gui.cbSize = ctypes.sizeof(GUITHREADINFO)
    if user32.GetGUIThreadInfo(tid, ctypes.byref(gui)) and gui.hwndFocus:
        return int(gui.hwndFocus)
    return int(hwnd)


def _deepest_child_at(top: int, x: int, y: int) -> tuple[int, int, int]:
    """Descend from `top` to the deepest child containing client point (x, y).

    Returns (target_hwnd, child_x, child_y) where child_x/y are client coords of
    the returned window.
    """
    cur, cx, cy = top, x, y
    for _ in range(32):  # guard against cycles
        pt = wintypes.POINT(cx, cy)
        child = user32.ChildWindowFromPointEx(cur, pt, CWP_SKIPINVISIBLE | CWP_SKIPTRANSPARENT)
        if not child or int(child) == int(cur):
            break
        p = wintypes.POINT(cx, cy)
        user32.MapWindowPoints(cur, child, ctypes.byref(p), 1)
        cur, cx, cy = int(child), p.x, p.y
    return cur, cx, cy


# --------------------------------------------------------------------------- #
# Background mouse / keyboard
# --------------------------------------------------------------------------- #
def _lparam_xy(x: int, y: int) -> int:
    return (y << 16) | (x & 0xFFFF)


_BTN = {
    "left": (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK, MK_LBUTTON),
    "right": (WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK, MK_RBUTTON),
    "middle": (WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MBUTTONDBLCLK, MK_MBUTTON),
}


def send_click(top: int, x: int, y: int, button: str = "left", double: bool = False) -> dict[str, Any]:
    """Post a mouse click to the deepest control at client point (x, y) of `top`.

    x, y are in client coordinates of the top-level window. The window does not
    need to be focused or visible.
    """
    down, up, dbl, mk = _BTN[button]
    target, cx, cy = _deepest_child_at(top, x, y)
    lp = _lparam_xy(cx, cy)
    user32.PostMessageW(target, WM_MOUSEMOVE, 0, lp)
    user32.PostMessageW(target, down, mk, lp)
    user32.PostMessageW(target, up, 0, lp)
    if double:
        user32.PostMessageW(target, dbl, mk, lp)
        user32.PostMessageW(target, up, 0, lp)
    return {"target_hwnd": target, "client_x": cx, "client_y": cy, "button": button, "double": double}


def send_text(hwnd: int, text: str) -> dict[str, Any]:
    """Post each character as WM_CHAR to a window (typically a focused control)."""
    for ch in text:
        user32.PostMessageW(hwnd, WM_CHAR, ord(ch), 0)
    return {"target_hwnd": int(hwnd), "chars": len(text)}


def set_control_text(hwnd: int, text: str) -> dict[str, Any]:
    """Set a control's text directly via WM_SETTEXT (reliable for edit controls)."""
    buf = ctypes.create_unicode_buffer(text)
    res = user32.SendMessageTimeoutW(
        hwnd, WM_SETTEXT, 0, ctypes.addressof(buf), 0, 2000, ctypes.byref(ctypes.c_size_t())
    )
    return {"target_hwnd": int(hwnd), "ok": bool(res), "text_len": len(text)}


_VK = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12, "win": 0x5B,
    "capslock": 0x14, "printscreen": 0x2C,
}
for _i in range(1, 25):  # F1..F24
    _VK[f"f{_i}"] = 0x70 + (_i - 1)


def _vk_for(name: str) -> int:
    name = name.lower()
    if name in _VK:
        return _VK[name]
    if len(name) == 1:
        res = user32.VkKeyScanW(name)
        if res != -1:
            return res & 0xFF
    raise WinIOError(f"Unknown key name: '{name}'")


def _key_lparams(vk: int) -> tuple[int, int]:
    scan = user32.MapVirtualKeyW(vk, 0)
    down = 1 | (scan << 16)
    up = down | (0x3 << 30)  # transition + previous-state bits
    return down, up


def send_keys(hwnd: int, keys: list[str]) -> dict[str, Any]:
    """Post a key / key-combo via WM_KEYDOWN/UP.

    A single key is pressed and released. Multiple keys are pressed in order then
    released in reverse (modifier-style), but note message-based modifier combos
    are unreliable for apps that check physical key state - prefer set_control_text
    for text entry.
    """
    vks = [_vk_for(k) for k in keys]
    for vk in vks:
        down, _ = _key_lparams(vk)
        user32.PostMessageW(hwnd, WM_KEYDOWN, vk, down)
    for vk in reversed(vks):
        _, up = _key_lparams(vk)
        user32.PostMessageW(hwnd, WM_KEYUP, vk, up)
    return {"target_hwnd": int(hwnd), "keys": keys}


# --------------------------------------------------------------------------- #
# Per-window capture via PrintWindow
# --------------------------------------------------------------------------- #
class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def capture_window(hwnd: int, client_only: bool = False):
    """Capture a window via PrintWindow into a PIL Image, even if unfocused/occluded.

    Returns (PIL.Image, rendered_ok: bool). Requires Pillow.
    """
    from PIL import Image  # local import keeps the dependency optional

    if not user32.IsWindow(hwnd):
        raise WinIOError(f"No window with handle {hwnd}.")

    rect = wintypes.RECT()
    if client_only:
        user32.GetClientRect(hwnd, ctypes.byref(rect))
    else:
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise WinIOError(f"Window has non-positive size ({w}x{h}); it may be minimized.")

    hdc_win = _check(user32.GetWindowDC(hwnd), "GetWindowDC")
    hdc_mem = _check(gdi32.CreateCompatibleDC(hdc_win), "CreateCompatibleDC")
    hbmp = _check(gdi32.CreateCompatibleBitmap(hdc_win, w, h), "CreateCompatibleBitmap")
    try:
        gdi32.SelectObject(hdc_mem, hbmp)
        flags = PW_RENDERFULLCONTENT | (PW_CLIENTONLY if client_only else 0)
        rendered = bool(user32.PrintWindow(hwnd, hdc_mem, flags))

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB

        buf = ctypes.create_string_buffer(w * h * 4)
        _check(
            gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), 0),
            "GetDIBits",
        )
        img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
        return img, rendered
    finally:
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_win)


# --------------------------------------------------------------------------- #
# Headless GUI: off-screen desktop
# --------------------------------------------------------------------------- #
user32.CreateDesktopW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
]
user32.CreateDesktopW.restype = HDESK
user32.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
user32.OpenDesktopW.restype = HDESK
user32.CloseDesktop.argtypes = [HDESK]
user32.EnumDesktopWindows.argtypes = [HDESK, WNDENUMPROC, LPARAM]


class STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


kernel32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
    wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
    ctypes.POINTER(STARTUPINFO), ctypes.POINTER(PROCESS_INFORMATION),
]
kernel32.CreateProcessW.restype = wintypes.BOOL

# name -> HDESK handle for desktops we created
_DESKTOPS: dict[str, int] = {}


def create_desktop(name: str) -> dict[str, Any]:
    """Create (or reopen) an off-screen desktop in the current window station."""
    hdesk = user32.CreateDesktopW(name, None, None, 0, GENERIC_ALL, None)
    _check(hdesk, f"CreateDesktopW('{name}')")
    _DESKTOPS[name] = int(hdesk)
    return {"name": name, "handle": int(hdesk), "full": f"WinSta0\\{name}"}


def launch_on_desktop(name: str, command_line: str) -> dict[str, Any]:
    """Launch a process whose GUI appears on the off-screen desktop `name`."""
    if name not in _DESKTOPS:
        create_desktop(name)
    si = STARTUPINFO()
    si.cb = ctypes.sizeof(STARTUPINFO)
    si.lpDesktop = f"WinSta0\\{name}"
    si.dwFlags = STARTF_USESHOWWINDOW
    si.wShowWindow = SW_SHOWNORMAL
    pi = PROCESS_INFORMATION()
    cmd_buf = ctypes.create_unicode_buffer(command_line)
    ok = kernel32.CreateProcessW(
        None, cmd_buf, None, None, False, CREATE_NEW_CONSOLE, None, None,
        ctypes.byref(si), ctypes.byref(pi),
    )
    _check(ok, f"CreateProcessW('{command_line}')")
    kernel32.CloseHandle(pi.hThread)
    kernel32.CloseHandle(pi.hProcess)
    return {"desktop": name, "pid": int(pi.dwProcessId), "command": command_line}


def list_desktop_windows(name: str) -> list[dict[str, Any]]:
    """Enumerate top-level windows that live on the off-screen desktop `name`."""
    hdesk = _DESKTOPS.get(name)
    if hdesk is None:
        hdesk = user32.OpenDesktopW(name, 0, False, GENERIC_ALL)
        _check(hdesk, f"OpenDesktopW('{name}')")
        _DESKTOPS[name] = int(hdesk)
    out: list[dict[str, Any]] = []

    @WNDENUMPROC
    def cb(hwnd, _lparam):
        text = _get_window_text(hwnd)
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        out.append(
            {
                "handle": int(hwnd),
                "title": text,
                "class": _get_class_name(hwnd),
                "width": r.right - r.left,
                "height": r.bottom - r.top,
            }
        )
        return True

    user32.EnumDesktopWindows(hdesk, cb, 0)
    return out


def close_desktop(name: str) -> dict[str, Any]:
    """Close our handle to the off-screen desktop (it is freed once no process uses it)."""
    hdesk = _DESKTOPS.pop(name, None)
    if hdesk is None:
        return {"name": name, "closed": False, "note": "not tracked"}
    user32.CloseDesktop(hdesk)
    return {"name": name, "closed": True}


# --------------------------------------------------------------------------- #
# Temporary visibility: show a hidden window / switch to a headless desktop
# (e.g. so a human can complete an interactive login), then hide again.
# --------------------------------------------------------------------------- #
SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_SHOW = 5
SW_MINIMIZE = 6
SW_RESTORE = 9
DESKTOP_SWITCHDESKTOP = 0x0100

user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
user32.OpenInputDesktop.restype = HDESK
user32.SwitchDesktop.argtypes = [HDESK]
user32.SwitchDesktop.restype = wintypes.BOOL

# Remembers the input desktop we left so hide_desktop can switch back.
_saved_input_desktop: Optional[int] = None


def show_window(hwnd: int) -> dict[str, Any]:
    """Make a hidden/minimized window visible and bring it to the foreground.

    Use this when an automated app on the *normal* desktop needs human interaction
    (e.g. a login prompt). Pair with hide_window to put it away again afterwards.
    """
    if not user32.IsWindow(hwnd):
        raise WinIOError(f"No window with handle {hwnd}.")
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.ShowWindow(hwnd, SW_SHOW)
    user32.SetForegroundWindow(hwnd)
    return {"hwnd": int(hwnd), "visible": True}


def hide_window(hwnd: int, minimize: bool = False) -> dict[str, Any]:
    """Hide a window again (SW_HIDE removes it from the taskbar; minimize just tucks it away)."""
    if not user32.IsWindow(hwnd):
        raise WinIOError(f"No window with handle {hwnd}.")
    user32.ShowWindow(hwnd, SW_MINIMIZE if minimize else SW_HIDE)
    return {"hwnd": int(hwnd), "visible": False, "minimized": minimize}


def show_desktop(name: str) -> dict[str, Any]:
    """Switch the live input desktop to a headless desktop so a human can use it.

    The whole off-screen desktop becomes the interactive one (its app windows are
    now on screen). Remember to call hide_desktop to return to the normal desktop -
    typically after the user finishes an interactive login.
    """
    global _saved_input_desktop
    hdesk = _DESKTOPS.get(name)
    if hdesk is None:
        hdesk = user32.OpenDesktopW(name, 0, False, GENERIC_ALL)
        _check(hdesk, f"OpenDesktopW('{name}')")
        _DESKTOPS[name] = int(hdesk)
    current = user32.OpenInputDesktop(0, False, GENERIC_ALL)
    _saved_input_desktop = int(current) if current else None
    _check(user32.SwitchDesktop(hdesk), f"SwitchDesktop('{name}')")
    return {
        "name": name,
        "visible": True,
        "note": "This desktop is now interactive. Call hide_headless_desktop to switch back.",
    }


def hide_desktop(name: Optional[str] = None) -> dict[str, Any]:
    """Switch the input desktop back to the one we came from (or 'Default')."""
    global _saved_input_desktop
    target = _saved_input_desktop
    if target is None:
        target = user32.OpenDesktopW("Default", 0, False, GENERIC_ALL)
        _check(target, "OpenDesktopW('Default')")
    _check(user32.SwitchDesktop(target), "SwitchDesktop(restore)")
    if _saved_input_desktop is not None:
        user32.CloseDesktop(_saved_input_desktop)
        _saved_input_desktop = None
    return {"restored": True}
