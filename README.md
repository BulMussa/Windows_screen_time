# Screen Time Tracker

A small, dependency-free Windows script that tracks screen time across multiple monitors.

Unlike trackers that only count the focused window, it samples once per second and records:

- **focus**: the foreground app and the monitor it is on
- **cursor**: the app under the mouse cursor and the monitor the cursor is on
- **active**: both combined (an app counts once per second even if focus and cursor match)

Each day's log also has two overall counters:

- **total_seconds**: how long the PC was on today, including idle time. When the script starts it also counts the time since boot (or midnight, or the end of an earlier run that day). If the PC has slept since booting, it counts from the script's start instead, so for the most accurate total, start the script at login.
- **active_seconds**: how long you were actively using the PC (sleep and idle time excluded).

Both reset at midnight: that day's log is saved to `logs/` and a new one starts. The log is also saved every minute, so a shutdown loses at most a minute.

Per-app time only counts while you have given keyboard or mouse input within the last 60 seconds (`IDLE_LIMIT` in the script).

## Usage

Requires Windows and Python 3 (nothing to install).

### App

Double-click `screentime_app.pyw`, or run `pythonw screentime_app.pyw`. It runs in the background and shows today's PC-on time (`On 5h 40m`) in the bottom-left corner of the taskbar; the tray icon's tooltip also shows the work (active) time. The clock hides while a fullscreen app is open or the taskbar is auto-hidden.

It also adds an icon to the hidden icons menu (the ^ arrow next to the system clock). Hover over it to see today's times. To keep it always visible, drag it from that menu onto the taskbar.

- **Click** the clock or the tray icon to open the window: live totals, a **Today** tab with time per app (active/focus/cursor, optionally split by monitor) and a **History** tab with one row per day.
- **Right-click** either one and choose **Quit tracking** to stop. Closing the window only hides it.

Tick **Start with Windows** in the window to launch it at login with only the taskbar clock showing.

Only one tracker can run at a time; starting a second copy (app or script) shows a message instead.

### Script

```bash
python screentime_tracker.py
```

Press Ctrl+C to stop.

`python show_screentime.py` (or `python show_screentime.py 7` for the last week) prints the overall time per day from the logs.

 A report is printed and totals are saved to `logs/<date>.json` (e.g. `logs/2026-10-28.json`), one file per day. Runs on the same day add to that day's file, and a session that runs past midnight starts a new file.

## Limitations

- Apps are identified by process name, not by tab or document.
- The cursor position is a proxy for attention, not a measure of it.
