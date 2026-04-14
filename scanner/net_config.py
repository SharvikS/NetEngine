"""
Ethernet adapter IPv4 configuration helper (Windows-only).

Wraps `netsh interface ipv4 …` so we can list adapters, read their current
configuration, and switch them between DHCP and static addressing.

All mutating commands require Administrator privileges; the helper will
return CalledProcessError so the GUI can present a clear permissions error.
"""

from __future__ import annotations

import ctypes
import platform
import re
import subprocess
from dataclasses import dataclass, field
from typing import Optional

import psutil

_IS_WINDOWS = platform.system() == "Windows"
_NO_WINDOW = 0x08000000 if _IS_WINDOWS else 0


def is_windows() -> bool:
    return _IS_WINDOWS


def is_admin() -> bool:
    """Best-effort administrator check (Windows)."""
    if not _IS_WINDOWS:
        try:
            import os
            return os.geteuid() == 0           # type: ignore[attr-defined]
        except Exception:
            return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ── Data ──────────────────────────────────────────────────────────────────────

@dataclass
class AdapterConfig:
    name: str
    is_up: bool = False
    dhcp_enabled: bool = False
    ip: str = ""
    mask: str = ""
    gateway: str = ""
    dns_servers: list[str] = field(default_factory=list)
    raw: str = ""


# ── Adapter listing ───────────────────────────────────────────────────────────

def list_ethernet_adapters() -> list[str]:
    """
    Return a list of physical-looking adapter friendly-names.
    Filters out loopback / virtual interfaces by name keywords.
    """
    skip_keywords = (
        "loopback", "isatap", "teredo", "pseudo", "vethernet", "wsl",
        "wireguard", "openvpn", "tailscale", "bluetooth",
    )
    names: list[str] = []
    for name, stats in psutil.net_if_stats().items():
        low = name.lower()
        if any(kw in low for kw in skip_keywords):
            continue
        names.append(name)
    return sorted(names)


# ── netsh helpers ─────────────────────────────────────────────────────────────

def _run_netsh(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    if not _IS_WINDOWS:
        raise RuntimeError("Network adapter configuration requires Windows.")
    cmd = ["netsh", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=15,
        check=check,
        creationflags=_NO_WINDOW,
    )


# ── Read configuration ───────────────────────────────────────────────────────

_RE_IP   = re.compile(r"IP Address[^\d]*(\d+\.\d+\.\d+\.\d+)", re.IGNORECASE)
_RE_MASK = re.compile(r"Subnet Prefix[^/]*/\s*(\d+)\s*\(mask\s*(\d+\.\d+\.\d+\.\d+)\)", re.IGNORECASE)
_RE_GW   = re.compile(r"Default Gateway[^\d]*(\d+\.\d+\.\d+\.\d+)", re.IGNORECASE)
_RE_DHCP = re.compile(r"DHCP enabled[^A-Za-z]*([A-Za-z]+)", re.IGNORECASE)


def get_adapter_config(name: str) -> AdapterConfig:
    """Return the current IPv4 configuration for an adapter."""
    if not _IS_WINDOWS:
        return AdapterConfig(name=name, raw="(not supported on this OS)")

    cfg = AdapterConfig(name=name)
    try:
        result = _run_netsh(
            ["interface", "ipv4", "show", "config", f'name={name}'],
            check=False,
        )
        cfg.raw = result.stdout or result.stderr or ""
    except Exception as exc:
        cfg.raw = f"netsh failed: {exc}"
        return cfg

    text = cfg.raw

    m = _RE_DHCP.search(text)
    if m:
        cfg.dhcp_enabled = m.group(1).strip().lower() == "yes"

    m = _RE_IP.search(text)
    if m:
        cfg.ip = m.group(1)

    m = _RE_MASK.search(text)
    if m:
        cfg.mask = m.group(2)

    m = _RE_GW.search(text)
    if m:
        cfg.gateway = m.group(1)

    # DNS servers — there can be several lines
    cfg.dns_servers = []
    in_dns = False
    for line in text.splitlines():
        if "DNS servers" in line or "Statically Configured DNS" in line:
            in_dns = True
            mm = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
            if mm:
                cfg.dns_servers.append(mm.group(1))
            continue
        if in_dns:
            mm = re.match(r"\s+(\d+\.\d+\.\d+\.\d+)\s*$", line)
            if mm:
                cfg.dns_servers.append(mm.group(1))
            else:
                in_dns = False

    # Live status from psutil
    try:
        stats = psutil.net_if_stats().get(name)
        if stats:
            cfg.is_up = stats.isup
    except Exception:
        pass

    return cfg


# ── Apply configuration ──────────────────────────────────────────────────────

def set_dhcp(name: str) -> None:
    """Switch the adapter to DHCP for IP and DNS."""
    _run_netsh(["interface", "ipv4", "set", "address", f'name={name}', "source=dhcp"])
    _run_netsh(["interface", "ipv4", "set", "dnsservers", f'name={name}', "source=dhcp"])


def set_static(
    name: str,
    ip: str,
    mask: str,
    gateway: str = "",
    dns_primary: str = "",
    dns_secondary: str = "",
) -> None:
    """Set a static IPv4 configuration on the adapter."""
    args = [
        "interface", "ipv4", "set", "address",
        f'name={name}', "source=static",
        f"address={ip}", f"mask={mask}",
    ]
    if gateway:
        args.append(f"gateway={gateway}")
        args.append("gwmetric=1")
    _run_netsh(args)

    if dns_primary:
        _run_netsh([
            "interface", "ipv4", "set", "dnsservers",
            f'name={name}', "source=static", f"address={dns_primary}",
            "register=primary",
        ])
        if dns_secondary:
            _run_netsh([
                "interface", "ipv4", "add", "dnsservers",
                f'name={name}', f"address={dns_secondary}", "index=2",
            ])
    else:
        _run_netsh([
            "interface", "ipv4", "set", "dnsservers",
            f'name={name}', "source=dhcp",
        ])
