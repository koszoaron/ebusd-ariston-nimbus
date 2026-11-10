"""Timestamps with explicit UTC offsets.

ebusd logs naive local time. When daylight saving ends, the hour before the change repeats
(03:00 back to 02:00), so a naive timestamp in that hour names two instants. LocalClock resolves
naive timestamps in stream order: within the repeated hour it keeps the first offset until the
clock jumps back, then switches to the second. Everything written by this package carries the
offset (``2026-10-25 02:30:00.000+01:00``), so files are unambiguous once written.

Aware datetimes returned here always use a fixed-offset ``timezone``, never the ``ZoneInfo``
itself: Python compares two datetimes with the same tzinfo object by wall time and ignores
``fold``, which would make both 02:30s equal.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Callable
from zoneinfo import ZoneInfo

TS_RE = r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:?\d\d)?"
_TS = re.compile(TS_RE)


def local_zone(name: str | None = None) -> tzinfo:
    """The zone ebusd's naive timestamps are in: ``name``, else $TZ, else /etc/localtime."""
    name = name or os.environ.get("TZ", "").lstrip(":") or None
    if name:
        return ZoneInfo(name)
    try:
        with open("/etc/localtime", "rb") as fh:
            return ZoneInfo.from_file(fh, key="localtime")
    except OSError:
        return datetime.now().astimezone().tzinfo  # fixed offset; no DST knowledge


def fixed(dt: datetime) -> datetime:
    """Same instant, tzinfo replaced by its fixed offset."""
    return dt.astimezone(timezone(dt.utcoffset()))


def candidates(naive: datetime, tz: tzinfo) -> list[datetime]:
    """The instants a naive local time can mean: one normally, two in the repeated hour."""
    a = fixed(naive.replace(tzinfo=tz, fold=0))
    b = fixed(naive.replace(tzinfo=tz, fold=1))
    return [a] if a == b else sorted([a, b])


def format_ts(dt: datetime) -> str:
    """``YYYY-MM-DD HH:MM:SS.mmm`` plus ``+HH:MM`` when the datetime is aware."""
    s = dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    off = dt.utcoffset()
    if off is None:
        return s
    sign = "-" if off < timedelta(0) else "+"
    mins = abs(int(off.total_seconds())) // 60
    return f"{s}{sign}{mins // 60:02d}:{mins % 60:02d}"


def parse_ts(s: str) -> datetime:
    """Parse a capture timestamp; aware (fixed offset) if it carries one, naive otherwise."""
    s = s.strip().replace("T", " ")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return fixed(dt) if dt.tzinfo else dt


def find_ts(text: str) -> datetime | None:
    m = _TS.search(text)
    if not m:
        return None
    try:
        return parse_ts(m.group(0))
    except ValueError:
        return None


class LocalClock:
    """Turns a stream of naive local timestamps into aware ones.

    In the repeated hour the first candidate is taken unless it lies more than ``SLACK`` before
    the previous timestamp of the stream, which happens exactly once: when the clock jumps back.
    With no previous timestamp (start, or after ``reset``), ``now`` decides: the latest candidate
    that is not in the future. Without ``now`` the first candidate is taken, which is right for a
    file read from its start.
    """

    SLACK = timedelta(minutes=30)

    def __init__(self, tz: tzinfo | None = None, *, now: Callable[[], datetime] | None = None,
                 last: datetime | None = None):
        self.tz = tz or local_zone()
        self.now = now
        self.last = last

    def reset(self) -> None:
        """The stream restarted (log truncated, follower reconnected): forget the previous time."""
        self.last = None

    def resolve(self, ts: datetime) -> datetime:
        if ts.tzinfo is not None:
            out = fixed(ts)
        else:
            cands = candidates(ts, self.tz)
            if len(cands) == 1:
                out = cands[0]
            elif self.last is not None:
                out = cands[0] if cands[0] >= self.last - self.SLACK else cands[1]
            elif self.now is not None:
                now = self.now()
                past = [c for c in cands if c <= now]
                out = past[-1] if past else cands[0]
            else:
                out = cands[0]
        self.last = out
        return out


def wall_now() -> datetime:
    return fixed(datetime.now().astimezone())


def day_of(dt: datetime) -> str:
    """Local calendar date of a timestamp, used for daily file names."""
    return dt.strftime("%Y-%m-%d")

