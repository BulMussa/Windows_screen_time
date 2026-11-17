"""Sync with your own Screen Time server (Windows, stdlib only).

Sends each day's PC-on and active periods and per-app times to the server over HTTPS, so
other devices can show them. The password is used once to register this PC; only the device
token is kept, in Windows Credential Manager. Everything else (server address, what was
already sent) is in sync_state.json next to the logs folder.

Days are the PC's own calendar days, so keep this PC on the timezone set for your account.
"""
import ctypes
import json
import os
import socket
import threading
import urllib.error
import urllib.request
import uuid
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from . import tracker
from .report import logged_days, read_log

SYNC_EVERY = 300  # seconds between syncs
RETRY_AFTER_FAILURE = 60  # seconds before trying again when the server can't be reached
REVOKED_RETRY = 3600  # a rejected token won't fix itself; just check back now and then
BATCH = 1000  # rows per request (the server takes up to 5000 of each kind)
TIMEOUT = 20
CRED_TARGET = "ScreenTimeSync"  # where the device token is kept in Credential Manager
INTERVAL_NAMESPACE = uuid.UUID("5c1a7e0e-3f0b-4b53-9d7c-6b1f4a0e2a10")  # makes the interval IDs repeatable
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")  # the only addresses allowed without HTTPS

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
CRED_TYPE_GENERIC, CRED_PERSIST_LOCAL_MACHINE = 1, 2


class CREDENTIAL(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                ("Attributes", wintypes.LPVOID), ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR)]


advapi32.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
advapi32.CredWriteW.restype = wintypes.BOOL
advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
advapi32.CredReadW.restype = wintypes.BOOL
advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
advapi32.CredDeleteW.restype = wintypes.BOOL
advapi32.CredFree.argtypes = [wintypes.LPVOID]

_lock = threading.Lock()  # one connect, disconnect or sync at a time


class SyncError(Exception):
    """status is the HTTP status, or None if the server couldn't be reached."""

    def __init__(self, message, status=None, retry_after=None):
        super().__init__(message)
        self.status, self.retry_after = status, retry_after


# --- Device token in Credential Manager ---

def save_token(token):
    blob = token.encode()
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred = CREDENTIAL()
    cred.Type, cred.TargetName, cred.UserName = CRED_TYPE_GENERIC, CRED_TARGET, "ScreenTime"
    cred.CredentialBlobSize, cred.CredentialBlob = len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = CRED_PERSIST_LOCAL_MACHINE
    if not advapi32.CredWriteW(ctypes.byref(cred), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def load_token():
    """The saved device token, or None."""
    ptr = ctypes.POINTER(CREDENTIAL)()
    if not advapi32.CredReadW(CRED_TARGET, CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
        return None
    try:
        cred = ptr.contents
        return ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize).decode()
    finally:
        advapi32.CredFree(ctypes.cast(ptr, wintypes.LPVOID))


def delete_token():
    advapi32.CredDeleteW(CRED_TARGET, CRED_TYPE_GENERIC, 0)  # fails harmlessly if there is none


# --- State file ---

def state_path():
    """sync_state.json next to the logs folder (looked up through tracker, so tests can redirect it)."""
    return os.path.join(os.path.dirname(tracker.LOG_DIR), "sync_state.json")


def load_state():
    state = {"install_id": None,  # names this PC's rows on the server; kept when reconnecting
             "server": None, "device_id": None, "device_name": None,
             "sent": {},  # row key -> what was last sent for it, so only changes go out again
             "done_days": [],  # old days that are fully sent and not looked at again
             "last_sync": None}
    try:
        with open(state_path()) as f:
            state.update(json.load(f))
    except (OSError, ValueError):
        pass
    if not state["install_id"]:
        state["install_id"] = str(uuid.uuid4())
    return state


def save_state(state):
    path = state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(state, f, indent=2)
    os.replace(path + ".tmp", path)


def connected():
    return bool(load_state()["server"] and load_token())


def default_device_name():
    return socket.gethostname()


# --- Server calls ---

def check_server(server):
    """The server address cleaned up; the password only goes over HTTPS (or to this PC itself)."""
    server = server.strip().rstrip("/")
    parts = urlsplit(server)
    if not parts.hostname:
        raise SyncError("Enter the server address, e.g. https://myserver.example:8443")
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS):
        raise SyncError("The server address must start with https://")
    return server


def request(server, method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(server + path, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = str(json.loads(e.read()).get("detail", ""))
        except (ValueError, AttributeError, OSError):
            pass
        raise SyncError(detail or f"The server answered with an error ({e.code})", e.code,
                        e.headers.get("Retry-After")) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SyncError(f"Can't reach the server ({getattr(e, 'reason', None) or e})") from None


def connect(server, name, password, device_name):
    """Log in, register this PC as a device and keep its token. The password is not kept."""
    server = check_server(server)
    name, device_name = name.strip(), device_name.strip()
    if not name or not password or not device_name:
        raise SyncError("Fill in the name, password and device name.")
    with _lock:
        try:
            login = request(server, "POST", "/v1/auth/login", {"name": name, "password": password})
        except SyncError as e:
            if e.status == 429:
                minutes = max(1, int(e.retry_after or 900) // 60)
                raise SyncError(f"Too many failed logins. Try again in {minutes} min.", 429) from None
            raise
        device = request(server, "POST", "/v1/devices", {"name": device_name, "type": "windows"},
                         token=login["session_token"])
        save_token(device["token"])
        state = load_state()
        state.update(server=server, device_id=device["device_id"], device_name=device["name"],
                     sent={}, done_days=[], last_sync=None)
        save_state(state)


def disconnect():
    """Forget the token and server. The device stays registered on the server."""
    with _lock:
        delete_token()
        state = load_state()
        state.update(server=None, device_id=None, device_name=None, sent={}, done_days=[], last_sync=None)
        save_state(state)


# --- What to send ---

def utc_iso(local_midnight, seconds):
    """UTC time, as the server wants it, of a moment `seconds` after the PC's local midnight."""
    moment = (local_midnight + timedelta(seconds=seconds)).astimezone(timezone.utc)  # naive = local
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def day_rows(install_id, day, log):
    """[(list name, key, fingerprint, row)] for one day's log, without updated_at.

    The key identifies the row on the server and the fingerprint changes when its data does;
    an interval that is still growing keeps its ID, so the server just gets a longer end.
    """
    rows = []
    midnight = datetime.combine(day, datetime.min.time())
    for kind, timeline in (("pc_on", "on_timeline"), ("pc_active", "active_timeline")):
        for start, end in log.get(timeline) or []:
            start, end = int(start), int(end)
            if end <= start:
                continue
            row = {"id": str(uuid.uuid5(INTERVAL_NAMESPACE, f"{install_id}|{kind}|{day}|{start}")),
                   "kind": kind, "start_utc": utc_iso(midnight, start), "end_utc": utc_iso(midnight, end),
                   "source": "tracker", "deleted_at": None}
            rows.append(("intervals", row["id"], row["end_utc"], row))

    apps = {}
    for field, kind in (("focus_s", "focus"), ("cursor_s", "cursor"), ("active_s", "active")):
        for key, secs in (log.get(kind) or {}).items():
            app, _, monitor = key.partition("|")
            if app:
                values = apps.setdefault((app[:255], monitor[:100]), {"focus_s": 0, "cursor_s": 0, "active_s": 0})
                values[field] = min(90000, round(secs))
    for (app, monitor), values in apps.items():
        if any(values.values()):
            row = {"local_date": day.isoformat(), "app": app, "monitor": monitor or None, **values}
            rows.append(("app_usage_daily", f"{day.isoformat()}|{app}|{monitor}",
                         f"{values['focus_s']},{values['cursor_s']},{values['active_s']}", row))
    return rows


def pending_rows(state, today, today_log):
    """The rows that weren't sent yet or changed since, and the days they were looked up for."""
    done = set(state["done_days"])
    pending, looked_at = [], []
    for day_text in sorted(logged_days() | {today.isoformat()}):
        if day_text in done and day_text != today.isoformat():
            continue
        log = today_log if day_text == today.isoformat() else read_log(day_text)
        if not log:
            continue
        day = datetime.strptime(day_text, "%Y-%m-%d").date()
        looked_at.append(day)
        pending += [r for r in day_rows(state["install_id"], day, log) if state["sent"].get(r[1]) != r[2]]
    return pending, looked_at


def sync_once(today, today_log):
    """Send what changed. Returns (rows sent, {row key: reason it was rejected}); raises SyncError."""
    with _lock:
        state, token = load_state(), load_token()
        if not state["server"] or not token:
            raise SyncError("Not connected")
        pending, looked_at = pending_rows(state, today, today_log)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sent, rejected = 0, {}
        for i in range(0, len(pending), BATCH):
            chunk = pending[i:i + BATCH]
            body = {name: [dict(row, updated_at=now) for n, _, _, row in chunk if n == name]
                    for name in ("intervals", "app_usage_daily")}
            result = request(state["server"], "POST", "/v1/sync/push", body, token=token)
            bad = {}
            for name in body:
                bad.update({r["id"]: r["reason"] for r in result[name]["rejected"]})
            for _, key, fingerprint, _ in chunk:
                if key in bad:
                    rejected[key] = bad[key]
                else:
                    state["sent"][key] = fingerprint
                    sent += 1
            save_state(state)  # progress survives a failure in a later batch
        if not rejected:  # a day older than yesterday won't change anymore
            cutoff = today - timedelta(days=2)
            state["done_days"] = sorted(set(state["done_days"]) | {d.isoformat() for d in looked_at if d <= cutoff})
        state["last_sync"] = datetime.now().isoformat(timespec="seconds")
        save_state(state)
        return sent, rejected


class SyncWorker:
    """Syncs in the background every SYNC_EVERY seconds; on_status gets a short text after each try."""

    def __init__(self, tracker, on_status):
        self.tracker, self.on_status = tracker, on_status
        self.stop = threading.Event()
        self.wake = threading.Event()

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def sync_now(self):
        self.wake.set()

    def close(self):
        self.stop.set()
        self.wake.set()

    def run(self):
        wait = 5  # the first sync comes soon after start
        while True:
            self.wake.wait(wait)
            self.wake.clear()
            if self.stop.is_set():
                return
            wait = self.cycle()

    def cycle(self):
        """One sync try; returns how many seconds to wait before the next."""
        try:
            sent, rejected = sync_once(self.tracker.day, self.tracker.snapshot())
        except SyncError as e:
            if str(e) == "Not connected":
                self.on_status("Not connected")
                return SYNC_EVERY
            if e.status == 401:
                self.on_status("The server doesn't accept this device anymore. Disconnect and connect again.")
                return REVOKED_RETRY
            if e.status is None or e.status >= 500:
                self.on_status(f"Offline, trying again soon. {e}")
                return RETRY_AFTER_FAILURE
            self.on_status(f"The server refused the data: {e}")
            return SYNC_EVERY
        except OSError as e:  # the state file or the Credential Manager
            self.on_status(f"Couldn't sync: {e}")
            return SYNC_EVERY
        text = f"Synced at {datetime.now():%H:%M}"
        if rejected:
            text += f", {len(rejected)} rows rejected"
            if "updated_at_in_future" in rejected.values():
                text += " (this PC's clock is ahead of the server's)"
        self.on_status(text)
        return SYNC_EVERY
