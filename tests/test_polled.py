"""What ebusd may send is exactly the owner-approved whitelists: reads (registers/polled.yaml, M2)
and writes (registers/writes.yaml, M3)."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"


def active_lines():
    for p in sorted(CONFIG.glob("*.csv")):
        if p.name.startswith("_"):
            continue  # templates
        for line in p.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#") and not line.startswith("*"):
                cols = line.split(",")
                yield p.name, cols[0].split("]")[-1], cols  # type without a [condition] prefix


def test_write_lines_equal_whitelist():
    writes = {(f, t, c[1], c[2], c[5], c[6], c[7]) for f, t, c in active_lines() if t.startswith("w")}
    wl = yaml.safe_load(open(ROOT / "registers" / "writes.yaml", encoding="utf-8"))
    want = {("writes.csv", "w", "energymgr", m["name"], "fe", m["pbsb"], str(m["id"])) for m in wl}
    assert writes == want


def test_write_lines_are_broadcasts_with_closed_fields():
    """Only 2020/2073 broadcasts; every field a value list or a constant, so ebusd refuses any other
    value before sending (no free numeric field)."""
    for f, t, c in active_lines():
        if not t.startswith("w"):
            continue
        assert c[4] == "" and c[5] == "fe" and c[6] in ("2020", "2073"), c[:8]
        fields = [c[i:i + 6] for i in range(8, len(c), 6)]
        assert fields, c[:8]
        for name, part, typ, values, *_ in fields:
            assert part == "m" and typ in ("UCH", "UIN"), (c[2], name)
            assert values.startswith("=") or all("=" in v for v in values.split(";")), (c[2], name)


def test_read_lines_equal_whitelist():
    # the type column carries the poll priority ("r" = read on request only, "r1".."r9" = polled)
    reads = {(t, c[1], c[2], c[5], c[6], c[7]) for f, t, c in active_lines() if t.startswith("r")}
    wl = yaml.safe_load(open(ROOT / "registers" / "polled.yaml", encoding="utf-8"))
    want = {(f"r{m['priority'] or ''}", "heatpump" if m["target"] == "1e" else "energymgr", m["name"],
             m["target"], "2000", "".join(str(f["id"]) for f in m["fields"])) for m in wl}
    assert reads == want


def test_reads_only_from_polled_csv_and_only_2000():
    for f, t, c in active_lines():
        if t.startswith("r"):
            assert f == "polled.csv" and c[6] == "2000", (f, c[:8])
        else:
            assert t in ("u", "b") or (t == "w" and f == "writes.csv"), (f, c[:3])  # the rest is passive


def test_never_203a_203b():
    for f, t, c in active_lines():
        assert c[6] not in ("203a", "203b"), (f, c[:8])
