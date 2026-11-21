"""Spans of one local day from the intervals pulled from the server (stdlib only)."""
from datetime import datetime, time, timedelta

DAY = 86400
SLEEP_JOIN_GAP = 90 * 60  # sleep periods less than this apart (seconds) are one session: a short wake-up doesn't split a night
SLEEP_REACH = 12 * 3600  # how far before and after the day sleep is read, to find a session that started the day before


def merge(spans):
    """Sorted [start, end] spans with overlapping or touching ones joined."""
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def day_spans(remote, day, kind):
    """[start, end] seconds after the PC's local midnight of `day`, for one kind of interval in
    `remote` (see sync.load_remote). Intervals crossing midnight are cut off at the day's edges."""
    first = datetime.combine(day, time.min).timestamp()
    last = datetime.combine(day + timedelta(days=1), time.min).timestamp()
    spans = []
    for kind_, start, end, *_ in remote["intervals"].values():
        if kind_ == kind and end > first and start < last:
            a, b = round(max(start, first) - first), min(round(min(end, last) - first), DAY)
            if b > a:
                spans.append([a, b])
    return merge(spans)


def total(spans):
    return sum(end - start for start, end in spans)


def sleep_sessions(remote, first, last):
    """Whole sleep sessions [start, end] (epoch seconds) touching first - SLEEP_REACH .. last + SLEEP_REACH:
    the pulled sleep intervals merged, with gaps under SLEEP_JOIN_GAP joined."""
    sessions = []
    rows = sorted((start, end) for kind, start, end, *_ in remote["intervals"].values()
                  if kind == "sleep" and end > start and end > first - SLEEP_REACH and start < last + SLEEP_REACH)
    for start, end in rows:
        if sessions and start - sessions[-1][1] < SLEEP_JOIN_GAP:
            sessions[-1][1] = max(sessions[-1][1], end)
        else:
            sessions.append([start, end])
    return sessions


def sleep_row(remote, day):
    """(spans, total) of the Sleep row of `day`. The spans are cut to the day exactly like day_spans, each as
    [start, end, full_start, full_end] where full_* is its whole session on the same scale (may be negative or
    past DAY). total is the whole length of the sessions that end on this day (a night belongs to the day it
    ends on), or None if none does."""
    first = datetime.combine(day, time.min).timestamp()
    last = datetime.combine(day + timedelta(days=1), time.min).timestamp()
    sessions = sleep_sessions(remote, first, last)
    spans = []
    for start, end in day_spans(remote, day, "sleep"):
        at = first + start
        session = next((s for s in sessions if s[0] <= at + 0.5 and at < s[1]), None)  # +0.5: day_spans rounds
        spans.append([start, end] + ([round(session[0] - first), round(session[1] - first)] if session else []))
    owned = [s for s in sessions if first < s[1] <= last]
    return spans, (round(sum(end - start for start, end in owned)) if owned else None)


def clock(seconds):
    """07:05 from seconds after midnight (24:00 at the end of the day)."""
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"


def span_times(span):
    """(time range text, seconds) of a span: its own, or for a sleep span its whole session, marked when that
    starts the day before or ends the day after."""
    start, end = span[2:4] if len(span) >= 4 else span[:2]
    text_from = f"{clock(start % DAY)} (yesterday)" if start < 0 else clock(start)
    text_until = f"{clock(end - DAY)} (tomorrow)" if end > DAY else clock(end)
    return f"{text_from} – {text_until}", end - start
