"""Offline check of the HA COP package (ha/cop.yaml) against ``ebus-cycles``.

``ebus-hasim captures/*.log captures/*.side.csv`` replays the captures through the private ebusd
(as ``ebus-updates``; or reads ``--stream FILE`` written by it), turns ebusd's updates and the
Shelly side file into HA state changes and evaluates the package's sensors the way HA does:

- an MQTT sensor changes state only when its value changes (no ``force_update``);
- a template sensor re-renders on every state change of an entity it references;
- ``statistics`` keeps at most ``sampling_size`` samples no older than ``max_age``. ``mean`` is the
  plain sample average; ``average_step`` weights each sample by the time to the next one, over the
  span of the samples still in the buffer (so the window starts at the oldest kept sample, not at
  now - max_age); ``change`` is the last minus the first sample;
- ``integration`` (``method: left``) adds the previous state times the elapsed time on each source
  change and, with ``max_sub_interval``, at least that often while the source is constant. Its state
  is rounded to ``round`` decimals and changes only when that rounded value does.

Variants of the live COP (the energy sensors and the daily COP do not depend on it):

- ``draft``: the package as of M1 (``mean`` of the power sensors, ``sampling_size`` 200, heat
  clamped at 0 also for the energy);
- ``step``: ``average_step`` of the power sensors, large buffer, signed heat;
- ``energy``: the 10-minute ``change`` of the two energy integrals (``max_sub_interval`` 30 s),
  signed heat. This is what ha/cop.yaml uses since M2.

Per compressor run it prints the COP of ebus-cycles (Shelly), the COP from the package's energy
sensors over the run, and each variant's live COP at the end of the run next to the exact COP of
the 10 minutes before it.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import heapq
import tempfile
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import cycles
from .localtime import local_zone, parse_ts

FIELDS = {("heatpump", "flow"): "flow", ("heatpump", "hp_flow_temperature"): "flow_t",
          ("heatpump", "hp_return_temperature"): "ret_t", ("heatpump", "compressor_frequency"): "freq"}
WINDOW = timedelta(minutes=10)


@dataclass
class Variant:
    name: str
    live: str  # "mean" | "average_step" (of the power sensors) | "change" (of the energy integrals)
    sampling_size: int
    clamp_energy: bool  # heat clamped at 0 for the energy and the live COP
    keep_last_sample: bool = False
    max_sub_interval: timedelta | None = None


VARIANTS = [
    Variant("draft", "mean", 200, True),
    Variant("step", "average_step", 10000, False, keep_last_sample=True),
    Variant("energy", "change", 10000, False, max_sub_interval=timedelta(seconds=30)),
]


class Statistics:
    def __init__(self, characteristic: str, size: int, keep_last: bool = False, max_age=WINDOW):
        self.c, self.keep_last, self.max_age = characteristic, keep_last, max_age
        self.buf: deque = deque(maxlen=size)

    def add(self, ts, x):
        self.buf.append((ts, x))

    def value(self, now):
        while self.buf and now - self.buf[0][0] > self.max_age:
            if self.keep_last and len(self.buf) == 1:
                break
            self.buf.popleft()
        b = self.buf
        if not b:
            return None
        if self.c == "mean":
            return sum(x for _, x in b) / len(b)
        if self.c == "change":
            return b[-1][1] - b[0][1]
        if len(b) == 1:
            return b[0][1]
        area = sum(p[1] * (q[0] - p[0]).total_seconds() for p, q in zip(b, list(b)[1:]))
        span = (b[-1][0] - b[0][0]).total_seconds()
        return area / span if span > 0 else b[-1][1]


class Integration:
    """HA integration sensor (left Riemann sum, kWh), emitting its state changes to ``sink``."""

    def __init__(self, scale: float, max_sub: timedelta | None, sink, digits: int = 3):
        self.scale, self.max_sub, self.sink, self.digits = scale, max_sub, sink, digits
        self.total, self.src, self.t, self.state = 0.0, None, None, None

    def _advance(self, ts):
        if self.src is not None:
            self.total += self.src * self.scale * (ts - self.t).total_seconds() / 3600
        self.t = ts
        s = round(self.total, self.digits)
        if s != self.state:
            self.state = s
            self.sink(ts, s)

    def tick(self, now):
        """max_sub_interval updates due before ``now``."""
        if self.max_sub is None or self.t is None:
            return
        while now - self.t >= self.max_sub:
            self._advance(self.t + self.max_sub)

    def source(self, ts, x):
        self.tick(ts)
        self._advance(ts)
        self.src = x


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=local_zone()).astimezone()  # lines before the DST fix


def load_stream(path: Path):
    for r in csv.DictReader(open(path, encoding="utf-8")):
        k = FIELDS.get((r["device"], r["field"]))
        if k:
            try:
                yield _aware(parse_ts(r["ts"])), k, float(r["value"])
            except ValueError:
                pass  # "-": ebusd's replacement value, not a number


def load_meter(paths):
    for p in paths:
        for r in csv.DictReader(open(p, encoding="utf-8")):
            if r["metric"] == "total_act_power":
                try:
                    yield _aware(parse_ts(r["ts"])), "power", float(r["value"])
                except ValueError:
                    pass


def heat_kw(st):
    f, ft, rt, fr = (st.get(k) for k in ("flow", "flow_t", "ret_t", "freq"))
    if None in (f, ft, rt):
        return None
    return f * (ft - rt) * 0.0698 if fr else 0.0


class Package:
    """The package's sensors for one variant, fed with source updates in time order."""

    def __init__(self, v: Variant):
        self.v = v
        self.st: dict[str, float] = {}
        self.heat: list[tuple[datetime, float]] = []  # state changes of the heat sensor (kW)
        self.el: list[tuple[datetime, float]] = []  # state changes of the Shelly sensor (W)
        if v.live == "change":
            self.h_stat = Statistics("change", v.sampling_size)
            self.e_stat = Statistics("change", v.sampling_size)
        else:
            self.h_stat = Statistics(v.live, v.sampling_size, v.keep_last_sample)
            self.e_stat = Statistics(v.live, v.sampling_size, v.keep_last_sample)
        self.h_int = Integration(1.0, v.max_sub_interval, self._h_energy)
        self.e_int = Integration(0.001, v.max_sub_interval, self._e_energy)
        self.live: list[tuple[datetime, float | None]] = []

    def _h_energy(self, ts, kwh):
        if self.v.live == "change":
            self.h_stat.add(ts, kwh)

    def _e_energy(self, ts, kwh):
        if self.v.live == "change":
            self.e_stat.add(ts, kwh)

    def event(self, ts, k, x):
        self.h_int.tick(ts)
        self.e_int.tick(ts)
        if self.st.get(k) == x:
            return  # same value: no state change in HA
        self.st[k] = x
        if k == "power":
            self.el.append((ts, x))
            self.e_int.source(ts, x)
            if self.v.live != "change":
                self.e_stat.add(ts, x)
        else:
            h = heat_kw(self.st)
            if h is None:
                return
            h = round(max(h, 0.0) if self.v.clamp_energy else h, 3)
            if self.heat and self.heat[-1][1] == h:
                return
            self.heat.append((ts, h))
            self.h_int.source(ts, h)
            if self.v.live != "change":
                self.h_stat.add(ts, h)
        if self.st.get("freq"):
            hv, ev = self.h_stat.value(ts), self.e_stat.value(ts)
            if self.v.live == "change":  # energies over the window; 200 W average as the floor
                ok = hv is not None and ev and ev > 0.2 * WINDOW.total_seconds() / 3600
            else:
                ok = hv is not None and ev and ev > 200
                ev = ev / 1000 if ev else ev
            self.live.append((ts, hv / ev if ok else None))

    def live_at(self, ts):
        i = bisect.bisect_right(self.live, (ts, float("inf"))) - 1
        return self.live[i][1] if i >= 0 else None


def step_integral(series: list[tuple[datetime, float]], a, b) -> float:
    """Integral of a step function given by its changes over [a, b], value * s."""
    tot = 0.0
    for (t0, v0), (t1, _) in zip(series, series[1:] + [(b, None)]):
        lo, hi = max(t0, a), min(t1, b)
        if hi > lo:
            tot += v0 * (hi - lo).total_seconds()
    return tot


def report(logs, sides, stream) -> list[dict]:
    meter = list(load_meter(sides))
    pk = [Package(v) for v in VARIANTS]
    for ts, k, x in heapq.merge(sorted(stream), sorted(meter)):
        for p in pk:
            p.event(ts, k, x)
    s = cycles.load(logs)
    m = cycles.load_meter(sides)
    ref = pk[-1]  # signed heat, exact step functions
    rows = []
    for r in cycles.runs(s):
        if r.reason == "running":
            continue
        a, b = r.start, r.stop
        d = cycles.describe(s, r, m)
        row = {"start": d["start"], "run_min": d["run_min"], "cop_cycles": d.get("cop_meter")}
        for p in (pk[0], pk[-1]):  # clamped and signed heat
            h, e = step_integral(p.heat, a, b), step_integral(p.el, a, b) / 1000
            row[f"cop_energy_{'clamped' if p.v.clamp_energy else 'signed'}"] = round(h / e, 2) if e > 0 else None
        for p in pk:
            x = p.live_at(b - timedelta(seconds=1))
            row[f"live_{p.v.name}"] = None if x is None else round(x, 2)
        w0 = b - WINDOW  # the live sensor's window, including any time before the start
        h, e = step_integral(ref.heat, w0, b), step_integral(ref.el, w0, b) / 1000
        row["exact_10min"] = round(h / e, 2) if e > 0 else None
        row["neg_heat_kwh"] = round(-step_integral([(t, min(v, 0.0)) for t, v in ref.heat], a, b) / 3600, 3)
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("captures", nargs="+", type=Path)
    ap.add_argument("--stream", type=Path, help="update stream from ebus-updates --stream")
    ap.add_argument("--ebusd", help="ebusd binary, when no --stream is given")
    a = ap.parse_args(argv)
    logs = sorted(p for p in a.captures if p.name.endswith(".log"))
    sides = sorted(p for p in a.captures if p.name.endswith(".side.csv"))
    if a.stream:
        stream = list(load_stream(a.stream))
    else:
        from .replay import ROOT, find_ebusd
        from .updates import collect, write_stream
        ebusd = find_ebusd(a.ebusd)
        if not ebusd:
            raise SystemExit("ebusd binary not found (use --ebusd, $EBUSD or --stream)")
        updates, _, _ = collect(logs, ebusd, ROOT / "config")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stream.csv"
            write_stream(updates, path)
            stream = list(load_stream(path))
    rows = report(logs, sides, stream)
    if not rows:
        print("no completed compressor runs")
        return
    cols = list(rows[0])
    print("| " + " | ".join(cols) + " |")
    print("|" + "---|" * len(cols))
    for r in rows:
        print("| " + " | ".join("" if r[c] is None else str(r[c]) for c in cols) + " |")


if __name__ == "__main__":
    main()
