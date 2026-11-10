"""HA sensor semantics used to check ha/cop.yaml offline."""

from datetime import datetime, timedelta, timezone

from ebusnimbus import hasim

T0 = datetime(2026, 10, 10, 18, 0, tzinfo=timezone(timedelta(hours=2)))


def at(s):
    return T0 + timedelta(seconds=s)


def test_statistics_characteristics():
    samples = ((0, 1.0), (300, 2.0), (330, 3.0), (600, 2.0))
    # mean: (1+2+3+2)/4; average_step: (1*300 + 2*30 + 3*270) / 600; change: last - first
    for c, want in (("mean", 2.0), ("average_step", 1.95), ("change", 1.0)):
        st = hasim.Statistics(c, 100)
        for t, x in samples:
            st.add(at(t), x)
        assert abs(st.value(at(600)) - want) < 1e-9, c


def test_average_step_window_starts_at_oldest_kept_sample():
    st = hasim.Statistics("average_step", 100)
    st.add(at(0), 0.0)  # off, constant: no new samples
    st.add(at(700), 4.0)  # compressor on
    st.add(at(900), 4.001)
    # at 900 s the 0 sample is older than 10 min and purged: the pause no longer counts
    assert st.value(at(900)) == 4.0


def test_integration_max_sub_interval_and_rounding():
    seen = []
    it = hasim.Integration(1.0, timedelta(seconds=30), lambda ts, v: seen.append((ts, v)))
    it.source(at(0), 3.6)  # kW
    it.tick(at(95))
    # updates at 30, 60, 90 s: 0.03, 0.06, 0.09 kWh
    assert [(round((t - T0).total_seconds()), v) for t, v in seen] == [(0, 0.0), (30, 0.03), (60, 0.06), (90, 0.09)]


def test_live_cop_after_a_pause():
    """Run, 4 min pause, run (as on 2026-10-10 18:13-18:37). At the end the 10-minute window holds
    the pause; average_step loses it from the heat side, the energy change does not."""
    on = lambda t: t < 300 or t >= 540  # noqa: E731
    ev = [(at(0), "ret_t", 30.0)]
    for t in range(0, 961, 5):  # Shelly every 5 s: 1000 W running, 100 W idle; flow every 5 s; noise
        ev.append((at(t), "power", (1000.0 if on(t) else 100.0) + (t % 10) / 100))
        ev.append((at(t), "flow", 25.0 + (t % 10) / 100))
    for t in range(0, 961, 25):  # heat pump reads every 25 s: 25 l/min x 3 K while running
        ev += [(at(t), "freq", 30.0 if on(t) else 0.0), (at(t), "flow_t", 33.0 + (t % 50) / 1000)]
    ev.sort(key=lambda e: e[0])
    pk = {v.name: hasim.Package(v) for v in hasim.VARIANTS}
    for e in ev:
        for p in pk.values():
            p.event(*e)
    end = at(960)
    ref = pk["energy"]
    exact = hasim.step_integral(ref.heat, end - hasim.WINDOW, end) / \
        (hasim.step_integral(ref.el, end - hasim.WINDOW, end) / 1000)
    assert abs(pk["energy"].live_at(end) - exact) / exact < 0.05
    assert pk["step"].live_at(end) > 1.2 * exact
