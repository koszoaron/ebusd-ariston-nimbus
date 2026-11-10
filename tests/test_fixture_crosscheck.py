"""Cross-check the parser against the owner's seed parser on a real capture."""

import importlib.util
from collections import Counter
from pathlib import Path

import pytest

from ebusnimbus.payload import PayloadError, decode_readings
from ebusnimbus.rawlog import iter_files

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "idle_heat_long1.log"
SEED = ROOT / "reference" / "ebus_seed.py"


def _telegrams():
    return [t for t in iter_files([FIXTURE])]


def test_fixture_parses_cleanly():
    tgs = _telegrams()
    assert len(tgs) > 1900
    assert Counter(t.status for t in tgs) == {"ok": len(tgs)}


def test_fixture_readings_decode():
    errors = Counter()
    n = 0
    for t in _telegrams():
        try:
            n += len(decode_readings(t))
        except PayloadError as exc:
            errors[str(exc)] += 1
    assert n > 4000
    assert not errors, errors


@pytest.mark.skipif(not SEED.exists(), reason="reference/ebus_seed.py not present")
def test_matches_seed_parser():
    spec = importlib.util.spec_from_file_location("ebus_seed", SEED)
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)

    theirs = Counter()
    for t in seed.telegrams(str(FIXTURE), "fx"):
        for owner, reg, kind, val, _ in seed.observations(t):
            if kind in ("PARSEFAIL", "request"):
                continue
            theirs[(owner, reg, val)] += 1

    ours = Counter()
    for t in _telegrams():
        for r in decode_readings(t):
            if r.how == "request" or r.value is None:
                continue
            ours[(r.device, r.reg, r.value)] += 1

    assert ours == theirs
