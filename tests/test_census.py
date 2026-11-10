from pathlib import Path

from ebusnimbus.census import Census, render_markdown, run, write_csv
from ebusnimbus.registers import RegisterDB

FIXTURE = Path(__file__).parent / "fixtures" / "idle_heat_long1.log"


def test_census_on_fixture(tmp_path):
    db = RegisterDB.load()
    c = run([FIXTURE], db)
    assert c.total == 1953 and c.ebusd_tx == 0 and not c.gaps
    cov = c.coverage()
    assert cov["seen"] > 150 and cov["seen_not_in_db"] == 0
    assert cov["seen_confirmed"] <= cov["seen_labelled"] <= cov["seen"]
    md = render_markdown(c, [FIXTURE.name])
    assert "ebusd transmitting" in md and "Never seen" in md
    write_csv(c, tmp_path / "c.csv")
    assert (tmp_path / "c.csv").read_text().startswith("device,id,name")


def test_state_ranges_and_period():
    c = run([FIXTURE], RegisterDB.load())
    st = c.regs[("energymgr", "c94b")]
    assert st.count > 10
    assert st.period() is not None


def test_gap_and_ebusd_tx_are_reported(tmp_path):
    f = tmp_path / "x.log"
    f.write_text(
        "2026-01-01 00:00:00.000 <131e20200c0b45001745001a45001ef3014c00000000\n"
        "2026-01-01 00:05:00.000 <131e20200c0b45001745001a45001ef3014c00000000\n"
        "2026-01-01 00:05:01.000 <3103200000>00\n"
        "# gap\n"
    )
    c = run([f], RegisterDB.load())
    assert len(c.gaps) == 1
    assert c.ebusd_tx == 1
    assert c.markers["gap"] == 1


def test_broadcast_requests_are_not_registers_of_the_requester(tmp_path):
    from ebusbuild import broadcast
    f = tmp_path / "r.log"
    f.write_text(
        f"2026-01-01 00:00:00.000 <{broadcast(0x7f, '2001', bytes.fromhex('6471'))}\n"
        f"2026-01-01 00:00:01.000 <{broadcast(0x13, '200e', bytes.fromhex('6471460000809600'))}\n"
        f"2026-01-01 00:00:02.000 <{broadcast(0x13, '2000', bytes.fromhex('0119'))}\n"
    )
    c = run([f], RegisterDB.load())
    assert ("gateway", "6471") not in c.regs and ("energymgr", "0119") not in c.regs
    assert ("energymgr", "6471") in c.regs
    req = {(q["requester"], q["id"]): q["answered_by"] for q in c.request_rows()}
    assert req == {("gateway", "6471"): "energymgr", ("energymgr", "0119"): "nobody"}
    assert "## Broadcast requests" in render_markdown(c, [f.name])


def test_unlisted_state_values_get_their_own_column(tmp_path):
    from ebusbuild import broadcast
    f = tmp_path / "s.log"
    f.write_text(
        f"2026-01-01 00:00:00.000 <{broadcast(0x13, '2010', bytes.fromhex('c94b1600'))}\n"
        f"2026-01-01 00:00:05.000 <{broadcast(0x13, '2010', bytes.fromhex('c94b0200'))}\n"
    )
    c = run([f], RegisterDB.load())
    assert c.state_columns() == ["heating", "c94b_22"]
    write_csv(c, tmp_path / "s.csv")
    assert "range_c94b_22" in (tmp_path / "s.csv").read_text().splitlines()[0]


def test_not_available_reads_are_shown(tmp_path):
    f = tmp_path / "x.log"
    f.write_text("2026-10-07 10:00:00.000+02:00 <701820000442da43da78000100" "9b00\n")
    c = run([f], RegisterDB.load())
    assert not c.unparsed
    assert c.regs[("energymgr", "42da")].na == 1
    md = render_markdown(c, ["x.log"])
    assert "| energymgr | 42da |" in md.split("## Registers read as not available")[1].split("## Registers\n")[0]


def test_error_broadcasts_are_listed(tmp_path):
    from ebusbuild import broadcast
    wire = broadcast(0x13, "2004", bytes.fromhex("690100"))
    f = tmp_path / "x.log"
    f.write_text(f"2026-10-07 10:36:01.000+02:00 <{wire}\n2026-10-07 11:36:01.000+02:00 <{wire}\n")
    c = run([f], RegisterDB.load())
    assert c.errors[("energymgr", "690100")][0] == 2
    md = render_markdown(c, ["x.log"])
    assert "| energymgr | `690100` | 105 (0x69): 411 room sensor not available | 2 |" in md and ":36 (2)" in md
