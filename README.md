# Screen Time Tracker

A small Windows app that tracks screen time across multiple monitors.

Unlike trackers that only count the focused window, it samples once per second and records:

- **focus**: the foreground app and the monitor it is on
- **cursor**: the app under the mouse cursor and the monitor the cursor is on
- **active**: both combined (an app counts once per second even if focus and cursor match)

Each day's log also has two overall counters:

- **total_seconds**: how long the PC was on today, including idle time but not time on the lock screen (Win+L). When the app starts it also counts the time the PC was already on: since boot, midnight, or the last save of an earlier run that day that crashed or was closed from Task Manager. It skips that after **Quit tracking** (or Ctrl+C), since you stopped tracking on purpose, and after the PC has slept, since the uptime can't tell when it was on. For the most accurate total, start the app at login.
- **active_seconds**: how long you were actively using the PC (sleep, idle and lock screen time excluded).

Both reset at midnight: that day's log is saved and a new one starts. The log is also saved every minute, so a shutdown loses at most a minute.

Active and per-app time only count while you have given keyboard or mouse input within the last 60 seconds (`IDLE_LIMIT` in `screentime/tracker.py`). Locking the screen stops all counting right away.

## Download and try it

1. Download `ScreenTime.exe` from the [latest release](https://github.com/BulMussa/Windows_screen_time/releases). No installation or Python needed; it needs Windows 10 or 11, 64-bit.
2. Run it. The exe isn't code-signed, so Windows may show **"Windows protected your PC"**: click **More info**, then **Run anyway**. Some antivirus tools also flag apps packaged this way (PyInstaller) by mistake.
3. Your active time appears in the bottom-left corner of the taskbar, and a clock icon in the hidden icons menu (the ^ arrow next to the system clock). Click either one to open the window. See [Usage](#usage) for what it shows.

Everything stays on your PC: the app sends nothing anywhere and keeps its data in `%LOCALAPPDATA%\ScreenTime\logs`.

**To remove it:** untick **Start with Windows** in the Settings tab, right-click the tray icon and choose **Quit tracking**, then delete `ScreenTime.exe` and the `%LOCALAPPDATA%\ScreenTime` folder.

**Found a bug or have an idea?** Open an [issue](https://github.com/BulMussa/Windows_screen_time/issues) and include your Windows version and what you did.

## Usage

Requires Windows 10 or 11. `dist\ScreenTime.exe` runs without Python; running from source needs Python 3.9+ and two packages for phone pairing: `pip install cryptography segno` (or `pip install -e .`). `cryptography` makes the TLS certificate the phone connects to, and `segno` draws the pairing QR code. segno is a small pure-Python QR encoder with no dependencies of its own. Getting a QR encoder's error correction and masking right by hand isn't worth the risk.

### App

Start `ScreenTime.exe`, or run `pythonw -m screentime` from the project folder. It runs in the background and shows today's active time (`Active 3h 12m`) in the bottom-left corner of the taskbar; the tray icon's tooltip also shows the PC-on time. The clock hides while a fullscreen app is open or the taskbar is auto-hidden.

It also adds an icon to the hidden icons menu (the ^ arrow next to the system clock). Hover over it to see today's times. To keep it always visible, drag it from that menu onto the taskbar.

- **Click** the clock or the tray icon to open the window: today's active time and the PC's up time (since Windows last started, like Task Manager's Up time; restarts and shutdowns reset it), a **Report** tab for today or any earlier day (pick it from the calendar next to "Timeline", or step a day back or forward with the ◀ ▶ arrows beside it; bold days have data, and sections without data say "No data"; the **Sync** button (cloud icon) under the timeline syncs right away, or opens the Connect tab if the sync mode uses a server and none is connected) with three 00:00-23:59 timelines: **Laptop** (blue: PC on, idle time included, with the PC-on total under the name; sleep, power-off and the lock screen left gray), **Phone** (orange) and **Sleep** (purple), where hovering over a period highlights it and shows its start, end and length. Phone and sleep come from your server, so they stay empty until another device sends them. The apps below count this PC only. the 3 most used apps, and every app's time with its icon (counted by active/focus/cursor, optionally split by monitor; the top 3 follow the same count), a **History** tab with one row per day, and a **Settings** tab (Start with Windows, showing or hiding the taskbar widget, logs folder, and **Restart Screen Time**, which saves and starts the app again without losing time).
- **Right-click** either one and choose **Quit tracking** to stop, or use the red **Quit tracking** button in the window. The green **Done** button (or closing the window) only hides it; tracking goes on.
- From a terminal or script, `ScreenTime.exe --quit` (or `python -m screentime --quit`) makes the running copy save and exit, e.g. before rebuilding. Unlike **Quit tracking**, it counts as a restart: the time until the next start can still count as PC-on time.

The **Connect** tab connects this PC to your own Screen Time server (HTTPS only; plain HTTP is accepted only for `localhost`). Enter the server address, your name and password, and a name for this PC. The password is used once to register the PC and is never saved; the device token goes into Windows Credential Manager, and the server address, the sync mode and what was already sent into `%LOCALAPPDATA%\ScreenTime\sync_state.json`. After that, changes to the PC-on and active periods and the per-app times are sent every 5 minutes (retried every minute while the server can't be reached), and only what changed. Each sync also pulls what other devices sent (phone and sleep periods), into `%LOCALAPPDATA%\ScreenTime\remote_intervals.json`, so the timelines work offline. Use **Sync** (the same button as in the Report tab) to sync right away, or **Disconnect** to stop (the device stays registered on the server). Keep this PC on the timezone set for your account, since days follow the PC's clock. Days logged before this version have PC-on periods but no active periods.

**Sync with** (also in the Connect tab) chooses how this PC syncs with your phone. Phone and sleep periods arrive by two routes: pulled from your server, or pushed by the phone straight to this PC.

| Mode | Server | Phone, directly |
|---|---|---|
| **Server** (the default) | this PC sends its periods and app times every 5 minutes and pulls the phone's | only if **Allow phone sync** is on |
| **Phone directly** | off: the status says so | the phone connects to this PC while **Allow phone sync** is on; this PC never connects to the phone |
| **Both** | as in **Server** | as in **Phone directly** |

The two routes are independent. Each keeps its own progress: what was sent to it, and where its last pull stopped, so switching never sends rows twice. If the server can't be reached, the phone still syncs directly, and the other way round. If the server doesn't know this PC's pull cursor (for example, it was restored from an older backup), only the server's cursor starts over; nothing that the phone pushed directly is lost. Connecting to another server or disconnecting forgets only the periods that came from the server alone. Periods the phone also pushed directly stay, and so do periods stored before routes were recorded. Periods show in the timelines the same way whichever route brought them.

Every row has an ID that doesn't depend on the route (made from the device and the period), and an `updated_at`. A row is merged the same way whichever route brings it: the newer `updated_at` wins; on the same `updated_at` a deletion wins over a live copy, so the result doesn't depend on which route was heard first; otherwise the copy already stored stays, so the same row arriving by both routes is applied once. Each stored row also remembers which routes delivered it.

**Export backup…** saves a JSON file with this PC's PC-on and active periods and app times (in the same format that is sent to the server) and the phone and sleep periods it has pulled, including the ones it saw deleted. **Import backup…** merges a backup in. Every row has an `updated_at`, and the newer copy wins, the same rule a sync uses. Rows the import loses don't change anything, and an old backup can't bring back a period that was deleted since. Phone and sleep periods are merged from any backup, also from a phone's. This PC's own periods and app times are only restored from a backup of this same install, i.e. with the same `sync_state.json`. They are restored into past days' logs whose file is older than the backup, or missing. Today's log is being written right now, so it is never overwritten. Restored days are checked again on the next sync.

**Allow phone sync** (under **Sync with**) lets the phone connect to this PC directly over HTTPS on your own network, with no server in between. It is off by default. While it's on, the line next to it shows the address it listens on, e.g. `Listening on 192.168.1.20:47613`. It listens only on this PC's address on a private network (192.168.x.x, 10.x.x.x, 172.16–31.x.x or link-local), follows that address when the PC moves to another network, and doesn't listen at all while the PC isn't on a private network. It answers only devices with a private address, and it stops when the app quits. While it listens, it also answers the paired phone's discovery requests on UDP port 47614, on the same address, so the phone finds this PC again after its address changes (see [Discovery](#discovery)). Windows Firewall asks once whether to allow it; allow it on **Private networks** only. The rule covers the discovery port too. See [Phone sync protocol](#phone-sync-protocol).

The line next to **Allow phone sync** shows the listener's state (`Listening on 192.168.1.20:47613`, `Off`, or why it can't listen). Once it listens, the app checks Windows Firewall in the background, read only (PowerShell's NetSecurity cmdlets, a few seconds), and says so in amber under it if the phone can't get in: "Blocks all incoming connections" is on, a rule blocks Screen Time, or no rule allows it on the kind of network this PC is on (Windows calls each Wi-Fi network Private or Public). The message says what to change. Only rules for Screen Time itself are looked at, so a rule that opens the port for every program isn't seen, and the message then says the firewall *may* block the phone. Discovery (UDP) being blocked is reported separately: the phone still connects at the address it knows.

**Pair a phone…** turns phone sync on if it's off and shows a QR code for the Screen Time phone app to scan. It holds this PC's name, address and port, a one-time pairing secret, and the SHA-256 fingerprint of this PC's certificate, so the phone knows it's talking to this PC. The code works for 10 minutes and only once. As soon as the phone has paired, the window says so and hides the code. Closing the window before that cancels the code. One phone is paired at a time, and pairing another replaces it. The paired phone is named next to the buttons, with when it last synced (`Phone last synced at 12:39.`, `… yesterday at 18:02.`, or `The phone hasn't synced yet.`); that's the last time it pulled or pushed, kept in `peer.json`. **Remove pairing** revokes it at once: it can't sync until you pair it again. The pairing secret and the phone's key are kept in Windows Credential Manager, like the server's device token; `%LOCALAPPDATA%\ScreenTime\peer.json` holds only the setting and the paired phone's name. `python -m screentime.peer` prints the address, the certificate fingerprint and the paired phone.

Tick **Start with Windows** in the Settings tab to launch it at login in the background. Untick **Show active time on the taskbar** to hide the widget in the taskbar's corner; the tray icon stays, and the choice is saved in `%LOCALAPPDATA%\ScreenTime\settings.json`.

Only one tracker can run at a time; starting a second copy (app or console tracker) shows a message instead.

### Console

```bash
python -m screentime.tracker      # track in the console; Ctrl+C stops and prints a report
python -m screentime.report       # overall time per day from the logs
python -m screentime.report 7     # ... for the last 7 days
python -m screentime.tracker --quit   # save and exit the running tracker or app
```

`pip install -e .` also installs these as the commands `screentime`, `screentime-tracker` and `screentime-report`.

### Logs

Totals are saved to `%LOCALAPPDATA%\ScreenTime\logs\<date>.json` (e.g. `2026-10-28.json`), one file per day. Runs on the same day add to that day's file, and a session that runs past midnight starts a new file.

## Building ScreenTime.exe

Run `packaging\build.bat`. It uses PyInstaller from `.venv` if there is one (`python -m venv .venv`, then `.venv\Scripts\pip install pyinstaller cryptography segno`), stops with a message if `cryptography` or `segno` is missing, bundles both into the exe, and writes `dist\ScreenTime.exe`, a single file that runs on PCs without Python.

## Project structure

```
screentime/            the Python package
  __main__.py          python -m screentime: starts the app
  app.py               window, taskbar clock, Start with Windows
  tray.py              tray icon (Win32 via ctypes)
  icons.py             app icons for the Report tab, read from each .exe
  tracker.py           sampling, daily logs, console tracker
  sync.py              sync: the server transport, sync mode, merge rule and routes, device token,
                       background push and pull, backup export and import
  peer.py              phone sync listener: HTTPS API the phone connects to, limits, change feed
  tlscert.py           makes the listener's self-signed TLS certificate (with cryptography)
  firewall.py          whether Windows Firewall lets the phone reach the listener (read only)
  timeline.py          phone and sleep spans of one day from the pulled data
  report.py            per-day summary of the logs
  assets/              app icon
packaging/
  build.bat            builds dist\ScreenTime.exe with PyInstaller
  version_info.txt     name and version shown in Task Manager
tests/                 test scripts (see Tests)
pyproject.toml         package metadata and commands
```

## Tests

```bash
python tests/run_all.py            # every tests/test_*.py, each in its own process, warnings as errors
python tests/run_all.py backup     # only tests/test_backup.py
```

Each test points the app at a new temporary logs folder and a test Credential Manager entry (`ScreenTimeSyncTEST`), and talks to a fake server on `127.0.0.1`. The phone sync tests listen on `127.0.0.1` only, so nothing is opened to the network. Your real logs, sync state, device token and Start with Windows entry are never touched. `test_ui.py` and `test_pull.py` open the app's window for a few seconds. Stop the running app first (`ScreenTime.exe --quit`).

- `test_sync.py`: connecting, pushing in batches, only sending changes, rejected rows, worker statuses
- `test_pull.py`: pulling phone and sleep periods, paging, cursors, the Report timelines
- `test_transports.py`: per-transport progress, migrating older state files, the sync mode, remembered deletions
- `test_backup.py`: export/import round trip and the newest-wins merge
- `test_both.py`: both routes at once, with a fake server and a fake phone pushing to the real listener: the same row applied once, overlapping and conflicting rows in either order, per-route cursors and resets, one route down, the timelines
- `test_firewall.py`: the firewall verdicts on made-up rules (allowed, blocked by a rule, all incoming blocked, no rule for this network type, discovery blocked), and one real read-only query of this PC's firewall
- `test_peer.py`: the phone sync listener on `127.0.0.1` over HTTPS: the phone's last sync, discovery over UDP (answers only the paired phone, rate limit, stops with the listener, a taken port), the certificate fingerprint, pairing (one-time secret, expiry, replacing a phone), wrong secret or key, replayed nonces, revoked pairing, hello, pull, push, bad input, oversize bodies, rate limits, too many connections
- `test_ui.py`: the Connect tab: connecting, Sync, Disconnect, sync mode, backup buttons, Allow phone sync

## Phone sync protocol

Version 1. The phone starts every exchange and the PC only answers. It mirrors the server's `/v1/sync/push` and `/v1/sync/pull`, with the same row format: intervals with `id` (UUID), `kind` (`pc_on`, `pc_active`, `phone_active`, `sleep`), `start_utc`, `end_utc`, `source` (`tracker`, `health_connect`, `manual`), `updated_at`, `deleted_at`; app rows with `local_date`, `app`, `monitor`, `focus_s`, `cursor_s`, `active_s` (0–90000) and `updated_at`. Times are UTC like `2026-11-10T04:05:00Z` (a fraction of a second is allowed).

**Connection.** HTTPS (TLS 1.2 or newer) to the PC's private network address, port 47613 (`port` in `peer.json`). The certificate is self-signed (P-256, made with `cryptography` on first use, valid 20 years) and kept in `%LOCALAPPDATA%\ScreenTime\peer\`. The phone doesn't trust it through a certificate authority: it compares the SHA-256 of the certificate the PC presents (its DER bytes) with `cert_sha256` from the QR code, and hangs up if they differ.

**Pairing QR code.** JSON: `{"name": "<PC name>", "addresses": ["192.168.1.20"], "port": 47613, "secret": "<one-time pairing secret>", "cert_sha256": "<64 hex digits>"}`.

**Proof.** No secret or key is ever sent; each request proves it instead. First get a nonce: `GET /peer/nonce` answers `{"nonce": "...", "expires_in": 120}`, and every proven answer also carries the next one in the `X-Peer-Nonce` header. Then send:

```
Authorization: HMAC <nonce>:<mac>
mac = hex(HMAC-SHA256(key, method + "\n" + path + "\n" + nonce + "\n" + hex(SHA-256(body))))
```

`key` is the UTF-8 text of the pairing secret for `/peer/pair`, and the phone's key for everything else. `path` is the request target exactly as sent, with its query (e.g. `/peer/pull?since=0&limit=1000`). `body` is the exact bytes sent, empty for GET. A nonce is valid for 2 minutes and works once, also when the proof was wrong, so a recorded request can't be played again. The proof also covers the method, path, query and body, so it can't be moved to another request.

**Pairing.** `POST /peer/pair` with `{"device_id": "<phone's id>", "name": "<phone's name>"}`, proven with the pairing secret, answers `{"key": "<the phone's key>", "device_id": "<this PC's install id>", "name": ..., "type": "windows", "protocol": 1}`. The phone keeps the key and proves every later request with it. The pairing secret stays valid until the phone's first request with the key (so it can ask again if the answer got lost), then it's deleted. Asking again gives a new key and the earlier one stops working.

| Request | Answer |
|---|---|
| `GET /peer/nonce` (no proof needed) | `{"nonce": "...", "expires_in": 120}` |
| `POST /peer/pair` (proven with the pairing secret) | `{"key": "...", "device_id": "...", "name": "...", "type": "windows", "protocol": 1}` |
| `GET /peer/hello` | `{"device_id": "<this PC's install id>", "name": "<PC name>", "type": "windows", "protocol": 1}` |
| `GET /peer/pull?since=<cursor>&limit=<n>` | `{"intervals": [...], "app_usage_daily": [...], "cursor": "<next>", "has_more": true/false}` |
| `POST /peer/push` with `{"intervals": [...], "app_usage_daily": [...]}` | `{"intervals": {"accepted": n, "ignored": n, "rejected": [{"id": ..., "reason": ...}]}, "app_usage_daily": {...}}` |

**Pull** serves this PC's own rows: PC-on and active periods, and app times. Each row gets a number when it first appears or changes. Start with `since=0` and pass the `cursor` of each answer to the next request, until `has_more` is false. `limit` is 1–5000 (default 1000). A period that disappears from the logs is served again with `deleted_at` set. If `since` is a cursor the PC doesn't know (e.g. its `peer_feed.json` was deleted), the answer is `400`: start again from `0`. Rows arrive again with a new `updated_at`, and the newer copy wins as usual.

**Push** takes the phone's rows. Phone and sleep periods go into the same store as periods pulled from the server, and the window shows them. App rows go into `phone_app_usage.json`, which the window doesn't show yet. For each row the copy with the newer `updated_at` wins: `accepted` counts rows taken, `ignored` counts rows not newer than this PC's copy. Rows are `rejected`, and the others still applied, if their `updated_at` is more than 1 hour ahead of the PC's clock (`updated_at_in_future`, the server's rule), or if they are `pc_on`/`pc_active` (`kind_not_allowed`: only this PC writes those). App rows are named `<date>|<app>|<monitor>` in `rejected`, as on the server.

<a id="discovery"></a>**Discovery.** When the phone doesn't reach this PC at the address it knows, it broadcasts a UDP datagram to port 47614 (fixed, unlike the HTTPS port): `{"type": "screentime-discover", "v": 1, "from": "<the phone's device id>", "want": "<this PC's install id>"}`. While the listener runs, a responder bound to the same private address answers with `{"type": "screentime-here", "v": 1, "device_id": "<this PC's install id>", "address": "192.168.1.20", "port": 47613}`, sent back to the asking address only. It answers only the paired phone's device id asking for this PC, and only clients with a private address; anything else, including a datagram over 512 bytes, gets no answer at all. At most 30 answers a minute go to one address, and an answer is no bigger than the request, so the responder can't be used to flood anyone. The answer proves nothing: the phone still checks the certificate pin when it connects. If the port is taken, the listener still runs and its status line says discovery is off. The device ids aren't secrets, so anyone on the network who sees the phone's request also learns this PC's address, which they could see anyway.

**Errors** come as `{"detail": "..."}`:

| Status | When |
|---|---|
| 400 | body isn't JSON (or UTF-8); unknown pull cursor; bad `limit` |
| 401 | no `Authorization: HMAC` header; a nonce that is unknown, expired or used; a wrong proof; no phone paired (or the pairing was removed); `/peer/pair` with no pairing open |
| 403 | the client isn't on a private network |
| 404 / 405 | unknown path / method |
| 411 | push without `Content-Length` (chunked bodies aren't accepted) |
| 413 | body over 2 MiB, refused before it's read |
| 422 | body or a row doesn't follow the format (the whole push is refused, nothing is stored); more than 5000 rows in a list |
| 429 | over 240 requests a minute from one address, or 5 wrong proofs within 10 minutes (then 10 minutes' wait, also for nonces); see `Retry-After` |

At most 8 connections are handled at once, and more are closed. A client gets 10 seconds for the TLS handshake and for each read.

## Limitations

- Apps are identified by process name, not by tab or document.
- The cursor position is a proxy for attention, not a measure of it.

## License

[MIT](LICENSE)
