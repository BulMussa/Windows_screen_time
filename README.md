# Screen Time Tracker

A small, dependency-free Windows app that tracks screen time across multiple monitors.

Unlike trackers that only count the focused window, it samples once per second and records:

- **focus**: the foreground app and the monitor it is on
- **cursor**: the app under the mouse cursor and the monitor the cursor is on
- **active**: both combined (an app counts once per second even if focus and cursor match)

Each day's log also has two overall counters:

- **total_seconds**: how long the PC was on today, including idle time but not time on the lock screen (Win+L). When the app starts it also counts the time the PC was already on: since boot, midnight, or the last save of an earlier run that day that crashed or was closed from Task Manager. It skips that after **Quit tracking** (or Ctrl+C), since you stopped tracking on purpose, and after the PC has slept, since the uptime can't tell when it was on. For the most accurate total, start the app at login.
- **active_seconds**: how long you were actively using the PC (sleep, idle and lock screen time excluded).

Both reset at midnight: that day's log is saved and a new one starts. The log is also saved every minute, so a shutdown loses at most a minute.

Active and per-app time only count while you have given keyboard or mouse input within the last 60 seconds (`IDLE_LIMIT` in `screentime/tracker.py`). Locking the screen stops all counting right away.

## Usage

Requires Windows 10 or 11. `dist\ScreenTime.exe` runs without Python; running from source needs Python 3.9+ and nothing else.

### App

Start `ScreenTime.exe`, or run `pythonw -m screentime` from the project folder. It runs in the background and shows today's PC-on time (`On 5h 40m`) in the bottom-left corner of the taskbar; the tray icon's tooltip also shows the work (active) time. The clock hides while a fullscreen app is open or the taskbar is auto-hidden.

It also adds an icon to the hidden icons menu (the ^ arrow next to the system clock). Hover over it to see today's times. To keep it always visible, drag it from that menu onto the taskbar.

- **Click** the clock or the tray icon to open the window: live totals, a **Today** tab with time per app (active/focus/cursor, optionally split by monitor) and a **History** tab with one row per day.
- **Right-click** either one and choose **Quit tracking** to stop. Closing the window only hides it.

Tick **Start with Windows** in the window to launch it at login with only the taskbar clock showing.

Only one tracker can run at a time; starting a second copy (app or console tracker) shows a message instead.

### Console

```bash
python -m screentime.tracker      # track in the console; Ctrl+C stops and prints a report
python -m screentime.report       # overall time per day from the logs
python -m screentime.report 7     # ... for the last 7 days
```

`pip install -e .` also installs these as the commands `screentime`, `screentime-tracker` and `screentime-report`.

### Logs

Totals are saved to `%LOCALAPPDATA%\ScreenTime\logs\<date>.json` (e.g. `2026-10-28.json`), one file per day. Runs on the same day add to that day's file, and a session that runs past midnight starts a new file.

## Building ScreenTime.exe

Run `packaging\build.bat`. It uses PyInstaller from `.venv` if there is one (`python -m venv .venv`, then `.venv\Scripts\pip install pyinstaller`) and writes `dist\ScreenTime.exe`, a single file that runs on PCs without Python.

## Project structure

```
screentime/            the Python package
  __main__.py          python -m screentime: starts the app
  app.py               window, taskbar clock, Start with Windows
  tray.py              tray icon (Win32 via ctypes)
  tracker.py           sampling, daily logs, console tracker
  report.py            per-day summary of the logs
  assets/              app icon
packaging/
  build.bat            builds dist\ScreenTime.exe with PyInstaller
  version_info.txt     name and version shown in Task Manager
pyproject.toml         package metadata and commands
```

## Limitations

- Apps are identified by process name, not by tab or document.
- The cursor position is a proxy for attention, not a measure of it.
