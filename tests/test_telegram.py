import pytest

from ebusbuild import broadcast, master_master, master_part, master_slave
from ebusnimbus.telegram import (EscapeError, crc8, crc_of, escape, is_master,
                                 parse_telegram, unescape)

# The worked example from docs/protocol.md, Framing (a 2001 read of register dc28).
EXAMPLE = "7f182001" "02dc28" "3b" "00" "03050005" "a900" "00"


def test_unescape_and_escape():
    assert unescape(bytes.fromhex("a900")) == b"\xa9"
    assert unescape(bytes.fromhex("a901")) == b"\xaa"
    assert unescape(bytes.fromhex("01a90002")) == b"\x01\xa9\x02"
    assert escape(b"\xa9\xaa\x01") == bytes.fromhex("a900a90101")
    for bad in ("a9", "a902"):
        with pytest.raises(EscapeError):
            unescape(bytes.fromhex(bad))


def test_crc_known_values():
    assert crc8(bytes.fromhex("7f18200102dc28")) == 0x3B
    assert crc8(bytes.fromhex("03050005")) == 0xA9  # slave CRC of the brief's example reply


def test_crc_over_expanded_form():
    logical = bytes.fromhex("1018200002a9")
    assert crc_of(logical) == crc8(escape(logical))
    assert crc_of(logical) != crc8(logical)


def test_example_from_brief_parses():
    tg = parse_telegram(EXAMPLE)
    assert tg.status == "ok", tg.note
    assert (tg.src, tg.dst, tg.pbsb) == (0x7F, 0x18, "2001")
    assert tg.data == bytes.fromhex("dc28")
    assert tg.reply == bytes.fromhex("050005")
    assert tg.reply_crc_ok and tg.crc_ok
    assert tg.reply_ack == 0 and tg.ack == 0


def test_master_addresses():
    for a in (0x00, 0x03, 0x10, 0x13, 0x31, 0x70, 0x7F, 0xF7, 0xFF):
        assert is_master(a), a
    for a in (0x18, 0x1E, 0x36, 0x75, 0x84, 0xFC, 0xFE):
        assert not is_master(a), a


def test_broadcast_layout():
    tg = parse_telegram(broadcast(0x13, "200f", bytes.fromhex("c94b0200")))
    assert tg.status == "ok" and tg.is_broadcast
    assert tg.data == bytes.fromhex("c94b0200")


def test_master_master_layout():
    tg = parse_telegram(master_master(0x70, 0x13, "2071", b"\x01\x02"))
    assert tg.status == "ok" and tg.ack == 0 and tg.reply is None


def test_master_slave_layout_with_escaped_payload():
    wire = master_slave(0x13, 0x1E, "2000", bytes.fromhex("6e13"), bytes([1, 0xA9, 0xAA]))
    assert "a900" in wire and "a901" in wire
    tg = parse_telegram(wire)
    assert tg.status == "ok", tg.note
    assert tg.reply == bytes([1, 0xA9, 0xAA])


def test_write_with_empty_reply():
    tg = parse_telegram(master_slave(0x13, 0x1E, "2020", bytes.fromhex("cd4603"), b""))
    assert tg.status == "ok" and tg.reply == b""


def test_crc_error_recorded_not_dropped():
    wire = broadcast(0x13, "200f", bytes.fromhex("c94b0200"))
    bad = wire[:-2] + ("00" if wire[-2:] != "00" else "01")
    tg = parse_telegram(bad)
    assert tg.status == "crc_error"


def test_slave_crc_error():
    wire = master_slave(0x13, 0x18, "2000", bytes.fromhex("c94b"), bytes([1, 2]))
    # flip the slave CRC (second to last byte)
    bad = wire[:-4] + ("00" if wire[-4:-2] != "00" else "01") + wire[-2:]
    assert parse_telegram(bad).status == "crc_error"


def test_nack():
    wire = escape(master_part(0x13, 0x18, "2000", bytes.fromhex("c94b"))) + b"\xff"
    tg = parse_telegram(wire.hex())
    assert tg.status == "nack" and tg.ack == 0xFF


@pytest.mark.parametrize("raw", ["10", "77", "aa", ""])
def test_noise(raw):
    assert parse_telegram(raw).status == "noise"


def test_truncated():
    wire = broadcast(0x13, "200f", bytes.fromhex("c94b0200"))
    assert parse_telegram(wire[:-6]).status == "truncated"


def test_fragment_flags():
    wire = broadcast(0x13, "200f", bytes.fromhex("c94b0200"))
    assert parse_telegram(wire, tail_fragment=True).status == "fragment"
    assert parse_telegram(wire, head_fragment=True).status == "fragment"


def test_malformed_hex_and_escape():
    assert parse_telegram("zz").status == "malformed"
    assert parse_telegram("7f18a9").status == "malformed"


def test_ebusd_source_flagged():
    tg = parse_telegram(master_slave(0x31, 0x18, "2000", bytes.fromhex("c94b"), bytes([1, 2])))
    assert tg.ebusd_tx
