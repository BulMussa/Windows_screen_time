"""Tests for "both": rows from the phone arriving by both routes, pulled from a fake server (HTTP on
127.0.0.1) and pushed by a fake phone to the real phone-sync listener (HTTPS on 127.0.0.1). Overlapping
and conflicting rows, in both orders: the newer copy wins, a newer deletion wins, the same row is applied
once. Per-route cursors and resets, one route down, and the timelines. Uses a scratch logs folder and test
Credential Manager entries; nothing listens on the network."""
import copy
import http.client
import json
import os
import ssl
import sys
import tempfile
import threading
import uuid
from datetime import date, datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from screentime import peer, sync, timeline, tracker  # noqa: E402

SCRATCH = tempfile.mkdtemp(prefix="st_both_")
tracker.LOG_DIR = os.path.join(SCRATCH, "logs")
os.makedirs(tracker.LOG_DIR)
sync.CRED_TARGET = "ScreenTimeSyncTEST"
peer.PAIRING_TARGET, peer.KEY_TARGET = "ScreenTimePhonePairingTEST", "ScreenTimePhoneKeyTEST"
for target in (sync.CRED_TARGET, peer.PAIRING_TARGET, peer.KEY_TARGET):
    sync.cred_delete(target)


def check(cond, name):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(1)


# --- a fake server: a change feed with numbered rows; a cursor it doesn't know gets 400 ---
class Server:
    rows = []  # (seq, row)
    seq = 0
    down = False


def server_put(row):
    Server.seq += 1
    Server.rows = [r for r in Server.rows if r[1]["id"] != row["id"]] + [(Server.seq, dict(row))]


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
        cursor, limit = q["cursor"][0], int(q["limit"][0])
        if not cursor.isdigit() or int(cursor) > Server.seq:
            return self.reply(400, {"detail": "invalid cursor"})
        newer = sorted((r for r in Server.rows if r[0] > int(cursor)), key=lambda r: r[0])
        page = newer[:limit]
        self.reply(200, {"intervals": [r for _, r in page], "app_usage_daily": [],
                         "cursor": str(page[-1][0]) if page else cursor, "has_more": len(newer) > limit})


http_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=http_server.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{http_server.server_address[1]}"
sync.connect(URL, "tester", "secret", "TestPC")
sync.set_mode("both")

# --- the real listener and a fake phone pushing to it ---
EMPTY_LOG = {"total_seconds": 0.0, "active_seconds": 0.0, "on_timeline": [], "active_timeline": [],
             "focus": {}, "cursor": {}, "active": {}}
peer.set_enabled(True)
listener = peer.PeerListener(lambda: (date.today(), EMPTY_LOG), host="127.0.0.1", port=0, discovery_port=0)
listener.start()
check(listener.ready.wait(10) and listener.address, "listener up on 127.0.0.1")
CTX = ssl.create_default_context(cafile=peer.certificate()[0])
CTX.check_hostname = False


def phone_call(method, path, key, body=None):
    """What the phone does: a nonce, then the request with its HMAC proof."""
    conn = http.client.HTTPSConnection(*listener.address, context=CTX, timeout=5)
    try:
        conn.request("GET", "/peer/nonce")
        nonce = json.loads(conn.getresponse().read())["nonce"]
    finally:
        conn.close()
    data = json.dumps(body).encode() if body is not None else b""
    conn = http.client.HTTPSConnection(*listener.address, context=CTX, timeout=5)
    try:
        conn.request(method, path, body=data if method == "POST" else None,
                     headers={"Authorization": f"HMAC {nonce}:{peer.request_mac(key, method, path, nonce, data)}"})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()


code = peer.start_pairing(["127.0.0.1"], listener.address[1])
status, answer = phone_call("POST", "/peer/pair", code["secret"], {"device_id": "phone-1", "name": "Pixel"})
KEY = answer["key"]
check(status == 200 and phone_call("GET", "/peer/hello", KEY)[0] == 200, "phone paired")


def phone_push(*rows):
    status, result = phone_call("POST", "/peer/push", KEY, {"intervals": list(rows), "app_usage_daily": []})
    assert status == 200, result
    return result["intervals"]


# --- rows; the phone's IDs are deterministic, so the same period has the same ID on both routes ---
DAY = date.today() - timedelta(days=1)
MIDNIGHT = datetime.combine(DAY, time.min)


def iso(hour, minute=0):
    return (MIDNIGHT + timedelta(hours=hour, minutes=minute)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pid(name):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"phone-1|{name}"))


def row(name, start, end, updated, deleted=False, kind="phone_active"):
    stamp = f"2026-11-0{updated}T00:00:00Z"
    return {"id": pid(name), "kind": kind, "start_utc": iso(*start), "end_utc": iso(*end), "source": "tracker",
            "updated_at": stamp, "deleted_at": stamp if deleted else None}


def fresh():
    """An empty store and a server cursor of 0, for the next case (the pairing and listener stay)."""
    sync.save_remote(sync.empty_remote())
    state = sync.load_state()
    for progress in state["transports"].values():
        progress["cursor"] = "0"
    sync.save_state(state)
    Server.rows, Server.seq = [], 0


def stored(name):
    r = sync.load_remote()
    return r["intervals"].get(pid(name)) or r["deleted"].get(pid(name))


# --- 1. the same row by both routes is applied once ---
fresh()
same = row("same", (8,), (9,), 2)
server_put(same)
check(phone_push(same)["accepted"] == 1, "direct: the phone's row is taken")
check(sync.pull_once() == 0, "server: the same row again changes nothing (applied once)")
check(stored("same")[5] == ["direct", "server"], f"both routes are remembered: {stored('same')[5]}")
check(phone_push(same)["ignored"] == 1, "direct again: ignored")
check(len(sync.load_remote()["intervals"]) == 1, "one row, not two")

# --- 2. overlapping and conflicting rows, in both orders ---
SERVER_ROWS = [row("A", (8,), (9,), 1), row("B", (10,), (11,), 3, deleted=True), row("C", (12,), (13,), 3),
               row("D", (14,), (15,), 4), row("E", (16,), (17,), 1)]
PHONE_ROWS = [row("A", (8,), (9, 30), 2), row("B", (10,), (11, 30), 2), row("C", (12,), (12, 30), 2, deleted=True),
              row("D", (14,), (15,), 4, deleted=True), row("F", (18,), (19,), 1)]
results = []
for order in ("direct first", "server first"):
    fresh()
    for r in SERVER_ROWS:
        server_put(r)
    if order == "direct first":
        phone_push(*PHONE_ROWS)
        sync.pull_once()
    else:
        sync.pull_once()
        phone_push(*PHONE_ROWS)
    r = sync.load_remote()
    results.append(({k: v[:4] for k, v in r["intervals"].items()}, {k: v[:4] for k, v in r["deleted"].items()}))
check(results[0] == results[1], "the same result whichever route comes first")
live, gone = results[0]
end = lambda name: datetime.fromtimestamp(live[pid(name)][2])
check(pid("A") in live and end("A") == MIDNIGHT + timedelta(hours=9, minutes=30), "A: the newer copy wins (the phone's edit)")
check(pid("B") in gone and pid("B") not in live, "B: a newer deletion wins")
check(pid("C") in live and end("C") == MIDNIGHT + timedelta(hours=13), "C: an older deletion loses")
check(pid("D") in gone and pid("D") not in live, "D: same updated_at, the deletion wins")
check(pid("E") in live and pid("F") in live, "rows on one route only are kept")

# --- 3. cursors are per route ---
fresh()
for r in SERVER_ROWS:
    server_put(r)
phone_push(row("only-direct", (20,), (21,), 1))
sync.pull_once()
before = copy.deepcopy(sync.load_remote())
state = sync.load_state()
check(state["transports"]["server"]["cursor"] == str(Server.seq) and state["transports"]["direct"]["cursor"] == "0",
      "pulling from the server moves only the server's cursor")
status, feed = phone_call("GET", "/peer/pull?since=0", KEY)
phone_cursor = feed["cursor"]
state["transports"]["server"]["cursor"] = "999"  # the server doesn't know it (restored from an older backup)
sync.save_state(state)
sync.pull_once()
after = sync.load_remote()
check(sync.load_state()["transports"]["server"]["cursor"] == str(Server.seq), "unknown cursor: the server starts over")
check(sync.load_state()["transports"]["direct"]["cursor"] == "0", "...the direct route's cursor isn't touched")
check(pid("only-direct") in after["intervals"], "...and what the phone pushed directly is kept")
check({k: v[:4] for k, v in after["intervals"].items()} == {k: v[:4] for k, v in before["intervals"].items()},
      "pulling the server again changes nothing")
check(phone_call("GET", f"/peer/pull?since={phone_cursor}", KEY)[0] == 200,
      "the phone's own cursor on this PC is unaffected by the server's")

# --- 4. disconnecting forgets only what the server alone delivered ---
fresh()
server_put(row("only-server", (8,), (9,), 1))
server_put(row("shared", (10,), (11,), 1))
phone_push(row("shared", (10,), (11,), 1), row("only-phone", (12,), (13,), 1))
sync.pull_once()
r = sync.load_remote()
r["intervals"]["legacy"] = ["phone_active", 1.0, 2.0, 1.0, None]  # saved before routes were kept
sync.save_remote(r)
sync.disconnect()
r = sync.load_remote()
check(pid("only-server") not in r["intervals"], "disconnect: a row only the server delivered is forgotten")
check(r["intervals"][pid("shared")][5] == ["direct"], "a row both delivered stays, now from the phone only")
check(pid("only-phone") in r["intervals"] and "legacy" in r["intervals"],
      "what the phone pushed, and rows of unknown route, stay")
sync.connect(URL, "tester", "secret", "TestPC")
check(pid("only-phone") in sync.load_remote()["intervals"], "connecting again keeps the phone's rows")

# --- 5. one route down, the other still works ---
fresh()
server_put(row("s1", (8,), (9,), 1))
state = sync.load_state()
state["server"] = "http://127.0.0.1:1"  # nothing listens there
sync.save_state(state)
try:
    sync.pull_once()
    check(False, "an unreachable server fails")
except sync.SyncError as e:
    check(e.status is None, f"server unreachable: {e}")
check(phone_push(row("d1", (10,), (11,), 1))["accepted"] == 1, "...the phone still pushes directly")
check(phone_call("GET", "/peer/pull?since=0", KEY)[0] == 200, "...and pulls")
check(pid("d1") in sync.load_remote()["intervals"], "...and its row is stored")
state["server"] = URL
sync.save_state(state)
listener.stop()
check(sync.pull_once() == 1 and pid("s1") in sync.load_remote()["intervals"], "listener off: the server still syncs")
listener.start()
check(listener.ready.wait(10) and listener.address, "listener back")

# --- 6. the timelines are the same whichever route brought a row ---
rows = [row("t1", (7,), (8,), 1), row("t2", (7, 30), (9,), 1), row("t3", (22,), (23,), 1, kind="sleep")]
spans = []
for route in ("server", "direct", "both"):
    fresh()
    if route in ("server", "both"):
        for r in rows:
            server_put(r)
        sync.pull_once()
    if route in ("direct", "both"):
        phone_push(*rows)
    r = sync.load_remote()
    spans.append([timeline.day_spans(r, DAY, kind) for kind in ("phone_active", "sleep")])
check(spans[0] == spans[1] == spans[2], f"same spans by server, by the phone, and by both: {spans[0]}")
check(spans[0][0] == [[7 * 3600, 9 * 3600]] and spans[0][1] == [[22 * 3600, 23 * 3600]], "and the right ones")

# --- 7. the merge rule ---
check(sync.replaces(1, False, 2, False) and not sync.replaces(2, False, 1, False), "newer wins, older loses")
check(not sync.replaces(1, False, 1, False), "the same copy again: applied once")
check(sync.replaces(1, False, 1, True) and not sync.replaces(1, True, 1, False), "a tie: the deletion wins, in either order")
check(sync.replaces(1, True, 2, False), "a newer live copy brings a deleted row back")

listener.stop()
http_server.shutdown()
http_server.server_close()
peer.remove_pairing()
for target in (sync.CRED_TARGET, peer.PAIRING_TARGET, peer.KEY_TARGET):
    sync.cred_delete(target)
print("ALL BOTH-MODE CHECKS PASSED")
