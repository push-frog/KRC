# -*- coding: utf-8 -*-
"""KeeneticOS 3.x-5.x parsers for the Monitor tab."""
import re
from typing import Dict, List

WAN_HINTS = (
    "wan", "isp", "pppoe", "pptp", "l2tp", "usblte", "usbqmi",
    "gigabitethernet0", "wirelessisp", "cabledsp", "yotahq",
)


def _kb_to_mb(value: int) -> int:
    if value >= 10_000_000:
        return round(value / (1024 * 1024))
    return round(value / 1024)


async def get_system_info_raw(self) -> Dict:
    raw_all = {}
    commands = [
        ("show_system", "show system", 12),
        ("show_version", "show version", 10),
        ("show_interface", "show interface", 60),
        ("show_interface_summary", "show interface summary", 20),
        ("show_ip_address", "show ip address", 10),
        ("show_ip_dns", "show ip dns", 8),
        ("show_ip_name_server", "show ip name-server", 8),
    ]
    for key, cmd, timeout in commands:
        try:
            if key in ("show_interface", "show_interface_summary"):
                out = await self.execute_large(cmd, timeout=timeout)
            else:
                out = await self.execute(cmd, timeout=timeout)
            raw_all[key] = out
        except Exception as e:
            raw_all[key] = f"(ошибка: {e})"
    return raw_all


def _parse_memory(sys_out: str):
    mem_total = mem_free = mem_used = 0
    in_memory = False
    for line in sys_out.splitlines():
        s = line.strip()
        m = re.match(r'memory[:\s]+(\d+)\s*/\s*(\d+)', s, re.I)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a <= b:
                mem_used, mem_total = a, b
                mem_free = b - a
            else:
                mem_total, mem_used = a, b
                mem_free = a - b
            continue
        m = re.match(r'memtotal[:\s]+(\d+)', s, re.I)
        if m:
            mem_total = int(m.group(1))
            continue
        m = re.match(r'memfree[:\s]+(\d+)', s, re.I)
        if m:
            mem_free = int(m.group(1))
            continue
        if re.match(r'memory[:\s]*$', s, re.I):
            in_memory = True
            continue
        if in_memory:
            m = re.match(r'total[:\s]+(\d+)', s, re.I)
            if m:
                mem_total = int(m.group(1))
            m = re.match(r'free[:\s]+(\d+)', s, re.I)
            if m:
                mem_free = int(m.group(1))
            if not re.match(r'(total|free|buffers|cached|used)[:\s]', s, re.I):
                in_memory = False
    if mem_total > 0 and mem_used == 0 and mem_free > 0:
        mem_used = max(mem_total - mem_free, 0)
    return mem_total, mem_used, mem_free


def _collect_ips(*texts):
    ips = []
    seen = set()
    for text in texts:
        for ip in re.findall(r'\b(\d{1,3}(?:\.\d{1,3}){3})\b', text or ""):
            if ip.startswith("127.") or ip.startswith("0.") or ip in seen:
                continue
            pts = ip.split(".")
            if any(not p.isdigit() or int(p) > 255 for p in pts):
                continue
            seen.add(ip)
            ips.append(ip)
    return ips


def _is_private(ip: str) -> bool:
    pts = ip.split(".")
    return (
        pts[0] == "10"
        or (pts[0] == "172" and 16 <= int(pts[1]) <= 31)
        or (pts[0] == "192" and pts[1] == "168")
        or ip.startswith("127.")
        or ip.startswith("169.254.")
    )


async def get_system_info(self) -> Dict:
    raw = await get_system_info_raw(self)
    info: Dict = {
        "uptime": "\u2014", "cpu": "\u2014",
        "ram_used": "\u2014", "ram_total": "\u2014", "ram_pct": 0,
        "temp": "\u2014", "model": "\u2014", "firmware": "\u2014",
        "wan_ip": "\u2014", "dns": "\u2014", "hostname": "\u2014",
        "_raw": raw,
    }
    sys_out = raw.get("show_system", "")
    mem_total, mem_used, mem_free = _parse_memory(sys_out)

    for line in sys_out.splitlines():
        s = line.strip()
        m = re.match(r'hostname[:\s]+(\S+)', s, re.I)
        if m:
            info["hostname"] = m.group(1)
        m = re.match(r'uptime[:\s]+(\d+)', s, re.I)
        if m:
            info["uptime"] = self._fmt_uptime(int(m.group(1)))
        m = re.match(r'cpuload[:\s]+(\d+)', s, re.I)
        if m:
            info["cpu"] = m.group(1) + " %"
        m = re.match(r'temperature[:\s]+([\d.]+)', s, re.I)
        if m:
            info["temp"] = m.group(1) + " \u00b0C"

    if mem_total > 0:
        info["ram_total"] = str(_kb_to_mb(mem_total))
        info["ram_used"] = str(_kb_to_mb(mem_used))
        try:
            info["ram_pct"] = round(mem_used / mem_total * 100)
        except Exception:
            pass

    ver_out = raw.get("show_version", "")
    for line in ver_out.splitlines():
        s = line.strip()
        m = re.match(r'(?:ndms(?:Version)?|version|release)[:\s]+(\S+)', s, re.I)
        if m and info["firmware"] == "\u2014":
            info["firmware"] = m.group(1)
        m = re.match(r'(?:deviceName|device|model|product)[:\s]+(.+)', s, re.I)
        if m and info["model"] == "\u2014":
            info["model"] = m.group(1).strip()
        m = re.match(r'hostname[:\s]+(\S+)', s, re.I)
        if m and info["hostname"] == "\u2014":
            info["hostname"] = m.group(1)

    if info["model"] == "\u2014":
        for line in sys_out.splitlines():
            m = re.match(r'(?:deviceName|device|model|name)[:\s]+(.+)',
                         line.strip(), re.I)
            if m:
                info["model"] = m.group(1).strip()
                break

    dns_ips = _collect_ips(
        raw.get("show_ip_name_server", ""),
        raw.get("show_ip_dns", ""),
    )
    if dns_ips:
        info["dns"] = ", ".join(dns_ips[:4])

    ifaces = _parse_interfaces_traffic(
        raw.get("show_interface", "") + "\n" + raw.get("show_interface_summary", "")
    )
    wan = ""
    for iface in ifaces:
        name = (iface.get("name") or "").lower()
        ip = iface.get("ip") or "\u2014"
        if ip == "\u2014":
            continue
        if any(h in name for h in WAN_HINTS):
            wan = ip
            break
    if not wan:
        addr_ips = _collect_ips(raw.get("show_ip_address", ""))
        public = [ip for ip in addr_ips if not _is_private(ip)]
        wan = (public[0] if public else (addr_ips[0] if addr_ips else ""))
    if not wan:
        for iface in ifaces:
            ip = iface.get("ip") or "\u2014"
            if ip != "\u2014" and not ip.startswith("127."):
                wan = ip
                break
    if wan:
        info["wan_ip"] = wan
    return info


def _parse_interfaces_traffic(raw: str) -> List[Dict]:
    interfaces: List[Dict] = []
    IFACE_RE = re.compile(
        r'^([A-Za-z][A-Za-z0-9]+(?:[0-9/.\-][A-Za-z0-9/.\-]*)?)$')
    SKIP_NAMES = {
        "description", "type", "state", "link", "mtu", "address",
        "inet", "index", "id", "connected", "flags", "queue",
        "rxbytes", "txbytes", "rxpackets", "txpackets",
        "rxerrors", "txerrors", "rxdrops", "txdrops",
        "memory", "hostname", "uptime", "interface", "summary",
        "name", "proto", "protocol", "status",
    }
    current: Dict = {}

    def flush():
        if current.get("name"):
            interfaces.append(dict(current))

    for line in (raw or "").splitlines():
        if line and not line[0].isspace():
            s = line.strip().rstrip(":")
            m = IFACE_RE.match(s)
            if m and s.lower() not in SKIP_NAMES and len(s) < 48:
                flush()
                current = {
                    "name": s, "state": "?",
                    "rx_bytes": 0, "tx_bytes": 0,
                    "ip": "\u2014", "mac": "\u2014",
                    "description": "",
                }
                continue
        if not current:
            continue
        s = line.strip()
        if not s:
            continue
        m = re.match(r'description[:\s]+(.+)', s, re.I)
        if m:
            current["description"] = m.group(1).strip()
            continue
        m = re.match(r'(?:link|state|connected)[:\s]+(\S+)', s, re.I)
        if m:
            val = m.group(1).lower().strip(",")
            if val in ("up", "yes", "on", "running"):
                current["state"] = "up"
            elif val in ("down", "no", "off"):
                current["state"] = "down"
            else:
                current["state"] = val
            continue
        m = re.match(r'address[:\s]+([\da-f:]{11,17})', s, re.I)
        if m:
            current["mac"] = m.group(1)
            continue
        m = re.match(r'(?:inet|address-ip|ip)[:\s]+([\d.]+)', s, re.I)
        if m and current["ip"] == "\u2014":
            current["ip"] = m.group(1)
            continue
        m = re.search(r'(\d+\.\d+\.\d+\.\d+)/\d+', s)
        if m and current["ip"] == "\u2014":
            current["ip"] = m.group(1)
            continue
        m = re.match(r'(?:rxbytes|rx-bytes|rx_bytes)[:\s]+(\d+)', s, re.I)
        if m:
            current["rx_bytes"] = int(m.group(1))
            continue
        m = re.match(r'(?:txbytes|tx-bytes|tx_bytes)[:\s]+(\d+)', s, re.I)
        if m:
            current["tx_bytes"] = int(m.group(1))
            continue

    flush()
    seen = set()
    unique: List[Dict] = []
    for iface in interfaces:
        if iface["name"] not in seen:
            seen.add(iface["name"])
            unique.append(iface)
    return unique


async def get_interfaces_traffic(self) -> List[Dict]:
    try:
        raw = await self.execute_large("show interface", timeout=90)
    except Exception:
        try:
            raw = await self.execute("show interface", timeout=25)
        except Exception:
            return []
    parsed = _parse_interfaces_traffic(raw)
    if parsed:
        return parsed
    try:
        raw2 = await self.execute("show interface summary", timeout=20)
    except Exception:
        raw2 = ""
    return _parse_interfaces_traffic(raw2)


def _flush_client(clients, seen, ip, mac, host, source):
    if not ip or not mac:
        return
    key = mac.lower()
    if key in seen:
        return
    seen.add(key)
    clients.append({
        "ip": ip,
        "mac": mac,
        "hostname": host or "\u2014",
        "source": source,
    })


async def get_clients(self) -> List[Dict]:
    clients: List[Dict] = []
    seen = set()
    commands = [
        ("show ip hotspot", "Hotspot", 20),
        ("show ip dhcp bindings", "DHCP", 12),
        ("show ip arp", "ARP", 12),
        ("show arp", "ARP", 12),
    ]
    for cmd, source, timeout in commands:
        try:
            out = await self.execute_large(cmd, timeout=timeout)
        except Exception:
            continue
        if not out or "unknown" in out.lower()[:80] or "error" in out.lower()[:80]:
            continue
        ip = mac = host = ""
        for line in out.splitlines() + [""]:
            s = line.strip()
            if not s:
                _flush_client(clients, seen, ip, mac, host, source)
                ip = mac = host = ""
                continue
            if re.match(r'^(host|lease|entry)[:\s]*$', s, re.I):
                _flush_client(clients, seen, ip, mac, host, source)
                ip = mac = host = ""
                continue
            m = re.match(r'ip[:\s]+([\d.]+)', s, re.I)
            if m:
                ip = m.group(1)
                continue
            m = re.match(r'mac[:\s]+([\da-fA-F:]{11,17})', s, re.I)
            if m:
                mac = m.group(1)
                continue
            m = re.match(r'(?:hostname|name)[:\s]+(.+)', s, re.I)
            if m:
                val = m.group(1).strip()
                if val and val.lower() not in ("yes", "no", "home"):
                    host = val
                continue
            m = re.match(r'\s*([\d.]+)\s+([\da-fA-F:]{11,17})\s*(\S*)', s)
            if m and not ip:
                ip, mac, host = m.groups()
                continue
            m = re.search(r'([\d.]+)\s+\S+\s+([\da-fA-F:]{11,17})', s)
            if m and not ip:
                ip, mac = m.groups()
        _flush_client(clients, seen, ip, mac, host, source)
    return clients


def install():
    from .client import KeeneticAdvancedClient
    KeeneticAdvancedClient.get_system_info_raw = get_system_info_raw
    KeeneticAdvancedClient.get_system_info = get_system_info
    KeeneticAdvancedClient.get_interfaces_traffic = get_interfaces_traffic
    KeeneticAdvancedClient.get_clients = get_clients
