# Net Engine — Build & Release Guide

Net Engine ships as a Windows desktop application. This file documents
how the `.exe` is produced and what to do when something breaks during
packaging.

## What produces the build

| Item | Value |
|------|-------|
| Packager | PyInstaller 6.x |
| Spec file | `NetEngine.spec` |
| Entry point | `main.py` |
| Mode | one-folder, windowed (`console=False`) |
| Output | `dist/NetEngine/NetEngine.exe` + `dist/NetEngine/_internal/` |
| Approx size | ~105 MB |

One-folder (not one-file) is the default because:

* startup is faster (no temp-dir unpack on every launch),
* there's no per-launch AV / UAC stall on locked-down Windows,
* packaging regressions are easier to diagnose — the bundle is just
  files on disk.

To switch to one-file, flip `ONEFILE = True` near the top of
`NetEngine.spec` and rebuild.

## Build prerequisites

One-time setup on a fresh Windows dev machine:

```bat
python -m pip install -r requirements.txt
python -m pip install pyinstaller
```

PyInstaller pulls in `paramiko` indirectly; the app's
`requirements.txt` handles the rest (PyQt6, paramiko, requests, psutil
— plus paramiko's `cryptography` / `bcrypt` / `pynacl` chain).

## Build command

From the project root:

```bat
python -m PyInstaller NetEngine.spec --noconfirm --clean
```

Artifacts:

* `dist/NetEngine/NetEngine.exe` — ship this folder
* `build/NetEngine/warn-NetEngine.txt` — list of modules PyInstaller
  couldn't find (expected platform-specific ones: `fcntl`, `_curses`,
  `sspi`/`gssapi`, `pwd`/`grp`, etc. — all harmless on Windows)

To distribute, zip the entire `dist/NetEngine/` directory. End users
extract the zip and run `NetEngine.exe` directly — no Python install
required.

## Why the spec looks the way it does

* **Hidden imports** — `paramiko` and `cryptography` are collected via
  `collect_submodules` because paramiko negotiates ciphers at runtime
  and PyInstaller's static analysis can miss lazily-imported backends.
  SSH/SFTP/SCP is load-bearing; a silent missing-module at connect time
  would be a miserable failure mode, so we're defensive here.
* **Excludes** — unused Qt modules (WebEngine, QtQuick, Qt3D, Charts,
  Multimedia, etc.) and `tkinter` are explicitly excluded. These
  otherwise sneak in transitively and inflate the bundle.
* **No UPX** — compressing Qt DLLs with UPX is a well-known source of
  random runtime crashes; the size win isn't worth it.
* **No datas** — the app paints its logo, loading screen, and theme
  accents entirely in code (`gui/components/loading_screen.py`,
  `gui/themes.py`). There are no external `.png`/`.ico`/`.qss`/font
  files to bundle.

## Runtime paths (what lives where)

The packaged app is local-first. Nothing reads or writes anywhere
unexpected:

| Path | Purpose | Writable location |
|------|---------|-------------------|
| `~/.netscope/settings.json` | user settings (theme, SSH hosts, editor pref, etc.) | ✓ |
| `~/.netscope/history/scan_*.json` | scan history (last 20) | ✓ |
| `%TEMP%/netengine-ft-*/` | file-transfer cache for remote-edit | ✓ |

The app never writes into its install directory, so it works fine
under `C:\Program Files\` or any read-only install location.

## Icon

The spec currently has `icon=None` because the repo ships no `.ico`
file. To brand the exe, drop `NetEngine.ico` next to `NetEngine.spec`
and change `icon=None` to `icon="NetEngine.ico"` in both `EXE(...)`
blocks, then rebuild.

## Versioning

The in-app version is set in `main.py` via
`app.setApplicationVersion("…")` and displayed by the splash
(`gui/components/loading_screen.py`) and About view. Bump both when
cutting a release.

## Troubleshooting packaging issues

**`ModuleNotFoundError` at runtime** — missing hidden import. Add the
module name to `hiddenimports` in `NetEngine.spec` and rebuild.

**`qt.qpa.plugin: could not find the Qt platform plugin "windows"`** —
the PyQt6 runtime hook didn't ship the platform plugin. Verify
`dist/NetEngine/_internal/PyQt6/Qt6/plugins/platforms/qwindows.dll`
exists. If not, reinstall `PyQt6` and rebuild with `--clean`.

**App launches then immediately exits silently** — rebuild with
`console=True` in the spec to see the crash traceback, fix, then flip
back to `console=False` for release.

**Paramiko connects but SSH handshake fails** — a cipher backend was
excluded. Don't trim `cryptography` submodules; keep the
`collect_submodules("cryptography")` line as-is.

**Antivirus flags the exe** — PyInstaller bootloaders are a common
false-positive. The fixes are (in order of effort) code-signing the
exe, reporting the false positive to the AV vendor, or switching to
one-folder mode (which is already the default here).

## Clean rebuild

```bat
rmdir /s /q build dist
python -m PyInstaller NetEngine.spec --noconfirm --clean
```

## Shipping checklist

- [ ] Bump version in `main.py` (`setApplicationVersion`) and
      `loading_screen.py` HUD label.
- [ ] Clean rebuild (above).
- [ ] Launch `dist/NetEngine/NetEngine.exe` on a clean Windows machine
      (VM or spare box without Python installed).
- [ ] Verify: splash plays → main window opens → sidebar loads →
      Settings opens → Terminal loads → File Transfer loads →
      AI view shows "Ollama unavailable" gracefully when Ollama is not
      running.
- [ ] Zip `dist/NetEngine/` as `NetEngine-<version>-win64.zip`.
