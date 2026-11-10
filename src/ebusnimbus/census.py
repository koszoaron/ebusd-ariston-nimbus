"""Census: what is on the bus, regenerated from capture files (passive analysis only)."""

from __future__ import annotations

import argparse
import csv
import statistics
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

from .localtime import format_ts, local_zone
from .payload import ERROR_CODES, KNOWN_PBSB, PayloadError, decode_error, decode_readings
from .rawlog import BadLine, Marker, iter_files
from .registers import DEFAULT_PATH, RegisterDB
from .telegram import Telegram

STATE_REG = ("energymgr", "c94b")
STATE_NAMES = {0: "idle", 1: "pump_only", 2: "heating"}  # hypotheses (docs/protocol.md); others: c94b_<n>
NO_STATE = "before_state"  # telegrams before the first c94b value
GAP_SECONDS = 30


def _marker_kind(text: str) -> str:
    words = [w for w in text.split() if not w[0].isdigit()]
    return words[0] if words else "marker"


class RegStats:
    def __init__(self):
        self.hows: Counter[str] = Counter()
        self.count = 0
        self.na = 0  # 2000 replies saying "not available" (mask bit clear)
        self.first = self.last = None
        self.deltas: Counter[float] = Counter()
        self._prev: dict[str, object] = {}
        self.ranges: dict[str, list[int]] = {}  # state -> [min, max]

    def add(self, how, ts, value, state):
        self.hows[how] += 1
        self.count += 1
        if how == "read_na":
            self.na += 1
        self.first = self.first or ts
        self.last = ts
        prev = self._prev.get(how)
        if prev is not None:
            self.deltas[round((ts - prev).total_seconds(), 1)] += 1
        self._prev[how] = ts
        if value is not None and value != 0x7FFF:
            r = self.ranges.setdefault(state, [value, value])
            r[0] = min(r[0], value)
            r[1] = max(r[1], value)

    def period(self) -> float | None:
        if not self.deltas:
            return None
        vals = sorted(self.deltas.elements())
        return round(statistics.median(vals), 1)


class Census:
    def __init__(self, db: RegisterDB, gap_seconds: float = GAP_SECONDS):
        self.db = db
        self.gap = timedelta(seconds=gap_seconds)
        self.regs: dict[tuple[str, str], RegStats] = defaultdict(RegStats)
        self.status: Counter[str] = Counter()
        self.pbsb: Counter[str] = Counter()
        self.unparsed: Counter[tuple[str, str]] = Counter()
        self.unparsed_sample: dict[tuple[str, str], str] = {}
        self.markers: Counter[str] = Counter()
        self.bad_lines = 0
        self.ebusd_tx = 0
        self.total = 0
        self.first = self.last = None
        self.covered = timedelta(0)
        self.gaps: list[tuple[object, object]] = []
        self.state = NO_STATE
        self.states: Counter[str] = Counter()
        # broadcast requests (2000/2001 to fe) name a register without saying who owns it
        self.requests: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
        # frame layouts carrying register values: (src, dst, pbsb, ids) -> count
        self.layouts: Counter[tuple[str, str, str, str]] = Counter()
        # 2004 error broadcasts: (device, payload hex) -> [count, first, last, minutes past the hour]
        self.errors: dict[tuple[str, str], list] = {}

    def feed(self, ev):
        if isinstance(ev, Marker):
            self.markers[_marker_kind(ev.text)] += 1
            return
        if isinstance(ev, BadLine):
            self.bad_lines += 1
            return
        tg: Telegram = ev
        self.total += 1
        self.status[tg.status] += 1
        if tg.ebusd_tx:
            self.ebusd_tx += 1
        if tg.ts is not None:
            if self.last is not None:
                d = tg.ts - self.last
                if d > self.gap:
                    self.gaps.append((self.last, tg.ts))
                elif d > timedelta(0):
                    self.covered += d
            self.first = self.first or tg.ts
            self.last = tg.ts
        if tg.status != "ok":
            key = (tg.status, tg.note)
            self.unparsed[key] += 1
            self.unparsed_sample.setdefault(key, tg.raw)
            return
        self.pbsb[tg.pbsb] += 1
        try:
            readings = decode_readings(tg)
            err = decode_error(tg)
        except PayloadError as exc:
            key = ("payload", f"{tg.pbsb}: {exc}")
            self.unparsed[key] += 1
            self.unparsed_sample.setdefault(key, tg.raw)
            return
        valued = [r.reg for r in readings if r.how not in ("request", "read_na")]
        if valued:
            self.layouts[(f"{tg.src:02x}", f"{tg.dst:02x}", tg.pbsb, "+".join(valued))] += 1
        if err is not None:
            e = self.errors.setdefault((err.device, tg.data.hex()), [0, tg.ts, tg.ts, Counter()])
            e[0] += 1
            e[2] = tg.ts
            if tg.ts is not None:
                e[3][tg.ts.minute] += 1
        for r in readings:
            if r.how == "request":
                self.requests[(r.device, r.reg)][tg.pbsb] += 1
                continue
            if (r.device, r.reg) == STATE_REG and r.value is not None:
                self.state = STATE_NAMES.get(r.value, f"c94b_{r.value}")
            self.states[self.state] += 1
            self.regs[(r.device, r.reg)].add(r.how, tg.ts, r.value, self.state)

    # ---- numbers ----
    def _labelled(self, key) -> bool:
        e = self.db.get(*key)
        return bool(e and (e.get("name") or e.get("description") or e.get("upstream")))

    def coverage(self) -> dict[str, int]:
        seen = set(self.regs)
        db = self.db
        return {
            "seen": len(seen),
            "seen_named": sum(1 for k in seen if db.named(*k)),
            "seen_labelled": sum(1 for k in seen if self._labelled(k)),
            "seen_confirmed": sum(1 for k in seen if db.status(*k) == "confirmed"),
            "seen_not_in_db": sum(1 for k in seen if db.get(*k) is None),
            "db_total": len(db.by_key),
            "db_named": sum(1 for e in db.entries if e.get("name")),
            "db_confirmed": sum(1 for e in db.entries if e["status"] == "confirmed"),
            "db_never_seen": sum(1 for k in db.by_key if k not in seen),
        }

    def state_columns(self) -> list[str]:
        known = [n for n in STATE_NAMES.values() if n in self.states]
        other = sorted(s for s in self.states if s not in STATE_NAMES.values() and s != NO_STATE)
        return known + other + ([NO_STATE] if NO_STATE in self.states else [])

    def request_rows(self) -> list[dict]:
        owners = defaultdict(set)
        for dev, rid in self.regs:
            owners[rid].add(dev)
        return [{"requester": dev, "id": rid, "pbsb": "+".join(sorted(c)), "count": sum(c.values()),
                 "answered_by": "+".join(sorted(owners.get(rid, ()))) or "nobody"}
                for (dev, rid), c in sorted(self.requests.items())]

    def rows(self) -> list[dict]:
        out = []
        for (dev, rid), st in sorted(self.regs.items()):
            e = self.db.get(dev, rid) or {}
            row = {
                "device": dev, "id": rid, "name": e.get("name") or "", "status": e.get("status", "not_in_db"),
                "how": "+".join(sorted(st.hows)), "count": st.count, "not_available": st.na,
                "period_s": "" if st.period() is None else st.period(),
            }
            for s in self.state_columns():
                r = st.ranges.get(s)
                row[f"range_{s}"] = "" if r is None else (str(r[0]) if r[0] == r[1] else f"{r[0]}..{r[1]}")
            out.append(row)
        return out


FIELDS = ["device", "id", "name", "status", "how", "count", "not_available", "period_s"]


def write_csv(c: Census, path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, FIELDS + [f"range_{s}" for s in c.state_columns()])
        w.writeheader()
        w.writerows(c.rows())


def _t(ts) -> str:
    """Timestamp to the second, with its UTC offset (the repeated autumn hour stays distinct)."""
    s = format_ts(ts)
    return s[:19] + s[23:]


OBSERVED_FIELDS = ["device", "id", "how", "count", "not_available", "min", "max", "first", "last"]


def write_observed(c: Census, path: Path) -> None:
    """Which device answers or broadcasts each register, with value range: committed to git as
    registers/observed.csv so the register database can be built without the (private) captures."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(OBSERVED_FIELDS)
        for (dev, rid), st in sorted(c.regs.items()):
            vals = [v for r in st.ranges.values() for v in r]
            w.writerow([dev, rid, "+".join(sorted(st.hows)), st.count, st.na,
                        min(vals) if vals else "", max(vals) if vals else "",
                        _t(st.first)[:10], _t(st.last)[:10]])


def write_layouts(c: Census, path: Path) -> None:
    """Frame layouts with register values (no values themselves), for the config generator."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["src", "dst", "pbsb", "ids", "count"])
        for (src, dst, pbsb, ids), n in sorted(c.layouts.items()):
            w.writerow([src, dst, pbsb, ids, n])


def render_markdown(c: Census, sources: list[str]) -> str:
    cov = c.coverage()
    hours = c.covered.total_seconds() / 3600
    span = "n/a" if c.first is None else f"{_t(c.first)} .. {_t(c.last)}"
    L = ["# eBUS census", "", f"Sources: {', '.join(sources)}", ""]
    L += ["## Capture", "",
          f"- span: {span}", f"- covered: {hours:.2f} h (gaps over {GAP_SECONDS} s are excluded)",
          f"- telegram lines: {c.total}; by status: " + ", ".join(f"{k} {v}" for k, v in sorted(c.status.items())),
          f"- gaps: {len(c.gaps)}; markers: " + (", ".join(f"{k} {v}" for k, v in sorted(c.markers.items())) or "none"),
          f"- lines that are neither telegram nor marker: {c.bad_lines}",
          f"- telegrams with ebusd transmitting (must be 0 for passive capture): **{c.ebusd_tx}**", ""]
    L += ["## Coverage", "",
          "| | count |", "|---|---|",
          f"| registers seen (device, id) | {cov['seen']} |",
          f"| seen and named | {cov['seen_named']} |",
          f"| seen and labelled (name, description or upstream label) | {cov['seen_labelled']} |",
          f"| seen and confirmed | {cov['seen_confirmed']} |",
          f"| seen but not in database | {cov['seen_not_in_db']} |",
          f"| database entries | {cov['db_total']} |",
          f"| database named / confirmed | {cov['db_named']} / {cov['db_confirmed']} |",
          f"| database entries never seen in these captures | {cov['db_never_seen']} |", ""]
    L += ["## PBSB codes", "", "| PBSB | meaning | telegrams |", "|---|---|---|"]
    for code in sorted(set(c.pbsb) | set(KNOWN_PBSB)):
        L.append(f"| {code} | {KNOWN_PBSB.get(code, '**not documented**')} | {c.pbsb.get(code, 0)} |")
    L += ["", "Never seen: " + (", ".join(sorted(k for k in KNOWN_PBSB if k not in c.pbsb)) or "none"), ""]
    L += ["## Unparsed telegrams", ""]
    if not c.unparsed:
        L.append("None.")
    else:
        L += ["| kind | reason | count | sample |", "|---|---|---|---|"]
        for (kind, note), n in c.unparsed.most_common(40):
            L.append(f"| {kind} | {note} | {n} | `{c.unparsed_sample[(kind, note)][:60]}` |")
    L += ["", "## Gaps", ""]
    L += [f"- {_t(a)} .. {_t(b)}" for a, b in c.gaps[:50]] or ["None."]
    reqs = c.request_rows()
    L += ["", "## Broadcast requests", "",
          "Register IDs asked for with `2000`/`2001` to `fe`. They are not counted as registers of the",
          "requester; `answered_by` lists devices seen with a value for the same ID.", ""]
    if reqs:
        L += ["| requester | id | pbsb | n | answered by |", "|---|---|---|---|---|"]
        L += [f"| {q['requester']} | {q['id']} | {q['pbsb']} | {q['count']} | {q['answered_by']} |" for q in reqs]
    else:
        L.append("None.")
    L += ["", "## Error broadcasts (2004)", "",
          "Payload as code, zone status (per ysard) and further bytes. Codes are named only when the",
          "Sensys error history confirms them.", ""]
    if c.errors:
        L += ["| device | payload | code | n | first | last | minute past the hour |", "|---|---|---|---|---|---|---|"]
        for (dev, hx), (n, first, last, mins) in sorted(c.errors.items()):
            m = ", ".join(f":{k:02d}" + (f" ({v})" if v > 1 else "") for k, v in sorted(mins.items()))
            name = ERROR_CODES.get(int(hx[:2], 16), "unconfirmed")
            L.append(f"| {dev} | `{hx}` | {int(hx[:2], 16)} (0x{hx[:2]}): {name} | {n} | {_t(first)} | {_t(last)} | {m} |")
    else:
        L.append("None.")
    na = [r for r in c.rows() if r["not_available"]]
    L += ["", "## Registers read as not available", "",
          "`2000` replies with the register's mask bit clear (mask `00`: nothing available).", ""]
    if na:
        L += ["| device | id | name | not available | of n reads |", "|---|---|---|---|---|"]
        L += [f"| {r['device']} | {r['id']} | {r['name']} | {r['not_available']} | {r['count']} |" for r in na]
    else:
        L.append("None.")
    cols = c.state_columns()
    L += ["", "## Registers", "",
          "Full table in the CSV. Value ranges per operating state, taken from `energymgr c94b` "
          f"({', '.join(f'{k}={v}' for k, v in STATE_NAMES.items())}, hypotheses; other values as `c94b_<n>`).", ""]
    L += ["| device | id | name | status | how | n | n/a | period s | " + " | ".join(cols) + " |",
          "|" + "---|" * (8 + len(cols))]
    for r in c.rows():
        L.append(f"| {r['device']} | {r['id']} | {r['name']} | {r['status']} | {r['how']} | {r['count']} | "
                 f"{r['not_available'] or ''} | "
                 f"{r['period_s']} | " + " | ".join(r[f"range_{s}"] for s in cols) + " |")
    return "\n".join(L) + "\n"


def run(paths: list[Path], db: RegisterDB, tz=None) -> Census:
    """tz: zone of capture lines written without a UTC offset (default: local zone)."""
    c = Census(db)
    for ev in iter_files(paths, tz):
        c.feed(ev)
    return c


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Generate a census report from capture files.")
    ap.add_argument("captures", nargs="+", type=Path, help="capture .log files, in time order")
    ap.add_argument("--registers", type=Path, default=DEFAULT_PATH)
    ap.add_argument("--out", type=Path, default=Path("reports"), help="output directory")
    ap.add_argument("--tz", help="zone of lines without UTC offset (captures before 2026-10-08); default local")
    ap.add_argument("--observed", type=Path, help="also write the observed-registers table (registers/observed.csv)")
    a = ap.parse_args(argv)
    paths = sorted(p for p in a.captures if not p.name.endswith(".side.csv"))
    c = run(paths, RegisterDB.load(a.registers), local_zone(a.tz))
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "census.md").write_text(render_markdown(c, [p.name for p in paths]), encoding="utf-8")
    write_csv(c, a.out / "census.csv")
    if a.observed:
        write_observed(c, a.observed)
        write_layouts(c, a.observed.with_name("layouts.csv"))
    cov = c.coverage()
    print(f"{c.total} telegrams, {cov['seen']} registers seen, {cov['seen_named']} named, "
          f"{cov['seen_confirmed']} confirmed, {len(c.requests)} broadcast-requested IDs; report in {a.out}/")


if __name__ == "__main__":
    main()
