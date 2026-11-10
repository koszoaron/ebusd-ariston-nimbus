"""ebus-mqttacl against a tiny MQTT 5 responder with per-user publish rules."""

import socket
import threading

from ebusnimbus import mqttacl

GETS = [f"ebusd/heatpump/{n}/get" for n in ("hp_poll_pressures", "hp_poll_compressor")]


def _parse_connect(body: bytes) -> str | None:
    """User name from a CONNECT (MQTT 5, no properties), None if anonymous."""
    pos = 2 + int.from_bytes(body[:2], "big")  # protocol name
    flags = body[pos + 1]
    pos += 4 + 1  # version, flags, keepalive, properties length 0
    cid = int.from_bytes(body[pos:pos + 2], "big")
    pos += 2 + cid
    if not flags & 0x80:
        return None
    ul = int.from_bytes(body[pos:pos + 2], "big")
    return body[pos + 2:pos + 2 + ul].decode()


def fake_broker(may_publish, anonymous=True, refuse_by="puback"):
    """``may_publish(user, topic)`` decides; refusals by PUBACK 0x87 or by DISCONNECT."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    published = []

    def serve(conn):
        with conn:
            _, body = mqttacl.read_packet(conn)
            user = _parse_connect(body)
            if user is None and not anonymous:
                conn.sendall(b"\x20\x03\x00\x87\x00")  # not authorized
                return
            conn.sendall(b"\x20\x03\x00\x00\x00")
            while True:
                try:
                    kind, body = mqttacl.read_packet(conn)
                except (ConnectionError, OSError):
                    return
                tl = int.from_bytes(body[:2], "big")
                topic, pid = body[2:2 + tl].decode(), body[2 + tl:4 + tl]
                if may_publish(user, topic):
                    published.append((user, topic))
                    conn.sendall(b"\x40\x03" + pid + b"\x10")  # no matching subscribers
                elif refuse_by == "disconnect":
                    conn.sendall(b"\xe0\x01\x87")
                    return
                else:
                    conn.sendall(b"\x40\x03" + pid + b"\x87")

    def run():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=serve, args=(conn,), daemon=True).start()

    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1], published


def good_acl(user, topic):
    if not topic.startswith("ebusd/"):
        return True
    return user == "ha" and (topic in GETS or topic == mqttacl.LIST_TOPIC)


def test_approved_topics_from_config(tmp_path):
    (tmp_path / "a.csv").write_text(
        "# comment\nr,heatpump,hp_poll_pressures,x\nr1,heatpump,hp_poll_compressor,x\n"
        "u,energymgr,status,x\nw,energymgr,z1_setpoint,x\n*r,ignored\n", encoding="utf-8")
    gets, sets = mqttacl.approved_topics(tmp_path)
    assert gets == GETS
    assert sets == ["ebusd/energymgr/z1_setpoint/set"]


def test_approved_topics_of_the_shipped_config():
    gets, sets = mqttacl.approved_topics()
    assert len(gets) == 4 and all(t.startswith("ebusd/heatpump/hp_poll_") for t in gets)
    assert sets == [f"ebusd/energymgr/{n}/set" for n in  # owner-approved writes (M3, registers/writes.yaml)
                    ("z1_mode_set", "z1_override_active_set", "z1_comfort_temp_set", "z1_override_temp_set",
                     "z1_override_end_set", "z1_override_clear")]
    assert mqttacl.constant_only_sets() == ["ebusd/energymgr/z1_override_clear/set"]  # never probed


def test_probes_never_touch_a_real_write_or_poll_priority():
    sets = ["ebusd/heatpump/hp_poll_pressures/set"]  # pretend the first read also had a write line
    for other in (True, False):
        for p in mqttacl.refusal_probes(GETS, sets, other_user=other):
            if p.topic in sets:  # a real write only with a value no write field accepts
                assert other and p.payload == mqttacl.BAD_VALUE
            assert "?" not in p.topic or mqttacl.NO_SUCH in p.topic
            assert (p.topic.endswith(("/list", "/set")) or mqttacl.NO_SUCH in p.topic
                    or p.topic == mqttacl.PROBE_TOPIC)
    assert all(p.topic != mqttacl.PROBE_TOPIC for p in mqttacl.refusal_probes(GETS, [], other_user=False))
    # the set probe of a read-only message uses one without a write line
    assert "ebusd/heatpump/hp_poll_compressor/set" in [p.topic for p in mqttacl.refusal_probes(GETS, sets, other_user=False)]


def test_ha_set_topics_published_only_with_the_invalid_value():
    sets = ["ebusd/energymgr/z1_mode_set/set"]
    seen = []
    port, published = fake_broker(lambda u, t: not t.startswith("ebusd/") or (u == "ha" and t in sets))
    ok, lines = mqttacl.check_user("127.0.0.1", port, "ha", "pw", label="ha", allowed=[], allowed_sets=sets,
                                   refused=mqttacl.refusal_probes([], sets, other_user=False))
    assert ok and any("approved write" in ln and "accepted" in ln for ln in lines)
    ok, lines = mqttacl.check_user("127.0.0.1", port, "test", "pw", label="test", allowed=[],
                                   refused=mqttacl.refusal_probes([], sets, other_user=True))
    assert ok and any(sets[0] in ln and "refused" in ln for ln in lines)


def _check(port, user, **kw):
    other = user != "ha"
    kw.setdefault("allow_list", not other)
    return mqttacl.check_user("127.0.0.1", port, user, "pw", label=user or "anon",
                              allowed=GETS if not other else [],
                              refused=mqttacl.refusal_probes(GETS, [], other_user=other), **kw)


def test_good_acl_passes_for_all_users():
    port, published = fake_broker(good_acl)
    assert _check(port, "test")[0]
    assert _check(port, "ha")[0]
    assert _check(port, None, anonymous=True)[0]
    # without --with-reads nothing but ebusd/list (cache only, no bus traffic) is sent
    assert [t for _, t in published if t.startswith("ebusd/")] == [mqttacl.LIST_TOPIC]


def test_with_reads_publishes_the_approved_gets():
    port, published = fake_broker(good_acl)
    ok, lines = _check(port, "ha", with_reads=True, read_pause=0)
    assert ok
    assert [t for u, t in published if t.startswith("ebusd/") and t != mqttacl.LIST_TOPIC] == GETS


def test_refused_by_disconnect():
    port, _ = fake_broker(good_acl, refuse_by="disconnect")
    assert _check(port, "test")[0]
    assert _check(port, "ha")[0]


def test_open_broker_fails():
    port, _ = fake_broker(lambda u, t: True)
    ok, lines = _check(port, "test")
    assert not ok and any("ACCEPTED" in ln for ln in lines)


def test_ha_wildcard_rule_fails():
    port, _ = fake_broker(lambda u, t: not t.startswith("ebusd/") or (u == "ha" and t.startswith("ebusd/heatpump/")))
    ok, lines = _check(port, "ha")
    assert not ok
    assert any(mqttacl.NO_SUCH in ln and "ACCEPTED" in ln for ln in lines)


def test_ha_refused_approved_read_fails():
    port, _ = fake_broker(lambda u, t: not t.startswith("ebusd/"))
    assert _check(port, "ha", allow_list=False)[0]  # nothing approved is published without --with-reads
    ok, lines = _check(port, "ha", allow_list=False, with_reads=True, read_pause=0)
    assert not ok and any("REFUSED, must be accepted" in ln for ln in lines)


def test_anonymous_login_refused_passes():
    port, _ = fake_broker(lambda u, t: True, anonymous=False)
    ok, lines = _check(port, None, anonymous=True)
    assert ok and "refused" in lines[0]


def test_anonymous_allowed_but_open_fails():
    port, _ = fake_broker(lambda u, t: True)
    assert not _check(port, None, anonymous=True)[0]


def test_list_allowed_for_ha_refused_for_others():
    assert any(p.topic == mqttacl.LIST_TOPIC for p in mqttacl.refusal_probes(GETS, [], other_user=True))
    assert all(p.topic != mqttacl.LIST_TOPIC for p in mqttacl.refusal_probes(GETS, [], other_user=False))
    port, published = fake_broker(lambda u, t: not t.startswith("ebusd/") or (u == "ha" and t in GETS))
    ok, lines = _check(port, "ha")  # broker refuses ebusd/list for HA: must fail
    assert not ok and any("ebusd/list" in ln and "REFUSED" in ln for ln in lines)
