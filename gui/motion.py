"""
Net Engine — global interaction & motion system.

This module is the single source of truth for the application's
animation language. Every interactive element in the UI talks to it so
hover, focus, press and transition feedback all share the same timing,
easing and visual character.

Design principles
-----------------
* **Subtle, intentional, premium** — animations feel like a high-end
  developer tool, not a website carousel. Short durations, smooth
  decelerations, no bouncing.
* **GPU friendly** — all effects route through `QPropertyAnimation`,
  `QVariantAnimation`, `QGraphicsDropShadowEffect` and lightweight
  child overlays. No heavy compositing, no per-frame Python loops on
  every widget.
* **Lazy by default** — effects are only attached on demand (the first
  hover, the first focus). Idle widgets cost nothing.
* **Theme aware** — pulls colours from `gui.themes.theme()` and
  re-resolves them on theme change.
* **Opt-in via a single global install** — `install_global_motion(app)`
  walks the widget tree and instruments standard controls (buttons,
  inputs, combos, table headers) automatically. Custom widgets call
  the helpers directly.

Public surface
--------------
    MOTION                          tuning constants (durations, easing)
    install_global_motion(app)      one-line app-wide setup
    attach_button_motion(btn)       hover glow + press pulse + ripple
    attach_focus_glow(widget)       focus drop-shadow on inputs/combos
    Ripple                          standalone ripple overlay class
    cross_fade(stack, new_index)    fade swap between QStackedWidget pages
    fade_in(widget, duration)       reveal helper for first-mount
    pulse_color(label, color, dur)  one-shot text colour flash
    PulseGlow                       breathing drop shadow used by logos
"""

from __future__ import annotations

import math
from typing import Iterable, Optional

from PyQt6.QtCore import (
    QEasingCurve, QEvent, QObject, QPoint, QPointF, QPropertyAnimation,
    QRectF, QSize, QTimer, QVariantAnimation, Qt,
)
from PyQt6.QtGui import (
    QColor, QPainter, QBrush, QPen, QPaintEvent,
)
from PyQt6.QtWidgets import (
    QApplication, QGraphicsDropShadowEffect, QLineEdit, QComboBox,
    QPlainTextEdit, QPushButton, QSpinBox, QStackedWidget, QTextEdit,
    QToolButton, QWidget,
)


# ── Tuning constants ──────────────────────────────────────────────────────────


class MOTION:
    """Central tuning knobs. Tweak here to retune the entire app."""

    # Durations (ms) — short and snappy. Anything > 220ms feels sluggish
    # for hover; anything < 80ms feels broken.
    HOVER_IN          = 140
    HOVER_OUT         = 180
    PRESS             = 90
    RELEASE           = 160
    FOCUS_IN          = 160
    FOCUS_OUT         = 200
    PAGE_FADE         = 220
    RIPPLE            = 480
    INTRO             = 360
    PULSE             = 1800   # full breath cycle for logo glow

    # Easing — sharp deceleration for "in", gentle for "out".
    EASE_IN           = QEasingCurve.Type.OutCubic
    EASE_OUT          = QEasingCurve.Type.InOutQuad
    EASE_PRESS        = QEasingCurve.Type.OutQuad
    EASE_PAGE         = QEasingCurve.Type.InOutCubic
    EASE_RIPPLE       = QEasingCurve.Type.OutCubic

    # Visual amplitudes — kept conservative so the UI never feels noisy.
    HOVER_BLUR        = 18.0
    HOVER_BLUR_PRIM   = 28.0   # primary / accent buttons get a touch more
    HOVER_ALPHA       = 110
    HOVER_ALPHA_PRIM  = 165
    PRESS_ALPHA       = 220
    FOCUS_BLUR        = 22.0
    FOCUS_ALPHA       = 165


# Property name we set on widgets we've already instrumented so the
# global event filter never double-installs.
_POLISH_PROP = "_ne_motion_polished"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _theme_colors():
    """Late import so this module is safe to import before the theme
    manager is constructed."""
    from gui.themes import theme
    return theme()


def _is_primary_button(btn: QWidget) -> bool:
    name = btn.objectName() or ""
    return name in ("btn_scan", "btn_primary")


def _is_danger_button(btn: QWidget) -> bool:
    name = btn.objectName() or ""
    return name in ("btn_stop", "btn_danger")


def _hover_color_for(widget: QWidget) -> QColor:
    t = _theme_colors()
    if _is_danger_button(widget):
        return QColor(t.red)
    if _is_primary_button(widget):
        return QColor(t.accent)
    # Inputs / generic buttons → cool accent at lower intensity.
    return QColor(t.accent)


# ── Animated drop shadow wrapper ──────────────────────────────────────────────


class _GlowEffect(QGraphicsDropShadowEffect):
    """
    A drop-shadow effect with helpers for animated alpha + blur.

    We never animate the QColor object directly (Qt has no animatable
    QColor property on the effect); instead we keep the base RGB and
    drive a separate QVariantAnimation that updates `setColor()` each
    tick. Blur radius is animated via a regular QPropertyAnimation.
    """

    def __init__(self, parent: QWidget, base_color: QColor):
        super().__init__(parent)
        self.setOffset(0, 0)
        self.setBlurRadius(0.0)
        self._base = QColor(base_color)
        c = QColor(base_color)
        c.setAlpha(0)
        self.setColor(c)

    def set_base_color(self, color: QColor) -> None:
        self._base = QColor(color)

    def update_alpha(self, alpha: int) -> None:
        c = QColor(self._base)
        c.setAlpha(max(0, min(255, alpha)))
        self.setColor(c)


# ── Button motion ─────────────────────────────────────────────────────────────


class _ButtonMotionFilter(QObject):
    """
    Per-button event filter that drives hover glow, press pulse and a
    click ripple. One instance is shared across all buttons — Qt routes
    events to whichever widget originated them.
    """

    def __init__(self, parent: QObject):
        super().__init__(parent)

    # The actual machinery hangs off the widget (effect, animations).
    # We just react to events.
    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if not isinstance(obj, QWidget):
            return False

        et = event.type()

        if et == QEvent.Type.Enter:
            _hover_in(obj)
        elif et == QEvent.Type.Leave:
            _hover_out(obj)
        elif et == QEvent.Type.MouseButtonPress:
            _press(obj)
            try:
                pos = event.position().toPoint()  # type: ignore[attr-defined]
            except Exception:
                pos = obj.rect().center()
            _spawn_ripple(obj, pos)
        elif et == QEvent.Type.MouseButtonRelease:
            _release(obj)
        elif et == QEvent.Type.FocusIn:
            # Focus glow uses the same effect, slightly stronger.
            _focus_in(obj)
        elif et == QEvent.Type.FocusOut:
            _focus_out(obj)
        return False  # never consume — pass through to the widget


# Singleton — installed once via install_global_motion().
_BTN_FILTER: Optional[_ButtonMotionFilter] = None


def _ensure_glow(widget: QWidget) -> _GlowEffect:
    """Lazily create and attach the per-widget glow effect."""
    eff = widget.graphicsEffect()
    if isinstance(eff, _GlowEffect):
        return eff
    if eff is not None:
        # Some widgets (sidebar mark, dialogs) install their own effect.
        # Don't fight them — return None sentinel via wrapping.
        return None  # type: ignore[return-value]
    glow = _GlowEffect(widget, _hover_color_for(widget))
    widget.setGraphicsEffect(glow)
    return glow


def _animate_blur(widget: QWidget, target: float, duration: int,
                  easing: QEasingCurve.Type) -> None:
    eff = widget.graphicsEffect()
    if not isinstance(eff, _GlowEffect):
        return
    anim_attr = "_ne_blur_anim"
    prev = getattr(widget, anim_attr, None)
    if prev is not None:
        try:
            prev.stop()
        except Exception:
            pass
    anim = QPropertyAnimation(eff, b"blurRadius", widget)
    anim.setDuration(duration)
    anim.setStartValue(float(eff.blurRadius()))
    anim.setEndValue(float(target))
    anim.setEasingCurve(QEasingCurve(easing))
    setattr(widget, anim_attr, anim)
    anim.start(QPropertyAnimation.DeletionPolicy.KeepWhenStopped)


def _animate_alpha(widget: QWidget, target: int, duration: int,
                   easing: QEasingCurve.Type) -> None:
    eff = widget.graphicsEffect()
    if not isinstance(eff, _GlowEffect):
        return
    anim_attr = "_ne_alpha_anim"
    prev = getattr(widget, anim_attr, None)
    if prev is not None:
        try:
            prev.stop()
        except Exception:
            pass
    start = eff.color().alpha()
    anim = QVariantAnimation(widget)
    anim.setDuration(duration)
    anim.setStartValue(int(start))
    anim.setEndValue(int(target))
    anim.setEasingCurve(QEasingCurve(easing))
    anim.valueChanged.connect(lambda v: eff.update_alpha(int(v)))
    setattr(widget, anim_attr, anim)
    anim.start(QVariantAnimation.DeletionPolicy.KeepWhenStopped)


def _hover_in(widget: QWidget) -> None:
    glow = _ensure_glow(widget)
    if glow is None:
        return
    # Re-resolve the colour each enter so theme changes are picked up
    # without re-instrumenting every widget.
    glow.set_base_color(_hover_color_for(widget))
    if _is_primary_button(widget):
        target_blur = MOTION.HOVER_BLUR_PRIM
        target_alpha = MOTION.HOVER_ALPHA_PRIM
    else:
        target_blur = MOTION.HOVER_BLUR
        target_alpha = MOTION.HOVER_ALPHA
    _animate_blur(widget, target_blur, MOTION.HOVER_IN, MOTION.EASE_IN)
    _animate_alpha(widget, target_alpha, MOTION.HOVER_IN, MOTION.EASE_IN)


def _hover_out(widget: QWidget) -> None:
    if widget.hasFocus():
        # Hover-out on a focused input shouldn't kill the focus glow.
        _focus_in(widget)
        return
    _animate_blur(widget, 0.0, MOTION.HOVER_OUT, MOTION.EASE_OUT)
    _animate_alpha(widget, 0, MOTION.HOVER_OUT, MOTION.EASE_OUT)


def _press(widget: QWidget) -> None:
    glow = _ensure_glow(widget)
    if glow is None:
        return
    glow.set_base_color(_hover_color_for(widget))
    _animate_blur(widget, MOTION.HOVER_BLUR_PRIM, MOTION.PRESS, MOTION.EASE_PRESS)
    _animate_alpha(widget, MOTION.PRESS_ALPHA, MOTION.PRESS, MOTION.EASE_PRESS)


def _release(widget: QWidget) -> None:
    if widget.underMouse():
        _hover_in(widget)
    else:
        _hover_out(widget)


def _focus_in(widget: QWidget) -> None:
    glow = _ensure_glow(widget)
    if glow is None:
        return
    glow.set_base_color(_hover_color_for(widget))
    _animate_blur(widget, MOTION.FOCUS_BLUR, MOTION.FOCUS_IN, MOTION.EASE_IN)
    _animate_alpha(widget, MOTION.FOCUS_ALPHA, MOTION.FOCUS_IN, MOTION.EASE_IN)


def _focus_out(widget: QWidget) -> None:
    if widget.underMouse():
        _hover_in(widget)
        return
    _animate_blur(widget, 0.0, MOTION.FOCUS_OUT, MOTION.EASE_OUT)
    _animate_alpha(widget, 0, MOTION.FOCUS_OUT, MOTION.EASE_OUT)


# ── Ripple overlay ────────────────────────────────────────────────────────────


class Ripple(QWidget):
    """
    Lightweight click-ripple overlay.

    A single instance is parented to its host widget on first use. The
    overlay covers the host, paints expanding circles whenever
    `start(point)` is called and clears itself when no ripples remain.
    Mouse events pass through to the host.
    """

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._ripples: list[dict] = []
        self._timer = QTimer(self)
        self._timer.setInterval(16)  # ~60fps
        self._timer.timeout.connect(self._tick)
        self.resize(parent.size())
        parent.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self.parent() and event.type() == QEvent.Type.Resize:
            self.resize(self.parent().size())
        return False

    def start(self, pos: QPoint) -> None:
        # Cap simultaneous ripples to avoid runaway overdraw on rapid
        # clicks.
        if len(self._ripples) >= 4:
            self._ripples.pop(0)
        # Reach the far corner so the ripple always covers the button.
        w, h = self.width(), self.height()
        far = max(
            math.hypot(pos.x(), pos.y()),
            math.hypot(w - pos.x(), pos.y()),
            math.hypot(pos.x(), h - pos.y()),
            math.hypot(w - pos.x(), h - pos.y()),
        )
        self._ripples.append({
            "cx": pos.x(),
            "cy": pos.y(),
            "max_r": far,
            "phase": 0.0,
        })
        self.raise_()
        self.show()
        if not self._timer.isActive():
            self._timer.start()

    def _tick(self) -> None:
        step = 16.0 / float(MOTION.RIPPLE)
        survivors = []
        for r in self._ripples:
            r["phase"] += step
            if r["phase"] < 1.0:
                survivors.append(r)
        self._ripples = survivors
        if not self._ripples:
            self._timer.stop()
        self.update()

    def paintEvent(self, _ev: QPaintEvent) -> None:
        if not self._ripples:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Resolve colour on every paint so it tracks theme + button kind.
        host = self.parentWidget()
        base = _hover_color_for(host) if host is not None else QColor(0, 212, 255)
        for r in self._ripples:
            # Ease out cubic so the ripple decelerates as it expands.
            t = r["phase"]
            eased = 1 - pow(1 - t, 3)
            radius = eased * r["max_r"]
            alpha = int(150 * (1.0 - t))
            col = QColor(base)
            col.setAlpha(max(0, alpha))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(col))
            p.drawEllipse(QPointF(r["cx"], r["cy"]), radius, radius)
        p.end()


def _spawn_ripple(host: QWidget, pos: QPoint) -> None:
    overlay = host.findChild(Ripple, "_ne_ripple")
    if overlay is None:
        overlay = Ripple(host)
        overlay.setObjectName("_ne_ripple")
    overlay.start(pos)


# ── Public attach helpers ─────────────────────────────────────────────────────


def attach_button_motion(widget: QWidget) -> None:
    """
    Make a single widget participate in the motion system without
    relying on the global event filter — useful when you build a widget
    after install_global_motion has run.
    """
    if widget.property(_POLISH_PROP):
        return
    global _BTN_FILTER
    if _BTN_FILTER is None:
        _BTN_FILTER = _ButtonMotionFilter(QApplication.instance())
    widget.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
    widget.installEventFilter(_BTN_FILTER)
    widget.setProperty(_POLISH_PROP, True)


def attach_focus_glow(widget: QWidget) -> None:
    """Alias of attach_button_motion — focus animation lives there."""
    attach_button_motion(widget)


# ── Cross-fade page transition ────────────────────────────────────────────────


def cross_fade(stack: QStackedWidget, new_index: int,
               duration: int = MOTION.PAGE_FADE) -> None:
    """
    Smoothly fade between two pages of a QStackedWidget.

    Animates a temporary `windowOpacity` style on the outgoing widget
    via a `QGraphicsOpacityEffect`. The new page slides into place at
    the same time so the swap feels effortless rather than abrupt.

    Falls back to a plain `setCurrentIndex` if the requested index is
    already current or out of range.
    """
    cur = stack.currentIndex()
    if new_index == cur or new_index < 0 or new_index >= stack.count():
        stack.setCurrentIndex(new_index)
        return

    from PyQt6.QtWidgets import QGraphicsOpacityEffect

    new_widget = stack.widget(new_index)

    # Apply a fresh opacity effect to the incoming page only — keeping
    # the outgoing page intact during the swap avoids a visible flash.
    eff = QGraphicsOpacityEffect(new_widget)
    eff.setOpacity(0.0)
    new_widget.setGraphicsEffect(eff)

    stack.setCurrentIndex(new_index)

    anim = QPropertyAnimation(eff, b"opacity", new_widget)
    anim.setDuration(duration)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve(MOTION.EASE_PAGE))

    def _cleanup():
        # Drop the effect once the fade is complete so the page goes
        # back to its native rendering path (no graphics-effect cost).
        try:
            new_widget.setGraphicsEffect(None)
        except Exception:
            pass

    anim.finished.connect(_cleanup)
    # Keep a reference on the widget so the animation isn't GC'd.
    setattr(new_widget, "_ne_page_anim", anim)
    anim.start(QPropertyAnimation.DeletionPolicy.KeepWhenStopped)


def fade_in(widget: QWidget, duration: int = MOTION.INTRO) -> None:
    """One-shot reveal animation — used by the main window on launch."""
    from PyQt6.QtWidgets import QGraphicsOpacityEffect
    eff = QGraphicsOpacityEffect(widget)
    eff.setOpacity(0.0)
    widget.setGraphicsEffect(eff)

    anim = QPropertyAnimation(eff, b"opacity", widget)
    anim.setDuration(duration)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve(MOTION.EASE_IN))

    def _cleanup():
        try:
            widget.setGraphicsEffect(None)
        except Exception:
            pass

    anim.finished.connect(_cleanup)
    setattr(widget, "_ne_intro_anim", anim)
    anim.start(QPropertyAnimation.DeletionPolicy.KeepWhenStopped)


# ── Pulse glow (logo / brand) ─────────────────────────────────────────────────


class PulseGlow(QObject):
    """
    Drives a slow breathing animation on a QGraphicsDropShadowEffect.

    Used by the brand mark in the sidebar to make the logo feel alive
    without distracting the user. The cycle is intentionally slow
    (~1.8s) so it reads as ambient rather than blinking.
    """

    def __init__(self, target: QWidget, color: QColor,
                 base_blur: float = 8.0, peak_blur: float = 28.0,
                 base_alpha: int = 60, peak_alpha: int = 165,
                 period_ms: int = MOTION.PULSE):
        super().__init__(target)
        self._target = target
        self._effect = QGraphicsDropShadowEffect(target)
        self._effect.setOffset(0, 0)
        self._effect.setBlurRadius(base_blur)
        c = QColor(color)
        c.setAlpha(base_alpha)
        self._effect.setColor(c)
        target.setGraphicsEffect(self._effect)

        self._color = QColor(color)
        self._base_blur = base_blur
        self._peak_blur = peak_blur
        self._base_alpha = base_alpha
        self._peak_alpha = peak_alpha
        self._period = max(400, period_ms)
        self._phase = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(40)  # 25 fps — plenty for a slow breath
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def set_color(self, color) -> None:
        self._color = QColor(color)
        self._tick()

    def _tick(self) -> None:
        self._phase = (self._phase + 40.0 / self._period) % 1.0
        # Smooth in/out using a cosine — feels organic and avoids the
        # snap of a triangular wave.
        s = (1 - math.cos(self._phase * 2 * math.pi)) / 2  # 0..1
        blur = self._base_blur + (self._peak_blur - self._base_blur) * s
        alpha = int(self._base_alpha + (self._peak_alpha - self._base_alpha) * s)
        self._effect.setBlurRadius(blur)
        c = QColor(self._color)
        c.setAlpha(max(0, min(255, alpha)))
        self._effect.setColor(c)


# ── Global install ────────────────────────────────────────────────────────────


# Widget classes that should automatically participate in the motion
# system. Anything else is left alone so we never accidentally polish
# decorative labels or layout containers.
_AUTO_POLISH = (
    QPushButton,
    QToolButton,
    QLineEdit,
    QSpinBox,
    QComboBox,
)


def _polish_existing(root: QWidget) -> None:
    for w in root.findChildren(QWidget):
        if isinstance(w, _AUTO_POLISH):
            attach_button_motion(w)


class _NewWidgetWatcher(QObject):
    """
    Application-level event filter that polishes widgets the moment
    they're shown for the first time. Required because views build
    their controls lazily after `install_global_motion` runs.
    """

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Show and isinstance(obj, _AUTO_POLISH):
            if not obj.property(_POLISH_PROP):
                attach_button_motion(obj)
        return False


_WATCHER: Optional[_NewWidgetWatcher] = None


def install_global_motion(app: QApplication) -> None:
    """
    Wire the motion system into a running QApplication.

    * Polishes every existing button / input / combo
    * Installs a watcher that polishes future widgets on first show
    * Tightens the system cursor flash time so terminal carets feel
      crisp instead of sluggish
    """
    global _WATCHER, _BTN_FILTER

    if _BTN_FILTER is None:
        _BTN_FILTER = _ButtonMotionFilter(app)

    # Sweep current top-level widgets.
    for tl in app.topLevelWidgets():
        _polish_existing(tl)

    if _WATCHER is None:
        _WATCHER = _NewWidgetWatcher(app)
        app.installEventFilter(_WATCHER)

    # Crisp caret blink — Qt default is 1000ms which feels lazy.
    try:
        app.setCursorFlashTime(530)
    except Exception:
        pass
