"""Capture service: follow ebusd's raw log and append telegrams to captures/YYYY-MM-DD.log.

Passive only: it reads a file (or stdin / a log-follow command) and never talks to ebusd.

ebusd writes naive local time. Every line is written with its UTC offset; in the hour that
repeats when daylight saving ends the offset is worked out from the order of the lines
(``localtime.LocalClock``). Duplicates (a source replaying lines already written) are recognised
by (timestamp, content), not by timestamp alone.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Callable, Iterator

from . import config as cfg
from .localtime import LocalClock, day_of, format_ts, local_zone, wall_now
from .rawlog import format_line, normalise_line

log = logging.getLogger("ebus-capture")

FSYNC_INTERVAL = 10.0
STATE_INTERVAL = 5.0


class DailyWriter:
    """Appends capture lines to <dir>/<date>.log, switching files with the line's own date."""

    def __init__(self, directory: Path, gap_seconds: float = 30.0, tz: tzinfo | None = None,
                 now: Callable[[], datetime] = wall_now):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.gap_seconds = gap_seconds
        self.tz = tz or local_zone()
        self.now = now
        self._fh = None
        self._day: str | None = None
        # newest timestamp on disk and the lines written at exactly that time
        self.last_ts: datetime | None = None
        self._last_bodies: set[str] = set()
        self._find_last()
        self.clock = LocalClock(self.tz, now=now, last=self.last_ts)
        self._last_sync = time.monotonic()
        self.count = 0
        self.skipped_dupes = 0

    def _path(self, day: str) -> Path:
        return self.dir / f"{day}.log"

    def _find_last(self) -> None:
        """Newest telegram already on disk, so a restart never duplicates lines."""
        files = sorted(self.dir.glob("????-??-??.log"))
        for f in reversed(files):
            try:
                with open(f, "rb") as fh:
                    fh.seek(0, os.SEEK_END)
                    size = fh.tell()
                    fh.seek(max(0, size - 65536))
                    tail = fh.read().decode("utf-8", errors="replace").splitlines()
                    if size > 65536:
                        tail = tail[1:]  # probably cut in the middle
            except OSError:
                continue
            clock = LocalClock(self.tz)  # lines without offset: written before 2026-10-08
            lines = [(clock.resolve(cl.ts), cl) for cl in map(normalise_line, tail) if cl]
            if lines:
                self.last_ts = lines[-1][0]
                self._last_bodies = {format_line(cl).split(" ", 2)[2] for ts, cl in lines if ts == self.last_ts}
                return

    def _open(self, day: str) -> None:
        if self._fh:
            self._fh.close()
        self._fh = open(self._path(day), "a", encoding="utf-8", buffering=1)
        self._day = day

    def marker(self, text: str, ts: datetime | None = None) -> None:
        """Write a '#' marker. It is stamped with the newest data timestamp (ebusd's clock) so it
        stays in order; the wall clock of this host is added to the text when it differs."""
        wall = self.now()
        if ts is None:
            ts = self.last_ts or wall
            if self.last_ts is not None:
                text += f" [wall {format_ts(wall)}]"
        day = day_of(ts)
        if day != self._day:
            self._open(day)
        self._fh.write(f"# {format_ts(ts)} {text}\n")

    def source_restarted(self) -> None:
        """The source starts over (log truncated, follower reconnected and replaying):
        the order of naive timestamps says nothing about the previous ones any more."""
        self.clock.reset()

    def write(self, line: str) -> bool:
        """Write one source line. Returns False if it was a duplicate or not a telegram line."""
        cl = normalise_line(line)
        if cl is None:
            return False
        cl.ts = self.clock.resolve(cl.ts)
        text = format_line(cl)
        body = text.split(" ", 2)[2]
        if self.last_ts is not None and (cl.ts < self.last_ts or (cl.ts == self.last_ts and body in self._last_bodies)):
            self.skipped_dupes += 1
            return False
        day = day_of(cl.ts)
        if day != self._day:
            self._open(day)
        if self.last_ts is not None:
            gap = (cl.ts - self.last_ts).total_seconds()
            if gap > self.gap_seconds:
                self._fh.write(f"# {format_ts(cl.ts)} gap {gap:.1f}s since {format_ts(self.last_ts)}\n")
        self._fh.write(text + "\n")
        if cl.ts != self.last_ts:
            self._last_bodies = set()
        self._last_bodies.add(body)
        self.last_ts = cl.ts
        self.count += 1
        now = time.monotonic()
        if now - self._last_sync > FSYNC_INTERVAL:
            self.sync()
        return True

    def sync(self) -> None:
        if self._fh:
            self._fh.flush()
            os.fsync(self._fh.fileno())
        self._last_sync = time.monotonic()

    def close(self) -> None:
        if self._fh:
            self.sync()
            self._fh.close()
            self._fh = None


class FileTail:
    """Follow ebusd's raw log file through its rotation (rename to .old) and truncation (restart).

    Yields complete lines, or None on every idle tick so the caller can check for shutdown.
    The read offset is persisted so a restart of the capture service resumes where it stopped,
    including the case where ebusd rotated the file in the meantime.
    """

    def __init__(self, path: Path, state_path: Path, poll: float = 0.5):
        self.path = Path(path)
        self.old = Path(str(path) + ".old")
        self.state_path = Path(state_path)
        self.poll = poll
        self._fh = None
        self._ino = None
        self._data = b""  # bytes read but not yet delivered (incl. a partial last line)
        self._consumed = 0  # file offset of the first byte of _data
        self._last_state = 0.0

    # -- state -------------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def save_state(self) -> None:
        if self._fh is None:
            return
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"inode": self._ino, "offset": self._consumed}))
        os.replace(tmp, self.state_path)
        self._last_state = time.monotonic()

    # -- file handling -----------------------------------------------------
    def _open(self, path: Path):
        fh = open(path, "rb")
        self._fh, self._ino = fh, os.fstat(fh.fileno()).st_ino
        self._data, self._consumed = b"", 0

    def _resume(self) -> None:
        st = self._load_state()
        if not st:
            return
        try:
            cur = os.stat(self.path).st_ino
        except OSError:
            cur = None
        try:
            old = os.stat(self.old).st_ino
        except OSError:
            old = None
        if st.get("inode") == cur and cur is not None:
            self._open(self.path)
            if st["offset"] <= os.fstat(self._fh.fileno()).st_size:
                self._fh.seek(st["offset"])
                self._consumed = st["offset"]
        elif st.get("inode") == old and old is not None:
            log.info("ebusd rotated the raw log while we were down; finishing %s first", self.old)
            self._open(self.old)
            self._fh.seek(st["offset"])
            self._consumed = st["offset"]

    def _drain(self) -> Iterator[str]:
        # _consumed only advances after the caller has come back for the next line, so a state
        # saved while a line is in flight re-delivers it (the writer drops duplicates) instead of losing it
        while True:
            nl = self._data.find(b"\n")
            if nl >= 0:
                line, self._data = self._data[:nl], self._data[nl + 1:]
                yield line.decode("utf-8", errors="replace")
                self._consumed += nl + 1
                continue
            chunk = self._fh.read(65536)
            if not chunk:
                return
            self._data += chunk

    def lines(self) -> Iterator[str | None]:
        self._resume()
        while True:
            if self._fh is None:
                try:
                    self._open(self.path)
                except OSError:
                    yield None
                    time.sleep(self.poll)
                    continue
            yield from self._drain()
            # rotation: path now points at a different inode -> old file is fully read, switch
            try:
                cur = os.stat(self.path)
            except OSError:
                cur = None
            if cur is not None and cur.st_ino != self._ino:
                yield from self._drain()
                self._fh.close()
                self._fh = None
                continue
            # truncation: ebusd restarted and reopened the file with "w"
            if cur is not None and cur.st_size < self._consumed + len(self._data):
                log.info("raw log truncated (ebusd restart?); reading from start")
                self._fh.seek(0)
                self._data, self._consumed = b"", 0
                yield "#SOURCE-RESTART truncated"
                continue
            if time.monotonic() - self._last_state > STATE_INTERVAL:
                self.save_state()
            yield None
            time.sleep(self.poll)


def cmd_lines(cmd: list[str]) -> Iterator[str | None]:
    """Follow the stdout of a long-running command (e.g. a container log follower); restart it if it exits."""
    backoff = 1.0
    while True:
        log.info("starting source command: %s", cmd[0])
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, errors="replace", bufsize=1)
        started = time.monotonic()
        assert proc.stdout is not None
        for line in proc.stdout:
            yield line.rstrip("\n")
        proc.wait()
        yield f"#SOURCE-RESTART exit {proc.returncode}"
        backoff = 1.0 if time.monotonic() - started > 60 else min(backoff * 2, 60)
        time.sleep(backoff)


def stdin_lines() -> Iterator[str | None]:
    for line in sys.stdin:
        yield line.rstrip("\n")


class _Stop(Exception):
    pass


def run(source: Iterator[str | None], writer: DailyWriter, *, tail: FileTail | None = None,
        strict: bool, stop_flag: dict | None = None) -> None:
    stop_flag = stop_flag if stop_flag is not None else {}
    writer.marker("capture start")
    try:
        for line in source:
            if stop_flag.get("stop"):
                break
            if line is None:
                continue
            if line.startswith("#SOURCE-RESTART"):
                _, kind, *detail = line.split()
                writer.marker("source truncated" if kind == "truncated" else f"source command exited ({' '.join(detail)})")
                writer.source_restarted()
                continue
            if not writer.write(line) and strict and line.strip():
                if normalise_line(line) is None:
                    writer.marker("unparsed source line: " + line.strip()[:200])
    finally:
        if tail is not None:
            tail.save_state()
        writer.marker(f"capture stop ({writer.count} lines, {writer.skipped_dupes} duplicates skipped)")
        writer.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", help="TOML config (default: $EBUS_CONFIG or config.local.toml)")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    c = cfg.load(args.config).get("capture", {})
    out_dir = Path(c.get("dir", "captures"))
    tz = local_zone(c.get("timezone"))
    writer = DailyWriter(out_dir, gap_seconds=float(c.get("gap_seconds", 30)), tz=tz)
    kind = c.get("source", "file")
    tail = None
    if kind == "file":
        tail = FileTail(Path(c["path"]), out_dir / ".capture-state.json")
        source, strict = tail.lines(), True
    elif kind == "cmd":
        source, strict = cmd_lines(list(c["cmd"])), False
    elif kind == "stdin":
        source, strict = stdin_lines(), False
    else:
        raise SystemExit(f"unknown capture.source {kind!r} (file, cmd or stdin)")

    stop = {"stop": False}

    def _sig(_s, _f):
        stop["stop"] = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    run(source, writer, tail=tail, strict=strict, stop_flag=stop)


if __name__ == "__main__":
    main()
