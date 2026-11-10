"""A fake eBUS line for a private, writable ebusd (testing only; never touches the real bus).

ebusd connects to it as a plain TCP device (``--device=127.0.0.1:<port>``). Like a real bus, the
line echoes every byte a master writes, so ebusd wins arbitration and sees its own telegram, and
it sends SYN (``aa``) when nobody has written for a while. Everything ebusd writes is recorded, so a
test can check byte for byte what would have gone onto the real bus (README, "Before you enable reads and writes").

``echo=False`` imitates the adapter after the 2026-10-20 power outage: it takes what ebusd writes
but never puts it on the bus, so every send fails.
"""

from __future__ import annotations

import socket
import threading
import time

SYN = 0xAA


class FakeBus:
    def __init__(self, syn_interval: float = 0.04, quiet_after_rx: float = 0.1, echo: bool = True):
        self.echo = echo
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.written = bytearray()  # every byte ebusd wrote, in order
        self.syn_interval = syn_interval
        self.quiet_after_rx = quiet_after_rx
        self._last_rx = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        self.srv.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except OSError:
                continue
            threading.Thread(target=self._syn, args=(conn,), daemon=True).start()
            try:
                with conn:
                    while not self._stop.is_set():
                        data = conn.recv(256)
                        if not data:
                            break
                        with self._lock:
                            self.written += data
                            if self.echo:
                                self._last_rx = time.monotonic()
                                conn.sendall(data)  # the bus echoes what a master sends
            except OSError:
                pass

    def _syn(self, conn: socket.socket):
        while not self._stop.is_set():
            time.sleep(self.syn_interval)
            with self._lock:
                if time.monotonic() - self._last_rx < self.quiet_after_rx:
                    continue  # a telegram is in progress
                try:
                    conn.sendall(bytes([SYN]))
                except OSError:
                    return

    def telegrams(self) -> list[str]:
        """What ebusd wrote, split at SYN, as hex strings (escaped, as on the wire)."""
        with self._lock:
            parts = bytes(self.written).split(bytes([SYN]))
        return [p.hex() for p in parts if p]

    def close(self):
        self._stop.set()
        self.srv.close()
