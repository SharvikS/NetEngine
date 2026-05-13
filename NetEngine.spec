# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Net Engine (Windows desktop GUI).

Build (one-folder, recommended first build):

    pyinstaller NetEngine.spec --noconfirm --clean

Build output:

    dist/NetEngine/NetEngine.exe        ← launch this
    dist/NetEngine/_internal/...        ← bundled runtime (do not touch)

To produce a single-file exe instead, flip ONEFILE to True below and
rebuild. One-folder is preferred for distribution because startup is
faster and the UAC prompt doesn't fire each time from the temp dir.
"""

from PyInstaller.utils.hooks import collect_submodules

ONEFILE = False

# Paramiko pulls in cryptography / bcrypt / nacl dynamically via its
# transport negotiation. PyInstaller's hooks usually catch these, but
# we force-collect the paramiko tree to be defensive: SSH/SFTP/SCP is
# load-bearing for this app and a silent missing-module at connect
# time would be a miserable end-user experience.
hiddenimports = []
hiddenimports += collect_submodules("paramiko")
hiddenimports += collect_submodules("cryptography")
hiddenimports += [
    "bcrypt",
    "nacl",
    "nacl.bindings",
    "nacl.signing",
    "nacl.public",
    "nacl.secret",
    "nacl.utils",
]

# Qt modules we actively use. Listing them keeps the analyzer focused
# and prevents accidental pickup of optional Qt packages (WebEngine,
# QtQuick3D, etc.) that would bloat the bundle for no reason.
hiddenimports += [
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
]

# Modules we know we don't ship — exclude so PyInstaller doesn't walk
# into them via transitive imports.
excludes = [
    "tkinter",
    "PyQt6.QtWebEngineWidgets",
    "PyQt6.QtWebEngineCore",
    "PyQt6.QtQml",
    "PyQt6.QtQuick",
    "PyQt6.QtQuick3D",
    "PyQt6.QtMultimedia",
    "PyQt6.QtMultimediaWidgets",
    "PyQt6.QtBluetooth",
    "PyQt6.QtPositioning",
    "PyQt6.QtSensors",
    "PyQt6.QtSerialPort",
    "PyQt6.QtTest",
    "PyQt6.QtCharts",
    "PyQt6.QtDataVisualization",
    "PyQt6.Qt3DCore",
    "PyQt6.Qt3DRender",
    "PyQt6.Qt3DInput",
    "PyQt6.Qt3DAnimation",
    "PyQt6.Qt3DExtras",
    # dev-only
    "pytest",
    "IPython",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[],                 # the app ships no external asset files
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="NetEngine",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,             # UPX-compressing Qt DLLs is a well-known
                               # source of random runtime crashes
        runtime_tmpdir=None,
        console=False,         # GUI app — no console window
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=None,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="NetEngine",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="NetEngine",
    )
