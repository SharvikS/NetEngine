"""
Core host scanning engine.

Scanning pipeline for each IP:
  1. Ping (subprocess) → alive / dead + latency + TTL
  2. If alive: reverse-DNS hostname lookup
  3. If alive: MAC from ARP cache + vendor lookup
  4. If alive: TCP port scan on selected ports

ScanController is a QThread that orchestrates all of this with a
ThreadPoolExecutor and reports progress / results via Qt signals.
"""

from __future__ import annotations

import platform
import re
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Callable

from PyQt6.QtCore import QThread, pyqtSignal

from .network import get_ip_range, get_arp_cache, resolve_hostname
from .port_scanner import scan_ports
from .fingerprint import lookup_vendor, os_hint_from_ttl
from .service_mapper import DEFAULT_SCAN_PORTS


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class HostInfo:
    ip: str
    status: str = "unknown"       # "alive" | "dead" | "unknown"
    hostname: str = ""
    mac: str = ""
    vendor: str = ""
    latency_ms: float = -1.0
    ttl: int = -1
    os_hint: str = ""
    open_ports: list[int] = field(default_factory=list)
    scanned_at: Optional[datetime] = None

    # ── Derived helpers ───────────────────────────────────────────────────────

    @property
    def is_alive(self) -> bool:
        return self.status == "alive"

    @property
    def latency_display(self) -> str:
        if self.latency_ms < 0:
            return "—"
        if self.latency_ms < 1:
            return f"{self.latency_ms * 1000:.0f} μs"
        return f"{self.latency_ms:.1f} ms"

    @property
    def ports_display(self) -> str:
        if not self.open_ports:
            return "—"
        from scanner.service_mapper import _SERVICES
        parts = []
        for p in sorted(self.open_ports):
            svc = _SERVICES.get(p)
            parts.append(f"{p}/{svc}" if svc else str(p))
        return ", ".join(parts)

    def to_dict(self) -> dict:
        return {
            "ip": self.ip,
            "status": self.status,
            "hostname": self.hostname,
            "mac": self.mac,
            "vendor": self.vendor,
            "latency_ms": round(self.latency_ms, 2),
            "ttl": self.ttl,
            "os_hint": self.os_hint,
            "open_ports": sorted(self.open_ports),
            "scanned_at": self.scanned_at.isoformat() if self.scanned_at else "",
        }


# ── Scan configuration ────────────────────────────────────────────────────────

@dataclass
class ScanConfig:
    network: str = "192.168.1.0"
    cidr: int = 24
    ports: list[int] = field(default_factory=lambda: DEFAULT_SCAN_PORTS.copy())
    ping_timeout: float = 1.0       # seconds
    port_timeout: float = 0.5       # seconds
    max_host_workers: int = 100     # concurrent host pings
    max_port_workers: int = 30      # concurrent port checks per host
    scan_ports_flag: bool = True    # whether to port-scan alive hosts
    resolve_hostnames: bool = True  # reverse DNS
    ping_count: int = 1


# ── Low-level ping ────────────────────────────────────────────────────────────

_IS_WINDOWS = platform.system() == "Windows"

_TTL_RE_WIN = re.compile(r"TTL=(\d+)", re.IGNORECASE)
_TTL_RE_UNIX = re.compile(r"ttl=(\d+)", re.IGNORECASE)
_TIME_RE_WIN = re.compile(r"(?:time|Zeit)[=<](\d+(?:\.\d+)?)\s*ms", re.IGNORECASE)
_TIME_RE_UNIX = re.compile(r"time=(\d+(?:\.\d+)?)\s*ms", re.IGNORECASE)


def _ping_host(ip: str, timeout: float = 1.0) -> tuple[bool, float, int]:
    """
    Ping a host using the OS ping command.
    Returns (alive, latency_ms, ttl).
    """
    try:
        if _IS_WINDOWS:
            cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), ip]
        else:
            cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout))), ip]

        t_start = time.perf_counter()
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 2,
            creationflags=0x08000000 if _IS_WINDOWS else 0,
        )
        elapsed = (time.perf_counter() - t_start) * 1000  # ms

        output = result.stdout + result.stderr
        alive = result.returncode == 0

        if not alive:
            return False, -1.0, -1

        # Extract TTL
        ttl = -1
        ttl_re = _TTL_RE_WIN if _IS_WINDOWS else _TTL_RE_UNIX
        m = ttl_re.search(output)
        if m:
            ttl = int(m.group(1))

        # Extract latency reported by ping
        latency = elapsed
        time_re = _TIME_RE_WIN if _IS_WINDOWS else _TIME_RE_UNIX
        m2 = time_re.search(output)
        if m2:
            latency = float(m2.group(1))

        return True, latency, ttl

    except (subprocess.TimeoutExpired, OSError):
        return False, -1.0, -1


# ── Per-host scan task ────────────────────────────────────────────────────────

def scan_single_host(
    ip: str,
    config: ScanConfig,
    arp_cache: dict[str, str],
    stop_event: threading.Event,
) -> HostInfo:
    """Full scan pipeline for a single host."""
    info = HostInfo(ip=ip, scanned_at=datetime.now())

    if stop_event.is_set():
        return info

    # 1. Ping
    alive, latency, ttl = _ping_host(ip, config.ping_timeout)
    info.status = "alive" if alive else "dead"
    info.latency_ms = latency
    info.ttl = ttl
    if ttl > 0:
        info.os_hint = os_hint_from_ttl(ttl)

    if not alive:
        return info

    if stop_event.is_set():
        return info

    # 2. Hostname
    if config.resolve_hostnames:
        info.hostname = resolve_hostname(ip, timeout=1.0)

    # 3. MAC address from ARP cache (populated automatically after ping)
    mac = arp_cache.get(ip, "")
    if not mac:
        # Re-query ARP cache — the ping may have just added the entry
        fresh = get_arp_cache()
        mac = fresh.get(ip, "")
    info.mac = mac
    if mac:
        info.vendor = lookup_vendor(mac)

    if stop_event.is_set():
        return info

    # 4. Port scan
    if config.scan_ports_flag and config.ports:
        info.open_ports = scan_ports(
            ip,
            config.ports,
            max_workers=config.max_port_workers,
            timeout=config.port_timeout,
            stop_event=stop_event,
        )

    return info


# ── Scan controller (QThread) ─────────────────────────────────────────────────

class ScanController(QThread):
    """
    Runs the full subnet scan on a background thread.
    Emits Qt signals to update the GUI.
    """

    # Emitted when a host result is ready (new or updated)
    host_result = pyqtSignal(object)          # HostInfo

    # Emitted periodically with (scanned_count, total_count)
    progress = pyqtSignal(int, int)

    # Emitted when the scan finishes (naturally or by stop)
    finished_scan = pyqtSignal(float)         # elapsed seconds

    # Emitted if something goes wrong
    error = pyqtSignal(str)

    def __init__(self, config: ScanConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()   # not paused initially

    # ── Control ───────────────────────────────────────────────────────────────

    def stop(self):
        self._stop_event.set()
        self._pause_event.set()   # unblock if paused

    def pause(self):
        self._pause_event.clear()

    def resume(self):
        self._pause_event.set()

    @property
    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

    # ── Main scan loop ────────────────────────────────────────────────────────

    def run(self):
        t_start = time.perf_counter()
        try:
            self._run_scan()
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            elapsed = time.perf_counter() - t_start
            self.finished_scan.emit(elapsed)

    def _run_scan(self):
        ip_list = get_ip_range(self.config.network, self.config.cidr)
        if not ip_list:
            self.error.emit(f"No hosts in range {self.config.network}/{self.config.cidr}")
            return

        total = len(ip_list)
        scanned = 0

        # Pre-populate ARP cache (best effort)
        arp_cache = get_arp_cache()

        max_w = min(self.config.max_host_workers, total)

        with ThreadPoolExecutor(max_workers=max_w) as pool:
            # Submit all hosts
            future_to_ip: dict[Future, str] = {
                pool.submit(
                    scan_single_host, ip, self.config, arp_cache, self._stop_event
                ): ip
                for ip in ip_list
            }

            for future in as_completed(future_to_ip):
                # Respect pause
                self._pause_event.wait()

                if self._stop_event.is_set():
                    for f in future_to_ip:
                        f.cancel()
                    break

                try:
                    info: HostInfo = future.result()
                    self.host_result.emit(info)
                except Exception as exc:
                    ip = future_to_ip[future]
                    self.host_result.emit(HostInfo(ip=ip, status="unknown"))

                scanned += 1
                self.progress.emit(scanned, total)
