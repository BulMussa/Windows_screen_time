"""Tests for screentime.sync against a fake server. Uses a scratch logs folder and a test
Credential Manager entry, never the real ones."""
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from screentime import sync, tracker  # noqa: E402

SCRATCH = tempfile.mkdtemp(prefix="st_test_")
tracker.LOG_DIR = os.path.join(SCRATCH, "logs")
os.makedirs(tracker.LOG_DIR)
sync.CRED_TARGET = "ScreenTimeSyncTEST"
sync.delete_token()

passed = []


def check(cond, name):
    passed.append(name)
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(1)


class Fake:
    sessions, device_tokens = set(), {}
    intervals, apps = {}, {}
    pushes = 0
    revoked = False
    login_429 = False
    skew = timedelta(0)  # the server's clock runs this far behind real time


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, obj, headers=()):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/v1/sync/pull"):
            return self.reply(200, {"intervals": [], "app_usage_daily": [], "cursor": "0", "has_more": False})
        self.reply(404, {"detail": "Not Found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        bearer = self.headers.get("Authorization", "")[7:]
        if self.path == "/v1/auth/login":
            if Fake.login_429:
                return self.reply(429, {"detail": "too many failed logins"}, [("Retry-After", "900")])
            if body == {"name": "tester", "password": "secret"}:
                token = uuid.uuid4().hex
                Fake.sessions.add(token)
                return self.reply(200, {"session_token": token, "expires_at": "2099-01-01T00:00:00Z"})
            return self.reply(401, {"detail": "invalid name or password"})
        if self.path == "/v1/devices":
            if bearer not in Fake.sessions:
                return self.reply(401, {"detail": "invalid or expired token"})
            token = uuid.uuid4().hex
            Fake.device_tokens[token] = 1
            return self.reply(201, {"device_id": 1, "name": body["name"], "type": body["type"], "token": token})
        if self.path == "/v1/sync/push":
            if bearer not in Fake.device_tokens or Fake.revoked:
                return self.reply(401, {"detail": "invalid or expired token"})
            Fake.pushes += 1
            return self.push(body)
        self.reply(404, {"detail": "Not Found"})

    def push(self, body):
        def ts(text):
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            assert moment.tzinfo, "timestamp needs an offset"
            return moment
        now = datetime.now(timezone.utc) - Fake.skew
        out = {"intervals": {"accepted": 0, "ignored": 0, "rejected": []},
               "app_usage_daily": {"accepted": 0, "ignored": 0, "rejected": []}}
        for row in body["intervals"]:
            uuid.UUID(row["id"])
            assert row["kind"] in ("pc_on", "pc_active", "phone_active", "sleep")
            assert row["source"] in ("tracker", "health_connect", "manual")
            assert ts(row["end_utc"]) > ts(row["start_utc"])
            self.store(out["intervals"], Fake.intervals, row["id"], row, row["id"], now, ts)
        for row in body["app_usage_daily"]:
            assert 0 <= row["focus_s"] <= 90000 and 0 <= row["cursor_s"] <= 90000 and 0 <= row["active_s"] <= 90000
            assert 1 <= len(row["app"]) <= 255
            key = f"{row['local_date']}|{row['app']}|{row['monitor'] or ''}"
            self.store(out["app_usage_daily"], Fake.apps, key, row, key, now, ts)
        self.reply(200, out)

    def store(self, result, table, key, row, ident, now, ts):
        if ts(row["updated_at"]) > now + timedelta(hours=1):
            result["rejected"].append({"id": ident, "reason": "updated_at_in_future"})
        elif key in table and ts(row["updated_at"]) <= ts(table[key]["updated_at"]):
            result["ignored"] += 1
        else:
            table[key] = row
            result["accepted"] += 1


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{server.server_address[1]}"

# --- logs ---
today = date.today()
days = [today - timedelta(days=5), today - timedelta(days=1), today]


def make_log(extra_active=0, extra_app=0):
    return {"total_seconds": 14400.0, "active_seconds": 6900.0,
            "on_timeline": [[32400, 46800]],
            "active_timeline": [[33000, 40000 + extra_active], [41000, 45000]],
            "focus": {"Code.exe|DISPLAY1 (primary)": 1500.4 + extra_app, "chrome.exe|DISPLAY2": 300.0},
            "cursor": {"Code.exe|DISPLAY1 (primary)": 1200.0},
            "active": {"Code.exe|DISPLAY1 (primary)": 1480.0, "chrome.exe|DISPLAY2": 305.0}}


def write_log(day, log):
    with open(os.path.join(tracker.LOG_DIR, f"{day.isoformat()}.json"), "w") as f:
        json.dump(log, f)


for d in days:
    write_log(d, make_log())

# --- address checks and connecting ---
for bad in ("http://example.com", "ftp://x", "", "   "):
    try:
        sync.check_server(bad)
        check(False, f"rejects {bad!r}")
    except sync.SyncError:
        check(True, f"rejects address {bad!r}")
check(sync.check_server("https://nextcloud.example:8443/") == "https://nextcloud.example:8443", "https address cleaned")
check(sync.check_server(URL + "/") == URL, "http to localhost allowed")

try:
    sync.connect(URL, "tester", "wrong", "TestPC")
    check(False, "wrong password fails")
except sync.SyncError as e:
    check(e.status == 401 and "invalid name or password" in str(e), "wrong password gives 401 message")
check(sync.load_token() is None and not sync.connected(), "nothing saved after a failed login")

Fake.login_429 = True
try:
    sync.connect(URL, "tester", "secret", "TestPC")
    check(False, "429 fails")
except sync.SyncError as e:
    check(e.status == 429 and "15 min" in str(e), f"429 message names the wait ({e})")
Fake.login_429 = False

sync.connect(URL, "tester", "secret", "TestPC")
token = sync.load_token()
with open(sync.state_path()) as f:
    state_text = f.read()
check(token in Fake.device_tokens, "device token saved in Credential Manager")
check("secret" not in state_text and token not in state_text, "state file holds neither password nor token")
check(sync.connected(), "connected() true")
state = sync.load_state()
check(state["device_id"] == 1 and state["server"] == URL and state["install_id"], "state has device and install id")
install_id = state["install_id"]

# --- first sync, in batches ---
sync.BATCH = 4
sent, rejected = sync.sync_once(today, make_log())
check(sent == 15 and not rejected, f"first sync sends all 15 rows ({sent})")
check(Fake.pushes == 4, f"15 rows in batches of 4 -> 4 requests ({Fake.pushes})")
check(len(Fake.intervals) == 9 and len(Fake.apps) == 6, "server has 9 intervals and 6 app rows")

midnight = datetime.combine(today, datetime.min.time())
expect_start = (midnight + timedelta(seconds=32400)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
pc_on_today = [r for r in Fake.intervals.values() if r["kind"] == "pc_on" and r["start_utc"] == expect_start]
check(len(pc_on_today) == 1, "09:00 local converted to UTC correctly")
want_id = str(uuid.uuid5(sync.INTERVAL_NAMESPACE, f"{install_id}|pc_on|{today}|32400"))
check(want_id in Fake.intervals, "interval ID is repeatable from install, kind, day and start")
code = Fake.apps[f"{today}|Code.exe|DISPLAY1 (primary)"]
check((code["focus_s"], code["cursor_s"], code["active_s"]) == (1500, 1200, 1480), "app seconds merged and rounded")
check(Fake.apps[f"{today}|chrome.exe|DISPLAY2"]["cursor_s"] == 0, "missing kind counts as 0")
check(all("device_id" not in r and "user_id" not in r for r in Fake.intervals.values()), "no owner fields sent")

# --- nothing changed: nothing sent ---
before = Fake.pushes
sent, rejected = sync.sync_once(today, make_log())
check(sent == 0 and Fake.pushes == before, "second sync sends nothing and makes no request")
done = sync.load_state()["transports"]["server"]["done_days"]
check(str(days[0]) in done, "day older than yesterday marked done")
check(str(days[1]) not in done and str(today) not in done,
      "yesterday and today not marked done")

reads = []
real_read = sync.read_log
sync.read_log = lambda d: (reads.append(d), real_read(d))[1]
sync.sync_once(today, make_log())
check(days[0].isoformat() not in reads, "done day is not read again")
sync.read_log = real_read

# --- a change sends only what changed ---
time.sleep(1.1)  # updated_at has one-second precision and must be strictly newer
sent, rejected = sync.sync_once(today, make_log(extra_active=600, extra_app=60))
check(sent == 2, f"grown interval + changed app row only ({sent})")
grown = [r for r in Fake.intervals.values() if r["start_utc"] == (midnight + timedelta(seconds=33000)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") and r["kind"] == "pc_active"]
check(len(grown) == 1 and len(Fake.intervals) == 9, "same interval ID updated, not duplicated")
check(Fake.apps[f"{today}|Code.exe|DISPLAY1 (primary)"]["focus_s"] == 1560, "changed app seconds replaced")

# --- clock ahead of the server: rejected, retried later ---
time.sleep(1.1)
Fake.skew = timedelta(hours=3)
sent, rejected = sync.sync_once(today, make_log(extra_active=900, extra_app=60))
check(sent == 0 and len(rejected) == 1 and "updated_at_in_future" in rejected.values(), "future rows rejected, not marked sent")
Fake.skew = timedelta(0)
sent, rejected = sync.sync_once(today, make_log(extra_active=900, extra_app=60))
check(sent == 1 and not rejected, "rejected row sent once the clock matches")

# --- worker cycles ---
class FakeTracker:
    day = today
    snapshot = staticmethod(lambda: make_log(extra_active=900, extra_app=60))


statuses = []
worker = sync.SyncWorker(FakeTracker, statuses.append)
check(worker.cycle() == sync.SYNC_EVERY and statuses[-1].startswith("Synced at"), "worker: success status")
Fake.revoked = True
time.sleep(1.1)
FakeTracker.snapshot = staticmethod(lambda: make_log(extra_active=1200, extra_app=60))
check(worker.cycle() == sync.REVOKED_RETRY and "doesn't accept" in statuses[-1], "worker: 401 stops retrying quickly")
Fake.revoked = False
server.shutdown()
server.server_close()
check(worker.cycle() == sync.RETRY_AFTER_FAILURE and statuses[-1].startswith("Offline"), "worker: offline retries in a minute")
try:
    sync.sync_once(today, make_log(extra_active=1200, extra_app=60))
    check(False, "offline raises")
except sync.SyncError as e:
    check(e.status is None, "offline error has no status")

# --- disconnect ---
sync.disconnect()
st = sync.load_state()
check(sync.load_token() is None and st["server"] is None and st["install_id"] == install_id and not st["transports"]["server"]["sent"],
      "disconnect clears token and server, keeps install id")
check(worker.cycle() == sync.SYNC_EVERY and statuses[-1] == "Not connected", "worker: not connected")

# --- tracker records active periods ---
tracker.idle_seconds = lambda: 0
tracker.screen_locked = lambda: False
tracker.sample = lambda: (("Code.exe", "DISPLAY1"), ("Code.exe", "DISPLAY1"))
write_log(today, {"total_seconds": 1.0, "on_timeline": [[0, 10]], "active_timeline": [[100, 200]]})
t = tracker.Tracker()
for _ in range(4):
    t.tick()
    time.sleep(1.0)
spans = t.snapshot()["active_timeline"]
secs = (datetime.now() - datetime.combine(t.day, datetime.min.time())).total_seconds()
check(len(spans) == 2 and spans[0] == [100, 200] and spans[1][1] - spans[1][0] >= 3 and abs(spans[1][1] - secs) < 5,
      f"tracker adds one growing active period at the current time {spans}")
t.save()
with open(tracker.log_path(t.day)) as f:
    check("active_timeline" in json.load(f), "active_timeline saved in the log")
tracker.idle_seconds = lambda: 999
n = len(t.snapshot()["active_timeline"])
t.tick()
check(len(t.snapshot()["active_timeline"]) == n and t.snapshot()["active_timeline"][-1][1] <= spans[-1][1] + 2,
      "idle time is not recorded as active")
write_log(days[0], {"total_seconds": 1.0, "on_timeline": [[0, 10]]})
check(tracker.load(days[0])["active_timeline"] == [], "older logs without active_timeline load fine")

print(f"\nALL {len(passed)} CHECKS PASSED")
