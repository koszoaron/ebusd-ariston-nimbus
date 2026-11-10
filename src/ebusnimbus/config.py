"""Local deployment configuration (TOML). Addresses, paths and topics never live in git."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

DEFAULT_PATH = "config.local.toml"


def load(path: str | os.PathLike | None = None) -> dict:
    p = Path(path or os.environ.get("EBUS_CONFIG") or DEFAULT_PATH)
    if not p.exists():
        raise SystemExit(f"config file {p} not found; copy config.example.toml to {DEFAULT_PATH} and edit it")
    with open(p, "rb") as fh:
        return tomllib.load(fh)
