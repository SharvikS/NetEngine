"""Network interface detection and subnet utilities."""

import socket
import struct
import platform
import subprocess
import re
from typing import Optional

import psutil


def get_local_ip() -> str:
    """Get the primary local IP by connecting to a public host (no packet sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def cidr_from_mask(netmask: str) -> int:
    """Convert dotted subnet mask to CIDR prefix length."""
    try:
        mask_int = struct.unpack(">I", socket.inet_aton(netmask))[0]
        return bin(mask_int).count("1")
    except Exception:
        return 24


def network_address(ip: str, netmask: str) -> str:
    """Compute the network address for a given IP and mask."""
    try:
        ip_int = struct.unpack(">I", socket.inet_aton(ip))[0]
        mask_int = struct.unpack(">I", socket.inet_aton(netmask))[0]
        return socket.inet_ntoa(struct.pack(">I", ip_int & mask_int))
    except Exception:
        return ip


def host_count(cidr: int) -> int:
    """Number of usable host addresses in a /{cidr} subnet."""
    if cidr >= 32:
        return 1
    if cidr <= 0:
        return 0
    return max(2 ** (32 - cidr) - 2, 0)


def get_all_interfaces() -> list[dict]:
    """Return list of IPv4 interfaces suitable for scanning."""
    results = []
    for iface_name, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family != socket.AF_INET:
                continue
            if addr.address in ("127.0.0.1", "0.0.0.0"):
                continue
            if not addr.netmask:
                continue
            try:
                cidr = cidr_from_mask(addr.netmask)
                network = network_address(addr.address, addr.netmask)
                count = host_count(cidr)

                # Cap extremely large ranges to /24 to keep scanning practical
                if count > 65534:
                    cidr = 24
                    octets = addr.address.split(".")
                    network = f"{octets[0]}.{octets[1]}.{octets[2]}.0"
                    count = 254

                results.append({
                    "name": iface_name,
                    "ip": addr.address,
                    "netmask": addr.netmask,
                    "cidr": cidr,
                    "network": network,
                    "host_count": count,
                    "display": f"{iface_name}  —  {addr.address}/{cidr}  ({count} hosts)",
                })
            except Exception:
                continue
    return results


def get_ip_range(network: str, cidr: int) -> list[str]:
    """Generate all usable host IPs in a subnet (excludes network and broadcast)."""
    try:
        base = struct.unpack(">I", socket.inet_aton(network))[0]
        count = host_count(cidr)
        return [socket.inet_ntoa(struct.pack(">I", base + i)) for i in range(1, count + 1)]
    except Exception:
        return []


def get_arp_cache() -> dict[str, str]:
    """
    Read the OS ARP cache and return {ip: mac} mapping.
    Does not require elevated privileges.
    """
    arp_map: dict[str, str] = {}
    try:
        result = subprocess.run(
            ["arp", "-a"],
            capture_output=True, text=True, timeout=5,
            creationflags=0x08000000 if platform.system() == "Windows" else 0,
        )
        output = result.stdout
    except Exception:
        return arp_map

    if platform.system() == "Windows":
        # "  192.168.1.1          aa-bb-cc-dd-ee-ff     dynamic"
        pattern = re.compile(
            r"\s*(\d{1,3}(?:\.\d{1,3}){3})\s+"
            r"([0-9a-fA-F]{2}(?:[:-][0-9a-fA-F]{2}){5})"
        )
        for line in output.splitlines():
            m = pattern.match(line)
            if m:
                ip = m.group(1)
                mac = m.group(2).replace("-", ":").lower()
                arp_map[ip] = mac
    else:
        # "hostname (192.168.1.1) at aa:bb:cc:dd:ee:ff [ether] ..."
        pattern = re.compile(
            r"\((\d{1,3}(?:\.\d{1,3}){3})\)\s+at\s+"
            r"([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})",
            re.IGNORECASE,
        )
        for line in output.splitlines():
            m = pattern.search(line)
            if m:
                ip = m.group(1)
                mac = m.group(2).lower()
                if mac not in ("<incomplete>", "ff:ff:ff:ff:ff:ff"):
                    arp_map[ip] = mac

    return arp_map


def resolve_hostname(ip: str, timeout: float = 1.0) -> str:
    """Reverse-DNS lookup with timeout. Returns empty string on failure."""
    try:
        socket.setdefaulttimeout(timeout)
        result = socket.gethostbyaddr(ip)
        return result[0]
    except Exception:
        return ""
    finally:
        socket.setdefaulttimeout(None)
