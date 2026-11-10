"""What a writable ebusd sends for each approved MQTT set (README, "Before you enable reads and writes").

A private ebusd (MQTT-enabled, without --readonly, with the shipped config/ and the deployed
options --scanconfig=off) runs against a fake bus that echoes and records every byte it writes
(ebusnimbus.fakebus), and a local MQTT broker that delivers HA-style ``.../set`` requests to it.
Checks, byte for byte: each approved value gives exactly the telegram the Light Gateway or the
Sensys sent for the same action (2026-10-14), except the source address (31); values outside a
list are refused without sending; nothing else is sent (no scan, no polls).

Needs an ebusd built with MQTT (EBUSD_MQTT=/path/to/ebusd); skipped otherwise.
"""

import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from ebusnimbus import fakebus, mqttsink, replay
from ebusnimbus.telegram import parse_telegram

EBUSD = os.environ.get("EBUSD_MQTT")
CFG = replay.ROOT / "config"
pytestmark = pytest.mark.skipif(not EBUSD, reason="no MQTT-enabled ebusd (set EBUSD_MQTT)")

# (topic name, payload, telegram without QQ/CRC, as captured from 7f/70 on 2026-10-14)
APPROVED = [
    ("z1_mode_set", "scheduled", "fe202003c67903"),
    ("z1_mode_set", "manual", "fe202003c67902"),
    ("z1_mode_set", "off", "fe202003c67900"),
    ("z1_override_active_set", "off", "fe2020030990" "00"),
    ("z1_override_active_set", "on", "fe2020030990" "01"),
    ("z1_comfort_temp_set", "22.5", "fe2020046271e100"),
    ("z1_comfort_temp_set", "22.0", "fe2020046271dc00"),
    ("z1_comfort_temp_set", "10.0", "fe20200462716400"),
    ("z1_comfort_temp_set", "30.0", "fe20200462712c01"),
    ("z1_override_temp_set", "22.0", "fe2020046997dc00"),
    ("z1_override_end_set", "22.0;15;10;14;10;26", "fe207309dc000f0a0e0a1a0101"),
    ("z1_override_clear", "", "fe207309" "0000" "0000000000" "00" "01"),  # 7f's cancel, 16:02:57
]
# all fields constant: ebusd ignores the payload and sends the one telegram (checked here, so
# ebus-mqttacl never probes this topic with a payload, unlike the value-list writes)
CONST_ONLY = [("z1_override_clear", "acl_probe"), ("z1_override_clear", "1")]
REFUSED = [
    ("z1_mode_set", "1"),  # manual night: not approved
    ("z1_mode_set", "manual night"),
    ("z1_comfort_temp_set", "30.5"),
    ("z1_comfort_temp_set", "9.5"),
    ("z1_comfort_temp_set", "22.3"),
    ("z1_comfort_temp_set", "-5"),
    ("z1_override_temp_set", "35"),
    ("z1_override_end_set", "22.0;60;10;14;10;26"),  # minute 60
    ("z1_override_end_set", "22.0;15;24;14;10;26"),  # hour 24
    ("z1_override_active_set", "2"),
] + [(n, "acl_probe") for n in ("z1_mode_set", "z1_override_active_set", "z1_comfort_temp_set",
                               "z1_override_temp_set", "z1_override_end_set")]  # ebus-mqttacl's probe value


@pytest.fixture(scope="module")
def ebusd():
    bus, sink = fakebus.FakeBus(), mqttsink.MqttSink()
    with tempfile.TemporaryDirectory() as tmp:
        log = open(Path(tmp) / "ebusd.log", "wb")
        proc = subprocess.Popen([
            EBUSD, "-f", "--nodevicecheck", f"--device=127.0.0.1:{bus.port}", f"--configpath={CFG}",
            "--scanconfig=off", f"--port={replay._free_port()}", "--localhost", "--log=all:info",
            "--mqtthost=127.0.0.1", f"--mqttport={sink.port}", "--mqttjson", f"--mqttint={CFG}/mqtt-hassio.cfg",
            "--mqtttopic=ebusd"], stdout=log, stderr=subprocess.STDOUT,
            env={k: v for k, v in os.environ.items() if not k.startswith("EBUSD_")})  # ebusd reads EBUSD_* as options
        try:
            deadline = time.monotonic() + 30
            while not sink.subscribed and time.monotonic() < deadline:
                time.sleep(0.2)
            assert sink.subscribed, "ebusd did not subscribe"
            time.sleep(3)  # let startup settle: anything ebusd sends now is unwanted and is checked
            startup = bus.telegrams()

            def send(name, payload, wait=2.0):
                before = len(bus.telegrams())
                sink.publish(f"ebusd/energymgr/{name}/set", payload)
                time.sleep(wait)
                return bus.telegrams()[before:]

            yield send, startup, bus
        finally:
            proc.terminate()
            proc.wait(10)
            log.close()
            print((Path(tmp) / "ebusd.log").read_text(errors="replace"))
            bus.close()
            sink.close()


def test_nothing_sent_at_startup(ebusd):
    _, startup, _ = ebusd
    assert startup == []


@pytest.mark.parametrize("name,payload,expect", APPROVED)
def test_approved_value_sends_the_captured_telegram(ebusd, name, payload, expect):
    send, _, _ = ebusd
    sent = send(name, payload)
    assert len(sent) == 1, sent
    tg = parse_telegram(sent[0])
    assert tg.status == "ok" and tg.crc_ok, (sent, tg.note)
    assert tg.src == 0x31
    assert sent[0][2:].startswith(expect) and len(bytes.fromhex(sent[0])) == 1 + len(expect) // 2 + 1, sent


@pytest.mark.parametrize("name,payload", REFUSED)
def test_value_outside_the_list_is_not_sent(ebusd, name, payload):
    send, _, _ = ebusd
    assert send(name, payload, wait=1.5) == []


def test_unknown_message_and_get_send_nothing(ebusd):
    send, _, bus = ebusd
    assert send("z1_mode", "manual", wait=1.5) == []  # passive message: no write line
    assert send("no_such_message", "1", wait=1.5) == []


@pytest.mark.parametrize("name,payload", CONST_ONLY)
def test_constant_only_write_ignores_the_payload(ebusd, name, payload):
    """Documents ebusd's behaviour: any payload sends the fixed telegram (so no probing with it)."""
    send, _, _ = ebusd
    sent = send(name, payload)
    assert [t[2:] for t in sent] and sent[0][2:].startswith("fe207309" "0000" "0000000000" "00" "01") and len(sent) == 1, sent
