"""Read-only access to the Ariston NET cloud, as a stimulus for decoding.

Built on ``ariston`` (fustom/python-ariston-api, the library of the HACS integration). Every
request of the library passes through ``_get``/``_post``; ``ReadOnlyAriston`` overrides both and
refuses any method/path that is not in ``ALLOWED`` before a request is made, so setters,
set-by-ID, holiday and consumption-settings writes cannot reach the cloud. On top of that:

- a call budget per run and a minimum interval between calls (the cloud bans busy accounts),
- every call logged to ``captures/YYYY-MM-DD.cloud.csv`` (start/end with UTC offset, method,
  endpoint with the gateway id replaced by ``{gw}``, outcome) so it lines up with the bus capture,
- raw responses saved under ``captures/cloud/`` (they hold the gateway id and plant data: never git),
- credentials only from the env file (``.env``: ARISTON_USERNAME, ARISTON_PASSWORD), never printed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .localtime import day_of, format_ts, wall_now

try:
    from ariston.ariston_api import AristonAPI, ConnectionException
    from ariston.const import ARISTON_API_URL
except ImportError:  # pragma: no cover - optional dependency
    AristonAPI = object  # type: ignore[assignment,misc]
    ConnectionException = Exception  # type: ignore[assignment,misc]
    ARISTON_API_URL = "https://www.ariston-net.remotethermo.com/api/v2/"

log = logging.getLogger("ebus-cloud")

GW = r"[^/?]+"
# (method, path relative to the API root, without query). Anything else is refused.
ALLOWED: list[tuple[str, re.Pattern]] = [(m, re.compile(p)) for m, p in [
    ("POST", r"accounts/login"),
    ("GET", r"remote/plants"),
    ("GET", r"remote/plants/lite"),
    ("GET", r"velis/plants"),
    ("GET", rf"remote/plants/{GW}/features"),
    ("POST", rf"remote/plants/{GW}/getConsumptionsSettings"),
    ("POST", rf"remote/dataItems/{GW}/get"),
    ("GET", rf"remote/reports/{GW}/energyAccount"),
    ("GET", rf"remote/reports/{GW}/consSequencesApi8"),
    ("GET", rf"remote/(?:plantData|medPlantData|sePlantData|slpPlantData|bsbPlantData)/{GW}"),
    ("GET", rf"remote/timeProgs/{GW}/ChZn\d+"),
    ("GET", rf"menuItems/{GW}"),
    ("GET", r"busErrors"),
]]


class Refused(RuntimeError):
    """A request outside the read allowlist, or over budget. Nothing was sent."""


def allowed(method: str, url: str, api_url: str = ARISTON_API_URL) -> bool:
    path = url[len(api_url):] if url.startswith(api_url) else url
    path = path.split("?", 1)[0].strip("/")
    return any(m == method and p.fullmatch(path) for m, p in ALLOWED)


def load_env(path: str | os.PathLike = ".env") -> dict[str, str]:
    """KEY=VALUE lines; comments and blanks ignored. Values are never echoed."""
    out = {}
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"{p} not found: copy .env.example to .env and fill in the Ariston account")
    if p.stat().st_mode & 0o077:
        log.warning("%s is readable by group/others; chmod 600 it", p)
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class CallLog:
    """captures/YYYY-MM-DD.cloud.csv: one row per cloud request; raw bodies in captures/cloud/."""

    HEADER = "start,end,method,endpoint,outcome,response\n"

    def __init__(self, directory: Path, gw_ids: Callable[[], list[str]]):
        self.dir = Path(directory)
        self.raw = self.dir / "cloud"
        self.raw.mkdir(parents=True, exist_ok=True)
        self.gw_ids = gw_ids

    def endpoint(self, url: str, api_url: str) -> str:
        path = url[len(api_url):] if url.startswith(api_url) else url
        for gw in self.gw_ids():
            path = path.replace(gw, "{gw}")
        return path

    def write(self, start, end, method: str, endpoint: str, outcome: str, body: Any) -> None:
        name = ""
        if body is not None:
            name = f"{start.strftime('%Y%m%d-%H%M%S')}-{re.sub(r'[^A-Za-z0-9]+', '_', endpoint.split('?')[0])[:60]}.json"
            (self.raw / name).write_text(json.dumps(body, indent=1, ensure_ascii=False), encoding="utf-8")
        path = self.dir / f"{day_of(start)}.cloud.csv"
        new = not path.exists()
        with open(path, "a", encoding="utf-8") as fh:
            if new:
                fh.write(self.HEADER)
            ep = endpoint.replace(",", ";")
            fh.write(f"{format_ts(start)},{format_ts(end)},{method},{ep},{outcome},{name}\n")


class ReadOnlyAriston(AristonAPI):  # type: ignore[misc,valid-type]
    def __init__(self, username: str, password: str, *, calls: CallLog | None = None,
                 budget: int = 30, min_interval: float = 10.0, api_url: str = ARISTON_API_URL):
        super().__init__(username, password, api_url=api_url)
        self._api_url = api_url
        self.calls = calls
        self.budget = budget
        self.min_interval = min_interval
        self.used = 0
        self._last = 0.0
        self.gw_ids: list[str] = []

    def _guard(self, method: str, url: str) -> None:
        if not allowed(method, url, self._api_url):
            raise Refused(f"{method} {url.split('?')[0][len(self._api_url):]} is not a read endpoint")
        if self.used >= self.budget:
            raise Refused(f"call budget of {self.budget} used up")
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)

    def _call(self, method: str, url: str, fn: Callable[[], Any]) -> Any:
        self._guard(method, url)
        self.used += 1
        start = wall_now()
        outcome, body = "ok", None
        try:
            body = fn()
            return body
        except ConnectionException as exc:
            outcome = f"error {exc.args[0] if exc.args else ''}".strip()
            raise
        except Exception as exc:  # recorded, then re-raised
            outcome = f"error {type(exc).__name__}"
            raise
        finally:
            self._last = time.monotonic()
            if self.calls is not None:
                login = url.endswith("accounts/login")
                self.calls.write(start, wall_now(), method, self.calls.endpoint(url, self._api_url),
                                 outcome, None if login else body)  # the login reply holds the token

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._call("GET", path, lambda: super(ReadOnlyAriston, self)._get(path, params))

    def _post(self, path: str, body: Any) -> Any:
        return self._call("POST", path, lambda: super(ReadOnlyAriston, self)._post(path, body))

    # the async request path is not used here and is closed entirely
    async def _async_get(self, *args: Any, **kw: Any) -> Any:
        raise Refused("async requests are disabled in the read-only client")

    async def _async_post(self, *args: Any, **kw: Any) -> Any:
        raise Refused("async requests are disabled in the read-only client")

    # menuItems with arbitrary ids (the library only asks for 124 and 119)
    def get_menu_items_by_id(self, gw_id: str, ids: list[int]) -> Any:
        return self._get(f"{self._api_url}menuItems/{gw_id}?menuItems={','.join(map(str, ids))}")


MENU_FIELDS = ("id", "nodeId", "text", "value", "unit", "min", "max", "decimals", "step", "isEnum", "readOnly")


def parse_ids(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        lo, _, hi = part.strip().partition("-")
        out += list(range(int(lo), int(hi or lo) + 1))
    return out


def interleaved_groups(lo: int, hi: int, region: int = 100, size: int = 5) -> list[list[int]]:
    """Groups of `size` ids from lo..hi, taken round-robin from each `region`-wide block."""
    blocks = []
    start = lo
    while start <= hi:
        end = min(hi, (start // region + 1) * region - 1)
        ids = list(range(start, end + 1))
        blocks.append([ids[i:i + size] for i in range(0, len(ids), size)])
        start = end + 1
    out = []
    while any(blocks):
        for b in blocks:
            if b:
                out.append(b.pop(0))
    return out


def run_sweep(api: "ReadOnlyAriston", gw: str, groups: list[list[int]], probe: int, probe_after: int) -> tuple[list, list]:
    """Read menu groups; after `probe_after` failed groups in a row read `probe` (a known-good id):
    if it works the failures were invalid ids and the sweep goes on, otherwise it stops."""
    found: list = []
    failed: list[list[int]] = []

    def one(ids: list[int]) -> list | None:
        label = f"{ids[0]}-{ids[-1]}" if len(ids) > 1 else str(ids[0])
        try:
            got = api.get_menu_items_by_id(gw, ids) or []
        except ConnectionException as exc:
            print(f"{wall_now():%H:%M:%S} ids {label}: cloud error {exc.args[0] if exc.args else ''}", flush=True)
            return None
        rows = [{k: m.get(k) for k in MENU_FIELDS} for m in got]
        desc = "; ".join(f"{m['id']} {m['nodeId']} {m['text']}={m['value']}{m['unit'] or ''}" for m in rows)
        print(f"{wall_now():%H:%M:%S} ids {label}: {len(rows)} found{': ' + desc if rows else ''}", flush=True)
        return rows

    def probe_ok() -> bool:
        rows = one([probe])
        ok = bool(rows)
        print(f"  probe {probe}: {'ok, continuing' if ok else 'FAILED, stopping'}", flush=True)
        return ok

    try:
        if not probe_ok():
            return found, failed
        in_row = 0
        for g in groups:
            rows = one(g)
            if rows is None:
                failed.append(g)
                in_row += 1
                if in_row >= probe_after:
                    if not probe_ok():
                        break
                    in_row = 0
                continue
            in_row = 0
            found += rows
    except Refused as exc:
        print(f"stopped: {exc}", flush=True)
    finally:
        spec = ",".join(f"{c[0]}-{c[-1]}" for c in failed)
        print(f"failed groups ({len(failed)}), not retried: {spec or 'none'}", flush=True)
    return found, failed


def _client(env_path: str, out_dir: Path, budget: int, interval: float) -> ReadOnlyAriston:
    if AristonAPI is object:
        raise SystemExit("the 'ariston' package is not installed (pip install 'ariston==0.19.9')")
    logging.getLogger("ariston").setLevel(logging.WARNING)  # its debug log prints responses
    env = load_env(env_path)
    try:
        user, pwd = env["ARISTON_USERNAME"], env["ARISTON_PASSWORD"]
    except KeyError as exc:
        raise SystemExit(f"{env_path} lacks {exc.args[0]}") from None
    holder: list[str] = []
    api = ReadOnlyAriston(user, pwd, calls=CallLog(out_dir, lambda: holder), budget=budget, min_interval=interval)
    api.gw_ids = holder
    return api


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Read-only Ariston cloud probe (see module docstring).")
    ap.add_argument("action", choices=["discover", "properties", "menu", "sweep", "energy", "consumption", "errors", "timeprogs"])
    ap.add_argument("--ids", help="menu: comma-separated ids and ranges, e.g. 119,124,200-299")
    ap.add_argument("--batch", type=int, default=10, help="menu: ids per request")
    ap.add_argument("--progress", action="store_true", help="menu: print one line per request")
    ap.add_argument("--skip-errors", action="store_true",
                    help="menu: continue after a cloud error (stops after two in a row)")
    ap.add_argument("--split-on-error", action="store_true",
                    help="menu: retry a failing batch one id at a time")
    ap.add_argument("--range", help="sweep: lo-hi id range, read round-robin per 100-id block")
    ap.add_argument("--groups-file", type=Path, help="sweep: one group per line (e.g. 405-409 or 132)")
    ap.add_argument("--probe", type=int, default=119, help="sweep: known-good id checked first and after failures")
    ap.add_argument("--probe-after", type=int, default=5, help="sweep: failed groups in a row before the probe")
    ap.add_argument("--max-errors", type=int, default=2, help="menu: stop after this many cloud errors in a row")
    ap.add_argument("--stop-after-empty", type=int, default=0, help="menu: stop after N empty batches in a row")
    ap.add_argument("--json-out", type=Path, help="write the summary JSON here instead of stdout")
    ap.add_argument("--zone", type=int, default=1)
    ap.add_argument("--env", default=".env")
    ap.add_argument("--out", type=Path, default=Path("captures"))
    ap.add_argument("--budget", type=int, default=10, help="max cloud requests in this run (login counts)")
    ap.add_argument("--interval", type=float, default=10.0, help="min seconds between requests")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    api = _client(a.env, a.out, a.budget, a.interval)
    if not api.connect():
        raise SystemExit("login failed")
    devices = api.get_devices()
    gws = [d.get("gw") or d.get("gwId") for d in devices if isinstance(d, dict)]
    api.gw_ids.extend(g for g in gws if g)
    if not api.gw_ids:
        raise SystemExit("no plant found for this account")
    gw = api.gw_ids[0]
    summary: Any
    if a.action == "discover":
        feats = api.get_features_for_device(gw)
        summary = {"plants": len(devices), "features": sorted(feats) if isinstance(feats, dict) else feats}
    elif a.action == "properties":
        feats = api.get_features_for_device(gw)
        props = api.get_properties(gw, feats, "en-US", "si")
        summary = [{k: i.get(k) for k in ("id", "zone", "value", "unit")} for i in props.get("items", [])]
    elif a.action == "menu":
        if not a.ids:
            raise SystemExit("menu needs --ids")
        ids = parse_ids(a.ids)
        summary = []
        state = {"errors_in_row": 0, "empty_in_row": 0, "stop": False}
        failed: list[list[int]] = []

        def ask(chunk: list[int]) -> None:
            label = f"{chunk[0]}-{chunk[-1]}" if len(chunk) > 1 else str(chunk[0])
            try:
                got = api.get_menu_items_by_id(gw, chunk) or []
                state["errors_in_row"] = 0
            except Refused as exc:
                print(f"stopped: {exc}", flush=True)
                state["stop"] = True
                return
            except ConnectionException as exc:
                state["errors_in_row"] += 1
                print(f"{wall_now():%H:%M:%S} ids {label}: cloud error {exc.args[0] if exc.args else ''}", flush=True)
                if a.skip_errors:
                    failed.append(chunk)
                if state["errors_in_row"] >= a.max_errors or not (a.skip_errors or a.split_on_error):
                    print("stopped after cloud error", flush=True)
                    state["stop"] = True
                elif a.split_on_error and not a.skip_errors and len(chunk) > 1:
                    print(f"  retrying {label} one id at a time", flush=True)
                    state["errors_in_row"] = 0
                    for one in chunk:
                        if state["stop"]:
                            return
                        ask([one])
                return
            rows = [{k: m.get(k) for k in MENU_FIELDS} for m in got]
            summary.extend(rows)
            if len(chunk) > 1 or rows:
                state["empty_in_row"] = 0 if rows else state["empty_in_row"] + 1
            if a.progress:
                found = "; ".join(f"{m['id']} {m['nodeId']} {m['text']}={m['value']}{m['unit'] or ''}" for m in rows)
                print(f"{wall_now():%H:%M:%S} ids {label}: {len(rows)} found{': ' + found if rows else ''}", flush=True)

        try:
            for i in range(0, len(ids), a.batch):
                ask(ids[i:i + a.batch])
                if state["stop"]:
                    break
                if a.stop_after_empty and state["empty_in_row"] >= a.stop_after_empty:
                    print(f"stopped: {state['empty_in_row']} empty batches in a row (last id {ids[min(i + a.batch, len(ids)) - 1]})", flush=True)
                    break
        finally:
            if failed:
                spec = ",".join(f"{c[0]}-{c[-1]}" if len(c) > 1 else str(c[0]) for c in failed)
                print(f"failed groups ({len(failed)}), not retried: {spec}", flush=True)
    elif a.action == "sweep":
        if a.groups_file:
            groups = [parse_ids(line) for line in a.groups_file.read_text().split() if line.strip()]
        else:
            lo, _, hi = (a.range or "").partition("-")
            if not hi:
                raise SystemExit("sweep needs --range lo-hi or --groups-file")
            groups = interleaved_groups(int(lo), int(hi), size=a.batch)
        summary, _failed = run_sweep(api, gw, groups, a.probe, a.probe_after)
    elif a.action == "energy":
        summary = api.get_energy_account(gw)
    elif a.action == "consumption":
        summary = {"account": api.get_energy_account(gw),
                   "sequences": api.get_consumptions_sequences(gw, "Ch,Dhw")}
    elif a.action == "errors":
        summary = api.get_bus_errors(gw)
    else:
        summary = api.get_thermostat_time_progs(gw, a.zone, "si")
    text = json.dumps(summary, indent=1, ensure_ascii=False, default=str)
    if a.json_out:
        a.json_out.write_text(text, encoding="utf-8")
    else:
        print(text)
    print(f"{api.used} cloud requests; log in {a.out}/{day_of(wall_now())}.cloud.csv", file=sys.stderr)


if __name__ == "__main__":
    main()
