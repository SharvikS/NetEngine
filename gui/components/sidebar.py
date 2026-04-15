"""
Vertical navigation sidebar with one button per top-level page.

Two layout modes:

  * **Expanded** (default, ~232px wide) — shows the brand mark, the
    wordmark "NET ENGINE", full navigation labels, the status block
    with scan activity.

  * **Compact**  (~64px wide) — shows the mark, a compact rail of
    icon-style navigation buttons (each with a short monogram), and
    just the activity dot at the bottom. Used when window width is
    reduced or the user explicitly collapses the sidebar.

Use `set_compact(bool)` to switch modes. The active-selection state,
brand mark colours, and theme restyle work identically in both modes.
"""

from __future__ import annotations

from PyQt6.QtCore import (
    Qt, pyqtSignal, QSize, QRect, QRectF, QPointF, QPropertyAnimation,
    QVariantAnimation, QEasingCurve, QTimer,
)
from PyQt6.QtGui import (
    QPainter, QPen, QColor, QPainterPath, QLinearGradient, QBrush,
    QFont, QFontMetrics,
)
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QButtonGroup, QLabel,
    QFrame, QSizePolicy, QToolButton, QStackedWidget,
)

from gui.themes import ThemeManager, theme
from gui.components.live_widgets import StatusDot
from gui.motion import MOTION, fade_in


# Per-page monogram used in compact mode. The list is indexed by the
# same order as PAGE_LABELS in main_window.
PAGE_MONOGRAMS: list[str] = ["SC", "TM", "SH", "AD", "MO", "TL", "API", "AI"]


class BrandHeader(QWidget):
    """
    Unified brand header — renders the hex logo, the NET ENGINE
    wordmark and the two-tone subtitle entirely inside a single
    paintEvent on a single widget.

    This replaces what used to be a QFrame containing a BrandMark
    QWidget plus two QLabels inside a nested QVBoxLayout. That
    three-widget composite had three independent paint / font-metric
    / layout pipelines, any of which could drift out of sync on
    theme polish, HiDPI transitions, or fullscreen state changes —
    which was the recurring brand-header distortion bug.

    Design properties:

    * **One widget, one paintEvent.** There are no child widgets, so
      there is nothing to resize, nothing to re-polish, no layout to
      invalidate when state changes. The hex, wordmark and subtitle
      are positioned by arithmetic against ``self.width()`` /
      ``self.height()`` inside a single QPainter session.

    * **Deterministic sizeHint from live QFontMetrics.** The hint is
      recomputed fresh from the current QFont every time the parent
      layout asks for it, so the widget reflows naturally when the
      screen DPR changes (different monitor, fullscreen, fractional
      Windows scaling). No "refresh" hook or cache invalidation is
      needed.

    * **No graphics effect.** The outer glow around the hex is drawn
      directly as multi-stroke alpha falloff in paintEvent, so there
      is no offscreen pixmap cache that could get stuck at the wrong
      DPR on a window state transition.

    * **Rotation is paint-scoped.** The hover spin animates only
      ``self._rot``. It is applied inside ``p.save() / p.restore()``
      around the hex draw — the text is always drawn in world
      coordinates and can't be yanked sideways by the transform.

    * **Stretches horizontally, fixed vertically.** Size policy is
      ``Expanding × Fixed`` so the widget fills the sidebar width
      (making centring trivial) and lets its vertical size be
      controlled exclusively by ``sizeHint()``.
    """

    # Deterministic layout constants. These are the only positioning
    # numbers the widget uses — every glyph position is computed from
    # these plus live font metrics, so there are no "magic offsets"
    # lurking inside paint code.
    _HEX_EXPANDED   = 40
    _HEX_COMPACT    = 32
    _TOP_PAD        = 14
    _BOT_PAD_FULL   = 18
    _BOT_PAD_COMPACT= 12
    _GAP_HEX_TITLE  = 12
    _GAP_TITLE_SUB  = 5
    #: Horizontal safety margin reserved on each side for the wordmark
    #: so a black-weighted mono font at HiDPI can't spill into the
    #: sidebar border. If the measured title would exceed the
    #: available width minus 2×this padding we back off to a smaller
    #: font in paint — see ``_title_font_for_width``.
    _TITLE_SIDE_PAD = 10

    _TITLE_TEXT       = "NET ENGINE"
    _SUBTITLE_PART1   = "v1.1 · "
    _SUBTITLE_PART2   = "TOOLKIT"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("brand_header")

        self._compact = False
        self._hex_size = self._HEX_EXPANDED

        # Colours — real values come in via set_colors; these defaults
        # just keep the widget drawable before the theme has applied.
        self._accent    = QColor("#00d4ff")
        self._accent2   = QColor("#ff5cd0")
        self._text      = QColor("#dde7f4")
        self._text_dim  = QColor("#7689a4")
        self._glyph_col = QColor("#ffffff")

        # ── Animation state ─────────────────────────────────────────
        # _phase drives the slow breathing of the hex glow + glyph
        # alpha. _rot drives the hover-triggered spin. _hover_boost
        # is a 0..1 envelope that smoothly brightens things on
        # enter/leave. ALL of these only affect paintEvent — none of
        # them touches geometry or the parent layout.
        self._phase = 0.0
        self._rot = 0.0
        self._hover_boost = 0.0
        self._hovering = False

        self._breath_timer = QTimer(self)
        self._breath_timer.setInterval(40)
        self._breath_timer.timeout.connect(self._tick_breath)
        self._breath_timer.start()

        self._spin_timer = QTimer(self)
        self._spin_timer.setInterval(16)  # ~60fps hover spin
        self._spin_timer.timeout.connect(self._tick_spin)

        self._settle_anim = QVariantAnimation(self)
        self._settle_anim.setDuration(560)
        self._settle_anim.setEasingCurve(
            QEasingCurve(QEasingCurve.Type.OutCubic))
        self._settle_anim.valueChanged.connect(self._on_settle)

        self._boost_anim = QVariantAnimation(self)
        self._boost_anim.setDuration(220)
        self._boost_anim.setEasingCurve(
            QEasingCurve(QEasingCurve.Type.OutCubic))
        self._boost_anim.valueChanged.connect(self._on_boost)

        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Expanding horizontally so we own the sidebar's full content
        # width (makes centring trivial). Fixed vertically because
        # sizeHint() is deterministic.
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)

    # ── Fonts (built fresh on every call) ───────────────────────────
    #
    # Constructing the QFont on-demand (instead of caching it on the
    # instance) is deliberate: it means sizeHint() and paintEvent()
    # always read the CURRENT QFontMetrics — so when the widget moves
    # to a new DPR (fullscreen on a different monitor, fractional
    # scaling, etc.) the next paint and next layout pass use the
    # correct metrics automatically. No cache to go stale.

    @staticmethod
    def _build_title_font(point_size: float = 10.5) -> QFont:
        f = QFont()
        f.setFamilies([
            "JetBrains Mono", "Cascadia Mono", "Cascadia Code",
            "Fira Code", "Consolas", "Segoe UI Mono", "Courier New",
        ])
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSizeF(point_size)
        f.setWeight(QFont.Weight.Black)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.2)
        f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        return f

    def _title_font_for_width(self, available_w: int) -> tuple[QFont, QFontMetrics, int]:
        """Pick the largest title font that fits in *available_w*.

        Walks a small ladder of point sizes so the wordmark shrinks
        gracefully instead of clipping when the sidebar is narrow or
        the running fallback font renders wider than expected. The
        ladder is tiny — 10.5 → 9.5 → 8.5 — so a normal layout always
        uses the top size and shrinking is a visual hint that the
        sidebar is too narrow rather than a routine event.
        """
        budget = max(0, available_w - 2 * self._TITLE_SIDE_PAD)
        for pt in (10.5, 9.5, 8.5):
            font = self._build_title_font(pt)
            fm = QFontMetrics(font)
            w = fm.horizontalAdvance(self._TITLE_TEXT)
            if w <= budget or pt == 8.5:
                return font, fm, w
        # Unreachable — the loop always returns on the last iteration.
        font = self._build_title_font(8.5)
        fm = QFontMetrics(font)
        return font, fm, fm.horizontalAdvance(self._TITLE_TEXT)

    @staticmethod
    def _build_subtitle_font() -> QFont:
        f = QFont()
        f.setFamilies([
            "JetBrains Mono", "Cascadia Mono", "Cascadia Code",
            "Fira Code", "Consolas", "Segoe UI Mono", "Courier New",
        ])
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSizeF(7.0)
        f.setWeight(QFont.Weight.Bold)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.0)
        f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        return f

    # ── Deterministic size hint ────────────────────────────────────

    def sizeHint(self) -> QSize:
        return self._compute_hint()

    def minimumSizeHint(self) -> QSize:
        return self._compute_hint()

    def _compute_hint(self) -> QSize:
        if self._compact:
            h = self._TOP_PAD + self._hex_size + self._BOT_PAD_COMPACT
        else:
            # Size the hint against the LARGEST title font in the
            # ladder so a resize into a narrower sidebar never shrinks
            # the header vertically as the paint-time font shrinks.
            fm_t = QFontMetrics(self._build_title_font(10.5))
            fm_s = QFontMetrics(self._build_subtitle_font())
            h = (self._TOP_PAD
                 + self._hex_size
                 + self._GAP_HEX_TITLE
                 + fm_t.height()
                 + self._GAP_TITLE_SUB
                 + fm_s.height()
                 + self._BOT_PAD_FULL)
        # Width floor: wide enough that the layout engine is willing
        # to give us the full sidebar width even if a parent splitter
        # is momentarily undersized. The final width still comes from
        # the layout's Expanding policy — this is just a safety floor.
        return QSize(max(180, self._hex_size + 24), h)

    # ── Public API ─────────────────────────────────────────────────

    def set_compact(self, compact: bool) -> None:
        if compact == self._compact:
            return
        self._compact = compact
        self._hex_size = (self._HEX_COMPACT if compact
                          else self._HEX_EXPANDED)
        # Ask the parent layout to pull a fresh sizeHint on its next
        # pass. This is the ONLY thing set_compact needs to do for
        # the layout to reflow — no manual margins, no visible/hidden
        # toggles on children (because there are no children).
        self.updateGeometry()
        self.update()

    def set_colors(self, *, accent, text, text_dim,
                   glyph, accent2=None) -> None:
        self._accent    = QColor(accent)
        self._text      = QColor(text)
        self._text_dim  = QColor(text_dim)
        self._glyph_col = QColor(glyph)
        if accent2 is not None:
            self._accent2 = QColor(accent2)
        self.update()

    # ── Animation ticks ────────────────────────────────────────────

    def _tick_breath(self) -> None:
        try:
            self._phase = (self._phase + 0.018) % 1.0
            self.update()
        except RuntimeError:
            return

    def _tick_spin(self) -> None:
        try:
            self._rot = (self._rot + 4.2) % 360.0
            self.update()
        except RuntimeError:
            return

    def _on_settle(self, v) -> None:
        try:
            self._rot = float(v) % 360.0
            self.update()
        except RuntimeError:
            return

    def _on_boost(self, v) -> None:
        try:
            self._hover_boost = float(v)
            self.update()
        except RuntimeError:
            return

    # ── Hover lifecycle ────────────────────────────────────────────

    def enterEvent(self, event):
        self._hovering = True
        self._settle_anim.stop()
        if not self._spin_timer.isActive():
            self._spin_timer.start()
        self._boost_anim.stop()
        self._boost_anim.setStartValue(float(self._hover_boost))
        self._boost_anim.setEndValue(1.0)
        self._boost_anim.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovering = False
        self._spin_timer.stop()
        start = self._rot % 360.0
        end = 360.0 if start > 180.0 else 0.0
        self._settle_anim.stop()
        self._settle_anim.setStartValue(float(start))
        self._settle_anim.setEndValue(float(end))
        self._settle_anim.start()
        self._boost_anim.stop()
        self._boost_anim.setStartValue(float(self._hover_boost))
        self._boost_anim.setEndValue(0.0)
        self._boost_anim.start()
        super().leaveEvent(event)

    def hideEvent(self, event):
        self._breath_timer.stop()
        self._spin_timer.stop()
        super().hideEvent(event)

    def showEvent(self, event):
        if not self._breath_timer.isActive():
            self._breath_timer.start()
        super().showEvent(event)

    # ── Paint ──────────────────────────────────────────────────────

    def paintEvent(self, _ev):
        import math

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        w = self.width()

        breath = (1 - math.cos(self._phase * 2 * math.pi)) / 2
        boost = self._hover_boost
        breath_eff = min(1.0, breath + boost * 0.55)

        # ── Hex logo ────────────────────────────────────────────────
        # Everything inside this block is drawn relative to the hex
        # centre, inside a save()/restore() pair so the rotation
        # transform is strictly local to the hex. The wordmark below
        # is drawn AFTER restore, in world coordinates, and therefore
        # can never be dragged sideways by the hex spin.
        hex_cx = w / 2.0
        hex_cy = self._TOP_PAD + self._hex_size / 2.0
        glow_pad = 5.0  # reserved annulus for outer glow strokes
        r = self._hex_size / 2.0 - glow_pad

        p.save()
        p.translate(hex_cx, hex_cy)
        if self._rot != 0.0:
            p.rotate(self._rot)

        # Hex path centred at origin.
        path = QPainterPath()
        pts = []
        for i in range(6):
            ang = math.radians(-90 + i * 60)
            pts.append((r * math.cos(ang), r * math.sin(ang)))
        path.moveTo(*pts[0])
        for x, y in pts[1:]:
            path.lineTo(x, y)
        path.closeSubpath()

        # Outer glow — paint-time multi-stroke alpha falloff.
        glow_peak = 28 + 100 * breath_eff + 55 * boost
        for stroke_w, alpha_scale in (
            (8.5, 0.20),
            (6.0, 0.35),
            (3.8, 0.60),
            (2.2, 0.90),
        ):
            c = QColor(self._accent)
            c.setAlpha(max(0, min(255, int(glow_peak * alpha_scale))))
            gp = QPen(c)
            gp.setWidthF(stroke_w)
            gp.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            gp.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(gp)
            p.drawPath(path)

        # Fill gradient.
        grad = QLinearGradient(0, -r, 0, r)
        fill_top = QColor(self._accent)
        fill_top.setAlpha(int(48 + 45 * breath_eff + 30 * boost))
        fill_bot = QColor(self._accent2 if boost > 0 else self._accent)
        fill_bot.setAlpha(int(18 + 28 * boost))
        grad.setColorAt(0.0, fill_top)
        grad.setColorAt(1.0, fill_bot)
        p.fillPath(path, grad)

        # Border — lerps slightly toward accent2 on hover.
        border_col = QColor(self._accent)
        if boost > 0:
            border_col.setRedF(min(1.0,
                border_col.redF() * (1 - 0.35 * boost)
                + self._accent2.redF() * 0.35 * boost))
            border_col.setGreenF(min(1.0,
                border_col.greenF() * (1 - 0.35 * boost)
                + self._accent2.greenF() * 0.35 * boost))
            border_col.setBlueF(min(1.0,
                border_col.blueF() * (1 - 0.35 * boost)
                + self._accent2.blueF() * 0.35 * boost))
        pen = QPen(border_col)
        pen.setWidthF(1.6 + 0.5 * boost)
        p.setPen(pen)
        p.drawPath(path)

        # '>_' glyph.
        glyph_color = QColor(self._glyph_col)
        glyph_color.setAlpha(int(220 + 35 * breath_eff))
        gpen = QPen(glyph_color)
        gpen.setWidthF(2.0 + 0.4 * boost)
        gpen.setCapStyle(Qt.PenCapStyle.RoundCap)
        gpen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(gpen)

        s = r
        # chevron '>'
        p.drawLine(QPointF(-s * 0.36, -s * 0.32),
                   QPointF(-s * 0.02,  0.0))
        p.drawLine(QPointF(-s * 0.02,  0.0),
                   QPointF(-s * 0.36,  s * 0.32))
        # underscore
        p.drawLine(QPointF( s * 0.08,  s * 0.40),
                   QPointF( s * 0.48,  s * 0.40))

        # Tiny corner network node — halo + dot.
        node_x =  s * 0.58
        node_y = -s * 0.58
        halo_src = self._accent2 if boost > 0.4 else self._accent
        halo = QColor(halo_src)
        halo.setAlpha(int(35 + 90 * breath_eff + 50 * boost))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(halo)
        halo_r = 3.0 + 2.5 * breath_eff + 1.5 * boost
        p.drawEllipse(QPointF(node_x, node_y), halo_r, halo_r)
        dot = QColor(self._accent)
        dot.setAlpha(235)
        p.setBrush(dot)
        p.drawEllipse(QPointF(node_x, node_y), 1.6, 1.6)

        p.restore()

        # In compact mode the wordmark + subtitle are hidden — we
        # exit here, leaving just the hex visible.
        if self._compact:
            p.end()
            return

        # ── Wordmark + subtitle ────────────────────────────────────
        # Drawn in world coordinates (no active transform) so the
        # hex rotation above cannot distort them. Positions are
        # computed from font metrics sampled at CURRENT DPR, so the
        # text is pixel-correct under any window state.
        #
        # The title font is picked against the CURRENT widget width
        # so a narrow / transient-width paint pass cannot cause left
        # or right clipping — the font shrinks a notch instead.
        title_font, fm_t, title_w = self._title_font_for_width(int(w))
        sub_font = self._build_subtitle_font()
        fm_s = QFontMetrics(sub_font)

        title_x = max(self._TITLE_SIDE_PAD, (w - title_w) / 2.0)
        title_y = (self._TOP_PAD
                   + self._hex_size
                   + self._GAP_HEX_TITLE
                   + fm_t.ascent())

        p.setFont(title_font)
        p.setPen(self._accent)
        p.drawText(QPointF(title_x, title_y), self._TITLE_TEXT)

        # Two-tone subtitle: version in text_dim, TOOLKIT in accent2.
        part1_w = fm_s.horizontalAdvance(self._SUBTITLE_PART1)
        part2_w = fm_s.horizontalAdvance(self._SUBTITLE_PART2)
        total_w = part1_w + part2_w
        sub_x = max(self._TITLE_SIDE_PAD, (w - total_w) / 2.0)
        sub_y = (title_y + fm_t.descent()
                 + self._GAP_TITLE_SUB + fm_s.ascent())

        p.setFont(sub_font)
        p.setPen(self._text_dim)
        p.drawText(QPointF(sub_x, sub_y), self._SUBTITLE_PART1)
        p.setPen(self._accent2)
        p.drawText(QPointF(sub_x + part1_w, sub_y), self._SUBTITLE_PART2)

        p.end()


class _ActiveIndicator(QWidget):
    """
    Smooth sliding accent bar painted on the left edge of the sidebar.

    The widget is a transparent overlay child of the sidebar; its
    geometry is animated to track whichever navigation button is
    currently selected. Mouse events pass straight through to the
    underlying buttons so it never blocks clicks.
    """

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._color = QColor(theme().accent)

    def set_color(self, color: str | QColor) -> None:
        self._color = QColor(color)
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Left accent bar — the primary selection cue. Inset top and
        # bottom so it reads as a neat marker rather than a full-height
        # slab. The bar is drawn first so the subtle glow (below)
        # blooms softly outward from it.
        bar_inset = 10
        bar = QRect(
            0, bar_inset, 3,
            max(0, self.height() - bar_inset * 2),
        )
        p.fillRect(bar, self._color)

        # Faint horizontal glow fading out from the bar. The previous
        # implementation drew a full-row rounded pill which combined
        # with the button's own ``:checked`` background to produce the
        # oversized-card look in the repair brief. We replace it with
        # a narrow gradient strip that sits flush against the bar and
        # decays to nothing within ~40% of the row width, so the
        # checked button keeps its own background as the dominant
        # surface and the indicator only adds a soft accent rail.
        bar_y = bar_inset
        bar_h = max(0, self.height() - bar_inset * 2)
        if bar_h > 0:
            glow_w = min(int(self.width() * 0.40), 90)
            grad = QLinearGradient(3, 0, 3 + glow_w, 0)
            near = QColor(self._color)
            near.setAlpha(42)
            far = QColor(self._color)
            far.setAlpha(0)
            grad.setColorAt(0.0, near)
            grad.setColorAt(1.0, far)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(grad))
            p.drawRect(QRect(3, bar_y, glow_w, bar_h))
        p.end()


class WorkspaceHeading(QWidget):
    """
    Animated section heading with a glossy shimmer + occasional flicker.

    The heading is drawn directly via QPainter so we can sweep a moving
    highlight across the text without re-running QSS or re-flowing the
    layout. The base text sits in the dim palette colour; a horizontal
    gradient pen is then drawn on top with a sliding bright band that
    walks across the glyphs every ~3.5 seconds.

    A second deterministic pulse fires every ~7 seconds, briefly
    raising the base text alpha so the heading "flickers awake". The
    flicker decays smoothly via cosine and never strobes — readability
    is preserved at every frame.

    A faint groove line under the text gives the heading a section-edge
    feel without adding a separate divider widget.
    """

    def __init__(self, text: str = "WORKSPACE", parent=None):
        super().__init__(parent)
        self._text = text
        self._show_text = True

        # Theme-driven colours, populated for real in set_colors().
        self._color_dim   = QColor("#7689a4")
        self._color_text  = QColor("#dde7f4")
        self._color_hi    = QColor("#ffffff")
        self._color_acc2  = QColor("#ff5cd0")

        # Heading font — sized to read as a real section title without
        # crowding the row that contains the collapse toggle. 11pt
        # matches the sidebar's overall type rhythm; the previous 12pt
        # made the row feel cramped against the 28×26 toggle button.
        font = QFont("JetBrains Mono", 11)
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setBold(True)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.2)
        self.setFont(font)

        fm = QFontMetrics(font)
        self._text_w = fm.horizontalAdvance(text)
        self._text_h = fm.height()

        # Reserve just enough vertical room for the glyphs + the 1.4px
        # groove underline + a small breathing margin. The previous
        # ``text_h + 16`` stacked on top of the parent row's own
        # padding produced the cramped look reported in the repair
        # brief — the heading widget already has internal bottom room
        # so the parent row only needs its own outer padding.
        self.setMinimumHeight(self._text_h + 10)
        self.setMinimumWidth(self._text_w + 12)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )

        # ── Animation state ──
        # Sweep phase advances at a fixed step per tick. Flicker phase
        # is a separate countdown / decay so the two effects never
        # interfere with each other.
        self._sweep_phase = 0.0
        self._flicker_value = 0.0       # 0..1, current pulse intensity
        self._flicker_countdown = 4.0   # seconds until next pulse
        self._flicker_decay = 0.0       # remaining decay duration

        self._tick_ms = 40
        self._timer = QTimer(self)
        self._timer.setInterval(self._tick_ms)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ── Public API ──────────────────────────────────────────────────────────

    def set_colors(self, dim: str | QColor, text: str | QColor,
                   hi: str | QColor, accent2: str | QColor) -> None:
        self._color_dim  = QColor(dim)
        self._color_text = QColor(text)
        self._color_hi   = QColor(hi)
        self._color_acc2 = QColor(accent2)
        self.update()

    def set_text_visible(self, visible: bool) -> None:
        """Hide just the text in compact mode (the row stays for the toggle)."""
        if self._show_text == visible:
            return
        self._show_text = visible
        if visible and not self._timer.isActive():
            self._timer.start()
        elif not visible:
            self._timer.stop()
        self.update()

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def showEvent(self, event):
        if self._show_text and not self._timer.isActive():
            self._timer.start()
        super().showEvent(event)

    # ── Animation tick ─────────────────────────────────────────────────────

    def _tick(self) -> None:
        try:
            # Sweep — full traversal every ~3.6s.
            self._sweep_phase = (self._sweep_phase + 0.011) % 1.0

            # Flicker — fire on a deterministic 7s cadence, decay over
            # 220ms via a smooth cosine curve so the pulse reads as a soft
            # awakening rather than a strobe.
            dt = self._tick_ms / 1000.0
            if self._flicker_decay > 0.0:
                self._flicker_decay = max(0.0, self._flicker_decay - dt)
                # Use a cosine ramp from 1.0 → 0.0 over the decay window.
                ratio = self._flicker_decay / 0.22
                import math
                self._flicker_value = (1 - math.cos(ratio * math.pi)) / 2
            else:
                self._flicker_value = 0.0
                self._flicker_countdown -= dt
                if self._flicker_countdown <= 0:
                    self._flicker_decay = 0.22
                    self._flicker_countdown = 7.0
                    self._flicker_value = 1.0

            self.update()
        except RuntimeError:
            return

    # ── Paint ──────────────────────────────────────────────────────────────

    def paintEvent(self, _ev):
        if not self._show_text:
            return

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        font = self.font()
        p.setFont(font)
        fm = QFontMetrics(font)
        text = self._text
        text_w = fm.horizontalAdvance(text)
        text_h = fm.height()

        # Layout — left-aligned with 4px text-padding so the glyphs sit
        # cleanly inside the row without touching the row's left edge.
        x = 4
        baseline = (self.height() + fm.ascent() - fm.descent()) // 2 - 1

        # ── Section groove ────────────────────────────────────────────
        # A 1px line beneath the heading sketched in dim → accent2 →
        # dim. Acts as the section underline you'd see in an IDE
        # sidebar without needing a second widget. Width matches the
        # heading text exactly so it never spans the toggle button.
        groove_y = self.height() - 4
        groove_grad = QLinearGradient(x, 0, x + text_w, 0)
        c0 = QColor(self._color_dim)
        c0.setAlpha(40)
        c1 = QColor(self._color_acc2)
        c1.setAlpha(150)
        groove_grad.setColorAt(0.0, c0)
        groove_grad.setColorAt(0.45, c1)
        groove_grad.setColorAt(1.0, c0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(groove_grad))
        p.drawRect(QRectF(x, groove_y, text_w, 1.4))

        # ── Base text ────────────────────────────────────────────────
        # Lerp between dim and bright text colour by the current
        # flicker value so the deterministic pulse momentarily makes
        # the heading look like it just received signal.
        flicker = self._flicker_value
        base = QColor(
            int(self._color_dim.red()
                + (self._color_text.red() - self._color_dim.red()) * flicker),
            int(self._color_dim.green()
                + (self._color_text.green() - self._color_dim.green()) * flicker),
            int(self._color_dim.blue()
                + (self._color_text.blue() - self._color_dim.blue()) * flicker),
        )
        p.setPen(base)
        p.drawText(x, baseline, text)

        # ── Glossy sweep highlight ───────────────────────────────────
        # The sweep is a horizontal gradient pen drawn over the same
        # text. The pen colour at any glyph pixel is sampled from the
        # gradient at that pixel's x-coordinate, so a moving bright
        # band gives a clean shimmer without per-glyph clipping.
        sweep_w = max(text_w * 0.32, 56.0)
        # Map phase to a sweep position that travels from left of text
        # to right of text plus margin so the highlight enters and
        # exits cleanly off-screen.
        travel = text_w + sweep_w * 2.0
        sweep_pos = -sweep_w + self._sweep_phase * travel

        gx0 = x + sweep_pos - sweep_w * 0.5
        gx1 = x + sweep_pos + sweep_w * 0.5
        sweep_grad = QLinearGradient(gx0, 0, gx1, 0)
        edge = QColor(self._color_hi)
        edge.setAlpha(0)
        peak = QColor(self._color_hi)
        peak.setAlpha(200)
        peak2 = QColor(self._color_acc2)
        peak2.setAlpha(180)
        sweep_grad.setColorAt(0.0, edge)
        sweep_grad.setColorAt(0.45, peak2)
        sweep_grad.setColorAt(0.55, peak)
        sweep_grad.setColorAt(1.0, edge)

        sweep_pen = QPen(QBrush(sweep_grad), 0)
        p.setPen(sweep_pen)
        p.drawText(x, baseline, text)

        p.end()


class Sidebar(QWidget):
    """
    Sidebar with checkable navigation buttons.
    Emits `page_changed(int)` when the active page changes.
    Emits `toggled(bool)` when expanded/compact state changes.
    """

    EXPANDED_WIDTH = 232
    COMPACT_WIDTH  = 64

    page_changed = pyqtSignal(int)
    toggled      = pyqtSignal(bool)   # True = compact, False = expanded

    def __init__(self, items: list[str], parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self._items = list(items)
        self._compact = False
        # Flipped in shutdown() so every animation callback and
        # deferred QTimer.singleShot(0, ...) knows to short-circuit
        # instead of painting into widgets that have been cascade-
        # deleted by the main window's closeEvent.
        self._shutting_down = False

        self.setFixedWidth(self.EXPANDED_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 22, 0, 14)
        root.setSpacing(0)

        # ── Brand header ──────────────────────────────────────────────
        # One custom-painted widget owns the hex logo, the wordmark
        # and the subtitle. See BrandHeader above for why — the short
        # version is that the previous three-widget nested-layout
        # composite kept producing fullscreen / HiDPI distortion
        # because each of its widgets had an independent paint and
        # font-metric pipeline. One widget with one paintEvent and a
        # deterministic sizeHint removes the entire class of bugs.
        self._brand_header = BrandHeader(self)
        root.addWidget(self._brand_header)

        # Back-compat aliases so any lingering external references to
        # the old names still point at a sensible object.
        self._brand_wrap = self._brand_header
        self._mark = self._brand_header
        self._brand = self._brand_header
        self._brand_sub = self._brand_header

        # Thin accent divider under the brand
        self._brand_divider = QFrame()
        self._brand_divider.setObjectName("sidebar_brand_divider")
        self._brand_divider.setFixedHeight(1)
        root.addWidget(self._brand_divider)

        # ── Workspace header row ─────────────────────────────────────────
        # The animated WORKSPACE heading and the collapse toggle share
        # one row so they read as a single section header. The row's
        # left inset (22px) lines up exactly with the navigation
        # buttons' text inset (border-left 3 + padding-left 19), and
        # the row's vertical padding (14 top / 10 bottom) gives the
        # heading the same breathing rhythm as a nav button without
        # double-counting the heading's own internal padding.
        self._workspace_header = QWidget()
        self._workspace_header.setObjectName("workspace_header")
        wh_lay = QHBoxLayout(self._workspace_header)
        wh_lay.setContentsMargins(22, 14, 14, 10)
        wh_lay.setSpacing(10)

        self._workspace_heading = WorkspaceHeading("WORKSPACE")
        wh_lay.addWidget(self._workspace_heading, 1,
                         Qt.AlignmentFlag.AlignVCenter)

        self._btn_toggle = QToolButton()
        self._btn_toggle.setObjectName("sidebar_toggle")
        self._btn_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_toggle.setFixedSize(26, 24)
        self._btn_toggle.setText("⟨")
        self._btn_toggle.setToolTip("Collapse sidebar (Ctrl+B)")
        self._btn_toggle.clicked.connect(self.toggle_compact)
        wh_lay.addWidget(self._btn_toggle, 0,
                         Qt.AlignmentFlag.AlignVCenter)

        root.addWidget(self._workspace_header)

        # Backward-compat alias kept for old set_compact() callers that
        # still expect a `_lbl_workspace` reference. It now points at
        # the animated heading; calling setVisible() on it would hide
        # the toggle button too, so set_compact() instead drives
        # `_workspace_heading.set_text_visible()` directly.
        self._lbl_workspace = self._workspace_heading

        # ── Navigation buttons ───────────────────────────────────────────
        self._buttons: list[QPushButton] = []
        for i, name in enumerate(self._items):
            btn = QPushButton()
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _checked, idx=i: self._on_clicked(idx))
            btn.setToolTip(name)
            btn.setProperty("full_label", name.upper())
            mono = PAGE_MONOGRAMS[i] if i < len(PAGE_MONOGRAMS) else name[:2].upper()
            btn.setProperty("monogram", mono)
            btn.setText(name.upper())
            self._group.addButton(btn, i)
            root.addWidget(btn)
            self._buttons.append(btn)
        if self._buttons:
            self._buttons[0].setChecked(True)

        # Animated active-selection indicator (overlay child). Created
        # last so it stacks above the navigation buttons; raise_() is
        # idempotent so calling it on each refresh costs nothing.
        self._indicator = _ActiveIndicator(self)
        self._indicator.resize(8, 0)
        self._indicator.show()
        self._indicator.raise_()

        # Geometry animation for the indicator slide.
        self._indicator_anim = QPropertyAnimation(self._indicator, b"geometry", self)
        self._indicator_anim.setDuration(MOTION.HOVER_IN + 60)
        self._indicator_anim.setEasingCurve(QEasingCurve(MOTION.EASE_PAGE))

        # Width animation used by set_compact() so the sidebar slides
        # between expanded and compact instead of jumping.
        self._width_anim: QVariantAnimation | None = None

        root.addStretch()

        # Divider above status block
        div = QFrame()
        div.setFrameShape(QFrame.Shape.HLine)
        div.setFixedHeight(1)
        div.setStyleSheet(f"background-color: {theme().border};")
        self._div = div
        root.addWidget(div)

        # ── Live status block ─────────────────────────────────────────────
        # Left margin matches the navigation button text inset
        # (border-left 3 + padding-left 19 = 22) so STATUS heading,
        # status dot, and hosts label all share a single column edge
        # with the buttons above them.
        status_wrap = QWidget()
        self._status_wrap = status_wrap
        self._status_lay = QVBoxLayout(status_wrap)
        self._status_lay.setContentsMargins(22, 14, 22, 8)
        self._status_lay.setSpacing(6)

        self._lbl_status_label = QLabel("STATUS")
        self._lbl_status_label.setObjectName("lbl_section")
        self._status_lay.addWidget(self._lbl_status_label)

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        status_row.setContentsMargins(0, 2, 0, 0)
        self._status_row_lay = status_row
        self._dot = StatusDot(size=10)
        self._dot.set_active(False)
        self._dot.set_color(theme().text_dim)
        status_row.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignVCenter)

        self._lbl_state = QLabel("Idle")
        self._lbl_state.setStyleSheet(
            f"color: {theme().text}; font-size: 12px; font-weight: 600;"
        )
        status_row.addWidget(self._lbl_state, 0, Qt.AlignmentFlag.AlignVCenter)
        status_row.addStretch(1)
        self._status_lay.addLayout(status_row)

        # Indent the hosts label so it sits under "Idle" rather than
        # under the dot — visually pairs the hosts metric with the
        # state value above it. Indent = dot_width(18) + spacing(8) = 26.
        self._lbl_hosts = QLabel("0 hosts alive")
        self._lbl_hosts.setStyleSheet(
            f"color: {theme().text_dim}; font-size: 11px;"
        )
        self._lbl_hosts.setContentsMargins(26, 0, 0, 0)
        self._status_lay.addWidget(self._lbl_hosts)

        root.addWidget(status_wrap)

        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

    def refresh_brand_metrics(self) -> None:
        """
        Ask the brand header to recompute its sizeHint at the current
        DPR and schedule a repaint. Kept as a no-op-ish hook so
        MainWindow's changeEvent handler has something safe to call
        after a WindowStateChange.

        BrandHeader's sizeHint() reads live QFontMetrics on every
        call, so updateGeometry() is enough to propagate a fresh
        hint up the layout tree — no cache invalidation dance is
        required any more.
        """
        self._brand_header.updateGeometry()
        self._brand_header.update()

    # ── Public API ──────────────────────────────────────────────────────────

    def set_current(self, idx: int) -> None:
        if 0 <= idx < len(self._buttons):
            self._buttons[idx].setChecked(True)
            self._slide_indicator_to(idx)

    def set_scan_active(self, active: bool) -> None:
        t = theme()
        if active:
            self._dot.set_active(True, color=t.accent)
            self._lbl_state.setText("Scanning…")
            self._lbl_state.setStyleSheet(
                f"color: {t.accent}; font-size: 12px; font-weight: 700;"
            )
        else:
            self._dot.set_active(False)
            self._dot.set_color(t.text_dim)
            self._lbl_state.setText("Idle")
            self._lbl_state.setStyleSheet(
                f"color: {t.text}; font-size: 12px; font-weight: 600;"
            )

    def set_host_summary(self, alive: int, total: int) -> None:
        if self._shutting_down:
            return
        try:
            if total <= 0:
                self._lbl_hosts.setText("No scans yet")
            else:
                self._lbl_hosts.setText(f"{alive} alive · {total} scanned")
        except RuntimeError:
            return

    # ── Cleanup ─────────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        """
        Stop every animation and timer the sidebar owns before the
        main window begins cascade-deleting its children.

        Covers:
          * BrandHeader breath / spin timers + settle / boost anims
          * WorkspaceHeading sweep / flicker timer
          * Sidebar indicator geometry animation
          * Sidebar width slide animation

        All stops are wrapped so a double-shutdown or a stop after
        the timer's C++ object is already gone is a safe no-op.
        """
        self._shutting_down = True

        try:
            self._indicator_anim.stop()
        except Exception:
            pass

        if self._width_anim is not None:
            try:
                self._width_anim.stop()
            except Exception:
                pass

        # BrandHeader owns multiple tickers; it already knows how to
        # pause them via hideEvent, but shutdown() should force them
        # to stop outright.
        brand = getattr(self, "_brand_header", None)
        if brand is not None:
            for attr in ("_breath_timer", "_spin_timer"):
                timer = getattr(brand, attr, None)
                if timer is not None:
                    try:
                        timer.stop()
                    except Exception:
                        pass
            for attr in ("_settle_anim", "_boost_anim"):
                anim = getattr(brand, attr, None)
                if anim is not None:
                    try:
                        anim.stop()
                    except Exception:
                        pass

        # WorkspaceHeading has its own sweep / flicker timer.
        heading = getattr(self, "_workspace_heading", None)
        if heading is not None:
            timer = getattr(heading, "_timer", None)
            if timer is not None:
                try:
                    timer.stop()
                except Exception:
                    pass

    # ── Compact / expanded mode ─────────────────────────────────────────────

    def is_compact(self) -> bool:
        return self._compact

    def toggle_compact(self) -> None:
        self.set_compact(not self._compact)

    def set_compact(self, compact: bool) -> None:
        if compact == self._compact:
            return
        self._compact = compact

        # Smooth width slide so the layout doesn't snap on toggle.
        target_w = self.COMPACT_WIDTH if compact else self.EXPANDED_WIDTH
        self._animate_width_to(target_w)

        # Brand header owns its own compact/expanded switch. Calling
        # set_compact here updates the widget's internal state and
        # triggers updateGeometry(), so the parent layout picks up
        # the new sizeHint naturally on its next pass — no margins
        # to twiddle, no children to hide/show.
        self._brand_header.set_compact(compact)

        if compact:
            # Replace nav button text with monograms and centre them.
            for btn in self._buttons:
                btn.setText(btn.property("monogram"))

            # Hide the "WORKSPACE" heading text but keep its row so
            # the toggle button stays visible. The STATUS label / host
            # summary collapse fully — there's no actionable widget on
            # those rows in compact mode.
            self._workspace_heading.set_text_visible(False)
            self._lbl_status_label.setVisible(False)
            self._lbl_state.setVisible(False)
            self._lbl_hosts.setVisible(False)

            self._btn_toggle.setText("⟩")
            self._btn_toggle.setToolTip("Expand sidebar (Ctrl+B)")
        else:
            for btn in self._buttons:
                btn.setText(btn.property("full_label"))

            self._workspace_heading.set_text_visible(True)
            self._lbl_status_label.setVisible(True)
            self._lbl_state.setVisible(True)
            self._lbl_hosts.setVisible(True)

            self._btn_toggle.setText("⟨")
            self._btn_toggle.setToolTip("Collapse sidebar (Ctrl+B)")

        # Re-apply the stylesheet so button padding switches between
        # compact-centered and expanded-left-aligned rules.
        self._restyle(theme())
        self.toggled.emit(compact)

    # ── Internal ────────────────────────────────────────────────────────────

    def _on_clicked(self, idx: int) -> None:
        self._slide_indicator_to(idx)
        self.page_changed.emit(idx)

    def _slide_indicator_to(self, idx: int, animate: bool = True) -> None:
        """Animate the active-selection bar to wrap the given button."""
        if not (0 <= idx < len(self._buttons)):
            return
        btn = self._buttons[idx]
        # Translate the button's geometry into the sidebar's coordinate
        # space — the indicator is parented to the sidebar.
        top_left = btn.mapTo(self, btn.rect().topLeft())
        target = QRect(0, top_left.y(), self.width(), btn.height())

        if not animate or self._indicator.geometry().isEmpty():
            self._indicator.setGeometry(target)
            return

        self._indicator_anim.stop()
        self._indicator_anim.setStartValue(self._indicator.geometry())
        self._indicator_anim.setEndValue(target)
        self._indicator_anim.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Snap the indicator to the new layout — the layout has already
        # been recalculated by the time resizeEvent fires.
        QTimer.singleShot(0, self._refresh_indicator_position)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._refresh_indicator_position)

        # On the very first show, kick the brand header so its
        # sizeHint is computed against the window's final screen
        # (rather than whichever transient display it was on while
        # the window was being constructed). After that, Qt handles
        # subsequent DPR changes on its own because BrandHeader's
        # sizeHint reads live QFontMetrics on every call.
        QTimer.singleShot(0, self._brand_header.updateGeometry)

    def _refresh_indicator_position(self) -> None:
        if self._shutting_down:
            return
        try:
            for i, btn in enumerate(self._buttons):
                if btn.isChecked():
                    self._slide_indicator_to(i, animate=False)
                    return
        except RuntimeError:
            return

    def _animate_width_to(self, target_w: int) -> None:
        """Smoothly slide setFixedWidth from the current width to target."""
        if self._shutting_down:
            return
        if self._width_anim is not None:
            try:
                self._width_anim.stop()
            except Exception:
                pass
        start_w = self.width()
        if start_w == target_w:
            try:
                self.setFixedWidth(target_w)
            except RuntimeError:
                pass
            return

        # The valueChanged + finished callbacks can still fire after
        # the sidebar has been torn down (main-window close during an
        # in-flight resize). Wrap both so a late tick can't crash.
        def _apply_width(v):
            if self._shutting_down:
                return
            try:
                self.setFixedWidth(int(v))
            except RuntimeError:
                pass

        def _on_finished():
            if self._shutting_down:
                return
            try:
                self._refresh_indicator_position()
            except RuntimeError:
                pass

        try:
            anim = QVariantAnimation(self)
            anim.setDuration(MOTION.PAGE_FADE)
            anim.setEasingCurve(QEasingCurve(MOTION.EASE_PAGE))
            anim.setStartValue(int(start_w))
            anim.setEndValue(int(target_w))
            anim.valueChanged.connect(_apply_width)
            anim.finished.connect(_on_finished)
            self._width_anim = anim
            anim.start(QVariantAnimation.DeletionPolicy.KeepWhenStopped)
        except RuntimeError:
            return

    def _restyle(self, t):
        self._div.setStyleSheet(f"background-color: {t.border};")
        self._lbl_hosts.setStyleSheet(f"color: {t.text_dim}; font-size: 11px;")

        # Sync the active-selection bar + brand pulse + workspace
        # heading colours to the new palette.
        accent2 = t.accent2 or t.accent
        if hasattr(self, "_indicator"):
            self._indicator.set_color(t.accent)
        if hasattr(self, "_workspace_heading"):
            self._workspace_heading.set_colors(
                dim=t.text_dim,
                text=t.text,
                hi=t.accent,
                accent2=accent2,
            )
        QTimer.singleShot(0, self._refresh_indicator_position)

        # Brand header — hand the current palette to the unified
        # BrandHeader widget. It paints its own hex + wordmark +
        # subtitle, so there is no QSS cascade to manage here: one
        # call updates every colour the brand uses.
        self._brand_header.set_colors(
            accent=t.accent,
            text=t.text,
            text_dim=t.text_dim,
            glyph=t.white,
            accent2=(t.accent2 or t.accent),
        )
        self._brand_divider.setStyleSheet(
            f"#sidebar_brand_divider {{"
            f"  background-color: {t.border_lt};"
            f"}}"
        )

        # Toggle button
        self._btn_toggle.setStyleSheet(
            f"QToolButton#sidebar_toggle {{"
            f"  background-color: {t.bg_raised};"
            f"  color: {t.accent};"
            f"  border: 1px solid {t.border_lt};"
            f"  border-radius: 6px;"
            f"  font-size: 13px;"
            f"  font-weight: 800;"
            f"}}"
            f"QToolButton#sidebar_toggle:hover {{"
            f"  background-color: {t.accent_bg};"
            f"  border-color: {t.accent};"
            f"}}"
        )

        # Navigation button styling depends on compact mode. The active
        # selection visual is handled by the animated overlay
        # `_ActiveIndicator`, so :checked here only adjusts colour and
        # background — never the left-edge bar.
        if self._compact:
            nav_qss = (
                f"#sidebar QPushButton {{"
                f"  background: transparent;"
                f"  border: none;"
                f"  border-left: 3px solid transparent;"
                f"  color: {t.text_dim};"
                f"  text-align: center;"
                f"  padding: 8px 0;"
                f"  margin: 0;"
                f"  font-family: 'JetBrains Mono', 'Consolas', monospace;"
                f"  font-size: 10px;"
                f"  font-weight: 800;"
                f"  letter-spacing: 0.8px;"
                f"  border-radius: 0;"
                f"  min-height: 36px;"
                f"}}"
                f"#sidebar QPushButton:hover {{"
                f"  background-color: {t.bg_hover};"
                f"  color: {t.text};"
                f"}}"
                f"#sidebar QPushButton:checked {{"
                f"  background-color: {t.bg_raised};"
                f"  color: {t.accent};"
                f"}}"
                f"#sidebar QPushButton:checked:hover {{"
                f"  background-color: {t.bg_hover};"
                f"  color: {t.accent};"
                f"}}"
            )
        else:
            # Padding + min-height chosen so the final button box is
            # ~42 px tall: padding_top (9) + content (max(font_h,24))
            # + padding_bottom (9) = 42. Matches the rhythm of the
            # brand header and status block — the old 13/42/13 rule
            # inherited from themes.py produced 68 px buttons which
            # (combined with the full-row active pill in the previous
            # indicator paint) is what the repair brief describes as
            # "oversized/misaligned".
            nav_qss = (
                f"#sidebar QPushButton {{"
                f"  background: transparent;"
                f"  border: none;"
                f"  border-left: 3px solid transparent;"
                f"  color: {t.text_dim};"
                f"  text-align: left;"
                f"  padding: 9px 22px 9px 19px;"
                f"  font-size: 12px;"
                f"  font-weight: 600;"
                f"  letter-spacing: 0.7px;"
                f"  border-radius: 0;"
                f"  min-height: 24px;"
                f"}}"
                f"#sidebar QPushButton:hover {{"
                f"  background-color: {t.bg_hover};"
                f"  color: {t.text};"
                f"}}"
                f"#sidebar QPushButton:checked {{"
                f"  background-color: {t.bg_raised};"
                f"  color: {t.accent};"
                f"}}"
                f"#sidebar QPushButton:checked:hover {{"
                f"  background-color: {t.bg_hover};"
                f"  color: {t.accent};"
                f"}}"
            )
        # Apply the sidebar-scoped QSS to this widget so it overrides
        # the global nav styling from themes.py when compact mode flips.
        self.setStyleSheet(nav_qss)

        # Re-apply state with new palette
        if self._lbl_state.text() == "Scanning…":
            self.set_scan_active(True)
        else:
            self.set_scan_active(False)
