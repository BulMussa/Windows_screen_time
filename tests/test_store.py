"""Tests for the pulled-rows store (remote_intervals.json) across formats: entries saved before routes were
kept, saved and pulled again unchanged; and a damaged file (garbage, cut short, the wrong shape), which is
kept aside while every cursor starts over and the server sends its rows again. A fake server on 127.0.0.1,
a scratch logs folder and a test credential."""
import glob
import json
import os
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from screentime import sync, tracker  # noqa: E402

SCRATCH = tempfile.mkdtemp(prefix="st_store_")
tracker.LOG_DIR = os.path.join(SCRATCH, "logs")
os.makedirs(tracker.LOG_DIR)
sync.CRED_TARGET = "ScreenTimeSyncTEST"
sync.delete_token()


def check(cond, name):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(1)


ROWS = []  # what the fake server's feed holds; the cursor is an index into it


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
        q = parse_qs(urlsplit(self.path).query)
        cursor, limit = int(q["cursor"][0]), int(q["limit"][0])
        if cursor > len(ROWS):
            return self.reply(400, {"detail": "invalid cursor"})
        page = ROWS[cursor:cursor + limit]
        self.reply(200, {"intervals": page, "app_usage_daily": [], "cursor": str(cursor + len(page)),
                         "has_more": cursor + len(page) < len(ROWS)})


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
sync.connect(f"http://127.0.0.1:{server.server_address[1]}", "tester", "secret", "TestPC")


def row(n, deleted=False):
    stamp = f"2026-11-1{n % 10}T08:00:00Z"
    return {"id": f"row-{n}", "kind": "phone_active" if n % 3 else "sleep", "start_utc": f"2026-11-1{n % 10}T06:00:00Z",
            "end_utc": f"2026-11-1{n % 10}T07:00:00Z", "source": "tracker", "device_id": 2,
            "updated_at": stamp, "deleted_at": stamp if deleted else None}


ROWS[:] = [row(n, deleted=n == 4) for n in range(1, 7)]


def cursor():
    return sync.load_state()["transports"]["server"]["cursor"]


def write(raw):
    with open(sync.remote_path(), "w" if isinstance(raw, str) else "wb") as f:
        f.write(raw)


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def damaged_files():
    return sorted(glob.glob(sync.remote_path() + ".damaged-*"))


# --- the store as pulled now ---
check(sync.pull_once() == 5 and cursor() == "6", "pulled six rows (five live, one deletion)")
current = sync.load_remote()
check(all(len(e) == 6 for s in ("intervals", "deleted") for e in current[s].values()), "entries carry routes")

# --- the older format: the same rows without routes ---
old = {s: {i: e[:5] for i, e in current[s].items()} for s in ("intervals", "deleted")}
write(json.dumps(old))
loaded = sync.load_remote()
check({s: {i: e[:5] for i, e in loaded[s].items()} for s in old} == old,
      "older entries load with their IDs, kinds, times, updated_at and devices")
check(all(sync.entry_routes(e) == ["server", "direct"] for s in old for e in loaded[s].values()),
      "older entries count as both routes")
before = read_bytes(sync.remote_path())
sync.save_remote(sync.load_remote())
sync.save_remote(sync.load_remote())
check(read_bytes(sync.remote_path()) == before, "saved twice unchanged: the same file")
state = sync.load_state()
state["transports"]["server"]["cursor"] = "0"
sync.save_state(state)
check(sync.pull_once() == 0, "pulled again from the start: nothing changes")
check(json.loads(read_bytes(sync.remote_path())) == old, "older entries stay as they were (still counted as both)")
sync.forget_server_route()
check(sync.load_remote() == old, "forgetting the server keeps older entries (their route isn't known)")
check(damaged_files() == [], "a good file is never set aside")

# --- damaged files ---
good = json.dumps(sync.load_remote())
CASES = [("garbage", b"\x00\xff\x13 not json at all"), ("cut short", good[: len(good) // 2]),
         ("a list", "[]"), ("intervals not a map", '{"intervals": [], "deleted": {}}'),
         ("an entry too short", '{"intervals": {"x": ["phone_active", 1, 2]}, "deleted": {}}'),
         ("an entry not a list", '{"intervals": {}, "deleted": {"x": 5}}')]
for label, raw in CASES:
    for path in damaged_files():
        os.remove(path)
    write(raw)
    raw_bytes = read_bytes(sync.remote_path())
    state = sync.load_state()
    state["transports"]["server"]["cursor"] = "6"
    sync.save_state(state)
    check(sync.load_remote() == sync.empty_remote(), f"{label}: read as empty, no crash")
    aside = damaged_files()
    check(len(aside) == 1 and read_bytes(aside[0]) == raw_bytes, f"{label}: the damaged file is kept as it was")
    check(cursor() == "0", f"{label}: the server's cursor starts over")
    check(sync.pull_once() == 5 and len(sync.load_remote()["intervals"]) == 5 and len(sync.load_remote()["deleted"]) == 1,
          f"{label}: the next pull brings every row back")
    check(damaged_files() == aside, f"{label}: the damaged file isn't overwritten")

# --- the phone's app times (phone_app_usage.json): a damaged file is kept aside too ---
from screentime import peer  # noqa: E402

check(os.path.dirname(peer.usage_path()).startswith(SCRATCH), "the usage file is in the scratch folder")
os.makedirs(os.path.dirname(peer.usage_path()), exist_ok=True)
with open(peer.usage_path(), "w") as f:
    f.write('{"2026-11-10|app": {"focus_s": 5}}')
check(peer.load_usage() == {"2026-11-10|app": {"focus_s": 5}}, "usage: a good file loads")
with open(peer.usage_path(), "w") as f:
    f.write('{"cut short')
check(peer.load_usage() == {}, "usage: a damaged file reads as empty")
kept = glob.glob(peer.usage_path() + ".damaged-*")
check(len(kept) == 1 and read_bytes(kept[0]) == b'{"cut short' and not os.path.exists(peer.usage_path()),
      "usage: the damaged file is kept aside, not overwritten later")

sync.disconnect()
sync.delete_token()
server.shutdown()
server.server_close()
print("ALL STORE CHECKS PASSED")
