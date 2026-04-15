"""
Small defensive helpers for Qt object lifetime management.

PyQt widgets are backed by C++ objects that can be destroyed before
the Python wrapper is garbage-collected. Any call that reaches the
C++ side on a destroyed wrapper raises ``RuntimeError: wrapped C/C++
object of type X has been deleted``. These helpers centralise the
swallow-RuntimeError-and-move-on pattern that every async callback,
timer callback, and worker-signal slot in this codebase needs.

Usage examples
--------------

    from gui.qt_safety import safe_call, stop_timer, is_alive

    # Inside an async callback:
    def _on_result(self, payload):
        if not is_alive(self):
            return
        safe_call(self._label.setText, payload.summary)

    # In shutdown():
    stop_timer(self._refresh_timer)
"""

from __future__ import annotations

from typing import Any, Callable, Optional


def is_alive(obj: Any) -> bool:
    """
    True if ``obj`` is a live Qt wrapper (or any non-Qt Python object).

    Works by probing a cheap attribute of every QObject — ``objectName()``
    — and catching the RuntimeError that PyQt raises when the underlying
    C++ side has been deleted. If the object is not a Qt wrapper at all,
    we still return True (treat plain Python objects as live).

    Prefer this over direct ``sip.isdeleted`` checks so the code works
    on both PyQt6 and PySide6 without an import.
    """
    if obj is None:
        return False
    try:
        obj_name = getattr(obj, "objectName", None)
        if obj_name is None:
            return True
        obj_name()
        return True
    except RuntimeError:
        return False
    except Exception:
        return True


def safe_call(fn: Optional[Callable], *args, **kwargs) -> Any:
    """
    Call ``fn(*args, **kwargs)`` and swallow the RuntimeError that
    PyQt raises on wrapped-C++-object-deleted.

    Returns the function's return value on success, or ``None`` if
    the call was skipped (``fn`` is None) or raised RuntimeError.
    Any other exception is propagated so real bugs still surface.
    """
    if fn is None:
        return None
    try:
        return fn(*args, **kwargs)
    except RuntimeError:
        return None


def stop_timer(timer) -> None:
    """
    Stop a QTimer idempotently. A no-op if the timer is None or has
    already been destroyed. Safe to call from shutdown paths where
    the order of teardown may not guarantee the timer still exists.
    """
    if timer is None:
        return
    try:
        timer.stop()
    except RuntimeError:
        pass
    except Exception:
        pass


def disconnect_signal(signal) -> None:
    """
    Disconnect every receiver of a bound Qt signal, ignoring the
    ``TypeError`` that Qt raises when the signal has no receivers
    and the ``RuntimeError`` that fires when the owning object has
    been destroyed. Useful in shutdown paths where we want to break
    signal connections before letting Qt cascade-delete the children.
    """
    if signal is None:
        return
    try:
        signal.disconnect()
    except (TypeError, RuntimeError):
        pass
    except Exception:
        pass
