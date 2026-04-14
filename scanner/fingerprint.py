"""
Best-effort device fingerprinting: MAC vendor lookup and OS hints from TTL.
No external API calls — uses a compiled OUI prefix table.
"""

# ── OUI prefix → vendor (first 6 hex chars, uppercase, no separators) ─────────
# A curated list covering ~80% of consumer/enterprise devices seen in practice.
_OUI: dict[str, str] = {
    # Apple
    "000393": "Apple", "000A27": "Apple", "000A95": "Apple", "000D93": "Apple",
    "001124": "Apple", "001451": "Apple", "0016CB": "Apple", "0017F2": "Apple",
    "0019E3": "Apple", "001CB3": "Apple", "001D4F": "Apple", "001E52": "Apple",
    "001EC2": "Apple", "001F5B": "Apple", "001FF3": "Apple", "0021E9": "Apple",
    "002241": "Apple", "002312": "Apple", "002332": "Apple", "00236C": "Apple",
    "002436": "Apple", "002500": "Apple", "00254B": "Apple", "0025BC": "Apple",
    "002608": "Apple", "00264A": "Apple", "0026B9": "Apple", "0026BB": "Apple",
    "3C0754": "Apple", "3C15C2": "Apple", "4C8D79": "Apple", "60D9A0": "Apple",
    "70CD60": "Apple", "7C6D62": "Apple", "A4B197": "Apple", "A8667F": "Apple",
    "B8E856": "Apple", "D0E140": "Apple", "F0CB38": "Apple", "F8027B": "Apple",
    # Samsung
    "0012FB": "Samsung", "001599": "Samsung", "0017C9": "Samsung", "001A8A": "Samsung",
    "001D25": "Samsung", "001EE1": "Samsung", "002119": "Samsung", "002399": "Samsung",
    "002566": "Samsung", "3425C4": "Samsung", "5001BB": "Samsung", "8425DB": "Samsung",
    "CC07AB": "Samsung", "F49F54": "Samsung",
    # Cisco
    "00000C": "Cisco", "000142": "Cisco", "000143": "Cisco", "000196": "Cisco",
    "000197": "Cisco", "000216": "Cisco", "000217": "Cisco", "000263": "Cisco",
    "001117": "Cisco", "001185": "Cisco", "0011BB": "Cisco", "00179A": "Cisco",
    "001A30": "Cisco", "001B54": "Cisco", "001C58": "Cisco", "005056": "VMware",
    # Intel
    "0002B3": "Intel", "000347": "Intel", "000423": "Intel", "0007E9": "Intel",
    "000CF1": "Intel", "001101": "Intel", "001167": "Intel", "001320": "Intel",
    "001517": "Intel", "001676": "Intel", "001F3B": "Intel", "002170": "Intel",
    "0023F8": "Intel", "002559": "Intel", "0090F5": "Intel",
    # Raspberry Pi Foundation
    "B827EB": "Raspberry Pi", "DCA632": "Raspberry Pi", "E45F01": "Raspberry Pi",
    # Espressif (ESP8266/ESP32)
    "240AC4": "Espressif", "30AEA4": "Espressif", "3C71BF": "Espressif",
    "5CCF7F": "Espressif", "60019F": "Espressif", "84CC5D": "Espressif",
    "A41348": "Espressif", "A4CF12": "Espressif",
    # TP-Link
    "50C7BF": "TP-Link", "54AF97": "TP-Link", "6032B1": "TP-Link", "640980": "TP-Link",
    "74EA3A": "TP-Link", "8416F9": "TP-Link", "9CA615": "TP-Link", "B09559": "TP-Link",
    "C025A2": "TP-Link", "CC32E5": "TP-Link", "D460E3": "TP-Link", "F8D111": "TP-Link",
    # Netgear
    "001B2F": "Netgear", "001E2A": "Netgear", "00226B": "Netgear", "0026B8": "Netgear",
    "20E52A": "Netgear", "2CB05D": "Netgear", "A040A0": "Netgear", "C4041A": "Netgear",
    # D-Link
    "00055D": "D-Link", "000D88": "D-Link", "0013467": "D-Link", "001346": "D-Link",
    "001CF0": "D-Link", "001E58": "D-Link", "14D64D": "D-Link", "1C7EE5": "D-Link",
    "28107B": "D-Link", "34088A": "D-Link", "9094E4": "D-Link", "B8A386": "D-Link",
    # ASUS
    "001731": "ASUS", "001A92": "ASUS", "04D4C4": "ASUS", "08606E": "ASUS",
    "1062EB": "ASUS", "107B44": "ASUS", "2C56DC": "ASUS", "38D547": "ASUS",
    "50465D": "ASUS", "5404A6": "ASUS", "74D02B": "ASUS", "AC220B": "ASUS",
    # Huawei
    "001E10": "Huawei", "002568": "Huawei", "005A13": "Huawei", "0868E7": "Huawei",
    "107B44": "Huawei", "286ED4": "Huawei", "4C54991": "Huawei", "5405DB": "Huawei",
    "7CE9D3": "Huawei", "888603": "Huawei", "9C28EF": "Huawei", "AC853D": "Huawei",
    # Dell
    "001143": "Dell", "001372": "Dell", "001A4B": "Dell", "001EC9": "Dell",
    "002170": "Dell", "00218E": "Dell", "002219": "Dell", "00237D": "Dell",
    "002564": "Dell", "F04DA2": "Dell", "F8DB88": "Dell",
    # HP / HP Enterprise
    "000E7F": "HP", "001635": "HP", "001A4B": "HP", "001B78": "HP",
    "001CC4": "HP", "001E0B": "HP", "002637": "HP", "30E171": "HP",
    "40B034": "HP", "7CF187": "HP", "9CB6D0": "HP", "D46A6A": "HP",
    # Ubiquiti
    "002722": "Ubiquiti", "04180A": "Ubiquiti", "0418D6": "Ubiquiti", "24A43C": "Ubiquiti",
    "44D9E7": "Ubiquiti", "680571": "Ubiquiti", "788A20": "Ubiquiti", "80274": "Ubiquiti",
    "802747": "Ubiquiti", "DC9FDB": "Ubiquiti", "F09FC2": "Ubiquiti", "FCECDA": "Ubiquiti",
    # Amazon (Echo, Fire, etc.)
    "0C473A": "Amazon", "34D270": "Amazon", "40B4CD": "Amazon", "44650D": "Amazon",
    "680571": "Amazon", "74C246": "Amazon", "A002DC": "Amazon", "A43135": "Amazon",
    "B47C9C": "Amazon", "F0272D": "Amazon", "FC65DE": "Amazon",
    # Google (Chromecast, Home, etc.)
    "3C5AB4": "Google", "54607E": "Google", "6C5C14": "Google", "7C2EBD": "Google",
    "A47733": "Google", "F4F5D8": "Google",
    # Microsoft
    "000D3A": "Microsoft", "0017FA": "Microsoft", "0050F2": "Microsoft",
    "28183F": "Microsoft", "485073": "Microsoft", "7045C4": "Microsoft",
    "902724": "Microsoft", "C83DD4": "Microsoft",
    # Realtek (common on cheap NICs)
    "00E04C": "Realtek", "0021EC": "Realtek", "6045CB": "Realtek", "74D435": "Realtek",
    "E01601": "Realtek",
    # Broadcom
    "000AF7": "Broadcom", "001018": "Broadcom", "00904C": "Broadcom",
}


def lookup_vendor(mac: str) -> str:
    """Return vendor name for a MAC address, or empty string."""
    if not mac or len(mac) < 8:
        return ""
    # Normalize to uppercase, no separators
    clean = mac.upper().replace(":", "").replace("-", "").replace(".", "")
    prefix = clean[:6]
    return _OUI.get(prefix, "")


# ── OS hint from TTL ──────────────────────────────────────────────────────────

def os_hint_from_ttl(ttl: int) -> str:
    """
    Make a best-effort OS guess from the ICMP/ping TTL value.
    Initial TTL values:
      128  → Windows
       64  → Linux / macOS / most Unix
      255  → Cisco IOS / network devices
       32  → older Windows / some embedded
    """
    if ttl <= 0:
        return ""
    if ttl <= 32:
        return "Windows (old)"
    if ttl <= 64:
        return "Linux / macOS"
    if ttl <= 128:
        return "Windows"
    return "Network Device"
