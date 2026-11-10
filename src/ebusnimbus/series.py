"""Value history of registers from capture files, for decoding work (passive analysis only).

``ebus-series captures/*.log --reg energymgr:c94b --reg heatpump:cb46`` prints one CSV row per
value change (``--all`` for every reading): ts, device, id, how, value. Without ``--reg`` every
register is included.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Iterable, Iterator

from .localtime import format_ts, local_zone
from .payload import PayloadError, decode_readings
from .rawlog import iter_files
from .telegram import Telegram


def readings(paths: Iterable[Path], tz=None) -> Iterator[tuple]:
    """(ts, device, id, how, value) for every register value in the captures (no requests)."""
    for ev in iter_files(paths, tz):
        if not isinstance(ev, Telegram) or ev.status != "ok":
            continue
        try:
            rs = decode_readings(ev)
        except PayloadError:
            continue
        for r in rs:
            if r.how != "request":
                yield ev.ts, r.device, r.reg, r.how, r.value


def changes(rows: Iterable[tuple]) -> Iterator[tuple]:
    """Only the rows where a register's value differs from its previous one (any `how`)."""
    last: dict[tuple[str, str], object] = {}
    for row in rows:
        key = (row[1], row[2])
        if key not in last or last[key] != row[4]:
            last[key] = row[4]
            yield row


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("captures", nargs="+", type=Path)
    ap.add_argument("--reg", action="append", default=[], help="device:id, repeatable (id alone matches any device)")
    ap.add_argument("--all", action="store_true", help="every reading, not only changes")
    ap.add_argument("--tz", help="zone of lines without UTC offset; default local")
    a = ap.parse_args(argv)
    want = [r.split(":", 1) if ":" in r else (None, r) for r in a.reg]
    paths = sorted(p for p in a.captures if not p.name.endswith(".side.csv"))
    rows = readings(paths, local_zone(a.tz))
    if want:
        rows = (r for r in rows if any((d is None or d == r[1]) and i == r[2] for d, i in want))
    if not a.all:
        rows = changes(rows)
    w = csv.writer(sys.stdout)
    w.writerow(["ts", "device", "id", "how", "value"])
    for ts, dev, rid, how, val in rows:
        w.writerow([format_ts(ts), dev, rid, how, "" if val is None else val])


if __name__ == "__main__":
    main()
