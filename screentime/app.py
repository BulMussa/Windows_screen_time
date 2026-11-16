"""Screen time tracker with a small window (Windows, stdlib only).

Tracks in the background exactly like screentime.tracker and shows today's PC-on
time ("On 5h 40m") in the bottom-left corner of the taskbar, plus an icon in the
hidden icons menu. Click either to open the window with today's apps and the daily history;
right-click either to quit. Closing the window only hides it, tracking goes on.

Run:     pythonw -m screentime     (or ScreenTime.exe, built by packaging/build.bat)
Quit:    ScreenTime.exe --quit     (or python -m screentime --quit): saves and exits the running copy
"""
import base64
import ctypes
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import winreg
from ctypes import wintypes
from datetime import datetime
from tkinter import font as tkfont, messagebox, ttk

from .tracker import (KINDS, LOG_DIR, MONITOR_DEFAULTTONEAREST, MONITORINFOEXW, TICK,
                      Tracker, already_running, fmt, on_quit_request, quit_command, user32,
                      wait_until_stopped)
from .icons import icon_png, small_icon_size
from .report import read_logs, summarize
from .tray import TrayIcon

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "ScreenTimeTracker"
THEME_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
TABLE_REFRESH = 5  # seconds between refreshes of the app tables
HISTORY_REFRESH = 60
CLOCK_OFFSET = 12  # pixels from the left end of the taskbar
RAISE_EVERY = 500  # ms; the taskbar covers the clock when clicked, so keep raising it
ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "screentime.ico")
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
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
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}h {m:02d}m"


def duration(seconds):
    """Like fmt, but leaves out the hours until there is at least one."""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def colored_button(parent, text, color, hover, command):
    """A flat button in a solid color (ttk buttons can't be colored on Windows)."""
    button = tk.Button(parent, text=text, command=command, bg=color, fg="white",
                       activebackground=hover, activeforeground="white", relief="flat", bd=0,
                       padx=16, pady=5, font=("Segoe UI Semibold", 9), cursor="hand2")
    button.bind("<Enter>", lambda e: button.config(bg=hover))
    button.bind("<Leave>", lambda e: button.config(bg=color))
    return button


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
    """Today's PC-on time, and since when it counts, drawn over the left end of the taskbar."""

    def __init__(self, app):
        self.app = app
        light = light_theme()
        # Pixels in this color turn see-through. It is close to the taskbar's own color
        # so the anti-aliased edges of the text blend in.
        key = "#eeeeef" if light else "#1c1c1d"
        text, dim = ("#000000", "#555555") if light else ("#ffffff", "#aaaaaa")

        self.win = tk.Toplevel(app.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-transparentcolor", key)
        self.win.configure(bg=key)
        self.label = tk.Label(self.win, bg=key, fg=text, font=("Segoe UI Semibold", 11), pady=0)
        self.since = tk.Label(self.win, bg=key, fg=dim, font=("Segoe UI", 8), pady=0)
        self.label.pack()
        self.since.pack()

        self.menu = tk.Menu(self.win, tearoff=False)
        self.menu.add_command(label="Open Screen Time", command=app.show)
        self.menu.add_separator()
        self.menu.add_command(label="Quit tracking", command=app.quit)
        for widget in (self.win, self.label, self.since):
            widget.bind("<Button-1>", lambda e: app.show())
            widget.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        self.place()

    def update(self, log):
        self.label.config(text=f"On {short(log['total_seconds'])}")
        if log["first_seen"]:
            self.since.config(text=f"since {datetime.fromisoformat(log['first_seen']):%H:%M}")

    def place(self):
        rect = visible_taskbar()
        if rect is None:
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


class App:
    def __init__(self, root, tracker):
        self.root = root
        self.tracker = tracker
        self.stop = threading.Event()
        self.ticks = 0

        root.title("Screen Time")
        root.geometry("560x560")
        root.minsize(440, 400)
        root.protocol("WM_DELETE_WINDOW", root.withdraw)  # hide; the clock and tray icon stay
        if os.path.exists(ICON):
            root.iconbitmap(default=ICON)
        style = ttk.Style()
        style.configure("Value.TLabel", font=("Segoe UI Semibold", 22))
        style.configure("Caption.TLabel", font=("Segoe UI", 9), foreground="#666")
        style.configure("Heading.TLabel", font=("Segoe UI Semibold", 10))
        # Rows tall enough for the app icons
        icon = small_icon_size()
        text = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        style.configure("Treeview", rowheight=max(icon, text) + 6)
        # Rows without the expand-arrow slot, which these flat tables don't use
        style.layout("Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [
            ("Treeitem.image", {"side": "left", "sticky": ""}),
            ("Treeitem.focus", {"side": "left", "sticky": "", "children": [
                ("Treeitem.text", {"side": "left", "sticky": ""})]})]})])
        self.icons = {}  # .exe path -> PhotoImage, or None if it has no icon
        self.no_icon = tk.PhotoImage(width=icon, height=icon)  # blank, keeps names aligned

        # Today's totals
        top = ttk.Frame(root, padding=(16, 12, 16, 4))
        top.pack(fill="x")
        self.pc_on = tk.StringVar()
        self.active = tk.StringVar()
        self.since = tk.StringVar()
        for col, (caption, var) in enumerate((("PC on today", self.pc_on), ("Active today", self.active))):
            ttk.Label(top, text=caption, style="Caption.TLabel").grid(row=0, column=col, sticky="w")
            ttk.Label(top, textvariable=var, style="Value.TLabel").grid(row=1, column=col, sticky="w", padx=(0, 32))
        ttk.Label(top, textvariable=self.since, style="Caption.TLabel").grid(row=2, column=0, columnspan=2, sticky="w")

        tabs = ttk.Notebook(root)
        tabs.pack(fill="both", expand=True, padx=16, pady=8)

        # Today tab: per-app time
        today = ttk.Frame(tabs, padding=8)
        tabs.add(today, text="Today")
        bar = ttk.Frame(today)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="Count by:").pack(side="left")
        self.kind = tk.StringVar(value="active")
        kind = ttk.Combobox(bar, textvariable=self.kind, values=KINDS, state="readonly", width=8)
        kind.pack(side="left", padx=6)
        kind.bind("<<ComboboxSelected>>", lambda e: self.refresh_apps())
        self.by_monitor = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Split by monitor", variable=self.by_monitor,
                        command=self.refresh_apps).pack(side="left", padx=12)
        frame, self.apps = make_table(today, [("Time", 110, "e"), ("Share", 70, "e")], tree_column=("App", 260))
        frame.pack(fill="both", expand=True)

        # History tab: one row per day
        history = ttk.Frame(tabs, padding=8)
        tabs.add(history, text="History")
        frame, self.days = make_table(history, [("Date", 100, "w"), ("PC on", 100, "e"),
                                                ("Active", 100, "e"), ("Top app", 160, "w")])
        frame.pack(fill="both", expand=True)
        self.summary = tk.StringVar()
        ttk.Label(history, textvariable=self.summary, style="Caption.TLabel").pack(anchor="w", pady=(6, 0))
        tabs.bind("<<NotebookTabChanged>>", lambda e: self.refresh_history())

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
        ttk.Label(settings, text="Opens at login with only the taskbar clock showing.",
                  style="Caption.TLabel").pack(anchor="w", padx=(21, 0))
        ttk.Separator(settings).pack(fill="x", pady=14)
        ttk.Label(settings, text="Logs", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(settings, text=LOG_DIR, style="Caption.TLabel").pack(anchor="w", pady=(4, 8))
        ttk.Button(settings, text="Open logs folder", command=self.open_logs).pack(anchor="w")
        ttk.Separator(settings).pack(fill="x", pady=14)
        ttk.Label(settings, text="App", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(settings, text="Saves today's log and starts Screen Time again, e.g. if something looks stuck.",
                  style="Caption.TLabel").pack(anchor="w", pady=(4, 8))
        ttk.Button(settings, text="Restart Screen Time", command=self.restart).pack(anchor="w")

        # Footer
        foot = ttk.Frame(root, padding=(16, 0, 16, 12))
        foot.pack(fill="x")
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
            self.pc_on.set(fmt(log["total_seconds"]))
            self.active.set(fmt(log["active_seconds"]))
            since = datetime.fromisoformat(log["first_seen"]).strftime("%H:%M") if log["first_seen"] else "now"
            self.since.set(f"Counting since {since} · closing this window keeps tracking in the background")
            if self.ticks % TABLE_REFRESH == 0:
                self.refresh_apps(log)
            if self.ticks % HISTORY_REFRESH == 0:
                self.refresh_history(log)
            self.ticks += 1
        self.root.after(1000, self.refresh)

    def run_calls(self):
        while not self.calls.empty():
            self.calls.get()()
        self.root.after(100, self.run_calls)

    def show(self):
        self.ticks = 0  # refresh everything right away
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def refresh_apps(self, log=None):
        log = log or self.tracker.snapshot()
        totals = {}
        for key, secs in log[self.kind.get()].items():
            app, monitor = key.split("|")
            name = f"{app}  ({monitor})" if self.by_monitor.get() else app
            totals[name] = totals.get(name, 0.0) + secs
        whole = sum(totals.values()) or 1
        self.apps.delete(*self.apps.get_children())
        for name, secs in sorted(totals.items(), key=lambda kv: -kv[1]):
            image = self.app_icon(log["paths"].get(name.split("  (")[0]))
            self.apps.insert("", "end", text=f"  {name}", image=image,  # spaces: gap after the icon
                             values=(duration(secs), f"{secs / whole:.0%}"))

    def app_icon(self, path):
        """The app's icon for the table, read from its .exe once and then cached."""
        if not path:
            return self.no_icon
        if path not in self.icons:
            png = icon_png(path)
            self.icons[path] = tk.PhotoImage(data=base64.b64encode(png)) if png else None
        return self.icons[path] or self.no_icon

    def refresh_history(self, log=None):
        today = log or self.tracker.snapshot()
        logs = dict(read_logs())
        logs[self.tracker.day.isoformat()] = today  # the live numbers, not the last save
        rows, on_sum, active_sum = [], 0.0, 0.0
        for day in sorted(logs, reverse=True):
            on, active, top = summarize(logs[day])
            on_sum += on
            active_sum += active
            rows.append((day, fmt(on), fmt(active), top))
        fill(self.days, rows)
        n = len(rows)
        self.summary.set(f"{n} day{'s' if n != 1 else ''} · average PC on {fmt(on_sum / n)}, "
                         f"active {fmt(active_sum / n)}")

    def toggle_autostart(self):
        try:
            set_autostart(self.autostart.get())
        except OSError as e:
            self.autostart.set(not self.autostart.get())
            messagebox.showerror("Screen Time", f"Couldn't change the startup setting:\n{e}")

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
