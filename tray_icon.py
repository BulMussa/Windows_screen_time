"""A notification-area (tray) icon built on ctypes.

The icon's hidden window runs its own message loop on a background thread (Python
callbacks can't run inside Tk's event loop), so on_click and the menu callbacks are
called on that thread: hand them over to the UI thread, e.g. through a queue.
New icons land in the "hidden icons" menu (the ^ arrow); drag one onto the taskbar to pin it.
"""
import ctypes
import os
import threading
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON)]


user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
user32.LoadIconW.restype = wintypes.HICON
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wintypes.HWND, wintypes.LPVOID]
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

CLASS_NAME = "ScreenTimeTray"
WM_NULL, WM_DESTROY, WM_CLOSE = 0x0000, 0x0002, 0x0010
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
WM_TRAY = 0x8000 + 1  # WM_APP + 1
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
IMAGE_ICON, LR_LOADFROMFILE, SM_CXSMICON, IDI_APPLICATION = 1, 0x10, 49, 32512
MF_STRING, MF_SEPARATOR = 0x0, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x2, 0x100


class TrayIcon:
    """menu is a list of (label, callback), with None for a separator."""

    def __init__(self, tooltip, icon_path, on_click, menu):
        self.on_click = on_click
        self.menu = menu
        # Explorer broadcasts this when it restarts; the icon has to be added again.
        self.taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self._proc = WNDPROC(self._wndproc)  # keep a reference or it gets garbage collected

        size = user32.GetSystemMetrics(SM_CXSMICON)
        hicon = None
        if icon_path and os.path.exists(icon_path):
            hicon = user32.LoadImageW(None, icon_path, IMAGE_ICON, size, size, LR_LOADFROMFILE)
        self.data = NOTIFYICONDATAW(
            cbSize=ctypes.sizeof(NOTIFYICONDATAW), uID=1,
            uFlags=NIF_MESSAGE | NIF_ICON | NIF_TIP, uCallbackMessage=WM_TRAY,
            hIcon=hicon or user32.LoadIconW(None, IDI_APPLICATION), szTip=tooltip[:127])
        ready = threading.Event()
        threading.Thread(target=self._run, args=(ready,), daemon=True).start()
        ready.wait()

    def _run(self, ready):
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW(lpfnWndProc=self._proc, hInstance=hinst, lpszClassName=CLASS_NAME)
        user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, CLASS_NAME, CLASS_NAME, 0, 0, 0, 0, 0,
                                           None, None, hinst, None)
        self.data.hWnd = self.hwnd
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.data))
        ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def set_tooltip(self, text):
        text = text[:127]
        if text != self.data.szTip:
            self.data.szTip = text
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.data))

    def remove(self):
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.data))
        user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)  # the window's own thread destroys it

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            event = lparam & 0xFFFF
            if event == WM_LBUTTONUP:
                self.on_click()
            elif event == WM_RBUTTONUP:
                self._show_menu()
            return 0
        if msg == self.taskbar_created:
            shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.data))
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)  # ends the message loop
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _show_menu(self):
        menu = user32.CreatePopupMenu()
        for i, item in enumerate(self.menu, 1):
            if item is None:
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            else:
                user32.AppendMenuW(menu, MF_STRING, i, item[0])
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)  # so the menu closes when clicking elsewhere
        cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if cmd:
            self.menu[cmd - 1][1]()
