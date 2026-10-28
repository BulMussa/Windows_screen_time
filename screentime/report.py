"""Show the overall screen time per day from the saved logs.

Run:     python -m screentime.report        (all days)
         python -m screentime.report 7      (last 7 days)
"""
import glob
import json
import os
import sys

from .tracker import LOG_DIR, fmt


def read_logs(last=None):
    """Return [(date string, log dict)] for the saved days, oldest first."""
    paths = sorted(glob.glob(os.path.join(LOG_DIR, "????-??-??.json")))
    if last:
        paths = paths[-last:]
    logs = []
    for path in paths:
        try:
            with open(path) as f:
                logs.append((os.path.splitext(os.path.basename(path))[0], json.load(f)))
        except (OSError, ValueError):
            continue
    return logs


def summarize(log):
    """Return (PC on seconds, active seconds, top app) for one day's log."""
    apps = {}
    for key, secs in log.get("focus", {}).items():
        app = key.split("|")[0]
        apps[app] = apps.get(app, 0.0) + secs
    top = max(apps, key=apps.get) if apps else "-"
    return log.get("total_seconds", 0.0), log.get("active_seconds", 0.0), top


def main():
    logs = read_logs(int(sys.argv[1]) if len(sys.argv) > 1 else None)
    if not logs:
        print(f"No logs found in {LOG_DIR}")
        return

    print(f"{'Date':<12} {'PC on':>12} {'Active':>12}  Top app")
    totals = [0.0, 0.0]
    for day, log in logs:
        on, active, top = summarize(log)
        totals[0] += on
        totals[1] += active
        print(f"{day:<12} {fmt(on):>12} {fmt(active):>12}  {top}")

    days = len(logs)
    print("-" * 52)
    print(f"{'Total':<12} {fmt(totals[0]):>12} {fmt(totals[1]):>12}")
    print(f"{'Average':<12} {fmt(totals[0] / days):>12} {fmt(totals[1] / days):>12}  ({days} day{'s' if days != 1 else ''})")


if __name__ == "__main__":
    main()
