"""Port-to-service mapping and scan port presets."""

# ── Port presets ──────────────────────────────────────────────────────────────

DEFAULT_SCAN_PORTS: list[int] = [
    21, 22, 23, 25, 53, 80, 110, 135, 139, 143,
    443, 445, 993, 995, 1433, 3306, 3389, 5432, 5900, 8080,
]

EXTENDED_SCAN_PORTS: list[int] = [
    21, 22, 23, 25, 53, 67, 68, 80, 110, 111, 119,
    135, 137, 138, 139, 143, 161, 194, 443, 445, 465,
    514, 515, 587, 631, 993, 995, 1080, 1194, 1433, 1521,
    1883, 2049, 2181, 2375, 3000, 3268, 3306, 3389, 3690,
    4444, 4505, 5000, 5432, 5900, 5985, 6379, 6881, 7000,
    8080, 8443, 8888, 9000, 9090, 9200, 27017,
]

TOP_1000_PORTS: list[int] = list(range(1, 1025))

# ── Service name lookup ───────────────────────────────────────────────────────

_SERVICES: dict[int, str] = {
    20: "FTP-data",
    21: "FTP",
    22: "SSH",
    23: "Telnet",
    25: "SMTP",
    53: "DNS",
    67: "DHCP",
    68: "DHCP",
    69: "TFTP",
    80: "HTTP",
    88: "Kerberos",
    110: "POP3",
    111: "RPC",
    119: "NNTP",
    123: "NTP",
    135: "MSRPC",
    137: "NetBIOS-NS",
    138: "NetBIOS-DGM",
    139: "NetBIOS",
    143: "IMAP",
    161: "SNMP",
    162: "SNMP-Trap",
    179: "BGP",
    194: "IRC",
    389: "LDAP",
    443: "HTTPS",
    445: "SMB",
    465: "SMTPS",
    500: "IKE",
    514: "Syslog",
    515: "LPD",
    520: "RIP",
    587: "Submission",
    631: "IPP",
    636: "LDAPS",
    873: "rsync",
    902: "VMware",
    993: "IMAPS",
    995: "POP3S",
    1080: "SOCKS",
    1194: "OpenVPN",
    1433: "MS-SQL",
    1521: "Oracle DB",
    1723: "PPTP",
    1883: "MQTT",
    2049: "NFS",
    2181: "ZooKeeper",
    2375: "Docker",
    2376: "Docker TLS",
    3000: "Dev Server",
    3268: "LDAP GC",
    3306: "MySQL",
    3389: "RDP",
    3690: "SVN",
    4444: "Meterpreter",
    4505: "Salt Master",
    4506: "Salt Worker",
    5000: "UPnP/Flask",
    5432: "PostgreSQL",
    5900: "VNC",
    5985: "WinRM",
    5986: "WinRM-S",
    6379: "Redis",
    6443: "K8s API",
    6881: "BitTorrent",
    7000: "Cassandra",
    7001: "WebLogic",
    8080: "HTTP-Alt",
    8443: "HTTPS-Alt",
    8888: "Jupyter",
    9000: "SonarQube",
    9090: "Prometheus",
    9200: "Elasticsearch",
    9300: "Elasticsearch",
    10250: "Kubelet",
    27017: "MongoDB",
    27018: "MongoDB",
    50000: "SAP",
}


def get_service(port: int) -> str:
    """Return the common service name for a port, or 'port/tcp'."""
    return _SERVICES.get(port, f"{port}/tcp")


def describe_ports(ports: list[int]) -> str:
    """Format port list as 'port(SVC), ...' string."""
    if not ports:
        return "—"
    parts = []
    for p in sorted(ports):
        svc = _SERVICES.get(p)
        parts.append(f"{p} ({svc})" if svc else str(p))
    return ", ".join(parts)


def ports_short(ports: list[int]) -> str:
    """Short comma-separated port list for table cells."""
    if not ports:
        return "—"
    return ", ".join(str(p) for p in sorted(ports))
