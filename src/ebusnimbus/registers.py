"""Register database loader (registers/registers.yaml), keyed by (device, id)."""

from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "registers" / "registers.yaml"
STATUSES = ("confirmed", "hypothesis", "unknown")


class RegisterDB:
    def __init__(self, entries: list[dict]):
        self.entries = entries
        self.by_key: dict[tuple[str, str], dict] = {}
        for e in entries:
            key = (e["device"], str(e["id"]).lower())
            if key in self.by_key:
                raise ValueError(f"duplicate register {key}")
            if e["status"] not in STATUSES:
                raise ValueError(f"{key}: bad status {e['status']!r}")
            self.by_key[key] = e

    @classmethod
    def load(cls, path: Path | str = DEFAULT_PATH) -> "RegisterDB":
        return cls(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or [])

    def get(self, device: str, reg_id: str) -> dict | None:
        return self.by_key.get((device, reg_id.lower()))

    def named(self, device: str, reg_id: str) -> bool:
        e = self.get(device, reg_id)
        return bool(e and e.get("name"))

    def status(self, device: str, reg_id: str) -> str:
        e = self.get(device, reg_id)
        return e["status"] if e else "unknown"
