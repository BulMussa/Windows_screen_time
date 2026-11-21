"""Screen time tracker with a small window (Windows; stdlib, plus cryptography and segno for phone pairing).

Tracks in the background exactly like screentime.tracker and shows today's active
time ("Active 3h 12m") in the bottom-left corner of the taskbar, plus an icon in the
hidden icons menu. Click either to open the window with today's apps and the daily history;
right-click either to quit. Closing the window only hides it, tracking goes on.

Run:     pythonw -m screentime     (or ScreenTime.exe, built by packaging/build.bat)
Quit:    ScreenTime.exe --quit     (or python -m screentime --quit): saves and exits the running copy
"""
import base64
import calendar
import ctypes
import json
import math
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import winreg
from datetime import date, datetime, timedelta
from ctypes import wintypes
from tkinter import filedialog, font as tkfont, messagebox, ttk

import segno

from . import firewall, peer, sync, tracker
from .tracker import (APP_PATHS, KINDS, LOG_DIR, MONITOR_DEFAULTTONEAREST, MONITORINFOEXW, TICK,
                      Tracker, already_running, on_quit_request, quit_command, uptime_seconds, user32,
                      wait_until_stopped)
from .icons import _png, icon_png, large_icon_size, small_icon_size
from .report import logged_days, read_log, read_logs, summarize
from .timeline import day_spans, total
from .tray import TrayIcon

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "ScreenTimeTracker"
THEME_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
TABLE_REFRESH = 5  # seconds between refreshes of the app tables
HISTORY_REFRESH = 60
CLOCK_OFFSET = 12  # pixels from the left end of the taskbar
RAISE_EVERY = 500  # ms; the taskbar covers the clock when clicked, so keep raising it
TIMELINE_TRACK, TIMELINE_ON, TIMELINE_NOW = "#e5e7eb", "#2563eb", "#4b5563"
TIMELINE_HOVER = "#1e3a8a"  # a laptop PC-on period under the mouse
TIMELINE_PHONE, TIMELINE_PHONE_HOVER = "#f97316", "#c2410c"
TIMELINE_SLEEP, TIMELINE_SLEEP_HOVER = "#7c3aed", "#5b21b6"
ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "screentime.ico")
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.LoadImageW.restype = wintypes.HANDLE
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = wintypes.LPARAM
user32.GetDpiForWindow.argtypes = [wintypes.HWND]
user32.GetDpiForWindow.restype = wintypes.UINT
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
WM_SETICON, ICON_BIG, IMAGE_ICON, LR_LOADFROMFILE = 0x80, 1, 1, 0x10
TASKBAR_ICON = 24  # px at 100% scaling; the taskbar scales it with the display
SHELL_CLASSES = ("Progman", "WorkerW", "Shell_TrayWnd")  # desktop and taskbar


def pythonw():
    """Python without a console window, next to the Python running this."""
    path = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return path if os.path.exists(path) else sys.executable


def autostart_command():
    if getattr(sys, "frozen", False):  # running as ScreenTime.exe
        return f'"{sys.executable}" --minimized'
    # Login starts it with no working directory, so put the project on the path itself.
    run = f"import sys; sys.path.insert(0, r'{PROJECT_DIR}'); from screentime.app import main; main()"
    return f'"{pythonw()}" -c "{run}" --minimized'


def restart_command():
    """Starts a new copy that first waits for this one to exit (see --restart in main)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--restart"]
    return [pythonw(), "-m", "screentime", "--restart"]


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_NAME)
        return True
    except OSError:
        return False


def set_autostart(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass


def settings_path():
    """settings.json next to the logs folder (looked up through tracker, so tests can redirect it)."""
    return os.path.join(os.path.dirname(tracker.LOG_DIR), "settings.json")


def load_settings():
    settings = {"taskbar_clock": True}
    try:
        with open(settings_path()) as f:
            settings.update(json.load(f))
    except (OSError, ValueError):
        pass
    return settings


def save_settings(settings):
    path = settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(settings, f, indent=2)
    os.replace(path + ".tmp", path)


def light_theme():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, THEME_KEY) as key:
            return winreg.QueryValueEx(key, "SystemUsesLightTheme")[0] == 1
    except OSError:
        return False


def window_rect(hwnd):
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect


def monitor_rect(hwnd):
    info = MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(MONITORINFOEXW)
    user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST), ctypes.byref(info))
    return info.rcMonitor


def visible_taskbar():
    """The main taskbar's rect, or None if it is auto-hidden or under a fullscreen app."""
    bar = user32.FindWindowW("Shell_TrayWnd", None)
    if not bar:
        return None
    rect, mon = window_rect(bar), monitor_rect(bar)
    if min(rect.bottom, mon.bottom) - max(rect.top, mon.top) < 10:  # slid off screen
        return None
    fg = user32.GetForegroundWindow()
    if fg and user32.MonitorFromWindow(fg, MONITOR_DEFAULTTONEAREST) == \
            user32.MonitorFromWindow(bar, MONITOR_DEFAULTTONEAREST):
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(fg, cls, 64)
        win = window_rect(fg)
        if cls.value not in SHELL_CLASSES and win.left <= mon.left and win.top <= mon.top \
                and win.right >= mon.right and win.bottom >= mon.bottom:
            return None
    return rect


def short(seconds):
    """Hours and minutes, e.g. 3h 05m; with days once there is at least one (for uptime)."""
    d, rem = divmod(int(seconds) // 60, 24 * 60)
    h, m = divmod(rem, 60)
    return f"{d}d {h}h {m:02d}m" if d else f"{h}h {m:02d}m"


def duration(seconds):
    """Like short, but leaves out the hours until there is at least one."""
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def fit(text, font, width):
    """text, shortened with an ellipsis to fit in width pixels."""
    if font.measure(text) <= width:
        return text
    while text and font.measure(text + "…") > width:
        text = text[:-1]
    return text + "…"


def pill_edges(px, y1, y2, left, right):
    """Top and bottom y at px of a bar from left to right with fully round ends (radius = half its height)."""
    r = (y2 - y1) / 2
    d = max(left + r - px, px - (right - r), 0)  # how far px is into a round end
    dy = r - math.sqrt(max(r * r - d * d, 0))
    return y1 + dy, y2 - dy


def pill_points(xa, xb, y1, y2, left, right):
    """Polygon points of the part xa..xb of that round-ended bar: square in the middle, curved at the ends."""
    xa, xb = max(xa, left), min(xb, right)
    r = (y2 - y1) / 2
    ends = [*range(math.ceil(left), math.floor(left + r) + 1), *range(math.ceil(right - r), math.floor(right) + 1)]
    xs = [xa, *(px for px in ends if xa < px < xb), xb]  # a point every pixel along the curves traces them exactly
    tops, bottoms = [], []
    for px in xs:
        ya, yb = pill_edges(px, y1, y2, left, right)
        tops += [px, ya]
        bottoms += [px, yb]
    return tops + [v for i in range(len(bottoms) - 2, -1, -2) for v in bottoms[i:i + 2]]


def last_synced_text(stamp, now=None):
    """"Phone last synced at 12:39." from peer.json's last_sync (local time, ISO); yesterday and older days say so."""
    if not stamp:
        return "The phone hasn't synced yet."
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return "The phone hasn't synced yet."
    today = (now or datetime.now()).date()
    clock = when.strftime("%H:%M")
    if when.date() == today:
        return f"Phone last synced at {clock}."
    if when.date() == today - timedelta(days=1):
        return f"Phone last synced yesterday at {clock}."
    return f"Phone last synced on {when.day} {when:%b} at {clock}."


def colored_button(parent, text, color, hover, command):
    """A flat button in a solid color (ttk buttons can't be colored on Windows)."""
    button = tk.Button(parent, text=text, command=command, bg=color, fg="white",
                       activebackground=hover, activeforeground="white", relief="flat", bd=0,
                       padx=16, pady=5, font=("Segoe UI Semibold", 9), cursor="hand2")
    button.bind("<Enter>", lambda e: button.config(bg=hover))
    button.bind("<Leave>", lambda e: button.config(bg=color))
    return button


def cloud_png(size, color):
    """A cloud icon, size x size pixels in color on a see-through background, as PNG bytes."""
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    bumps = ((0.29, 0.62, 0.19), (0.53, 0.47, 0.26), (0.75, 0.62, 0.19))  # (x, y, radius), 0-1 of the size
    n = 4  # samples per pixel side, for smooth edges

    def inside(x, y):
        if y > 0.81:  # flat bottom
            return False
        return 0.29 <= x <= 0.75 and y >= 0.62 or any((x - cx) ** 2 + (y - cy) ** 2 <= cr * cr for cx, cy, cr in bumps)

    rows = bytearray()
    for py in range(size):
        rows.append(0)  # PNG row filter: none
        for px in range(size):
            hits = sum(inside((px + (i + 0.5) / n) / size, (py + 0.08 * size + (j + 0.5) / n) / size)
                       for i in range(n) for j in range(n))
            rows += bytes((r, g, b, 255 * hits // (n * n)))
    return _png(size, size, bytes(rows))


class SyncButton:
    """The Sync button, the same in every tab: a cloud and "Sync" when idle, then "Syncing…" and
    "✓ Synced" or "Sync failed", or "Phone syncs" in "Phone directly" mode, where the phone starts every
    sync and this PC has nothing to send. Its size fits the longest of these, so the text changes inside it."""
    TEXTS = {"idle": " Sync", "syncing": "Syncing…", "done": "✓ Synced", "failed": "Sync failed",
             "direct": "Phone syncs"}

    def __init__(self, parent, app, command):
        font = app.sync_font
        if app.cloud is None:  # one image for every Sync button, sized to the text
            app.cloud = tk.PhotoImage(data=cloud_png(font.metrics("linespace") - 2, "#374151"))
        self.cloud = app.cloud
        padx, pady = int(8 * app.scale), int(2 * app.scale)
        width = max(font.measure(text) + (self.cloud.width() if state == "idle" else 0)
                    for state, text in self.TEXTS.items()) + 2 * padx
        height = font.metrics("linespace") + 2 * pady
        self.frame = tk.Frame(parent, width=width, height=height, bg="#e5e7eb")
        self.frame.pack_propagate(False)  # fixed size, whatever the text
        self.button = tk.Button(self.frame, command=command, relief="flat", bd=0, bg="#e5e7eb",
                                activebackground="#d1d5db", fg="#111", disabledforeground="#6b7280", font=font,
                                padx=0, pady=0, cursor="hand2", compound="left")
        self.button.pack(fill="both", expand=True)
        self.show("idle")

    def show(self, state):
        busy = state == "syncing"
        self.button.config(text=self.TEXTS[state], image=self.cloud if state == "idle" else "",
                           state="disabled" if busy else "normal", cursor="" if busy else "hand2")


def note(parent, text, **pack):
    """A gray caption that wraps to the width it gets."""
    label = ttk.Label(parent, text=text, style="Caption.TLabel")
    label.pack(fill="x", **pack)
    label.bind("<Configure>", lambda e: label.configure(wraplength=e.width))
    return label


def make_table(parent, columns, tree_column=None):
    """A Treeview with a scrollbar; columns is [(name, width, anchor)].

    tree_column=(name, width) adds a first column that can show an icon before the text.
    """
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns],
                        show="tree headings" if tree_column else "headings")
    if tree_column:
        tree.heading("#0", text=tree_column[0], anchor="w")
        tree.column("#0", width=tree_column[1], stretch=True)
    for name, width, anchor in columns:
        tree.heading(name, text=name)
        tree.column(name, width=width, anchor=anchor, stretch=anchor == "w")
    bar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=bar.set)
    tree.pack(side="left", fill="both", expand=True)
    bar.pack(side="right", fill="y")
    return frame, tree


def fill(tree, rows):
    tree.delete(*tree.get_children())
    for row in rows:
        tree.insert("", "end", values=row)


class TaskbarClock:
    """Today's active time drawn over the left end of the taskbar."""

    def __init__(self, app):
        self.app = app
        self.enabled = app.settings["taskbar_clock"]  # the Settings tab turns it on and off
        light = light_theme()
        # Pixels in this color turn see-through. It is close to the taskbar's own color
        # so the anti-aliased edges of the text blend in.
        key = "#eeeeef" if light else "#1c1c1d"
        text = "#000000" if light else "#ffffff"

        self.win = tk.Toplevel(app.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-transparentcolor", key)
        self.win.configure(bg=key)
        self.label = tk.Label(self.win, bg=key, fg=text, font=("Segoe UI Semibold", 11), pady=0)
        self.label.pack()

        self.menu = tk.Menu(self.win, tearoff=False)
        self.menu.add_command(label="Open Screen Time", command=app.show)
        self.menu.add_separator()
        self.menu.add_command(label="Quit tracking", command=app.quit)
        for widget in (self.win, self.label):
            widget.bind("<Button-1>", lambda e: app.show())
            widget.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        self.place()

    def update(self, log):
        self.label.config(text=f"Active {short(log['active_seconds'])}")

    def place(self):
        rect = visible_taskbar()
        if rect is None or not self.enabled:
            self.win.withdraw()
        else:
            self.win.update_idletasks()
            h = self.win.winfo_reqheight()
            y = rect.top + max(0, (rect.bottom - rect.top - h) // 2)
            self.win.geometry(f"+{rect.left + CLOCK_OFFSET}+{y}")
            self.win.deiconify()
            user32.SetWindowPos(int(self.win.wm_frame(), 16), HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        self.win.after(RAISE_EVERY, self.place)


class DatePicker:
    """A month calendar that drops down under a button. Days with a log are bold, today is
    blue, the picked day is filled blue; future days can't be picked."""

    def __init__(self, app, button, selected, on_pick):
        self.app, self.button, self.selected, self.on_pick = app, button, selected, on_pick
        self.today = datetime.now().date()
        self.logged = logged_days()
        self.month = selected.replace(day=1)
        self.win = tk.Toplevel(app.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        border = tk.Frame(self.win, bg="#c8c8c8", padx=1, pady=1)
        border.pack()
        self.body = tk.Frame(border, bg="white", padx=8, pady=8)
        self.body.pack()
        self.draw()
        self.win.geometry(f"+{button.winfo_rootx()}+{button.winfo_rooty() + button.winfo_height() + 2}")
        self.win.bind("<Escape>", lambda e: self.close())
        self.win.bind("<FocusOut>", lambda e: self.win.after(100, self.close_if_unfocused))
        app.root.bind_all("<Button-1>", self.click_outside, add="+")
        self.win.focus_force()

    def draw(self):
        for widget in self.body.winfo_children():
            widget.destroy()
        head = tk.Frame(self.body, bg="white")
        head.grid(row=0, column=0, columnspan=7, sticky="ew", pady=(0, 6))
        self.nav(head, "‹", -1, True).pack(side="left")
        self.nav(head, "›", 1, self.month < self.today.replace(day=1)).pack(side="right")
        tk.Label(head, text=f"{self.month:%B %Y}", bg="white",
                 font=("Segoe UI Semibold", 10)).pack(side="left", expand=True)
        for col, name in enumerate(("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")):
            tk.Label(self.body, text=name, bg="white", fg="#888", font=("Segoe UI", 8)).grid(row=1, column=col)
        weeks = calendar.Calendar().monthdatescalendar(self.month.year, self.month.month)
        for row, week in enumerate(weeks, 2):
            for col, day in enumerate(week):
                if day.month == self.month.month:
                    self.day_button(day).grid(row=row, column=col, padx=1, pady=1)
        tk.Label(self.body, text="Bold days have data", bg="white", fg="#888",
                 font=("Segoe UI", 8)).grid(row=8, column=0, columnspan=7, pady=(6, 0))

    def nav(self, parent, text, months, enabled):
        return tk.Button(parent, text=text, width=2, relief="flat", bd=0, bg="white", activebackground="#e5e7eb",
                         font=("Segoe UI Semibold", 11), cursor="hand2" if enabled else "",
                         state="normal" if enabled else "disabled", command=lambda: self.step(months))

    def day_button(self, day):
        has_data = day.isoformat() in self.logged
        font = ("Segoe UI Semibold" if has_data else "Segoe UI", 9)
        bg, fg = "white", "#111" if has_data else "#999"
        if day == self.today:
            fg = TIMELINE_ON
        if day == self.selected:
            bg, fg = TIMELINE_ON, "white"
        future = day > self.today
        return tk.Button(self.body, text=str(day.day), width=3, relief="flat", bd=0, bg=bg, fg=fg, font=font,
                         activebackground="#e5e7eb", disabledforeground="#d4d4d4", cursor="" if future else "hand2",
                         state="disabled" if future else "normal", command=lambda: self.pick(day))

    def step(self, months):
        year, month = divmod(self.month.month - 1 + months, 12)
        self.month = date(self.month.year + year, month + 1, 1)
        self.draw()

    def pick(self, day):
        self.close()
        self.on_pick(day)

    def click_outside(self, event):
        inside = str(event.widget).startswith(str(self.win))
        if not inside and event.widget is not self.button:  # the button toggles it itself
            self.close()

    def close_if_unfocused(self):
        if self.app.date_picker is self:
            focus = self.win.focus_get()
            if focus is None or not str(focus).startswith(str(self.win)):
                self.close()

    def close(self):
        if self.app.date_picker is self:
            self.app.date_picker = None
        self.app.root.unbind_all("<Button-1>")
        self.win.destroy()


def qr_image(text, size):
    """A QR code of text as a Tk image about size pixels wide (segno makes the code; Tk draws it)."""
    code = segno.make(text, error="m", micro=False)
    border = 4  # the quiet zone scanners need
    rows = [[0] * border + list(row) + [0] * border for row in code.matrix]
    width = len(rows[0])
    rows = [[0] * width] * border + rows + [[0] * width] * border
    image = tk.PhotoImage(width=width, height=width)
    image.put(" ".join("{" + " ".join("#000000" if dark else "#ffffff" for dark in row) + "}" for row in rows))
    return image.zoom(max(2, size // width))


class PairDialog(tk.Toplevel):
    """The pairing QR code: this PC's name, address, port, one-time secret and certificate fingerprint."""

    def __init__(self, app, payload):
        super().__init__(app.root)
        self.app, self.done = app, False
        self.phone_before = peer.paired_phone()
        self.title("Pair a phone")
        self.resizable(False, False)
        self.transient(app.root)
        self.protocol("WM_DELETE_WINDOW", self.close)
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Scan this with Screen Time on your phone", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(frame, text=f"The phone must be on the same network. The code works once, for "
                              f"{peer.PAIRING_TTL // 60} minutes. Pairing replaces any phone paired before.",
                  style="Caption.TLabel", wraplength=int(300 * app.scale)).pack(anchor="w", pady=(4, 10))
        self.image = qr_image(json.dumps(payload, separators=(",", ":")), int(300 * app.scale))
        self.qr = ttk.Label(frame, image=self.image)
        self.qr.pack()
        fp = payload["cert_sha256"].upper()
        ttk.Label(frame, text=f"{payload['name']} · {', '.join(payload['addresses'])}:{payload['port']}\n"
                              f"Certificate {':'.join(fp[i:i + 2] for i in range(0, 16, 2))}…",
                  style="Caption.TLabel", justify="center").pack(pady=(10, 0))
        self.status = tk.StringVar(value="Waiting for the phone...")
        ttk.Label(frame, textvariable=self.status).pack(pady=(8, 0))
        self.close_button = ttk.Button(frame, text="Cancel", command=self.close)
        self.close_button.pack(pady=(10, 0))
        self.expiry = self.after(peer.PAIRING_TTL * 1000, self.expired)
        self.geometry(f"+{app.root.winfo_rootx() + 40}+{app.root.winfo_rooty() + 40}")
        self.focus_set()

    def expired(self):
        if not self.done:
            self.status.set("This code has expired. Close this window and pair again.")
            self.qr.configure(image="")

    def close(self):
        self.after_cancel(self.expiry)
        self.destroy()
        self.app.pairing_closed()


class App:
    def __init__(self, root, tracker):
        self.root = root
        self.tracker = tracker
        self.settings = load_settings()
        self.stop = threading.Event()
        self.ticks = 0

        root.title("Screen Time")
        root.geometry("560x720")
        root.minsize(440, 720)  # room for every Settings section
        root.protocol("WM_DELETE_WINDOW", root.withdraw)  # hide; the clock and tray icon stay
        if os.path.exists(ICON):
            root.iconbitmap(default=ICON)
            self.taskbar_icon = None
            root.bind("<Map>", self.set_taskbar_icon, add="+")
        style = ttk.Style()
        style.configure("Value.TLabel", font=("Segoe UI Semibold", 22))
        style.configure("Caption.TLabel", font=("Segoe UI", 9), foreground="#666")
        style.configure("Warning.TLabel", font=("Segoe UI", 9), foreground="#b45309")  # amber: something to fix
        style.configure("NoData.TLabel", font=("Segoe UI", 10), foreground="#888")
        style.configure("Heading.TLabel", font=("Segoe UI Semibold", 10))
        style.configure("Rank.TLabel", font=("Segoe UI Semibold", 16), foreground="#888")
        self.app_name_font = tkfont.Font(family="Segoe UI Semibold", size=10)
        self.timeline_font = tkfont.Font(family="Segoe UI", size=8)
        self.now_font = tkfont.Font(family="Segoe UI", size=9)  # the now-line's time, a bit bigger than the hours
        self.sync_font = tkfont.Font(family="Segoe UI", size=8)
        self.cloud = None  # the Sync buttons' icon, made with the first one
        style.configure("AppName.TLabel", font=self.app_name_font)
        self.scale = root.winfo_fpixels("1i") / 96  # display scaling, for canvas sizes in pixels
        # Rows tall enough for the app icons
        icon = small_icon_size()
        text = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        style.configure("Treeview", rowheight=max(icon, text) + 6)
        # Rows without the expand-arrow slot, which these flat tables don't use
        style.layout("Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [
            ("Treeitem.image", {"side": "left", "sticky": ""}),
            ("Treeitem.focus", {"side": "left", "sticky": "", "children": [
                ("Treeitem.text", {"side": "left", "sticky": ""})]})]})])
        self.icons = {}  # (.exe path, large) -> PhotoImage, or None if it has no icon
        self.no_icon = tk.PhotoImage(width=icon, height=icon)  # blank, keeps names aligned
        self.no_large_icon = tk.PhotoImage(width=large_icon_size(), height=large_icon_size())

        # Today's totals
        top = ttk.Frame(root, padding=(16, 12, 16, 4))
        top.pack(fill="x")
        self.active = tk.StringVar()
        self.uptime = tk.StringVar()
        for col, (caption, var) in enumerate((("Active today", self.active), ("PC up time", self.uptime))):
            ttk.Label(top, text=caption, style="Caption.TLabel").grid(row=0, column=col, sticky="w")
            ttk.Label(top, textvariable=var, style="Value.TLabel").grid(row=1, column=col, sticky="w", padx=(0, 32))

        tabs = self.tabs = ttk.Notebook(root)
        tabs.pack(fill="both", expand=True, padx=16, pady=8)

        # Report tab: when the PC was on today, the top 3 apps, and every app's time
        report = ttk.Frame(tabs, padding=12)
        tabs.add(report, text="Report")
        head = ttk.Frame(report)
        head.pack(fill="x")
        ttk.Label(head, text="Timeline", style="Heading.TLabel").pack(side="left")
        self.report_day = None  # the day the Report shows; None follows today
        self.past_log = None  # that day's saved log when it isn't today (None: no data)
        self.date_picker = None
        bg = style.lookup("TFrame", "background") or root.cget("bg")
        arrow = lambda text, days: tk.Button(head, text=text, command=lambda: self.step_day(days), relief="flat", bd=0,
                                             bg=bg, activebackground="#e5e7eb", fg="#374151",
                                             disabledforeground="#d1d5db", font=("Segoe UI", 9), padx=4, cursor="hand2")
        arrow("\u25c0", -1).pack(side="left", padx=(10, 2))  # the day before
        self.date_button = ttk.Button(head, command=self.toggle_date_picker)
        self.date_button.pack(side="left")
        self.next_day = arrow("\u25b6", 1)  # the day after; light and inactive when that day hasn't come yet
        self.next_day.pack(side="left", padx=(2, 0))
        self.timeline = tk.Canvas(report, height=int(140 * self.scale), highlightthickness=0,
                                  bg=style.lookup("TFrame", "background") or root.cget("bg"))
        self.timeline.pack(fill="x", pady=(6, 0))
        self.timeline.bind("<Configure>", lambda e: self.refresh_report())
        self.timeline.bind("<Motion>", self.hover_timeline)
        self.timeline.bind("<Enter>", self.hover_timeline)  # entering lands on a period without a <Motion>
        self.timeline.bind("<Leave>", lambda e: self.hover_timeline(None))
        self.periods = []  # (canvas item, start, end, title, color, hover color) of each period drawn
        # Under the row names: sync without going to the Connect tab
        self.sync_buttons = [SyncButton(self.timeline, self, self.sync_from_report)]
        self.syncing = False  # a sync started from a Sync button, whose result they wait for
        self.sync_reset = None  # pending switch of the buttons back to "Sync" after showing a result
        self.hover = None  # the last mouse event over the timeline, to keep the pop-up through redraws
        self.tip = None
        ttk.Separator(report).pack(fill="x", pady=10)
        ttk.Label(report, text="Most used apps", style="Heading.TLabel").pack(anchor="w")
        self.top_apps = ttk.Frame(report)
        self.top_apps.pack(fill="x", pady=(10, 0))
        self.top_apps.bind("<Configure>", lambda e: self.refresh_report())
        for col in range(3):
            self.top_apps.columnconfigure(col, weight=1, uniform="top")
        self.top_shown = None  # what the top apps row shows, to rebuild it only on changes
        ttk.Separator(report).pack(fill="x", pady=10)
        bar = ttk.Frame(report)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="Count by:").pack(side="left")
        self.kind = tk.StringVar(value="active")
        kind = ttk.Combobox(bar, textvariable=self.kind, values=KINDS, state="readonly", width=8)
        kind.pack(side="left", padx=6)
        kind.bind("<<ComboboxSelected>>", lambda e: self.refresh_report())
        self.by_monitor = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Split by monitor", variable=self.by_monitor,
                        command=self.refresh_apps).pack(side="left", padx=12)
        frame, self.apps = make_table(report, [("Time", 110, "e"), ("Share", 70, "e")], tree_column=("App", 260))
        self.apps.configure(height=4)  # asks for little; grows into whatever height is left
        frame.pack(fill="both", expand=True)
        self.no_apps = tk.Label(self.apps, text="No data", fg="#888", font=("Segoe UI", 10),
                                bg=style.lookup("Treeview", "fieldbackground") or "white")

        # History tab: one row per day
        history = ttk.Frame(tabs, padding=8)
        tabs.add(history, text="History")
        frame, self.days = make_table(history, [("Date", 100, "w"), ("PC on", 100, "e"),
                                                ("Active", 100, "e"), ("Top app", 160, "w")])
        frame.pack(fill="both", expand=True)
        self.summary = tk.StringVar()
        ttk.Label(history, textvariable=self.summary, style="Caption.TLabel").pack(anchor="w", pady=(6, 0))
        # Fresh data the moment a tab is picked; after_idle, so it is laid out to draw into
        tabs.bind("<<NotebookTabChanged>>", lambda e: root.after_idle(self.refresh_tab))

        # Settings tab
        settings = ttk.Frame(tabs, padding=12)
        tabs.add(settings, text="Settings")
        self.autostart = tk.BooleanVar(value=autostart_enabled())
        if self.autostart.get():  # point it at whichever copy runs now, the script or the .exe
            try:
                set_autostart(True)
            except OSError:
                pass
        ttk.Label(settings, text="Startup", style="Heading.TLabel").pack(anchor="w")
        ttk.Checkbutton(settings, text="Start with Windows", variable=self.autostart,
                        command=self.toggle_autostart).pack(anchor="w", pady=(6, 0))
        note(settings, "Opens at login in the background.", padx=(21, 0))
        ttk.Separator(settings).pack(fill="x", pady=14)
        ttk.Label(settings, text="Taskbar", style="Heading.TLabel").pack(anchor="w")
        self.show_clock = tk.BooleanVar(value=self.settings["taskbar_clock"])
        ttk.Checkbutton(settings, text="Show active time on the taskbar", variable=self.show_clock,
                        command=self.toggle_clock).pack(anchor="w", pady=(6, 0))
        note(settings, "In its bottom-left corner. The tray icon stays either way.", padx=(21, 0))
        ttk.Separator(settings).pack(fill="x", pady=14)
        ttk.Label(settings, text="Logs", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(settings, text=LOG_DIR, style="Caption.TLabel").pack(anchor="w", pady=(4, 8))
        ttk.Button(settings, text="Open logs folder", command=self.open_logs).pack(anchor="w")
        ttk.Separator(settings).pack(fill="x", pady=14)
        ttk.Label(settings, text="App", style="Heading.TLabel").pack(anchor="w")
        note(settings, "Saves today's log and starts Screen Time again, e.g. if something looks stuck.", pady=(4, 8))
        ttk.Button(settings, text="Restart Screen Time", command=self.restart).pack(anchor="w")

        # Connect tab: connects to your own server, which the logs are synced with
        sync_tab = self.connect_tab = ttk.Frame(tabs, padding=12)
        tabs.add(sync_tab, text="Connect")
        choices = ttk.Frame(sync_tab)
        choices.pack(fill="x")
        ttk.Label(choices, text="Sync with", style="Heading.TLabel").pack(side="left", padx=(0, 14))
        self.sync_mode = tk.StringVar(value=sync.get_mode())
        self.mode_buttons = [ttk.Radiobutton(choices, text=label, value=value, variable=self.sync_mode,
                                             command=self.change_sync_mode)
                             for value, label in (("server", "Server"), ("direct", "Phone directly"), ("both", "Both"))]
        for button in self.mode_buttons:  # Phone directly turns server sync off; the status below says so
            button.pack(side="left", padx=(0, 12))
        phone = ttk.Frame(sync_tab)
        phone.pack(fill="x", pady=(6, 0))
        self.allow_phone = tk.BooleanVar(value=peer.load_config()["enabled"])
        self.allow_phone_button = ttk.Checkbutton(phone, text="Allow phone sync", variable=self.allow_phone,
                                                  command=self.toggle_phone_sync)
        self.allow_phone_button.pack(side="left")
        self.phone_status = tk.StringVar(value="Off")
        phone_status = ttk.Label(phone, textvariable=self.phone_status, style="Caption.TLabel")
        phone_status.pack(side="left", fill="x", expand=True, padx=(10, 0))
        phone_status.bind("<Configure>", lambda e: phone_status.configure(wraplength=e.width))
        # Windows Firewall blocking the listener, checked whenever it starts listening (empty when it's fine)
        self.firewall_text = tk.StringVar()
        self.firewall_check = None  # the check whose answer is still wanted
        firewall_label = ttk.Label(sync_tab, textvariable=self.firewall_text, style="Warning.TLabel")
        firewall_label.pack(fill="x", pady=(4, 0))
        firewall_label.bind("<Configure>", lambda e: firewall_label.configure(wraplength=e.width))
        pairing = ttk.Frame(sync_tab)
        pairing.pack(fill="x", pady=(6, 0))
        self.pair_button = ttk.Button(pairing, text="Pair a phone…", command=self.pair_phone)
        self.pair_button.pack(side="left")
        self.unpair_button = ttk.Button(pairing, text="Remove pairing", command=self.remove_pairing)
        self.unpair_button.pack(side="left", padx=(8, 0))
        self.paired_text = tk.StringVar()
        paired = ttk.Label(pairing, textvariable=self.paired_text, style="Caption.TLabel")
        paired.pack(side="left", fill="x", expand=True, padx=(10, 0))
        paired.bind("<Configure>", lambda e: paired.configure(wraplength=e.width))
        self.pair_dialog = None
        self.show_pairing()
        ttk.Separator(sync_tab).pack(fill="x", pady=10)
        ttk.Label(sync_tab, text="Server sync", style="Heading.TLabel").pack(anchor="w", pady=(0, 4))
        self.sync_status = tk.StringVar()
        status = ttk.Label(sync_tab, textvariable=self.sync_status, style="Caption.TLabel")
        status.pack(fill="x")
        status.bind("<Configure>", lambda e: status.configure(wraplength=e.width))
        self.sync_form = ttk.Frame(sync_tab)
        self.sync_fields = {}
        for row, (label, key, default, secret) in enumerate((
                ("Server", "server", "", False), ("Name", "name", "", False),
                ("Password", "password", "", True), ("Device name", "device", sync.default_device_name(), False))):
            ttk.Label(self.sync_form, text=label).grid(row=row, column=0, sticky="w", pady=3)
            self.sync_fields[key] = tk.StringVar(value=default)
            ttk.Entry(self.sync_form, textvariable=self.sync_fields[key], show="•" if secret else "") \
                .grid(row=row, column=1, sticky="ew", padx=(10, 0), pady=3)
        self.sync_form.columnconfigure(1, weight=1)
        self.sync_error = tk.StringVar()
        error = ttk.Label(self.sync_form, textvariable=self.sync_error, foreground="#dc2626")
        error.grid(row=4, column=1, sticky="ew", padx=(10, 0), pady=(8, 0))  # beside the button, to save room
        error.bind("<Configure>", lambda e: error.configure(wraplength=e.width))
        self.connect_button = ttk.Button(self.sync_form, text="Connect", command=self.connect_sync)
        self.connect_button.grid(row=4, column=0, sticky="nw", pady=(8, 0))
        self.sync_connected = ttk.Frame(sync_tab)
        self.sync_info = tk.StringVar()
        info = ttk.Label(self.sync_connected, textvariable=self.sync_info)
        info.pack(fill="x")
        info.bind("<Configure>", lambda e: info.configure(wraplength=e.width))
        buttons = ttk.Frame(self.sync_connected)
        buttons.pack(anchor="w", pady=(8, 0))
        self.sync_buttons.append(SyncButton(buttons, self, self.start_sync))
        self.sync_buttons[-1].frame.pack(side="left")
        ttk.Button(buttons, text="Disconnect", command=self.disconnect_sync).pack(side="left", padx=(8, 0))
        self.show_sync_state()
        # Packed from the bottom, so they stay put while the form above changes to the connected panel
        backup = ttk.Frame(sync_tab)
        backup.pack(side="bottom", fill="x")
        ttk.Separator(sync_tab).pack(side="bottom", fill="x", pady=(8, 10))
        ttk.Label(backup, text="Backup", style="Heading.TLabel").pack(anchor="w")
        note(backup, "This PC's times and the pulled phone and sleep periods, as a JSON file. Importing one "
                     "merges it: the newer copy of each period wins.", pady=(2, 6))
        row = ttk.Frame(backup)
        row.pack(fill="x")
        self.backup_buttons = [ttk.Button(row, text="Export backup…", command=self.export_backup),
                               ttk.Button(row, text="Import backup…", command=self.import_backup)]
        self.backup_buttons[0].pack(side="left", anchor="n")
        self.backup_buttons[1].pack(side="left", anchor="n", padx=(8, 0))
        self.backup_status = tk.StringVar()
        status = ttk.Label(row, textvariable=self.backup_status, style="Caption.TLabel")
        status.pack(side="left", fill="x", expand=True, padx=(10, 0))
        status.bind("<Configure>", lambda e: status.configure(wraplength=e.width))

        # Footer
        foot = ttk.Frame(root, padding=(16, 0, 16, 12))
        foot.pack(fill="x", side="bottom", before=tabs)  # space kept first, so the tabs can't squeeze it out
        colored_button(foot, "Done", "#16a34a", "#15803d", root.withdraw).pack(side="right")
        colored_button(foot, "Quit tracking", "#dc2626", "#b91c1c", self.quit).pack(side="right", padx=(0, 8))

        self.clock = TaskbarClock(self)
        # The tray icon calls back on its own thread; Tk runs the calls from this queue.
        self.calls = queue.Queue()
        self.tray = TrayIcon("Screen Time", ICON, lambda: self.calls.put(self.show), [
            ("Open Screen Time", lambda: self.calls.put(self.show)),
            None,
            ("Quit tracking", lambda: self.calls.put(self.quit)),
        ])
        self.syncer = sync.SyncWorker(tracker, lambda text: self.calls.put(lambda: self.sync_status_changed(text)))
        self.syncer.start()
        # The phone connects to this PC while "Allow phone sync" is on
        self.peer = peer.PeerListener(
            lambda: (self.tracker.day, self.tracker.snapshot()),
            on_status=lambda text: self.calls.put(lambda: self.phone_status_changed(text)),
            on_change=lambda: self.calls.put(self.phone_pushed),
            on_paired=lambda: self.calls.put(self.show_pairing),
            on_synced=lambda: self.calls.put(self.show_pairing))
        if self.allow_phone.get():
            self.peer.start()
        threading.Thread(target=self.track, daemon=True).start()
        self.refresh()
        self.run_calls()

    def track(self):
        while not self.stop.wait(TICK):
            self.tracker.tick()

    def refresh(self):
        log = self.tracker.snapshot()
        self.clock.update(log)
        self.tray.set_tooltip(f"Screen Time\nWork {short(log['active_seconds'])}\n"
                              f"PC on {short(log['total_seconds'])}")
        if self.root.state() == "normal":  # skip the window's work while it is hidden
            self.active.set(short(log["active_seconds"]))
            self.uptime.set(short(uptime_seconds()))  # since Windows started; restarts reset it
            if self.ticks % TABLE_REFRESH == 0:
                self.refresh_tab(log, history=self.ticks % HISTORY_REFRESH == 0)
            self.ticks += 1
        self.root.after(1000, self.refresh)

    def refresh_tab(self, log=None, history=True):
        """Refresh the tab that is showing; history=False skips History (it reads every log)."""
        log = log or self.tracker.snapshot()
        tab = self.tabs.tab(self.tabs.select(), "text")
        if tab == "Report":
            self.refresh_report(log)
        elif tab == "History" and history:
            self.refresh_history(log)

    def run_calls(self):
        while not self.calls.empty():
            self.calls.get()()
        self.root.after(100, self.run_calls)

    def set_taskbar_icon(self, event):
        """Gives the taskbar button the icon at the exact size it draws (30 px at 125%).

        Tk only hands Windows the 40 px icon at 125%, and the taskbar shrinks that to 30 px,
        which blurs the bars. The .ico has a frame for each scaling; load the matching one.
        """
        if event.widget is not self.root:  # <Map> also fires for every child widget
            return
        hwnd = int(self.root.wm_frame(), 16)
        if not self.taskbar_icon:
            size = TASKBAR_ICON * (user32.GetDpiForWindow(hwnd) or 96) // 96
            self.taskbar_icon = user32.LoadImageW(None, ICON, IMAGE_ICON, size, size, LR_LOADFROMFILE)
        if self.taskbar_icon:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, self.taskbar_icon)

    def show(self):
        if self.root.state() != "normal":  # reopening a hidden or minimized window: start on Report
            self.tabs.select(0)
        self.ticks = 0  # refresh everything right away
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def shown_day(self):
        return self.report_day or self.tracker.day

    def shown_log(self, live=None):
        """The log the Report shows: today's live one, or the picked day's (None if it has no data)."""
        if self.report_day is None or self.report_day == self.tracker.day:
            return live or self.tracker.snapshot()
        return self.past_log

    def toggle_date_picker(self):
        if self.date_picker:
            self.date_picker.close()
        else:
            self.date_picker = DatePicker(self, self.date_button, self.shown_day(), self.pick_day)

    def step_day(self, days):
        if self.date_picker:  # it would still show the old day
            self.date_picker.close()
        day = self.shown_day() + timedelta(days=days)
        if day <= self.tracker.day:  # days that haven't come yet aren't shown
            self.pick_day(day)

    def pick_day(self, day):
        self.report_day = None if day == self.tracker.day else day
        self.past_log = read_log(day) if self.report_day else None
        self.refresh_report()

    def refresh_apps(self, log=None):
        log = self.shown_log(log) or {}
        totals = {}
        for key, secs in log.get(self.kind.get(), {}).items():
            app, monitor = key.split("|")
            name = f"{app}  ({monitor})" if self.by_monitor.get() else app
            totals[name] = totals.get(name, 0.0) + secs
        whole = sum(totals.values()) or 1
        self.apps.delete(*self.apps.get_children())
        if totals:
            self.no_apps.place_forget()
        else:
            self.no_apps.place(relx=0.5, rely=0.5, anchor="center")
        for name, secs in sorted(totals.items(), key=lambda kv: -kv[1]):
            image = self.app_icon(self.app_path(name.split("  (")[0], log))
            self.apps.insert("", "end", text=f"  {name}", image=image,  # spaces: gap after the icon
                             values=(duration(secs), f"{secs / whole:.0%}"))

    def app_path(self, app, log):
        """The app's .exe from the shown log, else from this run's tracking (older logs have none)."""
        return log.get("paths", {}).get(app) or APP_PATHS.get(app)

    def app_icon(self, path, large=False):
        """The app's icon, read from its .exe once and then cached; a blank one if it has none."""
        blank = self.no_large_icon if large else self.no_icon
        if not path:
            return blank
        if (path, large) not in self.icons:
            png = icon_png(path, large)
            self.icons[path, large] = tk.PhotoImage(data=base64.b64encode(png)) if png else None
        return self.icons[path, large] or blank

    def refresh_report(self, log=None):
        day = self.shown_day()
        log = self.shown_log(log) or {}
        self.date_button.config(text=f"{day:%d %b %Y}  ▾")
        self.next_day.config(state="normal" if day < self.tracker.day else "disabled",
                             cursor="hand2" if day < self.tracker.day else "")
        today = day == self.tracker.day
        # Past days without PC-on times (no log, or logged before they were recorded) and without
        # phone or sleep data from the server: no data
        self.draw_timeline(self.timeline_rows(day, log, today), today)
        self.refresh_apps(log)

        # Top 3 by the same "Count by" measure as the list below, one entry per app
        apps = {}
        for key, secs in log.get(self.kind.get(), {}).items():
            app = key.split("|")[0]
            apps[app] = apps.get(app, 0.0) + secs
        top = sorted(apps.items(), key=lambda kv: -kv[1])[:3]
        column = self.top_apps.winfo_width() // 3
        shown = [(app, int(secs) // 60, self.app_path(app, log)) for app, secs in top] + [column]
        if shown == self.top_shown:
            return
        self.top_shown = shown
        for widget in self.top_apps.winfo_children():
            widget.destroy()
        if not top:
            ttk.Label(self.top_apps, text="No data", style="NoData.TLabel").grid(row=0, column=0, columnspan=3, pady=8)
        gap = int(8 * self.scale)
        for col, (app, minutes, path) in enumerate(shown[:-1]):
            cell = ttk.Frame(self.top_apps)
            cell.grid(row=0, column=col, sticky="w")
            rank = ttk.Label(cell, text=str(col + 1), style="Rank.TLabel")
            rank.grid(row=0, column=0, rowspan=2, padx=(0, gap))
            icon = ttk.Label(cell, image=self.app_icon(path, large=True))
            icon.grid(row=0, column=1, rowspan=2, padx=(0, gap))
            # The name gets what is left of the column after the rank, the icon and the gaps
            room = column - rank.winfo_reqwidth() - icon.winfo_reqwidth() - 3 * gap
            name = fit(app[:-4] if app.lower().endswith(".exe") else app, self.app_name_font, max(room, 40))
            ttk.Label(cell, text=name, style="AppName.TLabel").grid(row=0, column=2, sticky="sw")
            ttk.Label(cell, text=duration(minutes * 60), style="Caption.TLabel").grid(row=1, column=2, sticky="nw")

    def timeline_rows(self, day, log, today):
        """What the timeline shows: None if there is no data at all, else [(name, summary, hint, parts)].

        parts are (title, color, hover color, spans) drawn in order; hint is for a row with no data.
        """
        on = log.get("on_timeline") if today or log.get("on_timeline") else None
        remote = sync.load_remote()
        phone, sleep = day_spans(remote, day, "phone_active"), day_spans(remote, day, "sleep")
        if on is None and not phone and not sleep:
            return None
        hint = "" if sync.load_state()["server"] else "Not connected (Connect tab)"
        return [("Laptop", f"{short(total(on))} on" if on is not None else "", "",
                 [("Laptop · PC on", TIMELINE_ON, TIMELINE_HOVER, on or [])]),
                ("Phone", short(total(phone)) if phone else "", hint,
                 [("Phone · Active", TIMELINE_PHONE, TIMELINE_PHONE_HOVER, phone)]),
                ("Sleep", short(total(sleep)) if sleep else "", hint,
                 [("Sleep", TIMELINE_SLEEP, TIMELINE_SLEEP_HOVER, sleep)])]

    def draw_timeline(self, rows, today=True):
        """Laptop, phone and sleep as 00:00-23:59 bars divided by hour, colored in where each was
        on; "No data" if rows is None."""
        c, s = self.timeline, self.scale
        c.delete("all")
        self.periods = []
        width = c.winfo_width()
        if width < 50:  # not laid out yet
            return
        left, right, top, row_h, pitch = int(78 * s), width - 1, 20 * s, 26 * s, 34 * s  # room on top for the now-line's time
        bottom = top + 2 * pitch + row_h  # under the third row (Sleep), where the Sync button goes
        c.create_window(2, bottom + 4 * s, window=self.sync_buttons[0].frame, anchor="nw")
        if rows is None:
            c.create_text(width / 2, c.winfo_height() / 2, text="No data", fill="#888", font=("Segoe UI", 10))
            self.hover_timeline(None)
            return
        bottom = top + (len(rows) - 1) * pitch + row_h
        x = lambda sec: left + (right - left) * sec / 86400
        bg = c.cget("bg")
        for i, (name, summary, hint, parts) in enumerate(rows):
            y1 = top + i * pitch
            y2 = y1 + row_h
            mid = (y1 + y2) / 2
            c.create_text(2, mid, text=name, anchor="sw", fill="#111", font=self.app_name_font)
            c.create_text(2, mid + 1, text=summary, anchor="nw", fill="#666", font=self.timeline_font)
            c.create_polygon(pill_points(left, right, y1, y2, left, right), fill=TIMELINE_TRACK, outline="")
            for h in range(1, 24):  # thin gaps divide the gray bar into hours; the colored spans cover them
                xh = x(h * 3600)
                ya, yb = pill_edges(xh, y1, y2, left, right)
                c.create_line(xh, ya, xh, yb, fill=bg)
            for title, color, hover, spans in parts:
                for start, end in spans:
                    points = pill_points(x(start), max(x(end), x(start) + 1), y1, y2, left, right)
                    item = c.create_polygon(points, fill=color, outline="")
                    self.periods.append((item, start, end, title, color, hover))
            if hint and not any(spans for *_, spans in parts):
                c.create_text((left + right) / 2, mid, text=hint, fill="#888", font=self.timeline_font)
        # A label every 3 hours, or every 6 when the bar is too narrow for them to fit side by side
        label_width = self.timeline_font.measure("00:00")
        step = 3 if x(3 * 3600) - x(0) >= 1.5 * label_width + 6 * s else 6
        for h in range(25):
            xh = x(h * 3600)
            c.create_line(xh, bottom, xh, bottom + (6 if h % step == 0 else 3) * s, fill="#999")
            if h % step == 0:
                label, anchor = ("23:59", "ne") if h == 24 else (f"{h:02d}:00", "nw" if h == 0 else "n")
                c.create_text(xh, bottom + 8 * s, text=label, anchor=anchor, fill="#666", font=self.timeline_font)
        if today:
            now = datetime.now()
            xn = x(now.hour * 3600 + now.minute * 60 + now.second)
            w = max(3, round(3 * s))  # any thinner and the round ends can't show
            ya, yb = round(top - 3 * s), round(bottom + 3 * s)
            c.create_image(round(xn - w / 2), ya, image=self.now_line_image(w, yb - ya, bg), anchor="nw")
            # The time on top of the line, centered on it but kept inside the canvas near the edges
            label = now.strftime("%H:%M")
            half = self.now_font.measure(label) / 2
            xl = min(max(xn, half + 1), width - half - 1)
            c.create_text(xl, top - 3 * s, text=label, anchor="s", fill="#111", font=self.now_font)
        self.hover_timeline(self.hover)  # a redraw keeps the period under the mouse highlighted

    def now_line_image(self, w, length, bg):
        """The now-line as a w x length image with fully round ends. Tk draws round line caps this thin as
        squares, so the ends are drawn here, their edge pixels blended into bg (the ends sit off the bars)."""
        key = (w, length, bg)
        if getattr(self, "now_line", (None,))[0] != key:
            line = [int(TIMELINE_NOW[i:i + 2], 16) for i in (1, 3, 5)]
            back = [v >> 8 for v in self.timeline.winfo_rgb(bg)]
            r, n = w / 2, 4  # radius, and n x n samples per pixel for the coverage
            rows = []
            for py in range(length):
                row = []
                for px in range(w):
                    hits = 0
                    for i in range(n):
                        for j in range(n):
                            sx, sy = px + (i + .5) / n, py + (j + .5) / n
                            cy = min(max(sy, r), length - r)  # nearest point on the line's middle
                            hits += (sx - r) ** 2 + (sy - cy) ** 2 <= r * r
                    k = hits / (n * n)
                    row.append("#%02x%02x%02x" % tuple(round(a * k + b * (1 - k)) for a, b in zip(line, back)))
                rows.append("{" + " ".join(row) + "}")
            image = tk.PhotoImage(width=w, height=length)
            image.put(" ".join(rows))
            self.now_line = (key, image)  # kept, or Tk drops the image
        return self.now_line[1]

    def hover_timeline(self, event):
        """Highlight the period under the mouse and show its times in a pop-up."""
        self.hover = event
        c = self.timeline
        found = None
        if event is not None:
            slack = 2 * self.scale  # thin periods are hard to hit exactly
            for period in reversed(self.periods):  # the last one drawn is on top
                x1, y1, x2, y2 = c.bbox(period[0])
                if x1 - slack <= event.x <= x2 + slack and y1 - slack <= event.y <= y2 + slack:
                    found = period
                    break
        for period in self.periods:
            c.itemconfigure(period[0], fill=period[5] if period is found else period[4])
        if not found:
            if self.tip:
                self.tip.destroy()
                self.tip = None
            return
        _, start, end, title, _, _ = found
        hm = lambda sec: f"{int(sec) // 3600:02d}:{int(sec) % 3600 // 60:02d}"
        lines = [f"{hm(start)} – {hm(end)}", title, short(end - start)]
        if not self.tip:
            self.tip = tk.Toplevel(self.root)
            self.tip.overrideredirect(True)
            self.tip.attributes("-topmost", True)
            box = tk.Frame(self.tip, bg="#111827", padx=8, pady=5)
            box.pack()
            def label(row, column, fg="white", font=("Segoe UI", 9), **grid):
                widget = tk.Label(box, bg="#111827", fg=fg, font=font, padx=0, pady=0)
                widget.grid(row=row, column=column, **grid)
                return widget
            # The time range on top, then the name on the left and its value on the right
            self.tip_labels = [label(0, 0, font=("Segoe UI Semibold", 9), columnspan=2, sticky="w"),
                               label(1, 0, fg="#9ca3af", sticky="w", padx=(0, 12)), label(1, 1, sticky="e")]
        for widget, text in zip(self.tip_labels, lines):
            widget.configure(text=text)
        self.tip.geometry(f"+{event.x_root + 14}+{event.y_root + 16}")

    def refresh_history(self, log=None):
        today = log or self.tracker.snapshot()
        logs = dict(read_logs())
        logs[self.tracker.day.isoformat()] = today  # the live numbers, not the last save
        rows, on_sum, active_sum = [], 0.0, 0.0
        for day in sorted(logs, reverse=True):
            on, active, top = summarize(logs[day])
            on_sum += on
            active_sum += active
            rows.append((day, short(on), short(active), top))
        fill(self.days, rows)
        n = len(rows)
        self.summary.set(f"{n} day{'s' if n != 1 else ''} · average PC on {short(on_sum / n)}, "
                         f"active {short(active_sum / n)}")

    def toggle_autostart(self):
        try:
            set_autostart(self.autostart.get())
        except OSError as e:
            self.autostart.set(not self.autostart.get())
            messagebox.showerror("Screen Time", f"Couldn't change the startup setting:\n{e}")

    def toggle_clock(self):
        self.settings["taskbar_clock"] = self.clock.enabled = self.show_clock.get()
        if not self.clock.enabled:
            self.clock.win.withdraw()  # turning it on shows it at its next placement, within RAISE_EVERY
        try:
            save_settings(self.settings)
        except OSError as e:
            messagebox.showerror("Screen Time", f"Couldn't save the setting:\n{e}")

    def show_sync_state(self):
        """Show the connected panel or the connect form, whichever applies."""
        state = sync.load_state()
        if state["server"] and sync.load_token():
            self.sync_form.pack_forget()
            self.sync_info.set(f"Connected to {state['server']} as \"{state['device_name']}\".")
            self.sync_connected.pack(fill="x", pady=(12, 0))
            self.sync_status.set("Waiting for the first sync...")
        else:
            self.sync_connected.pack_forget()
            self.sync_form.pack(fill="x", pady=(12, 0))
            self.sync_status.set("Not connected")

    def connect_sync(self):
        fields = {key: var.get() for key, var in self.sync_fields.items()}
        self.sync_error.set("")
        self.sync_status.set("Connecting...")
        self.connect_button.state(["disabled"])

        def work():  # off the Tk thread: it waits for the server
            try:
                sync.connect(fields["server"], fields["name"], fields["password"], fields["device"])
                error = None
            except (sync.SyncError, OSError) as e:
                error = str(e)
            self.calls.put(lambda: self.connect_done(error))
        threading.Thread(target=work, daemon=True).start()

    def connect_done(self, error):
        self.connect_button.state(["!disabled"])
        if error:
            self.sync_error.set(error)
            self.sync_status.set("Not connected")
            return
        self.sync_fields["password"].set("")
        self.show_sync_state()
        self.start_sync()
        self.sync_status.set("Connected. Syncing...")

    def sync_from_report(self):
        """The Report tab's Sync button: sync now, or open the Connect tab when the sync mode needs a
        server and none is connected."""
        state = sync.load_state()
        if sync.uses_server(state["mode"]) and not (state["server"] and sync.load_token()):
            self.tabs.select(self.connect_tab)
            return
        self.start_sync()

    def show_sync_buttons(self, state):
        for button in self.sync_buttons:
            button.show(state)

    def start_sync(self):
        """Sync now; every Sync button shows "Syncing…" until the result comes."""
        if self.sync_reset:
            self.root.after_cancel(self.sync_reset)
            self.sync_reset = None
        self.syncing = True
        self.show_sync_buttons("syncing")
        self.sync_status.set("Syncing...")
        self.syncer.sync_now()

    def sync_status_changed(self, text):
        self.sync_status.set(text)
        if self.syncing:  # the Sync buttons are waiting for this
            self.syncing = False
            if text == sync.DIRECT_MODE_STATUS:  # nothing to sync from here: not a failure
                result = "direct"
            else:
                result = "done" if text.startswith("Synced") else "failed"
            self.show_sync_buttons(result)
            self.sync_reset = self.root.after(3000, lambda: self.show_sync_buttons("idle"))
        self.ticks = 0  # redraw the timeline soon, in case the sync brought phone or sleep data

    def disconnect_sync(self):
        if messagebox.askyesno("Screen Time", "Disconnect from the server?\n\nWhat was already sent stays "
                                              "there, and this PC stops sending."):
            sync.disconnect()
            self.show_sync_state()
            self.ticks = 0  # the pulled phone and sleep data is gone; redraw the timeline soon

    def change_sync_mode(self):
        """Save the chosen sync mode and sync with it right away."""
        mode = self.sync_mode.get()

        def work():  # off the Tk thread: it waits for a sync that is running
            try:
                sync.set_mode(mode)
            except OSError as e:
                self.calls.put(lambda: messagebox.showerror("Screen Time", f"Couldn't save the sync mode:\n{e}"))
                return
            self.calls.put(self.start_sync)
        threading.Thread(target=work, daemon=True).start()

    def toggle_phone_sync(self):
        """Save "Allow phone sync" and start or stop the listener, off the Tk thread (stopping waits
        for it, and the first start makes the certificate)."""
        enabled = self.allow_phone.get()
        self.allow_phone_button.state(["disabled"])
        self.phone_status.set("Starting..." if enabled else "Stopping...")

        def work():
            try:
                peer.set_enabled(enabled)
                if enabled:
                    self.peer.start()
                else:
                    self.peer.stop()
                    self.calls.put(lambda: self.phone_status.set("Off"))
            except OSError as e:
                self.calls.put(lambda: (self.allow_phone.set(not enabled), self.phone_status.set(
                    f"Couldn't save the setting: {e}")))
            self.calls.put(lambda: self.allow_phone_button.state(["!disabled"]))
        threading.Thread(target=work, daemon=True).start()

    def phone_status_changed(self, text):
        """The listener's state; once it listens, check in the background whether Windows Firewall lets the phone in."""
        self.phone_status.set(text)
        address, responder = self.peer.address, self.peer.responder
        if not (text.startswith("Listening on") and address):
            self.firewall_check = None
            self.firewall_text.set("")
            return
        token = self.firewall_check = object()
        discovery = responder.address[1] if responder else peer.DISCOVERY_PORT

        def work():  # PowerShell takes a few seconds
            verdict, message = firewall.check(address[0], address[1], discovery)
            self.calls.put(lambda: self.firewall_text.set(message) if self.firewall_check is token else None)
        threading.Thread(target=work, daemon=True).start()

    def phone_pushed(self):
        self.ticks = 0  # the phone sent periods: redraw the timeline soon

    def show_pairing(self):
        """The paired phone under "Allow phone sync", and in the pairing window once the phone paired."""
        phone = peer.paired_phone()
        self.paired_text.set(f"Paired with {phone['name']}. {last_synced_text(phone.get('last_sync'))}"
                             if phone else "No phone paired")
        self.unpair_button.state(["!disabled"] if phone else ["disabled"])
        dialog = self.pair_dialog
        if dialog and phone and phone != dialog.phone_before and not dialog.done:
            dialog.done = True
            dialog.status.set(f"Paired with {phone['name']}. You can close this window.")
            dialog.qr.configure(image="")  # the code is spent; don't leave it on screen
            dialog.close_button.configure(text="Done")

    def pair_phone(self):
        """Allow phone sync if it's off, open a pairing and show its QR code."""
        if self.pair_dialog:
            self.pair_dialog.lift()
            return
        self.pair_button.state(["disabled"])
        if not self.allow_phone.get():
            self.allow_phone.set(True)
            self.phone_status.set("Starting...")

        def work():  # off the Tk thread: starting the listener can take a moment the first time
            error, payload = None, None
            try:
                peer.set_enabled(True)
                self.peer.start()
                self.peer.ready.wait(10)
                address = self.peer.address
                if not address:
                    error = "This PC isn't on a private network right now, so the phone can't reach it."
                else:
                    payload = peer.start_pairing([address[0]], address[1])
            except OSError as e:
                error = f"Couldn't start pairing:\n{e}"
            self.calls.put(lambda: self.pairing_ready(payload, error))
        threading.Thread(target=work, daemon=True).start()

    def pairing_ready(self, payload, error):
        self.pair_button.state(["!disabled"])
        if error:
            messagebox.showerror("Screen Time", error)
            return
        self.pair_dialog = PairDialog(self, payload)

    def pairing_closed(self):
        dialog, self.pair_dialog = self.pair_dialog, None
        if dialog and not dialog.done:
            threading.Thread(target=peer.cancel_pairing, daemon=True).start()  # the code stops working
        self.show_pairing()

    def remove_pairing(self):
        phone = peer.paired_phone()
        if phone and messagebox.askyesno(
                "Screen Time", f"Remove the pairing with {phone['name']}?\n\nIt can't sync with this PC "
                               "until you pair it again."):
            peer.remove_pairing()
            self.show_pairing()

    def run_backup(self, start_text, work, done):
        """Run a backup export or import off the Tk thread (it waits for a running sync), then done(result)."""
        for button in self.backup_buttons:
            button.state(["disabled"])
        self.backup_status.set(start_text)

        def run():
            try:
                result, error = work(), None
            except (sync.BackupError, OSError) as e:
                result, error = None, str(e)
            self.calls.put(lambda: finish(result, error))

        def finish(result, error):
            for button in self.backup_buttons:
                button.state(["!disabled"])
            if error:
                self.backup_status.set("")
                messagebox.showerror("Screen Time", error)
            else:
                self.backup_status.set(done(result))
        threading.Thread(target=run, daemon=True).start()

    def export_backup(self):
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Export backup", defaultextension=".json",
            initialfile=f"screentime-backup-{date.today().isoformat()}.json",
            filetypes=[("Screen Time backup", "*.json"), ("All files", "*.*")])
        if not path:
            return
        day, log = self.tracker.day, self.tracker.snapshot()
        self.run_backup("Exporting...", lambda: sync.export_backup(path, day, log),
                        lambda r: f"Saved {r[0] + r[1]} rows to {os.path.basename(path)}.")  # short: it fits beside the buttons

    def import_backup(self):
        path = filedialog.askopenfilename(parent=self.root, title="Import backup",
                                          filetypes=[("Screen Time backup", "*.json"), ("All files", "*.*")])
        if not path:
            return

        def done(result):
            self.ticks = 0  # redraw the timeline and tables soon with what came in
            if result["skipped"]:  # unusual enough to say out loud
                messagebox.showwarning("Screen Time", f"{result['skipped']} rows of the backup look like this PC's "
                                                      "but don't match it, so they were skipped.")
            days = len(result["days"])
            return f"Imported: {result['changed']} phone/sleep periods, {days} {'day' if days == 1 else 'days'} " \
                   f"of this PC."  # short: it fits beside the buttons
        self.run_backup("Importing...", lambda: sync.import_backup(path, self.tracker.day), done)

    def open_logs(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        subprocess.Popen(["explorer", LOG_DIR])

    def restart(self):
        """Start a new copy, then save and quit like --quit, so no time is lost."""
        # Make a PyInstaller .exe unpack itself afresh instead of reusing this copy's files,
        # which are deleted when this copy exits.
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
        # Not PROJECT_DIR for the .exe: that is this copy's unpack folder, and a new copy working
        # in it keeps the old launcher waiting to delete it until the new copy exits.
        cwd = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else PROJECT_DIR
        try:
            subprocess.Popen(restart_command(), cwd=cwd, env=env)
        except OSError as e:
            messagebox.showerror("Screen Time", f"Couldn't restart:\n{e}")
            return
        self.quit(stopped=False)

    def quit(self, stopped=True):
        """stopped=False (from --quit) saves without the deliberate-quit mark, like a restart."""
        self.stop.set()
        self.syncer.close()
        self.peer.stop()
        self.tracker.save(stopped=stopped)
        self.tray.remove()
        self.root.destroy()


def main():
    if "--quit" in sys.argv:
        sys.exit(quit_command())
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on scaled displays
    except (AttributeError, OSError):
        pass
    if "--restart" in sys.argv:
        wait_until_stopped()  # the copy that restarted us is still saving and exiting
    if already_running():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("Screen Time", "The tracker is already running (as the script or the app).")
        return
    root = tk.Tk()
    if "--minimized" in sys.argv:
        root.withdraw()  # started with Windows: only the taskbar clock shows
    app = App(root, Tracker())
    on_quit_request(lambda: app.calls.put(lambda: app.quit(stopped=False)))
    root.mainloop()


if __name__ == "__main__":
    main()
