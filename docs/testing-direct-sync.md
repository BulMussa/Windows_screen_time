# Testing direct sync (phone ↔ laptop) and the three sync modes

A checklist for the hybrid sync: the Windows app (this repo) and the Android app (`Screen-time-android`). Each item says how to run it, what should happen, and whether it is **automated** (a test covers it) or needs **a person** (real networks, Windows prompts, a phone in hand).

Words used below:

- **Mode** is **Sync with**. On the phone it is in Settings: **Server**, **Laptop directly** or **Both**. On the PC it is in the Connect tab: **Server**, **Phone directly** or **Both**.
- **Status** is the phone's sync status line, under **Sync now**.
- **The PC's sync line** is the line under **Sync with** in the PC's Connect tab, and the amber firewall note under it.

Before the manual items:

1. Start `dist\ScreenTime.exe`, connect it to your server, and set **Sync with: Both**.
2. Install the debug APK on the phone with `adb install -r app/build/outputs/apk/debug/app-debug.apk` (`-r` keeps the data; never uninstall the app, that deletes its database).
3. Pair the two (PC: **Pair phone**; phone: Settings, Laptop, **Pair with laptop**).

## Automated

Run these first; every manual item assumes they pass.

| What | Command | Covers |
|---|---|---|
| Windows tests | `.venv\Scripts\python.exe tests/run_all.py` (quit the app first: `dist\ScreenTime.exe --quit`) | Sync, pull, transports, backup, both routes (`test_both.py`), the remote store's formats and damaged files (`test_store.py`), the listener (pairing, wrong secret/key, revoked pairing, discovery), firewall verdicts, UI |
| Android unit tests | `./gradlew testDebugUnitTest` | Merge rule, both routes with fakes (`BothModeTest`), discovery order and timeouts, status texts and backoff (`SyncPolicyTest`), the pin and proof |
| Android on a phone | `adb install -r` both APKs (`assembleDebug assembleDebugAndroidTest`), then `adb shell am instrument -w com.screentime.mobile.test/androidx.test.runner.AndroidJUnitRunner` | Both routes against real SQLite (`BothModeDbTest`), the database upgrade to version 3 (`DbUpgradeTest`). The live tests are skipped unless given arguments (see each file). Don't use `gradlew connectedAndroidTest`: it uninstalls the app afterwards and so deletes its data |
| Upgrade on a copy of the real phone database | See [Database upgrade on a real copy](#database-upgrade-on-a-real-copy) | `DbUpgradeTest` on a copy of your own data |
| The PC's remote store on a copy | See [The PC's remote store on a real copy](#the-pcs-remote-store-on-a-real-copy) | Older entries without routes, loaded, saved and pulled again unchanged |

## Manual checklist

### 1. Home Wi-Fi: each mode syncs

- **Steps.** Put the phone and PC on the same home Wi-Fi.
  1. On the phone, set **Sync with: Server** and tap **Sync now**.
  2. Set **Laptop directly** and tap **Sync now**.
  3. Set **Both** and tap **Sync now**.
  4. After each step, open the phone's timeline for today and the PC's Report tab.
- **Expected.**
  - **Server:** `Synced at HH:MM`.
  - **Laptop directly:** `Synced at HH:MM · laptop found via last known address (192.168.x.x)` (or `via broadcast`).
  - **Both:** the same as **Laptop directly**.
  - **Timelines:** the phone's Laptop row and the PC's Phone row show the same periods in every mode. The PC's line says `Last synced at HH:MM.` after each direct sync.
- **Automated?** Partly: `BothModeTest` and `test_both.py` cover the logic with fakes. The real network needs a person.

### 2. Phone hotspot

- **Steps.**
  1. Turn on the phone's hotspot and connect the PC to it. Windows asks whether the network is Public or Private; see item 8.
  2. Leave the phone in **Both** and tap **Sync now**.
  3. Then turn the hotspot off, connect the PC to the phone's Wi-Fi network again, and **Sync now** once more.
- **Expected.**
  - **On the hotspot:** the PC's sync line shows a new address (e.g. `Listening on 10.x.x.x:47613` or `192.168.43.x`). The phone finds the laptop `via broadcast`; the Wi-Fi gateway is the phone itself, so the gateway step can't help. In **Both** the server syncs as well if the phone has mobile data.
  - **Back on home Wi-Fi:** the last known address is tried first, then the laptop is found again.
- **Automated?** No, it needs a person. The discovery order is unit-tested (`DiscoveryTest`).

### 3. A Wi-Fi network that isolates clients

- **Steps.** Use a guest network or a router with "AP/client isolation" on, phone and PC on it.
  1. **Sync now** in **Laptop directly**.
  2. Then enter the PC's address under Settings, Laptop, **Laptop's address, if it isn't found**, and **Sync now** again.
- **Expected.**
  - **Sync now:** `Laptop not found. Is it on, with Screen Time running and syncing with the phone, on the same Wi-Fi? Will try again.`
  - **With the address entered:** still not found if the network blocks all traffic between devices. That is a known limit, not a bug. Nothing crashes. In **Both**, the server part still syncs: `Laptop: Laptop not found. … Server: synced at HH:MM.`
- **Automated?** No, it needs a person.

### 4. Wrong secret

- **Steps.**
  1. On the PC choose **Pair phone** and note the code.
  2. On the phone choose **Enter code** and type the IP, port and fingerprint right, but change one character of the secret.
- **Expected.**
  - **On the phone:** the status says `The laptop didn't accept the pairing code: it's wrong, expired or already used. Pair again with a new code.`
  - **On the PC:** the code stays open, and pairing with the right secret afterwards works.
  - **After 5 wrong proofs within 10 minutes:** the PC refuses that phone for 10 minutes (429).
- **Automated?** Yes. `test_peer.py` covers the wrong secret, wrong key and rate limit on the listener. `StatusLiveTest` covers the phone's status text against a scratch listener (run it with the arguments in its file). Typing the code is manual.

### 5. Pairing removed on either side

- **Steps.**
  - **On the PC:** press **Remove pairing**, then **Sync now** on the phone.
  - **On the phone:** pair again first, then on the phone press **Remove pairing**, then **Sync now**.
- **Expected.**
  - **Removed on the PC:** the phone shows `The laptop doesn't accept this phone anymore (the pairing was removed or replaced there). Pair again.`. In **Both**, the server part still syncs.
  - **Removed on the phone:** the phone forgets the laptop, and **Sync with** goes back to **Server** if a server is connected. The PC still lists the phone until **Remove pairing** is pressed there; that is documented.
  - **Pairing again afterwards:** the phone sends all its rows to the laptop once more, and nothing is duplicated, because the IDs are stable.
- **Automated?** Partly: `test_peer.py` (revoked pairing → 401) and `SyncPolicyTest` (the text). The real sequence needs a person.

### 6. Laptop asleep or app closed

- **Steps.**
  1. With the phone in **Both**, put the PC to sleep (or `ScreenTime.exe --quit`) and tap **Sync now**.
  2. Leave the phone on Wi-Fi for an hour in the background.
  3. Then wake the PC (or start the app).
- **Expected.**
  - **Sync now:** `Laptop: Laptop not found. … Server: synced at HH:MM.`
  - **In the background:** no laptop error is ever shown. The status reads `Synced with the server at HH:MM`. After 2 misses the background waits 30 min, then 1, 2 and 4 h between tries. **Sync now** or opening the app tries at once.
  - **Once the PC is back:** the next sync (or opening the app) finds it, and the backoff starts over.
  - **In "Laptop directly"** while the PC is away, the background leaves the status as it was.
- **Automated?** Partly: `SyncPolicyTest` (backoff, quiet background) and `BackgroundRunTest` (one background run on demand). Real sleep needs a person.

### 7. Firewall prompt declined

- **Steps.**
  1. Delete Screen Time's rules: Windows Security, Firewall & network protection, **Allow an app through firewall**, remove **ScreenTime**. Or in an admin terminal: `Get-NetFirewallApplicationFilter -Program "<path to ScreenTime.exe>" | Get-NetFirewallRule | Remove-NetFirewallRule`.
  2. Start the app and set **Sync with** to Phone directly or Both. When Windows asks, press **Cancel**.
  3. **Sync now** on the phone.
- **Expected.**
  - **On the PC:** declining makes Windows add block rules, so after a few seconds the sync line shows the amber note that a rule blocks Screen Time, with what to change.
  - **On the phone:** `Laptop not found…`.
  - **After allowing it:** allow Screen Time on Private networks (the same **Allow an app** page). The note goes away the next time the listener starts (choose Server, then Both again), and the phone syncs.
- **Automated?** Partly: `test_firewall.py` covers the verdicts on made-up rules, plus one read-only query of the real firewall. The prompt needs a person.

### 8. Windows network profile: Public vs Private

- **Steps.**
  1. Settings, Network & internet, Wi-Fi, your network: set **Network profile type** to **Public**, with Screen Time allowed on Private only.
  2. Choose Server, then Both again in **Sync with**, then **Sync now** on the phone.
  3. Then set the profile to **Private** and repeat.
- **Expected.**
  - **Public:** the amber note says no rule allows Screen Time on this kind of network (Public), and the phone shows `Laptop not found…`.
  - **Private:** no note, and the phone syncs.
  - **The listener:** it listens on the private address in both cases; the firewall decides.
  - **What to recommend:** on your own home network, set the profile to Private. Ticking Public in the firewall rule also works, but opens the port on every public network the PC joins.
- **Automated?** Partly: `test_firewall.py` checks the verdict for a rule that doesn't cover the current profile. Switching the profile needs a person.

### 9. "Both" with one route down

- **Steps.** Phone and PC in **Both**.
  - **(a) Server down, laptop up:** stop the server, or disconnect the PC from Tailscale and the phone from it, keeping both on home Wi-Fi. Make a new phone period (use the phone for a minute), then **Sync now**.
  - **(b) Laptop down, server up:** quit the PC app, then **Sync now**.
  - Then bring the missing side back and **Sync now** again.
- **Expected.**
  - **(a):** `Laptop: synced at HH:MM. Server: Offline, will try again. …`. The new period shows on the PC at once.
  - **(b):** `Laptop: Laptop not found. … Server: synced at HH:MM.`
  - **When the missing side is back:** it catches up on its own. The period isn't sent twice, it isn't drawn twice in either timeline, and the PC's store still has one row for it (`"routes": ["direct", "server"]` in `remote_intervals.json`).
- **Automated?** Yes, with fakes: `BothModeTest` (`serverUnreachableLaptopStillSyncs`, `laptopUnreachableServerStillSyncs`) and `test_both.py` (server unreachable, listener down). The real outage needs a person.

### 10. Backup export and import after a reinstall

- **Steps.**
  1. On the phone, Settings, **Export backup**; save the file outside the app (Downloads).
  2. Uninstall the app. **This deletes its database**, so only do it on a phone whose data you have backed up, or with a test phone.
  3. Install the APK again, and set up Usage access and the server.
  4. **Import backup**, choosing the file.
  5. Pair with the laptop again, and **Sync now** in **Both**.
- **Expected.**
  - **After the import:** the phone's earlier periods and the laptop's periods are back in the timelines.
  - **"Own" rows:** the phone's own rows count as its own only for a backup of the same install. After a reinstall the install ID is new, so they are kept and shown but not sent.
  - **After pairing again:** the laptop and server send their rows again, and nothing is duplicated, because the IDs are stable.
  - **On the PC:** **Export backup…** then **Import backup…** gives `0` changed rows when nothing changed.
- **Automated?** Partly: `test_backup.py` covers export/import, newest wins and same-install rules. The reinstall needs a person.

## Database upgrade on a real copy

The phone's database moved from version 2 to 3 (a `routes` column). To check the upgrade on your own data without touching it:

1. **Copy, never move.** With the debug build installed, run `adb exec-out run-as com.screentime.mobile cat databases/screentime.db > <scratch folder>\screentime.db`. Use a scratch folder outside both repos; the copy holds your data.
2. **Copy into the app's files folder.** The test then opens only copies of it, named `upgrade-*.db`:
   ```
   adb push <scratch folder>\screentime.db /data/local/tmp/upgrade-source.db
   adb shell run-as com.screentime.mobile mkdir -p files
   adb shell run-as com.screentime.mobile cp /data/local/tmp/upgrade-source.db files/upgrade-source.db
   ```
   Use a version 2 copy (taken before installing a version 3 build), or the test checks nothing new: it asserts the source is version 2.
3. **Run the test:** `adb shell am instrument -w -e class com.screentime.mobile.DbUpgradeTest com.screentime.mobile.test/androidx.test.runner.AndroidJUnitRunner`. Without `upgrade-source.db` it uses a made-up version 2 database.
4. **Expected.** All pass:
   - Row counts per kind and owner are the same.
   - Every row keeps its ID, times, `updated_at`, owner and deletion.
   - App times and send records are the same.
   - Rows from other devices have routes 3 (both); the phone's own rows have 0.
   - Repeating the upgrade changes nothing.
   - An interrupted upgrade leaves version 2 as it was.
   - A partly upgraded file opens without loss.
   - A damaged or truncated file is kept as `upgrade-work.db.damaged-<time>`, and an empty database takes its place.
5. **Afterwards:** remove the copies with `adb shell run-as com.screentime.mobile rm files/upgrade-source.db` and `adb shell rm /data/local/tmp/upgrade-source.db`.

## The PC's remote store on a real copy

`remote_intervals.json` entries gained a sixth field, the routes. Entries without it are read as "both routes", and they are kept as they are until a route changes them, so there is no one-off conversion to go wrong. To check this on your own data:

1. Copy `%LOCALAPPDATA%\ScreenTime\remote_intervals.json` to a scratch folder.
2. Point `screentime.tracker.LOG_DIR` at `<scratch>\logs`. The store lives next to it, at `<scratch>\remote_intervals.json`.
3. Put the copy there. Check that `sync.load_remote()` returns the same IDs, kinds, times, `updated_at` and devices, and that `sync.entry_routes()` is `["server", "direct"]` for entries without routes.
4. Check that saving the store twice gives the same bytes, and that `apply_pulled` of the same rows by either route changes nothing.

`tests/test_store.py` does the same on made-up rows.
