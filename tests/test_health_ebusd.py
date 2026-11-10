"""What HA sees when ebusd cannot send (M4 step 2, the alert in ha/ebus_health.yaml).

A private ebusd (MQTT-enabled, without --readonly, with the shipped config/ and --scanconfig=off)
runs against a fake bus that takes what ebusd writes but never echoes it, as the adapter did after
the 2026-10-20 power outage. Each HA-style ``.../get`` of the polled heat pump messages must then
fail without any publish on the message's state topic: ha/ebus_health.yaml counts read rounds that
get no reply, so a failed read must not look like a reply. (A successful read publishes the state
topic even when the values did not change: seen live, 2026-11-10.)

Needs an ebusd built with MQTT (EBUSD_MQTT=/path/to/ebusd); skipped otherwise.
"""

import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from ebusnimbus import fakebus, mqttsink, replay

EBUSD = os.environ.get("EBUSD_MQTT")
CFG = replay.ROOT / "config"
POLLED = ["hp_poll_pressures", "hp_poll_compressor", "hp_poll_valve_fans", "hp_poll_heater_protection"]
pytestmark = pytest.mark.skipif(not EBUSD, reason="no MQTT-enabled ebusd (set EBUSD_MQTT)")


@pytest.fixture(scope="module")
def failed_gets():
    bus, sink = fakebus.FakeBus(echo=False), mqttsink.MqttSink()
    with tempfile.TemporaryDirectory() as tmp:
        logpath = Path(tmp) / "ebusd.log"
        log = open(logpath, "wb")
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
            time.sleep(3)
            for name in POLLED:  # one round, 5 s apart as in ha/polled_reads.yaml
                sink.publish(f"ebusd/heatpump/{name}/get")
                time.sleep(5)
            time.sleep(3)
            topics = dict(sink.messages)
        finally:
            proc.terminate()
            proc.wait(10)
            log.close()
            sink.close()
            bus.close()
        return topics, logpath.read_text(errors="replace"), bus.written


def test_ebusd_tried_to_send(failed_gets):
    _, _, written = failed_gets
    assert any(b != fakebus.SYN for b in written), "ebusd wrote nothing, so the test proves nothing"


def test_each_read_failed_in_the_log(failed_gets):
    _, log, _ = failed_gets
    for name in POLLED:
        assert f"read heatpump {name}: ERR" in log, name


def test_no_state_publish_for_a_failed_read(failed_gets):
    topics, _, _ = failed_gets
    assert not [t for t in topics if t in {f"ebusd/heatpump/{n}" for n in POLLED}]
