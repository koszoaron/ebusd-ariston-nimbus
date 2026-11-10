import itertools
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ebusbuild import broadcast
from ebusnimbus import capture
from ebusnimbus.rawlog import iter_events, normalise_line
from ebusnimbus.telegram import Telegram

WIRE = broadcast(0x13, "200f", bytes.fromhex("c94b0200"))
TZ = ZoneInfo("Europe/Berlin")
CEST = timezone(timedelta(hours=2))


def writer(path, now=datetime(2026, 4, 10, 12, 0, tzinfo=CEST), **kw):
    return capture.DailyWriter(path, tz=TZ, now=lambda: now, **kw)


def raw(ts, body=None, prefix=""):
    return f"{ts} {prefix}<{body or WIRE}"


def test_normalise_raw_ebusd_line_and_ha_prefix():
    a = normalise_line(raw("2026-04-09 23:29:17.477"))
    b = normalise_line(raw("2026-04-09 23:29:17.477", prefix="[bus notice] "))
    assert a.hex == b.hex == WIRE and a.ts == datetime(2026, 4, 9, 23, 29, 17, 477000)
    assert not a.tx and not a.head_fragment


def test_normalise_fragments_and_tx_and_junk():
    f = normalise_line("2026-04-09 23:29:17.477 ...<7f18...")
    assert f.head_fragment and f.tail_fragment
    t = normalise_line("2026-04-09 23:29:17.477 <10>31<aa")
    assert t.tx and t.hex == "1031aa"
    assert normalise_line("2026-04-09 23:29:17.477 [main notice] ebusd started") is None
    assert normalise_line("") is None


def test_events_never_drop_lines():
    lines = [raw("2026-04-09 23:29:17.477"), "# 2026-04-09 23:29:18.000 gap 5s", "garbage",
             "2026-04-09 23:29:19.000 <10", "2026-04-09 23:29:20.000 ...<7f18..."]
    ev = list(iter_events(lines))
    assert [type(e).__name__ for e in ev] == ["Telegram", "Marker", "BadLine", "Telegram", "Telegram"]
    assert [e.status for e in ev if isinstance(e, Telegram)] == ["ok", "noise", "fragment"]


def test_writer_dedupe_gap_and_day_rotation(tmp_path):
    w = writer(tmp_path, gap_seconds=30)
    assert w.write(raw("2026-04-09 23:59:58.000"))
    assert not w.write(raw("2026-04-09 23:59:58.000"))  # duplicate
    assert w.write(raw("2026-04-10 00:00:01.000"))  # next day -> new file, small gap
    assert w.write(raw("2026-04-10 00:05:00.000"))  # gap marker
    w.close()
    d1 = (tmp_path / "2026-04-09.log").read_text().splitlines()
    d2 = (tmp_path / "2026-04-10.log").read_text().splitlines()
    assert len(d1) == 1 and d1[0].startswith("2026-04-09 23:59:58.000+02:00 <")
    assert len(d2) == 3 and d2[1].startswith("# 2026-04-10 00:05:00.000+02:00 gap 299.0s")


def test_writer_same_timestamp_different_content_is_kept(tmp_path):
    other = broadcast(0x13, "200f", bytes.fromhex("c94b0000"))
    w = writer(tmp_path)
    assert w.write(raw("2026-04-09 10:00:00.000"))
    assert w.write(raw("2026-04-09 10:00:00.000", other))
    assert not w.write(raw("2026-04-09 10:00:00.000"))
    w.close()
    w2 = writer(tmp_path)  # restart: both lines at the last timestamp are known
    assert not w2.write(raw("2026-04-09 10:00:00.000", other))
    assert not w2.write(raw("2026-04-09 10:00:00.000"))
    w2.close()
    assert len((tmp_path / "2026-04-09.log").read_text().splitlines()) == 2


def test_writer_restart_does_not_duplicate(tmp_path):
    w = writer(tmp_path)
    w.write(raw("2026-04-09 10:00:00.000"))
    w.write(raw("2026-04-09 10:00:01.000"))
    w.close()
    w2 = writer(tmp_path)
    assert w2.last_ts == datetime(2026, 4, 9, 10, 0, 1, tzinfo=CEST)
    assert not w2.write(raw("2026-04-09 10:00:01.000"))
    assert w2.write(raw("2026-04-09 10:00:02.000"))
    w2.close()
    lines = [l for l in (tmp_path / "2026-04-09.log").read_text().splitlines() if not l.startswith("#")]
    assert len(lines) == 3


def test_writer_resumes_after_old_format_capture(tmp_path):
    """Captures before 2026-10-08 have no offset; the writer continues after them."""
    (tmp_path / "2026-04-09.log").write_text(raw("2026-04-09 10:00:01.000") + "\n")
    w = writer(tmp_path)
    assert w.last_ts == datetime(2026, 4, 9, 10, 0, 1, tzinfo=CEST)
    assert not w.write(raw("2026-04-09 10:00:01.000"))
    assert w.write(raw("2026-04-09 10:00:02.000"))
    w.close()
    assert (tmp_path / "2026-04-09.log").read_text().splitlines()[-1].startswith("2026-04-09 10:00:02.000+02:00 <")


def _take(gen, n):
    return [x for x in itertools.islice((l for l in gen if l is not None), n)]


def test_filetail_follows_rotation_and_truncation(tmp_path):
    path = tmp_path / "ebusd.log"
    state = tmp_path / "state.json"
    path.write_text("a\nb\n")
    tail = capture.FileTail(path, state, poll=0.01)
    gen = tail.lines()
    assert _take(gen, 2) == ["a", "b"]
    # rotate: rename to .old, new file with new inode
    with open(path, "a") as fh:
        fh.write("c\n")
    os.rename(path, str(path) + ".old")
    path.write_text("dddd\n")
    assert _take(gen, 2) == ["c", "dddd"]
    # truncate in place (ebusd restart opens with "w")
    path.write_text("e\n")
    # shorter than what was already read, so truncation is seen and announced
    assert _take(gen, 2) == ["#SOURCE-RESTART truncated", "e"]


def test_filetail_partial_line_waits_for_newline(tmp_path):
    path = tmp_path / "ebusd.log"
    path.write_text("abc")
    tail = capture.FileTail(path, tmp_path / "s.json", poll=0.01)
    gen = tail.lines()

    def finish():
        import time
        time.sleep(0.1)
        with open(path, "a") as fh:
            fh.write("def\n")

    threading.Thread(target=finish).start()
    assert _take(gen, 1) == ["abcdef"]


def test_filetail_inflight_line_is_redelivered_not_lost(tmp_path):
    path = tmp_path / "ebusd.log"
    path.write_text("a\nb\n")
    t1 = capture.FileTail(path, tmp_path / "s.json", poll=0.01)
    assert _take(t1.lines(), 1) == ["a"]
    t1.save_state()  # "a" was handed out but may not have been written yet
    assert _take(capture.FileTail(path, tmp_path / "s.json", poll=0.01).lines(), 2) == ["a", "b"]


def test_filetail_resume_after_restart_and_rotation(tmp_path):
    path = tmp_path / "ebusd.log"
    state = tmp_path / "s.json"
    path.write_text("a\nb\n")
    t1 = capture.FileTail(path, state, poll=0.01)
    g1 = t1.lines()
    got = []
    for line in g1:
        if line is None:  # idle tick: everything delivered has been consumed
            break
        got.append(line)
    assert got == ["a", "b"]
    t1.save_state()
    # we are down; ebusd appends then rotates
    with open(path, "a") as fh:
        fh.write("c\n")
    os.rename(path, str(path) + ".old")
    path.write_text("d\n")
    t2 = capture.FileTail(path, state, poll=0.01)
    assert _take(t2.lines(), 2) == ["c", "d"]


def test_run_end_to_end_with_unparsed_marker(tmp_path):
    w = capture.DailyWriter(tmp_path)
    src = iter([raw("2026-04-09 10:00:00.000"), "something odd", None,
                raw("2026-04-09 10:00:01.000", prefix="[bus notice] ")])
    capture.run(src, w, strict=True)
    text = (tmp_path / "2026-04-09.log").read_text()
    assert "unparsed source line: something odd" in text
    assert text.count(" <" + WIRE) == 2


# ---- end of daylight saving: 2026-10-25 03:00 CEST -> 02:00 CET, 02:00-02:59 local happens twice ----

def _fold_source(step=10):
    """Naive local timestamps as ebusd writes them, 01:50 to 03:10 local, every ``step`` s."""
    out = []
    t = datetime(2026, 10, 25, 1, 50)
    while t < datetime(2026, 10, 25, 3, 0):  # 01:50 .. 02:59:50, first pass
        out.append(t)
        t += timedelta(seconds=step)
    t = datetime(2026, 10, 25, 2, 0)
    while t < datetime(2026, 10, 25, 3, 10):  # 02:00 .. 03:09:50, after the change
        out.append(t)
        t += timedelta(seconds=step)
    return [raw(x.strftime("%Y-%m-%d %H:%M:%S.000")) for x in out]


FOLD_LINES = 6 * (10 + 60 + 60 + 10)
AFTER = datetime(2026, 10, 25, 3, 15, tzinfo=timezone(timedelta(hours=1)))


def _written(tmp_path):
    return [l for l in (tmp_path / "2026-10-25.log").read_text().splitlines() if not l.startswith("#")]


def test_fold_is_written_with_offsets_and_nothing_dropped(tmp_path):
    w = writer(tmp_path, now=AFTER)
    capture.run(iter(_fold_source()), w, strict=True)
    lines = _written(tmp_path)
    assert len(lines) == FOLD_LINES and w.skipped_dupes == 0
    assert lines[0].startswith("2026-10-25 01:50:00.000+02:00")
    assert lines[6 * 10].startswith("2026-10-25 02:00:00.000+02:00")
    assert lines[6 * 70 - 1].startswith("2026-10-25 02:59:50.000+02:00")
    assert lines[6 * 70].startswith("2026-10-25 02:00:00.000+01:00")
    assert lines[-1].startswith("2026-10-25 03:09:50.000+01:00")
    assert "gap" not in (tmp_path / "2026-10-25.log").read_text()


def test_fold_census_counts_the_repeated_hour(tmp_path):
    from ebusnimbus.census import run as census
    from ebusnimbus.registers import RegisterDB
    w = writer(tmp_path, now=AFTER)
    capture.run(iter(_fold_source()), w, strict=True)
    c = census([tmp_path / "2026-10-25.log"], RegisterDB.load())
    assert c.total == FOLD_LINES and not c.gaps
    assert c.covered == timedelta(seconds=10 * (FOLD_LINES - 1))
    assert (c.last - c.first) == timedelta(hours=2, minutes=20) - timedelta(seconds=10)


def test_fold_in_old_naive_capture_is_resolved_on_read():
    ev = [e for e in iter_events(_fold_source(), tz=TZ) if isinstance(e, Telegram)]
    ts = [e.ts for e in ev]
    assert all(b - a == timedelta(seconds=10) for a, b in zip(ts, ts[1:]))


def test_fold_restart_and_replay_in_second_pass(tmp_path):
    """Capture restarts at 02:20 (second pass); the follower replays ebusd's log from the start."""
    src = _fold_source()
    cut = 6 * 70 + 6 * 20  # 02:20:00 second pass
    now = datetime(2026, 10, 25, 2, 21, tzinfo=timezone(timedelta(hours=1)))
    capture.run(iter(src[:cut]), writer(tmp_path, now=now), strict=True)
    capture.run(iter(src), writer(tmp_path, now=now), strict=True)
    lines = _written(tmp_path)
    assert len(lines) == FOLD_LINES and len(set(lines)) == FOLD_LINES


def test_fold_follower_reconnects_and_replays(tmp_path):
    """ssh drops during the second pass; the reconnected tail replays the whole raw log."""
    src = _fold_source()
    cut = 6 * 70 + 6 * 40
    stream = src[:cut] + ["#SOURCE-RESTART exit 255"] + src
    w = writer(tmp_path, now=AFTER)
    capture.run(iter(stream), w, strict=True)
    lines = _written(tmp_path)
    assert len(lines) == FOLD_LINES and len(set(lines)) == FOLD_LINES
    assert w.skipped_dupes == cut
    assert "source command exited (255)" in (tmp_path / "2026-10-25.log").read_text()


def test_clock_without_history_uses_wall_clock():
    from ebusnimbus.localtime import LocalClock
    cet = timezone(timedelta(hours=1))
    first = LocalClock(TZ, now=lambda: datetime(2026, 10, 25, 2, 40, tzinfo=CEST))
    assert first.resolve(datetime(2026, 10, 25, 2, 30)).utcoffset() == timedelta(hours=2)
    second = LocalClock(TZ, now=lambda: datetime(2026, 10, 25, 2, 40, tzinfo=cet))
    assert second.resolve(datetime(2026, 10, 25, 2, 30)).utcoffset() == timedelta(hours=1)
