"""
Continuous live-ping worker.

A QThread that runs `ping -t`-style indefinitely against a target,
emitting per-line output and accumulated stats. Used by the Monitor
page to track multiple targets simultaneously.
"""

from __future__ import annotations

import platform
import re
import subprocess
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal


_IS_WINDOWS = platform.system() == "Windows"
_NO_WINDOW = 0x08000000 if _IS_WINDOWS else 0


_TIME_RE = re.compile(r"time[=<](\d+(?:\.\d+)?)\s*ms", re.IGNORECASE)


class LivePingWorker(QThread):
    """
    Runs an OS ping against `target` until stop() is called.

    Signals:
        line(target, raw_line)
        stats(target, sent, lost, last_rtt)
        finished_target(target)
    """

    line            = pyqtSignal(str, str)
    stats           = pyqtSignal(str, int, int, str)
    finished_target = pyqtSignal(str)

    def __init__(self, target: str, parent=None):
        super().__init__(parent)
        self.target = target
        self._proc: Optional[subprocess.Popen] = None
        self._stop = False

    def stop(self) -> None:
        self._stop = True
        try:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
        except Exception:
            pass

    def run(self) -> None:
        if _IS_WINDOWS:
            cmd = ["ping", "-t", self.target]
        else:
            cmd = ["ping", self.target]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_NO_WINDOW,
            )
        except Exception as exc:
            self.line.emit(self.target, f"ping launch failed: {exc}")
            self.finished_target.emit(self.target)
            return

        sent = 0
        lost = 0
        last = "—"

        try:
            assert self._proc.stdout is not None
            for raw in self._proc.stdout:
                if self._stop:
                    break
                line = raw.strip()
                if not line:
                    continue
                self.line.emit(self.target, line)

                lower = line.lower()
                replied = ("reply from" in lower) or ("bytes from" in lower)
                timed_out = ("timed out" in lower) or ("unreachable" in lower)
                if replied:
                    sent += 1
                    m = _TIME_RE.search(line)
                    last = f"{m.group(1)} ms" if m else "<1 ms"
                    self.stats.emit(self.target, sent, lost, last)
                elif timed_out:
                    sent += 1
                    lost += 1
                    last = "timeout"
                    self.stats.emit(self.target, sent, lost, last)
        except Exception:
            pass
        finally:
            try:
                if self._proc and self._proc.poll() is None:
                    self._proc.terminate()
            except Exception:
                pass
            self.finished_target.emit(self.target)
