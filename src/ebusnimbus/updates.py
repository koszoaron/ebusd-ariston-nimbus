"""Update rate of the generated config's entities, measured by replaying captures through ebusd.

``ebus-updates captures/2026-10-10.log`` runs the private read-only ebusd of ``ebus-replay`` over
the captures and records every field value ebusd produces: that is the update stream ebusd
publishes over MQTT. For each register it compares how often ebusd updates it with how often the
register's value is on the bus at all (any layout, any PBSB), and writes ``reports/updates.md``.

``--stream FILE`` also writes the update stream as CSV (ts, device, regs, message, field, value),
the input of ``ebus-hasim``.

Nothing here talks to the deployed ebusd or the bus.
"""

from __future__ import annotations

import argparse
import csv
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .localtime import format_ts
from .payload import PayloadError, decode_readings
from .rawlog import iter_files
from .replay import ROOT, find_ebusd, replay
from .telegram import Telegram

# registers the cycling analysis and the COP package depend on
KEY = {
    ("energymgr", "759e"): "buffer high temperature",
    ("heatpump", "7610"): "flow temperature",
    ("heatpump", "7710"): "return temperature",
    ("heatpump", "6e13"): "water flow",
    ("sensys", "7118"): "room temperature",
    ("energymgr", "6197"): "zone 1 flow target",
    ("energymgr", "6047"): "flow setpoint",
    ("energymgr", "649f"): "flow setpoint (same value as 6047)",
    ("heatpump", "cb46"): "compressor frequency",
    ("heatpump", "7c4f"): "electrical power",
    ("energymgr", "7647"): "outdoor temperature",
}


def gaps(ts: list[datetime]) -> list[float]:
    return [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]


def _fmt_s(v: float | None) -> str:
    if v is None:
        return ""
    return f"{v:.0f} s" if v < 600 else f"{v / 60:.0f} min"


def summary(ts: list[datetime]) -> dict:
    g = sorted(gaps(ts))
    return {
        "n": len(ts),
        "median": statistics.median(g) if g else None,
        "p95": g[int(0.95 * (len(g) - 1))] if g else None,
        "max": g[-1] if g else None,
    }


def collect(paths: list[Path], ebusd: str, configpath: Path):
    """(update stream rows, bus appearances per (device, reg), replay report)."""
    tgs = [e for e in iter_files(paths) if isinstance(e, Telegram) and e.status == "ok"]
    seen: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    for tg in tgs:
        try:
            rs = decode_readings(tg)
        except PayloadError:
            continue
        for r in rs:
            if r.value is not None and r.how != "request":
                seen[(r.device, r.reg)].append(tg.ts)
    stream: list[tuple] = []
    rep = replay(paths, ebusd, configpath, telegrams=tgs,
                 sink=lambda ts, msg, fld, dev, regs, val: stream.append((ts, dev, regs, msg, fld, val)))
    return stream, seen, rep


def render(stream, seen, sources: list[str]) -> str:
    upd: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    via: dict[tuple[str, str], set[str]] = defaultdict(set)
    for ts, dev, regs, msg, fld, _ in stream:
        for r in regs:
            upd[(dev, r)].append(ts)
            via[(dev, r)].add(f"{msg}.{fld}")
    for v in upd.values():
        v.sort()
    span = ""
    if stream:
        span = f"{format_ts(stream[0][0])[:19]} to {format_ts(stream[-1][0])[:19]}"

    def row(k, label=""):
        u, s = summary(upd[k]), summary(seen.get(k, []))
        ratio = s["n"] / u["n"] if u["n"] else float("inf")
        return (f"| {k[0]} {k[1]} | {label} | {', '.join(sorted(via[k]))} | {u['n']} | {_fmt_s(u['median'])} | "
                f"{_fmt_s(u['p95'])} | {_fmt_s(u['max'])} | {s['n']} | {_fmt_s(s['median'])} | "
                f"{_fmt_s(s['max'])} | {ratio:.1f} |"), ratio

    head = ["| register | meaning | ebusd field(s) | updates | median gap | p95 gap | max gap | on the bus | "
            "median gap | max gap | bus / updates |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    L = ["# Entity update rate", "", f"Sources: {', '.join(sources)} ({span})", "",
         "Updates: field values ebusd produced from the replayed captures (what it publishes over MQTT).",
         "On the bus: telegrams carrying the register's value in any layout. A ratio well above 1 means",
         "most of the register's appearances are in layouts the config does not decode.", "",
         "## Key registers", ""] + head
    for k, label in KEY.items():
        L.append(row(k, label)[0] if k in upd else f"| {k[0]} {k[1]} | {label} | not decoded | 0 | | | | "
                 f"{len(seen.get(k, []))} | | | |")
    L += ["", "## All decoded registers, by ratio", ""] + head
    rows = sorted((row(k) for k in upd), key=lambda x: -x[1])
    L += [r for r, _ in rows]
    return "\n".join(L) + "\n"


def write_stream(stream, path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "device", "regs", "message", "field", "value"])
        for ts, dev, regs, msg, fld, val in stream:
            w.writerow([format_ts(ts), dev, "+".join(regs), msg, fld, val])


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("captures", nargs="+", type=Path)
    ap.add_argument("--config", type=Path, default=ROOT / "config", help="ebusd config directory")
    ap.add_argument("--ebusd", help="ebusd binary (default: $EBUSD or ebusd on PATH)")
    ap.add_argument("--out", type=Path, default=Path("reports") / "updates.md")
    ap.add_argument("--stream", type=Path, help="also write the update stream as CSV")
    a = ap.parse_args(argv)
    ebusd = find_ebusd(a.ebusd)
    if not ebusd:
        raise SystemExit("ebusd binary not found (use --ebusd or $EBUSD)")
    paths = sorted(p for p in a.captures if p.name.endswith(".log"))
    stream, seen, rep = collect(paths, ebusd, a.config)
    if rep.failed:
        print("warning: the replay check fails on these captures; see ebus-replay")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(render(stream, seen, [p.name for p in paths]), encoding="utf-8")
    if a.stream:
        write_stream(stream, a.stream)
    print(f"{len(stream)} field updates from {rep.decoded} decoded telegrams; report in {a.out}")


if __name__ == "__main__":
    main()
