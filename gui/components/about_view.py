"""
About page — app identity, rotating tagline, website link, credit.

Shown as a first-class page in the main workspace stack. Calls
``on_entered()`` every time the user navigates to it; that hook picks
the next tagline from a curated list so the page feels alive without
resorting to random-on-every-paint behaviour.

The tagline index is persisted to :mod:`utils.settings` so the rotation
survives app restarts — first launch shows line 0, second launch
line 1, and so on, cycling cleanly through the whole set.
"""

from __future__ import annotations

import math
import platform
import sys
import webbrowser
from typing import List

from PyQt6.QtCore import Qt, QTimer, QPointF, QRectF
from PyQt6.QtGui import (
    QPainter, QColor, QFont, QLinearGradient, QPainterPath, QPen,
)
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QSizePolicy, QGridLayout,
)

from gui.themes import theme, ThemeManager
from utils import settings


APP_VERSION = "v1.1.0"
APP_NAME = "NET ENGINE"
APP_SUBTITLE = "NETWORK TOOLKIT"
WEBSITE_URL = "https://sharvik.tech"
WEBSITE_LABEL = "sharvik.tech"

#: Social / profile links shown under the website button. Each entry
#: is (label, url, tooltip). Keep the list short, one row — this is
#: an identity block, not a bookmark bar.
SOCIAL_LINKS: list[tuple[str, str, str]] = [
    ("GitHub",    "https://github.com/SharvikS",
     "github.com/SharvikS"),
    ("Instagram", "https://instagram.com/sharvik69",
     "@sharvik69 on Instagram"),
    ("LinkedIn",  "https://www.linkedin.com/in/sharviksutar",
     "LinkedIn · sharviksutar"),
    ("Steam",     "https://steamcommunity.com/profiles/76561199049007509/",
     "Steam · 76561199049007509"),
]


#: Curated rotating taglines. All lines are thematic, short, and
#: production-sounding. The rotation walks through them in order so a
#: repeat viewer sees every line once before anything repeats.
TAGLINES: List[str] = [
    "Welcome to the world of computer networks.",
    "Built for discovery, control, and connection.",
    "Where terminals, transfers, and networks meet.",
    "A local-first workspace for modern network workflows.",
    "Precision tools for systems, sessions, and infrastructure.",
    "Scan the subnet. Open the session. Move the bytes.",
    "One workspace for every link in the chain.",
    "Your network, rendered with intent.",
    "Packets, ports, and pipes — all under one roof.",
    "Operator-grade tools without the operator-grade weight.",
    "Local AI, remote shells, real answers.",
    "Because every hop deserves a first-class UI.",
]


class _HexBadge(QWidget):
    """Compact hex-logo badge for the About page header.

    Self-contained: paints a hex outline with a soft accent glow and
    the ``>_`` glyph that ties it visually to the sidebar brand.
    Animates a gentle pulse so the page feels alive without getting
    loud.
    """

    _SIZE = 96

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self._SIZE, self._SIZE)
        self._accent = QColor(theme().accent)
        self._accent2 = QColor(theme().accent2 or theme().accent)
        self._glyph = QColor(theme().white)
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(45)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_colors(self, accent: str, accent2: str, glyph: str) -> None:
        self._accent = QColor(accent)
        self._accent2 = QColor(accent2)
        self._glyph = QColor(glyph)
        self.update()

    def _tick(self) -> None:
        try:
            self._phase = (self._phase + 0.018) % 1.0
            self.update()
        except RuntimeError:
            return

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def showEvent(self, event):
        if not self._timer.isActive():
            self._timer.start()
        super().showEvent(event)

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        r = self._SIZE / 2.0 - 9.0

        breath = (1 - math.cos(self._phase * 2 * math.pi)) / 2

        # Build hex path.
        path = QPainterPath()
        pts = []
        for i in range(6):
            ang = math.radians(-90 + i * 60)
            pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
        path.moveTo(*pts[0])
        for x, y in pts[1:]:
            path.lineTo(x, y)
        path.closeSubpath()

        # Outer glow.
        peak_alpha = 40 + 100 * breath
        for stroke_w, scale in ((9.0, 0.20), (6.0, 0.38), (3.5, 0.70)):
            c = QColor(self._accent)
            c.setAlpha(max(0, min(255, int(peak_alpha * scale))))
            pen = QPen(c)
            pen.setWidthF(stroke_w)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.drawPath(path)

        # Fill gradient.
        grad = QLinearGradient(cx, cy - r, cx, cy + r)
        top = QColor(self._accent)
        top.setAlpha(int(60 + 50 * breath))
        bot = QColor(self._accent2)
        bot.setAlpha(40)
        grad.setColorAt(0.0, top)
        grad.setColorAt(1.0, bot)
        p.fillPath(path, grad)

        # Border.
        pen = QPen(self._accent)
        pen.setWidthF(1.8)
        p.setPen(pen)
        p.drawPath(path)

        # '>_' glyph.
        glyph = QColor(self._glyph)
        glyph.setAlpha(int(220 + 35 * breath))
        gpen = QPen(glyph)
        gpen.setWidthF(2.4)
        gpen.setCapStyle(Qt.PenCapStyle.RoundCap)
        gpen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(gpen)
        s = r
        p.drawLine(
            QPointF(cx - s * 0.36, cy - s * 0.30),
            QPointF(cx - s * 0.02, cy),
        )
        p.drawLine(
            QPointF(cx - s * 0.02, cy),
            QPointF(cx - s * 0.36, cy + s * 0.30),
        )
        p.drawLine(
            QPointF(cx + s * 0.08, cy + s * 0.38),
            QPointF(cx + s * 0.48, cy + s * 0.38),
        )
        p.end()


class _InfoCard(QFrame):
    """A titled card used for the environment/info blocks on About."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("about_card")
        self.setFrameShape(QFrame.Shape.NoFrame)


class AboutView(QWidget):
    """Standalone About page rendered in the main workspace stack."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tagline_index = int(settings.get("about_tagline_index", -1) or -1)
        self._build_ui()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())
        self._rotate_tagline()  # pick an initial line on first show

    # ── Build ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(18)

        # ── Hero ────────────────────────────────────────────────────
        hero = QFrame()
        hero.setObjectName("about_hero")
        hero_lay = QHBoxLayout(hero)
        hero_lay.setContentsMargins(26, 24, 26, 24)
        hero_lay.setSpacing(22)

        self._badge = _HexBadge()
        hero_lay.addWidget(self._badge, 0, Qt.AlignmentFlag.AlignVCenter)

        txt_col = QVBoxLayout()
        txt_col.setContentsMargins(0, 0, 0, 0)
        txt_col.setSpacing(6)

        self._lbl_title = QLabel(APP_NAME)
        self._lbl_title.setObjectName("about_title")
        txt_col.addWidget(self._lbl_title)

        self._lbl_subtitle = QLabel(f"{APP_SUBTITLE} · {APP_VERSION}")
        self._lbl_subtitle.setObjectName("about_subtitle")
        txt_col.addWidget(self._lbl_subtitle)

        self._lbl_tagline = QLabel("")
        self._lbl_tagline.setObjectName("about_tagline")
        self._lbl_tagline.setWordWrap(True)
        self._lbl_tagline.setMinimumHeight(22)
        txt_col.addSpacing(6)
        txt_col.addWidget(self._lbl_tagline)

        hero_lay.addLayout(txt_col, 1)
        outer.addWidget(hero)

        # ── Product blurb ──────────────────────────────────────────
        blurb = QFrame()
        blurb.setObjectName("about_card")
        bl = QVBoxLayout(blurb)
        bl.setContentsMargins(22, 18, 22, 18)
        bl.setSpacing(8)

        self._lbl_blurb_heading = QLabel("WHAT IT IS")
        self._lbl_blurb_heading.setObjectName("lbl_section")
        bl.addWidget(self._lbl_blurb_heading)

        self._lbl_blurb = QLabel(
            "Net Engine is a local-first network workstation — a single "
            "workspace for subnet scanning, multi-session SSH, dual-pane "
            "SFTP transfers, adapter configuration, live monitoring, and "
            "a built-in REST console. An optional local-AI assistant "
            "(Ollama) adds command and chat help without ever sending "
            "traffic off your machine."
        )
        self._lbl_blurb.setWordWrap(True)
        self._lbl_blurb.setObjectName("about_blurb")
        bl.addWidget(self._lbl_blurb)

        outer.addWidget(blurb)

        # ── Links card (website + socials) ─────────────────────────
        links = QFrame()
        links.setObjectName("about_card")
        links_col = QVBoxLayout(links)
        links_col.setContentsMargins(22, 16, 22, 16)
        links_col.setSpacing(10)

        # Top row: WEBSITE label + website button + New tagline button.
        website_row = QHBoxLayout()
        website_row.setContentsMargins(0, 0, 0, 0)
        website_row.setSpacing(12)

        self._lbl_website_label = QLabel("WEBSITE")
        self._lbl_website_label.setObjectName("lbl_field_label")
        website_row.addWidget(
            self._lbl_website_label, 0, Qt.AlignmentFlag.AlignVCenter
        )

        self._btn_website = QPushButton(WEBSITE_LABEL)
        self._btn_website.setObjectName("about_website_btn")
        self._btn_website.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_website.setToolTip(WEBSITE_URL)
        self._btn_website.clicked.connect(self._open_website)
        website_row.addWidget(
            self._btn_website, 0, Qt.AlignmentFlag.AlignVCenter
        )

        website_row.addStretch(1)

        self._btn_rotate = QPushButton("New tagline")
        self._btn_rotate.setObjectName("btn_action")
        self._btn_rotate.setToolTip("Cycle to the next tagline")
        self._btn_rotate.clicked.connect(self._on_rotate_clicked)
        website_row.addWidget(
            self._btn_rotate, 0, Qt.AlignmentFlag.AlignVCenter
        )

        links_col.addLayout(website_row)

        # Bottom row: CONNECT label + one pill button per social link.
        social_row = QHBoxLayout()
        social_row.setContentsMargins(0, 0, 0, 0)
        social_row.setSpacing(8)

        self._lbl_social_label = QLabel("CONNECT")
        self._lbl_social_label.setObjectName("lbl_field_label")
        social_row.addWidget(
            self._lbl_social_label, 0, Qt.AlignmentFlag.AlignVCenter
        )

        self._social_buttons: list[QPushButton] = []
        for label, url, tip in SOCIAL_LINKS:
            btn = QPushButton(label)
            btn.setObjectName("about_social_btn")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(f"{tip}\n{url}")
            btn.clicked.connect(lambda _c=False, u=url: self._open_url(u))
            social_row.addWidget(btn, 0, Qt.AlignmentFlag.AlignVCenter)
            self._social_buttons.append(btn)

        social_row.addStretch(1)
        links_col.addLayout(social_row)

        outer.addWidget(links)

        # ── Environment / build info ───────────────────────────────
        env = QFrame()
        env.setObjectName("about_card")
        eg = QGridLayout(env)
        eg.setContentsMargins(22, 16, 22, 16)
        eg.setHorizontalSpacing(26)
        eg.setVerticalSpacing(10)

        self._lbl_env_heading = QLabel("ENVIRONMENT")
        self._lbl_env_heading.setObjectName("lbl_section")
        eg.addWidget(self._lbl_env_heading, 0, 0, 1, 4)

        py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        try:
            from PyQt6.QtCore import QT_VERSION_STR
            qt_ver = QT_VERSION_STR
        except Exception:
            qt_ver = "?"

        pairs = [
            ("Version", APP_VERSION),
            ("Platform", f"{platform.system()} {platform.release()}"),
            ("Python", py),
            ("Qt", qt_ver),
        ]
        self._env_value_labels: list[QLabel] = []
        self._env_key_labels: list[QLabel] = []
        for row_i, (k, v) in enumerate(pairs):
            lk = QLabel(k.upper())
            lk.setObjectName("lbl_field_label")
            lv = QLabel(v)
            lv.setObjectName("about_env_value")
            eg.addWidget(lk, 1 + row_i // 2, (row_i % 2) * 2, 1, 1)
            eg.addWidget(lv, 1 + row_i // 2, (row_i % 2) * 2 + 1, 1, 1)
            self._env_key_labels.append(lk)
            self._env_value_labels.append(lv)
        eg.setColumnStretch(1, 1)
        eg.setColumnStretch(3, 1)

        outer.addWidget(env)

        outer.addStretch(1)

        # ── Credit footer ──────────────────────────────────────────
        footer_row = QHBoxLayout()
        footer_row.setContentsMargins(0, 0, 0, 0)
        footer_row.setSpacing(8)
        footer_row.addStretch(1)

        self._lbl_made_with = QLabel("Made with")
        self._lbl_made_with.setObjectName("about_made_with")

        self._lbl_heart = QLabel("♥")
        self._lbl_heart.setObjectName("about_heart")

        self._lbl_by = QLabel(" · sharvik.tech")
        self._lbl_by.setObjectName("about_made_with")

        footer_row.addWidget(self._lbl_made_with, 0, Qt.AlignmentFlag.AlignVCenter)
        footer_row.addWidget(self._lbl_heart, 0, Qt.AlignmentFlag.AlignVCenter)
        footer_row.addWidget(self._lbl_by, 0, Qt.AlignmentFlag.AlignVCenter)
        footer_row.addStretch(1)
        outer.addLayout(footer_row)

    # ── Public hooks ─────────────────────────────────────────────────

    def on_entered(self) -> None:
        """Called by MainWindow when the user navigates to this page.

        Picks a fresh tagline so repeat visits feel intentional.
        """
        self._rotate_tagline()

    def shutdown(self) -> None:
        try:
            self._badge._timer.stop()
        except Exception:
            pass

    # ── Handlers ─────────────────────────────────────────────────────

    def _rotate_tagline(self) -> None:
        if not TAGLINES:
            return
        self._tagline_index = (self._tagline_index + 1) % len(TAGLINES)
        try:
            settings.set_value("about_tagline_index", self._tagline_index)
        except Exception:
            pass
        self._lbl_tagline.setText(TAGLINES[self._tagline_index])

    def _on_rotate_clicked(self) -> None:
        self._rotate_tagline()

    def _open_website(self) -> None:
        self._open_url(WEBSITE_URL)

    def _open_url(self, url: str) -> None:
        if not url:
            return
        try:
            webbrowser.open(url, new=2)
        except Exception:
            pass

    # ── Theme ────────────────────────────────────────────────────────

    def _restyle(self, t) -> None:
        accent2 = t.accent2 or t.accent
        # Hex badge colours.
        try:
            self._badge.set_colors(t.accent, accent2, t.white)
        except RuntimeError:
            return

        # Outer page owns a soft gradient card look.
        self.setStyleSheet(
            f"#about_hero {{"
            f"  background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
            f"      stop:0 {t.bg_raised}, stop:1 {t.bg_base});"
            f"  border: 1px solid {t.border_lt};"
            f"  border-radius: 12px;"
            f"}}"
            f"#about_card {{"
            f"  background-color: {t.bg_raised};"
            f"  border: 1px solid {t.border};"
            f"  border-radius: 10px;"
            f"}}"
            f"#about_title {{"
            f"  color: {t.accent};"
            f"  font-family: 'JetBrains Mono','Consolas',monospace;"
            f"  font-size: 30px;"
            f"  font-weight: 900;"
            f"  letter-spacing: 4px;"
            f"}}"
            f"#about_subtitle {{"
            f"  color: {accent2};"
            f"  font-family: 'JetBrains Mono','Consolas',monospace;"
            f"  font-size: 11px;"
            f"  font-weight: 800;"
            f"  letter-spacing: 2.2px;"
            f"}}"
            f"#about_tagline {{"
            f"  color: {t.text};"
            f"  font-size: 14px;"
            f"  font-weight: 500;"
            f"  font-style: italic;"
            f"}}"
            f"#about_blurb {{"
            f"  color: {t.text};"
            f"  font-size: 12.5px;"
            f"  line-height: 150%;"
            f"}}"
            f"#about_env_value {{"
            f"  color: {t.text};"
            f"  font-family: 'JetBrains Mono','Consolas',monospace;"
            f"  font-size: 12px;"
            f"}}"
            f"QPushButton#about_website_btn {{"
            f"  background: transparent;"
            f"  color: {t.accent};"
            f"  border: 1px solid {t.accent_dim};"
            f"  border-radius: 6px;"
            f"  padding: 6px 14px;"
            f"  font-weight: 700;"
            f"  font-size: 12px;"
            f"  font-family: 'JetBrains Mono','Consolas',monospace;"
            f"  letter-spacing: 0.6px;"
            f"}}"
            f"QPushButton#about_website_btn:hover {{"
            f"  color: {t.white};"
            f"  background-color: {t.accent_bg};"
            f"  border-color: {t.accent};"
            f"}}"
            f"QPushButton#about_social_btn {{"
            f"  background-color: {t.bg_base};"
            f"  color: {t.text};"
            f"  border: 1px solid {t.border_lt};"
            f"  border-radius: 14px;"
            f"  padding: 5px 14px;"
            f"  font-family: 'JetBrains Mono','Consolas',monospace;"
            f"  font-size: 11px;"
            f"  font-weight: 700;"
            f"  letter-spacing: 0.6px;"
            f"}}"
            f"QPushButton#about_social_btn:hover {{"
            f"  color: {accent2};"
            f"  border-color: {accent2};"
            f"  background-color: {t.bg_hover};"
            f"}}"
            f"QPushButton#about_social_btn:pressed {{"
            f"  background-color: {t.accent_bg};"
            f"  color: {t.accent};"
            f"  border-color: {t.accent};"
            f"}}"
            f"#about_made_with {{"
            f"  color: {t.text_dim};"
            f"  font-size: 12px;"
            f"}}"
            f"#about_heart {{"
            f"  color: {accent2};"
            f"  font-size: 15px;"
            f"  font-weight: 800;"
            f"}}"
        )
