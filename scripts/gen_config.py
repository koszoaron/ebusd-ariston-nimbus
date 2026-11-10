#!/usr/bin/env python3
"""Generate the passive ebusd config (M1): config/ariston.csv and config/_templates.csv.

Inputs, all committed: registers/registers.yaml (names, types, evidence), registers/layouts.csv
(frame layouts seen on the bus, no values) and reference/claytonn73/ (field types for registers
we have not typed ourselves). Edit the inputs, not the output.

Rules (README, "Before you enable reads and writes"):
- Passive only. Reads by other masters become `u` lines without QQ (ebusd marks them passive and
  never sends them), broadcasts become `b` lines. No `r`, `w` or poll lines. No 203a/203b.
- ebusd keys a message on PBSB plus at most 6 data bytes and matches by prefix. Reads are grouped
  by their first three register ids, broadcasts and writes by their first id. Within a group only
  the registers at positions shared by every layout of the group are decoded.
- Each register is decoded from exactly one group (the most frequent one), so HA sees one entity.
- Zones 2-7, DHW, cooling and unnamed registers are not exposed: as IGN fields inside active
  messages, and as commented-out lines when nothing in the message is active.
"""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
REG = ROOT / "registers" / "registers.yaml"
LAYOUTS = ROOT / "registers" / "layouts.csv"
POLLED = ROOT / "registers" / "polled.yaml"  # the whitelist of reads ebusd sends itself
WRITES = ROOT / "registers" / "writes.yaml"  # the whitelist of writes ebusd may send (M3)
CLAY = ROOT / "reference" / "claytonn73"
LABELS = ROOT / "registers" / "labels.yaml"
OUT = ROOT / "config"

DEV = {"13": "energymgr", "18": "energymgr", "03": "heatpump", "1e": "heatpump", "70": "sensys",
       "75": "sensys", "7f": "gateway", "84": "gateway", "f7": "mfk", "fc": "mfk"}
REG_PBSB = {"2000", "2001", "200e", "200f", "2010", "2020"}
MAX_ID_REGS = 3  # ebusd: at most 6 data bytes in a message id
# Key registers that the energy manager's broadcast rotation moves out of their main layout for minutes
# at a time (M2: buffer high 17 min, zone 1 flow target 21 min without an update on 2026-10-10). They
# are also decoded from their other safe layouts seen at least ALT_MIN_COUNT times, as extra fields
# "<name>_alt", "<name>_alt2" (diagnostic entities; ha/merged.yaml combines them). See reports/updates.md.
# M3: the zone 1 mode (c679) is answered in 200e only on request (median gap 5 min); after a write the
# energy manager first sends it in a 2020 settings broadcast or a 200f answer starting with c679
# (2026-10-14 T5: 2020 at +2 s, 200f at +8 s, 200e at +29 s). ha/climate_zone1.yaml merges them.
# M4: the zone 1 heating curve offset (6471) and fixed flow temperature (6571) came in 200e answers to
# the gateway's cloud-driven reads, which stopped with the HACS pause (2026-10-14). The energy manager
# also sends both in its 2020 settings broadcast every 30 min; ha/merged.yaml combines them.
ALT_LAYOUTS = {("energymgr", "759e"), ("energymgr", "6197"), ("heatpump", "7810"), ("energymgr", "c679"),
               ("energymgr", "6471"), ("energymgr", "6571")}
ALT_MIN_COUNT = 100
# rare frames, but each one is the freshest value
ALT_MIN_COUNT_FOR = {("energymgr", "c679"): 10, ("energymgr", "6471"): 10, ("energymgr", "6571"): 10}

# 32-bit counters split over two ids (low word first): lo -> (hi, field name, type, divisor, unit, comment)
PAIRS = {
    "52d9": ("53d9", "hp_energy_consumed", "ULG", 360, "kWh",
             "outdoor unit electrical energy, counter in 10 kJ (confirmed against the cloud)"),
    "40d8": ("41d8", "hp_compressor_starts", "ULG", None, "", "compressor starts (confirmed)"),
}
PAIR_HI = {v[0]: k for k, v in PAIRS.items()}

ONOFF = "0=off;1=on"


def width_of(rid: str) -> int | None:
    b = int(rid[:2], 16)
    if b <= 0x1F or 0xC0 <= b <= 0xDF:
        return 1
    if 0x40 <= b <= 0x7F:
        return 2
    return None


# ---------------------------------------------------------------- claytonn73 types

def _load_clay_templates() -> dict[str, list[str]]:
    t = {}
    for row in csv.reader(open(CLAY / "_templates.csv", encoding="utf-8", errors="replace")):
        if row and row[0].strip() and not row[0].startswith("#") and len(row) > 1:
            t[row[0].strip()] = [c.strip() for c in row[1:4]] + [""] * 3
    return t


def _resolve(token: str, tmpl: dict, depth: int = 0) -> tuple[str, str, str] | None:
    """Template name -> (base type, divisor/values, unit)."""
    name = token.split(":")[0]
    if name.startswith("ignore") or depth > 5:
        return None
    if name not in tmpl:
        return None
    typ, div, unit = tmpl[name][0], tmpl[name][1], tmpl[name][2]
    if ";" in typ or typ in tmpl:  # chained template: first non-ignored part
        for part in typ.split(";"):
            r = _resolve(part, tmpl, depth + 1)
            if r:
                return r
        return None
    return typ, div, unit


def clay_types() -> dict[str, tuple[str, str, str]]:
    """Register id -> (type, divisor/values, unit) from claytonn73's single-id read/broadcast lines."""
    tmpl = _load_clay_templates()
    out: dict[str, tuple[str, str, str]] = {}
    for path in sorted(CLAY.glob("*.csv")):
        if path.name == "_templates.csv":
            continue
        for row in csv.reader(open(path, encoding="utf-8", errors="replace")):
            if len(row) < 11 or row[0].strip().startswith(("#", "*", "w")):
                continue
            rid = row[7].strip().lower()
            if not re.fullmatch(r"[0-9a-f]{4}", rid) or rid in out:
                continue
            spec = row[10].strip()
            res = None
            for tok in spec.split(";"):
                res = _resolve(tok, tmpl)
                if res:
                    break
                base = tok.split(":")[0]
                if base and not base.startswith("ignore") and re.fullmatch(r"[A-Z0-9:]+", base) and not base.startswith("IGN"):
                    res = (base, row[11].strip() if len(row) > 11 else "", row[12].strip() if len(row) > 12 else "")
                    break
            if res:
                out[rid] = res
    return out


BASE_WIDTH = {"UCH": 1, "SCH": 1, "D1B": 1, "D1C": 1, "BCD": 1, "U1L": 1, "S1L": 1, "HEX:1": 1, "UIN": 2,
              "SIN": 2, "U2L": 2, "S2L": 2, "D2B": 2, "D2C": 2, "FLT": 2, "HEX:2": 2}


# ---------------------------------------------------------------- register catalogue

def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:40]


def derive_name(e: dict) -> tuple[str | None, str]:
    """(name, source). None when there is nothing better than a label of unknown meaning."""
    if e.get("name"):
        return e["name"], "db"
    zone = e.get("zone")
    for u in e.get("upstream", []):
        src, nm = u.get("source", ""), u.get("name", "")
        if src.startswith("claytonn73") and "." in nm:
            circ, base = nm.split(".", 1)
            if circ == "unknown" or base.startswith(("unknown", "broadcast_", "read_", "bundled")):
                continue
            m = re.fullmatch(r"zone(\d)", circ)
            if m and not base.startswith(f"z{m.group(1)}_"):
                base = f"z{m.group(1)}_{base}"
            elif circ in ("buffer", "dhw", "cooling") and not base.startswith(circ):
                base = f"{circ}_{base}"
            return base, "claytonn73"
    for u in e.get("upstream", []):
        if u.get("source") in ("wrongisthenewright", "hesselonline", "ysard") and u.get("name"):
            return slug(u["name"]), u["source"]
    return None, ""


# per-zone families where the zone is encoded in the first byte: second byte -> first byte of zone 1
ZONE_FIRST = {"91": 0x01, "96": 0x69, "97": 0x61, "18": 0x71}
# energy manager registers that only mirror a heat pump value read from 1e
MIRRORS = {"ca46", "cb46", "6e13", "7810", "7c4f", "7610", "7710"}
# 0/1 registers shown as on/off (values seen 0..1, labels on/off, active, request, enabled)
# Sensys registers it owns rather than echoes: room temperature, and the zone 1 override, which the
# Sensys holds and re-broadcasts every minute and ends itself (docs/protocol.md, Settings and writes)
SENSYS_OWN = {"7118", "0990", "6997"}
ONOFF_IDS = {"0120", "0220", "0f23", "0320", "0520", "0b20", "1b23", "0781", "0191", "1b45", "c200", "0c45",
             "1ef3", "1421", "0921", "0488", "1803", "1c11", "1d11", "1e11", "1f11", "0990"}
# physical units taken over from claytonn73's templates
UNITS_OK = {"°C", "K", "h", "min", "s", "W", "kW", "Hz", "l/min", "kWh", "bar", "rpm", "days"}


def zone_of(rid: str, e: dict) -> int | None:
    if e.get("zone"):
        return e["zone"]
    first, fam = int(rid[:2], 16), rid[2:]
    if fam in ZONE_FIRST and 0 <= first - ZONE_FIRST[fam] < 7:
        return first - ZONE_FIRST[fam] + 1
    return None


def category(e: dict, name: str | None, nsrc: str) -> str | None:
    """Why a register is not exposed (None = exposed)."""
    rid, dev, status = e["id"], e["device"], e.get("status", "unknown")
    if not name:
        return "unknown"
    z = zone_of(rid, e)
    if z and z >= 2 or re.match(r"z[2-7]_", name):
        return "zone 2-7"
    own = [name, e.get("description") or ""] if nsrc == "db" else \
        [name, e.get("description") or ""] + [u.get("name", "") for u in e.get("upstream", [])]
    text = " ".join(own).lower()
    if re.search(r"\bdhw|dhw_|hot.?water|storage|antilegionella|thermal_cleanse|legionella", text):
        return "dhw"
    if re.search(r"cool", text):
        return "cooling"
    if dev == "sensys" and rid not in SENSYS_OWN:
        return "copy (the Sensys echoes energy manager settings)"
    if dev == "energymgr" and rid in MIRRORS:
        return "copy (energy manager mirror of a heat pump value)"
    if rid == "d140":
        return "diagnostic"
    if dev == "gateway" and rid in ("7617", "c19a"):
        return "online weather (not used by the owner; 7617 is 0x7fff = not available)"
    if status == "confirmed":
        return None
    if status == "hypothesis" and nsrc == "db":
        return None
    return "unconfirmed"


def field_type(e: dict, clay: dict) -> tuple[str, str, str, str]:
    """(type or template, divisor/values, unit, note) for one register."""
    rid, w = e["id"], width_of(e["id"])
    if e.get("values"):
        return f"{e['device']}_{e['id']}_values", "", "", ""
    if rid in ONOFF_IDS:
        return "onoff", "", "", ""
    if rid == "c54b":
        return "UCH", "", "", "error code (number; upstream lists are for gas boilers)"
    if e.get("type") and re.fullmatch(r"[A-Z][A-Z0-9:]+", e["type"]):  # an ebusd type set in the seed notes
        return e["type"], "", e.get("unit") or "", ""
    unit = e.get("unit") or ""
    if e.get("scale") in (0.1,) or unit in ("°C", "K"):
        return ("SIN", "10", unit or "°C", "") if w == 2 else ("SCH", "", unit, "")
    if rid in clay:
        typ, div, cu = clay[rid]
        bw = BASE_WIDTH.get(typ)
        if typ in ("onoff",):
            return "onoff", "", "", ""
        if bw == w:
            if div and "=" in div:  # value list
                return typ, div, "", "type from claytonn73"
            return typ, div, unit or (cu if cu in UNITS_OK else ""), "type from claytonn73"
        if typ == "BCD" and w == 1:
            return "UCH", div if "=" in div else "", cu, "claytonn73 BCD read as UCH"
    if e.get("scale") == 1 and unit in ("h", "W", "min"):
        return ("UIN" if unit == "h" else "SIN"), "", unit, ""
    return ("UCH", "", unit, "width rule") if w == 1 else ("SIN", "", unit, "width rule")


# ---------------------------------------------------------------- layouts -> groups

def load_layouts():
    groups: dict[tuple, dict] = {}
    for r in csv.DictReader(open(LAYOUTS, encoding="utf-8")):
        if r["pbsb"] not in REG_PBSB:
            continue
        ids = r["ids"].split("+")
        if r["dst"] == "fe":
            key = ("b", r["src"], r["dst"], r["pbsb"], (ids[0],))
            dev = DEV.get(r["src"])
        elif r["pbsb"] == "2020":  # write from a master to a slave (energy manager -> heat pump)
            key = ("w", r["src"], r["dst"], r["pbsb"], (ids[0],))
            dev = DEV.get(r["dst"])
        else:
            key = ("r", "", r["dst"], r["pbsb"], tuple(ids[:MAX_ID_REGS]))
            dev = DEV.get(r["dst"])
        g = groups.setdefault(key, {"layouts": Counter(), "dev": dev})
        g["layouts"][tuple(ids)] += int(r["count"])
    for key, g in groups.items():
        g["count"] = sum(g["layouts"].values())
        g["layout"], g["safe"] = decodable(key, g["layouts"])
        g["common"] = [g["layout"][i] for i in sorted(g["safe"])]
    return groups


def _ends(kind: str, pbsb: str, ids: tuple) -> list[int]:
    """Byte offset where each register's value ends in the decoded part of the frame."""
    pos, out = (1 if pbsb == "2000" else 0) if kind == "r" else 0, []
    for i, rid in enumerate(ids):
        w = width_of(rid) or 1
        if kind == "r":
            pos += w * (3 if pbsb == "2001" else 1)
        else:
            pos += 2 + w + (1 if pbsb in ("200f", "2010") else 0) + (2 * w if pbsb == "200e" else 0)
        out.append(pos)
    return out


def decodable(key: tuple, layouts: Counter) -> tuple[tuple, set[int]]:
    """Positions of the dominant layout that ebusd can decode without ever giving a wrong value.

    Another layout of the group is harmless at position i if it has the same registers up to i (same
    offsets), or if its data is shorter than the message, so ebusd rejects the frame as a whole.
    """
    kind, _src, _dst, pbsb, _ = key
    dom = max(layouts, key=layouts.get)
    best: tuple[int, int, set[int]] = (-1, -1, set())  # (score, k, safe)
    for k in range(len(dom)):
        need = _ends(kind, pbsb, dom)[k]
        safe = set()
        for i in range(k + 1):
            if all(l[:i + 1] == dom[:i + 1] or _ends(kind, pbsb, l)[-1] < need for l in layouts if l != dom):
                safe.add(i)
        # values ebusd will deliver: frames long enough to be decoded, times the safe positions in them
        score = sum(n * sum(1 for i in safe if l[:i + 1] == dom[:i + 1])
                    for l, n in layouts.items() if _ends(kind, pbsb, l)[-1] >= need)
        if score > best[0]:
            best = (score, k, safe)
    return dom[:best[1] + 1], best[2]


def build():
    regs = {(e["device"], e["id"]): e for e in yaml.safe_load(open(REG, encoding="utf-8"))}
    labels = yaml.safe_load(open(LABELS, encoding="utf-8")) or {}

    def label_of(dev: str, rid: str, name: str, e: dict) -> str:
        lab = (labels.get(f"{dev} {rid}") or {}).get("label")
        if not lab:
            lab = name.replace("_", " ").capitalize()
        return lab.replace(",", " ")
    clay = clay_types()
    groups = load_layouts()

    # choose, per register, the most frequent group that decodes it
    best: dict[tuple[str, str], tuple] = {}
    for key, g in groups.items():
        for rid in g["common"]:
            k = (g["dev"], rid)
            if k not in best or g["count"] > groups[best[k]]["count"]:
                best[k] = key
    # a 32-bit pair must come from a group where both halves are adjacent
    for lo, (hi, *_r) in PAIRS.items():
        for dev in ("energymgr",):
            cands = [key for key, g in groups.items() if g["dev"] == dev and key[0] == "r"
                     and any(g["common"][i:i + 2] == [lo, hi] for i in range(len(g["common"]) - 1))]
            if cands:
                k = max(cands, key=lambda x: groups[x]["count"])
                best[(dev, lo)] = best[(dev, hi)] = k

    lines_active: dict[str, list[str]] = defaultdict(list)
    lines_off: dict[str, list[str]] = defaultdict(list)
    templates: dict[str, str] = {"onoff": ONOFF}
    used_names: dict[str, set] = defaultdict(set)  # message names per circuit
    report = Counter()
    alt_count: Counter = Counter()  # second-layout fields emitted per register

    def unique(circ: str, name: str) -> str:
        n, i = name, 2
        while n in used_names[circ]:
            n, i = f"{name}_{i}", i + 1
        used_names[circ].add(n)
        return n

    for key in sorted(groups, key=lambda k: (groups[k]["dev"] or "", k[3], k[4])):
        g = groups[key]
        kind, src, dst, pbsb, idkey = key
        dev = g["dev"]
        if dev is None:
            continue
        common = list(g["layout"])
        safe = g["safe"]
        assigned = [rid for i, rid in enumerate(common) if i in safe and best.get((dev, rid)) == key]
        alt = [rid for i, rid in enumerate(common) if i in safe and (dev, rid) in ALT_LAYOUTS
               and best.get((dev, rid)) != key and g["count"] >= ALT_MIN_COUNT_FOR.get((dev, rid), ALT_MIN_COUNT)]
        assigned += alt
        if not assigned:
            continue
        fields, active_names, off_reasons, tech = [], [], Counter(), []
        skip_next = False

        def fld(name, part, typ, div="", unit="", comment=""):
            fields.append([name, part, typ, div, unit, comment.replace(",", ";")])

        if kind == "r" and pbsb == "2000":
            fld("", "s", "IGN:1")  # mask byte (2001 replies have none)
        for i, rid in enumerate(common):
            w = width_of(rid) or 1
            if skip_next:
                skip_next = False
                continue
            e = regs.get((dev, rid), {"device": dev, "id": rid})
            mine = rid in assigned and i in safe
            if kind != "r" and i > 0:
                fld("", "m", "IGN:2")  # the register id inside the payload
            part = "s" if kind == "r" else "m"
            if mine and rid in PAIRS and i + 1 < len(common) and common[i + 1] == PAIRS[rid][0]:
                hi, nm, typ, div, unit, com = PAIRS[rid]
                fld(nm, part, typ, "" if div is None else str(div), unit, label_of(dev, rid, nm, e))
                tech.append(f"{nm} = {rid}+{hi}: {com}")
                active_names.append(nm)
                skip_next = True
                report["exposed"] += 2
                continue
            name, nsrc = derive_name(e)
            override = (labels.get(f"{dev} {rid}") or {}).get("name")
            if override:
                name, nsrc = override, "db"
            cat = category(e, name, nsrc) if mine else "other group"
            if mine and rid in PAIR_HI:
                cat = cat or "pair"
            if mine and cat is None:
                typ, div, unit, note = field_type(e, clay)
                unit = {"l/min": "L/min"}.get(unit, unit)  # HA's spelling
                if typ == "onoff":
                    name += "_onoff"  # mqtt-hassio.cfg turns these into binary sensors
                if typ.endswith("_values"):
                    templates[typ] = e["values"]
                lab = label_of(dev, rid, name, e)
                if rid in alt:
                    n_alt = alt_count[(dev, rid)] = alt_count[(dev, rid)] + 1
                    name += "_alt" if n_alt == 1 else f"_alt{n_alt}"
                    lab += " (alt. layout)" if n_alt == 1 else f" (alt. layout {n_alt})"
                    note = (note + "; " if note else "") + "second layout (ALT_LAYOUTS)"
                status = e.get("status", "unknown")
                fld(name, part, typ, div, unit, lab)
                tech.append(f"{name} = {rid}: {status}{'; name from ' + nsrc if nsrc not in ('db', '') else ''}"
                            f"{'; ' + note if note else ''}")
                active_names.append(name)
                report["exposed"] += 1
            else:
                fld("", part, f"IGN:{w}", "", "", "" if not mine else f"{rid} not exposed: {cat}")
                if mine:
                    off_reasons[cat] += 1
                    report[f"not exposed: {cat}"] += 1
            if kind == "b" and pbsb in ("200f", "2010"):
                fld("", "m", "IGN:1")  # flag byte
            if pbsb in ("200e", "2001"):
                fld("", "m" if pbsb == "200e" else "s", f"IGN:{2 * w}")  # min, max
        # message line
        typ = {"r": "u", "w": "u", "b": "b"}[kind]
        qq = src if kind in ("b", "w") else ""
        circ = dev
        base = [n for n in active_names if not re.search(r"_alt\d*$", n)]
        if len(active_names) == 1 or len(base) == 1:
            # one exposed register: message and field carry its name (HA names entities by field).
            # A second-layout field added to such a message keeps the name, so the HA unique id
            # (topic + field) of the existing entity does not change
            msg_name = unique(circ, (base or active_names)[0])
        else:
            msg_name = unique(circ, f"grp_{active_names[0]}" if active_names else f"grp_{'_'.join(idkey)}")
        idhex = "".join(idkey)
        what = {"r": "read of", "w": "write", "b": "broadcast"}[kind]
        others = len(g["layouts"]) - 1
        comment = f"{pbsb} {what} {'+'.join(common)}; seen {g['count']}x" + (
            f"; {others} other layout(s) with this id are not decoded" if others else "")
        msg_comment = fields_label = ""
        named = [f for f in fields if f[0]]
        msg_comment = named[0][5] if len(named) == 1 else comment.split(";")[0]
        cols = [typ, circ, msg_name, msg_comment, qq, dst, pbsb, idhex]
        for f in fields:
            cols += f
        line = ",".join(str(c) for c in cols)
        info = f"# {comment}" + "".join(f"\n#   {t}" for t in tech)
        if active_names:
            lines_active[circ].append(info + "\n" + line)
        else:
            why = ", ".join(sorted(off_reasons)) or "nothing exposed"
            lines_off[why].append(f"# {comment}\n#" + line)
    return lines_active, lines_off, templates, report


HEADER = """\
# Ariston Nimbus (Bridgenet) ebusd configuration, passive part.
# GENERATED by scripts/gen_config.py from registers/registers.yaml and registers/layouts.csv.
# Do not edit by hand. Evidence and status for each register: registers/registers.yaml.
#
# Passive only: `u` lines decode reads other masters make, `b` lines broadcasts. ebusd never sends
# any of them. Each field comment ends with [register id; status; ...].
# Never poll 203a/203b and keep filter-seen = 1 in mqtt-hassio.cfg: another Bridgenet user's boiler
# crashed and rebooted after a full address scan that polling 203b triggered.
#
# type,circuit,name,comment,QQ,ZZ,PBSB,ID,field,part,type,divider/values,unit,comment
"""


POLLED_HEADER = """\
# Ariston Nimbus (Bridgenet) ebusd configuration, reads ebusd sends itself.
# GENERATED by scripts/gen_config.py from registers/polled.yaml, the owner-approved whitelist.
# Do not edit by hand. Without this file ebusd sends nothing. With it, and without --readonly,
# ebusd reads these lines when asked over MQTT (lines "r") or polls them itself (lines "r1".."r9").
#
# type,circuit,name,comment,QQ,ZZ,PBSB,ID,field,part,type,divider/values,unit,comment
"""


def build_polled(templates: dict[str, str]) -> list[str]:
    """One `r` line per whitelist entry: a 2000 read of its registers from the target. Fields of
    type "values" use the register's value list from registers.yaml (added to ``templates``)."""
    regs = {(e["device"], e["id"]): e for e in yaml.safe_load(open(REG, encoding="utf-8"))}
    out = []
    for m in yaml.safe_load(open(POLLED, encoding="utf-8")):
        circ = DEV[m["target"]]
        ids = [str(f["id"]) for f in m["fields"]]
        assert 1 <= len(ids) <= MAX_ID_REGS, m["name"]
        cols = [f"r{m['priority'] or ''}", circ, m["name"], ", ".join(f["label"] for f in m["fields"]).replace(",", ";"),
                "", m["target"], "2000", "".join(ids)]
        cols += ["", "s", "IGN:1", "", "", ""]  # mask byte
        for f in m["fields"]:
            w = width_of(str(f["id"]))
            typ = f["type"]
            if typ == "values":
                e = regs[(circ, str(f["id"]))]
                typ = f"{circ}_{f['id']}_values"
                templates[typ] = e["values"]
            assert typ in ("onoff", "UCH") or typ.endswith("_values") if w == 1 else typ in ("SIN", "UIN"), \
                (m["name"], f["id"], typ)
            cols += [f["name"], "s", typ, f.get("divisor", ""), f.get("unit", ""), f["label"].replace(",", ";")]
        how = f"polled with priority {m['priority']}" if m["priority"] else "read on request only (MQTT get)"
        out.append(f"# read of {'+'.join(ids)} from {m['target']} ({circ}), {how}\n"
                   + ",".join(str(c) for c in cols))
    return out


WRITES_HEADER = """\
# Ariston Nimbus (Bridgenet) ebusd configuration, writes ebusd may send.
# GENERATED by scripts/gen_config.py from registers/writes.yaml, the owner-approved whitelist.
# Do not edit by hand. Each line is a broadcast (ZZ fe) that copies what the Light Gateway or the
# Sensys sends for the same action. ebusd sends one only on an MQTT ".../set" request, and every
# field is a value list or a constant, so values outside the list are refused before sending.
#
# type,circuit,name,comment,QQ,ZZ,PBSB,ID,field,part,type,divider/values,unit,comment
"""


def _values(f: dict) -> str:
    """ebusd values column of a write field: "=c" for a constant, else "raw=name;..."."""
    if "const" in f:
        return f"={f['const']}"
    if "values" in f:
        return ";".join(f"{k}={v}" for k, v in f["values"].items())
    r = f["range"]
    div, n = r.get("divisor", 1), round((r["to"] - r["from"]) / r["step"])
    out = []
    for i in range(n + 1):
        v = r["from"] + i * r["step"]
        out.append(f"{round(v * div)}={v:.1f}" if div != 1 else f"{round(v)}={round(v)}")
    return ";".join(out)


def build_writes() -> list[str]:
    """One `w` line per whitelist entry: a broadcast from ebusd (QQ empty = its own address)."""
    out = []
    for m in yaml.safe_load(open(WRITES, encoding="utf-8")):
        assert m["pbsb"] in ("2020", "2073"), m["name"]
        cols = ["w", "energymgr", m["name"], m["label"].replace(",", ";"), "", "fe", m["pbsb"], str(m["id"])]
        for f in m["fields"]:
            assert f["type"] in ("UCH", "UIN") and sum(k in f for k in ("const", "values", "range")) == 1, f
            cols += [f["name"], "m", f["type"], _values(f), "", ""]
        out.append(f"# {m['approved']}: {m['source']}\n" + ",".join(cols))
    return out


def write():
    active, off, templates, report = build()
    OUT.mkdir(exist_ok=True)
    out = [HEADER]
    for circ in ("energymgr", "heatpump", "sensys", "gateway", "mfk"):
        if active.get(circ):
            out.append(f"\n# ===== {circ} =====")
            out += active[circ]
    out.append("\n# ===== Not exposed (zones 2-7, DHW, cooling, unknown meaning). Uncomment to use. =====")
    for why in sorted(off):
        out.append(f"\n# --- {why} ---")
        out += off[why]
    (OUT / "ariston.csv").write_text("\n".join(out) + "\n", encoding="utf-8")
    polled = build_polled(templates)
    (OUT / "polled.csv").write_text(POLLED_HEADER + "\n".join(polled) + "\n", encoding="utf-8")
    writes = build_writes()
    (OUT / "writes.csv").write_text(WRITES_HEADER + "\n".join(writes) + "\n", encoding="utf-8")
    t = ["# GENERATED by scripts/gen_config.py. Value lists of enum fields (sources in registers/registers.yaml).",
         "# name,type,divider/values,unit,comment", "onoff,UCH,0=off;1=on,,"]
    for name, vals in sorted(templates.items()):
        if name == "onoff":
            continue
        t.append(f"{name},UCH,{vals},,")
    (OUT / "_templates.csv").write_text("\n".join(t) + "\n", encoding="utf-8")
    print(", ".join(f"{k}: {v}" for k, v in sorted(report.items())))
    print("lines:", sum(len(v) for v in active.values()), "active,", sum(len(v) for v in off.values()), "commented,",
          len(polled), "polled reads,", len(writes), "writes")


if __name__ == "__main__":
    write()
