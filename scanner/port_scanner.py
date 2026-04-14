"""Fast TCP port scanner using a thread pool."""

import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional


_CONNECT_TIMEOUT = 0.5  # seconds per port attempt


def scan_port(ip: str, port: int, timeout: float = _CONNECT_TIMEOUT) -> bool:
    """Return True if the TCP port is open."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((ip, port)) == 0
    except Exception:
        return False


def scan_ports(
    ip: str,
    ports: list[int],
    max_workers: int = 50,
    timeout: float = _CONNECT_TIMEOUT,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    stop_event=None,
) -> list[int]:
    """
    Scan a list of TCP ports on an IP address concurrently.

    Args:
        ip:          Target IP address.
        ports:       List of port numbers to scan.
        max_workers: Thread pool size.
        timeout:     Per-port connection timeout in seconds.
        progress_cb: Optional callback(scanned_count, total_count).
        stop_event:  threading.Event; scanning stops when set.

    Returns:
        Sorted list of open port numbers.
    """
    open_ports: list[int] = []
    total = len(ports)
    done = 0

    with ThreadPoolExecutor(max_workers=min(max_workers, total or 1)) as pool:
        futures = {pool.submit(scan_port, ip, p, timeout): p for p in ports}
        for future in as_completed(futures):
            if stop_event and stop_event.is_set():
                # Cancel remaining (best effort)
                for f in futures:
                    f.cancel()
                break
            port = futures[future]
            done += 1
            if progress_cb:
                progress_cb(done, total)
            try:
                if future.result():
                    open_ports.append(port)
            except Exception:
                pass

    return sorted(open_ports)
