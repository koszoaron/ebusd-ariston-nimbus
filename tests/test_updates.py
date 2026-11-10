"""Update-rate report: entity updates against bus appearances."""

from datetime import datetime, timedelta, timezone

from ebusnimbus import updates

T0 = datetime(2026, 10, 10, 12, 0, tzinfo=timezone(timedelta(hours=2)))


def test_summary_gaps():
    ts = [T0 + timedelta(seconds=s) for s in (0, 25, 50, 650)]
    s = updates.summary(ts)
    assert s["n"] == 4 and s["median"] == 25 and s["max"] == 600


def test_render_ratio_and_fields():
    # ebusd updated 759e from two messages, every 50 s; the bus carried it every 25 s
    stream = [(T0 + timedelta(seconds=50 * i), "energymgr", ("759e",), "energymgr.buffer_high_temp",
               "buffer_high_temp", "30.0") for i in range(5)]
    seen = {("energymgr", "759e"): [T0 + timedelta(seconds=25 * i) for i in range(9)]}
    md = updates.render(stream, seen, ["x.log"])
    row = next(line for line in md.splitlines() if line.startswith("| energymgr 759e | buffer high"))
    assert "energymgr.buffer_high_temp.buffer_high_temp" in row
    assert row.rstrip().endswith("| 1.8 |")  # 9 on the bus / 5 updates
    assert "| heatpump 7610 | flow temperature | not decoded |" in md
