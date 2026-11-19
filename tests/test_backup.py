"""Tests for Export backup / Import backup in screentime.sync: the round trip, and merging with the
newest-updated_at-wins rule. Uses a scratch logs folder only; no server or credential needed."""
import copy
import json
import os
import sys
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from screentime import sync, tracker  # noqa: E402

SCRATCH = tempfile.mkdtemp(prefix="st_backup_")
tracker.LOG_DIR = os.path.join(SCRATCH, "logs")
os.makedirs(tracker.LOG_DIR)
sync.CRED_TARGET = "ScreenTimeSyncTEST"


def check(cond, name):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(1)


def write_log(day, log):
    with open(tracker.log_path(day), "w") as f:
        json.dump(log, f)


def read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def make_log(shift=0):
    return {"total_seconds": 7200.0 + shift, "active_seconds": 3000.0, "stopped": True,
            "on_timeline": [[32400 + shift, 36000], [43200, 46800]],
            "active_timeline": [[32500 + shift, 34000], [44000, 45500]],
            "focus": {"Code.exe|DISPLAY1 (primary)": 1500.4, "chrome.exe|DISPLAY2": 300.0},
            "cursor": {"Code.exe|DISPLAY1 (primary)": 1200.0},
            "active": {"Code.exe|DISPLAY1 (primary)": 1480.0, "chrome.exe|DISPLAY2": 305.0, "notepad.exe|": 20.0}}


def fingerprints(day):
    log = sync.read_log(day.isoformat())
    return {key: fp for _, key, fp, _ in sync.day_rows(install_id, day, log)} if log else None


def backup_path(name):
    return os.path.join(SCRATCH, name)


def save_backup(name, backup):
    with open(backup_path(name), "w") as f:
        json.dump(backup, f)
    return backup_path(name)


def utc(day, hour, minute=0):
    return datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute) \
        .astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


today = date.today()
days = [today - timedelta(days=n) for n in (5, 3, 1)]
for i, d in enumerate(days):
    write_log(d, make_log(shift=60 * i))
today_log = make_log(shift=600)
write_log(today, today_log)

state = sync.load_state()
state["device_name"] = "TestPC"
state["transports"]["server"]["done_days"] = [days[0].isoformat(), days[1].isoformat()]
sync.save_state(state)
install_id = state["install_id"]

# The pulled store: two phone periods, a night of sleep and a remembered deletion
ids = {k: str(uuid.uuid4()) for k in ("p1", "p2", "sleep", "gone")}
remote = sync.empty_remote()
for key, kind, start, end, updated in (
        ("p1", "phone_active", utc(days[2], 8), utc(days[2], 8, 30), "2026-11-17T05:00:00.928605Z"),
        ("p2", "phone_active", utc(days[2], 12), utc(days[2], 12, 20), "2026-11-17T05:00:01Z"),
        ("sleep", "sleep", utc(days[1], 23), utc(days[2], 7), "2026-11-17T06:00:00.5Z")):
    sync.apply_pulled(remote, {"id": ids[key], "kind": kind, "start_utc": start, "end_utc": end,
                               "updated_at": updated, "device_id": 2})
sync.apply_pulled(remote, {"id": ids["gone"], "kind": "phone_active", "start_utc": utc(days[2], 15),
                           "end_utc": utc(days[2], 15, 10), "updated_at": "2026-11-17T07:00:00Z",
                           "deleted_at": "2026-11-17T07:00:00Z", "device_id": 2})
sync.save_remote(remote)
original_remote = copy.deepcopy(sync.load_remote())
original = {d: fingerprints(d) for d in days}

# --- timestamps survive the trip exactly ---
for text in ("2026-11-17T05:00:00.928605Z", "2026-11-17T05:00:00.000000Z", "2026-11-17T05:00:00.500000Z"):
    check(sync.iso_epoch(sync.parse_utc(text)) == text, f"timestamp round trip {text}")
check(sync.iso_epoch(sync.parse_utc("2026-11-17T05:00:00Z")) == "2026-11-17T05:00:00.000000Z", "whole seconds")
check(sync.iso_epoch(None) is None, "no time stays None")

# --- export ---
path = backup_path("backup.json")
own_count, pulled_count = sync.export_backup(path, today, today_log)
with open(path, encoding="utf-8") as f:
    backup = json.load(f)
check(backup["format"] == "screentime-backup" and backup["version"] == 1, "format and version")
check(backup["device"] == {"install_id": install_id, "type": "windows", "name": "TestPC"}, "device block")
want_rows = sum(len(sync.day_rows(install_id, d, sync.read_log(d.isoformat()))) for d in days) \
    + len(sync.day_rows(install_id, today, today_log))
check(own_count == want_rows == len(backup["own"]["intervals"]) + len(backup["own"]["app_usage_daily"]),
      f"every own row exported ({own_count})")
check(len(backup["own"]["intervals"]) == 4 * 4 and len(backup["own"]["app_usage_daily"]) == 3 * 4,
      "4 intervals and 3 app rows per day")
check(pulled_count == 4 and len(backup["remote"]["intervals"]) == 4, "pulled rows and the deletion exported")
gone = next(r for r in backup["remote"]["intervals"] if r["id"] == ids["gone"])
check(gone["deleted_at"] and gone["kind"] == "phone_active", "deletion exported as a deleted row")
p1 = next(r for r in backup["remote"]["intervals"] if r["id"] == ids["p1"])
check(p1["updated_at"] == "2026-11-17T05:00:00.928605Z" and p1["start_utc"] == utc(days[2], 8)[:-1] + ".000000Z",
      "pulled row times exported exactly")
stamps = {r["updated_at"] for r in backup["own"]["intervals"] if r["start_utc"] >= utc(days[0], 0)
          and r["start_utc"] < utc(days[0] + timedelta(days=1), 0)}
check(stamps == {sync.log_stamp(days[0].isoformat())}, "own rows of a past day carry the log's save time")
check(all(set(r) >= {"id", "kind", "start_utc", "end_utc", "source", "deleted_at", "updated_at"}
          for r in backup["own"]["intervals"]), "own intervals are in the server's push format")
check(not os.path.exists(path + ".tmp"), "no temp file left behind")

# --- importing it straight back changes nothing ---
before = {d: (read_bytes(tracker.log_path(d)), os.stat(tracker.log_path(d)).st_mtime_ns) for d in days + [today]}
remote_bytes = read_bytes(sync.remote_path())
result = sync.import_backup(path, today)
check(result == {"changed": 0, "days": [], "skipped": 0}, f"re-import: nothing to do {result}")
check(all((read_bytes(tracker.log_path(d)), os.stat(tracker.log_path(d)).st_mtime_ns) == before[d]
          for d in before), "logs not rewritten")
check(read_bytes(sync.remote_path()) == remote_bytes, "pulled store not rewritten")

# --- round trip: lost logs and pulled data come back the same ---
os.remove(tracker.log_path(days[0]))
os.remove(tracker.log_path(days[1]))
os.remove(sync.remote_path())
result = sync.import_backup(path, today)
check(result["days"] == [days[0], days[1]] and result["changed"] == 3 and result["skipped"] == 0,
      f"restored the 2 lost days and the 3 pulled periods {result}")
check(all(fingerprints(d) == original[d] for d in days), "restored logs give the same rows as before")
check(sync.load_remote()["intervals"] == original_remote["intervals"], "pulled periods restored exactly")
check(sync.load_remote()["deleted"] == original_remote["deleted"], "the deletion is restored too")
restored = tracker.load(days[0])
check(restored["stopped"] is True and restored["total_seconds"] >= sum(b - a for a, b in restored["on_timeline"]),
      "restored log loads, with a total covering its PC-on periods")
check(restored["focus"]["Code.exe|DISPLAY1 (primary)"] == 1500 and restored["active"]["notepad.exe|"] == 20,
      "app times restored under the tracker's keys (monitor-less too)")
check(days[0].isoformat() not in sync.load_state()["transports"]["server"]["done_days"],
      "a restored day is looked at again on the next sync")
check(days[1].isoformat() not in sync.load_state()["transports"]["server"]["done_days"], "... both restored days")
check(sync.import_backup(path, today) == {"changed": 0, "days": [], "skipped": 0}, "importing again: nothing to do")

# --- pulled rows: the newer copy wins ---
b2 = copy.deepcopy(backup)
rows = {r["id"]: r for r in b2["remote"]["intervals"]}
rows[ids["p1"]].update(end_utc=utc(days[2], 8, 45), updated_at="2026-11-18T00:00:00Z")  # newer: wins
rows[ids["p2"]].update(end_utc=utc(days[2], 12, 50), updated_at="2026-11-01T00:00:00Z")  # older: loses
rows[ids["sleep"]].update(deleted_at="2026-11-18T00:00:00Z", updated_at="2026-11-18T00:00:00Z")  # newer delete
rows[ids["gone"]].update(deleted_at=None, updated_at="2026-11-10T00:00:00Z")  # older than our deletion
new_id = str(uuid.uuid4())
b2["remote"]["intervals"].append({"id": new_id, "kind": "phone_active", "start_utc": utc(days[2], 18),
                                  "end_utc": utc(days[2], 18, 5), "updated_at": "2026-11-18T00:00:00Z"})
b2["remote"]["intervals"].append({"id": str(uuid.uuid4()), "kind": "pc_on", "start_utc": utc(days[2], 1),
                                  "end_utc": utc(days[2], 2), "updated_at": "2026-11-18T00:00:00Z"})
result = sync.import_backup(save_backup("b2.json", b2), today)
store = sync.load_remote()
check(result["changed"] == 3, f"3 pulled rows changed: newer p1, deleted sleep, new row ({result['changed']})")
check(store["intervals"][ids["p1"]][2] == sync.parse_utc(utc(days[2], 8, 45)), "newer copy replaced ours")
check(store["intervals"][ids["p2"]][2] == sync.parse_utc(utc(days[2], 12, 20)), "older copy ignored")
check(ids["sleep"] not in store["intervals"] and ids["sleep"] in store["deleted"], "newer deletion applied")
check(ids["gone"] not in store["intervals"], "older copy doesn't bring back a row we saw deleted")
check(new_id in store["intervals"], "new row added")
check(all(v[0] in sync.REMOTE_KINDS for v in store["intervals"].values()), "another PC's rows are not kept")

unseen = str(uuid.uuid4())
only_delete = {"format": "screentime-backup", "version": 1, "device": {"install_id": "other"},
               "remote": {"intervals": [{"id": unseen, "kind": "sleep", "start_utc": None, "end_utc": None,
                                         "updated_at": "2026-11-18T00:00:00Z", "deleted_at": "2026-11-18T00:00:00Z"}]}}
result = sync.import_backup(save_backup("only_delete.json", only_delete), today)
check(result["changed"] == 0 and unseen in sync.load_remote()["deleted"],
      "a backup holding only a deletion of a row we never had is still remembered")

# --- this PC's own rows: the newer copy of each day wins; local-only data is kept ---
d = days[2]
local = make_log(shift=120)
local["focus"]["only-local.exe|DISPLAY1"] = 99.0
write_log(d, local)  # saved after the export: newer than the backup
b3 = copy.deepcopy(backup)
for r in b3["own"]["intervals"]:
    if r["kind"] == "pc_on" and r["end_utc"] == utc(d, 13):
        r["end_utc"] = utc(d, 14)
check(sync.import_backup(save_backup("b3.json", b3), today)["days"] == [], "local log newer than the backup: kept")
check(sync.read_log(d.isoformat()) == local, "... unchanged")

os.utime(tracker.log_path(d), (946684800, 946684800))  # now the local log is older (year 2000)
gone_start = sync.utc_iso(datetime.combine(d, datetime.min.time()), 44000)
for r in b3["own"]["intervals"]:
    if r["kind"] == "pc_active" and r["start_utc"] == gone_start:
        r["deleted_at"] = r["updated_at"]  # deleted in the newer copy
b3["own"]["app_usage_daily"] = [dict(r, focus_s=1600) if r["local_date"] == d.isoformat() and r["app"] == "Code.exe"
                                else r for r in b3["own"]["app_usage_daily"]]
result = sync.import_backup(save_backup("b3.json", b3), today)
merged = sync.read_log(d.isoformat())
check(result["days"] == [d], f"older local log: the backup's day merged in {result}")
check(merged["on_timeline"] == [[32520, 36000], [43200, 50400]],
      f"the backup's longer period replaced ours, the other stays {merged['on_timeline']}")
check(merged["active_timeline"] == [[32620, 34000]], f"a deleted period is removed {merged['active_timeline']}")
check(merged["focus"]["Code.exe|DISPLAY1 (primary)"] == 1600, "the backup's newer app time replaced ours")
check(merged["focus"]["only-local.exe|DISPLAY1"] == 99.0, "an app only in the local log is kept")
check(merged["total_seconds"] >= sum(b - a for a, b in merged["on_timeline"]), "total covers the merged periods")

# --- today's log is never overwritten ---
os.utime(tracker.log_path(today), (946684800, 946684800))
today_bytes = read_bytes(tracker.log_path(today))
b4 = copy.deepcopy(backup)
for r in b4["own"]["intervals"] + b4["own"]["app_usage_daily"]:
    r["updated_at"] = "2099-01-01T00:00:00Z"
sync.import_backup(save_backup("b4.json", b4), today)
check(read_bytes(tracker.log_path(today)) == today_bytes, "today's log left alone even by a newer backup")

# --- a backup of another install or a phone: no own rows, its phone rows count ---
os.remove(tracker.log_path(days[0]))
b5 = copy.deepcopy(backup)
b5["device"] = {"install_id": str(uuid.uuid4()), "type": "android", "name": "Phone"}
phone_id = str(uuid.uuid4())
b5["own"]["intervals"].append({"id": phone_id, "kind": "phone_active", "start_utc": utc(days[2], 20),
                               "end_utc": utc(days[2], 20, 15), "updated_at": "2026-11-18T00:00:00Z",
                               "source": "tracker", "deleted_at": None})
result = sync.import_backup(save_backup("b5.json", b5), today)
check(result["days"] == [] and not os.path.exists(tracker.log_path(days[0])),
      "another install's PC rows don't go into this PC's logs")
check(phone_id in sync.load_remote()["intervals"] and result["changed"] == 1, "a phone backup's own rows are pulled in")

# --- forged IDs in a backup of this install are skipped ---
b6 = copy.deepcopy(backup)
moved = next(r for r in b6["own"]["intervals"] if r["start_utc"] >= utc(days[0], 0) and r["kind"] == "pc_on")
moved["start_utc"] = utc(days[0], 1)
result = sync.import_backup(save_backup("b6.json", b6), today)
check(result["skipped"] == 1 and result["days"] == [days[0]], f"forged row skipped, the rest restored {result}")
check(not any(a == 3600 for a, _ in sync.read_log(days[0].isoformat())["on_timeline"]), "forged row not written")

# --- bad files change nothing ---
snapshot = {p: read_bytes(p) for p in [sync.remote_path(), sync.state_path()] + [tracker.log_path(x) for x in days]}
bad_remote = copy.deepcopy(backup)
bad_remote["remote"]["intervals"].append({"id": "z", "kind": "sleep", "start_utc": "yesterday",
                                          "end_utc": "today", "updated_at": "2099-01-01T00:00:00Z"})
bad_own = copy.deepcopy(backup)
for r in bad_own["own"]["intervals"]:
    r["updated_at"] = "2099-01-01T00:00:00Z"
bad_own["own"]["app_usage_daily"][0]["local_date"] = "not a date"
newer = dict(backup, version=2)
with open(backup_path("not.json"), "w") as f:
    f.write("{nope")
cases = [("not JSON", backup_path("not.json"), "not a JSON"),
         ("missing file", backup_path("missing.json"), "Couldn't read"),
         ("another format", save_backup("other.json", {"format": "something", "version": 1}), "isn't a Screen Time"),
         ("newer version", save_backup("v2.json", newer), "newer version"),
         ("rows not a list", save_backup("rows.json", dict(backup, remote={"intervals": {}})), "damaged"),
         ("damaged pulled row", save_backup("badr.json", bad_remote), "damaged row"),
         ("damaged own row", save_backup("bado.json", bad_own), "damaged row")]
for name, p, words in cases:
    try:
        sync.import_backup(p, today)
        check(False, f"{name}: rejected")
    except sync.BackupError as e:
        check(words in str(e), f"{name}: rejected ({e})")
check(all(read_bytes(p) == b for p, b in snapshot.items()), "rejected imports changed no file")

print("ALL BACKUP CHECKS PASSED")
