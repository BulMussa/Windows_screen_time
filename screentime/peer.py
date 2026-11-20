"""Phone sync listener: the phone connects to this PC directly over HTTPS on your own network (Windows).

It mirrors the server's sync API, with the same row format as sync.py:

    GET  /peer/nonce                 a one-time nonce for the next request (no proof needed)
    POST /peer/pair                  pairing: proves the QR code's secret, answers with the phone's key
    GET  /peer/hello                 this PC's device id and name, and the protocol version (1)
    GET  /peer/pull?since=N&limit=M  this PC's own rows changed since cursor N (start with 0)
    POST /peer/push                  the phone's rows; the newer updated_at wins, as with the server

Pairing: "Pair a phone" makes a one-time pairing secret (kept in Windows Credential Manager, valid
for PAIRING_TTL) and shows it in a QR code with this PC's name, address, port and certificate
fingerprint. The phone pins the certificate and calls /peer/pair, proving the secret. This PC then
makes a key for that phone (also in Credential Manager) and answers with it over TLS. Once the phone
has used its key, the pairing secret is deleted. Only one phone is paired at a time; pairing another
replaces it, and "Remove pairing" revokes it.

Proof: no secret or key is ever sent. Each request carries
    Authorization: HMAC <nonce>:<hex HMAC-SHA256(key, method \\n path \\n nonce \\n hex SHA-256(body))>
where the nonce came from GET /peer/nonce or the X-Peer-Nonce header of the previous answer. A nonce
works once and only for NONCE_TTL seconds, so a recorded request can't be played again.

Discovery: when the phone doesn't find this PC at its last address, it broadcasts a UDP datagram
to DISCOVERY_PORT asking "who is <this PC's install id>?" from its own device id. While the listener
runs, a responder on that port (same address as the listener) answers with this PC's address and
port, but only the paired phone, and only when it asks for this PC.

The listener is off unless "Allow phone sync" is ticked. It binds only to this PC's address on a
private network (192.168.x.x, 10.x.x.x, 172.16-31.x.x, link-local), follows that address when it
changes, and answers only clients from private addresses. It also limits request size, requests per
address and failed proofs per address.

Run:     python -m screentime.peer     (prints the address, certificate fingerprint and paired phone)
"""
import collections
import copy
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import ssl
import sys
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import sync, tlscert, tracker
from .report import logged_days, read_log

PROTOCOL = 1
DEFAULT_PORT = 47613
DISCOVERY_PORT = 47614  # UDP: the paired phone asks where this PC is (fixed, so the phone knows it before finding the PC)
MAX_DATAGRAM = 512  # bytes; a discovery request is about 150
DISCOVERIES_PER_MINUTE = 30  # answers per client address, so the responder can't be used to flood anyone
MAX_BODY = 2 * 1024 * 1024  # bytes; a full phone batch (1000 + 1000 rows) is about 0.5 MB
MAX_ROWS = 5000  # per list, like the server
PULL_LIMIT, MAX_PULL_LIMIT = 1000, 5000
FUTURE_LIMIT = 3600  # seconds; rows whose updated_at is further ahead are rejected, like the server
REQUESTS_PER_MINUTE = 240  # per client address; with a nonce in each answer a sync needs about one per page
FAILED_AUTH_LIMIT, FAILED_AUTH_WINDOW = 5, 600  # failed proofs per address within that many seconds
NONCE_TTL, MAX_NONCES = 120, 512  # seconds a nonce stays valid; at most this many waiting at once
PAIRING_TTL = 600  # seconds a pairing QR code stays valid
MAX_CONNECTIONS = 8  # handled at once; more are closed straight away
CONNECTION_TIMEOUT = 10  # seconds a client may take for the handshake and each read
ADDRESS_CHECK = 30  # seconds between checks whether this PC's network address changed
INTERVAL_KINDS = ("pc_on", "pc_active", "phone_active", "sleep")  # what the server knows
PHONE_KINDS = ("phone_active", "sleep")  # what the phone may push; this PC's own kinds come from its logs
SOURCES = ("tracker", "health_connect", "manual")
PAIRING_TARGET = "ScreenTimePhonePairing"  # Credential Manager: the open pairing's one-time secret
KEY_TARGET = "ScreenTimePhoneKey"  # Credential Manager: the paired phone's key


# --- Config and files (next to the logs folder, found through tracker so tests can redirect it) ---

def data_dir():
    return os.path.dirname(tracker.LOG_DIR)


def config_path():
    return os.path.join(data_dir(), "peer.json")


def load_config():
    """peer.json: whether phone sync is allowed, the port, the paired phone (no secrets: those are
    in Credential Manager) and when the open pairing started."""
    config = {"enabled": False, "port": DEFAULT_PORT,
              "paired": None,  # {"device_id", "name", "since"} of the paired phone
              "pairing": None}  # {"started": epoch seconds, "phone": {...} once it has its key}
    try:
        with open(config_path()) as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            config.update(loaded)
    except (OSError, ValueError):
        pass
    config.pop("test_secret", None)  # from before pairing
    return config


def save_config(config):
    write_json(config_path(), config, indent=2)


def write_json(path, data, indent=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(data, f, indent=indent)
    os.replace(path + ".tmp", path)


_config_lock = threading.Lock()  # pairing changes come from the window and from listener threads


def set_enabled(enabled):
    """Save "Allow phone sync"."""
    with _config_lock:
        config = load_config()
        config["enabled"] = bool(enabled)
        save_config(config)
        return config


def certificate():
    """(cert.pem, key.pem), made on first use."""
    return tlscert.ensure_certificate(os.path.join(data_dir(), "peer"))


def now_iso():
    return sync.iso_micros(round(time.time() * 1_000_000))


# --- Pairing ---

def start_pairing(addresses, port):
    """Open a pairing: make a new one-time secret and return what the QR code shows. Opening it
    again replaces the earlier QR code's secret. The phone paired now stays paired until another
    one completes this pairing."""
    secret = secrets.token_urlsafe(32)
    with _config_lock:
        sync.cred_write(PAIRING_TARGET, secret)
        config = load_config()
        config["pairing"] = {"started": time.time(), "phone": None}
        save_config(config)
    state = sync.load_state()
    return {"name": state["device_name"] or sync.default_device_name(), "addresses": list(addresses),
            "port": port, "secret": secret, "cert_sha256": tlscert.sha256(certificate()[0])}


def cancel_pairing():
    """Close the open pairing (the QR code stops working); a phone that already paired stays paired."""
    with _config_lock:
        sync.cred_delete(PAIRING_TARGET)
        config = load_config()
        config["pairing"] = None
        save_config(config)


def pairing_secret():
    """The open pairing's secret, or None if none is open (or it has expired, which closes it)."""
    config = load_config()
    pairing = config.get("pairing")
    if not pairing:
        return None
    if time.time() - pairing.get("started", 0) > PAIRING_TTL:
        cancel_pairing()
        return None
    return sync.cred_read(PAIRING_TARGET)


def complete_pairing(device_id, name):
    """The phone proved the pairing secret: make its key, make it the paired phone (replacing any
    other) and return the key. The pairing stays open until the phone first uses the key, so it can
    ask again if this answer gets lost."""
    key = secrets.token_urlsafe(32)
    with _config_lock:
        sync.cred_write(KEY_TARGET, key)
        config = load_config()
        phone = {"device_id": device_id, "name": name, "since": datetime.now().isoformat(timespec="seconds")}
        config["paired"] = phone
        if config.get("pairing"):
            config["pairing"]["phone"] = phone
        save_config(config)
    return key


def confirm_pairing():
    """The paired phone used its key: the pairing secret has done its job. Returns whether it was open."""
    with _config_lock:
        config = load_config()
        if not (config.get("pairing") or {}).get("phone"):
            return False
        sync.cred_delete(PAIRING_TARGET)
        config["pairing"] = None
        save_config(config)
        return True


def phone_key():
    """The paired phone's key, or None."""
    return sync.cred_read(KEY_TARGET) if load_config().get("paired") else None


def paired_phone():
    """{"device_id", "name", "since"} of the paired phone, or None."""
    config = load_config()
    return config.get("paired") if config.get("paired") and sync.cred_read(KEY_TARGET) else None


def record_sync():
    """The paired phone just pulled or pushed: remember when, for "Phone last synced at ..." in the window."""
    with _config_lock:
        config = load_config()
        if not config.get("paired"):
            return
        config["paired"]["last_sync"] = datetime.now().isoformat(timespec="seconds")
        save_config(config)


def remove_pairing():
    """Revoke the paired phone (and close any open pairing): its key stops working at once."""
    with _config_lock:
        sync.cred_delete(KEY_TARGET)
        sync.cred_delete(PAIRING_TARGET)
        config = load_config()
        config["paired"], config["pairing"] = None, None
        save_config(config)


def request_mac(key, method, target, nonce, body=b""):
    """The proof for one request: HMAC-SHA256 with the key over method, path (with query), nonce and
    the body's SHA-256, as hex."""
    message = "\n".join((method, target, nonce, hashlib.sha256(body).hexdigest())).encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


class Nonces:
    """One-time nonces: each is valid for NONCE_TTL seconds and can be used once."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.issued = collections.OrderedDict()  # nonce -> when issued, oldest first
        self.lock = threading.Lock()

    def issue(self):
        nonce = secrets.token_urlsafe(24)
        with self.lock:
            self.issued[nonce] = self.clock()
            while len(self.issued) > MAX_NONCES:
                self.issued.popitem(last=False)
        return nonce

    def take(self, nonce):
        """True if nonce was issued, is still valid and wasn't used; it can't be used again either way."""
        with self.lock:
            issued = self.issued.pop(nonce, None)
        return issued is not None and self.clock() - issued <= NONCE_TTL


# --- Addresses ---

def lan_address():
    """This PC's IPv4 address on the network its traffic goes out on, or None. Connecting a UDP
    socket only picks the route; nothing is sent."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: never answered, only routed
            return s.getsockname()[0]
    except OSError:
        return None


def private_address(text):
    """True for addresses on a private network (not loopback, not the internet)."""
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    return (ip.is_private or ip.is_link_local) and not ip.is_loopback and not ip.is_unspecified


def client_allowed(text):
    """Clients from a private network, or this PC itself."""
    try:
        return private_address(text) or ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


# --- Limits ---

class RateLimiter:
    """Per client address: at most REQUESTS_PER_MINUTE requests, and FAILED_AUTH_LIMIT failed
    logins within FAILED_AUTH_WINDOW seconds."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.requests = collections.defaultdict(collections.deque)
        self.failures = collections.defaultdict(collections.deque)
        self.lock = threading.Lock()

    @staticmethod
    def _trim(times, window, now):
        while times and times[0] <= now - window:
            times.popleft()

    def check(self, ip, limit=None):
        """0 if ip may make a request now (and counts it), else the seconds to wait. limit is the
        requests per minute (REQUESTS_PER_MINUTE unless given)."""
        limit = REQUESTS_PER_MINUTE if limit is None else limit
        with self.lock:
            now = self.clock()
            failures = self.failures[ip]
            self._trim(failures, FAILED_AUTH_WINDOW, now)
            if len(failures) >= FAILED_AUTH_LIMIT:
                return int(failures[0] + FAILED_AUTH_WINDOW - now) + 1
            requests = self.requests[ip]
            self._trim(requests, 60, now)
            if len(requests) >= limit:
                return int(requests[0] + 60 - now) + 1
            requests.append(now)
            if len(self.requests) > 1000:  # forget quiet addresses
                for key in [k for k, v in self.requests.items() if not v]:
                    del self.requests[key]
            return 0

    def failed(self, ip):
        with self.lock:
            self.failures[ip].append(self.clock())


# --- Validation of pushed rows ---

class Invalid(ValueError):
    """A pushed body or row that doesn't follow the format; the whole request is refused (422)."""


def need(cond, message):
    if not cond:
        raise Invalid(message)


def timestamp(row, field, optional=False):
    value = row.get(field)
    if value is None and optional:
        return None
    need(isinstance(value, str), f"{field} must be a timestamp")
    try:
        return sync.parse_utc(value)
    except ValueError:
        raise Invalid(f"{field} isn't a UTC timestamp like 2026-11-10T04:05:00Z") from None


def check_interval(row):
    need(isinstance(row, dict), "intervals must be objects")
    try:
        need(isinstance(row.get("id"), str) and str(uuid.UUID(row["id"])) == row["id"].lower(), "id must be a UUID")
    except ValueError:
        raise Invalid("id must be a UUID") from None
    need(row.get("kind") in INTERVAL_KINDS, f"kind must be one of {', '.join(INTERVAL_KINDS)}")
    need(row.get("source") in SOURCES, f"source must be one of {', '.join(SOURCES)}")
    deleted = timestamp(row, "deleted_at", optional=True)
    start = timestamp(row, "start_utc", optional=deleted is not None)
    end = timestamp(row, "end_utc", optional=deleted is not None)
    need(start is None or end is None or end > start, "end_utc must be after start_utc")
    return timestamp(row, "updated_at")


def check_usage(row):
    need(isinstance(row, dict), "app_usage_daily rows must be objects")
    try:
        need(isinstance(row.get("local_date"), str) and date.fromisoformat(row["local_date"]).isoformat()
             == row["local_date"], "local_date must be YYYY-MM-DD")
    except ValueError:
        raise Invalid("local_date must be YYYY-MM-DD") from None
    need(isinstance(row.get("app"), str) and 1 <= len(row["app"]) <= 255, "app must be 1-255 characters")
    need(row.get("monitor") is None or (isinstance(row["monitor"], str) and len(row["monitor"]) <= 100),
         "monitor must be empty or up to 100 characters")
    for field in ("focus_s", "cursor_s", "active_s"):
        value = row.get(field, 0)
        need(isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 90000,
             f"{field} must be a whole number of seconds from 0 to 90000")
    return timestamp(row, "updated_at")


def usage_key(row):
    return f"{row['local_date']}|{row['app']}|{row.get('monitor') or ''}"


def check_push(body):
    """The pushed lists with each row's updated_at, or Invalid."""
    need(isinstance(body, dict), "the body must be a JSON object")
    need(set(body) <= set(sync.TABLES), f"unknown fields: {', '.join(sorted(set(body) - set(sync.TABLES)))}")
    out = {}
    for table, check in (("intervals", check_interval), ("app_usage_daily", check_usage)):
        rows = body.get(table, [])
        need(isinstance(rows, list), f"{table} must be a list")
        need(len(rows) <= MAX_ROWS, f"at most {MAX_ROWS} {table} rows per request")
        out[table] = [(row, check(row)) for row in rows]
    return out


# --- Phone app usage (the window doesn't show it yet; kept so nothing the phone sends is lost) ---

def usage_path():
    return os.path.join(data_dir(), "phone_app_usage.json")


def load_usage():
    try:
        with open(usage_path()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


_usage_lock = threading.Lock()


def apply_push(pushed):
    """Merge checked rows into this PC's store: phone intervals into the pulled intervals (as sync
    pulls them), app usage into phone_app_usage.json. Returns the server's push answer."""
    limit = time.time() + FUTURE_LIMIT
    result = {table: {"accepted": 0, "ignored": 0, "rejected": []} for table in sync.TABLES}
    with sync._lock:
        before = sync.load_remote()
        remote = copy.deepcopy(before)
        for row, updated in pushed["intervals"]:
            part = result["intervals"]
            if updated > limit:
                part["rejected"].append({"id": row["id"], "reason": "updated_at_in_future"})
            elif row["kind"] not in PHONE_KINDS:
                part["rejected"].append({"id": row["id"], "reason": "kind_not_allowed"})
            else:
                old = remote["intervals"].get(row["id"]) or remote["deleted"].get(row["id"])
                newer = not old or old[3] < updated
                sync.apply_pulled(remote, row)
                part["accepted" if newer else "ignored"] += 1
        if remote != before:
            sync.save_remote(remote)
    with _usage_lock:
        usage, changed = load_usage(), False
        for row, updated in pushed["app_usage_daily"]:
            part, key = result["app_usage_daily"], usage_key(row)
            if updated > limit:
                part["rejected"].append({"id": key, "reason": "updated_at_in_future"})
            elif key in usage and sync.parse_utc(usage[key]["updated_at"]) >= updated:
                part["ignored"] += 1
            else:
                usage[key] = {k: row.get(k) for k in ("local_date", "app", "monitor", "focus_s", "cursor_s",
                                                      "active_s", "updated_at")}
                part["accepted"] += 1
                changed = True
        if changed:
            write_json(usage_path(), usage)
    return result


# --- What the phone pulls: this PC's own rows, as a change feed ---

def feed_path():
    return os.path.join(data_dir(), "peer_feed.json")


class Feed:
    """This PC's own rows (from its logs), each numbered by when it last changed, so the phone can
    pull "everything after N". A row that disappears from a log is served as deleted.

    peer_feed.json: {"seq": last number, "done_days": [...], "rows": {key: {"t": table, "d": day,
    "fp": fingerprint, "s": number, "u": updated_at, "row": row, "del": deleted}}}
    """

    def __init__(self, today_log):
        self.today_log = today_log  # () -> (today's date, today's log)
        self.lock = threading.Lock()

    def load(self):
        try:
            with open(feed_path()) as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("seq"), int) and isinstance(data.get("rows"), dict):
                data.setdefault("done_days", [])
                return data
        except (OSError, ValueError):
            pass
        return {"seq": 0, "done_days": [], "rows": {}}

    def refresh(self, feed):
        """Number the rows that changed since the last look. Returns whether anything did."""
        today, today_log = self.today_log()
        install_id = sync.load_state()["install_id"]
        done, changed, stamp = set(feed["done_days"]), False, now_iso()
        by_day = collections.defaultdict(set)
        for key, entry in feed["rows"].items():
            by_day[entry["d"]].add(key)
        for day_text in sorted(logged_days() | set(by_day) | {today.isoformat()}):  # by_day: also logs since removed
            if day_text in done and day_text != today.isoformat():
                continue
            log = today_log if day_text == today.isoformat() else read_log(day_text)
            day = date.fromisoformat(day_text)
            seen = set()
            for table, key, fp, row in sync.day_rows(install_id, day, log or {}):
                seen.add(key)
                entry = feed["rows"].get(key)
                if entry and entry["fp"] == fp and not entry["del"]:
                    continue
                feed["seq"] += 1
                feed["rows"][key] = {"t": table, "d": day_text, "fp": fp, "s": feed["seq"], "u": stamp,
                                     "row": row, "del": False}
                changed = True
            for key in by_day[day_text] - seen:  # gone from the log (e.g. after an import)
                entry = feed["rows"][key]
                if not entry["del"] and entry["t"] == "intervals":
                    feed["seq"] += 1
                    entry.update(s=feed["seq"], u=stamp, fp="deleted", **{"del": True})
                    changed = True
            if day <= today - timedelta(days=2) and day_text not in done:  # an old day won't change anymore
                feed["done_days"].append(day_text)
                changed = True
        return changed

    def page(self, since, limit):
        """(intervals, app rows, next cursor, more?) after cursor `since`; ValueError if it's unknown."""
        with self.lock:
            feed = self.load()
            if self.refresh(feed):
                write_json(feed_path(), feed)
            if not since.isdigit() or int(since) > feed["seq"]:
                raise ValueError("unknown cursor")
            newer = sorted((e for e in feed["rows"].values() if e["s"] > int(since)), key=lambda e: e["s"])
            chosen = newer[:limit]
        rows = {table: [] for table in sync.TABLES}
        for entry in chosen:
            row = dict(entry["row"], updated_at=entry["u"])
            if entry["del"]:
                row["deleted_at"] = entry["u"]
            rows[entry["t"]].append(row)
        cursor = str(chosen[-1]["s"]) if chosen else since
        return rows["intervals"], rows["app_usage_daily"], cursor, len(newer) > limit


# --- HTTP ---

class Handler(BaseHTTPRequestHandler):
    server_version = "ScreenTimePeer/1"
    sys_version = ""  # don't announce the Python version
    timeout = CONNECTION_TIMEOUT

    def log_message(self, *args):
        pass

    def reply(self, code, obj, headers=()):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        if getattr(self, "proven", False):  # the next request's nonce, saving the phone a round trip
            self.send_header("X-Peer-Nonce", self.server.nonces.issue())
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def error(self, code, detail, headers=()):
        self.reply(code, {"detail": detail}, headers)

    def do_GET(self):
        self.serve("GET")

    def do_POST(self):
        self.serve("POST")

    def serve(self, method):
        ip = self.client_address[0]
        if not client_allowed(ip):
            return self.error(403, "only devices on your own network may connect")
        wait = self.server.limiter.check(ip)
        if wait:
            return self.error(429, "too many requests", [("Retry-After", str(wait))])
        path = urlsplit(self.path).path
        if method == "GET" and path == "/peer/nonce":
            return self.reply(200, {"nonce": self.server.nonces.issue(), "expires_in": NONCE_TTL})
        routes = {("GET", "/peer/hello"): self.hello, ("GET", "/peer/pull"): self.pull,
                  ("POST", "/peer/push"): self.push, ("POST", "/peer/pair"): self.pair}
        if (method, path) not in routes:
            return self.error(405 if path in {p for _, p in routes} else 404,
                              "Method Not Allowed" if path in {p for _, p in routes} else "Not Found")
        # The proof: Authorization: HMAC <nonce>:<mac>
        scheme, _, value = self.headers.get("Authorization", "").partition(" ")
        nonce, _, mac = value.strip().partition(":")
        if scheme != "HMAC" or not nonce or not mac:
            self.server.limiter.failed(ip)
            return self.error(401, "send Authorization: HMAC <nonce>:<mac> (see GET /peer/nonce)")
        if not self.server.nonces.take(nonce):  # used up even if the rest is wrong
            return self.error(401, "unknown, expired or already used nonce")
        body = b""
        if method == "POST":
            if self.headers.get("Transfer-Encoding"):
                return self.error(411, "send the body with a Content-Length")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self.error(411, "send the body with a Content-Length")
            if length > MAX_BODY:  # refused before reading any of it
                return self.error(413, f"the body may be at most {MAX_BODY} bytes")
            if length < 0:
                return self.error(400, "bad Content-Length")
            body = self.rfile.read(length)
        key = pairing_secret() if path == "/peer/pair" else phone_key()
        if not key or not hmac.compare_digest(mac.encode(), request_mac(key, method, self.path, nonce, body).encode()):
            self.server.limiter.failed(ip)
            return self.error(401, "no pairing is open" if path == "/peer/pair" and not key else
                              "this phone isn't paired" if not key else "wrong proof")
        self.proven = True
        if path != "/peer/pair" and confirm_pairing():  # the phone has its key: the QR secret is spent
            self.server.on_paired()
        routes[(method, path)](body)

    def json_body(self, body):
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError):
            self.error(400, "the body isn't JSON")
            return None

    def hello(self, body):
        state = sync.load_state()
        self.reply(200, {"device_id": state["install_id"], "type": "windows", "protocol": PROTOCOL,
                         "name": state["device_name"] or sync.default_device_name()})

    def pull(self, body):
        query = parse_qs(urlsplit(self.path).query)
        since = query.get("since", ["0"])[0]
        try:
            limit = int(query.get("limit", [str(PULL_LIMIT)])[0])
        except ValueError:
            return self.error(400, "limit must be a number")
        if not 1 <= limit <= MAX_PULL_LIMIT:
            return self.error(400, f"limit must be from 1 to {MAX_PULL_LIMIT}")
        try:
            intervals, usage, cursor, more = self.server.feed.page(since, limit)
        except ValueError:
            return self.error(400, "invalid cursor")
        self.synced()
        self.reply(200, {"intervals": intervals, "app_usage_daily": usage, "cursor": cursor, "has_more": more})

    def push(self, body):
        body = self.json_body(body)
        if body is None:
            return
        try:
            pushed = check_push(body)
        except Invalid as e:
            return self.error(422, str(e))
        result = apply_push(pushed)
        self.server.on_change()
        self.synced()
        self.reply(200, result)

    def synced(self):
        """A pull or push is about to be answered: the phone synced. Recorded first, so it's known by the time the
        phone has its answer (failures here must not break the answer)."""
        try:
            record_sync()
            self.server.on_synced()
        except Exception:  # noqa: BLE001
            pass

    def pair(self, body):
        body = self.json_body(body)
        if body is None:
            return
        if not (isinstance(body, dict) and all(isinstance(body.get(f), str) and 1 <= len(body[f].strip()) <= 100
                                               for f in ("device_id", "name"))):
            return self.error(422, "send {\"device_id\": ..., \"name\": ...} (1-100 characters each)")
        key = complete_pairing(body["device_id"].strip(), body["name"].strip())
        self.server.on_paired()
        state = sync.load_state()
        self.reply(200, {"key": key, "device_id": state["install_id"], "type": "windows", "protocol": PROTOCOL,
                         "name": state["device_name"] or sync.default_device_name()})

    def do_PUT(self):
        self.error(405, "Method Not Allowed")

    do_DELETE = do_PATCH = do_HEAD = do_PUT


class PeerServer(ThreadingHTTPServer):
    """HTTPS, a thread per connection (at most MAX_CONNECTIONS), the TLS handshake done in that thread."""
    daemon_threads = True
    allow_reuse_address = False  # Windows lets another program take the port too if this is on

    def __init__(self, address, context, feed, on_change, on_paired, on_synced=lambda: None):
        self.context, self.feed, self.on_change, self.on_paired = context, feed, on_change, on_paired
        self.on_synced = on_synced
        self.limiter = RateLimiter()
        self.nonces = Nonces()
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)  # busy: drop it rather than queue
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            request.settimeout(CONNECTION_TIMEOUT)
            request = self.context.wrap_socket(request, server_side=True)  # handshake here, not in accept()
        except (OSError, ssl.SSLError):
            self.shutdown_request(request)
            self.slots.release()
            return
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        pass  # a client that hangs up or speaks plain HTTP isn't worth a traceback


def discovery_reply(data, phone_id, pc_id, address, port):
    """The answer (bytes) to one discovery datagram, or None if it gets none: it must be
    {"type": "screentime-discover", "v": 1, "from": <the paired phone's device id>, "want": <this PC's install id>}."""
    if not phone_id or len(data) > MAX_DATAGRAM:
        return None
    try:
        msg = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None
    if not (isinstance(msg, dict) and msg.get("type") == "screentime-discover" and msg.get("v") == 1
            and msg.get("from") == phone_id and msg.get("want") == pc_id):
        return None
    return json.dumps({"type": "screentime-here", "v": 1, "device_id": pc_id,
                       "address": address, "port": port}, separators=(",", ":")).encode()


class Responder:
    """Answers the paired phone's discovery datagrams on (host, port) with (host, tcp_port). It binds to the
    listener's own address like the listener; on Windows that socket also gets the subnet's broadcasts."""

    def __init__(self, host, port, tcp_port, clock=time.monotonic):
        self.host, self.tcp_port = host, tcp_port
        self.limiter = RateLimiter(clock=clock)
        self.sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # no other program shares it
        except (AttributeError, OSError):
            pass
        try:
            self.sock.bind((host, port))
        except OSError:
            self.sock.close()
            raise
        self.sock.settimeout(0.25)
        self.address = self.sock.getsockname()[:2]
        self.stop_event = threading.Event()
        self.pc_id = sync.load_state()["install_id"]
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop_event.is_set():
            try:
                data, client = self.sock.recvfrom(MAX_DATAGRAM + 1)
            except socket.timeout:
                continue
            except OSError:  # closed, or Windows reports a lost earlier send (WSAECONNRESET) here
                if self.stop_event.is_set():
                    return
                continue
            try:
                self.answer(data, client)
            except Exception:  # noqa: BLE001 - one bad datagram must never stop the responder
                pass

    def answer(self, data, client):
        ip = client[0]
        if not client_allowed(ip) or len(data) > MAX_DATAGRAM:
            return
        phone = paired_phone()
        reply = discovery_reply(data, phone and phone.get("device_id"), self.pc_id, self.host, self.tcp_port)
        if reply is None:
            return
        if self.limiter.check(ip, limit=DISCOVERIES_PER_MINUTE):
            return  # over the limit: stay quiet
        self.sock.sendto(reply, client)

    def close(self):
        self.stop_event.set()
        self.sock.close()
        self.thread.join(2)


def tls_context():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(*certificate())
    return context


class PeerListener:
    """Runs the listener in the background while allowed. on_status gets a short text whenever it
    changes; on_change is called after the phone pushed rows, on_paired when a phone paired or
    first used its key, on_synced after each pull or push (see record_sync). host/port are for tests (the default is this PC's private network address
    and the configured port)."""

    def __init__(self, today_log, on_status=lambda text: None, on_change=lambda: None, on_paired=lambda: None,
                 host=None, port=None, discovery_port=None, on_synced=lambda: None):
        self.today_log, self.on_status, self.on_change, self.on_paired = today_log, on_status, on_change, on_paired
        self.on_synced = on_synced
        self.host, self.port = host, port
        self.discovery_port = DISCOVERY_PORT if discovery_port is None else discovery_port  # 0 in tests
        self.responder = None  # answers the phone's discovery datagrams while listening
        self.feed = Feed(today_log)
        self.thread = None
        self.stop_event = threading.Event()
        self.server = None
        self.address = None  # (host, port) while listening
        self.ready = threading.Event()  # set once listening (or given up for now), for tests

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event = threading.Event()
        self.ready.clear()
        self.thread = threading.Thread(target=self.run, args=(self.stop_event,), daemon=True)
        self.thread.start()

    def stop(self, wait=3.0):
        self.stop_event.set()
        thread = self.thread
        if thread and thread is not threading.current_thread():
            thread.join(wait)
        self.thread = None

    def run(self, stop):
        while not stop.is_set():
            host = self.host or lan_address()
            if not host or (self.host is None and not private_address(host)):
                self.status("Not on a private network, so the phone can't connect. Waiting for one...")
                self.ready.set()
                stop.wait(ADDRESS_CHECK)
                continue
            port = self.port if self.port is not None else int(load_config().get("port") or DEFAULT_PORT)
            try:
                server = PeerServer((host, port), tls_context(), self.feed, self.on_change, self.on_paired, self.on_synced)
            except (OSError, ssl.SSLError, ValueError) as e:
                self.status(f"Couldn't listen on {host}:{port}: {e}")
                self.ready.set()
                stop.wait(ADDRESS_CHECK)
                continue
            self.server, self.address = server, server.server_address[:2]
            serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True)
            serving.start()
            try:
                self.responder = Responder(host, self.discovery_port, self.address[1])
                found = ""
            except OSError as e:  # the phone can still connect at the address it knows
                found = f" (discovery off: UDP port {self.discovery_port}: {e})"
            self.status(f"Listening on {self.address[0]}:{self.address[1]}{found}")
            self.ready.set()
            while not stop.wait(ADDRESS_CHECK):
                if self.host is None and lan_address() != host:  # moved to another network
                    break
            if self.responder:
                self.responder.close()
                self.responder = None
            server.shutdown()
            server.server_close()
            self.server = self.address = None
        self.status("Off")

    def status(self, text):
        try:
            self.on_status(text)
        except Exception:  # noqa: BLE001 - a status callback must never stop the listener
            pass


def main():
    config = load_config()
    cert, _ = certificate()
    print(f"Allow phone sync: {'on' if config['enabled'] else 'off'}")
    print(f"Address:          {lan_address() or '(no network)'}:{config['port']}")
    print(f"Discovery:        UDP port {DISCOVERY_PORT} (answers only the paired phone)")
    print(f"Certificate:      {tlscert.fingerprint(cert)}")
    phone = paired_phone()
    print(f"Paired phone:     {phone['name'] + ' since ' + phone['since'] if phone else '(none)'}")
    if phone:
        print(f"Last synced:      {phone.get('last_sync') or '(not yet)'}")


if __name__ == "__main__":
    sys.exit(main())
