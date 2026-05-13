"""
Serial / UART session, modelled after :class:`scanner.ssh_client.SSHSession`.

Public surface mirrors :class:`SSHSession` so the same TerminalWidget
attach path can drive either backend:

  * ``start(profile)`` opens the COM port via pyserial.
  * ``send(data)``     writes bytes to the port (line endings are NOT
                       transformed here — the terminal widget owns that
                       so we can match PuTTY's per-session CR/LF/CRLF
                       toggle without re-encoding bytes twice).
  * ``read_loop(cb, on_close)``
                       background-thread blocking read; emits each
                       chunk to ``cb`` and calls ``on_close`` exactly
                       once when the loop exits.
  * ``resize(cols,rows)``
                       no-op (serial has no PTY size). Kept for API
                       parity with SSHSession.
  * ``close()``        idempotent; safe from any thread.
  * ``is_open``        property.

Lifecycle safety follows the same rules as SSHSession:

  * Starting a brand-new instance is the only legal use; reusing one
    after close() raises.
  * ``close()`` sets the stop event before tearing the port down so a
    concurrent ``read_loop`` exits cleanly.
  * ``read_loop()`` swallows every transport-level error pyserial can
    raise and ends the loop gracefully — a yanked USB-UART or a
    closed handle does not crash the worker thread.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

try:
    import serial                   # pyserial
    from serial.tools import list_ports as _list_ports
    HAS_PYSERIAL = True
except ImportError:                                          # pragma: no cover
    serial = None                                            # type: ignore[assignment]
    _list_ports = None                                       # type: ignore[assignment]
    HAS_PYSERIAL = False


# ── Constants used by the GUI form & profile validation ──────────────────────

BAUD_PRESETS: tuple[int, ...] = (
    300, 1200, 2400, 4800, 9600, 19200, 38400, 57600,
    115200, 230400, 460800, 921600,
)

DATA_BITS_OPTIONS: tuple[int, ...] = (5, 6, 7, 8)
STOP_BITS_OPTIONS: tuple[float, ...] = (1.0, 1.5, 2.0)
PARITY_OPTIONS: tuple[str, ...] = ("none", "even", "odd", "mark", "space")
FLOW_OPTIONS: tuple[str, ...] = ("none", "rts_cts", "xon_xoff", "dsr_dtr")
LINE_ENDINGS: tuple[str, ...] = ("cr", "lf", "crlf")


def _parity_to_pyserial(name: str):
    if not HAS_PYSERIAL:
        return None
    return {
        "none":  serial.PARITY_NONE,
        "even":  serial.PARITY_EVEN,
        "odd":   serial.PARITY_ODD,
        "mark":  serial.PARITY_MARK,
        "space": serial.PARITY_SPACE,
    }.get(name.lower(), serial.PARITY_NONE)


def _stopbits_to_pyserial(value: float):
    if not HAS_PYSERIAL:
        return None
    if abs(value - 1.5) < 0.01:
        return serial.STOPBITS_ONE_POINT_FIVE
    if abs(value - 2.0) < 0.01:
        return serial.STOPBITS_TWO
    return serial.STOPBITS_ONE


def _bytesize_to_pyserial(value: int):
    if not HAS_PYSERIAL:
        return None
    return {
        5: serial.FIVEBITS,
        6: serial.SIXBITS,
        7: serial.SEVENBITS,
        8: serial.EIGHTBITS,
    }.get(int(value), serial.EIGHTBITS)


# ── Error translation ────────────────────────────────────────────────────────

def _humanise_open_error(port_name: str, exc: BaseException) -> str:
    """
    Turn a pyserial / OS exception from ``serial.Serial(port=…)`` into a
    short, readable single-line message for the terminal banner and the
    saved-session log.

    Falls back to ``str(exc)`` when the exception text doesn't match a
    known pattern — pyserial's own ``SerialException`` formatting is
    usually readable enough.
    """
    name = port_name or "(unset)"
    text = str(exc) or type(exc).__name__
    low = text.lower()

    # Windows: pyserial wraps the OS error inside SerialException's str.
    if "filenotfounderror" in low or "could not find" in low or \
       "the system cannot find the file specified" in low:
        return f"Port '{name}' was not found. Try Refresh."
    if ("access is denied" in low
            or "permissionerror" in low
            or "permission denied" in low):
        return f"Port '{name}' is in use or permission denied."
    if "semaphore timeout period has expired" in low:
        return (
            f"Port '{name}' did not respond — the device may be "
            f"unplugged or stuck."
        )
    if "the i/o operation has been aborted" in low:
        return f"I/O on '{name}' was aborted (port closed or unplugged)."
    if "invalid handle" in low:
        return f"Port '{name}' returned an invalid handle (driver issue)."

    # POSIX
    if "no such file or directory" in low:
        return f"Port '{name}' does not exist."
    if "device or resource busy" in low:
        return f"Port '{name}' is busy — another process has it open."

    # Fall through — pyserial's own message is usually fine.
    return text


# ── Port discovery ───────────────────────────────────────────────────────────

@dataclass
class SerialPortInfo:
    device: str          # "COM3", "/dev/ttyUSB0"
    description: str     # human-friendly description from the OS
    hwid: str            # USB VID/PID etc.

    @property
    def label(self) -> str:
        if self.description and self.description.lower() != "n/a":
            return f"{self.device} — {self.description}"
        return self.device


def list_serial_ports() -> list[SerialPortInfo]:
    """
    Enumerate COM/UART ports visible to the OS.

    Returns an empty list if pyserial is unavailable or the OS reports
    nothing — never raises. Sorted by device name so the dropdown order
    is stable across refreshes.
    """
    if not HAS_PYSERIAL or _list_ports is None:
        return []
    try:
        raw = list(_list_ports.comports())
    except Exception:
        return []
    out: list[SerialPortInfo] = []
    for entry in raw:
        try:
            out.append(SerialPortInfo(
                device=str(getattr(entry, "device", "") or ""),
                description=str(getattr(entry, "description", "") or ""),
                hwid=str(getattr(entry, "hwid", "") or ""),
            ))
        except Exception:
            continue
    out.sort(key=lambda p: p.device.lower())
    return out


# ── Connection profile ───────────────────────────────────────────────────────

@dataclass
class SerialProfile:
    name: str = ""
    port: str = ""              # "COM3", "/dev/ttyUSB0"
    baud: int = 115200
    data_bits: int = 8
    stop_bits: float = 1.0
    parity: str = "none"        # one of PARITY_OPTIONS
    flow_control: str = "none"  # one of FLOW_OPTIONS
    line_ending: str = "crlf"   # cr / lf / crlf — what Enter sends
    local_echo: bool = False
    favorite: bool = False
    last_connected: str = ""

    # Marker used by the GUI layer to discriminate at a glance without
    # an isinstance import cycle.
    kind: str = field(default="serial", init=False, repr=False)

    def to_dict(self) -> dict:
        return {
            "kind": "serial",
            "name": self.name,
            "port": self.port,
            "baud": int(self.baud),
            "data_bits": int(self.data_bits),
            "stop_bits": float(self.stop_bits),
            "parity": self.parity,
            "flow_control": self.flow_control,
            "line_ending": self.line_ending,
            "local_echo": bool(self.local_echo),
            "favorite": bool(self.favorite),
            "last_connected": self.last_connected,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SerialProfile":
        return cls(
            name=d.get("name", ""),
            port=d.get("port", ""),
            baud=int(d.get("baud", 115200) or 115200),
            data_bits=int(d.get("data_bits", 8) or 8),
            stop_bits=float(d.get("stop_bits", 1.0) or 1.0),
            parity=str(d.get("parity", "none") or "none"),
            flow_control=str(d.get("flow_control", "none") or "none"),
            line_ending=str(d.get("line_ending", "crlf") or "crlf"),
            local_echo=bool(d.get("local_echo", False)),
            favorite=bool(d.get("favorite", False)),
            last_connected=str(d.get("last_connected", "") or ""),
        )

    def summary(self) -> str:
        """Short one-line label e.g. 'COM3 @ 115200 8N1'."""
        parity_letter = {
            "none": "N", "even": "E", "odd": "O",
            "mark": "M", "space": "S",
        }.get((self.parity or "none").lower(), "N")
        stop = "1" if self.stop_bits == 1.0 else (
            "1.5" if abs(self.stop_bits - 1.5) < 0.01 else "2"
        )
        return (
            f"{self.port or '—'} @ {self.baud} "
            f"{self.data_bits}{parity_letter}{stop}"
        )


# ── Interactive serial session ────────────────────────────────────────────────

class SerialSession:
    """
    pyserial-backed serial port wrapper with the same interface as
    :class:`SSHSession`. The TerminalWidget treats both as duck-typed
    objects: ``is_open``, ``send``, ``read_loop``, ``resize`` (no-op),
    ``close``.
    """

    def __init__(self):
        self._port = None                           # serial.Serial | None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._closed = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @property
    def is_open(self) -> bool:
        port = self._port
        if port is None:
            return False
        try:
            return bool(port.is_open) and not self._closed
        except Exception:
            return False

    def start(self, profile: SerialProfile, timeout: float = 5.0) -> None:
        """
        Open the serial port described by ``profile``.

        Raises a clean, single-line ``RuntimeError`` (or ``ValueError``
        for input validation) on every error path. The original
        pyserial / OS exception type is wrapped so callers get a
        message safe to render in the terminal banner without leaking
        a Python traceback. Common cases:

          * "Port 'COM7' was not found." (FileNotFoundError)
          * "Port 'COM3' is in use by another application."
            (PermissionError / SerialException with "Access is denied")
          * "Permission denied opening 'COM3'." (POSIX EACCES)

        Anything not recognised falls back to the raw
        ``SerialException`` text, which pyserial already formats in
        a readable way.
        """
        if not HAS_PYSERIAL:
            raise RuntimeError(
                "pyserial is not installed — install it with "
                "`pip install pyserial`"
            )
        if not profile.port:
            raise ValueError("Serial port is required (e.g. COM3, /dev/ttyUSB0).")
        try:
            baud = int(profile.baud or 0)
        except (TypeError, ValueError):
            raise ValueError(
                f"Baud rate must be a whole number — got {profile.baud!r}."
            )
        if baud <= 0:
            raise ValueError("Baud rate must be a positive integer.")

        with self._lock:
            if self._port is not None:
                raise RuntimeError(
                    "SerialSession is already started; create a new one."
                )
            if self._closed:
                raise RuntimeError(
                    "SerialSession has been closed; create a new one."
                )

        flow = (profile.flow_control or "none").lower()

        try:
            port = serial.Serial(
                port=profile.port,
                baudrate=baud,
                bytesize=_bytesize_to_pyserial(profile.data_bits),
                parity=_parity_to_pyserial(profile.parity),
                stopbits=_stopbits_to_pyserial(profile.stop_bits),
                # We poll with a short read timeout instead of using
                # blocking reads so close() can unblock the loop
                # promptly — pyserial's blocking reads on Windows can
                # take seconds to honor a port close request.
                timeout=0.05,
                write_timeout=max(0.5, timeout),
                xonxoff=(flow == "xon_xoff"),
                rtscts=(flow == "rts_cts"),
                dsrdtr=(flow == "dsr_dtr"),
            )
        except Exception as exc:
            raise RuntimeError(_humanise_open_error(profile.port, exc)) from exc

        with self._lock:
            if self._closed:
                try:
                    port.close()
                except Exception:
                    pass
                raise RuntimeError("SerialSession closed during start()")
            self._port = port
            self._stop.clear()

    def send(self, data: bytes | str) -> None:
        port = self._port
        if port is None or self._closed:
            return
        if isinstance(data, str):
            data = data.encode("utf-8", errors="replace")
        try:
            if not bool(port.is_open):
                return
        except Exception:
            return
        try:
            port.write(data)
            try:
                port.flush()
            except Exception:
                pass
        except Exception:
            # The port could have been pulled mid-write (USB-UART
            # unplug). The read loop will observe and call on_close.
            pass

    def resize(self, cols: int, rows: int) -> None:
        # No PTY on a serial line — nothing to size. Kept for parity
        # with SSHSession so the terminal widget can call resize on
        # any backend without branching.
        return

    def read_loop(
        self,
        callback: Callable[[bytes], None],
        on_close: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        Blocking read loop — call from a worker thread.

        Loops at ~50ms cadence pulling whatever is in the pyserial
        in_waiting buffer. Exits cleanly on ``close()``, on a serial
        exception (port pulled, closed by another thread), or on
        repeated zero-length reads following a port close.
        """
        port = self._port
        try:
            if port is None:
                return
            while not self._stop.is_set():
                try:
                    if not port.is_open:
                        break
                except Exception:
                    break

                try:
                    waiting = port.in_waiting
                except Exception:
                    # Port handle invalidated (unplug) — exit.
                    break

                if waiting:
                    try:
                        data = port.read(waiting)
                    except Exception:
                        break
                    if data:
                        try:
                            callback(data)
                        except Exception:
                            pass
                    continue

                # Nothing buffered. Issue a tiny blocking read so we
                # don't spin the CPU; the 0.05s read timeout set in
                # start() makes this return quickly. Any data also
                # arrives via this call when it slips in between
                # in_waiting checks.
                try:
                    data = port.read(1)
                except Exception:
                    break
                if data:
                    try:
                        callback(data)
                    except Exception:
                        pass
                # Use the stop event as the throttle so close() can
                # unblock us immediately.
                if self._stop.wait(0.01):
                    break
        except Exception:
            pass
        finally:
            if on_close is not None:
                try:
                    on_close()
                except Exception:
                    pass

    def close(self) -> None:
        """Idempotent shutdown; safe from any thread."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            port = self._port
            self._port = None

        if port is not None:
            try:
                port.close()
            except Exception:
                pass
        # Tiny grace pause — pyserial on Windows occasionally returns
        # before the COM handle is fully released.
        time.sleep(0)
