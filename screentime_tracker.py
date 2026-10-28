"""Multi-monitor screen time tracker (Windows, stdlib only).

Every second it records, if you were active in the last IDLE_LIMIT seconds:
  - focus:  the foreground app and the monitor it is on
  - cursor: the app under the mouse cursor and the monitor the cursor is on
  - active: the union of the two (an app counts once per second even if both match)

Each day's log also holds the overall time the PC was on (total_seconds, counted from
boot, midnight or the script's start) and the time you were active (active_seconds).

Run:     python screentime_tracker.py
Stop:    Ctrl+C  (prints a report and saves logs/<date>.json next to the script)
"""
import ctypes
import json
import os
import time
from collections import defaultdict
from ctypes import wintypes
from datetime import datetime, timedelta

IDLE_LIMIT = 60  # seconds without input before time stops counting
TICK = 1.0
SAVE_EVERY = 60  # seconds between saves, so a shutdown loses at most this much
MAX_GAP = 5  # a longer pause between ticks means the PC was asleep
KINDS = ("active", "focus", "cursor")
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

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
kernel32.GetTickCount64.restype = ctypes.c_ulonglong
kernel32.QueryUnbiasedInterruptTime.argtypes = [ctypes.POINTER(ctypes.c_ulonglong)]
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


def boot_time():
    """When the PC booted, or None if it has slept since (uptime then includes the sleep)."""
    uptime = kernel32.GetTickCount64() / 1000.0
    awake = ctypes.c_ulonglong()
    kernel32.QueryUnbiasedInterruptTime(ctypes.byref(awake))  # 100ns units, sleep excluded
    if uptime - awake.value / 1e7 > 60:
        return None
    return datetime.now() - timedelta(seconds=uptime)


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


def report(log):
    print("\n=== Screen time report ===")
    print(f"\n  PC on today:  {fmt(log['total_seconds'])}  (since {log['first_seen']})")
    print(f"  Active today: {fmt(log['active_seconds'])}")
    for kind in KINDS:
        rows = sorted(log[kind].items(), key=lambda kv: -kv[1])
        print(f"\n[{kind}]")
        for key, secs in rows[:15]:
            app, mon = key.split("|")
            print(f"  {fmt(secs):>12}  {app:<28} {mon}")


def log_path(day):
    return os.path.join(LOG_DIR, f"{day.isoformat()}.json")


def load(day):
    log = {"total_seconds": 0.0, "active_seconds": 0.0, "first_seen": None, "last_seen": None}
    log.update({k: defaultdict(float) for k in KINDS})
    path = log_path(day)
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        for key, value in data.items():
            if key in KINDS:
                log[key].update(value)
            elif key in log:
                log[key] = value
    return log


def save(day, log):
    log["last_seen"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(log_path(day), "w") as f:
        json.dump(log, f, indent=2)


def main():
    now = datetime.now()
    day = now.date()
    log = load(day)

    # Count the time the PC was already on today before the script started:
    # from boot, midnight or the last save of an earlier run, whichever is latest.
    # After a sleep the uptime can't tell when the PC was on, so start counting now.
    boot = boot_time()
    since = now
    if boot:
        since = max(boot, datetime.combine(day, datetime.min.time()))
        if log["last_seen"]:
            since = max(since, datetime.fromisoformat(log["last_seen"]))
    log["total_seconds"] += max(0.0, (now - since).total_seconds())
    log["first_seen"] = log["first_seen"] or since.isoformat(timespec="seconds")

    print(f"Tracking... idle limit {IDLE_LIMIT}s. Ctrl+C to stop and show the report.")
    last = last_save = now
    try:
        while True:
            time.sleep(TICK)
            now = datetime.now()
            if now.date() != day:  # past midnight: close out yesterday's log, restart counters
                save(day, log)
                day = now.date()
                log = load(day)
                log["first_seen"] = log["first_seen"] or now.isoformat(timespec="seconds")
            gap = (now - last).total_seconds()
            last = now
            if 0 < gap <= MAX_GAP:
                log["total_seconds"] += gap
            if (now - last_save).total_seconds() >= SAVE_EVERY:
                save(day, log)
                last_save = now
            if idle_seconds() > IDLE_LIMIT:
                continue
            log["active_seconds"] += TICK
            focus, cursor = sample()
            seen = set()
            for kind, item in (("focus", focus), ("cursor", cursor)):
                if item and item[0]:
                    key = f"{item[0]}|{item[1]}"
                    log[kind][key] += TICK
                    seen.add(key)
            for key in seen:
                log["active"][key] += TICK
    except KeyboardInterrupt:
        pass
    finally:
        save(day, log)
        report(log)


if __name__ == "__main__":
    main()
