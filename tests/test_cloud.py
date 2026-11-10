"""The cloud wrapper must never let a write through, and must log every call."""

import pytest

pytest.importorskip("ariston")

import requests  # noqa: E402

from ebusnimbus import cloud  # noqa: E402

GW = "GWTEST123"


class Resp:
    ok, status_code = True, 200

    def __init__(self, body):
        import json
        self.content = json.dumps(body).encode()
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake(method, path, params=None, json=None, headers=None, timeout=None):
        calls.append((method, path))
        return Resp({"token": "t"} if path.endswith("login") else {"items": []})

    monkeypatch.setattr(requests, "request", fake)
    return calls


@pytest.fixture
def api(tmp_path, sent):
    a = cloud.ReadOnlyAriston("u", "p", calls=cloud.CallLog(tmp_path, lambda: [GW]), budget=50, min_interval=0)
    assert a.connect()
    return a


def _setters():
    import inspect
    from ariston.ariston_api import AristonAPI
    return sorted(n for n, f in inspect.getmembers(AristonAPI, inspect.isfunction)
                  if n.startswith("set_") and not n.startswith("set_up"))


def test_library_has_setters_to_block():
    assert len(_setters()) >= 15


@pytest.mark.parametrize("name", _setters())
def test_every_setter_is_refused_before_the_network(api, sent, name):
    """Each library setter, called with dummy arguments, must be refused by the allowlist."""
    import inspect
    from unittest.mock import MagicMock
    fn = getattr(api, name)
    args = [GW if p == "gw_id" else MagicMock() for p in inspect.signature(fn).parameters]
    before = len(sent)
    with pytest.raises(cloud.Refused):
        fn(*args)
    assert len(sent) == before


def test_reads_go_through_and_are_logged(api, sent, tmp_path):
    api.get_features_for_device(GW)
    api.get_properties(GW, {"zones": [{"num": 1}]}, "en-US", "si")
    api.get_energy_account(GW)
    api.get_menu_items_by_id(GW, [124, 119, 200])
    api.get_bus_errors(GW)
    api.get_thermostat_time_progs(GW, 1, "si")
    assert [m for m, _ in sent].count("POST") == 2  # login + dataItems/get
    log = next(tmp_path.glob("*.cloud.csv")).read_text().splitlines()
    assert log[0] == cloud.CallLog.HEADER.strip()
    assert len(log) == 1 + 7 and all(GW not in line for line in log)
    assert ",menuItems/{gw}?menuItems=124;119;200,ok," in log[5]
    assert not any("login" in p.name for p in (tmp_path / "cloud").iterdir())  # token not stored


def test_budget(tmp_path, sent):
    a = cloud.ReadOnlyAriston("u", "p", calls=None, budget=2, min_interval=0)
    a.connect()
    a.get_energy_account(GW)
    with pytest.raises(cloud.Refused, match="budget"):
        a.get_energy_account(GW)
    assert len(sent) == 2


@pytest.mark.parametrize("method,path,ok", [
    ("POST", "remote/dataItems/X/get?umsys=si", True),
    ("POST", "remote/dataItems/X/set?umsys=si", False),
    ("POST", "remote/plants/X/consumptionsSettings", False),
    ("POST", "remote/plants/X/getConsumptionsSettings", True),
    ("POST", "remote/plantData/X/holiday", False),
    ("GET", "remote/plantData/X", True),
    ("POST", "remote/plantData/X", False),
    ("GET", "remote/bsbZones/X/1/mode", False),
])
def test_allowlist(method, path, ok):
    assert cloud.allowed(method, cloud.ARISTON_API_URL + path) is ok


def test_env_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# c\nARISTON_USERNAME=a@b\nARISTON_PASSWORD='x=y'\n")
    p.chmod(0o600)
    assert cloud.load_env(p) == {"ARISTON_USERNAME": "a@b", "ARISTON_PASSWORD": "x=y"}


def test_async_path_is_closed(api):
    import asyncio
    with pytest.raises(cloud.Refused):
        asyncio.run(api.async_get_energy_account(GW))
    with pytest.raises(cloud.Refused):
        asyncio.run(api.async_set_holiday(GW, None))


def test_parse_ids():
    assert cloud.parse_ids("119, 124,200-203") == [119, 124, 200, 201, 202, 203]


def test_interleaved_groups():
    g = cloud.interleaved_groups(333, 512, size=5)
    assert g[:4] == [[333, 334, 335, 336, 337], [400, 401, 402, 403, 404], [500, 501, 502, 503, 504],
                     [338, 339, 340, 341, 342]]
    assert sorted(i for x in g for i in x) == list(range(333, 513))


def test_sweep_probes_after_failures(capsys):
    calls = []

    class Api:
        def get_menu_items_by_id(self, gw, ids):
            calls.append(ids)
            if ids == [119]:
                if sum(1 for c in calls if c == [119]) > 2:
                    raise cloud.ConnectionException(500)
                return [{"id": 119, "nodeId": "T_19.1.1", "text": "Signal", "value": 1, "unit": "%"}]
            raise cloud.ConnectionException(500)

    found, failed = cloud.run_sweep(Api(), GW, [[i] for i in range(1, 20)], 119, 3)
    # probe, 3 fails, probe ok, 3 fails, probe fails -> stop
    assert calls == [[119], [1], [2], [3], [119], [4], [5], [6], [119]]
    assert failed == [[1], [2], [3], [4], [5], [6]]
    assert "FAILED, stopping" in capsys.readouterr().out
