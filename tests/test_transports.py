"""Tests for the transport layer of screentime.sync: per-transport progress, migrating state files
written before transports, the sync mode, and remembered deletions. Uses a scratch logs folder and
a test Credential Manager entry, never the real ones."""
import json
import os
import sys
import tempfile
import threading
import uuid
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from screentime import sync, tracker  # noqa: E402

SCRATCH = tempfile.mkdtemp(prefix="st_transports_")
tracker.LOG_DIR = os.path.join(SCRATCH, "logs")
os.makedirs(tracker.LOG_DIR)
sync.CRED_TARGET = "ScreenTimeSyncTEST"
sync.delete_token()


def check(cond, name):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(1)


class Fake:
    pushes, pulls = [], []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if self.path == "/v1/auth/login":
            return self.reply(200, {"session_token": "s", "expires_at": "x"})
        if self.path == "/v1/devices":
            return self.reply(201, {"device_id": 1, "name": body["name"], "type": "windows", "token": uuid.uuid4().hex})
        Fake.pushes.append(body)
        self.reply(200, {k: {"accepted": len(v), "ignored": 0, "rejected": []} for k, v in body.items()})

    def do_GET(self):
        Fake.pulls.append(parse_qs(urlsplit(self.path).query)["cursor"][0])
        self.reply(200, {"intervals": [], "app_usage_daily": [], "cursor": "50", "has_more": False})


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{server.server_address[1]}"

today = date.today()
old_day = today - timedelta(days=4)
LOG = {"total_seconds": 3600.0, "active_seconds": 600.0, "on_timeline": [[36000, 39600]],
       "active_timeline": [[36000, 36600]], "focus": {"Code.exe|DISPLAY1": 500.0}, "cursor": {}, "active": {}}
for d in (old_day, today):
    with open(tracker.log_path(d), "w") as f:
        json.dump(LOG, f)

# --- a state file from before transports is migrated without resending or re-pulling ---
install_id = str(uuid.uuid4())
sent = {key: fp for d in (old_day, today) for _, key, fp, _ in sync.day_rows(install_id, d, LOG)}
with open(sync.state_path(), "w") as f:
    json.dump({"install_id": install_id, "server": URL, "device_id": 1, "device_name": "TestPC",
               "sent": sent, "done_days": [old_day.isoformat()], "last_sync": None}, f)
p1 = str(uuid.uuid4())
with open(sync.remote_path(), "w") as f:
    json.dump({"cursor": "42", "intervals": {p1: ["phone_active", 1.0e9, 1.0e9 + 60, 1.0e9, 2]}}, f)

state = sync.load_state()
check("sent" not in state and "done_days" not in state, "old top-level progress fields are gone")
server_progress = state["transports"]["server"]
check(server_progress["sent"] == sent and server_progress["done_days"] == [old_day.isoformat()],
      "old sent/done_days became the server's progress")
check(server_progress["cursor"] == "42", "the pull cursor moved from remote_intervals.json into the state")
check(state["transports"]["direct"] == sync.fresh_progress(), "direct starts with fresh progress")
check(state["mode"] == "server", "mode defaults to server")
check(sync.load_remote()["intervals"][p1][0] == "phone_active" and sync.load_remote()["deleted"] == {},
      "old remote file still loads, with an empty deletion record")

sync.save_token("test-token")
n, rejected = sync.sync_once(today, LOG)
check(n == 0 and not rejected and Fake.pushes == [], "after migrating, nothing already sent goes out again")
check(sync.pull_once() == 0 and Fake.pulls == ["42"], "the first pull after migrating uses the old cursor")
with open(sync.state_path()) as f:
    saved = json.load(f)
check(saved["transports"]["server"]["cursor"] == "50" and "sent" not in saved, "state saved in the new form")
with open(sync.remote_path()) as f:
    check("cursor" not in json.load(f), "remote file no longer holds a cursor")
check(sync.load_state()["transports"]["server"]["cursor"] == "50", "migration doesn't run again over the new form")

# --- sync mode ---
sync.set_mode("both")
check(sync.get_mode() == "both", "mode saved")
try:
    sync.set_mode("carrier pigeon")
    check(False, "unknown mode rejected")
except ValueError:
    check(sync.get_mode() == "both", "unknown mode rejected, old one kept")
with open(sync.state_path()) as f:
    raw = json.load(f)
raw["mode"] = "bogus"
with open(sync.state_path(), "w") as f:
    json.dump(raw, f)
check(sync.get_mode() == "server", "a broken mode in the file falls back to server")
sync.set_mode("direct")
sync.connect(URL, "tester", "secret", "TestPC")
check(sync.get_mode() == "direct", "connect keeps the mode")
sync.disconnect()
check(sync.get_mode() == "direct", "disconnect keeps the mode")
check(sync.uses_server("server") and sync.uses_server("both") and not sync.uses_server("direct"), "uses_server")


class FakeTracker:
    day = today
    snapshot = staticmethod(lambda: LOG)


statuses = []
worker = sync.SyncWorker(FakeTracker, statuses.append)
sync.connect(URL, "tester", "secret", "TestPC")
Fake.pushes.clear()
Fake.pulls.clear()
check(worker.cycle() == sync.SYNC_EVERY and statuses[-1] == sync.DIRECT_MODE_STATUS, "direct mode: says server sync is off")
check(Fake.pushes == [] and Fake.pulls == [], "direct mode: the server isn't contacted")
sync.set_mode("both")
check(worker.cycle() == sync.SYNC_EVERY and statuses[-1].startswith("Synced at"), "both mode: syncs with the server")
check(len(Fake.pushes) == 1 and Fake.pulls == ["0"], "both mode: pushed and pulled like server mode")
sync.set_mode("server")
check(worker.cycle() == sync.SYNC_EVERY and statuses[-1].startswith("Synced at"), "server mode: syncs as before")
try:
    sync.sync_once(today, LOG, name="direct")
    check(False, "this PC doesn't push to the phone")
except sync.SyncError as e:
    check("connects to it" in str(e), f"direct transport: the phone connects to this PC instead ({e})")
try:
    sync.transport_for("nope", sync.load_state())
    check(False, "unknown transport")
except ValueError:
    check(True, "unknown transport name rejected")

# --- any transport: own progress and cursor (an in-memory one stands in for direct) ---
class MemoryTransport(sync.Transport):
    name = "direct"
    received, pages = [], []

    def send(self, rows):
        self.received.append(rows)
        return {}

    def fetch(self, cursor, limit):
        if cursor not in ("0", "m1"):
            raise sync.CursorUnknown("unknown cursor", 400)
        if cursor == "0":
            return self.pages[0], "m1", False
        return [], "m1", False


phone = str(uuid.uuid4())
MemoryTransport.pages = [[{"id": phone, "kind": "phone_active", "start_utc": "2026-11-10T08:00:00Z",
                           "end_utc": "2026-11-10T08:30:00Z", "updated_at": "2026-11-10T09:00:00Z"}]]
real_transport_for = sync.transport_for
sync.transport_for = lambda name, state: MemoryTransport() if name == "direct" else real_transport_for(name, state)
server_before = sync.load_state()["transports"]["server"]
n, _ = sync.sync_once(today, LOG, name="direct")
rows_today = len(sync.day_rows(sync.load_state()["install_id"], today, LOG))
check(n == 2 * rows_today and len(MemoryTransport.received) == 1, f"direct gets every row once ({n})")
check(all(r["updated_at"] for table in sync.TABLES for r in MemoryTransport.received[0][table]), "rows carry updated_at")
check(sync.sync_once(today, LOG, name="direct")[0] == 0, "direct: nothing changed, nothing sent")
check(sync.load_state()["transports"]["server"] == server_before, "sending to direct leaves the server's progress alone")
check(sync.pull_once(name="direct") == 1 and sync.load_state()["transports"]["direct"]["cursor"] == "m1",
      "direct pull stores the phone row and keeps its own cursor")
check(sync.load_state()["transports"]["server"]["cursor"] == server_before["cursor"], "server cursor untouched")
state = sync.load_state()
state["transports"]["direct"]["cursor"] = "lost"
state["transports"]["server"]["cursor"] = "77"
sync.save_state(state)
check(sync.pull_once(name="direct") == 0, "unknown cursor: direct pulls again from the start; its row is there already")
state = sync.load_state()
check(state["transports"]["direct"]["cursor"] == "m1" and state["transports"]["server"]["cursor"] == "77",
      "only that route's cursor starts over; the server's stays")
check(phone in sync.load_remote()["intervals"], "the store isn't emptied")
sync.transport_for = real_transport_for

# --- remembered deletions ---
remote = sync.empty_remote()
row = {"id": "x", "kind": "sleep", "start_utc": "2026-11-10T00:00:00Z", "end_utc": "2026-11-10T06:00:00Z",
       "updated_at": "2026-11-10T07:00:00Z"}
check(sync.apply_pulled(remote, row), "new row stored")
check(sync.apply_pulled(remote, dict(row, updated_at="2026-11-10T08:00:00Z", deleted_at="2026-11-10T08:00:00Z")),
      "newer delete removes it")
check("x" in remote["deleted"] and "x" not in remote["intervals"], "the deletion is remembered")
check(not sync.apply_pulled(remote, dict(row, updated_at="2026-11-10T07:30:00Z")),
      "an older copy doesn't bring a deleted row back")
check(not sync.apply_pulled(remote, dict(row, updated_at="2026-11-10T08:00:00Z")), "same time: the deletion stays")
check(sync.apply_pulled(remote, dict(row, updated_at="2026-11-10T09:00:00Z")) and "x" not in remote["deleted"],
      "a newer copy brings it back and clears the deletion")
check(not sync.apply_pulled(remote, {"id": "y", "kind": "sleep", "updated_at": "2026-11-10T09:00:00Z",
                                     "deleted_at": "2026-11-10T09:00:00Z"}) and remote["deleted"]["y"][1] is None,
      "a deletion without times is remembered too")

sync.disconnect()
sync.delete_token()
server.shutdown()
server.server_close()
print("ALL TRANSPORT CHECKS PASSED")
