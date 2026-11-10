import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from ebusnimbus import sidechannel as sc


class Handler(BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        Handler.seen.append(self.path)
        body = json.dumps({"id": 0, "a_act_power": 100.5, "b_act_power": 200.0, "c_act_power": 300.25,
                           "total_act_power": 600.75, "errors": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_shelly_poll_and_csv(tmp_path):
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        src = sc.ShellyEM("meter", {"url": f"http://127.0.0.1:{srv.server_port}", "id": 0,
                                    "metrics": ["total_act_power", "missing"]})
        assert src.poll() == [("total_act_power", 600.75)]
        assert Handler.seen[-1] == "/rpc/EM.GetStatus?id=0"
        w = sc.SideWriter(tmp_path)
        from datetime import datetime, timedelta, timezone
        w.write(datetime(2026, 4, 9, 10, 0, 0, 500000, tzinfo=timezone(timedelta(hours=2))),
                "meter", "total_act_power", 600.75)
        w.close()
        assert (tmp_path / "2026-04-09.side.csv").read_text() == (
            "ts,source,metric,value\n2026-04-09 10:00:00.500+02:00,meter,total_act_power,600.75\n")
    finally:
        srv.shutdown()


def test_unreachable_meter_is_recorded_as_error(tmp_path):
    import time
    src = sc.ShellyEM("meter", {"url": "http://127.0.0.1:1", "timeout": 0.2, "interval": 0.05})
    w = sc.SideWriter(tmp_path)
    stop = threading.Event()
    t = threading.Thread(target=sc.poll_loop, args=(src, w, stop))
    t.start()
    time.sleep(0.3)
    stop.set()
    t.join()
    w.close()
    rows = next(tmp_path.glob("*.side.csv")).read_text().splitlines()[1:]
    assert rows and all(r.split(",")[2] == "_error" for r in rows)


def test_unknown_source_type_rejected():
    import pytest
    with pytest.raises(SystemExit):
        sc.build_sources({"sources": [{"type": "nope"}]})
