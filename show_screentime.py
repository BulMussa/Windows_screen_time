"""Show the overall screen time per day from the logs saved by screentime_tracker.py.

Run:     python show_screentime.py        (all days)
         python show_screentime.py 7      (last 7 days)
"""
import glob
import json
import os
import sys

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


def fmt(seconds):
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s"


def main():
    paths = sorted(glob.glob(os.path.join(LOG_DIR, "????-??-??.json")))
    if len(sys.argv) > 1:
        paths = paths[-int(sys.argv[1]):]
    if not paths:
        print(f"No logs found in {LOG_DIR}")
        return

    print(f"{'Date':<12} {'PC on':>12} {'Active':>12}  Top app")
    totals = [0.0, 0.0]
    for path in paths:
        try:
            with open(path) as f:
                log = json.load(f)
        except (OSError, ValueError):
            continue
        day = os.path.splitext(os.path.basename(path))[0]
        on = log.get("total_seconds", 0.0)
        active = log.get("active_seconds", 0.0)
        apps = {}
        for key, secs in log.get("focus", {}).items():
            app = key.split("|")[0]
            apps[app] = apps.get(app, 0.0) + secs
        top = max(apps, key=apps.get) if apps else "-"
        totals[0] += on
        totals[1] += active
        print(f"{day:<12} {fmt(on):>12} {fmt(active):>12}  {top}")

    days = len(paths)
    print("-" * 52)
    print(f"{'Total':<12} {fmt(totals[0]):>12} {fmt(totals[1]):>12}")
    print(f"{'Average':<12} {fmt(totals[0] / days):>12} {fmt(totals[1] / days):>12}  ({days} day{'s' if days != 1 else ''})")


if __name__ == "__main__":
    main()
