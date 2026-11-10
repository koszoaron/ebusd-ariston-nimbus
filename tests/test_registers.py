from ebusnimbus.registers import RegisterDB, STATUSES


def test_database_loads_and_is_consistent():
    db = RegisterDB.load()
    assert len(db.entries) > 300
    for e in db.entries:
        assert e["status"] in STATUSES
        assert e.get("evidence") or e["status"] == "unknown"
        if e["status"] == "confirmed":
            assert e["evidence"], e


def test_flow_is_confirmed_on_the_heat_pump():
    db = RegisterDB.load()
    e = db.get("heatpump", "6e13")
    assert e["status"] == "confirmed" and e["scale"] == 0.1 and e["unit"] == "l/min"


def test_registers_are_per_device():
    db = RegisterDB.load()
    devs = {d for (d, i) in db.by_key if i == "d140"}
    assert {"energymgr", "gateway", "mfk"} <= devs


def test_no_serial_numbers_in_database():
    text = open("registers/registers.yaml", encoding="utf-8").read()
    assert "092221" not in text
