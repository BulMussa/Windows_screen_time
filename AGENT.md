# Rules for AI agents working on this project

## Stop the running app while implementing and testing

- Before implementing or testing a new feature, stop any running copy of the app: `ScreenTime.exe`, or `pythonw`/`python` running `screentime`. Only one tracker can run at a time, and Windows won't let `packaging\build.bat` replace a running `dist\ScreenTime.exe`.
- Prefer a clean quit, so today's log gets saved. Force-stopping (`Stop-Process`) loses up to a minute of unsaved tracking (`SAVE_EVERY`).
- When the work is done, rebuild with `packaging\build.bat` and start `dist\ScreenTime.exe` again, so tracking keeps running.

## Test every new feature with everything it needs

- Test each feature for real, not only by reading the code:
  - Import every module with warnings as errors: `python -W error -c "import screentime.app, screentime.tracker, screentime.report, screentime.tray"`.
  - Run each affected entry point: `python -m screentime`, `python -m screentime.tracker`, `python -m screentime.report`.
  - Exercise the UI that changed: the window, the taskbar clock and the tray icon. Take screenshots when the look matters.
  - Rebuild `dist\ScreenTime.exe` and check that it starts.
  - Cover edge cases, simulating them when they can't happen live: midnight, sleep, locked screen, crashes, a second copy starting, different drives.
- Point test runs at a scratch logs folder (set `screentime.tracker.LOG_DIR`), so tests never write fake data into the real logs in `%LOCALAPPDATA%\ScreenTime\logs`.
- Report what was tested and what couldn't be tested.
