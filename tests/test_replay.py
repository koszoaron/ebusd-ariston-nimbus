"""Replay check: a private read-only ebusd decodes a fixture; its values must equal the parser's."""

from pathlib import Path

import pytest

from ebusnimbus import replay

FIX = Path(__file__).parent / "fixtures" / "heat_request_20261007.log"
EBUSD = replay.find_ebusd()


def test_parse_log_pairs_commands_with_decodes():
    lines = [
        "2026-10-08 11:45:33.196 [update info] received MS cmd: 131e2000020c45 / 020100",
        "2026-10-08 11:45:33.197 [update notice] received read heatpump heatpump_flow_switch QQ=13: open",
        "2026-10-08 11:45:33.201 [update info] received BC cmd: 00fe203a011e",
        "2026-10-08 11:45:33.201 [update notice] received unknown BC cmd: 00fe203a011e",
        "2026-10-08 11:45:33.201 [update info] received BC cmd: f7fe20100c1191",
        "2026-10-08 11:45:33.201 [update notice] received update-read mfk relays: on;off;off",
    ]
    d = replay.parse_log(lines)
    assert [(x.master, x.slave, x.unknown, x.name, x.values) for x in d] == [
        ("131e2000020c45", "020100", False, "heatpump_flow_switch", ["open"]),
        ("00fe203a011e", None, True, "", []),
        ("f7fe20100c1191", None, False, "relays", ["on", "off", "off"]),
    ]


def test_field_positions_skip_id_bytes_and_share_bit_bytes():
    msg = {"id": [0x20, 0x00, 0x78, 0x10], "fielddefs": [
        {"name": "", "slave": True, "type": "IGN", "length": 1, "isignored": True},
        {"name": "t", "slave": True, "type": "SIN", "length": 2, "divisor": 10},
        {"name": "a", "slave": True, "type": "BI0", "length": 1, "isbits": True},
        {"name": "b", "slave": True, "type": "BI1", "length": 1, "isbits": True},
        {"name": "c", "slave": True, "type": "UCH", "length": 1},
    ]}
    assert [(f.name, f.part, f.offset) for f in replay._fields(msg)] == [
        ("", "s", 0), ("t", "s", 1), ("a", "s", 3), ("b", "s", 3), ("c", "s", 4)]


@pytest.mark.skipif(not EBUSD, reason="ebusd binary not available (set EBUSD)")
def test_fixture_replay_has_no_value_mismatch():
    rep = replay.replay([FIX], EBUSD, replay.ROOT / "config")
    assert rep.telegrams == 1210 and rep.not_seen == 0
    assert rep.fields_ok > 300
    assert not rep.mismatch_n, dict(rep.mismatch_n)
