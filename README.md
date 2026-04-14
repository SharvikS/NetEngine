# NetScope

A modern desktop network IP scanner with an embedded terminal,
multi-session SSH client, and Windows network-adapter configurator.
Built with Python + PyQt6 — pure Python, no native extensions.

```
+-------------------+--------------------------------------------------+
| WORKSPACE         |  NETSCOPE   Network IP Scanner                   |
| > Scanner         |  ----------------------------------------------- |
|   Terminal        |  TOTAL SCANNED   ALIVE   OFFLINE   OPEN PORTS    |
|   SSH Sessions    |       254          37       217        82       |
|   Adapter         |  ----------------------------------------------- |
|   Monitor         |  IP Address  Hostname    MAC ...   Status        |
|   Tools           |  192.168...  router      ...       ALIVE         |
|   API Console     |  ...                                             |
+-------------------+--------------------------------------------------+
```

## Features

- **Subnet scanner** — concurrent ping sweep, reverse-DNS, ARP/MAC vendor
  lookup, OS hint from TTL, configurable TCP port scan, sortable + filterable
  results, CSV/JSON export, persistent scan history.
- **Host details drawer** — right-side toggleable panel that opens on row
  click and dismisses with an X button. Provides quick actions, host info,
  and an inline SSH connect form.
- **Theme system** — three built-in themes (`Dark`, `Neon`, `Space`)
  selectable from `View → Theme` or the Settings dialog. Themes switch
  instantly and the choice is persisted in `~/.netscope/settings.json`.
- **Embedded terminal** — full in-app terminal panel with selectable shell
  backend (`PowerShell`, `CMD`, `WSL` on Windows; `bash` on Linux). Supports
  command history, built-in `cd`/`clear`/`cls`, `Ctrl+C` / `Ctrl+L`.
- **SSH Sessions workspace** — multi-tab SSH client with collapsible
  connection form, saved-session manager (search + pin + last-connected),
  quick connect bar (`user@host:port`), per-tab status indicators, session
  duplication, and rename via double-click on the tab.
- **Network adapter configuration (Windows)** — view current IPv4 settings,
  manage saved IP profiles, and switch between DHCP and static (address /
  mask / gateway / DNS) using `netsh`. Requires Administrator to apply.
- **Monitor** — live multi-target ping monitor and one-shot port tester.
- **Tools** — quick OS diagnostics (`ipconfig`, `arp`, `route`, `netsh`)
  with custom command runner and rolling activity log.
- **API Console** — built-in REST client (Basic / Bearer auth, headers,
  body, save/load named requests, cURL import/export).
- **Production-grade UI** — theme-driven QSS, splitter layouts, status bar
  with live CPU/MEM, keyboard shortcuts, About dialog.

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
│       ├── detail_panel.py        Host details drawer
│       ├── terminal_view.py
│       ├── terminal_widget.py     Embedded terminal control
│       ├── collapsible.py         Collapsible section helper
│       ├── ssh_view.py            Multi-session SSH workspace
│       ├── ssh_session_tab.py     One SSH session = one tab
│       ├── monitor_view.py        Multi-ping monitor + port tester
│       ├── tools_view.py          Diagnostics + command runner
│       ├── api_console_view.py    Built-in REST client
│       └── network_config_view.py
├── scanner/
│   ├── network.py             Interface enumeration, ARP, IP range
│   ├── host_scanner.py        Ping/DNS/MAC pipeline + ScanController QThread
│   ├── live_ping.py           Continuous ping worker for the Monitor page
│   ├── port_scanner.py        TCP connect scanner
│   ├── service_mapper.py      Port → service name + presets
│   ├── fingerprint.py         OUI vendor table + OS hint from TTL
│   ├── ssh_client.py          paramiko-based SSH session
│   └── net_config.py          Windows netsh adapter helper
└── utils/
    ├── export.py              CSV / JSON export
    ├── history.py             Scan history persistence
    └── settings.py            Theme + saved sessions + IP profiles
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
| psutil    | Network interface enumeration + live system metrics       |
| paramiko  | SSH transport for the multi-session client                |
| requests  | Built-in API console (optional)                           |

## OS-specific notes

- **Windows** is the primary target — pings use `ping -n 1`, ARP cache via
  `arp -a`, and adapter configuration via `netsh interface ipv4`. The
  terminal supports `PowerShell`, `CMD`, and `WSL` backends.
- **Linux/macOS** — scanner, terminal, and SSH all work. The Network
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

- `settings.json` — active theme, saved SSH sessions (passwords are not
  persisted unless the user explicitly opts in per session), saved IP
  profiles, saved API requests, terminal-shell preference
- `history/scan_*.json` — last 20 completed scans

## Limitations

- Embedded terminal is line-buffered. It is not a full PTY — programs that
  expect a TTY (e.g. `vim`, `top`) should be launched via SSH instead.
- The OUI vendor table is a hand-curated subset (~150 prefixes).
- IPv6 is not in scope; the scanner is IPv4-only.
- Network-adapter configuration is Windows only.
