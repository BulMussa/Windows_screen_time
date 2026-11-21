"""Tests for the Sleep row: bars cut to the day, totals of whole sessions counted on the day they end on, and the
hover text. Pure functions on a made-up pulled store; nothing is written anywhere."""
import os
import sys
from datetime import date, datetime, time, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from screentime import timeline  # noqa: E402

H = 3600
DAY = date(2026, 11, 18)
MIDNIGHT = datetime.combine(DAY, time.min).timestamp()


def check(cond, name):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(1)


def store(*periods, kind="sleep"):
    """A pulled store (see sync.load_remote) with (start, end) periods in seconds after DAY's midnight."""
    return {"intervals": {f"id{i}": [kind, MIDNIGHT + a, MIDNIGHT + b, "2026-11-18T07:00:00Z", 2, ["server"]]
                          for i, (a, b) in enumerate(periods)}, "deleted": {}}


def hover(span):
    return timeline.span_times(span)


# --- a night across midnight: 6 h on the bar, 8 h in the total ---
spans, total = timeline.sleep_row(store((-2 * H, 6 * H)), DAY)
check(spans == [[0, 6 * H, -2 * H, 6 * H]], f"bar cut at midnight, carrying the whole session ({spans})")
check(timeline.total([s[:2] for s in spans]) == 6 * H, "6 h visible")
check(total == 8 * H, f"total is the whole 8 h night ({total})")
check(hover(spans[0]) == ("22:00 (yesterday) – 06:00", 8 * H), f"hover shows the whole night ({hover(spans[0])})")
check(timeline.day_spans(store((-2 * H, 6 * H)), DAY, "sleep") == [[0, 6 * H]], "day_spans unchanged")

# --- a short wake-up keeps one session; the bar still shows the gap ---
spans, total = timeline.sleep_row(store((-1 * H, 2 * H), (2 * H + 1800, 6 * H + 1800)), DAY)
check(spans == [[0, 2 * H, -H, 6 * H + 1800], [2 * H + 1800, 6 * H + 1800, -H, 6 * H + 1800]],
      f"both pieces belong to one session ({spans})")
check(total == 7 * H + 1800, f"total 7h 30m from 23:00 to 06:30 ({total})")
check(hover(spans[1]) == ("23:00 (yesterday) – 06:30", 7 * H + 1800), "hovering either piece shows the night")

# --- a wake-up of SLEEP_JOIN_GAP or more splits it ---
gap = timeline.SLEEP_JOIN_GAP
check(gap == 90 * 60, "join gap is 90 minutes, as on the phone")
spans, total = timeline.sleep_row(store((H, 2 * H), (2 * H + gap, 4 * H + gap)), DAY)
check([s[2] for s in spans] == [H, 2 * H + gap] and total == 3 * H, f"long wake-up: two sessions ({spans}, {total})")

# --- a nap and a night on one day ---
spans, total = timeline.sleep_row(store((-2 * H, 6 * H), (14 * H, 15 * H)), DAY)
check(len(spans) == 2 and total == 9 * H, f"night + nap = 9 h ({total})")
check(hover(spans[1]) == ("14:00 – 15:00", H), "nap hover is its own times")

# --- no sleep ---
check(timeline.sleep_row(store(), DAY) == ([], None), "no sleep: no spans, no total")
check(timeline.sleep_row(store((-30 * H, -20 * H)), DAY) == ([], None), "sleep only on other days: nothing")
check(timeline.sleep_row(store((1 * H, 2 * H), kind="phone_active"), DAY) == ([], None), "other kinds don't count")

# --- a session ending tomorrow: shown on the right, counted tomorrow ---
spans, total = timeline.sleep_row(store((-2 * H, 6 * H), (22 * H, 30 * H)), DAY)
check(spans[1] == [22 * H, 86400, 22 * H, 30 * H], f"tonight's piece cut at 24:00 ({spans[1]})")
check(total == 8 * H, "tonight isn't in today's total")
check(hover(spans[1]) == ("22:00 – 06:00 (tomorrow)", 8 * H), f"hover says tomorrow ({hover(spans[1])})")
check(timeline.sleep_row(store((22 * H, 30 * H)), DAY) == ([[22 * H, 86400, 22 * H, 30 * H]], None),
      "only tonight: a bar but no total")
spans, total = timeline.sleep_row(store((22 * H, 30 * H)), DAY + timedelta(days=1))
check(spans == [[0, 6 * H, -2 * H, 6 * H]] and total == 8 * H, "and tomorrow counts it")
check(timeline.sleep_row(store((22 * H, 24 * H)), DAY)[1] == 2 * H, "a session ending at 24:00 belongs to the day")

# --- other rows keep their text ---
check(hover([7 * H + 1800, 8 * H + 900]) == ("07:30 – 08:15", 2700), "a plain span shows its own times")
check(hover([20 * H, 86400]) == ("20:00 – 24:00", 4 * H), "a plain span to the end of the day")
print("all timeline tests passed")
