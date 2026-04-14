"""
Persistent application settings stored under ~/.netscope/settings.json.

Handles:
  - Active theme name
  - Saved SSH host shortcuts
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_SETTINGS_DIR = Path.home() / ".netscope"
_SETTINGS_FILE = _SETTINGS_DIR / "settings.json"

_DEFAULTS: dict[str, Any] = {
    "theme": "Dark",
    "ssh_hosts": [],          # list of {name, host, port, user, key_path}
    "last_interface": "",
    "last_port_preset": 0,
    "terminal_shell": "",     # PowerShell / CMD / WSL / Bash
    "api_requests": [],       # list of saved REST requests
    "ip_profiles": [],        # list of {name, ip, mask, gateway, dns}
}


def _load() -> dict:
    if not _SETTINGS_FILE.exists():
        return dict(_DEFAULTS)
    try:
        data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
        merged = dict(_DEFAULTS)
        merged.update(data)
        return merged
    except Exception:
        return dict(_DEFAULTS)


def _save(data: dict) -> None:
    try:
        _SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        _SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def get(key: str, default: Any = None) -> Any:
    return _load().get(key, default if default is not None else _DEFAULTS.get(key))


def set_value(key: str, value: Any) -> None:
    data = _load()
    data[key] = value
    _save(data)


def get_ssh_hosts() -> list[dict]:
    return list(get("ssh_hosts", []))


def save_ssh_host(entry: dict) -> None:
    """Add or replace an SSH host entry by name."""
    hosts = get_ssh_hosts()
    name = entry.get("name", "")
    if not name:
        return
    hosts = [h for h in hosts if h.get("name") != name]
    hosts.append(entry)
    hosts.sort(key=lambda h: h.get("name", "").lower())
    set_value("ssh_hosts", hosts)


def delete_ssh_host(name: str) -> None:
    hosts = [h for h in get_ssh_hosts() if h.get("name") != name]
    set_value("ssh_hosts", hosts)


# ── IP profiles (network adapter presets) ────────────────────────────────────


def get_ip_profiles() -> list[dict]:
    return list(get("ip_profiles", []))


def save_ip_profile(entry: dict) -> None:
    """Add or replace a saved adapter IP profile by name."""
    profiles = get_ip_profiles()
    name = entry.get("name", "")
    if not name:
        return
    profiles = [p for p in profiles if p.get("name") != name]
    profiles.append(entry)
    profiles.sort(key=lambda p: p.get("name", "").lower())
    set_value("ip_profiles", profiles)


def delete_ip_profile(name: str) -> None:
    profiles = [p for p in get_ip_profiles() if p.get("name") != name]
    set_value("ip_profiles", profiles)
