"""Compressor cycle report from capture files (the compressor cycling analysis).

``ebus-cycles captures/*.log`` lists every compressor run: start, stop, run length, pause before it,
frequency, flow/return temperatures and flow, buffer temperatures, electrical energy (52d9, 10 kJ
counts), average power, delivered heat and COP, outdoor and room temperature, and why it stopped:

- ``overshoot``: the heat pump stopped by itself (ca46 = 11 timeguard) while the energy manager kept
  requesting (d09c = 10): the flow overshot the setpoint at minimum output.
- ``charge end``: the energy manager ended the charge (d09c 10 -> 2) with the heat request still on.
- ``request end``: the zone heat request (0191) ended.
- ``other``: none of the above within 30 s of the stop.

Heat: flow (l/min) x (flow - return) x 0.0698 kW, integrated over the run.
COP uses 52d9 (outdoor unit only) and, when the side file is given, the Shelly total.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .localtime import format_ts, local_zone, parse_ts
from .series import readings

KEYS = {
    ("heatpump", "cb46"): "freq", ("heatpump", "7610"): "flow_t", ("heatpump", "7710"): "ret_t",
    ("heatpump", "6e13"): "flow", ("heatpump", "ca46"): "hp_state", ("heatpump", "d09c"): "req_state",
    ("heatpump", "7c4f"): "power", ("energymgr", "759e"): "buf_hi", ("energymgr", "769e"): "buf_lo",
    ("energymgr", "6047"): "setpoint", ("energymgr", "0191"): "request", ("energymgr", "52d9"): "e_lo",
    ("energymgr", "53d9"): "e_hi", ("energymgr", "7647"): "outdoor", ("sensys", "7118"): "room",
    ("energymgr", "c94b"): "em_state", ("heatpump", "c446"): "cap",
}


class Series:
    def __init__(self):
        self.t: list[datetime] = []
        self.v: list[int] = []

    def add(self, ts, val):
        self.t.append(ts)
        self.v.append(val)

    def at(self, ts):
        i = bisect.bisect_right(self.t, ts) - 1
        return self.v[i] if i >= 0 else None

    def between(self, a, b):
        i, j = bisect.bisect_left(self.t, a), bisect.bisect_right(self.t, b)
        return list(zip(self.t[i:j], self.v[i:j]))


@dataclass
class Run:
    start: datetime
    stop: datetime
    pause_before: timedelta | None
    reason: str
    data: dict


def load(paths, tz=None) -> dict[str, Series]:
    s = {k: Series() for k in KEYS.values()}
    for ts, dev, rid, how, val in readings(paths, tz):
        k = KEYS.get((dev, rid))
        if k and val is not None:
            s[k].add(ts, val)
    return s


def runs(s: dict[str, Series]) -> list[Run]:
    out, on, last_stop = [], None, None
    for ts, f in zip(s["freq"].t, s["freq"].v):
        if f and on is None:
            on = ts
        elif not f and on is not None:
            out.append((on, ts))
            on = None
    if on is not None:
        out.append((on, None))
    result = []
    for a, b in out:
        if b is None:
            result.append(Run(a, a, None, "running", {}))
            continue
        result.append(Run(a, b, (a - last_stop) if last_stop else None, reason(s, b), {}))
        last_stop = b
    return result


def reason(s, stop) -> str:
    w0, w1 = stop - timedelta(seconds=30), stop + timedelta(seconds=10)
    if any(v == 11 for _, v in s["hp_state"].between(w0, w1)) and s["req_state"].at(stop) == 10:
        return "overshoot"
    req = s["req_state"].between(w0, w1)
    if any(a == 10 and b == 2 for (_, a), (_, b) in zip(req, req[1:])) or (req and req[-1][1] == 2 and s["req_state"].at(w0) == 10):
        return "request end" if s["request"].at(w1) == 0 else "charge end"
    if s["request"].at(w1) == 0:
        return "request end"
    return "other"


def integrate(points, a, b, f=lambda v: v):
    tot = 0.0
    for (t0, v0), (t1, _) in zip(points, points[1:] + [(b, None)]):
        lo, hi = max(t0, a), min(t1, b)
        if hi > lo:
            tot += f(v0) * (hi - lo).total_seconds()
    return tot


def energy_counts(s, ts):
    lo, hi = s["e_lo"].at(ts), s["e_hi"].at(ts)
    if lo is None or hi is None:
        return None
    return (hi & 0xFFFF) * 65536 + (lo & 0xFFFF)


def describe(s, r: Run, meter=None) -> dict:
    a, b = r.start, r.stop
    secs = (b - a).total_seconds()
    fr = [v for _, v in s["freq"].between(a, b) if v]
    # heat: flow l/min x dT K x 0.0698 kW, sampled on the 25 s temperature reads
    th = []
    for ts, ft in s["flow_t"].between(a, b):
        rt, fl = s["ret_t"].at(ts), s["flow"].at(ts)
        if rt is not None and fl is not None:
            th.append((ts, fl / 10 * (ft - rt) / 10 * 0.0698))
    pre = [(a, th[0][1])] if th else []
    heat_kwh = integrate(pre + th, a, b) / 3600 if th else None
    el_hp = integrate(s["power"].between(a - timedelta(seconds=30), b) or [(a, 0)], a, b) / 3600 / 1000
    d = {
        "start": format_ts(a)[:19], "stop": format_ts(b)[11:19], "run_min": round(secs / 60, 1),
        "pause_before_min": None if r.pause_before is None else round(r.pause_before.total_seconds() / 60, 1),
        "reason": r.reason,
        "freq_min_max": f"{min(fr)}-{max(fr)}" if fr else "",
        "cap": s["cap"].at(a),
        "flow_t_end": _t(s["flow_t"].at(b)), "ret_t_end": _t(s["ret_t"].at(b)),
        "setpoint": _t(s["setpoint"].at(b)),
        "buf_hi_start": _t(s["buf_hi"].at(a)), "buf_hi_end": _t(s["buf_hi"].at(b)),
        "buf_lo_start": _t(s["buf_lo"].at(a)), "buf_lo_end": _t(s["buf_lo"].at(b)),
        "flow_lpm": _t(s["flow"].at(b)),
        "outdoor": _t(s["outdoor"].at(a)), "room": _t(s["room"].at(a)),
        "el_kwh_7c4f": round(el_hp, 3),
        "heat_kwh": None if heat_kwh is None else round(heat_kwh, 3),
        "cop_7c4f": round(heat_kwh / el_hp, 2) if heat_kwh and el_hp > 0.01 else None,
    }
    e0, e1 = energy_counts(s, a - timedelta(hours=2)), energy_counts(s, b + timedelta(hours=2))
    d["note"] = "52d9 is read only every 2 h; use it for daily totals, 7c4f for single runs" if e0 == e1 else ""
    if meter:
        el_m = integrate(meter.between(a, b) or [(a, 0)], a, b) / 3600 / 1000
        d["el_kwh_meter"] = round(el_m, 3)
        d["cop_meter"] = round(heat_kwh / el_m, 2) if heat_kwh and el_m > 0.01 else None
    return d


def _t(v):
    return None if v is None else v / 10


def load_meter(paths) -> Series:
    m = Series()
    for p in paths:
        for row in csv.DictReader(open(p, encoding="utf-8")):
            if row["metric"] == "total_act_power":
                ts = parse_ts(row["ts"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=local_zone()).astimezone()
                try:
                    m.add(ts, float(row["value"]))
                except ValueError:
                    pass
    return m


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("captures", nargs="+", type=Path)
    ap.add_argument("--since", help="only runs starting after this local time, e.g. '2026-10-09 18:00'")
    ap.add_argument("--csv", action="store_true", help="CSV output instead of a table")
    a = ap.parse_args(argv)
    logs = sorted(p for p in a.captures if p.name.endswith(".log"))
    sides = sorted(p for p in a.captures if p.name.endswith(".side.csv"))
    s = load(logs)
    meter = load_meter(sides) if sides else None
    since = parse_ts(a.since).replace(tzinfo=local_zone()) if a.since else None
    rows = [describe(s, r, meter) for r in runs(s) if r.reason != "running" and (since is None or r.start >= since)]
    if not rows:
        print("no completed compressor runs")
        return
    cols = list(rows[0])
    w = csv.writer(sys.stdout) if a.csv else None
    if w:
        w.writerow(cols)
        for r in rows:
            w.writerow([r[c] for c in cols])
        return
    for r in rows:
        print("  ".join(f"{c}={r[c]}" for c in cols if r[c] not in (None, "")))


if __name__ == "__main__":
    main()
