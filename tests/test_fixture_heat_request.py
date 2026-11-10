"""Regression on a 4-minute slice of the 2026-10-07 capture: the 20:14:53 setpoint change that started the compressor."""

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ebusnimbus.census import run
from ebusnimbus.payload import decode_readings
from ebusnimbus.rawlog import iter_files
from ebusnimbus.registers import RegisterDB
from ebusnimbus.telegram import Telegram

FIX = Path(__file__).parent / "fixtures"
LOG = FIX / "heat_request_20261007.log"
SIDE = FIX / "heat_request_20261007.side.csv"


def _changes(device, reg):
    """(time, value) each time the register's value changes."""
    out, last = [], None
    for t in iter_files([LOG]):
        if not isinstance(t, Telegram) or t.status != "ok":
            continue
        for r in decode_readings(t):
            if (r.device, r.reg) == (device, reg) and r.how != "request" and r.value != last:
                out.append((t.ts, r.value))
                last = r.value
    return out


def test_slice_parses_cleanly():
    c = run([LOG], RegisterDB.load())
    assert c.total == 1260 and c.status["ok"] == 1210
    assert c.ebusd_tx == 0 and c.bad_lines == 0 and not c.gaps
    assert not [k for k in c.unparsed if k[0] == "payload"]


def test_heat_request_sequence():
    setpoint = _changes("sensys", "6271")
    assert 230 in [v for _, v in setpoint]
    t_set = next(ts for ts, v in setpoint if v == 230)
    assert t_set == datetime(2026, 10, 7, 20, 14, 53, 969000, tzinfo=timezone(timedelta(hours=2)))

    status = [v for ts, v in _changes("energymgr", "c94b") if ts >= t_set]
    assert status[:2] == [22, 2]
    d09c = [v for ts, v in _changes("heatpump", "d09c") if ts >= t_set]
    assert d09c[:2] == [2, 10]
    first_run = next(ts for ts, v in _changes("heatpump", "cb46") if v and v > 0)
    assert 60 < (first_run - t_set).total_seconds() < 90


def test_census_states_in_slice():
    c = run([LOG], RegisterDB.load())
    assert c.state_columns()[:3] == ["idle", "heating", "c94b_22"]


def test_hp_power_follows_meter():
    meter = [(datetime.fromisoformat(r["ts"]), float(r["value"]))
             for r in csv.DictReader(open(SIDE)) if r["metric"] == "total_act_power"]
    assert len(meter) == 48
    hp = _changes("heatpump", "7c4f")
    on = next(ts for ts, v in hp if v > 0)
    before = [p for ts, p in meter if ts < on and (on - ts).total_seconds() > 60]
    after = [p for ts, p in meter if (ts - on).total_seconds() > 30]
    assert max(before) < 300 < 1000 < min(after)
