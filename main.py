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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont

from gui.themes import ThemeManager
from gui.main_window import MainWindow
from gui.motion import install_global_motion
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
    ThemeManager.instance().attach(app)
    saved = settings.get("theme", "Dark")
    if saved in ThemeManager.instance().theme_names():
        ThemeManager.instance().set_theme(saved)

    window = MainWindow()
    window.show()

    # Install the global motion / interaction system AFTER the main
    # window is constructed so the initial sweep finds every control.
    # Future widgets are picked up by the watcher's first-show filter.
    install_global_motion(app)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
