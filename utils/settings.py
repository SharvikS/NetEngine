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
    "glass_opacity": 88,
    "og_accent": "Blue",
    "ssh_hosts": [],          # list of {name, host, port, user, key_path}
    "serial_hosts": [],       # list of saved Serial/UART profile dicts
    "last_interface": "",
    "last_port_preset": 0,
    "terminal_shell": "",     # PowerShell / CMD / WSL / Bash
    "api_requests": [],       # list of saved REST requests
    "ip_profiles": [],        # list of {name, ip, mask, gateway, dns}
    # File Transfer → external editor preference. One of the codes
    # defined in utils.editor_launcher (auto / notepadpp / notepad /
    # vscode / system / custom). Default "auto" resolves to the
    # Notepad++ → Notepad → system-default chain, matching the
    # user's preferred workflow.
    "preferred_editor": "auto",
    "custom_editor_path": "",
    # File Transfer → local pane collapse state. Persisted so the
    # user's last choice survives app restarts.
    "file_transfer_local_collapsed": False,
    # About page — index of the last tagline shown. Lets the rotating
    # line advance across restarts instead of always starting at 0.
    "about_tagline_index": -1,
    # AI chat history sync folder. When non-empty, chat sessions are
    # read/written here instead of the local ~/.netscope/chats/ dir.
    # Point to a Dropbox/OneDrive folder to sync history across machines.
    "chat_sync_dir": "",
}


def settings_file_path() -> str:
    """Absolute path to the on-disk settings file."""
    return str(_SETTINGS_FILE)


def settings_dir_path() -> str:
    """Absolute path to the settings directory (~/.netscope)."""
    return str(_SETTINGS_DIR)


def reset_all() -> None:
    """Overwrite the settings file with built-in defaults."""
    _save(dict(_DEFAULTS))


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


# ── Serial / UART hosts ──────────────────────────────────────────────────────


def get_serial_hosts() -> list[dict]:
    return list(get("serial_hosts", []))


def save_serial_host(entry: dict) -> None:
    """Add or replace a saved Serial/UART profile by name."""
    hosts = get_serial_hosts()
    name = (entry.get("name") or "").strip()
    if not name:
        return
    # Always tag the kind so generic loaders can route the entry.
    entry = dict(entry)
    entry["kind"] = "serial"
    entry["name"] = name
    hosts = [h for h in hosts if h.get("name") != name]
    hosts.append(entry)
    hosts.sort(key=lambda h: h.get("name", "").lower())
    set_value("serial_hosts", hosts)


def delete_serial_host(name: str) -> None:
    hosts = [h for h in get_serial_hosts() if h.get("name") != name]
    set_value("serial_hosts", hosts)


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
