"""What HA would receive: a private MQTT-enabled ebusd replays a fixture into a local sink.

Needs an ebusd built with MQTT (EBUSD_MQTT=/path/to/ebusd); skipped otherwise.
"""

import json
import os
from pathlib import Path

import pytest

from ebusnimbus import mqttsink, replay
from ebusnimbus.rawlog import iter_files
from ebusnimbus.telegram import Telegram

EBUSD = os.environ.get("EBUSD_MQTT")
FIX = Path(__file__).parent / "fixtures" / "heat_request_20261007.log"
CFG = replay.ROOT / "config"


@pytest.fixture(scope="module")
def discovery():
    tgs = [e for e in iter_files([FIX]) if isinstance(e, Telegram) and e.status == "ok"]
    sink = mqttsink.MqttSink()
    try:
        replay.run_ebusd(replay.wire_stream(tgs), EBUSD, CFG, chunk=512, pause=0.05, settle=15, extra_args=[
            "--mqtthost=127.0.0.1", f"--mqttport={sink.port}", "--mqttjson",
            f"--mqttint={CFG}/mqtt-hassio.cfg", "--mqtttopic=ebusd"])
    finally:
        sink.close()
    return {t: json.loads(p) for t, p in sink.messages.items()
            if t.startswith("homeassistant/") and t.endswith("/config") and p
            and json.loads(p).get("device", {}).get("name", "").startswith("ebusd ")}


pytestmark = pytest.mark.skipif(not EBUSD, reason="no MQTT-enabled ebusd (set EBUSD_MQTT)")


def test_entities_are_published(discovery):
    assert len(discovery) > 20


def test_nothing_is_settable(discovery):
    assert not [d["name"] for d in discovery.values() if "command_topic" in d]


def test_names_are_labels(discovery):
    assert all(d["name"] and "_" not in d["name"] for d in discovery.values()), \
        [d["name"] for d in discovery.values() if not d["name"] or "_" in d["name"]]


def test_classes_and_categories(discovery):
    by = {d["name"]: d for d in discovery.values()}
    assert by["Flow temperature"]["device_class"] == "temperature"
    assert by["Compressor frequency"]["device_class"] == "frequency"
    assert by["Water flow"]["unit_of_measurement"] == "L/min"
    assert "entity_category" not in by["Flow temperature"]
    # counters are read every 2 h, so they are in the fixture only by chance
    for name in ("Compressor running hours", "Compressor starts", "Heating running hours"):
        if name in by:
            assert by[name]["entity_category"] == "diagnostic"
    assert all(t.split("/")[1] == "binary_sensor" for t, d in discovery.items() if d["name"] == "Zone 1 heat request")


def test_m2_review_entities(discovery):
    by = {d["name"]: d for d in discovery.values()}
    # 769e stays (Sensys menu 20.1.0; the owner knows it follows the heat pump return)
    assert "Buffer low temperature" in by
    # c446: % at the owner's request, although the unit is unconfirmed
    assert by["Compressor frequency limit"]["unit_of_measurement"] == "%"
    # second-layout copies are diagnostic temperatures (only those whose layout is in the fixture)
    alts = [d for n, d in by.items() if "(alt. layout" in n]
    assert alts
    for d in alts:
        assert d["entity_category"] == "diagnostic" and d["device_class"] == "temperature", d
