"""Tests for pulling phone/sleep data, the day spans and the three-row timeline. Scratch logs and a
test credential only."""
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from screentime import app as app_mod, sync, timeline, tracker  # noqa: E402

SCRATCH = tempfile.mkdtemp(prefix="st_pull_")
tracker.LOG_DIR = os.path.join(SCRATCH, "logs")
os.makedirs(tracker.LOG_DIR)
sync.CRED_TARGET = "ScreenTimeSyncTEST"
sync.delete_token()
app_mod.autostart_enabled = lambda: False
app_mod.set_autostart = lambda enabled: None


def check(cond, name):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(1)


def utc(day, hour, minute=0):
    return datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute) \
        .astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Fake:
    rows = []  # (seq, interval dict), in the order the "server" saved them
    requests = []


def put(seq, **row):
    row.setdefault("deleted_at", None)
    row.setdefault("source", "tracker")
    row.setdefault("device_id", 2)
    row.setdefault("updated_at", "2026-11-17T05:00:00Z")
    Fake.rows = [r for r in Fake.rows if r[1]["id"] != row["id"]]
    Fake.rows.append((seq, row))


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
        self.reply(200, {k: {"accepted": len(v), "ignored": 0, "rejected": []} for k, v in body.items()})

    def do_GET(self):
        parts = urlsplit(self.path)
        q = parse_qs(parts.query)
        Fake.requests.append(self.path)
        cursor, limit = q["cursor"][0], int(q["limit"][0])
        top = max([s for s, _ in Fake.rows] or [0])
        if not cursor.isdigit() or int(cursor) > top:
            return self.reply(400, {"detail": "invalid cursor"})
        newer = sorted((r for r in Fake.rows if r[0] > int(cursor)), key=lambda r: r[0])
        page = newer[:limit]
        self.reply(200, {"intervals": [r for _, r in page], "app_usage_daily": [],
                         "cursor": str(page[-1][0]) if page else cursor, "has_more": len(newer) > limit})


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{server.server_address[1]}"

today = date.today()
yesterday = today - timedelta(days=1)
sid = lambda: str(uuid.uuid4())
ids = {k: sid() for k in ("p1", "p2", "p3", "sleep", "own", "gone")}

# --- parsing ---
check(sync.parse_utc("2026-11-10T04:05:00Z") == datetime(2026, 11, 10, 4, 5, tzinfo=timezone.utc).timestamp(), "parse_utc plain")
check(abs(sync.parse_utc("2026-11-17T06:03:39.928605Z") % 1 - 0.928605) < 1e-3, "parse_utc microseconds")
check(abs(sync.parse_utc("2026-11-17T06:03:39.5Z") % 1 - 0.5) < 1e-3, "parse_utc short fraction")
for bad in ("2026-11-10T04:05:00", "2026-11-10 04:05:00Z", "x"):
    try:
        sync.parse_utc(bad)
        check(False, f"parse_utc rejects {bad!r}")
    except ValueError:
        check(True, f"parse_utc rejects {bad!r}")

# --- pull ---
put(1, id=ids["p1"], kind="phone_active", start_utc=utc(today, 7, 30), end_utc=utc(today, 8, 10))
put(2, id=ids["p2"], kind="phone_active", start_utc=utc(today, 8, 5), end_utc=utc(today, 8, 30))  # overlaps p1
put(3, id=ids["p3"], kind="phone_active", start_utc=utc(today, 12, 15), end_utc=utc(today, 12, 40))
put(4, id=ids["sleep"], kind="sleep", source="health_connect", start_utc=utc(yesterday, 23, 10), end_utc=utc(today, 6, 50))
put(5, id=ids["own"], kind="pc_active", device_id=1, start_utc=utc(today, 9, 0), end_utc=utc(today, 9, 30))
put(6, id=ids["gone"], kind="phone_active", start_utc=utc(today, 15, 0), end_utc=utc(today, 15, 20))
put(7, id=ids["gone"], kind="phone_active", start_utc=utc(today, 15, 0), end_utc=utc(today, 15, 20),
    deleted_at="2026-11-17T05:00:01Z", updated_at="2026-11-17T05:00:01Z")

check(sync.load_remote() == {"intervals": {}, "deleted": {}}, "no remote file: empty store")
sync.connect(URL, "tester", "secret", "TestPC")
sync.PULL_LIMIT = 2
changed = sync.pull_once()
remote = sync.load_remote()
check(changed == 4 and set(remote["intervals"]) == {ids["p1"], ids["p2"], ids["p3"], ids["sleep"]},
      f"pull stores phone and sleep, skips PC rows and deleted ({changed})")
cursor = lambda: sync.load_state()["transports"]["server"]["cursor"]
check(cursor() == "7" and len(Fake.requests) == 3, f"paged through 6 rows in 2-row pages ({len(Fake.requests)} requests)")
check(ids["own"] not in remote["intervals"], "own PC rows are not stored")
before = len(Fake.requests)
check(sync.pull_once() == 0 and len(Fake.requests) == before + 1, "nothing new: one request, nothing changed")

# newer wins, older ignored, delete removes
put(8, id=ids["p3"], kind="phone_active", start_utc=utc(today, 12, 15), end_utc=utc(today, 12, 50), updated_at="2026-11-17T05:10:00Z")
check(sync.pull_once() == 1 and sync.load_remote()["intervals"][ids["p3"]][2] == sync.parse_utc(utc(today, 12, 50)), "newer update replaces")
put(9, id=ids["p3"], kind="phone_active", start_utc=utc(today, 12, 15), end_utc=utc(today, 12, 20), updated_at="2026-11-17T05:05:00Z")
check(sync.pull_once() == 0 and sync.load_remote()["intervals"][ids["p3"]][2] == sync.parse_utc(utc(today, 12, 50)), "older update ignored")
put(10, id=ids["p3"], kind="phone_active", start_utc=utc(today, 12, 15), end_utc=utc(today, 12, 50),
    deleted_at="2026-11-17T05:20:00Z", updated_at="2026-11-17T05:20:00Z")
check(sync.pull_once() == 1 and ids["p3"] not in sync.load_remote()["intervals"], "delete removes the interval")
put(11, id=ids["p3"], kind="phone_active", start_utc=utc(today, 12, 15), end_utc=utc(today, 12, 40), updated_at="2026-11-17T05:30:00Z")
check(sync.pull_once() == 1 and ids["p3"] in sync.load_remote()["intervals"], "newer undelete brings it back")

# the server doesn't know our cursor (e.g. it was restored from an older backup)
state = sync.load_state()
state["transports"]["server"]["cursor"] = "999"
sync.save_state(state)
sync.pull_once()
r = sync.load_remote()
check(cursor() == "11" and len(r["intervals"]) == 4, "unknown cursor: starts over from 0")

# --- day spans ---
r = sync.load_remote()
first = lambda h, m=0: h * 3600 + m * 60
check(timeline.day_spans(r, today, "phone_active") == [[first(7, 30), first(8, 30)], [first(12, 15), first(12, 40)]],
      "overlapping phone intervals merge")
check(timeline.day_spans(r, today, "sleep") == [[0, first(6, 50)]], "sleep from last night is cut at midnight (morning part)")
check(timeline.day_spans(r, yesterday, "sleep") == [[first(23, 10), 86400]], "sleep cut at midnight (evening part)")
check(timeline.day_spans(r, today - timedelta(days=3), "sleep") == [], "no spans on other days")
check(timeline.total([[0, 10], [20, 25]]) == 15, "total")

# --- disconnect and reconnect clear the store ---
sync.disconnect()
check(not os.path.exists(sync.remote_path()) and sync.load_remote()["intervals"] == {}, "disconnect clears pulled data")
sync.connect(URL, "tester", "secret", "TestPC")
check(not os.path.exists(sync.remote_path()), "connect starts with an empty store")

# --- the worker pulls after pushing ---
class FakeTracker:
    day = today
    snapshot = staticmethod(lambda: {"on_timeline": [], "active_timeline": [], "focus": {}, "cursor": {}, "active": {}})


statuses = []
worker = sync.SyncWorker(FakeTracker, statuses.append)
check(worker.cycle() == sync.SYNC_EVERY and statuses[-1].startswith("Synced at"), "worker cycle ok")
check(len(sync.load_remote()["intervals"]) == 4, "worker cycle pulled the data")

# --- the window ---
with open(os.path.join(tracker.LOG_DIR, f"{today.isoformat()}.json"), "w") as f:
    json.dump({"total_seconds": 20000.0, "active_seconds": 11100.0, "first_seen": f"{today}T03:19:00",
               "last_seen": f"{today}T04:00:00", "stopped": True,
               "on_timeline": [[first(3, 19), first(4, 30)]],
               "active_timeline": [[first(3, 25), first(3, 55)], [first(4, 5), first(4, 20)]],
               "focus": {}, "cursor": {}, "active": {}}, f)
old = today - timedelta(days=2)
with open(os.path.join(tracker.LOG_DIR, f"{old.isoformat()}.json"), "w") as f:
    json.dump({"total_seconds": 100.0, "active_seconds": 50.0, "on_timeline": [[first(9), first(10)]],
               "focus": {}, "cursor": {}, "active": {}}, f)

root = app_mod.tk.Tk()
root.geometry("+80+60")
app = app_mod.App(root, tracker.Tracker())
pump = lambda s: [(root.update(), time.sleep(0.05)) for _ in range(int(s / 0.05))]
pump(1)
app.tabs.select(0)
pump(1)
check(app.syncer is not None, "app up")

rows = app.timeline_rows(today, app.tracker.snapshot(), True)
names = [r[0] for r in rows]
check(names == ["Laptop", "Phone", "Sleep"], "three rows in order")
check([p[0] for p in rows[0][3]] == ["Laptop \u00b7 PC on"], "laptop: PC on only")
check(rows[1][1] == "1h 25m" and rows[2][1] == "6h 50m", f"summaries: {rows[0][1]} / {rows[1][1]} / {rows[2][1]}")
check(rows[0][1].startswith("1h 1") and rows[0][1].endswith(" on"), f"laptop summary is the PC-on total ({rows[0][1]})")

# hover a phone period
item = next(p for p in app.periods if p[3].startswith("Phone"))
x1, y1, x2, y2 = app.timeline.bbox(item[0])
Event = type("E", (), {})
ev = Event()
ev.x, ev.y, ev.x_root, ev.y_root = (x1 + x2) / 2, (y1 + y2) / 2, 400, 400
app.hover_timeline(ev)
check(app.tip is not None and app.tip_labels[1].cget("text") == "Phone \u00b7 Active", "hovering a phone period shows its tooltip")
check(app.timeline.itemcget(item[0], "fill") == item[5], "hovered period highlighted")
ev.y = y1 - 20
ev.y += 0
app.hover_timeline(None)
check(app.tip is None, "tooltip gone when the mouse leaves")
# a laptop period
on = next(p for p in app.periods if p[3] == "Laptop \u00b7 PC on")
x1, y1, x2, y2 = app.timeline.bbox(on[0])
ev.x, ev.y = (x1 + x2) / 2, (y1 + y2) / 2
app.hover_timeline(ev)
check(app.tip_labels[1].cget("text") == "Laptop \u00b7 PC on", "hovering a laptop period shows PC on")
app.hover_timeline(None)

# old day: laptop only, solid blue, other rows empty with no hint when connected
old_rows = app.timeline_rows(old, app_mod.read_log(old), False)
check(old_rows[0][3] == [("Laptop \u00b7 PC on", app_mod.TIMELINE_ON, app_mod.TIMELINE_HOVER, [[first(9), first(10)]])],
      "old day without active periods: solid PC on")
check(app.timeline_rows(today - timedelta(days=9), {}, False) is None, "a day with nothing: no data")

# not connected: hints on the empty rows
sync.disconnect()
rows = app.timeline_rows(today, app.tracker.snapshot(), True)
check(rows[1][2].startswith("Not connected") and rows[1][3][0][3] == [], "disconnected: hint, no phone data")
pump(1)

app.quit()
sync.delete_token()
server.shutdown()
server.server_close()
print("ALL PULL/WINDOW CHECKS PASSED")
