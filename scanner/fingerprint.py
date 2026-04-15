"""
Best-effort device fingerprinting: MAC vendor lookup and OS hints from TTL.
No external API calls — uses a compiled OUI prefix table.
"""

# ── OUI prefix → vendor (first 6 hex chars, uppercase, no separators) ─────────
# A curated list covering the most common consumer/enterprise devices in practice.
_OUI: dict[str, str] = {
    # ── Apple ─────────────────────────────────────────────────────────────────
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
    "18AF61": "Apple", "28E02C": "Apple", "34363B": "Apple", "40A6D9": "Apple",
    "44D884": "Apple", "5C8D4E": "Apple", "68A86D": "Apple", "78CA39": "Apple",
    "8863DF": "Apple", "98B8E3": "Apple", "AC3C0B": "Apple", "C82A14": "Apple",
    "D821D9": "Apple", "E8802E": "Apple", "F00B6E": "Apple", "F45C89": "Apple",

    # ── Samsung ───────────────────────────────────────────────────────────────
    "0012FB": "Samsung", "001599": "Samsung", "0017C9": "Samsung", "001A8A": "Samsung",
    "001D25": "Samsung", "001EE1": "Samsung", "002119": "Samsung", "002399": "Samsung",
    "002566": "Samsung", "3425C4": "Samsung", "5001BB": "Samsung", "8425DB": "Samsung",
    "CC07AB": "Samsung", "F49F54": "Samsung",
    "0024E9": "Samsung", "04FE31": "Samsung", "08D4B3": "Samsung", "0C71BF": "Samsung",
    "14BB6E": "Samsung", "1C62B8": "Samsung", "2C0E3D": "Samsung", "3C5A37": "Samsung",
    "40D3AE": "Samsung", "5C2E59": "Samsung", "6C2F2C": "Samsung", "7492BE": "Samsung",
    "84A466": "Samsung", "8CAB8E": "Samsung", "945E22": "Samsung", "A8D8F3": "Samsung",
    "B83FE4": "Samsung", "C4504D": "Samsung", "D022BE": "Samsung", "E4E0C5": "Samsung",
    "F4428F": "Samsung", "F87BEF": "Samsung",

    # ── Cisco ─────────────────────────────────────────────────────────────────
    "00000C": "Cisco", "000142": "Cisco", "000143": "Cisco", "000196": "Cisco",
    "000197": "Cisco", "000216": "Cisco", "000217": "Cisco", "000263": "Cisco",
    "001117": "Cisco", "001185": "Cisco", "0011BB": "Cisco", "00179A": "Cisco",
    "001A30": "Cisco", "001B54": "Cisco", "001C58": "Cisco",
    "0006D6": "Cisco", "000E39": "Cisco", "001120": "Cisco", "002693": "Cisco",
    "002CAB": "Cisco", "00D06E": "Cisco", "2C3144": "Cisco", "3482E2": "Cisco",
    "406CBE": "Cisco", "547FEE": "Cisco", "6899CD": "Cisco", "70010D": "Cisco",
    "74A02F": "Cisco", "A8B4E5": "Cisco", "B43A28": "Cisco", "C89C1D": "Cisco",
    # Cisco Meraki (cloud-managed)
    "001BD9": "Cisco Meraki", "0CB9C0": "Cisco Meraki", "3427DE": "Cisco Meraki",
    "3C8ACF": "Cisco Meraki", "884445": "Cisco Meraki", "88DC96": "Cisco Meraki",
    "E2553E": "Cisco Meraki",

    # ── VMware (virtual machines) ─────────────────────────────────────────────
    "000C29": "VMware", "000569": "VMware", "001C14": "VMware", "005056": "VMware",

    # ── VirtualBox ────────────────────────────────────────────────────────────
    "080027": "VirtualBox",

    # ── Parallels ─────────────────────────────────────────────────────────────
    "001C42": "Parallels",

    # ── Intel ─────────────────────────────────────────────────────────────────
    "0002B3": "Intel", "000347": "Intel", "000423": "Intel", "0007E9": "Intel",
    "000CF1": "Intel", "001101": "Intel", "001167": "Intel", "001320": "Intel",
    "001517": "Intel", "001676": "Intel", "001F3B": "Intel", "002170": "Intel",
    "0023F8": "Intel", "002559": "Intel", "0090F5": "Intel",
    "40A5EF": "Intel", "5CF951": "Intel", "60D819": "Intel", "7C7635": "Intel",
    "8C8D28": "Intel", "9CEB07": "Intel", "A0369F": "Intel", "A4C3F0": "Intel",
    "E8B4C8": "Intel",

    # ── Raspberry Pi Foundation ───────────────────────────────────────────────
    "B827EB": "Raspberry Pi", "DCA632": "Raspberry Pi", "E45F01": "Raspberry Pi",

    # ── Espressif (ESP8266 / ESP32 IoT modules) ───────────────────────────────
    "240AC4": "Espressif", "30AEA4": "Espressif", "3C71BF": "Espressif",
    "5CCF7F": "Espressif", "60019F": "Espressif", "84CC5D": "Espressif",
    "A41348": "Espressif", "A4CF12": "Espressif", "C45BBE": "Espressif",
    "D8F15B": "Espressif", "F412FA": "Espressif",

    # ── TP-Link ───────────────────────────────────────────────────────────────
    "50C7BF": "TP-Link", "54AF97": "TP-Link", "6032B1": "TP-Link", "640980": "TP-Link",
    "74EA3A": "TP-Link", "8416F9": "TP-Link", "9CA615": "TP-Link", "B09559": "TP-Link",
    "C025A2": "TP-Link", "CC32E5": "TP-Link", "D460E3": "TP-Link", "F8D111": "TP-Link",
    "14CC20": "TP-Link", "1C61B4": "TP-Link", "2C55D3": "TP-Link", "3C84FF": "TP-Link",
    "50FA84": "TP-Link", "709F2D": "TP-Link", "98DAC4": "TP-Link", "A42BB0": "TP-Link",
    "B4B024": "TP-Link", "C4E984": "TP-Link", "E4C172": "TP-Link", "F81A67": "TP-Link",

    # ── Netgear ───────────────────────────────────────────────────────────────
    "001B2F": "Netgear", "001E2A": "Netgear", "00226B": "Netgear", "0026B8": "Netgear",
    "20E52A": "Netgear", "2CB05D": "Netgear", "A040A0": "Netgear", "C4041A": "Netgear",
    "00146C": "Netgear", "001E2A": "Netgear", "04A151": "Netgear", "10DA43": "Netgear",
    "2CBEBE": "Netgear", "44944F": "Netgear", "6CB0CE": "Netgear", "84189F": "Netgear",
    "9C3DCF": "Netgear", "A021B7": "Netgear", "C03F0E": "Netgear",

    # ── D-Link ────────────────────────────────────────────────────────────────
    "00055D": "D-Link", "000D88": "D-Link", "001346": "D-Link",
    "001CF0": "D-Link", "001E58": "D-Link", "14D64D": "D-Link", "1C7EE5": "D-Link",
    "28107B": "D-Link", "34088A": "D-Link", "9094E4": "D-Link", "B8A386": "D-Link",
    "1C5F2B": "D-Link", "2419B0": "D-Link", "34EF44": "D-Link", "54B80A": "D-Link",
    "6463C8": "D-Link", "78321B": "D-Link", "9CF387": "D-Link", "BC0F9A": "D-Link",

    # ── ASUS ──────────────────────────────────────────────────────────────────
    "001731": "ASUS", "001A92": "ASUS", "04D4C4": "ASUS", "08606E": "ASUS",
    "1062EB": "ASUS", "2C56DC": "ASUS", "38D547": "ASUS",
    "50465D": "ASUS", "5404A6": "ASUS", "74D02B": "ASUS", "AC220B": "ASUS",
    "048D38": "ASUS", "0CB63C": "ASUS", "18C04D": "ASUS", "2C4D54": "ASUS",
    "30C9AB": "ASUS", "3C7C3F": "ASUS", "40167E": "ASUS", "44D9E7": "ASUS",
    "5404A6": "ASUS", "60A44C": "ASUS", "706F81": "ASUS", "74D02B": "ASUS",
    "788A20": "ASUS", "88D7F6": "ASUS", "A8F7E0": "ASUS", "F832E4": "ASUS",

    # ── Huawei ────────────────────────────────────────────────────────────────
    "001E10": "Huawei", "002568": "Huawei", "005A13": "Huawei", "0868E7": "Huawei",
    "286ED4": "Huawei", "5405DB": "Huawei",
    "7CE9D3": "Huawei", "888603": "Huawei", "9C28EF": "Huawei", "AC853D": "Huawei",
    "001E10": "Huawei", "04C06F": "Huawei", "0C96BF": "Huawei", "10C61F": "Huawei",
    "1C8E5C": "Huawei", "246A7D": "Huawei", "283AB7": "Huawei", "2C9EFC": "Huawei",
    "38F889": "Huawei", "3C47C9": "Huawei", "40CB A8": "Huawei",
    "485A3F": "Huawei", "4CAC0A": "Huawei", "5022A7": "Huawei", "549F13": "Huawei",
    "5CB09A": "Huawei", "64013B": "Huawei", "64D154": "Huawei", "6C2B59": "Huawei",
    "70723C": "Huawei", "74882A": "Huawei", "786A89": "Huawei", "7C1CF1": "Huawei",
    "8C0D76": "Huawei", "90671C": "Huawei", "9C37F4": "Huawei", "A43560": "Huawei",
    "AC4E91": "Huawei", "B0C654": "Huawei", "BC3EA3": "Huawei", "C4072F": "Huawei",
    "C49E0E": "Huawei", "C8D15E": "Huawei", "CC8870": "Huawei", "D0272F": "Huawei",
    "E4C2D1": "Huawei", "EC238D": "Huawei", "F0CA91": "Huawei", "F4559C": "Huawei",
    "FC48EF": "Huawei",

    # ── Dell ──────────────────────────────────────────────────────────────────
    "001143": "Dell", "001372": "Dell", "001EC9": "Dell",
    "002170": "Dell", "00218E": "Dell", "002219": "Dell", "00237D": "Dell",
    "002564": "Dell", "F04DA2": "Dell", "F8DB88": "Dell",
    "109A98": "Dell", "14FEB5": "Dell", "18DB F2": "Dell",
    "24B6FD": "Dell", "2C27D7": "Dell", "34480D": "Dell", "40F2E9": "Dell",
    "485B39": "Dell", "4C2FC5": "Dell", "50979A": "Dell", "546734": "Dell",
    "5CF9DD": "Dell", "608F5C": "Dell", "6451062A": "Dell",
    "848592": "Dell", "A4BADB": "Dell", "B083FE": "Dell", "B4D5E8": "Dell",
    "BC305B": "Dell", "C8D3FF": "Dell", "EC9A74": "Dell", "F06B6F": "Dell",

    # ── HP / HPE ──────────────────────────────────────────────────────────────
    "000E7F": "HP", "001635": "HP", "001A4B": "HP", "001B78": "HP",
    "001CC4": "HP", "001E0B": "HP", "002637": "HP", "30E171": "HP",
    "40B034": "HP", "7CF187": "HP", "9CB6D0": "HP", "D46A6A": "HP",
    "10604B": "HP", "18A905": "HP", "38EAA7": "HP", "3C4A92": "HP",
    "405BD8": "HP", "58205B": "HP", "6446A7": "HP", "70107A": "HP",
    "78AC44": "HP", "84971C": "HP", "98E7F4": "HP", "A0A8CD": "HP",
    "B499BA": "HP", "C8346E": "HP", "D8D385": "HP", "E4E0A2": "HP",

    # ── Ubiquiti ──────────────────────────────────────────────────────────────
    "002722": "Ubiquiti", "04180A": "Ubiquiti", "0418D6": "Ubiquiti", "24A43C": "Ubiquiti",
    "44D9E7": "Ubiquiti", "788A20": "Ubiquiti",
    "802747": "Ubiquiti", "DC9FDB": "Ubiquiti", "F09FC2": "Ubiquiti", "FCECDA": "Ubiquiti",
    "00272E": "Ubiquiti", "046202": "Ubiquiti", "18D7C6": "Ubiquiti", "24A43C": "Ubiquiti",
    "68725D": "Ubiquiti", "782BCE": "Ubiquiti", "805A04": "Ubiquiti", "B4FBE4": "Ubiquiti",
    "E063DA": "Ubiquiti", "F09FC2": "Ubiquiti", "FC5B39": "Ubiquiti",

    # ── Amazon (Echo, Fire TV, Ring, Kindle) ──────────────────────────────────
    "0C473A": "Amazon", "34D270": "Amazon", "40B4CD": "Amazon", "44650D": "Amazon",
    "74C246": "Amazon", "A002DC": "Amazon", "A43135": "Amazon",
    "B47C9C": "Amazon", "F0272D": "Amazon", "FC65DE": "Amazon",
    "1C12B0": "Amazon", "2491AA": "Amazon", "38F73D": "Amazon", "4000E2": "Amazon",
    "44650D": "Amazon", "4CADB8": "Amazon", "68C4B3": "Amazon", "7483C2": "Amazon",
    "84D6D0": "Amazon", "88718A": "Amazon", "AC63BE": "Amazon",
    "B4F2E5": "Amazon", "CC9EAD": "Amazon",

    # ── Google (Chromecast, Home, Nest, Pixel) ────────────────────────────────
    "3C5AB4": "Google", "54607E": "Google", "6C5C14": "Google", "7C2EBD": "Google",
    "A47733": "Google", "F4F5D8": "Google",
    "1C9E46": "Google", "20DF5B": "Google", "48D6D5": "Google", "4C24D7": "Google",
    "58CF79": "Google", "6C40B5": "Google", "7C61B7": "Google", "94EB2C": "Google",
    "A4977A": "Google", "D83134": "Google", "F88FCA": "Google",

    # ── Microsoft ─────────────────────────────────────────────────────────────
    "000D3A": "Microsoft", "0017FA": "Microsoft", "0050F2": "Microsoft",
    "28183F": "Microsoft", "485073": "Microsoft", "7045C4": "Microsoft",
    "902724": "Microsoft", "C83DD4": "Microsoft",
    "000F4B": "Microsoft", "001DD8": "Microsoft", "00BFF2": "Microsoft",
    "0C5515": "Microsoft", "28C63F": "Microsoft", "485073": "Microsoft",
    "6045BD": "Microsoft", "6C5CB1": "Microsoft", "70066A": "Microsoft",
    "7C1E52": "Microsoft", "88CBAD": "Microsoft",

    # ── Realtek ───────────────────────────────────────────────────────────────
    "00E04C": "Realtek", "0021EC": "Realtek", "6045CB": "Realtek", "74D435": "Realtek",
    "E01601": "Realtek", "7C76A7": "Realtek",

    # ── Broadcom ──────────────────────────────────────────────────────────────
    "000AF7": "Broadcom", "001018": "Broadcom", "00904C": "Broadcom",

    # ── MikroTik (RouterOS, very common in enterprise/ISP networks) ───────────
    "2CC8A6": "MikroTik", "4CBF3E": "MikroTik", "74DA38": "MikroTik",
    "D4CA6D": "MikroTik", "DC2C6E": "MikroTik", "B8A170": "MikroTik",
    "48A979": "MikroTik", "6C3B6B": "MikroTik", "CC2DE0": "MikroTik",
    "E4967E": "MikroTik",

    # ── Aruba Networks / HPE ──────────────────────────────────────────────────
    "002369": "Aruba", "70888B": "Aruba", "84D47E": "Aruba",
    "40E3D6": "Aruba", "A8BD27": "Aruba", "B4C0A0": "Aruba",
    "D0D3E0": "Aruba",

    # ── Juniper Networks ──────────────────────────────────────────────────────
    "0022AB": "Juniper", "2C6BF5": "Juniper", "84B587": "Juniper",
    "A0A8CD": "Juniper", "F43301": "Juniper",

    # ── Fortinet ──────────────────────────────────────────────────────────────
    "00090F": "Fortinet", "009096": "Fortinet", "70261C": "Fortinet",
    "90A4D5": "Fortinet", "E84CF6": "Fortinet",

    # ── Xiaomi (phones, IoT, routers, TVs) ───────────────────────────────────
    "28E31F": "Xiaomi", "34CE00": "Xiaomi", "3C14D7": "Xiaomi",
    "64CC2E": "Xiaomi", "98FAE3": "Xiaomi", "F0F61C": "Xiaomi",
    "0C1DAF": "Xiaomi", "1894D3": "Xiaomi", "2C2DA3": "Xiaomi",
    "34DE1A": "Xiaomi", "50871E": "Xiaomi", "60AB14": "Xiaomi",
    "6C3096": "Xiaomi", "8C8B83": "Xiaomi", "9882DF": "Xiaomi",
    "A44A7B": "Xiaomi", "C461E6": "Xiaomi", "D4970B": "Xiaomi",

    # ── Sony ──────────────────────────────────────────────────────────────────
    "001A80": "Sony", "0024BE": "Sony", "0013A9": "Sony",
    "30EFEC": "Sony", "6C2F6C": "Sony", "A8E0AF": "Sony",
    "FCBCD9": "Sony", "1C9E46": "Sony", "6067A3": "Sony",
    "84C7CB": "Sony", "A8066D": "Sony", "B40EDE": "Sony",
    "CC9902": "Sony",

    # ── LG Electronics ───────────────────────────────────────────────────────
    "001A43": "LG Electronics", "002CE9": "LG Electronics",
    "5409A1": "LG Electronics", "8CF872": "LG Electronics",
    "74BF60": "LG Electronics", "B4E643": "LG Electronics",
    "0C96E6": "LG Electronics", "28C63F": "LG Electronics",
    "34E2FD": "LG Electronics", "40B0FA": "LG Electronics",
    "6C40B5": "LG Electronics", "7C66EF": "LG Electronics",
    "A8169F": "LG Electronics", "C46C2D": "LG Electronics",
    "CC2D83": "LG Electronics", "E8F2E2": "LG Electronics",

    # ── Nintendo ──────────────────────────────────────────────────────────────
    "002709": "Nintendo", "001B7A": "Nintendo", "7CBB8A": "Nintendo",
    "8C56C5": "Nintendo", "9CE635": "Nintendo", "A4C0E1": "Nintendo",
    "58BD69": "Nintendo", "E0C97B": "Nintendo", "F078F5": "Nintendo",

    # ── Hikvision (IP cameras, NVRs) ─────────────────────────────────────────
    "4C1D96": "Hikvision", "144FD7": "Hikvision", "283AB7": "Hikvision",
    "4CBCF8": "Hikvision", "6CA9CB": "Hikvision", "C0B4F6": "Hikvision",
    "D07C32": "Hikvision", "54C7A8": "Hikvision", "BC9CE5": "Hikvision",

    # ── Dahua Technology (cameras, NVRs) ─────────────────────────────────────
    "4CE675": "Dahua", "70A598": "Dahua", "90E96B": "Dahua",
    "A4148B": "Dahua", "C4A48C": "Dahua",

    # ── Western Digital ───────────────────────────────────────────────────────
    "001A7A": "Western Digital", "14CF92": "Western Digital",
    "3453D2": "Western Digital", "5C9F09": "Western Digital",

    # ── Seagate ───────────────────────────────────────────────────────────────
    "0004CF": "Seagate", "001C22": "Seagate",

    # ── Synology (NAS) ────────────────────────────────────────────────────────
    "001132": "Synology",

    # ── QNAP (NAS) ────────────────────────────────────────────────────────────
    "247EDA": "QNAP", "000891B": "QNAP",

    # ── Motorola ──────────────────────────────────────────────────────────────
    "0016E4": "Motorola", "001858": "Motorola", "002232": "Motorola",
    "28371C": "Motorola", "602AD0": "Motorola", "840D2E": "Motorola",
    "9CF33A": "Motorola",

    # ── Lenovo ────────────────────────────────────────────────────────────────
    "28D244": "Lenovo", "34E6D7": "Lenovo", "40A8F3": "Lenovo",
    "484D7E": "Lenovo", "5CE0C5": "Lenovo", "6894B0": "Lenovo",
    "80E82C": "Lenovo", "90BB0D": "Lenovo",

    # ── Brother (printers) ────────────────────────────────────────────────────
    "00803F": "Brother", "001BA9": "Brother", "0026B9": "Brother",
    "30055C": "Brother",

    # ── Canon (cameras, printers) ─────────────────────────────────────────────
    "001EBF": "Canon", "0020E0": "Canon", "001E8F": "Canon",
    "2C54CF": "Canon", "3C96EF": "Canon", "7487BF": "Canon",

    # ── Epson (printers) ──────────────────────────────────────────────────────
    "000486": "Epson", "0026AB": "Epson", "18660A": "Epson", "44D9E7": "Epson",

    # ── ZTE (routers, phones) ─────────────────────────────────────────────────
    "001AB0": "ZTE", "0026EE": "ZTE", "005A4A": "ZTE",
    "10DB3B": "ZTE", "2C9575": "ZTE", "40494D": "ZTE",
    "5CCF9F": "ZTE", "78A3E4": "ZTE", "9800B7": "ZTE",

    # ── Ruckus / CommScope (Wi-Fi APs) ───────────────────────────────────────
    "00252E": "Ruckus", "0C8112": "Ruckus", "58A11E": "Ruckus",
    "80EE73": "Ruckus", "C82CB2": "Ruckus",

    # ── Microchip Technology ──────────────────────────────────────────────────
    "002322": "Microchip",

    # ── Texas Instruments ─────────────────────────────────────────────────────
    "F0795C": "Texas Instruments", "E03F49": "Texas Instruments",
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
