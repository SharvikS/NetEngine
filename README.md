# NetScope

A modern desktop network IP scanner with an embedded terminal, SSH/SCP
client and Windows network-adapter configurator. Built with Python +
PyQt6 — pure Python, no native extensions.

```
+-------------------+--------------------------------------------------+
| WORKSPACE         |  NETSCOPE   Network IP Scanner                   |
| > Scanner         |  ----------------------------------------------- |
|   Terminal        |  TOTAL SCANNED   ALIVE   OFFLINE   OPEN PORTS    |
|   SSH / SCP       |       254          37       217        82       |
|   Adapter         |  ----------------------------------------------- |
|                   |  IP Address  Hostname    MAC ...   Status        |
| THEME             |  192.168...  router      ...       ALIVE         |
| [ Dark        v ] |  ...                                             |
+-------------------+--------------------------------------------------+
```

## Features

- **Subnet scanner** — concurrent ping sweep, reverse-DNS, ARP/MAC vendor
  lookup, OS hint from TTL, configurable TCP port scan, sortable + filterable
  results, CSV/JSON export, persistent scan history.
- **Theme system** — four built-in themes (`Dark`, `Light`, `Cyber`,
  `High Contrast`) selectable from the sidebar or `View → Theme`. Themes
  switch instantly and the choice is persisted in `~/.netscope/settings.json`.
- **Embedded terminal** — full in-app terminal panel running a real shell
  (`cmd.exe` on Windows, `/bin/sh` on Linux). Supports command history,
  built-in `cd`/`clear`/`cls`, `Ctrl+C` to abort a running command, and
  `Ctrl+L` to clear the screen.
- **SSH client** — connect to a host with password or key authentication
  (paramiko under the hood). Sessions run inside the embedded terminal,
  not in an external window. Saved hosts persist across launches.
- **SCP / SFTP transfer** — upload/download files using the same connection
  profile, with live progress and cancel support.
- **Network adapter configuration (Windows)** — view current IPv4 settings
  and switch between DHCP and static (address / mask / gateway / DNS) using
  `netsh`. Requires Administrator to apply changes.
- **Production-grade UI** — no emoji icons, theme-driven QSS, splitter
  layouts, status bar, keyboard shortcuts, About dialog.

## Project layout

```
NetScope/
├── main.py                    Entry point
├── run.bat                    Launcher (Windows)
├── run-admin.bat              Launcher with elevation
├── requirements.txt
├── gui/
│   ├── themes.py              Theme palette + ThemeManager + QSS builder
│   ├── main_window.py         Sidebar + stacked pages
│   ├── dialogs.py             PortScan, Export, About dialogs
│   └── components/
│       ├── sidebar.py
│       ├── scanner_view.py
│       ├── scan_toolbar.py
│       ├── host_table.py
│       ├── detail_panel.py
│       ├── terminal_view.py
│       ├── terminal_widget.py     Embedded terminal control
│       ├── ssh_view.py
│       ├── scp_panel.py
│       └── network_config_view.py
├── scanner/
│   ├── network.py             Interface enumeration, ARP, IP range
│   ├── host_scanner.py        Ping/DNS/MAC pipeline + ScanController QThread
│   ├── port_scanner.py        TCP connect scanner
│   ├── service_mapper.py      Port → service name + presets
│   ├── fingerprint.py         OUI vendor table + OS hint from TTL
│   ├── ssh_client.py          paramiko-based SSH session + SCP transfer
│   └── net_config.py          Windows netsh adapter helper
└── utils/
    ├── export.py              CSV / JSON export
    ├── history.py             Scan history persistence
    └── settings.py            Theme + saved SSH host persistence
```

## Setup

Requires **Python 3.10+**.

```bash
python -m pip install -r requirements.txt
python main.py
```

Or on Windows just double-click `run.bat`.
For network-adapter changes, launch with `run-admin.bat` (UAC prompt).

### Dependencies

| Package   | Why                                                       |
|-----------|-----------------------------------------------------------|
| PyQt6     | Desktop UI                                                |
| psutil    | Network interface enumeration                             |
| paramiko  | SSH and SCP/SFTP transport                                |

## OS-specific notes

- **Windows** is the primary target — pings use `ping -n 1`, ARP cache via
  `arp -a`, and adapter configuration via `netsh interface ipv4`.
- **Linux/macOS** — scanner, terminal, SSH and SCP all work. The Network
  Adapter page shows a "Windows only" notice (different OSes use different
  tools — `nmcli`, `networksetup`, etc.; not implemented in this build).
- **Admin privileges** are only required for the *Apply* action on the
  Adapter page. All other features run as a regular user.

## Keyboard shortcuts

| Shortcut   | Action                          |
|------------|---------------------------------|
| `F5`       | Start scan                      |
| `Esc`      | Stop scan                       |
| `Ctrl+E`   | Export results                  |
| `Ctrl+1…4` | Switch workspace page           |
| `Ctrl+Q`   | Quit                            |
| `Ctrl+L`   | Clear embedded terminal         |
| `Ctrl+C`   | Cancel running terminal command |

## Persistent state

NetScope writes to `~/.netscope/`:

- `settings.json` — active theme, saved SSH host shortcuts (no passwords)
- `history/scan_*.json` — last 20 completed scans

## Limitations

- Embedded terminal is line-buffered. It is not a full PTY — programs that
  expect a TTY (e.g. `vim`, `top`) should be launched via SSH instead.
- The OUI vendor table is a hand-curated subset (~150 prefixes).
- IPv6 is not in scope; the scanner is IPv4-only.
- Network-adapter configuration is Windows only.
