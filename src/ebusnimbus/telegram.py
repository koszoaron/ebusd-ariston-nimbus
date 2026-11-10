"""eBUS telegram framing: unescape, CRC, telegram layouts (docs/protocol.md, Framing)."""

from __future__ import annotations

from dataclasses import dataclass, field

SYN = 0xAA
ESC = 0xA9
ACK = 0x00
NACK = 0xFF
BROADCAST = 0xFE

DEVICES: dict[int, str] = {
    0x13: "energymgr", 0x18: "energymgr",
    0x03: "heatpump", 0x1E: "heatpump",
    0x70: "sensys", 0x75: "sensys",
    0x7F: "gateway", 0x84: "gateway",
    0xF7: "mfk", 0xFC: "mfk",
    0x31: "ebusd", 0x36: "ebusd",
    0x00: "scanner",
}


class EscapeError(ValueError):
    pass


def unescape(data: bytes) -> bytes:
    """a9 00 -> a9, a9 01 -> aa. A dangling or unknown escape raises EscapeError."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b == ESC:
            if i + 1 >= n:
                raise EscapeError("dangling escape byte")
            nxt = data[i + 1]
            if nxt == 0x00:
                out.append(ESC)
            elif nxt == 0x01:
                out.append(SYN)
            else:
                raise EscapeError(f"invalid escape sequence a9 {nxt:02x}")
            i += 2
        else:
            out.append(b)
            i += 1
    return bytes(out)


def escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b == ESC:
            out += b"\xa9\x00"
        elif b == SYN:
            out += b"\xa9\x01"
        else:
            out.append(b)
    return bytes(out)


def _make_crc_table() -> list[int]:
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = ((c << 1) ^ 0x9B) & 0xFF if c & 0x80 else (c << 1) & 0xFF
        table.append(c)
    return table


_CRC_TABLE = _make_crc_table()


def crc8(data: bytes) -> int:
    """eBUS CRC-8 (poly 0x9b, init 0) over the bytes exactly as given."""
    crc = 0
    for b in data:
        crc = _CRC_TABLE[crc] ^ b
    return crc


def crc_of(logical: bytes) -> int:
    """CRC over the escaped (expanded) form of unescaped bytes."""
    return crc8(escape(logical))


def is_master(addr: int) -> bool:
    """A master address has both nibbles in {0,1,3,7,f}."""
    ok = (0, 1, 3, 7, 0xF)
    return (addr >> 4) in ok and (addr & 0xF) in ok


def device_name(addr: int) -> str:
    return DEVICES.get(addr, f"addr{addr:02x}")


@dataclass
class Telegram:
    ts: object  # datetime or None
    raw: str  # hex as captured (escaped), segments concatenated
    status: str  # ok | noise | fragment | truncated | crc_error | nack | malformed
    note: str = ""
    src: int | None = None
    dst: int | None = None
    pb: int | None = None
    sb: int | None = None
    data: bytes = b""
    crc_ok: bool | None = None
    ack: int | None = None
    reply: bytes | None = None
    reply_crc_ok: bool | None = None
    reply_ack: int | None = None
    ebusd_tx: bool = False  # ebusd transmitted part of this telegram (or src is ebusd)

    @property
    def pbsb(self) -> str | None:
        if self.pb is None:
            return None
        return f"{self.pb:02x}{self.sb:02x}"

    @property
    def is_broadcast(self) -> bool:
        return self.dst == BROADCAST

    @property
    def is_slave_target(self) -> bool:
        return self.dst is not None and self.dst != BROADCAST and not is_master(self.dst)


def parse_telegram(raw_hex: str, ts=None, *, head_fragment: bool = False,
                   tail_fragment: bool = False, ebusd_tx: bool = False) -> Telegram:
    """Parse one telegram from escaped wire bytes (hex, no separators).

    Anything that does not parse is returned with a non-ok status; it is never dropped.
    """
    raw_hex = raw_hex.strip().lower()
    tg = Telegram(ts=ts, raw=raw_hex, status="ok", ebusd_tx=ebusd_tx)
    try:
        wire = bytes.fromhex(raw_hex)
    except ValueError:
        tg.status, tg.note = "malformed", "non-hex characters"
        return tg
    if head_fragment or tail_fragment:
        tg.status, tg.note = "fragment", "raw log line was cut by ebusd ('...')"
        return tg
    try:
        d = unescape(wire)
    except EscapeError as exc:
        tg.status, tg.note = "malformed", str(exc)
        return tg

    if len(d) < 6:
        tg.status, tg.note = "noise", "shorter than a minimal telegram (arbitration/idle noise)"
        return tg

    tg.src, tg.dst, tg.pb, tg.sb = d[0], d[1], d[2], d[3]
    if tg.src == 0x31:
        tg.ebusd_tx = True
    nn = d[4]
    end = 5 + nn
    if len(d) < end + 1:
        tg.status, tg.note = "truncated", f"need {end + 1} bytes, have {len(d)}"
        return tg
    tg.data = d[5:end]
    tg.crc_ok = crc_of(d[:end]) == d[end]
    if not tg.crc_ok:
        tg.status, tg.note = "crc_error", "master CRC mismatch"
        return tg
    rest = d[end + 1:]

    if tg.dst == BROADCAST:
        if rest:
            tg.status, tg.note = "malformed", f"{len(rest)} trailing bytes after broadcast"
        return tg

    if not rest:
        tg.status, tg.note = "truncated", "missing ACK"
        return tg
    tg.ack = rest[0]
    if tg.ack == NACK:
        tg.status, tg.note = "nack", "target answered NACK"
        return tg
    if tg.ack != ACK:
        tg.status, tg.note = "malformed", f"unexpected ACK byte {tg.ack:02x}"
        return tg
    rest = rest[1:]

    if is_master(tg.dst):
        if rest:
            tg.status, tg.note = "malformed", f"{len(rest)} trailing bytes after master-master telegram"
        return tg

    # master-slave: NN data CRC ACK
    if not rest:
        tg.status, tg.note = "truncated", "missing slave reply"
        return tg
    rnn = rest[0]
    rend = 1 + rnn
    if len(rest) < rend + 1:
        tg.status, tg.note = "truncated", f"slave reply needs {rend + 1} bytes, have {len(rest)}"
        return tg
    tg.reply = rest[1:rend]
    tg.reply_crc_ok = crc_of(rest[:rend]) == rest[rend]
    if not tg.reply_crc_ok:
        tg.status, tg.note = "crc_error", "slave CRC mismatch"
        return tg
    tail = rest[rend + 1:]
    if not tail:
        tg.status, tg.note = "truncated", "missing final ACK"
        return tg
    tg.reply_ack = tail[0]
    if tg.reply_ack != ACK:
        tg.status, tg.note = "nack", f"master answered {tg.reply_ack:02x} to slave reply"
        return tg
    if len(tail) > 1:
        tg.status, tg.note = "malformed", f"{len(tail) - 1} trailing bytes after final ACK"
    return tg
