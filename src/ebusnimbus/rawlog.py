"""Capture file format: reading, writing and normalising ebusd raw-log lines.

Capture line:  ``2026-10-08 23:29:17.477+02:00 <7f182001 02 dc28 3b ...`` (hex without spaces).
Lines starting with ``#`` are markers written by the capture service (start, gap, rotate).
Captures written before 2026-10-08 have no UTC offset; readers resolve those as local time
(see ``localtime``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Iterable, Iterator

from .localtime import TS_RE, LocalClock, find_ts, format_ts, parse_ts
from .telegram import Telegram, parse_telegram


# optional ebusd/HA prefix such as "[bus notice] " between timestamp and payload
_LINE = re.compile(
    rf"^(?P<ts>{TS_RE})\s+(?:\[[^\]]*\]\s*)?"
    r"(?P<head>\.\.\.)?(?P<body>(?:[<>][0-9a-fA-F]+)+)(?P<tail>\.\.\.)?\s*$"
)
_SEG = re.compile(r"([<>])([0-9a-fA-F]+)")


@dataclass
class CaptureLine:
    ts: datetime  # as written in the line: naive (ebusd, old captures) or aware
    hex: str  # all segments concatenated, lower case
    body: str  # segments with their direction markers, e.g. "<7f18..." or "<10>31<..."
    tx: bool  # contains a '>' segment (ebusd transmitted)
    head_fragment: bool
    tail_fragment: bool


def normalise_line(line: str) -> CaptureLine | None:
    """Parse a raw ebusd log line (message mode) or a capture line. None if it is not one."""
    m = _LINE.match(line.strip())
    if not m:
        return None
    try:
        ts = parse_ts(m.group("ts"))
    except ValueError:
        return None
    segs = _SEG.findall(m.group("body"))
    return CaptureLine(
        ts=ts,
        hex="".join(h for _, h in segs).lower(),
        body=m.group("body").lower(),
        tx=any(d == ">" for d, _ in segs),
        head_fragment=bool(m.group("head")),
        tail_fragment=bool(m.group("tail")),
    )


def format_line(cl: CaptureLine) -> str:
    """Capture line; direction markers and '...' cut markers are kept so nothing is lost silently."""
    head = "..." if cl.head_fragment else ""
    tail = "..." if cl.tail_fragment else ""
    return f"{format_ts(cl.ts)} {head}{cl.body}{tail}"


@dataclass
class Marker:
    ts: datetime | None
    text: str


@dataclass
class BadLine:
    line_no: int
    text: str


def iter_events(lines: Iterable[str], clock: LocalClock | None = None,
                tz: tzinfo | None = None) -> Iterator[Telegram | Marker | BadLine]:
    """Yield a Telegram for every capture line, Markers for '#' lines, BadLine for the rest.

    Timestamps come out aware. Lines without an offset are resolved in order by ``clock``
    (a new one for ``tz``, default the local zone), so a repeated hour in old captures is
    placed correctly as long as the lines are read in time order.
    """
    clock = clock or LocalClock(tz)
    for n, line in enumerate(lines, 1):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            ts = find_ts(s)
            yield Marker(None if ts is None else clock.resolve(ts), s.lstrip("# ").strip())
            continue
        cl = normalise_line(s)
        if cl is None:
            yield BadLine(n, s)
            continue
        yield parse_telegram(
            cl.hex, clock.resolve(cl.ts), head_fragment=cl.head_fragment,
            tail_fragment=cl.tail_fragment, ebusd_tx=cl.tx,
        )


def iter_files(paths: Iterable[Path], tz: tzinfo | None = None) -> Iterator[Telegram | Marker | BadLine]:
    clock = LocalClock(tz)
    for p in paths:
        with open(p, encoding="utf-8", errors="replace") as fh:
            yield from iter_events(fh, clock)
