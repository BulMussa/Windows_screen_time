# Screen Time Tracker

A small, dependency-free Windows script that tracks screen time across multiple monitors.

Unlike trackers that only count the focused window, it samples once per second and records:

- **focus**: the foreground app and the monitor it is on
- **cursor**: the app under the mouse cursor and the monitor the cursor is on
- **active**: both combined (an app counts once per second even if focus and cursor match)

Time only counts while you have given keyboard or mouse input within the last 60 seconds (`IDLE_LIMIT` in the script).

## Usage

Requires Windows and Python 3.

```bash
python screentime_tracker.py
```

Press Ctrl+C to stop. A report is printed and totals are saved to `screentime_log.json` (accumulated across runs).

## Limitations

- Apps are identified by process name, not by tab or document.
- The cursor position is a proxy for attention, not a measure of it.
