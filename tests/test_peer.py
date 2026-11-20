"""Tests for the phone-sync listener (screentime.peer) on 127.0.0.1, over real HTTPS: pairing with
the one-time secret, nonce+HMAC proofs (wrong secret, replayed nonce, revoked pairing), UDP discovery, the
certificate fingerprint, hello, pull, push, and refusing bad input, oversize bodies, too many
requests and too many connections. Uses a scratch logs folder and test Credential Manager entries;
nothing listens on the network."""
import hashlib
import http.client
import json
import os
import socket
import ssl
import sys
import tempfile
import time
import uuid
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from screentime import peer, sync, tlscert, tracker  # noqa: E402

SCRATCH = tempfile.mkdtemp(prefix="st_peer_")
tracker.LOG_DIR = os.path.join(SCRATCH, "logs")
os.makedirs(tracker.LOG_DIR)
peer.PAIRING_TARGET, peer.KEY_TARGET = "ScreenTimePhonePairingTEST", "ScreenTimePhoneKeyTEST"
sync.cred_delete(peer.PAIRING_TARGET)
sync.cred_delete(peer.KEY_TARGET)


def check(cond, name):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(1)


def iso(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


now = datetime.now(timezone.utc)
today = date.today()
days = [today - timedelta(days=4), today - timedelta(days=1)]
LOG = {"total_seconds": 7200.0, "active_seconds": 3000.0, "on_timeline": [[32400, 36000], [43200, 46800]],
       "active_timeline": [[32500, 34000]], "focus": {"Code.exe|DISPLAY1": 1500.0}, "cursor": {}, "active": {}}
for d in days:
    with open(tracker.log_path(d), "w") as f:
        json.dump(LOG, f)
today_log = json.loads(json.dumps(LOG))

# --- config and addresses ---
check(peer.load_config() == {"enabled": False, "port": peer.DEFAULT_PORT, "paired": None, "pairing": None},
      "off by default, nothing paired")
check(peer.set_enabled(True)["enabled"] and not peer.set_enabled(False)["enabled"], "Allow phone sync is saved")
peer.set_enabled(True)
for ip, private, client in (("192.168.1.20", True, True), ("10.0.0.5", True, True), ("172.20.1.1", True, True),
                            ("169.254.3.4", True, True), ("127.0.0.1", False, True), ("::1", False, True),
                            ("8.8.8.8", False, False), ("100.64.1.1", False, False), ("0.0.0.0", False, False),
                            ("not an ip", False, False)):
    check(peer.private_address(ip) == private and peer.client_allowed(ip) == client,
          f"{ip}: binds={private} answers={client}")
install_id = sync.load_state()["install_id"]

# --- start on localhost ---
statuses, pushed, paired_events, synced_events = [], [], [], []
listener = peer.PeerListener(lambda: (today, today_log), on_status=statuses.append,
                             on_change=lambda: pushed.append(1), on_paired=lambda: paired_events.append(1),
                             host="127.0.0.1", port=0, discovery_port=0, on_synced=lambda: synced_events.append(1))
listener.start()
check(listener.ready.wait(10) and listener.address, f"listening: {statuses}")
HOST, PORT = listener.address
cert_path, _ = peer.certificate()
check(listener.responder is not None and "discovery off" not in statuses[-1], f"discovery responder up: {statuses[-1]}")
UDP = listener.responder.address


def ask(msg=None, raw=None, timeout=0.6):
    """Send one discovery datagram the way the phone does; the answer as a dict, or None if none came."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(raw if raw is not None else json.dumps(msg).encode(), UDP)
        return json.loads(s.recvfrom(1024)[0])
    except (socket.timeout, OSError):  # Windows reports a closed port as a reset
        return None
    finally:
        s.close()


WHO = {"type": "screentime-discover", "v": 1, "from": "phone-1", "want": install_id}
check(ask(WHO) is None, "discovery: no phone paired, no answer")


CTX = ssl.create_default_context(cafile=cert_path)  # trusts only this certificate (the phone pins its SHA-256)
CTX.check_hostname = False


class Phone:
    """What the phone does: fetch a nonce, prove the key with an HMAC, use the next nonce it gets."""

    def __init__(self, key=None):
        self.key, self.nonce = key, None

    def fresh_nonce(self):
        code, body, _ = raw_call("GET", "/peer/nonce")
        assert code == 200, body
        return body["nonce"]

    def call(self, method, path, body=None, raw=None, key=None, nonce=None, keep=True):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
        nonce = nonce or self.nonce or self.fresh_nonce()
        self.nonce = None
        mac = peer.request_mac(key or self.key, method, path, nonce, data)
        code, answer, resp = raw_call(method, path, data if method == "POST" else None,
                                      {"Authorization": f"HMAC {nonce}:{mac}"})
        if keep:
            self.nonce = resp.getheader("X-Peer-Nonce")
        return code, answer, resp


def raw_call(method, path, data=None, headers=None):
    conn = http.client.HTTPSConnection(HOST, PORT, context=CTX, timeout=10)
    try:
        h = dict(headers or {})
        if data is not None:
            h.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        text = resp.read()
        return resp.status, (json.loads(text) if text else None), resp
    finally:
        conn.close()


# --- the certificate ---
with socket.create_connection((HOST, PORT)) as raw_sock, CTX.wrap_socket(raw_sock) as tls:
    served, version = tls.getpeercert(binary_form=True), tls.version()
check(version in ("TLSv1.2", "TLSv1.3"), f"TLS 1.2 or newer ({version})")
check(hashlib.sha256(served).hexdigest() == tlscert.sha256(cert_path), "served certificate has the stored SHA-256")
fp = tlscert.fingerprint(cert_path)
check(fp.replace(":", "").lower() == tlscert.sha256(cert_path) and len(fp) == 95, "fingerprint written AB:CD:...")
with open(cert_path, "rb") as f:
    from cryptography import x509  # noqa: E402
    cert = x509.load_pem_x509_certificate(f.read())
check(cert.signature_hash_algorithm.name == "sha256" and cert.public_key().curve.name == "secp256r1",
      "P-256 key, SHA-256 signature")
check(cert.not_valid_after_utc - cert.not_valid_before_utc > timedelta(days=365 * 19), "valid for 20 years")
check(peer.certificate() == (cert_path, _) and tlscert.sha256(cert_path) == hashlib.sha256(served).hexdigest(),
      "made once, then kept")

# --- before pairing nothing works ---
code, body, _ = raw_call("GET", "/peer/nonce")
check(code == 200 and len(body["nonce"]) >= 32 and body["expires_in"] == peer.NONCE_TTL, "nonce needs no proof")
check(raw_call("GET", "/peer/nonce")[1]["nonce"] != body["nonce"], "every nonce is new")
code, body, _ = Phone("anything").call("GET", "/peer/hello")
check(code == 401 and "isn't paired" in body["detail"], "not paired: 401")
code, body, _ = Phone("anything").call("POST", "/peer/pair", {"device_id": "p1", "name": "Pixel"})
check(code == 401 and "no pairing is open" in body["detail"], "pairing not open: 401")
peer.FAILED_AUTH_LIMIT = 1000  # the many refusals below shouldn't lock us out; the lockout is tested at the end

# --- pairing: the QR code's contents ---
payload = peer.start_pairing([HOST], PORT)
check(set(payload) == {"name", "addresses", "port", "secret", "cert_sha256"}, f"QR fields {sorted(payload)}")
check(payload["addresses"] == [HOST] and payload["port"] == PORT and payload["name"] == sync.default_device_name(),
      "name, addresses and port")
check(payload["cert_sha256"] == hashlib.sha256(served).hexdigest(), "QR certificate fingerprint matches the served one")
SECRET = payload["secret"]
check(len(SECRET) >= 40 and sync.cred_read(peer.PAIRING_TARGET) == SECRET, "secret kept in Credential Manager")
with open(peer.config_path()) as f:
    check(SECRET not in f.read(), "secret not in peer.json")
check(peer.start_pairing([HOST], PORT)["secret"] != SECRET, "each pairing gets a new secret")
check(Phone(SECRET).call("POST", "/peer/pair", {"device_id": "p1", "name": "Pixel"})[0] == 401,
      "the replaced QR code's secret stops working")
payload = peer.start_pairing([HOST], PORT)
SECRET = payload["secret"]

import segno  # noqa: E402
code = segno.make(json.dumps(payload, separators=(",", ":")), error="m")
check(code.version <= 12 and code.error == "M", f"QR code fits a small version ({code.version}-{code.error})")

# --- pairing: proofs ---
code, body, _ = Phone("wrong-secret").call("POST", "/peer/pair", {"device_id": "p1", "name": "Pixel"})
check(code == 401 and body["detail"] == "wrong proof", "wrong secret: 401")
pair_nonce = Phone().fresh_nonce()
mac = peer.request_mac(SECRET, "POST", "/peer/pair", pair_nonce, json.dumps({"device_id": "p1", "name": "Pixel"}).encode())
code, body, _ = raw_call("POST", "/peer/pair", json.dumps({"device_id": "p1", "name": "Mallory"}).encode(),
                         {"Authorization": f"HMAC {pair_nonce}:{mac}"})
check(code == 401, "proof made for another body doesn't fit this one")
code, body, _ = raw_call("GET", "/peer/hello", None, {"Authorization": f"Bearer {SECRET}"})
check(code == 401 and "HMAC" in body["detail"], "the raw secret is refused")
code, body, _ = raw_call("GET", "/peer/hello", None, {"Authorization": f"HMAC made-up-nonce:{'0' * 64}"})
check(code == 401 and "nonce" in body["detail"], "a nonce the PC never issued: 401")
check(Phone(SECRET).call("POST", "/peer/pair", {"device_id": "p1", "name": "x" * 101})[0] == 422, "bad pair body: 422")
check(not peer.paired_phone(), "nothing paired yet")

phone = Phone()
code, body, resp = phone.call("POST", "/peer/pair", {"device_id": "phone-1", "name": "Pixel 8"}, key=SECRET)
check(code == 200 and body["device_id"] == install_id and body["protocol"] == 1 and len(body["key"]) >= 40,
      "pairing answers with this PC's id and the phone's key")
KEY = phone.key = body["key"]
check(KEY != SECRET and sync.cred_read(peer.KEY_TARGET) == KEY, "the key is new and kept in Credential Manager")
check(peer.paired_phone()["name"] == "Pixel 8" and paired_events, "phone paired, window told")
check(sync.cred_read(peer.PAIRING_TARGET) == SECRET, "secret kept until the phone uses its key (answer may be lost)")
again = Phone()
code, body, _ = again.call("POST", "/peer/pair", {"device_id": "phone-1", "name": "Pixel 8"}, key=SECRET)
check(code == 200 and body["key"] != KEY, "the phone may ask again before using a key")
KEY = phone.key = body["key"]
phone.nonce = None

# --- the key and nonces ---
code, body, resp = phone.call("GET", "/peer/hello")
check(code == 200 and body == {"device_id": install_id, "type": "windows", "protocol": 1,
                               "name": sync.default_device_name()}, f"hello with the key: {body}")
check(sync.cred_read(peer.PAIRING_TARGET) is None and peer.load_config()["pairing"] is None,
      "first use of the key spends the pairing secret")
check(Phone(SECRET).call("POST", "/peer/pair", {"device_id": "evil", "name": "Evil"})[0] == 401,
      "the QR code's secret works only once")
check("Python" not in (resp.getheader("Server") or ""), "Server header doesn't name Python")
next_nonce = resp.getheader("X-Peer-Nonce")
check(next_nonce and phone.nonce == next_nonce, "answers carry the next nonce")
check(phone.call("GET", "/peer/hello")[0] == 200, "the next nonce works")

used = phone.fresh_nonce()
check(phone.call("GET", "/peer/hello", nonce=used, keep=False)[0] == 200, "a nonce works once...")
code, body, _ = phone.call("GET", "/peer/hello", nonce=used, keep=False)
check(code == 401 and "already used" in body["detail"], "...a replayed nonce is refused")
mac = peer.request_mac(KEY, "GET", "/peer/hello", (n := phone.fresh_nonce()))
check(raw_call("GET", "/peer/hello", None, {"Authorization": f"HMAC {n}:{mac}"})[0] == 200, "recorded request works once")
check(raw_call("GET", "/peer/hello", None, {"Authorization": f"HMAC {n}:{mac}"})[0] == 401,
      "...and replaying it exactly is refused")
mac = peer.request_mac(KEY, "GET", "/peer/pull?since=0", (n := phone.fresh_nonce()))
check(raw_call("GET", "/peer/pull?since=5", None, {"Authorization": f"HMAC {n}:{mac}"})[0] == 401,
      "proof for one query doesn't fit another")
check(phone.call("GET", "/peer/hello", key="wrong-key", keep=False)[0] == 401, "wrong key: 401")
peer.NONCE_TTL = 0.5
stale = phone.fresh_nonce()
time.sleep(1)
code, body, _ = phone.call("GET", "/peer/hello", nonce=stale, keep=False)
check(code == 401 and "expired" in body["detail"], "an expired nonce is refused")
peer.NONCE_TTL = 120
nonces = peer.Nonces()
first = nonces.issue()
for _ in range(peer.MAX_NONCES):
    nonces.issue()
check(not nonces.take(first) and len(nonces.issued) == peer.MAX_NONCES, "at most MAX_NONCES wait at once")

code, _, _ = phone.call("GET", "/peer/nothing", keep=False)
check(code == 404, "unknown path: 404")
code, _, _ = raw_call("PUT", "/peer/push", b"{}")
check(code == 405, "PUT: 405")
code, _, _ = raw_call("GET", "/peer/push")
check(code == 405, "GET on push: 405")

# --- discovery ---
check(ask(WHO) == {"type": "screentime-here", "v": 1, "device_id": install_id, "address": HOST, "port": PORT},
      "discovery: the paired phone gets this PC's address and port")
for name, msg, raw in (("another phone's device id", dict(WHO, **{"from": "phone-2"}), None),
                       ("asking for another PC", dict(WHO, want="another-pc"), None),
                       ("a newer protocol", dict(WHO, v=2), None),
                       ("another type", dict(WHO, type="hello"), None),
                       ("not JSON", None, b"who is it?"),
                       ("JSON that isn't an object", None, b"[1, 2]"),
                       ("too long", None, json.dumps(dict(WHO, pad="x" * 600)).encode())):
    check(ask(msg, raw) is None, f"discovery: {name}: no answer")
check(ask(WHO) is not None, "discovery: still answers after bad datagrams")
check(peer.discovery_reply(json.dumps(WHO).encode(), None, install_id, HOST, PORT) is None,
      "discovery_reply: nothing when no phone is paired")
peer.DISCOVERIES_PER_MINUTE = 2
listener.responder.limiter.requests.clear()
answers = [ask(WHO) is not None for _ in range(3)]
check(answers == [True, True, False], f"discovery: at most DISCOVERIES_PER_MINUTE answers per address {answers}")
peer.DISCOVERIES_PER_MINUTE = 30
listener.responder.limiter.requests.clear()

# --- pull ---
expect = {}
for d, log in ((days[0], LOG), (days[1], LOG), (today, today_log)):
    for table, key, fp_, row in sync.day_rows(install_id, d, log):
        expect[key] = (table, row)
check(peer.paired_phone().get("last_sync") is None and not synced_events, "no sync yet: no last sync")
code, page, _ = phone.call("GET", "/peer/pull?since=0")
check(code == 200 and page["has_more"] is False, "pull from the start")
last = peer.paired_phone().get("last_sync")
check(last and abs((datetime.fromisoformat(last) - datetime.now()).total_seconds()) < 5 and synced_events,
      f"a pull records the phone's last sync and tells the window: {last}")
got_rows = {r["id"]: r for r in page["intervals"]}
want_intervals = {k: r for k, (t, r) in expect.items() if t == "intervals"}
check(set(got_rows) == set(want_intervals), f"every own interval served ({len(got_rows)})")
check(all({k: v for k, v in got_rows[k].items() if k != "updated_at"} == want_intervals[k] for k in got_rows),
      "rows in the format sync.py sends")
check(len(page["app_usage_daily"]) == 3, "app rows served too")
full_cursor = page["cursor"]

seen, cursor, requests = [], "0", 0
while True:
    code, page, _ = phone.call("GET", f"/peer/pull?since={cursor}&limit=2")
    requests += 1
    seen += [r["id"] for r in page["intervals"]] + [r["local_date"] + r["app"] for r in page["app_usage_daily"]]
    cursor = page["cursor"]
    if not page["has_more"]:
        break
check(len(seen) == len(set(seen)) == len(expect) and requests == (len(expect) + 1) // 2,
      f"pages of 2: every row once ({len(seen)} rows, {requests} requests)")
check(cursor == full_cursor, "paging ends at the same cursor")
code, page, _ = phone.call("GET", f"/peer/pull?since={cursor}")
check(code == 200 and page["intervals"] == [] and page["cursor"] == cursor, "nothing new: empty page, same cursor")

today_log["on_timeline"][-1][1] += 600
today_log["focus"]["Code.exe|DISPLAY1"] += 60
code, page, _ = phone.call("GET", f"/peer/pull?since={cursor}")
check(len(page["intervals"]) == 1 and page["intervals"][0]["end_utc"] == sync.utc_iso(
    datetime.combine(today, datetime.min.time()), 46800 + 600), "a grown period comes again, with its new end")
check(len(page["app_usage_daily"]) == 1 and page["app_usage_daily"][0]["focus_s"] == 1560, "a changed app time too")
cursor = page["cursor"]
os.remove(tracker.log_path(days[1]))
code, page, _ = phone.call("GET", f"/peer/pull?since={cursor}")
check(len(page["intervals"]) == 3 and all(r["deleted_at"] for r in page["intervals"]), "removed log: deletions")
for query, name in (("since=99999", "cursor ahead of the feed"), ("since=abc", "cursor not a number"),
                    ("since=0&limit=0", "limit 0"), ("since=0&limit=99999", "limit too big"),
                    ("since=0&limit=x", "limit not a number")):
    code, body, _ = phone.call("GET", f"/peer/pull?{query}")
    check(code == 400 and body["detail"], f"{name}: 400 ({body['detail']})")

# --- push ---
def interval(kind="phone_active", start=None, minutes=30, updated=None, **extra):
    start = start or now - timedelta(hours=3)
    row = {"id": str(uuid.uuid4()), "kind": kind, "start_utc": iso(start),
           "end_utc": iso(start + timedelta(minutes=minutes)),
           "source": "health_connect" if kind == "sleep" else "tracker",
           "updated_at": iso(updated or now - timedelta(minutes=5)), "deleted_at": None}
    row.update(extra)
    return row


def usage(app="org.telegram.messenger", seconds=600, updated=None, **extra):
    row = {"local_date": today.isoformat(), "app": app, "monitor": None, "focus_s": seconds, "cursor_s": 0,
           "active_s": seconds, "updated_at": iso(updated or now - timedelta(minutes=5))}
    row.update(extra)
    return row


mine, night = interval(), interval("sleep", start=now - timedelta(hours=10), minutes=420)
code, result, _ = phone.call("POST", "/peer/push", {"intervals": [mine, night], "app_usage_daily": [usage()]})
check(code == 200 and result == {"intervals": {"accepted": 2, "ignored": 0, "rejected": []},
                                 "app_usage_daily": {"accepted": 1, "ignored": 0, "rejected": []}},
      f"push answers like the server: {result}")
check(mine["id"] in sync.load_remote()["intervals"] and night["id"] in sync.load_remote()["intervals"], "stored")
check(peer.load_usage()[f"{today}|org.telegram.messenger|"]["active_s"] == 600 and pushed, "app time stored, window told")
code, result, _ = phone.call("POST", "/peer/push", {"intervals": [mine]})
check(result["intervals"]["ignored"] == 1, "same row again: ignored")
code, result, _ = phone.call("POST", "/peer/push", {"intervals": [dict(mine, updated_at=iso(now), deleted_at=iso(now))]})
check(result["intervals"]["accepted"] == 1 and mine["id"] not in sync.load_remote()["intervals"], "deletion applied")
future, almost, own_kind = interval(updated=now + timedelta(hours=1, minutes=5)), \
    interval(updated=now + timedelta(minutes=55)), interval("pc_on")
code, result, _ = phone.call("POST", "/peer/push", {"intervals": [future, almost, own_kind],
                                                    "app_usage_daily": [usage(app="f", updated=now + timedelta(hours=2))]})
reasons = {r["id"]: r["reason"] for r in result["intervals"]["rejected"]}
check(result["intervals"]["accepted"] == 1 and reasons == {future["id"]: "updated_at_in_future",
                                                           own_kind["id"]: "kind_not_allowed"},
      "over 1 h ahead and this PC's own kinds rejected, 55 min ahead accepted")
check(result["app_usage_daily"]["rejected"] == [{"id": f"{today}|f|", "reason": "updated_at_in_future"}],
      "future app row rejected")

before_remote, before_usage = json.dumps(sync.load_remote(), sort_keys=True), json.dumps(peer.load_usage(), sort_keys=True)
good = interval()
cases = [
    ("not JSON", None, b"{nope"), ("a list", [good], None), ("unknown field", {"intervals": [], "extra": 1}, None),
    ("intervals not a list", {"intervals": {}}, None), ("row not an object", {"intervals": ["x"]}, None),
    ("missing id", {"intervals": [good, {k: v for k, v in interval().items() if k != "id"}]}, None),
    ("id not a UUID", {"intervals": [good, interval(id="1234")]}, None),
    ("unknown kind", {"intervals": [good, interval(kind="tv")]}, None),
    ("unknown source", {"intervals": [good, interval(source="guess")]}, None),
    ("bad timestamp", {"intervals": [good, interval(start_utc="yesterday")]}, None),
    ("end before start", {"intervals": [good, interval(minutes=-5)]}, None),
    ("app seconds negative", {"app_usage_daily": [usage(focus_s=-1)]}, None),
    ("app seconds a bool", {"app_usage_daily": [usage(cursor_s=True)]}, None),
    ("bad date", {"app_usage_daily": [usage(local_date="2026-13-01")]}, None),
    ("text not UTF-8", None, b'{"intervals": ["\xff"]}'),
]
for name, body, raw in cases:
    code, answer, _ = phone.call("POST", "/peer/push", body, raw=raw)
    check(code in (400, 422) and answer["detail"], f"{name}: {code} ({answer['detail']})")
peer.MAX_ROWS = 3
code, answer, _ = phone.call("POST", "/peer/push", {"intervals": [interval() for _ in range(4)]})
check(code == 422 and "at most 3" in answer["detail"], "too many rows: 422")
peer.MAX_ROWS = 5000
check(json.dumps(sync.load_remote(), sort_keys=True) == before_remote and
      json.dumps(peer.load_usage(), sort_keys=True) == before_usage, "refused pushes stored nothing")

# --- size and framing ---
def raw_request(head):
    """Send only the request head over TLS; return the status line and how long the answer took."""
    t = time.time()
    with socket.create_connection((HOST, PORT), timeout=10) as s, CTX.wrap_socket(s) as tls:
        tls.sendall(head.encode())
        answer = b""
        while b"\r\n" not in answer:
            chunk = tls.recv(4096)
            if not chunk:
                break
            answer += chunk
    return answer.split(b"\r\n")[0].decode(), time.time() - t


def auth_header(method, path, body=b""):
    n = phone.fresh_nonce()
    return f"Authorization: HMAC {n}:{peer.request_mac(KEY, method, path, n, body)}\r\n"


status, took = raw_request(f"POST /peer/push HTTP/1.1\r\nHost: x\r\n{auth_header('POST', '/peer/push')}"
                           f"Content-Length: {peer.MAX_BODY + 1}\r\n\r\n")
check(" 413 " in status and took < 3, f"oversize body refused before it's sent ({status})")
peer.MAX_BODY = 1000
code, answer, _ = phone.call("POST", "/peer/push", raw=json.dumps({"intervals": [interval() for _ in range(10)]}).encode())
check(code == 413, "a body over the limit is refused (413)")
peer.MAX_BODY = 2 * 1024 * 1024
status, _ = raw_request(f"POST /peer/push HTTP/1.1\r\nHost: x\r\n{auth_header('POST', '/peer/push')}"
                        "Transfer-Encoding: chunked\r\n\r\n")
check(" 411 " in status, f"chunked body refused ({status})")
status, _ = raw_request(f"POST /peer/push HTTP/1.1\r\nHost: x\r\n{auth_header('POST', '/peer/push')}\r\n")
check(" 411 " in status, f"no Content-Length refused ({status})")
status, _ = raw_request("POST /peer/push HTTP/1.1\r\nHost: x\r\nContent-Length: 999999999\r\n\r\n")
check(" 401 " in status, f"no proof: refused before the size is even looked at ({status})")

with socket.create_connection((HOST, PORT), timeout=5) as s:  # plain HTTP to the HTTPS port
    s.sendall(b"GET /peer/nonce HTTP/1.0\r\n\r\n")
    try:
        plain = s.recv(100)
    except OSError:
        plain = b""
check(b"nonce" not in plain, "plain HTTP gets nothing")

# --- too many connections at once ---
idle = [socket.create_connection((HOST, PORT)) for _ in range(peer.MAX_CONNECTIONS)]  # no handshake: hold a slot
time.sleep(0.5)
try:
    refused = raw_call("GET", "/peer/nonce")[0] != 200
except (OSError, http.client.HTTPException):
    refused = True
check(refused, f"connection {peer.MAX_CONNECTIONS + 1} is dropped while {peer.MAX_CONNECTIONS} are open")
for s in idle:
    s.close()
deadline, ok = time.time() + 10, False
while time.time() < deadline and not ok:
    try:
        ok = raw_call("GET", "/peer/nonce")[0] == 200
    except (OSError, http.client.HTTPException):
        time.sleep(0.2)
check(ok, "answers again once they close")

# --- pairing another phone replaces this one; removing the pairing revokes it ---
phone.nonce = None
second = peer.start_pairing([HOST], PORT)
check(phone.call("GET", "/peer/hello")[0] == 200, "an open pairing doesn't revoke the paired phone yet")
other = Phone()
code, body, _ = other.call("POST", "/peer/pair", {"device_id": "phone-2", "name": "Galaxy"}, key=second["secret"])
other.key = body["key"]
check(peer.paired_phone().get("last_sync") is None, "another phone paired: no last sync of its own yet")
check(code == 200 and peer.paired_phone()["name"] == "Galaxy", "another phone paired")
code, body, _ = phone.call("GET", "/peer/hello", keep=False)
check(code == 401 and body["detail"] == "wrong proof", "the first phone's key no longer works")
before = len(synced_events)
check(other.call("POST", "/peer/push", {"intervals": [], "app_usage_daily": []})[0] == 200
      and peer.paired_phone().get("last_sync") and len(synced_events) == before + 1, "a push records the last sync too")
check(other.call("GET", "/peer/hello")[0] == 200, "the new phone's key works")

peer.remove_pairing()
check(peer.paired_phone() is None and sync.cred_read(peer.KEY_TARGET) is None, "Remove pairing deletes the key")
code, body, _ = other.call("GET", "/peer/hello", keep=False)
check(code == 401 and "isn't paired" in body["detail"], "revoked pairing: 401")
code, body, _ = other.call("POST", "/peer/push", {"intervals": [interval()]}, keep=False)
check(code == 401, "revoked pairing can't push")
check(Phone(second["secret"]).call("POST", "/peer/pair", {"device_id": "x", "name": "x"})[0] == 401,
      "nor pair again with an old code")
check(ask(dict(WHO, **{"from": "phone-2"})) is None, "discovery: no answer once the pairing is removed")

third = peer.start_pairing([HOST], PORT)
peer.PAIRING_TTL = 0.5
time.sleep(1)
code, body, _ = Phone(third["secret"]).call("POST", "/peer/pair", {"device_id": "late", "name": "Late"})
check(code == 401 and "no pairing is open" in body["detail"] and sync.cred_read(peer.PAIRING_TARGET) is None,
      "an expired QR code is refused and its secret deleted")
peer.PAIRING_TTL = 600
fourth = peer.start_pairing([HOST], PORT)
peer.cancel_pairing()
check(Phone(fourth["secret"]).call("POST", "/peer/pair", {"device_id": "x", "name": "x"})[0] == 401,
      "closing the pairing window stops its code")

# --- rate limits ---
clock = [1000.0]
limiter = peer.RateLimiter(clock=lambda: clock[0])
peer.REQUESTS_PER_MINUTE = 5
check([limiter.check("10.0.0.2") for _ in range(6)] == [0, 0, 0, 0, 0, 61], "6th request in a minute waits a minute")
check(limiter.check("10.0.0.3") == 0, "other addresses are counted on their own")
clock[0] += 61
check(limiter.check("10.0.0.2") == 0, "a minute later it may go on")
peer.REQUESTS_PER_MINUTE = 240

restart = peer.start_pairing([HOST], PORT)
code, body, _ = Phone().call("POST", "/peer/pair", {"device_id": "p", "name": "P"}, key=restart["secret"])
phone = Phone(body["key"])
peer.FAILED_AUTH_LIMIT = 3
listener.server.limiter.failures.clear()
ready = phone.fresh_nonce()  # fetched before the lockout
for _ in range(3):
    Phone("guess").call("GET", "/peer/hello", keep=False)
code, body, resp = phone.call("GET", "/peer/hello", nonce=ready, keep=False)
check(code == 429 and int(resp.getheader("Retry-After")) > 500, "after 3 wrong proofs even the right key waits")
code, body, _ = raw_call("GET", "/peer/nonce")
check(code == 429, "...for nonces too")

# --- stopping ---
listener.stop()
check(listener.thread is None and statuses[-1] == "Off", "stop: thread ends, status Off")
check(listener.responder is None and ask(WHO) is None, "stop: the discovery responder is closed too")
try:
    socket.create_connection((HOST, PORT), timeout=2).close()
    check(False, "port closed after stop")
except OSError:
    check(True, "port closed after stop")

# A UDP port that is taken: the phone can still connect at the address it knows
taken = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
taken.bind(("127.0.0.1", 0))
busy = peer.PeerListener(lambda: (today, today_log), on_status=statuses.append, host="127.0.0.1", port=0,
                         discovery_port=taken.getsockname()[1])
busy.start()
check(busy.ready.wait(10) and busy.address and busy.responder is None and "discovery off" in statuses[-1],
      f"discovery port taken: still listening, says so: {statuses[-1]}")
busy.stop()
taken.close()

peer.ADDRESS_CHECK = 0.2
for found in ("8.8.8.8", None):
    peer.lan_address = lambda found=found: found
    waiting = peer.PeerListener(lambda: (today, today_log), on_status=statuses.append)
    waiting.start()
    check(waiting.ready.wait(5) and waiting.address is None and "Not on a private network" in statuses[-1],
          f"address {found}: doesn't listen")
    waiting.stop()

peer.remove_pairing()
print("ALL PEER CHECKS PASSED")
