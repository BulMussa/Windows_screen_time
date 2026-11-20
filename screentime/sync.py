"""Sync with your other devices (Windows, stdlib only).

Sends each day's PC-on and active periods and per-app times to other devices and pulls theirs
(phone and sleep periods). Each way of doing that is a transport with two calls: send the rows
that changed, and fetch the rows changed since a cursor. This PC uses one: your own Screen Time
server over HTTPS. The other route, "direct", is the phone connecting to this PC (peer.py); its rows
go into the same store with the same merge rule (replaces), and each stored row remembers its routes.
The sync mode setting picks server, direct or both; in direct, this PC doesn't sync with the server.

The password is used once to register this PC; only the device token is kept, in Windows
Credential Manager. Everything else (server address, sync mode, and per transport what was
already sent and the pull cursor) is in sync_state.json next to the logs folder.

Backups (export_backup / import_backup) are a JSON file with this PC's own rows and the pulled
rows; importing merges them with the same newest-updated_at-wins rule as a pull.

Days are the PC's own calendar days, so keep this PC on the timezone set for your account.
"""
import copy
import ctypes
import json
import os
import re
import socket
import threading
import urllib.error
import urllib.request
import uuid
from ctypes import wintypes
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlencode, urlsplit

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
REMOTE_KINDS = ("phone_active", "sleep")  # the pulled intervals the window shows; this PC's own come from its logs
PULL_LIMIT = 1000  # rows per pull request
TRANSPORTS = ("server", "direct")  # each keeps its own progress in state["transports"]
MODES = {"server": ("server",), "direct": ("direct",), "both": ("server", "direct")}  # mode -> transports
DEFAULT_MODE = "server"
# Mode "Phone directly": this PC doesn't sync with the server; the phone connects to this PC instead
DIRECT_MODE_STATUS = ("Server sync is off (Sync with: Phone directly). The phone syncs with this PC itself "
                      "while Allow phone sync is on.")
ROUTES = ("server", "direct")  # how rows from other devices arrive: pulled from the server, pushed by the phone
OWN_TIMELINES = {"pc_on": "on_timeline", "pc_active": "active_timeline"}  # this PC's interval kinds
APP_FIELDS = {"focus_s": "focus", "cursor_s": "cursor", "active_s": "active"}  # app row field -> log kind
TABLES = ("intervals", "app_usage_daily")
BACKUP_FORMAT, BACKUP_VERSION = "screentime-backup", 1

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


class CursorUnknown(SyncError):
    """The other side doesn't know the pull cursor (e.g. the server was restored from an older backup)."""


class BackupError(Exception):
    """A backup file that can't be read or imported; the message says why."""


# --- Secrets in Credential Manager (the server's device token; peer.py keeps the phone's there too) ---

def cred_write(target, text):
    blob = text.encode()
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred = CREDENTIAL()
    cred.Type, cred.TargetName, cred.UserName = CRED_TYPE_GENERIC, target, "ScreenTime"
    cred.CredentialBlobSize, cred.CredentialBlob = len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = CRED_PERSIST_LOCAL_MACHINE
    if not advapi32.CredWriteW(ctypes.byref(cred), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def cred_read(target):
    """The text saved under target, or None."""
    ptr = ctypes.POINTER(CREDENTIAL)()
    if not advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
        return None
    try:
        cred = ptr.contents
        return ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize).decode()
    finally:
        advapi32.CredFree(ctypes.cast(ptr, wintypes.LPVOID))


def cred_delete(target):
    advapi32.CredDeleteW(target, CRED_TYPE_GENERIC, 0)  # fails harmlessly if there is none


def save_token(token):
    cred_write(CRED_TARGET, token)


def load_token():
    """The saved device token, or None."""
    return cred_read(CRED_TARGET)


def delete_token():
    cred_delete(CRED_TARGET)


# --- State file ---

def state_path():
    """sync_state.json next to the logs folder (looked up through tracker, so tests can redirect it)."""
    return os.path.join(os.path.dirname(tracker.LOG_DIR), "sync_state.json")


def fresh_progress():
    """A transport's progress: what was last sent to it, old days fully sent, and its pull cursor."""
    return {"sent": {},  # row key -> what was last sent for it, so only changes go out again
            "done_days": [],  # old days that are fully sent and not looked at again
            "cursor": "0"}


def load_state():
    state = {"install_id": None,  # names this PC's rows on the server; kept when reconnecting
             "server": None, "device_id": None, "device_name": None,
             "mode": DEFAULT_MODE,
             "transports": {},  # transport name -> fresh_progress()
             "last_sync": None}
    loaded = {}
    try:
        with open(state_path()) as f:
            loaded = json.load(f)
        state.update(loaded)
    except (OSError, ValueError, TypeError):
        pass
    legacy_sent, legacy_done = state.pop("sent", None), state.pop("done_days", None)
    if "transports" not in loaded:  # written before transports: it was all the server's
        state["transports"] = {"server": {"sent": legacy_sent or {}, "done_days": legacy_done or [],
                                          "cursor": str(load_remote().get("cursor", "0"))}}
    for name in TRANSPORTS:
        progress = state["transports"].setdefault(name, {})
        for key, value in fresh_progress().items():
            progress.setdefault(key, value)
    if state["mode"] not in MODES:
        state["mode"] = DEFAULT_MODE
    if not state["install_id"]:
        state["install_id"] = new_install_id()
    return state


_install_lock = threading.Lock()  # not _lock: load_state also runs while a sync holds that


def new_install_id():
    """Make this install's ID and save it at once, so it stays the same even if nothing else is ever
    saved (e.g. a PC that only syncs with the phone directly, or only makes backups)."""
    with _install_lock:
        try:
            with open(state_path()) as f:
                saved = json.load(f).get("install_id")
            if saved:
                return saved  # another thread just made it
        except (OSError, ValueError, AttributeError):
            saved = None
        install_id = str(uuid.uuid4())
        try:
            path = state_path()
            try:
                with open(path) as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            data["install_id"] = install_id
            save_state(data)
        except OSError:
            pass  # read-only folder: it will be made again next time, as before
        return install_id


def save_state(state):
    path = state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(state, f, indent=2)
    os.replace(path + ".tmp", path)


def remote_path():
    """Intervals pulled from other devices, next to the state file."""
    return os.path.join(os.path.dirname(tracker.LOG_DIR), "remote_intervals.json")


_remote_cache = {"stamp": None, "data": None}


def empty_remote():
    return {"intervals": {}, "deleted": {}}


def load_remote():
    """{"intervals": {id: [kind, start, end, updated_at, device_id, routes]}, "deleted": {id: same}} (times
    are UTC epoch seconds; "deleted" remembers deletions, so an older copy can't bring a row back; routes
    lists how the row arrived, "server" and/or "direct", and is missing in entries saved before it existed).
    The result is replaced, never changed, when the file does, so readers can keep it."""
    path = remote_path()
    try:
        stamp = (path, os.stat(path).st_mtime_ns)
    except OSError:
        return empty_remote()
    if _remote_cache["stamp"] != stamp:
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return empty_remote()
        data.setdefault("intervals", {})
        data.setdefault("deleted", {})
        _remote_cache.update(stamp=stamp, data=data)
    return _remote_cache["data"]


def save_remote(remote):
    path = remote_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump({"intervals": remote["intervals"], "deleted": remote["deleted"]}, f)
    os.replace(path + ".tmp", path)


def entry_routes(entry):
    """How a stored row arrived; an entry saved before routes were kept counts as both, so forgetting one
    route never drops what the other may have delivered."""
    return list(entry[5]) if len(entry) > 5 else list(ROUTES)


def forget_route(remote, route):
    """Forget what only `route` delivered (e.g. after disconnecting the server). Rows the other route also
    delivered stay, without this route; imported rows (no route) are never touched."""
    for store in (remote["intervals"], remote["deleted"]):
        for row_id, entry in list(store.items()):
            routes = entry_routes(entry)
            if routes == [route]:
                del store[row_id]
            elif route in routes and len(entry) > 5:
                entry[5] = [r for r in routes if r != route]


def forget_server_route():
    """After connecting to another server or disconnecting: drop what only the server delivered. What the
    phone pushed directly stays, and only the server's cursor starts over (the caller resets it)."""
    remote = copy.deepcopy(load_remote())
    forget_route(remote, "server")
    save_remote(remote)


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
        state.update(server=server, device_id=device["device_id"], device_name=device["name"], last_sync=None)
        state["transports"]["server"] = fresh_progress()  # a new server has its own history to pull
        forget_server_route()
        save_state(state)


def disconnect():
    """Forget the token and server. The device stays registered on the server."""
    with _lock:
        delete_token()
        state = load_state()
        state.update(server=None, device_id=None, device_name=None, last_sync=None)
        state["transports"]["server"] = fresh_progress()
        forget_server_route()
        save_state(state)


def get_mode():
    return load_state()["mode"]


def set_mode(mode):
    """Set the sync mode: "server", "direct" or "both". Waits for a running sync, so the window
    calls it off the Tk thread."""
    if mode not in MODES:
        raise ValueError(f"unknown sync mode {mode!r}")
    with _lock:
        state = load_state()
        state["mode"] = mode
        save_state(state)


def uses_server(mode):
    return "server" in MODES[mode]


# --- Transports ---

class Transport:
    """One way of swapping rows with other devices. Its progress (what was sent to it, its pull
    cursor) is kept per transport in state["transports"][name]."""
    name = None

    def send(self, rows):
        """rows: {"intervals": [...], "app_usage_daily": [...]}, each row with updated_at.
        Returns {row key: reason} for the rows the other side rejected; raises SyncError."""
        raise NotImplementedError

    def fetch(self, cursor, limit):
        """(interval rows changed since cursor, the next cursor, whether more are waiting).
        Raises CursorUnknown if the other side doesn't know the cursor, else SyncError."""
        raise NotImplementedError


class ServerTransport(Transport):
    """Your own Screen Time server: POST /v1/sync/push and GET /v1/sync/pull."""
    name = "server"

    def __init__(self, server, token):
        self.server, self.token = server, token

    def send(self, rows):
        result = request(self.server, "POST", "/v1/sync/push", rows, token=self.token)
        rejected = {}
        for name in rows:
            rejected.update({r["id"]: r["reason"] for r in result[name]["rejected"]})
        return rejected

    def fetch(self, cursor, limit):
        query = urlencode({"cursor": cursor, "limit": limit})
        try:
            page = request(self.server, "GET", f"/v1/sync/pull?{query}", token=self.token)
        except SyncError as e:
            if e.status == 400:
                raise CursorUnknown(str(e), e.status, e.retry_after) from None
            raise
        try:
            return page["intervals"], page["cursor"], page["has_more"]
        except (KeyError, TypeError) as e:
            raise SyncError(f"Unexpected data from the server ({e})") from None


def transport_for(name, state):
    """The transport called name, ready to use; raises SyncError if it can't be used now."""
    if name == "server":
        token = load_token()
        if not state["server"] or not token:
            raise SyncError("Not connected")
        return ServerTransport(state["server"], token)
    if name == "direct":
        raise SyncError("This PC doesn't connect to the phone: the phone connects to it (Allow phone sync).")
    raise ValueError(f"unknown transport {name!r}")


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


def pending_rows(install_id, progress, today, today_log):
    """The rows not sent to a transport yet or changed since, and the days they were looked up for."""
    done = set(progress["done_days"])
    pending, looked_at = [], []
    for day_text in sorted(logged_days() | {today.isoformat()}):
        if day_text in done and day_text != today.isoformat():
            continue
        log = today_log if day_text == today.isoformat() else read_log(day_text)
        if not log:
            continue
        day = datetime.strptime(day_text, "%Y-%m-%d").date()
        looked_at.append(day)
        pending += [r for r in day_rows(install_id, day, log) if progress["sent"].get(r[1]) != r[2]]
    return pending, looked_at


def sync_once(today, today_log, name="server"):
    """Send what changed to one transport. Returns (rows sent, {row key: reason it was rejected});
    raises SyncError."""
    with _lock:
        state = load_state()
        transport = transport_for(name, state)
        progress = state["transports"][name]
        pending, looked_at = pending_rows(state["install_id"], progress, today, today_log)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sent, rejected = 0, {}
        for i in range(0, len(pending), BATCH):
            chunk = pending[i:i + BATCH]
            body = {table: [dict(row, updated_at=now) for n, _, _, row in chunk if n == table] for table in TABLES}
            bad = transport.send(body)
            for _, key, fingerprint, _ in chunk:
                if key in bad:
                    rejected[key] = bad[key]
                else:
                    progress["sent"][key] = fingerprint
                    sent += 1
            save_state(state)  # progress survives a failure in a later batch
        if not rejected:  # a day older than yesterday won't change anymore
            cutoff = today - timedelta(days=2)
            progress["done_days"] = sorted(set(progress["done_days"]) | {d.isoformat() for d in looked_at if d <= cutoff})
        state["last_sync"] = datetime.now().isoformat(timespec="seconds")
        save_state(state)
        return sent, rejected


def parse_utc(text):
    """UTC epoch seconds of a server timestamp like 2026-11-10T04:05:00Z or ...00.928605Z."""
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z", text)
    if not match:
        raise ValueError(f"unexpected timestamp {text!r}")
    base = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return base.timestamp() + float(match.group(2) or 0)


def iso_micros(micros):
    """A timestamp that parse_utc reads back exactly, from whole microseconds since the epoch."""
    secs, frac = divmod(int(micros), 1_000_000)
    return datetime.fromtimestamp(secs, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + f".{frac:06d}Z"


def iso_epoch(seconds):
    """The timestamp of UTC epoch seconds as parse_utc returned them (None stays None)."""
    return None if seconds is None else iso_micros(round(seconds * 1_000_000))


def replaces(old_updated, old_deleted, updated, deleted):
    """The merge rule, the same on the phone and for both routes: the newer updated_at wins; on the same
    updated_at a deletion wins over a live copy (so the result doesn't depend on which route was heard
    first); otherwise ours stays, so the same row arriving by both routes is applied once."""
    if updated != old_updated:
        return updated > old_updated
    return deleted and not old_deleted


def apply_pulled(remote, row, route=None):
    """Apply one interval from another device with replaces(); deleted ones are removed (and remembered).
    route ("server" or "direct") is recorded on the row even when ours is kept; None (an import) adds none.
    Returns whether a stored interval changed."""
    if row["kind"] not in REMOTE_KINDS:
        return False
    updated = parse_utc(row["updated_at"])
    deleted = bool(row.get("deleted_at"))
    live, gone = remote["intervals"].get(row["id"]), remote["deleted"].get(row["id"])
    old = live or gone
    routes = entry_routes(old) if old else []
    if route and route not in routes:
        routes.append(route)
    if old and not replaces(old[3], old is gone, updated, deleted):
        if route and len(old) > 5 and route not in old[5]:
            old[5] = sorted(routes)
        return False
    if deleted:
        when = lambda text: parse_utc(text) if text else None
        remote["deleted"][row["id"]] = [row["kind"], when(row.get("start_utc")), when(row.get("end_utc")),
                                        updated, row.get("device_id"), sorted(routes)]
        return remote["intervals"].pop(row["id"], None) is not None
    remote["intervals"][row["id"]] = [row["kind"], parse_utc(row["start_utc"]), parse_utc(row["end_utc"]),
                                      updated, row.get("device_id"), sorted(routes)]
    remote["deleted"].pop(row["id"], None)
    return True


def pull_once(name="server"):
    """Fetch what changed on one transport since its cursor (phone and sleep intervals are kept).

    Returns how many stored intervals changed; raises SyncError.
    """
    with _lock:
        state = load_state()
        transport = transport_for(name, state)
        progress = state["transports"][name]
        remote = copy.deepcopy(load_remote())
        changed, restarted = 0, False
        while True:
            try:
                rows, cursor, more = transport.fetch(progress["cursor"], PULL_LIMIT)
            except CursorUnknown:
                # The other side doesn't know our cursor (e.g. it was restored from an older backup): pull this
                # transport from the start. Only its cursor moves; rows stay, and replaces() keeps the newer copies.
                if progress["cursor"] != "0" and not restarted:
                    progress["cursor"], restarted = "0", True
                    save_state(state)
                    continue
                raise
            try:
                changed += sum(apply_pulled(remote, row, name) for row in rows)
            except (KeyError, ValueError, TypeError) as e:
                raise SyncError(f"Unexpected data from the server ({e})") from None
            save_remote(remote)
            progress["cursor"] = cursor  # the cursor moves only after its page is applied and saved
            save_state(state)
            if not more:
                return changed


# --- Backup ---

def log_stamp(day_text):
    """When a day's log was last saved, as a timestamp like a row's updated_at; None if there is none."""
    try:
        return iso_micros(os.stat(os.path.join(tracker.LOG_DIR, f"{day_text}.json")).st_mtime_ns // 1000)
    except OSError:
        return None


def export_backup(path, today, today_log):
    """Write a backup of this PC's own rows and the pulled rows to path. Returns (own rows, pulled rows).

    Own rows are in the format sent to the server. Their updated_at is when that day's log was
    last saved (now for today), so an import can tell which copy is newer. Pulled rows include the
    deletions seen, so an import elsewhere deletes them too.
    """
    with _lock:
        state = load_state()
        remote = load_remote()
        own = {table: [] for table in TABLES}
        now = iso_micros(round(datetime.now(timezone.utc).timestamp() * 1_000_000))
        for day_text in sorted(logged_days() | {today.isoformat()}):
            if day_text == today.isoformat():
                log, stamp = today_log, now
            else:
                log, stamp = read_log(day_text), log_stamp(day_text)
            if not log or not stamp:
                continue
            day = datetime.strptime(day_text, "%Y-%m-%d").date()
            for table, _, _, row in day_rows(state["install_id"], day, log):
                own[table].append(dict(row, updated_at=stamp))
        pulled = []
        for deleted, store in ((False, remote["intervals"]), (True, remote["deleted"])):
            for row_id, (kind, start, end, updated, device_id, *_) in sorted(store.items()):
                pulled.append({"id": row_id, "kind": kind, "start_utc": iso_epoch(start), "end_utc": iso_epoch(end),
                               "updated_at": iso_epoch(updated), "device_id": device_id,
                               "deleted_at": iso_epoch(updated) if deleted else None})
        backup = {"format": BACKUP_FORMAT, "version": BACKUP_VERSION, "exported_at": now,
                  "device": {"install_id": state["install_id"], "type": "windows",
                             "name": state["device_name"] or default_device_name()},
                  "own": own, "remote": {"intervals": pulled}}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(backup, f, indent=1)
    os.replace(path + ".tmp", path)
    return sum(len(rows) for rows in own.values()), len(pulled)


def read_backup(path):
    """The backup at path, checked; raises BackupError."""
    try:
        with open(path, encoding="utf-8") as f:
            backup = json.load(f)
    except OSError as e:
        raise BackupError(f"Couldn't read the file: {e}") from None
    except ValueError:
        raise BackupError("This isn't a Screen Time backup (not a JSON file).") from None
    if not isinstance(backup, dict) or backup.get("format") != BACKUP_FORMAT:
        raise BackupError("This isn't a Screen Time backup.")
    if not isinstance(backup.get("version"), int) or backup["version"] > BACKUP_VERSION:
        raise BackupError("This backup is from a newer version of Screen Time. Update the app to import it.")
    own, remote, device = backup.get("own") or {}, backup.get("remote") or {}, backup.get("device") or {}
    if not all(isinstance(x, dict) for x in (own, remote, device)):
        raise BackupError("The backup is damaged (unexpected sections).")
    sections = (own.get("intervals", []), own.get("app_usage_daily", []), remote.get("intervals", []))
    if not all(isinstance(rows, list) and all(isinstance(r, dict) for r in rows) for rows in sections):
        raise BackupError("The backup is damaged (its rows aren't lists of objects).")
    return backup


def import_backup(path, today):
    """Merge a backup into this PC's data: for each row, the copy with the newer updated_at wins.

    Pulled rows (phone and sleep, also those among a phone backup's own rows) go into the pulled
    store. This PC's own rows, only from a backup of this same install, restore missing or older
    periods and app times in past days' logs; today's log is being written right now, so it wins.
    Returns {"changed": pulled intervals changed, "days": [days restored], "skipped": own rows whose
    ID doesn't belong to this PC}. Raises BackupError, and then nothing was changed.
    """
    backup = read_backup(path)
    own = backup.get("own") or {}
    with _lock:
        state = load_state()
        before = load_remote()
        remote = copy.deepcopy(before)
        rows = (backup.get("remote") or {}).get("intervals", []) + own.get("intervals", [])
        try:
            changed = sum(apply_pulled(remote, row) for row in rows)
        except (KeyError, ValueError, TypeError, AttributeError) as e:
            raise BackupError(f"The backup has a damaged row ({e}).") from None
        updates, skipped = {}, 0
        if (backup.get("device") or {}).get("install_id") == state["install_id"]:
            try:
                updates, skipped = own_updates(state["install_id"], own, today)
            except (KeyError, ValueError, TypeError, AttributeError, OverflowError, OSError) as e:
                raise BackupError(f"The backup has a damaged row of this PC ({e}).") from None
        logs = {}
        for day, items in updates.items():
            log = merge_into_log(day, items)
            if log is not None:
                logs[day] = log
        if remote != before:  # also when only a deletion was learned
            save_remote(remote)
        for day, log in sorted(logs.items()):
            write_log(day, log)
        if logs:  # look at restored days again on the next sync, in case they hold rows not sent yet
            restored = {day.isoformat() for day in logs}
            for progress in state["transports"].values():
                progress["done_days"] = [d for d in progress["done_days"] if d not in restored]
            save_state(state)
        return {"changed": changed, "days": sorted(logs), "skipped": skipped}


def own_updates(install_id, own, today):
    """This PC's rows in a backup by day, {day: [(updated_at, what, *values)]}, and how many rows were
    skipped because their ID doesn't belong to this PC. Days from today on are left out."""
    updates, skipped = {}, 0
    for row in own.get("intervals", []):
        timeline = OWN_TIMELINES.get(row["kind"])
        if not timeline:
            continue
        start_local = datetime.fromtimestamp(parse_utc(row["start_utc"]))  # naive local time, like the logs
        day = start_local.date()
        midnight = datetime.combine(day, time.min)
        start = round((start_local - midnight).total_seconds())
        end = round((datetime.fromtimestamp(parse_utc(row["end_utc"])) - midnight).total_seconds())
        if row["id"] != str(uuid.uuid5(INTERVAL_NAMESPACE, f"{install_id}|{row['kind']}|{day}|{start}")):
            skipped += 1  # another PC's row, or one changed by hand
            continue
        if day < today:
            updates.setdefault(day, []).append((parse_utc(row["updated_at"]), "interval", timeline, start, end,
                                                bool(row.get("deleted_at"))))
    for row in own.get("app_usage_daily", []):
        day = date.fromisoformat(row["local_date"])
        if day < today and row["app"]:
            values = {kind: int(row.get(field) or 0) for field, kind in APP_FIELDS.items()}
            updates.setdefault(day, []).append((parse_utc(row["updated_at"]), "app",
                                                f"{row['app']}|{row.get('monitor') or ''}", values))
    return updates, skipped


def merge_into_log(day, items):
    """The day's log with the backup's rows that are newer than it merged in; None if none are."""
    stamp = log_stamp(day.isoformat())
    local = parse_utc(stamp) if stamp else None
    newer = [item for item in items if local is None or item[0] > local]
    if not newer:
        return None
    log = tracker.load(day)
    for _, what, *values in newer:
        if what == "interval":
            timeline, start, end, deleted = values
            spans = [span for span in log[timeline] if span[0] != start]
            if not deleted and end > start:
                spans.append([start, end])
            log[timeline] = sorted(spans)
        else:
            key, by_kind = values
            for kind, secs in by_kind.items():
                if secs:
                    log[kind][key] = float(secs)
                else:
                    log[kind].pop(key, None)
    on, active = (sum(end - start for start, end in log[t]) for t in ("on_timeline", "active_timeline"))
    log["total_seconds"] = max(log["total_seconds"], float(on))
    log["active_seconds"] = max(log["active_seconds"], float(active))
    if local is None:
        log["stopped"] = True  # a restored day, not a run that ended unexpectedly
    return log


def write_log(day, log):
    """Save a past day's log like the tracker does (temp file swapped in), leaving its run fields alone."""
    path = tracker.log_path(day)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(log, f, indent=2)
    os.replace(path + ".tmp", path)


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
            mode = get_mode()
            if not uses_server(mode):  # "Phone directly": the phone connects to this PC; nothing to do here
                self.on_status(DIRECT_MODE_STATUS)
                return SYNC_EVERY
            sent, rejected = sync_once(self.tracker.day, self.tracker.snapshot())
            pull_once()
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
