"""Side channel: record independent measurements next to the bus capture.

Output is captures/YYYY-MM-DD.side.csv with columns ts,source,metric,value; ts carries its UTC
offset (``2026-10-25 02:30:00.000+01:00``) from 2026-10-08 on. Sources are
pluggable (register a class in SOURCES); only the Shelly Gen2 HTTP RPC poller exists so far.
An MQTT source can be added without touching the writer.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from . import config as cfg
from .localtime import day_of, format_ts, wall_now

log = logging.getLogger("ebus-side")
HEADER = "ts,source,metric,value\n"


class Source:
    """A pollable measurement source: poll() returns [(metric, value), ...]."""

    interval = 5.0

    def __init__(self, name: str, conf: dict):
        self.name = name
        self.conf = conf
        self.interval = float(conf.get("interval", self.interval))

    def poll(self) -> list[tuple[str, float | str]]:  # pragma: no cover - interface
        raise NotImplementedError


class ShellyEM(Source):
    """Shelly Pro 3EM (Gen2) via ``GET /rpc/EM.GetStatus?id=0`` (no MQTT needed).

    Field names per the Shelly Gen2 EM component: total_act_power, a/b/c_act_power, ...
    """

    DEFAULT_METRICS = ["total_act_power", "a_act_power", "b_act_power", "c_act_power"]

    def poll(self):
        base = self.conf["url"].rstrip("/")
        url = f"{base}/rpc/EM.GetStatus?id={int(self.conf.get('id', 0))}"
        with urllib.request.urlopen(url, timeout=float(self.conf.get("timeout", 3))) as r:
            data = json.load(r)
        out = []
        for m in self.conf.get("metrics", self.DEFAULT_METRICS):
            v = data.get(m)
            if isinstance(v, (int, float)):
                out.append((m, v))
        if not out:
            raise ValueError("no configured metric present in response")
        return out


SOURCES = {"shelly_em": ShellyEM}


class SideWriter:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = None
        self._day = None

    def write(self, ts: datetime, source: str, metric: str, value) -> None:
        day = day_of(ts)
        with self._lock:
            if day != self._day:
                if self._fh:
                    self._fh.close()
                path = self.dir / f"{day}.side.csv"
                new = not path.exists() or path.stat().st_size == 0
                self._fh = open(path, "a", encoding="utf-8", buffering=1)
                if new:
                    self._fh.write(HEADER)
                self._day = day
            text = str(value).replace(",", ";").replace("\n", " ")
            self._fh.write(f"{format_ts(ts)},{source},{metric},{text}\n")

    def close(self):
        with self._lock:
            if self._fh:
                self._fh.close()
                self._fh = None


def poll_loop(src: Source, writer: SideWriter, stop: threading.Event) -> None:
    next_t = time.monotonic()
    last_err = None
    while not stop.is_set():
        ts = wall_now()
        try:
            for metric, value in src.poll():
                writer.write(ts, src.name, metric, value)
            last_err = None
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            # recorded in the file so gaps are explained; logged once per streak
            writer.write(ts, src.name, "_error", type(exc).__name__)
            if last_err != type(exc).__name__:
                log.warning("%s: %s", src.name, exc)
                last_err = type(exc).__name__
        next_t += src.interval
        delay = next_t - time.monotonic()
        if delay < 0:
            next_t = time.monotonic()
            delay = 0
        stop.wait(delay)


def build_sources(conf: dict) -> list[Source]:
    out = []
    for i, s in enumerate(conf.get("sources", [])):
        kind = s.get("type")
        if kind not in SOURCES:
            raise SystemExit(f"unknown side channel source type {kind!r}; known: {', '.join(SOURCES)}")
        out.append(SOURCES[kind](s.get("name", f"{kind}{i}"), s))
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    full = cfg.load(args.config)
    conf = full.get("sidechannel", {})
    out_dir = conf.get("dir") or full.get("capture", {}).get("dir", "captures")
    writer = SideWriter(Path(out_dir))
    sources = build_sources(conf)
    if not sources:
        raise SystemExit("no [[sidechannel.sources]] configured")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    threads = [threading.Thread(target=poll_loop, args=(s, writer, stop), daemon=True) for s in sources]
    for t in threads:
        t.start()
    stop.wait()
    for t in threads:
        t.join(timeout=5)
    writer.close()


if __name__ == "__main__":
    main()
