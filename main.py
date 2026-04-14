#!/usr/bin/env python3
"""
NetScope — Modern Network IP Scanner
Entry point.
"""

from __future__ import annotations

import os
import sys

# HiDPI / fractional scaling on Windows
os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

# Make the project root importable regardless of the working directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont

from gui.themes import ThemeManager
from gui.main_window import MainWindow
from utils import settings


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("NetScope")
    app.setApplicationDisplayName("NetScope")
    app.setApplicationVersion("1.1.0")
    app.setOrganizationName("NetScope")

    app.setFont(QFont("Segoe UI", 10))

    # Attach theme manager → applies the active stylesheet.
    ThemeManager.instance().attach(app)
    saved = settings.get("theme", "Dark")
    if saved in ThemeManager.instance().theme_names():
        ThemeManager.instance().set_theme(saved)

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
