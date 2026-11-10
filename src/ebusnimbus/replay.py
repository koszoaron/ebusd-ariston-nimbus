"""Replay check: decode a capture with ebusd and compare every value with this parser's.

The capture's wire bytes are served on a localhost TCP port that a private ebusd process uses as
its (plain) eBUS device, started with ``--readonly`` and without MQTT or scanning. That ebusd
never touches the real bus; nothing here talks to the deployed ebusd.

For each telegram ebusd decodes, its fields are matched to the parser's register readings by
position (master or slave part, byte offset, length, taken from ``ebusd --dumpconfig=json``) and
the values are compared: numbers after the field's divisor, enums through the field's value list.
A field that covers no register value exactly (wrong width, flag byte, id bytes) is a layout
mismatch. Registers the parser decodes but no ebusd message covers are reported as not mapped.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .payload import PayloadError, decode_readings
from .rawlog import iter_files
from .telegram import Telegram

ROOT = Path(__file__).resolve().parents[2]
# PBSB codes whose telegrams carry register values the parser decodes
REGISTER_PBSB = {"2000", "2001", "200e", "200f", "2010", "2020"}

_CMD = re.compile(r"\[update info\] received (MS|BC|MM) cmd: ([0-9a-f]+)(?: / ([0-9a-f]+))?$")
_DEC = re.compile(r"\[update notice\] received (?:(unknown) (?:MS|BC|MM) cmd: .*|"
                  r"(\S+) (\S+) (\S+?)(?: QQ=[0-9a-f]{2})?: (.*))$")
_ERR = re.compile(r"\[update (?:error|notice)\] (unable to .*|.*invalid.*)$")


# ---------------------------------------------------------------- feeding ebusd

def wire_stream(telegrams: list[Telegram]) -> bytes:
    """Escaped wire bytes of the telegrams, separated by SYN."""
    out = bytearray(b"\xaa\xaa")
    for tg in telegrams:
        out += bytes.fromhex(tg.raw) + b"\xaa"
    return bytes(out)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_ebusd(stream: bytes, ebusd: str, configpath: Path, *, chunk: int = 4096,
              pause: float = 0.01, settle: float = 3.0, timeout: float = 3600,
              extra_args: list[str] | None = None) -> list[str]:
    """Serve ``stream`` as an eBUS device to a private, read-only ebusd; return its log lines."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    dev_port = srv.getsockname()[1]
    done = threading.Event()

    def serve():
        conn, _ = srv.accept()
        with conn:
            time.sleep(0.5)
            for i in range(0, len(stream), chunk):
                conn.sendall(stream[i:i + chunk])
                time.sleep(pause)
            done.wait(timeout)  # keep the device open until the log has settled
        srv.close()

    threading.Thread(target=serve, daemon=True).start()
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "ebusd.log"
        cmd = [ebusd, "-f", "--readonly", "--nodevicecheck", f"--device=127.0.0.1:{dev_port}",
               f"--configpath={configpath}", "--scanconfig=none",
               f"--port={_free_port()}", "--localhost",  # private command port, never used
               "--log=all:notice", "--log=update:info"] + list(extra_args or [])  # with -f the log goes to stdout
        log_fh = open(log_path, "wb")
        proc = subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT)
        try:
            expected_s = len(stream) / chunk * pause
            start = time.monotonic()
            last_size, last_change = -1, time.monotonic()
            while time.monotonic() - start < timeout:
                time.sleep(0.5)
                size = log_path.stat().st_size if log_path.exists() else 0
                if size != last_size:
                    last_size, last_change = size, time.monotonic()
                elif time.monotonic() - start > expected_s and time.monotonic() - last_change > settle:
                    break
                if proc.poll() is not None:
                    break
        finally:
            done.set()
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
            log_fh.close()
        return log_path.read_text(errors="replace").splitlines() if log_path.exists() else []


def dump_config(ebusd: str, configpath: Path) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "dump.json"
        subprocess.run([ebusd, f"--configpath={configpath}", "--scanconfig=none", "--dumpconfig=json",
                        f"--dumpconfigto={out}", "-f"], check=True, capture_output=True, timeout=120)
        return json.loads(out.read_text())


# ---------------------------------------------------------------- ebusd log

@dataclass
class Decoded:
    master: str  # QQ ZZ PB SB NN data, hex, no CRC
    slave: str | None
    unknown: bool = False
    kind: str = ""
    circuit: str = ""
    name: str = ""
    values: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def parse_log(lines: list[str]) -> list[Decoded]:
    out: list[Decoded] = []
    for line in lines:
        m = _CMD.search(line)
        if m:
            out.append(Decoded(m.group(2), m.group(3)))
            continue
        if not out:
            continue
        m = _DEC.search(line)
        if m:
            if m.group(1):
                out[-1].unknown = True
            else:
                out[-1].kind, out[-1].circuit, out[-1].name = m.group(2), m.group(3), m.group(4)
                out[-1].values = m.group(5).split(";") if m.group(5) else []
            continue
        m = _ERR.search(line)
        if m:
            out[-1].errors.append(m.group(1))
    return out


# ---------------------------------------------------------------- comparison

def _master_hex(tg: Telegram) -> str:
    return f"{tg.src:02x}{tg.dst:02x}{tg.pbsb}{len(tg.data):02x}{tg.data.hex()}"


@dataclass
class FieldDef:
    name: str
    part: str
    offset: int
    length: int
    type: str
    ignored: bool
    bits: bool
    divisor: float | None
    values: dict[int, str] | None


def _fields(msg: dict) -> list[FieldDef]:
    """Field positions; master data starts after the id bytes that follow PBSB."""
    pos = {"m": len(msg["id"]) - 2, "s": 0}
    out = []
    prev_bits = None
    for f in msg.get("fielddefs", []):
        part = "s" if f.get("slave") else "m"
        bits = bool(f.get("isbits"))
        if bits and prev_bits == part:
            pos[part] -= f["length"]  # bit fields of one byte share it
        values = {int(k): v for k, v in f["values"].items()} if isinstance(f.get("values"), dict) else None
        out.append(FieldDef(f.get("name", ""), part, pos[part], f["length"], f["type"],
                            bool(f.get("isignored")), bits, f.get("divisor"), values))
        pos[part] += f["length"]
        prev_bits = part if bits else None
    return out


def _expected(fd: FieldDef, raw: int) -> str | float:
    if fd.values is not None:
        return fd.values.get(raw, str(raw))
    d = fd.divisor
    if d is None or d == 1:
        return float(raw)
    return raw / d if d > 0 else raw * -d


def _same(fd: FieldDef, ebusd_value: str, raw: int) -> bool:
    exp = _expected(fd, raw)
    if isinstance(exp, str):
        return ebusd_value == exp
    if ebusd_value == "-":  # ebusd's replacement value
        return raw in (0x7FFF, -0x8000, 0xFF, 0xFFFF)
    try:
        return abs(float(ebusd_value) - exp) < 1e-6 * max(1.0, abs(exp))
    except ValueError:
        return False


@dataclass
class Report:
    telegrams: int = 0
    seen_by_ebusd: int = 0
    not_seen: int = 0
    decoded: int = 0
    unknown: Counter = field(default_factory=Counter)  # (src, dst, pbsb, first id) -> n
    fields_ok: int = 0
    mismatches: dict = field(default_factory=lambda: defaultdict(list))  # (msg, field, reg) -> samples
    mismatch_n: Counter = field(default_factory=Counter)
    layout: Counter = field(default_factory=Counter)  # (msg, field, why) -> n
    unmapped: Counter = field(default_factory=Counter)  # (device, reg, msg) -> n
    ebusd_errors: Counter = field(default_factory=Counter)
    no_def: Counter = field(default_factory=Counter)
    ok_by_msg: Counter = field(default_factory=Counter)
    not_comparable: Counter = field(default_factory=Counter)
    rejected: Counter = field(default_factory=Counter)  # frames ebusd refused (layout shorter than the message)  # decoded, but the parser has no registers there

    @property
    def failed(self) -> bool:
        return bool(self.mismatch_n or self.layout or self.not_seen or self.ebusd_errors)


def compare(telegrams: list[Telegram], decoded: list[Decoded], config: dict, sink=None) -> Report:
    """Match ebusd's decoding to the parser's. ``sink(ts, message, field, device, regs, value)`` is
    called for every field value ebusd produced on a register (the update stream HA would get)."""
    defs: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for m in config["messages"]:
        defs[(m["circuit"], m["name"])].append(m)
    rep = Report(telegrams=len(telegrams))
    j = 0
    for tg in telegrams:
        mh = _master_hex(tg)
        k = j
        while k < len(decoded) and decoded[k].master != mh:
            k += 1
        if k == len(decoded):
            rep.not_seen += 1
            continue
        d, j = decoded[k], k + 1
        rep.seen_by_ebusd += 1
        try:
            readings = [r for r in decode_readings(tg) if r.part]
        except PayloadError:
            readings = []
        for e in d.errors:
            if "invalid pos" in e:  # frame shorter than the definition: rejected, no value produced
                rep.rejected[f"{d.circuit}.{d.name}" if d.circuit else e.split(" from ")[0][:80]] += 1
            else:
                rep.ebusd_errors[e[:120]] += 1
        if d.unknown or not d.circuit:
            first = tg.data[:2].hex() if tg.pbsb and tg.pbsb.startswith("20") else ""
            rep.unknown[(f"{tg.src:02x}", f"{tg.dst:02x}", tg.pbsb, first)] += 1
            continue
        rep.decoded += 1
        msg_name = f"{d.circuit}.{d.name}"
        if tg.pbsb not in REGISTER_PBSB:
            rep.not_comparable[(msg_name, tg.pbsb)] += 1  # 2070 date, 2038 bus control, ...
            continue
        body = bytes.fromhex(tg.pbsb) + tg.data
        cands = [m for m in defs.get((d.circuit, d.name), []) if body[:len(m["id"])] == bytes(m["id"])]
        if not cands:
            rep.no_def[msg_name] += 1
            continue
        fields = _fields(max(cands, key=lambda m: (len(m["id"]), m.get("passive", False))))
        shown = [f for f in fields if not f.ignored]
        if len(shown) != len(d.values):
            rep.layout[(msg_name, "*", f"{len(d.values)} values for {len(shown)} fields")] += 1
            continue
        by_pos = {(r.part, r.offset): r for r in readings}
        covered = set()
        for fd, val in zip(shown, d.values):
            r = by_pos.get((fd.part, fd.offset))
            if fd.bits:
                continue  # bit fields are not compared
            if r is not None and len(r.raw) < fd.length:
                # a field over consecutive registers (32-bit counter split over two ids): join the
                # raw values in order, unsigned little-endian
                raw, pos, parts = b"", fd.offset, []
                while len(raw) < fd.length and (fd.part, pos) in by_pos:
                    q = by_pos[(fd.part, pos)]
                    raw += q.raw
                    parts.append(q)
                    pos += len(q.raw)
                if len(raw) == fd.length:
                    covered.update(q.reg for q in parts)
                    joined = int.from_bytes(raw, "little")
                    if sink:
                        sink(tg.ts, msg_name, fd.name, r.device, tuple(q.reg for q in parts), val)
                    if _same(fd, val, joined):
                        rep.fields_ok += 1
                        rep.ok_by_msg[msg_name] += 1
                    else:
                        key = (msg_name, fd.name, f"{r.device} {'+'.join(q.reg for q in parts)}")
                        rep.mismatch_n[key] += 1
                        if len(rep.mismatches[key]) < 3:
                            rep.mismatches[key].append(f"ebusd {val!r} vs parser {joined}")
                    continue
            if r is None or len(r.raw) != fd.length:
                why = "no register value at this position" if r is None else \
                    f"{fd.length} byte field on {len(r.raw)} byte register {r.reg}"
                rep.layout[(msg_name, fd.name, why)] += 1
                continue
            covered.add(r.reg)
            if sink:
                sink(tg.ts, msg_name, fd.name, r.device, (r.reg,), val)
            if _same(fd, val, r.value):
                rep.fields_ok += 1
                rep.ok_by_msg[msg_name] += 1
            else:
                key = (msg_name, fd.name, f"{r.device} {r.reg}")
                rep.mismatch_n[key] += 1
                if len(rep.mismatches[key]) < 3:
                    rep.mismatches[key].append(f"ebusd {val!r} vs parser {r.value} -> {_expected(fd, r.value)!r}")
        for r in readings:
            if r.reg not in covered:
                rep.unmapped[(r.device, r.reg, msg_name)] += 1
    return rep


def render(rep: Report, sources: list[str]) -> str:
    L = ["# Replay check", "", f"Sources: {', '.join(sources)}", "",
         f"- telegrams (parsed ok): {rep.telegrams}; seen by ebusd: {rep.seen_by_ebusd}; "
         f"**not seen: {rep.not_seen}**",
         f"- decoded by a message definition: {rep.decoded}; unknown to the config: {sum(rep.unknown.values())}",
         f"- fields equal to the parser: {rep.fields_ok}; **mismatching: {sum(rep.mismatch_n.values())}**; "
         f"**layout mismatches: {sum(rep.layout.values())}**; ebusd errors: {sum(rep.ebusd_errors.values())}",
         f"- frames rejected by ebusd because their layout is shorter than the definition (no value "
         f"produced): {sum(rep.rejected.values())}",
         f"- result: **{'FAIL' if rep.failed else 'PASS'}**", ""]
    L += ["## Value mismatches", ""]
    if rep.mismatch_n:
        L += ["| message | field | register | n | samples |", "|---|---|---|---|---|"]
        for key, n in rep.mismatch_n.most_common():
            L.append(f"| {key[0]} | {key[1]} | {key[2]} | {n} | {'; '.join(rep.mismatches[key])} |")
    else:
        L.append("None.")
    L += ["", "## Layout mismatches", ""]
    if rep.layout:
        L += ["| message | field | problem | n |", "|---|---|---|---|"]
        L += [f"| {m} | {f} | {w} | {n} |" for (m, f, w), n in rep.layout.most_common()]
    else:
        L.append("None.")
    if rep.rejected:
        L += ["", "## Rejected frames by message", ""]
        L += [f"- {m}: {n}" for m, n in rep.rejected.most_common()]
    if rep.ebusd_errors or rep.no_def:
        L += ["", "## ebusd errors", ""]
        L += [f"- {e} ({n})" for e, n in rep.ebusd_errors.most_common()]
        L += [f"- decoded as {m} but no definition with a matching id ({n})" for m, n in rep.no_def.most_common()]
    if rep.not_comparable:
        L += ["", "## Decoded by ebusd, not comparable", "",
              "Telegrams without register values (the parser does not decode these PBSB codes yet).", ""]
        L += [f"- {m} ({p}): {n}" for (m, p), n in sorted(rep.not_comparable.items())]
    L += ["", "## Registers decoded by the parser but not by ebusd", "",
          "Inside a telegram ebusd decoded, no field covers them (for example a single-id message", "matching a multi-id read).", ""]
    if rep.unmapped:
        L += ["| device | register | in message | n |", "|---|---|---|---|"]
        L += [f"| {d} | {r} | {m} | {n} |" for (d, r, m), n in sorted(rep.unmapped.items())]
    else:
        L.append("None.")
    L += ["", "## Telegrams unknown to the config", "", "| QQ | ZZ | PBSB | first id | n |", "|---|---|---|---|---|"]
    L += [f"| {a} | {b} | {p} | {i} | {n} |" for (a, b, p, i), n in sorted(rep.unknown.items())]
    return "\n".join(L) + "\n"


def find_ebusd(explicit: str | None = None) -> str | None:
    return explicit or os.environ.get("EBUSD") or shutil.which("ebusd")


def replay(paths: list[Path], ebusd: str, configpath: Path, sink=None, telegrams=None, **kw) -> Report:
    tgs = telegrams if telegrams is not None else \
        [e for e in iter_files(paths) if isinstance(e, Telegram) and e.status == "ok"]
    lines = run_ebusd(wire_stream(tgs), ebusd, configpath, **kw)
    return compare(tgs, parse_log(lines), dump_config(ebusd, configpath), sink)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Replay captures through a private read-only ebusd and "
                                             "compare its decoding with the parser.")
    ap.add_argument("captures", nargs="+", type=Path)
    ap.add_argument("--config", type=Path, default=ROOT / "config", help="ebusd config directory")
    ap.add_argument("--ebusd", help="ebusd binary (default: $EBUSD or ebusd on PATH)")
    ap.add_argument("--out", type=Path, default=Path("reports") / "replay.md")
    a = ap.parse_args(argv)
    ebusd = find_ebusd(a.ebusd)
    if not ebusd:
        raise SystemExit("ebusd binary not found (use --ebusd or $EBUSD)")
    paths = sorted(p for p in a.captures if not p.name.endswith(".side.csv"))
    rep = replay(paths, ebusd, a.config)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(render(rep, [p.name for p in paths]), encoding="utf-8")
    print(f"{rep.telegrams} telegrams, {rep.seen_by_ebusd} seen by ebusd, {rep.fields_ok} fields equal, "
          f"{sum(rep.mismatch_n.values())} mismatching, {sum(rep.layout.values())} layout mismatches: "
          f"{'FAIL' if rep.failed else 'PASS'}; report in {a.out}")
    raise SystemExit(1 if rep.failed else 0)


if __name__ == "__main__":
    main()
