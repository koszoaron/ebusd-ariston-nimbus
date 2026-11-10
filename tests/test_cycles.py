"""Cycle report: run detection and stop classification."""

from datetime import datetime, timedelta, timezone

from ebusnimbus import cycles

T0 = datetime(2026, 10, 7, 20, 0, tzinfo=timezone(timedelta(hours=2)))


def series(**points):
    s = {k: cycles.Series() for k in cycles.KEYS.values()}
    for key, pts in points.items():
        for sec, v in pts:
            s[key].add(T0 + timedelta(seconds=sec), v)
    return s


def test_overshoot_stop_is_recognised():
    s = series(freq=[(0, 0), (60, 20), (600, 0)], hp_state=[(0, 3), (596, 11)], req_state=[(0, 10)],
               request=[(0, 1)])
    r = cycles.runs(s)
    assert [(x.reason, (x.stop - x.start).seconds) for x in r] == [("overshoot", 540)]


def test_charge_end_and_request_end():
    s = series(freq=[(0, 20), (300, 0), (600, 20), (900, 0)], hp_state=[(0, 3)],
               req_state=[(0, 10), (298, 2), (590, 10), (898, 2)], request=[(0, 1), (899, 0)])
    assert [x.reason for x in cycles.runs(s)] == ["charge end", "request end"]
    assert cycles.runs(s)[1].pause_before == timedelta(seconds=300)
