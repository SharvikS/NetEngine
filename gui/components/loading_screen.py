"""
Net Engine — boot sequence loading screen.

A standalone frameless window shown before the main application. Paints
everything in a single ``paintEvent`` so the layers stay perfectly in
sync and GPU-friendly transforms drive the motion:

    Layer 0   animated dark radial backdrop + faint network grid
    Layer 1   drifting particle field with occasional connection lines
    Layer 2   hexagonal "Net Engine" logo (slow spin, hover-accelerated,
              mouse-tilt parallax, breathing glow)
    Layer 3   orbital energy ring — a broken, digital arc arrangement
              rotating on its own axis with scan flickers
    Layer 4   terminal-style boot log (typewriter-appearing lines) and
              a thin glowing progress line

Interactivity
-------------
* Hover over the logo  →  spin accelerates, glow blooms, the logo tilts
  toward the cursor (cheap perspective fake via X/Y-axis shears).
* Cursor position nudges the particle parallax a few pixels for the
  "something alive" feeling.

Integration
-----------
    screen = LoadingScreen()
    screen.show()
    screen.finished.connect(lambda: (main_window.show(), screen.close()))

The screen self-paces the boot log. Call ``finish()`` early to skip.
Pair colors pull from the active ``Theme`` so the boot sequence matches
whichever palette the user has selected.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from PyQt6.QtCore import (
    QEasingCurve, QPointF, QRectF, QTimer, QVariantAnimation, Qt,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath,
    QPen, QRadialGradient,
)
from PyQt6.QtWidgets import QApplication, QWidget

from gui.themes import theme


# ── Boot log script ──────────────────────────────────────────────────────────

# (status, text, delay_before_ms) — a tiny DSL that drives the typewriter
# rollout. Statuses map to accent/green/amber/red color lanes in the
# paint pass. Keep this list short; the whole sequence should finish in
# roughly the PAUSE + sum(delays) budget so the splash feels crisp.
_BOOT_STEPS: list[tuple[str, str, int]] = [
    ("INIT", "Booting Net Engine runtime ...",        80),
    ("OK",   "SSH engine ready",                      340),
    ("OK",   "File transfer module ready",            260),
    ("OK",   "Network scanner online",                220),
    ("OK",   "Terminal subsystem attached",           240),
    ("OK",   "AI assistant initialized",              300),
    ("DONE", "Net Engine ready",                      240),
]


@dataclass
class _Particle:
    x: float
    y: float
    vx: float
    vy: float
    radius: float
    alpha: float
    phase: float = 0.0

    def step(self, dt: float, w: int, h: int) -> None:
        self.x += self.vx * dt
        self.y += self.vy * dt
        self.phase += dt * 0.9
        # Wrap around the viewport so the field never depopulates.
        if self.x < -20: self.x = w + 20
        elif self.x > w + 20: self.x = -20
        if self.y < -20: self.y = h + 20
        elif self.y > h + 20: self.y = -20


@dataclass
class _LogLine:
    status: str
    text: str
    typed: int = 0            # chars typed so far
    revealed_at: float = 0.0  # seconds since splash start when this line began


# ── Main widget ──────────────────────────────────────────────────────────────

class LoadingScreen(QWidget):
    """
    Frameless, always-on-top boot screen. Runs an internal ~60fps tick,
    fades in on ``show()``, plays the boot log, then emits ``finished``
    and fades itself out. The caller is responsible for showing the
    main window (typically on the ``finished`` signal) and for calling
    ``close()`` once the fade-out completes.
    """

    finished = pyqtSignal()
    closed = pyqtSignal()

    # Visual tuning — one place for the knobs.
    _W                 = 900
    _H                 = 600
    _FPS_MS            = 16
    _LOGO_RADIUS       = 88
    _RING_RADIUS       = 138
    _PARTICLE_COUNT    = 55
    _SPIN_BASE_DPS     = 18.0      # degrees per second at rest
    _SPIN_HOVER_DPS    = 95.0      # degrees per second while hovered
    _RING_DPS          = -42.0     # opposite direction for contrast
    _TYPE_CPS          = 55.0      # characters per second
    _LINE_GAP_S        = 0.22      # pause between lines
    _HOLD_AFTER_DONE_S = 0.55      # linger on the completed log
    _FADE_MS           = 380

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Frameless + always-on-top is the right combo for a boot
        # splash. We intentionally avoid ``Qt.WindowType.SplashScreen``
        # — on some Windows + PyQt6 combinations it swallows mouse
        # events, which would kill the hover-accelerates-spin and
        # tilt-follow-cursor interactions this screen is built around.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setMouseTracking(True)
        self.setFixedSize(self._W, self._H)
        self.setWindowTitle("Net Engine")
        self._center_on_screen()

        # ── Runtime state ────────────────────────────────────────────
        self._elapsed_s: float = 0.0          # total time since show
        self._logo_angle: float = 0.0         # degrees
        self._ring_angle: float = 0.0
        self._spin_dps: float = self._SPIN_BASE_DPS
        self._hover: bool = False
        self._mouse = QPointF(self._W / 2, self._H / 2)
        self._tilt = QPointF(0.0, 0.0)        # smoothed cursor-offset
        self._glow_intensity: float = 0.45    # 0..1
        # Pre-initialised so the first ``paintEvent`` (which can fire
        # before the tick timer's first pulse) has a valid value to
        # sample. Overwritten every tick.
        self._glow_render: float = 0.45
        self._progress: float = 0.0
        self._finishing: bool = False
        self._holding_after_done: float = 0.0
        self._opacity: float = 0.0            # for fade in/out

        # ── Log lines ────────────────────────────────────────────────
        self._lines: list[_LogLine] = []
        self._script_cursor: int = 0
        self._next_line_at: float = 0.08      # first line waits a beat
        self._typing_line: _LogLine | None = None

        # ── Particle field ───────────────────────────────────────────
        rng = random.Random(0x8E741)
        self._particles: list[_Particle] = []
        for _ in range(self._PARTICLE_COUNT):
            self._particles.append(_Particle(
                x=rng.uniform(0, self._W),
                y=rng.uniform(0, self._H),
                vx=rng.uniform(-10, 10),
                vy=rng.uniform(-8, 8),
                radius=rng.uniform(0.8, 2.2),
                alpha=rng.uniform(0.18, 0.68),
                phase=rng.uniform(0, 2 * math.pi),
            ))

        # ── Animation driver ─────────────────────────────────────────
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)

        # Fade animation. A plain ``QVariantAnimation`` driving the
        # window opacity via a signal avoids declaring a dynamic Qt
        # property on the widget (``pyqtProperty`` + ``QPropertyAnimation``
        # has bitten offscreen Qt setups in this codebase before).
        self._fade = QVariantAnimation(self)
        self._fade.setDuration(self._FADE_MS)
        self._fade.setEasingCurve(QEasingCurve(QEasingCurve.Type.InOutQuad))
        self._fade.valueChanged.connect(self._on_fade_value)

        self._last_tick_ms: int = 0

    # ── Fade helpers ─────────────────────────────────────────────────────────

    def _on_fade_value(self, v) -> None:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return
        self._opacity = max(0.0, min(1.0, f))
        self.setWindowOpacity(self._opacity)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def _center_on_screen(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move(
            geo.x() + (geo.width()  - self._W) // 2,
            geo.y() + (geo.height() - self._H) // 2,
        )

    def showEvent(self, e) -> None:  # type: ignore[override]
        super().showEvent(e)
        # Fade in from 0.
        self.setWindowOpacity(0.0)
        self._opacity = 0.0
        self._fade.stop()
        try:
            self._fade.finished.disconnect()
        except TypeError:
            pass
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()
        self._last_tick_ms = 0
        self._elapsed_s = 0.0
        if not self._timer.isActive():
            self._timer.start(self._FPS_MS)

    def closeEvent(self, e) -> None:  # type: ignore[override]
        self._timer.stop()
        self.closed.emit()
        super().closeEvent(e)

    def finish(self) -> None:
        """Skip the remaining boot log and transition to the done state."""
        if self._finishing:
            return
        self._script_cursor = len(_BOOT_STEPS)
        for ln in self._lines:
            ln.typed = len(ln.text)
        self._progress = 1.0
        self._holding_after_done = self._HOLD_AFTER_DONE_S
        self._typing_line = None

    def start_fade_out(self) -> None:
        """Fade the splash out and ``close()`` on completion."""
        if self._fade.state() == QVariantAnimation.State.Running:
            self._fade.stop()
        self._fade.setStartValue(float(self._opacity))
        self._fade.setEndValue(0.0)
        try:
            self._fade.finished.disconnect()
        except TypeError:
            pass
        self._fade.finished.connect(self.close)
        self._fade.start()

    # ── Tick / simulation ────────────────────────────────────────────────────

    def _tick(self) -> None:
        try:
            self._tick_impl()
        except Exception:  # pragma: no cover — diagnostic only
            import traceback
            traceback.print_exc()
            self._timer.stop()

    def _tick_impl(self) -> None:
        # Fixed-step dt keeps the visuals deterministic even under load.
        dt = self._FPS_MS / 1000.0
        self._elapsed_s += dt

        # Ease the spin toward base / hover target.
        target = self._SPIN_HOVER_DPS if self._hover else self._SPIN_BASE_DPS
        self._spin_dps += (target - self._spin_dps) * min(1.0, dt * 6.0)
        self._logo_angle = (self._logo_angle + self._spin_dps * dt) % 360.0
        self._ring_angle = (self._ring_angle + self._RING_DPS * dt) % 360.0

        # Ease glow intensity toward hover target.
        g_target = 0.95 if self._hover else 0.45
        # Add the breathing term so even at rest it pulses gently.
        breath = 0.08 * math.sin(self._elapsed_s * 2.0 * math.pi / 1.8)
        self._glow_intensity += (g_target - self._glow_intensity) * min(1.0, dt * 5.0)
        self._glow_render = max(0.0, min(1.0, self._glow_intensity + breath))

        # Smooth tilt toward the cursor offset (normalized to [-1,1]).
        cx, cy = self._W / 2, self._H / 2 - 30
        dx = (self._mouse.x() - cx) / (self._W / 2)
        dy = (self._mouse.y() - cy) / (self._H / 2)
        dx = max(-1.0, min(1.0, dx))
        dy = max(-1.0, min(1.0, dy))
        ease = min(1.0, dt * 6.0)
        self._tilt = QPointF(
            self._tilt.x() + (dx - self._tilt.x()) * ease,
            self._tilt.y() + (dy - self._tilt.y()) * ease,
        )

        # Advance particles.
        for p in self._particles:
            p.step(dt, self._W, self._H)

        # Boot log advancement.
        if not self._finishing:
            self._advance_log(dt)

        # Progress advances smoothly toward (completed_lines / total).
        total = len(_BOOT_STEPS)
        done_lines = sum(1 for ln in self._lines if ln.typed >= len(ln.text))
        partial = 0.0
        if self._typing_line is not None and self._typing_line.text:
            partial = self._typing_line.typed / max(1, len(self._typing_line.text))
        progress_target = (done_lines + partial) / max(1, total)
        self._progress += (progress_target - self._progress) * min(1.0, dt * 7.0)

        # When the log is fully consumed, hold briefly then emit finished.
        if (self._script_cursor >= total and self._typing_line is None
                and not self._finishing):
            self._holding_after_done -= dt
            if self._holding_after_done <= 0.0:
                self._finishing = True
                self.finished.emit()

        self.update()

    def _advance_log(self, dt: float) -> None:
        # Currently typing a line? Keep typing.
        if self._typing_line is not None:
            cps = self._TYPE_CPS
            self._typing_line.typed = min(
                len(self._typing_line.text),
                self._typing_line.typed + int(math.ceil(cps * dt)),
            )
            if self._typing_line.typed >= len(self._typing_line.text):
                self._next_line_at = self._elapsed_s + self._LINE_GAP_S
                self._typing_line = None
            return

        # Waiting for the next line to start?
        if self._script_cursor >= len(_BOOT_STEPS):
            return
        if self._elapsed_s < self._next_line_at:
            return

        status, text, delay_ms = _BOOT_STEPS[self._script_cursor]
        line = _LogLine(status=status, text=text, typed=0,
                        revealed_at=self._elapsed_s)
        self._lines.append(line)
        self._typing_line = line
        self._script_cursor += 1
        # Fold the per-step delay in as a post-complete pause by advancing
        # the typewriter start via delay_ms (capped minimum so it stays
        # responsive).
        self._next_line_at = self._elapsed_s + max(0.0, delay_ms / 1000.0) * 0.0

    # ── Mouse interaction ────────────────────────────────────────────────────

    def mouseMoveEvent(self, e) -> None:  # type: ignore[override]
        self._mouse = QPointF(e.position())
        # Hover = cursor inside an inflated logo region.
        cx, cy = self._W / 2, self._H / 2 - 30
        inside = (self._mouse.x() - cx) ** 2 + (self._mouse.y() - cy) ** 2
        self._hover = inside <= (self._RING_RADIUS + 18) ** 2
        self.update()

    def leaveEvent(self, e) -> None:  # type: ignore[override]
        self._hover = False
        super().leaveEvent(e)

    def mousePressEvent(self, e) -> None:  # type: ignore[override]
        # Allow a click on the logo to fast-forward the boot (feels
        # responsive without ever being required).
        cx, cy = self._W / 2, self._H / 2 - 30
        inside = (e.position().x() - cx) ** 2 + (e.position().y() - cy) ** 2
        if inside <= self._RING_RADIUS ** 2:
            self.finish()

    # ── Painting ─────────────────────────────────────────────────────────────

    def paintEvent(self, _e) -> None:  # type: ignore[override]
        try:
            self._paint_impl()
        except Exception:  # pragma: no cover — diagnostic only
            import traceback
            traceback.print_exc()

    def _paint_impl(self) -> None:
        t = theme()
        p = QPainter(self)
        p.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
            | QPainter.RenderHint.TextAntialiasing
        )

        accent  = QColor(t.accent)
        accent2 = QColor(t.accent2 or t.accent)
        bg_deep = QColor(t.bg_deep)
        text_dim = QColor(t.text_dim)
        text = QColor(t.text)
        green = QColor(t.green)
        amber = QColor(t.amber)
        red = QColor(t.red)

        self._paint_background(p, bg_deep, accent)
        self._paint_grid(p, accent)
        self._paint_particles(p, accent, accent2)
        self._paint_ring(p, accent, accent2)
        self._paint_logo(p, accent, accent2, bg_deep)
        self._paint_wordmark(p, text, text_dim, accent2)
        self._paint_boot_log(p, text, text_dim, accent, green, amber, red)
        self._paint_progress_line(p, accent, accent2)
        self._paint_corner_hud(p, text_dim, accent)

        p.end()

    # ── Paint helpers ────────────────────────────────────────────────────────

    def _paint_background(self, p: QPainter, deep: QColor, accent: QColor) -> None:
        # Solid deep base + soft radial accent bloom from the logo center.
        p.fillRect(self.rect(), deep)
        cx, cy = self._W / 2, self._H / 2 - 30
        rg = QRadialGradient(QPointF(cx, cy), max(self._W, self._H) * 0.65)
        bloom = QColor(accent)
        bloom.setAlpha(int(42 * self._glow_render))
        rg.setColorAt(0.0, bloom)
        mid = QColor(accent)
        mid.setAlpha(int(12 * self._glow_render))
        rg.setColorAt(0.35, mid)
        rg.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), QBrush(rg))

        # Vignette toward the top/bottom.
        lg = QLinearGradient(0, 0, 0, self._H)
        lg.setColorAt(0.0, QColor(0, 0, 0, 70))
        lg.setColorAt(0.5, QColor(0, 0, 0, 0))
        lg.setColorAt(1.0, QColor(0, 0, 0, 90))
        p.fillRect(self.rect(), QBrush(lg))

    def _paint_grid(self, p: QPainter, accent: QColor) -> None:
        # Faint 40px grid with a slow horizontal shimmer. Drawn in
        # a low-alpha accent color so it reads as atmosphere, not content.
        step = 40
        shimmer = 0.5 + 0.5 * math.sin(self._elapsed_s * 0.9)
        c = QColor(accent)
        c.setAlpha(int(14 + 10 * shimmer))
        pen = QPen(c, 1.0)
        pen.setCosmetic(True)
        p.setPen(pen)
        # Offset so the grid drifts left-right slightly.
        ox = (self._elapsed_s * 6.0) % step
        x = -ox
        while x < self._W:
            p.drawLine(QPointF(x, 0), QPointF(x, self._H))
            x += step
        y = 0.0
        while y < self._H:
            p.drawLine(QPointF(0, y), QPointF(self._W, y))
            y += step

    def _paint_particles(self, p: QPainter, accent: QColor, accent2: QColor) -> None:
        # Slight parallax: shift particles by a fraction of the cursor tilt.
        ox = self._tilt.x() * 6.0
        oy = self._tilt.y() * 6.0

        # Dots.
        for i, pa in enumerate(self._particles):
            breath = 0.5 + 0.5 * math.sin(pa.phase)
            a = int(max(0, min(255, pa.alpha * 230 * (0.55 + 0.45 * breath))))
            c = QColor(accent2 if (i % 5 == 0) else accent)
            c.setAlpha(a)
            p.setBrush(QBrush(c))
            p.setPen(Qt.PenStyle.NoPen)
            r = pa.radius
            p.drawEllipse(QPointF(pa.x + ox, pa.y + oy), r, r)

        # Occasional short connection lines between nearby particles.
        # Limit to close pairs so this stays cheap.
        pen = QPen(QColor(accent.red(), accent.green(), accent.blue(), 32), 0.8)
        pen.setCosmetic(True)
        p.setPen(pen)
        threshold_sq = 92 * 92
        n = len(self._particles)
        # Stride so we don't do O(n^2) — sample pairs spaced apart.
        for i in range(0, n, 2):
            a = self._particles[i]
            b = self._particles[(i + 3) % n]
            dx = a.x - b.x
            dy = a.y - b.y
            d2 = dx * dx + dy * dy
            if d2 < threshold_sq:
                alpha = int(48 * (1.0 - d2 / threshold_sq))
                c = QColor(accent)
                c.setAlpha(alpha)
                p.setPen(QPen(c, 0.8))
                p.drawLine(QPointF(a.x + ox, a.y + oy),
                           QPointF(b.x + ox, b.y + oy))

    def _paint_ring(self, p: QPainter, accent: QColor, accent2: QColor) -> None:
        cx, cy = self._W / 2, self._H / 2 - 30
        p.save()
        p.translate(cx, cy)
        p.rotate(self._ring_angle)

        r_outer = self._RING_RADIUS
        r_inner = self._RING_RADIUS - 14

        # Broken arcs (digital ring) — each arc a few degrees with gaps.
        arcs = [
            (  0,  68), ( 80,  26), (118,  42),
            (172,  50), (232,  34), (278,  58), (344,  10),
        ]
        pen = QPen(QColor(accent), 2.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        for start, span in arcs:
            # Occasional flicker → drop alpha briefly.
            flicker = 1.0
            if math.sin(self._elapsed_s * 4.0 + start) > 0.95:
                flicker = 0.35
            c = QColor(accent)
            c.setAlpha(int(200 * flicker * (0.55 + 0.45 * self._glow_render)))
            pen.setColor(c)
            p.setPen(pen)
            rect = QRectF(-r_outer, -r_outer, 2 * r_outer, 2 * r_outer)
            p.drawArc(rect, int(start * 16), int(span * 16))

        # Inner thin ring (accent2) — always full circle, very low alpha.
        c2 = QColor(accent2)
        c2.setAlpha(int(60 * (0.5 + 0.5 * self._glow_render)))
        p.setPen(QPen(c2, 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(0, 0), r_inner, r_inner)

        # Four tick notches at the cardinals for a scan-line feel.
        tick_pen = QPen(QColor(accent), 2.0)
        tick_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(tick_pen)
        for ang in (0, 90, 180, 270):
            rad = math.radians(ang)
            x1 = math.cos(rad) * (r_outer + 4)
            y1 = math.sin(rad) * (r_outer + 4)
            x2 = math.cos(rad) * (r_outer + 14)
            y2 = math.sin(rad) * (r_outer + 14)
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

        p.restore()

    def _paint_logo(self, p: QPainter, accent: QColor, accent2: QColor,
                    deep: QColor) -> None:
        cx, cy = self._W / 2, self._H / 2 - 30
        r = self._LOGO_RADIUS

        # Glow halo (drawn before the logo so it sits underneath).
        halo = QRadialGradient(QPointF(cx, cy), r * 2.4)
        c_in = QColor(accent)
        c_in.setAlpha(int(180 * self._glow_render))
        halo.setColorAt(0.0, c_in)
        c_mid = QColor(accent)
        c_mid.setAlpha(int(38 * self._glow_render))
        halo.setColorAt(0.35, c_mid)
        halo.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(halo))
        p.drawRect(QRectF(cx - r * 2.4, cy - r * 2.4, r * 4.8, r * 4.8))

        p.save()
        p.translate(cx, cy)

        # Mouse tilt — fake 3D via anisotropic scale. Cursor above the
        # logo squashes the vertical axis slightly, cursor to the side
        # squashes the horizontal axis. Very small amplitude so the
        # logo reads as "responsive", not "warped".
        tilt_x = self._tilt.x() * 0.10
        tilt_y = self._tilt.y() * 0.10
        sx = 1.0 - abs(tilt_x) * 0.35
        sy = 1.0 - abs(tilt_y) * 0.35

        # Breathing scale.
        breath = 1.0 + 0.03 * math.sin(self._elapsed_s * 2.0 * math.pi / 2.4)
        hover_boost = 1.05 if self._hover else 1.0
        p.scale(sx * breath * hover_boost, sy * breath * hover_boost)

        # Spin.
        p.rotate(self._logo_angle)

        # Hexagon path.
        hex_path = QPainterPath()
        for i in range(6):
            ang = math.radians(60 * i - 30)
            x = math.cos(ang) * r
            y = math.sin(ang) * r
            if i == 0:
                hex_path.moveTo(x, y)
            else:
                hex_path.lineTo(x, y)
        hex_path.closeSubpath()

        # Inner fill — dark with a subtle accent radial so the logo
        # reads as a rim-lit solid, not flat.
        fill = QRadialGradient(QPointF(0, 0), r)
        fill.setColorAt(0.0, QColor(deep.red(), deep.green(), deep.blue(), 235))
        mix = QColor(accent)
        mix.setAlpha(30)
        fill.setColorAt(0.85, QColor(deep.red(), deep.green(), deep.blue(), 255))
        fill.setColorAt(1.0, mix)
        p.setBrush(QBrush(fill))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPath(hex_path)

        # Outer stroke.
        stroke = QColor(accent)
        stroke.setAlpha(255)
        outer_pen = QPen(stroke, 2.6)
        outer_pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        p.setPen(outer_pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(hex_path)

        # Inner concentric hexagon.
        inner = QPainterPath()
        rr = r * 0.62
        for i in range(6):
            ang = math.radians(60 * i - 30)
            x = math.cos(ang) * rr
            y = math.sin(ang) * rr
            if i == 0: inner.moveTo(x, y)
            else:      inner.lineTo(x, y)
        inner.closeSubpath()
        inner_c = QColor(accent2)
        inner_c.setAlpha(180)
        p.setPen(QPen(inner_c, 1.2))
        p.drawPath(inner)

        # Node graph inside — six radial spokes with end-dots, like a
        # little network topology. Reads as "Net Engine" at a glance.
        spoke_pen = QPen(QColor(accent), 1.4)
        spoke_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(spoke_pen)
        node_r = 3.2
        for i in range(6):
            ang = math.radians(60 * i)
            x = math.cos(ang) * rr * 0.9
            y = math.sin(ang) * rr * 0.9
            p.drawLine(QPointF(0, 0), QPointF(x, y))
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(6):
            ang = math.radians(60 * i)
            x = math.cos(ang) * rr * 0.9
            y = math.sin(ang) * rr * 0.9
            p.setBrush(QBrush(QColor(accent2)))
            p.drawEllipse(QPointF(x, y), node_r, node_r)

        # Central hub node.
        hub_grad = QRadialGradient(QPointF(0, 0), 10)
        hub_grad.setColorAt(0.0, QColor(255, 255, 255, 230))
        hc = QColor(accent)
        hc.setAlpha(180)
        hub_grad.setColorAt(0.6, hc)
        hub_grad.setColorAt(1.0, QColor(accent.red(), accent.green(), accent.blue(), 0))
        p.setBrush(QBrush(hub_grad))
        p.drawEllipse(QPointF(0, 0), 10, 10)

        p.restore()

    def _paint_wordmark(self, p: QPainter, text: QColor, dim: QColor,
                        accent2: QColor) -> None:
        cx = self._W / 2
        baseline = self._H / 2 + self._RING_RADIUS - 4

        # Primary wordmark.
        f = QFont("Segoe UI", 20)
        f.setWeight(QFont.Weight.Black)
        f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 118)
        p.setFont(f)
        fm = p.fontMetrics()
        title = "NET  ENGINE"
        tw = fm.horizontalAdvance(title)
        p.setPen(QPen(text))
        p.drawText(QPointF(cx - tw / 2, baseline), title)

        # Kicker under the title.
        fk = QFont("Consolas", 9)
        fk.setWeight(QFont.Weight.DemiBold)
        fk.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 165)
        p.setFont(fk)
        kfm = p.fontMetrics()
        kicker = "SYSTEM  BOOT  SEQUENCE"
        kw = kfm.horizontalAdvance(kicker)
        p.setPen(QPen(accent2))
        p.drawText(QPointF(cx - kw / 2, baseline + 22), kicker)

    def _paint_boot_log(self, p: QPainter, text: QColor, dim: QColor,
                        accent: QColor, green: QColor, amber: QColor,
                        red: QColor) -> None:
        # A left-aligned block sitting below the wordmark.
        left = self._W / 2 - 210
        top  = self._H / 2 + self._RING_RADIUS + 48
        line_h = 20

        mono = QFont("Consolas", 10)
        mono.setWeight(QFont.Weight.DemiBold)
        p.setFont(mono)

        # Only keep the last N lines visible so nothing gets clipped.
        visible_n = 6
        recent = self._lines[-visible_n:]
        y = top
        for ln in recent:
            status_color = {
                "INIT": accent,
                "OK":   green,
                "WARN": amber,
                "FAIL": red,
                "DONE": accent,
            }.get(ln.status, accent)

            # [ STATUS ]
            bracket = f"[ {ln.status:<4} ] "
            p.setPen(QPen(dim))
            p.drawText(QPointF(left, y), "[")
            p.setPen(QPen(status_color))
            p.drawText(QPointF(left + 10, y), f" {ln.status:<4} ")
            p.setPen(QPen(dim))
            p.drawText(QPointF(left + 56, y), "]")

            # Typed body.
            body = ln.text[: ln.typed]
            p.setPen(QPen(text))
            p.drawText(QPointF(left + 76, y), body)

            # Blinking caret on the currently-typing line.
            if ln is self._typing_line and ln.typed < len(ln.text):
                fm = p.fontMetrics()
                w = fm.horizontalAdvance(body)
                if int(self._elapsed_s * 2.0) % 2 == 0:
                    p.fillRect(
                        QRectF(left + 76 + w + 2, y - 11, 7, 13),
                        QBrush(accent),
                    )
            y += line_h

    def _paint_progress_line(self, p: QPainter, accent: QColor,
                             accent2: QColor) -> None:
        # A 3px glowing line near the bottom. Track is very faint; fill
        # is a gradient between the two accents. A single bright "head"
        # rides the leading edge so the fill reads as a beam, not a bar.
        bar_w = 460
        bar_h = 2
        x = (self._W - bar_w) / 2
        y = self._H - 66

        # Track.
        track = QColor(accent)
        track.setAlpha(40)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(track))
        p.drawRoundedRect(QRectF(x, y, bar_w, bar_h), 1.0, 1.0)

        # Fill.
        fill_w = bar_w * max(0.0, min(1.0, self._progress))
        lg = QLinearGradient(x, y, x + fill_w, y)
        c0 = QColor(accent2); c0.setAlpha(200)
        c1 = QColor(accent);  c1.setAlpha(255)
        lg.setColorAt(0.0, c0)
        lg.setColorAt(1.0, c1)
        p.setBrush(QBrush(lg))
        p.drawRoundedRect(QRectF(x, y, fill_w, bar_h), 1.0, 1.0)

        # Leading "head" — small glowing dot at the end of the fill.
        if fill_w > 2:
            head_x = x + fill_w
            head_grad = QRadialGradient(QPointF(head_x, y + bar_h / 2), 10)
            hc = QColor(accent); hc.setAlpha(255)
            head_grad.setColorAt(0.0, hc)
            hc2 = QColor(accent); hc2.setAlpha(0)
            head_grad.setColorAt(1.0, hc2)
            p.setBrush(QBrush(head_grad))
            p.drawEllipse(QPointF(head_x, y + bar_h / 2), 9, 9)

        # Percentage label, monospace, under the bar.
        mono = QFont("Consolas", 9)
        mono.setWeight(QFont.Weight.DemiBold)
        mono.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 135)
        p.setFont(mono)
        label = f"{int(self._progress * 100):>3d}%  ::  LOADING"
        fm = p.fontMetrics()
        lw = fm.horizontalAdvance(label)
        dim_col = QColor(accent); dim_col.setAlpha(170)
        p.setPen(QPen(dim_col))
        p.drawText(QPointF((self._W - lw) / 2, y + 22), label)

    def _paint_corner_hud(self, p: QPainter, dim: QColor, accent: QColor) -> None:
        # Tiny HUD labels in the corners — pure atmosphere, but it
        # reliably sells the "developer tool" identity.
        mono = QFont("Consolas", 8)
        mono.setWeight(QFont.Weight.DemiBold)
        mono.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 150)
        p.setFont(mono)

        dim_c = QColor(dim); dim_c.setAlpha(200)
        acc_c = QColor(accent); acc_c.setAlpha(210)

        pad = 18
        p.setPen(QPen(dim_c))
        p.drawText(QPointF(pad, pad + 4), "NET.ENGINE  //  v1.1.0")
        p.setPen(QPen(acc_c))
        p.drawText(QPointF(pad, pad + 20), "● SECURE  ●  LOCAL")

        # Right side — ticks that change every second so it feels live.
        frame = int(self._elapsed_s * 60) % 9999
        right_text = f"FRAME {frame:04d}"
        fm = p.fontMetrics()
        rw = fm.horizontalAdvance(right_text)
        p.setPen(QPen(dim_c))
        p.drawText(QPointF(self._W - rw - pad, pad + 4), right_text)

        hint = "HOLD  ·  BOOTING CORE SERVICES"
        hw = fm.horizontalAdvance(hint)
        p.setPen(QPen(acc_c))
        p.drawText(QPointF(self._W - hw - pad, pad + 20), hint)

        # Tiny L-shaped corner crosshairs for the tech-UI look.
        pen = QPen(QColor(accent.red(), accent.green(), accent.blue(), 140), 1.4)
        p.setPen(pen)
        arm = 14
        m = 10
        corners = [
            (m, m, 1, 1),
            (self._W - m, m, -1, 1),
            (m, self._H - m, 1, -1),
            (self._W - m, self._H - m, -1, -1),
        ]
        for cx, cy, sx, sy in corners:
            p.drawLine(QPointF(cx, cy), QPointF(cx + arm * sx, cy))
            p.drawLine(QPointF(cx, cy), QPointF(cx, cy + arm * sy))
