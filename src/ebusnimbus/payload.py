"""Register-level decoding of 20xx telegrams (docs/protocol.md, commands and register ids)."""

from __future__ import annotations

from dataclasses import dataclass

from .telegram import BROADCAST, Telegram, device_name

NA_VALUE = 0x7FFF

# PBSB codes documented in docs/protocol.md; used by the census to list never-seen codes.
KNOWN_PBSB: dict[str, str] = {
    "2000": "read values",
    "2001": "read value with limits",
    "200e": "broadcast answer to 2001 request",
    "200f": "value broadcast",
    "2010": "value broadcast",
    "2020": "settings broadcast / write",
    "2031": "serial number broadcast",
    "2038": "bus control",
    "203a": "bus scan request",
    "203b": "bus scan reply",
    "2050": "timer program",
    "2051": "timer program",
    "2070": "date and time",
    "2071": "unknown",
    "2073": "unknown (counter 1-6)",
    "2002": "error / version query",
    "2004": "error broadcast",
}


def width_of(reg_id: bytes) -> int | None:
    """Value width from the first ID byte; None when the class was never seen."""
    b = reg_id[0]
    if b <= 0x1F or 0xC0 <= b <= 0xDF:
        return 1
    if 0x60 <= b <= 0x7F:
        return 2
    if 0x40 <= b <= 0x5F:
        return 2  # seen in menu captures only; untested in broadcasts
    return None


def width_status(reg_id: bytes) -> str:
    b = reg_id[0]
    if b <= 0x1F or 0xC0 <= b <= 0xDF or 0x60 <= b <= 0x7F:
        return "verified"
    if 0x40 <= b <= 0x5F:
        return "untested"
    return "unknown"


def decode_value(raw: bytes) -> int:
    """1 byte unsigned, 2 bytes little-endian signed."""
    if len(raw) == 1:
        return raw[0]
    return int.from_bytes(raw, "little", signed=True)


@dataclass
class Reading:
    device: str  # who owns the register (the answering slave, or the broadcasting master)
    reg: str  # register ID, wire order, hex
    how: str  # read | read_na | read_limits | bc_200f | bc_2010 | bc_200e | bc_2020 | write_2020 | request
    value: int | None  # None for requests and for read_na (the slave reported "not available")
    raw: bytes = b""
    vmin: int | None = None
    vmax: int | None = None
    flag: int | None = None
    part: str = ""  # where the value bytes are: "m" master data, "s" slave reply
    offset: int = -1  # position of the value in that part


class PayloadError(ValueError):
    """The telegram has a known PBSB but its payload does not fit the documented layout."""


def _split_id(b: bytes, pos: int) -> bytes:
    if pos + 2 > len(b):
        raise PayloadError(f"truncated register id at offset {pos}")
    return b[pos:pos + 2]


def _need_width(reg: bytes) -> int:
    w = width_of(reg)
    if w is None:
        raise PayloadError(f"register {reg.hex()}: value width class unknown")
    return w


def _take(b: bytes, pos: int, w: int, reg: bytes) -> bytes:
    if pos + w > len(b):
        raise PayloadError(f"register {reg.hex()}: value runs past end of payload")
    return b[pos:pos + w]


def _entries(payload: bytes, *, flag: bool) -> list[tuple[bytes, bytes, int | None, int]]:
    """(id, value bytes, flag byte, value offset) for each register in a list payload."""
    out = []
    pos = 0
    while pos < len(payload):
        reg = _split_id(payload, pos)
        pos += 2
        w = _need_width(reg)
        val = _take(payload, pos, w, reg)
        off = pos
        pos += w
        fl = None
        if flag:
            if pos >= len(payload):
                raise PayloadError(f"register {reg.hex()}: missing flag byte")
            fl = payload[pos]
            pos += 1
        out.append((reg, val, fl, off))
    return out


def _mask_values(dev: str, ids: list[bytes], mask: int, rep: bytes) -> list[Reading]:
    """2000 reply as a per-register bitmap: bit i set means a value for ids[i] follows."""
    if mask >> len(ids):
        raise PayloadError(f"2000 reply mask {mask:02x} has bits beyond {len(ids)} ids")
    out, pos = [], 1
    for i, reg in enumerate(ids):
        if not mask >> i & 1:
            out.append(Reading(dev, reg.hex(), "read_na", None))
            continue
        w = _need_width(reg)
        raw = _take(rep, pos, w, reg)
        out.append(Reading(dev, reg.hex(), "read", decode_value(raw), raw, part="s", offset=pos))
        pos += w
    if pos != len(rep):
        raise PayloadError(f"2000 reply mask {mask:02x}: {len(rep) - pos} unused bytes")
    return out


def decode_readings(tg: Telegram) -> list[Reading]:
    """Decode the register values carried by a parsed telegram.

    Returns [] for PBSB codes that carry no register values. Raises PayloadError when
    the layout does not fit, so the caller can record the telegram as unparsed.
    """
    if tg.status != "ok" or tg.pbsb is None:
        return []
    pbsb = tg.pbsb
    d = tg.data
    src, dst = device_name(tg.src), device_name(tg.dst)

    if pbsb == "2000":
        if len(d) % 2:
            raise PayloadError("2000 request is not a list of 2-byte ids")
        ids = [d[i:i + 2] for i in range(0, len(d), 2)]
        if tg.dst == BROADCAST:
            return [Reading(src, r.hex(), "request", None) for r in ids]
        if tg.reply is None:
            raise PayloadError("2000 without slave reply")
        rep = tg.reply
        if not rep:
            raise PayloadError("2000 with empty reply")
        mask = rep[0]
        full = (1 << len(ids)) - 1
        if mask == 0:
            if len(rep) != 1:
                raise PayloadError(f"2000 reply mask 00 followed by {len(rep) - 1} bytes")
            return [Reading(dst, reg.hex(), "read_na", None) for reg in ids]
        out = _mask_values(dst, ids, mask, rep)
        if mask != full:
            # hypothesis: bit i set = value of ids[i] present. Never seen yet, so the telegram
            # stays unparsed until one turns up and the layout can be checked.
            present = "+".join(r.reg for r in out if r.how == "read")
            raise PayloadError(f"2000 partial reply mask {mask:02x} of {full:02x} (fits bitmap hypothesis: {present})")
        return out

    if pbsb == "2001":
        if len(d) != 2:
            raise PayloadError("2001 request is not a single 2-byte id")
        reg = d
        if tg.dst == BROADCAST:
            return [Reading(src, reg.hex(), "request", None)]
        if tg.reply is None:
            raise PayloadError("2001 without slave reply")
        w = _need_width(reg)
        rep = tg.reply
        if len(rep) != 3 * w:
            raise PayloadError(f"2001 reply is {len(rep)} bytes, expected {3 * w}")
        return [Reading(dst, reg.hex(), "read_limits", decode_value(rep[:w]), rep[:w],
                        decode_value(rep[w:2 * w]), decode_value(rep[2 * w:]), part="s", offset=0)]

    if pbsb == "200e":
        reg = _split_id(d, 0)
        w = _need_width(reg)
        if len(d) != 2 + 3 * w:
            raise PayloadError(f"200e payload is {len(d)} bytes, expected {2 + 3 * w}")
        body = d[2:]
        return [Reading(src, reg.hex(), "bc_200e", decode_value(body[:w]), body[:w],
                        decode_value(body[w:2 * w]), decode_value(body[2 * w:]), part="m", offset=2)]

    if pbsb in ("200f", "2010"):
        return [Reading(src, r.hex(), "bc_" + pbsb, decode_value(v), v, flag=fl, part="m", offset=o)
                for r, v, fl, o in _entries(d, flag=True)]

    if pbsb == "2020":
        if tg.dst == BROADCAST:
            return [Reading(src, r.hex(), "bc_2020", decode_value(v), v, part="m", offset=o)
                    for r, v, _, o in _entries(d, flag=False)]
        # to a slave: write. The reply is empty.
        if tg.reply:
            raise PayloadError("2020 write with non-empty reply")
        return [Reading(dst, r.hex(), "write_2020", decode_value(v), v, part="m", offset=o)
                for r, v, _, o in _entries(d, flag=False)]

    return []


# 2004 error codes as shown on the Sensys. Only codes with evidence from this system go here.
# 0x69: the only code on the bus (hourly from the energy manager) and the only entry in the
# Sensys error history ("411 - Room sensor not available"), owner 2026-10-08. Upstream:
# wrongisthenewright agrees (105=411); ysard's 105=410 does not apply here.
ERROR_CODES: dict[int, str] = {0x69: "411 room sensor not available"}


@dataclass
class ErrorBroadcast:
    """A 2004 broadcast. Layout per ysard: error code, then zone status; this bus sends a third
    byte as well (``69 01 00``). Names come from ERROR_CODES (confirmed codes only)."""

    device: str
    code: int
    zone_status: int | None
    rest: bytes


def decode_error(tg: Telegram) -> ErrorBroadcast | None:
    """Decode a 2004 error broadcast; None for any other telegram."""
    if tg.status != "ok" or tg.pbsb != "2004":
        return None
    d = tg.data
    if tg.dst != BROADCAST:
        raise PayloadError("2004 not sent to broadcast")
    if not d:
        raise PayloadError("2004 without payload")
    return ErrorBroadcast(device_name(tg.src), d[0], d[1] if len(d) > 1 else None, d[2:])


@dataclass
class SensysClock:
    """2070 broadcast from the Sensys (about every 20 s): plain binary, weekday 1 = Monday."""

    minute: int
    hour: int
    day: int
    month: int
    weekday: int
    year: int
    extra: int  # constant 0x11 so far; to watch around the DST change


def decode_clock(tg: Telegram) -> SensysClock | None:
    if tg.status != "ok" or tg.pbsb != "2070":
        return None
    d = tg.data
    if len(d) != 7:
        raise PayloadError(f"2070 payload is {len(d)} bytes, expected 7")
    return SensysClock(d[0], d[1], d[2], d[3], d[4], 2000 + d[5], d[6])


@dataclass
class TimeProgramHalfDay:
    """2051 broadcast from the Sensys: one channel's program for half a day.

    Byte 0 = day (0-6) for 00:00-12:00, 16 + day for 12:00-24:00; byte 1 = channel; then 12 bytes,
    each holding four 2-bit levels (first slot in the high bits), one per quarter hour."""

    channel: int
    day: int
    afternoon: bool
    levels: list[int]  # 48 quarter-hour levels: 1 = comfort, 0 = reduced (owner, zone 1)


def decode_time_program(tg: Telegram) -> TimeProgramHalfDay | None:
    if tg.status != "ok" or tg.pbsb != "2051":
        return None
    d = tg.data
    if len(d) != 14:
        raise PayloadError(f"2051 payload is {len(d)} bytes, expected 14")
    levels = [(b >> sh) & 3 for b in d[2:] for sh in (6, 4, 2, 0)]
    return TimeProgramHalfDay(d[1], d[0] & 0x0F, bool(d[0] & 0x10), levels)
