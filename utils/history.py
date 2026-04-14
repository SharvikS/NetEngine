"""
Scan history — persists each completed scan to a JSON file in
~/.netscope/history/.  Keeps the last N scans.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from scanner.host_scanner import HostInfo

_HISTORY_DIR = Path.home() / ".netscope" / "history"
_MAX_RECORDS = 20


def _ensure_dir():
    _HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def save_scan(hosts: list[HostInfo], network: str, cidr: int, elapsed: float):
    """Persist a completed scan result."""
    _ensure_dir()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = _HISTORY_DIR / f"scan_{ts}.json"
    record = {
        "timestamp": datetime.now().isoformat(),
        "network": f"{network}/{cidr}",
        "elapsed_s": round(elapsed, 2),
        "host_count": len(hosts),
        "alive_count": sum(1 for h in hosts if h.is_alive),
        "hosts": [h.to_dict() for h in hosts],
    }
    fname.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    _prune()


def load_history() -> list[dict]:
    """Return list of scan records, newest first."""
    _ensure_dir()
    files = sorted(_HISTORY_DIR.glob("scan_*.json"), reverse=True)
    records = []
    for f in files:
        try:
            records.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return records


def _prune():
    """Delete oldest history files beyond _MAX_RECORDS."""
    files = sorted(_HISTORY_DIR.glob("scan_*.json"), reverse=True)
    for old in files[_MAX_RECORDS:]:
        try:
            old.unlink()
        except Exception:
            pass
