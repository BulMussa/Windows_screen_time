"""Multi-monitor screen time tracker (Windows, stdlib only).

Every second it records, if you were active in the last IDLE_LIMIT seconds:
  - focus:  the foreground app and the monitor it is on
  - cursor: the app under the mouse cursor and the monitor the cursor is on
  - active: the union of the two (an app counts once per second even if both match)

Run:     python screentime_tracker.py
Stop:    Ctrl+C  (prints a report and saves screentime_log.json next to the script)
"""
import ctypes
import json
import os
import time
from collections import defaultdict
from ctypes import wintypes

IDLE_LIMIT = 60  # seconds without input before time stops counting
TICK = 1.0
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screentime_log.json")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.GetForegroundWindow.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
user32.MonitorFromPoint.restype = wintypes.HANDLE
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HANDLE
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]

GA_ROOT = 2
MONITOR_DEFAULTTONEAREST = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds():
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO))
    user32.GetLastInputInfo(ctypes.byref(info))
    return (kernel32.GetTickCount() - info.dwTime) / 1000.0


def monitor_name(hmon):
    info = MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(MONITORINFOEXW)
    if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
        primary = " (primary)" if info.dwFlags & 1 else ""
        return info.szDevice.lstrip("\\.") + primary
    return "unknown"


def app_name(hwnd):
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return f"pid {pid.value}"
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return f"pid {pid.value}"
    finally:
        kernel32.CloseHandle(handle)


def sample():
    """Return (focus, cursor) as (app, monitor) pairs; either may be None."""
    fg = user32.GetForegroundWindow()
    focus = None
    if fg:
        focus = (app_name(fg), monitor_name(user32.MonitorFromWindow(fg, MONITOR_DEFAULTTONEAREST)))

    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    under = user32.WindowFromPoint(pt)
    root = user32.GetAncestor(under, GA_ROOT) if under else None
    cursor = None
    if root:
        cursor = (app_name(root), monitor_name(user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)))
    return focus, cursor


def fmt(seconds):
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s"


def report(totals):
    print("\n=== Screen time report ===")
    for kind in ("active", "focus", "cursor"):
        rows = sorted(totals[kind].items(), key=lambda kv: -kv[1])
        print(f"\n[{kind}]")
        for key, secs in rows[:15]:
            app, mon = key.split("|")
            print(f"  {fmt(secs):>12}  {app:<28} {mon}")


def main():
    totals = {k: defaultdict(float) for k in ("active", "focus", "cursor")}
    if os.path.exists(LOG):
        try:
            with open(LOG) as f:
                for kind, d in json.load(f).items():
                    totals[kind].update(d)
        except (OSError, ValueError):
            pass

    print(f"Tracking... idle limit {IDLE_LIMIT}s. Ctrl+C to stop and show the report.")
    try:
        while True:
            time.sleep(TICK)
            if idle_seconds() > IDLE_LIMIT:
                continue
            focus, cursor = sample()
            seen = set()
            for kind, item in (("focus", focus), ("cursor", cursor)):
                if item and item[0]:
                    key = f"{item[0]}|{item[1]}"
                    totals[kind][key] += TICK
                    seen.add(key)
            for key in seen:
                totals["active"][key] += TICK
    except KeyboardInterrupt:
        pass
    finally:
        with open(LOG, "w") as f:
            json.dump(totals, f, indent=2)
        report(totals)


if __name__ == "__main__":
    main()
