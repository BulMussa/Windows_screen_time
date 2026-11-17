"""Spans of one local day from the intervals pulled from the server (stdlib only)."""
from datetime import datetime, time, timedelta

DAY = 86400


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
