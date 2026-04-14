"""Export scan results to CSV or JSON."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from scanner.host_scanner import HostInfo


def export_hosts(hosts: list[HostInfo], path: Path, fmt: str = "csv"):
    """
    Write host list to file in the given format.
    fmt must be 'csv' or 'json'.
    """
    fmt = fmt.lower().strip()
    if fmt == "csv":
        _to_csv(hosts, path)
    elif fmt == "json":
        _to_json(hosts, path)
    else:
        raise ValueError(f"Unsupported export format: {fmt!r}")


def _to_csv(hosts: list[HostInfo], path: Path):
    fieldnames = [
        "ip", "status", "hostname", "mac", "vendor",
        "latency_ms", "ttl", "os_hint", "open_ports", "scanned_at",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for h in hosts:
            row = h.to_dict()
            row["open_ports"] = ", ".join(str(p) for p in row["open_ports"])
            writer.writerow(row)


def _to_json(hosts: list[HostInfo], path: Path):
    data = [h.to_dict() for h in hosts]
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
