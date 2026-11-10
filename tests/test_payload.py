import pytest

from ebusbuild import broadcast, master_slave
from ebusnimbus.payload import (PayloadError, decode_clock, decode_error, decode_readings,
                                decode_time_program, decode_value, width_of, width_status)
from ebusnimbus.telegram import parse_telegram


def rd(wire):
    tg = parse_telegram(wire)
    assert tg.status == "ok", tg.note
    return decode_readings(tg)


@pytest.mark.parametrize("reg,w", [
    ("0119", 1), ("1f00", 1), ("c94b", 1), ("df00", 1),
    ("6e13", 2), ("7f00", 2), ("6000", 2),
    ("4000", 2), ("5fff", 2),
    ("8000", None), ("bf00", None), ("e000", None), ("ff00", None),
])
def test_width_rule(reg, w):
    assert width_of(bytes.fromhex(reg)) == w


def test_width_status():
    assert width_status(bytes.fromhex("6e13")) == "verified"
    assert width_status(bytes.fromhex("4c00")) == "untested"
    assert width_status(bytes.fromhex("9000")) == "unknown"


def test_decode_value_signed_le():
    assert decode_value(b"\xff") == 255
    assert decode_value(bytes.fromhex("ffff")) == -1
    assert decode_value(bytes.fromhex("e803")) == 1000
    assert decode_value(bytes.fromhex("ff7f")) == 0x7FFF


def test_2000_read_two_registers():
    wire = master_slave(0x13, 0x1E, "2000", bytes.fromhex("6e13c94b"),
                        bytes([0b11]) + bytes.fromhex("5000") + b"\x02")
    r = rd(wire)
    assert [(x.device, x.reg, x.how, x.value) for x in r] == [
        ("heatpump", "6e13", "read", 80), ("heatpump", "c94b", "read", 2)]


def test_2000_bad_mask_is_error():
    wire = master_slave(0x13, 0x1E, "2000", bytes.fromhex("c94bc200"), bytes([0b01, 2, 3]))
    with pytest.raises(PayloadError):
        decode_readings(parse_telegram(wire))


def test_2000_mask_00_is_not_available():
    # as seen every 2 h: the Sensys reads 42da/43da from the energy manager
    r = rd("701820000442da43da78" "00" "01009b" "00")
    assert [(x.device, x.reg, x.how, x.value) for x in r] == [
        ("energymgr", "42da", "read_na", None), ("energymgr", "43da", "read_na", None)]


def test_2000_mask_00_with_data_is_error():
    wire = master_slave(0x13, 0x1E, "2000", bytes.fromhex("c94bc200"), bytes([0, 2]))
    with pytest.raises(PayloadError, match="mask 00 followed by 1"):
        decode_readings(parse_telegram(wire))


def test_2000_partial_mask_stays_unparsed_but_names_the_fit():
    wire = master_slave(0x13, 0x1E, "2000", bytes.fromhex("c94b6e13c200"),
                        bytes([0b101, 2, 1]))
    with pytest.raises(PayloadError, match=r"partial reply mask 05 of 07 \(fits bitmap hypothesis: c94b\+c200\)"):
        decode_readings(parse_telegram(wire))


def test_2000_partial_mask_that_does_not_fit():
    wire = master_slave(0x13, 0x1E, "2000", bytes.fromhex("c94b6e13"), bytes([0b10, 2]))
    with pytest.raises(PayloadError, match="runs past end"):
        decode_readings(parse_telegram(wire))


def test_2000_broadcast_request():
    r = rd(broadcast(0x7F, "2000", bytes.fromhex("c94b6e13")))
    assert [(x.device, x.reg, x.how) for x in r] == [("gateway", "c94b", "request"),
                                                      ("gateway", "6e13", "request")]


def test_2001_with_limits_matches_brief_example():
    r = rd("7f182001" "02dc28" "3b" "00" "03050005" "a900" "00")
    assert len(r) == 1
    x = r[0]
    assert (x.device, x.reg, x.how, x.value, x.vmin, x.vmax) == ("energymgr", "dc28", "read_limits", 5, 0, 5)


def test_2001_two_byte_limits():
    reply = bytes.fromhex("e803" "0000" "ff7f")
    r = rd(master_slave(0x70, 0x18, "2001", bytes.fromhex("6e13"), reply))
    assert (r[0].value, r[0].vmin, r[0].vmax) == (1000, 0, 0x7FFF)


def test_200e():
    r = rd(broadcast(0x18, "200e", bytes.fromhex("dc28" "05" "00" "05")))
    assert (r[0].device, r[0].reg, r[0].value, r[0].vmin, r[0].vmax) == ("energymgr", "dc28", 5, 0, 5)


def test_200f_mixed_widths_with_flag():
    r = rd(broadcast(0x13, "200f", bytes.fromhex("c94b" "02" "00" "7510" "d600" "00")))
    assert [(x.reg, x.value, x.flag) for x in r] == [("c94b", 2, 0), ("7510", 214, 0)]


def test_2010_negative_temperature():
    r = rd(broadcast(0x13, "2010", bytes.fromhex("7610" "9cff" "00")))
    assert r[0].value == -100


def test_2020_broadcast_vs_write():
    b = rd(broadcast(0x13, "2020", bytes.fromhex("c446" "64" "7110" "e001")))
    assert [(x.device, x.how) for x in b] == [("energymgr", "bc_2020")] * 2
    w = rd(master_slave(0x13, 0x1E, "2020", bytes.fromhex("cd46" "03"), b""))
    assert [(x.device, x.reg, x.how, x.value) for x in w] == [("heatpump", "cd46", "write_2020", 3)]


def test_unknown_width_class_is_payload_error():
    with pytest.raises(PayloadError):
        decode_readings(parse_telegram(broadcast(0x13, "2020", bytes.fromhex("9000" "01"))))


def test_truncated_value_is_payload_error():
    with pytest.raises(PayloadError):
        decode_readings(parse_telegram(broadcast(0x13, "2020", bytes.fromhex("7110" "e0"))))


def test_non_register_pbsb_has_no_readings():
    assert rd(broadcast(0x70, "2070", bytes.fromhex("1e0c0a0a0518" "00"))) == []


def test_2004_error_broadcast():
    tg = parse_telegram(broadcast(0x13, "2004", bytes.fromhex("690100")))
    assert decode_readings(tg) == []
    e = decode_error(tg)
    assert (e.device, e.code, e.zone_status, e.rest) == ("energymgr", 0x69, 1, b"\x00")
    assert decode_error(parse_telegram(broadcast(0x13, "200f", bytes.fromhex("c94b0200")))) is None


def test_2070_sensys_clock():
    c = decode_clock(parse_telegram(broadcast(0x70, "2070", bytes.fromhex("0a09070a031a11"))))
    assert (c.hour, c.minute, c.day, c.month, c.year, c.weekday) == (9, 10, 7, 10, 2026, 3)


def test_2051_half_day_program():
    t = decode_time_program(parse_telegram(broadcast(0x70, "2051", bytes.fromhex("1001" + "00" * 7 + "55" * 5))))
    assert (t.channel, t.day, t.afternoon) == (1, 0, True)
    assert t.levels == [0] * 28 + [1] * 20  # 12:00-19:00 level 0, 19:00-24:00 level 1
