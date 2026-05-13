#!/usr/bin/env python3
"""
Net Engine — Modern Network IP Scanner
Entry point.
"""

from __future__ import annotations

import os
import sys

# HiDPI / fractional scaling on Windows. These env vars must be set
# before QApplication is constructed, otherwise Qt has already committed
# to a DPR for the primary screen and late changes are ignored.
os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

# Make the project root importable regardless of the working directory.
# When frozen (PyInstaller), all modules are already bundled and this
# would only confuse the import system by pointing at _MEIPASS.
if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont

from gui.themes import ThemeManager
from gui.main_window import MainWindow
from gui.motion import install_global_motion, fade_in
from gui.components.loading_screen import LoadingScreen
from utils import settings


def main() -> int:
    # Pass fractional device-pixel ratios through untouched so text and
    # layout stay crisp on 125% / 150% Windows scaling and when the
    # window moves between monitors with different DPI (e.g. when it
    # enters fullscreen on a different display). The default policy
    # rounds the DPR, which causes the brand header to reflow and look
    # distorted whenever the window changes state.
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("Net Engine")
    app.setApplicationDisplayName("Net Engine")
    app.setApplicationVersion("1.1.0")
    app.setOrganizationName("Net Engine")

    app.setFont(QFont("Segoe UI", 10))

    # Attach theme manager → applies the active stylesheet.
    tm = ThemeManager.instance()
    tm.set_glass_opacity(settings.get("glass_opacity", 88))
    tm.set_og_accent(settings.get("og_accent", "Blue"))
    tm.attach(app)
    saved = settings.get("theme", "Dark")
    if saved in tm.theme_names():
        tm.set_theme(saved)

    # Construct the main window up-front but keep it hidden. We show
    # the boot-sequence splash first, then hand off to the main window
    # once the splash finishes — no blocking sleeps, no flicker.
    window = MainWindow()

    # Install the global motion / interaction system AFTER the main
    # window is constructed so the initial sweep finds every control.
    # Future widgets are picked up by the watcher's first-show filter.
    install_global_motion(app)

    splash = LoadingScreen()

    def _reveal_main_window():
        # Show the main window behind the still-visible splash, then
        # fade the splash out; cross-fade gives a premium handoff.
        window.show()
        window.raise_()
        try:
            fade_in(window, duration=300)
        except Exception:
            # fade_in is a best-effort premium touch; never let a
            # missing dependency block the app from appearing.
            pass
        splash.start_fade_out()

    splash.finished.connect(_reveal_main_window)
    splash.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
