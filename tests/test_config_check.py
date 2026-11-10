import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EBUSD = os.environ.get("EBUSD") or shutil.which("ebusd")


@pytest.mark.skipif(not EBUSD, reason="ebusd binary not available (set EBUSD)")
def test_ebusd_checkconfig_passes():
    res = subprocess.run([str(ROOT / "scripts" / "check_config.sh")], capture_output=True, text=True,
                         env={**os.environ, "EBUSD": EBUSD})
    assert res.returncode == 0, res.stdout[-2000:] + res.stderr[-2000:]


def test_baseline_has_no_active_write_messages():
    import csv
    for r in csv.reader(open(ROOT / "config" / "ariston.csv", encoding="utf-8")):
        assert not (r and r[0] == "w"), r[:4]


PRESENT = {"03", "13", "18", "1e", "70", "75", "7f", "84", "f7", "fc", "fe"}


def _active_rows():
    import csv
    for r in csv.reader(open(ROOT / "config" / "ariston.csv", encoding="utf-8")):
        if len(r) >= 8 and r[0].strip() and not r[0].startswith(("#", "*")):
            yield r


def test_baseline_is_passive():
    for r in _active_rows():
        t, qq, zz, pbsb = r[0].strip().lower(), r[4].strip().lower(), r[5].strip().lower(), r[6].strip().lower()
        assert not t.startswith("w"), r[:4]
        assert t not in {f"r{i}" for i in range(1, 10)}, f"polling priority: {r[:4]}"
        assert pbsb not in ("203a", "203b"), f"bus scan message: {r[:4]}"
        assert not qq or qq in PRESENT, f"sender not on this bus: {r[:5]}"
        assert not zz or zz in PRESENT, f"target not on this bus: {r[:6]}"


@pytest.mark.skipif(not EBUSD, reason="ebusd binary not available (set EBUSD)")
@pytest.mark.skipif(not (ROOT / "config-baseline").is_dir() or not (ROOT / "reference" / "SOURCES.md").exists(),
                    reason="needs config-baseline/ and the upstream copies in reference/ (not published)")
def test_width_rule_conflicts_are_only_the_known_unseen_ones():
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    os.environ["EBUSD"] = EBUSD
    import build_config
    build_config.EBUSD = EBUSD
    regs = {c[3] for c in build_config.width_conflicts()}
    # never seen on this bus (2026-10-07/08 capture); docs/protocol.md, open questions
    assert regs <= {"762e", "782d", "7547"}, regs


def _all_rows():
    import csv
    for r in csv.reader(open(ROOT / "config" / "ariston.csv", encoding="utf-8")):
        if r and r[0].strip() and not r[0].startswith(("#", "*")):
            yield r


def test_generated_config_is_passive_only():
    for r in _all_rows():
        assert r[0] in ("u", "b"), f"ariston.csv holds passive u/b lines only (reads: polled.csv): {r[:4]}"
        assert len(r[7]) <= 12, f"message id longer than ebusd allows: {r[:8]}"


def test_mqtt_filters_stay_safe():
    text = (ROOT / "config" / "mqtt-hassio.cfg").read_text(encoding="utf-8")
    active = [l.strip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    assert "filter-seen = 1" in active  # never poll 203a/203b
    # passive messages and the whitelisted polled reads (tests/test_polled.py); never writes, and
    # "u" anchored: ebusd treats an excluded write message as direction "uw" (M3)
    assert "filter-direction = r|^u$" in active


def test_generated_config_is_up_to_date(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("gen_config", ROOT / "scripts" / "gen_config.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    monkeypatch.setattr(gen, "OUT", tmp_path)
    gen.write()
    for name in ("ariston.csv", "_templates.csv", "polled.csv"):
        assert (tmp_path / name).read_text() == (ROOT / "config" / name).read_text(), f"regenerate config/{name}"
