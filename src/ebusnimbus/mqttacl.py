"""Check that the MQTT broker lets only approved clients publish ebusd requests (M2, extended in M3).

ebusd 26.1 subscribes to ``ebusd/#`` and acts on three request topics (mqtthandler.cpp,
``notifyMqttTopic``), each optionally with ``?N`` appended (N = 1-9 also makes ebusd poll that
message by itself from then on; ``?N`` at the end of a ``get`` payload does the same):

- ``ebusd/<circuit>/<name>/get``: one bus read of an active (``r``) message; a passive message is
  only re-published;
- ``ebusd/<circuit>/<name>/set``: one bus write of a ``w`` message;
- ``.../list``: re-publishes known values, no bus traffic.

It also subscribes to the ``config_restart`` topic (``homeassistant/status``, payload ``online``),
which only makes it re-publish the HA discovery definitions. There is no scan request over MQTT.

``ebus-mqttacl`` logs in with MQTT 5, so the broker reports the outcome of each QoS 1 publish
(PUBACK reason >= 0x80, or a disconnect, means refused). Users, from ``.env`` (never printed):

- ``MQTT_TEST_USER``/``MQTT_TEST_PASS``: a throwaway user standing for every client except ebusd
  and HA; every request topic must be refused;
- ``MQTT_HASS_USER``/``MQTT_HASS_PASS`` (optional): Home Assistant's user; it may publish only the
  approved request topics: ``get`` of the active ``r`` lines and ``set`` of the ``w`` lines in
  ``config/*.csv``. Everything else below ``ebusd/`` must be refused;
- anonymous: the broker must refuse the login, or refuse every request topic.

The HA user may also publish ``ebusd/list`` (ha/ebusd_list_on_start.yaml): ebusd then re-publishes
its cached values (no bus traffic), so HA has them again after its own restart. Per-message
``.../list`` topics stay refused.

Each refusal probe is chosen so that, if the broker wrongly accepted it, nothing reaches the bus:
``list``, ``set`` on a read-only message, or a message name that does not exist. One exception:
the test user's ``get`` of a whitelisted read (that is the case the check is for), which would cost
one approved read. The approved ``set`` topics are published only with the payload ``acl_probe``,
which no value-list field accepts, so ebusd refuses it and sends nothing: the test user must be
refused, the HA user accepted. A write with only constant fields ignores its payload and always
sends, so its topic is never published (listed as such). ``?N`` is never used on a real
message. The approved ``get`` topics are published only with ``--with-reads`` (one bus read per
``get``, 5 s apart as in ha/polled_reads.yaml).
Exit status 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import csv
import os
import secrets
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from .replay import ROOT

CONFIG = ROOT / "config"
PREFIX = "ebusd"
PROBE_TOPIC = "ebusd/heatpump/hp_poll_pressures/get"  # a whitelisted read
LIST_TOPIC = "ebusd/list"  # ebusd re-publishes its cached values, no bus traffic (HA sends it after its start)
NO_SUCH = "acl_probe_no_such_message"  # ebusd logs "message not found" and sends nothing
# payload for the approved set topics: every write field is a value list or a constant
# (tests/test_polled.py), so ebusd refuses this value ("element not found") and sends nothing
BAD_VALUE = b"acl_probe"


def _vbi(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n % 128, n // 128
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _str(s: str) -> bytes:
    b = s.encode()
    return len(b).to_bytes(2, "big") + b


def _read_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed by the broker")
        buf += chunk
    return buf


def read_packet(sock: socket.socket) -> tuple[int, bytes]:
    head = _read_exact(sock, 1)[0]
    mult, n = 1, 0
    while True:
        b = _read_exact(sock, 1)[0]
        n += (b & 0x7F) * mult
        if not b & 0x80:
            break
        mult *= 128
    return head >> 4, _read_exact(sock, n)


def connect(host: str, port: int, user: str | None, password: str | None, timeout: float = 10) -> socket.socket:
    """MQTT 5 login; ``user`` None logs in anonymously. Raises ConnectionError if refused."""
    sock = socket.create_connection((host, port), timeout=timeout)
    flags = 0x02 | (0xC0 if user is not None else 0)  # clean start, user+pass if given
    var = _str("MQTT") + bytes([5, flags]) + (30).to_bytes(2, "big") + b"\x00"  # v5, no properties
    payload = _str(f"ebus-acltest-{secrets.token_hex(3)}")
    if user is not None:
        payload += _str(user) + _str(password or "")
    body = var + payload
    sock.sendall(b"\x10" + _vbi(len(body)) + body)
    try:
        kind, ack = read_packet(sock)
    except ConnectionError:
        sock.close()
        raise
    if kind != 2 or len(ack) < 2 or ack[1] != 0:
        sock.close()
        raise ConnectionError(f"CONNACK refused (reason 0x{ack[1]:02x})" if kind == 2 and len(ack) > 1
                              else "no CONNACK")
    return sock


def publish_qos1(sock: socket.socket, topic: str, payload: bytes = b"", pid: int = 1) -> int:
    """PUBACK reason code (< 0x80 = accepted); raises ConnectionError if the broker disconnects."""
    body = _str(topic) + pid.to_bytes(2, "big") + b"\x00" + payload  # no properties
    sock.sendall(b"\x32" + _vbi(len(body)) + body)
    while True:
        kind, data = read_packet(sock)
        if kind == 4 and data[:2] == pid.to_bytes(2, "big"):
            return data[2] if len(data) > 2 else 0  # reason omitted = success
        if kind == 14:  # DISCONNECT from the broker
            raise ConnectionError(f"disconnected by the broker (reason 0x{data[0]:02x})" if data else "disconnected")


def load_env(path: Path) -> dict[str, str]:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def approved_topics(config_dir: Path = CONFIG) -> tuple[list[str], list[str]]:
    """(get topics, set topics) of the messages ebusd may send: active ``r`` lines (with or without
    poll priority) and ``w`` lines in the config CSVs. Passive (``u``) lines cause no bus traffic."""
    gets, sets = [], []
    for path in sorted(config_dir.glob("*.csv")):
        with path.open(encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) < 3 or row[0].startswith("#"):
                    continue
                kind, circuit, name = row[0].strip(), row[1].strip(), row[2].strip()
                if kind[:1] == "r" and kind[1:] in ("", *"123456789"):
                    gets.append(f"{PREFIX}/{circuit}/{name}/get")
                elif kind[:1] == "w":
                    sets.append(f"{PREFIX}/{circuit}/{name}/set")
    return gets, sets


def constant_only_sets(config_dir: Path = CONFIG) -> list[str]:
    """Set topics of ``w`` lines whose fields are all constants (``=value``). ebusd ignores the
    payload of such a write and always sends its telegram (tests/test_writes_ebusd.py), so the
    invalid-value probe would send it: these topics are never published by this tool."""
    out = []
    for path in sorted(config_dir.glob("*.csv")):
        with path.open(encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) < 3 or row[0].startswith("#") or row[0].strip()[:1] != "w":
                    continue
                values = [row[i + 3] for i in range(8, len(row) - 3, 6) if row[i]]
                if values and all(v.startswith("=") for v in values):
                    out.append(f"{PREFIX}/{row[1].strip()}/{row[2].strip()}/set")
    return out


@dataclass
class Probe:
    topic: str
    why: str
    payload: bytes = b""


def refusal_probes(gets: list[str], sets: list[str], *, other_user: bool) -> list[Probe]:
    """Topics that must be refused. None of them sends anything on the bus if accepted, except the
    test user's whitelisted get (``other_user``), which would cost one approved read."""
    probes = []
    if other_user:
        probes.append(Probe(PROBE_TOPIC, "read request of a whitelisted read"))
        probes += [Probe(t, "approved write, invalid value (ebusd would refuse it)", BAD_VALUE) for t in sets]
    read_only = [t.rsplit("/", 1)[0] for t in gets if t.rsplit("/", 1)[0] + "/set" not in sets]
    if read_only:  # set and list of a read-only message: ebusd finds no write message / no bus traffic
        probes += [Probe(f"{read_only[0]}/set", "write request (read-only message: no write found)"),
                   Probe(f"{read_only[0]}/list", "list request (no bus traffic)")]
    probes += [
        Probe(f"{PREFIX}/heatpump/{NO_SUCH}/get", "read request outside the approved names (wildcard rule?)"),
        Probe(f"{PREFIX}/heatpump/{NO_SUCH}/get?1", "read request with poll priority"),
        Probe(f"{PREFIX}/energymgr/{NO_SUCH}/set", "write request outside the approved names"),
        Probe(f"{PREFIX}/heatpump/{NO_SUCH}", "state topic (spoofed value)"),
    ]
    if other_user:  # the HA user may send it (ha/ebusd_list_on_start.yaml)
        probes.append(Probe(LIST_TOPIC, "list request"))
    return probes


def _publish(host, port, user, password, topics: list[Probe], pause: float = 0.0
             ) -> list[tuple[Probe, bool, str]]:
    """Publish each topic on its own connection (a refusing broker may disconnect). Returns
    (probe, accepted, detail) per topic."""
    out = []
    for i, p in enumerate(topics):
        if i and pause:
            time.sleep(pause)
        sock = connect(host, port, user, password)
        try:
            try:
                rc = publish_qos1(sock, p.topic, p.payload, pid=1)
                out.append((p, rc < 0x80, f"reason 0x{rc:02x}"))
            except ConnectionError as e:
                out.append((p, False, str(e)))
        finally:
            sock.close()
    return out


def check_user(host, port, user, password, *, label: str, allowed: list[str], refused: list[Probe],
               allowed_sets: list[str] = (), allow_list: bool = False, with_reads: bool = False,
               read_pause: float = 5.0,
               anonymous: bool = False
               ) -> tuple[bool, list[str]]:
    lines, ok = [], True
    try:
        sock = connect(host, port, user, password)
    except ConnectionError as e:
        if anonymous:
            return True, [f"[{label}] login: {e} (refused, as it should be)"]
        return False, [f"[{label}] login: {e} (check the credentials in .env)"]
    try:
        rc = publish_qos1(sock, f"acltest/{secrets.token_hex(4)}", b"x", pid=1)
    except ConnectionError as e:
        rc, ok = 0xFF, False
        lines.append(f"[{label}] control publish: {e}")
    finally:
        sock.close()
    if rc < 0x80:  # MQTT 5: codes below 0x80 are successes (0x10 = no matching subscribers)
        lines.append(f"[{label}] control publish to acltest/...: reason 0x{rc:02x} (accepted)")
    else:
        ok = False
        lines.append(f"[{label}] control publish to acltest/...: reason 0x{rc:02x} (REFUSED: login or test broken)")
    for p, accepted, detail in _publish(host, port, user, password, refused):
        ok &= not accepted
        lines.append(f"[{label}] {p.topic}: {detail} ({'ACCEPTED, must be refused' if accepted else 'refused'})"
                     f" - {p.why}")
    if allow_list:  # safe to publish: ebusd answers from its cache, nothing on the bus
        for p, accepted, detail in _publish(host, port, user, password, [Probe(LIST_TOPIC, "list request", b"1")]):
            ok &= accepted
            lines.append(f"[{label}] {p.topic}: {detail} ({'accepted' if accepted else 'REFUSED, must be accepted'})"
                         " - list request (ebusd re-publishes cached values, nothing on the bus)")
    if allowed_sets:  # safe to publish: the invalid value makes ebusd send nothing
        for p, accepted, detail in _publish(host, port, user, password,
                                            [Probe(t, "approved write, invalid value", BAD_VALUE) for t in allowed_sets]):
            ok &= accepted
            lines.append(f"[{label}] {p.topic}: {detail} ({'accepted' if accepted else 'REFUSED, must be accepted'})"
                         " - approved write, invalid value (ebusd refuses it, nothing sent)")
    if allowed and with_reads:
        for p, accepted, detail in _publish(host, port, user, password, [Probe(t, "approved") for t in allowed],
                                            pause=read_pause):
            ok &= accepted
            lines.append(f"[{label}] {p.topic}: {detail} ({'accepted' if accepted else 'REFUSED, must be accepted'})"
                         " - approved read (one bus read)")
    elif allowed:
        lines.append(f"[{label}] approved get topics not published ({len(allowed)}; --with-reads sends them,"
                     " one bus read each)")
    return ok, lines


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", type=Path, default=ROOT / ".env")
    ap.add_argument("--config", type=Path, default=CONFIG, help="ebusd config dir (approved topics)")
    ap.add_argument("--with-reads", action="store_true",
                    help="as the HA user, also publish each approved get topic (one bus read each, 5 s apart)")
    a = ap.parse_args(argv)
    env = {**load_env(a.env), **{k: v for k, v in os.environ.items() if k.startswith("MQTT_")}}
    missing = [k for k in ("MQTT_HOST", "MQTT_TEST_USER", "MQTT_TEST_PASS") if not env.get(k)]
    if missing:
        raise SystemExit(f"missing in {a.env}: {', '.join(missing)}")
    host, port = env["MQTT_HOST"], int(env.get("MQTT_PORT", "1883"))
    gets, sets = approved_topics(a.config)
    const = constant_only_sets(a.config)
    print(f"approved request topics (config): {len(gets)} get, {len(sets)} set")
    for t in gets + sets:
        print(f"  {t}" + ("  (constant-only write: any publish sends it, so never probed)" if t in const else ""))
    sets = [t for t in sets if t not in const]  # probed with the invalid value

    results = [check_user(host, port, env["MQTT_TEST_USER"], env["MQTT_TEST_PASS"], label="test user",
                          allowed=[], refused=refusal_probes(gets, sets, other_user=True))]
    results.append(check_user(host, port, None, None, label="anonymous", allowed=[], anonymous=True,
                              refused=refusal_probes(gets, sets, other_user=True)))
    if env.get("MQTT_HASS_USER"):
        results.append(check_user(host, port, env["MQTT_HASS_USER"], env.get("MQTT_HASS_PASS", ""), label="HA user",
                                  allowed=gets, allowed_sets=sets, allow_list=True,
                                  refused=refusal_probes(gets, sets, other_user=False),
                                  with_reads=a.with_reads))
    else:
        results.append((False, ["[HA user] MQTT_HASS_USER not set in .env: HA user not checked"]))
    for _, lines in results:
        print("\n".join(lines))
    ok = all(r for r, _ in results)
    print("PASS: only ebusd may publish requests; HA only the approved ones" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
