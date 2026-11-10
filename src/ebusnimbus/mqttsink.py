"""A minimal MQTT 3.1.1 broker on localhost that records what a client publishes (testing only).

Used to check what a private ebusd would send to Home Assistant (discovery topics, payloads)
without a real broker. It accepts CONNECT, SUBSCRIBE, PUBLISH (QoS 0/1), PINGREQ and DISCONNECT;
it forwards nothing between clients. ``publish`` sends a message to every client that subscribed
(as HA would publish a ``.../set`` request to ebusd); topic filters are not matched.
"""

from __future__ import annotations

import socket
import threading


def _read_exact(conn: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError
        buf += chunk
    return buf


def _read_len(conn: socket.socket) -> int:
    mult, val = 1, 0
    while True:
        b = _read_exact(conn, 1)[0]
        val += (b & 0x7F) * mult
        if not b & 0x80:
            return val
        mult *= 128


class MqttSink:
    def __init__(self):
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.messages: dict[str, bytes] = {}  # last payload per topic
        self.count = 0
        self._subscribers: list[socket.socket] = []
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        self.srv.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except OSError:
                continue
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn: socket.socket):
        try:
            with conn:
                while not self._stop.is_set():
                    head = _read_exact(conn, 1)[0]
                    body = _read_exact(conn, _read_len(conn))
                    kind, flags = head >> 4, head & 0x0F
                    if kind == 1:  # CONNECT
                        conn.sendall(b"\x20\x02\x00\x00")
                    elif kind == 3:  # PUBLISH
                        tl = int.from_bytes(body[:2], "big")
                        topic = body[2:2 + tl].decode("utf-8", "replace")
                        pos = 2 + tl
                        qos = (flags >> 1) & 3
                        if qos:
                            pid = body[pos:pos + 2]
                            pos += 2
                            conn.sendall(b"\x40\x02" + pid)  # PUBACK
                        self.messages[topic] = body[pos:]
                        self.count += 1
                    elif kind == 8:  # SUBSCRIBE
                        pid, n = body[:2], 0
                        pos = 2
                        while pos < len(body):
                            tl = int.from_bytes(body[pos:pos + 2], "big")
                            pos += 2 + tl + 1
                            n += 1
                        with self._send_lock:
                            conn.sendall(bytes([0x90, 2 + n]) + pid + b"\x00" * n)
                            if conn not in self._subscribers:  # one SUBSCRIBE per topic filter
                                self._subscribers.append(conn)
                    elif kind == 12:  # PINGREQ
                        conn.sendall(b"\xd0\x00")
                    elif kind == 14:  # DISCONNECT
                        return
        except (ConnectionError, OSError):
            return

    @property
    def subscribed(self) -> bool:
        return bool(self._subscribers)

    def publish(self, topic: str, payload: bytes | str = b"") -> int:
        """QoS 0 PUBLISH to every subscribed client; returns how many got it."""
        if isinstance(payload, str):
            payload = payload.encode()
        t = topic.encode()
        body = len(t).to_bytes(2, "big") + t + payload
        n, rem = 0, len(body)
        head = bytearray([0x30])
        while True:
            b, rem = rem % 128, rem // 128
            head.append(b | (0x80 if rem else 0))
            if not rem:
                break
        with self._send_lock:
            for conn in list(self._subscribers):
                try:
                    conn.sendall(bytes(head) + body)
                    n += 1
                except OSError:
                    self._subscribers.remove(conn)
        return n

    def close(self):
        self._stop.set()
        self.srv.close()
