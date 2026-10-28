"""Multi-monitor screen time tracker (Windows, stdlib only).

Every second it records, if you were active in the last IDLE_LIMIT seconds:
  - focus:  the foreground app and the monitor it is on
  - cursor: the app under the mouse cursor and the monitor the cursor is on
  - active: the union of the two (an app counts once per second even if both match)

Each day's log also holds the overall time the PC was on (total_seconds, counted from
boot, midnight or the script's start) and the time you were active (active_seconds).

Run:     python -m screentime.tracker
         python -m screentime.tracker --quit   (asks the running tracker or app to save and exit)
Stop:    Ctrl+C  (prints a report and saves <date>.json in %LOCALAPPDATA%\\ScreenTime\\logs)
"""
import ctypes
import json
import _thread
import os
import sys
import threading
import time
from collections import defaultdict
from ctypes import wintypes
from datetime import datetime, timedelta

IDLE_LIMIT = 60  # seconds without input before time stops counting
TICK = 1.0
SAVE_EVERY = 60  # seconds between saves, so a shutdown loses at most this much
MAX_GAP = 5  # a longer pause between ticks means the PC was asleep
KINDS = ("active", "focus", "cursor")
# Logs live in the user's app data, wherever the script or ScreenTime.exe is.
LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local"),
                       "ScreenTime", "logs")

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
user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
user32.OpenInputDesktop.restype = wintypes.HANDLE
user32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
user32.CloseDesktop.argtypes = [wintypes.HANDLE]
kernel32.GetTickCount64.restype = ctypes.c_ulonglong
kernel32.QueryUnbiasedInterruptTime.argtypes = [ctypes.POINTER(ctypes.c_ulonglong)]
kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenMutexW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenEventW.restype = wintypes.HANDLE
kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]

GA_ROOT = 2
MONITOR_DEFAULTTONEAREST = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
UOI_NAME = 2
DESKTOP_READOBJECTS = 0x0001
ERROR_ALREADY_EXISTS = 183
SYNCHRONIZE, EVENT_MODIFY_STATE, INFINITE = 0x00100000, 0x0002, 0xFFFFFFFF
MUTEX_NAME = "Local\\ScreenTimeTracker"  # held by the running tracker, so only one runs
QUIT_EVENT = "Local\\ScreenTimeTrackerQuit"  # set by --quit to ask it to save and exit
_mutex = None


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


def screen_locked():
    """True while the lock screen (a desktop other than the user's "Default") has the input."""
    desk = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if not desk:
        return True  # the lock screen's desktop can't be opened from the user's session
    try:
        name = ctypes.create_unicode_buffer(64)
        user32.GetUserObjectInformationW(desk, UOI_NAME, name, ctypes.sizeof(name), None)
        return name.value.lower() != "default"
    finally:
        user32.CloseDesktop(desk)


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
    log = {"total_seconds": 0.0, "active_seconds": 0.0, "first_seen": None, "last_seen": None,
           "stopped": False}  # stopped: the last run ended with a deliberate quit
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
    # Write to a temp file and swap it in, so a crash mid-write can't corrupt the log.
    path = log_path(day)
    with open(path + ".tmp", "w") as f:
        json.dump(log, f, indent=2)
    os.replace(path + ".tmp", path)


def already_running():
    """True if another copy of the tracker (script or app) is already running."""
    global _mutex
    _mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)  # held until exit
    return ctypes.get_last_error() == ERROR_ALREADY_EXISTS


def on_quit_request(callback):
    """Call callback (on a background thread) when another process runs --quit."""
    event = kernel32.CreateEventW(None, False, False, QUIT_EVENT)  # auto-reset

    def wait():
        kernel32.WaitForSingleObject(event, INFINITE)
        callback()
    threading.Thread(target=wait, daemon=True).start()


def request_quit(timeout=10.0):
    """Ask the running tracker or app to save and exit, and wait until it has.

    Returns None if none is running, True once it exited, False if it didn't in time.
    """
    event = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, QUIT_EVENT)
    if not event:
        return None
    kernel32.SetEvent(event)
    kernel32.CloseHandle(event)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        mutex = kernel32.OpenMutexW(SYNCHRONIZE, False, MUTEX_NAME)
        if not mutex:  # its process is gone
            return True
        kernel32.CloseHandle(mutex)
        time.sleep(0.2)
    return False


def quit_command():
    """Handle --quit for the entry points; returns the process exit code."""
    result = request_quit()
    print({None: "The tracker isn't running.", True: "The tracker saved and quit.",
           False: "The tracker didn't quit within 10 seconds."}[result])
    return {None: 1, True: 0, False: 2}[result]


class Tracker:
    """Counts screen time into today's log. Call tick() about every TICK seconds."""

    def __init__(self):
        now = datetime.now()
        self.day = now.date()
        self.log = load(self.day)
        self.lock = threading.Lock()

        # Count the time the PC was already on today before the script started:
        # from boot, midnight or the last save of an earlier run, whichever is latest.
        # Not after a deliberate quit (that time wasn't tracked on purpose), and not after
        # a sleep (the uptime then can't tell when the PC was on): start counting now.
        boot = boot_time()
        since = now
        if boot and not self.log["stopped"]:
            since = max(boot, datetime.combine(self.day, datetime.min.time()))
            if self.log["last_seen"]:
                since = max(since, datetime.fromisoformat(self.log["last_seen"]))
        self.log["total_seconds"] += max(0.0, (now - since).total_seconds())
        self.log["first_seen"] = self.log["first_seen"] or since.isoformat(timespec="seconds")
        self.log["stopped"] = False
        self.last = self.last_save = now

    def tick(self):
        with self.lock:
            now = datetime.now()
            if now.date() != self.day:  # past midnight: close out yesterday's log, restart counters
                save(self.day, self.log)
                self.day = now.date()
                self.log = load(self.day)
                self.log["first_seen"] = self.log["first_seen"] or now.isoformat(timespec="seconds")
            gap = (now - self.last).total_seconds()
            self.last = now
            locked = screen_locked()  # away: nothing counts, not even PC-on time
            if 0 < gap <= MAX_GAP and not locked:
                self.log["total_seconds"] += gap
            if (now - self.last_save).total_seconds() >= SAVE_EVERY:
                save(self.day, self.log)
                self.last_save = now
            if locked or idle_seconds() > IDLE_LIMIT:
                return
            self.log["active_seconds"] += TICK
            focus, cursor = sample()
            seen = set()
            for kind, item in (("focus", focus), ("cursor", cursor)):
                if item and item[0]:
                    key = f"{item[0]}|{item[1]}"
                    self.log[kind][key] += TICK
                    seen.add(key)
            for key in seen:
                self.log["active"][key] += TICK

    def save(self, stopped=False):
        """Save today's log; stopped=True marks a deliberate quit, so the gap until the
        next start isn't counted as PC-on time."""
        with self.lock:
            self.log["stopped"] = stopped
            save(self.day, self.log)

    def snapshot(self):
        """A copy of today's log that is safe to read from another thread."""
        with self.lock:
            return {k: dict(v) if k in KINDS else v for k, v in self.log.items()}


def main():
    if "--quit" in sys.argv:
        sys.exit(quit_command())
    if already_running():
        print("The tracker is already running (as the script or the app).")
        return
    tracker = Tracker()
    remote_quit = threading.Event()
    on_quit_request(lambda: (remote_quit.set(), _thread.interrupt_main()))
    print(f"Tracking... idle limit {IDLE_LIMIT}s. Ctrl+C to stop and show the report.")
    stopped = False
    try:
        while True:
            time.sleep(TICK)
            tracker.tick()
    except KeyboardInterrupt:
        # Ctrl+C is a deliberate stop; --quit is a restart, so its gap may still count.
        stopped = not remote_quit.is_set()
    finally:
        tracker.save(stopped)
        report(tracker.log)


if __name__ == "__main__":
    main()
