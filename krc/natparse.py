# -*- coding: utf-8 -*-
import re
from typing import List

PAT_NAT_A = re.compile(
    r'ip\s+nat\s+destination\s+proto\s+(\w+)\s+'
    r'from\s+(\S+)\s+port\s+(\d+(?:-\d+)?)\s+'
    r'inside\s+(\S+)\s+port\s+(\d+(?:-\d+)?)'
    r'(?:\s+comment\s+"([^"]*)")?', re.I)
PAT_NAT_B = re.compile(
    r'ip\s+nat\s+destination\s+proto\s+(\w+)\s+'
    r'port\s+(\d+(?:-\d+)?)\s+'
    r'inside\s+(\S+)\s+port\s+(\d+(?:-\d+)?)'
    r'(?:\s+comment\s+"([^"]*)")?', re.I)
PAT_NAT_C = re.compile(
    r'ip\s+nat\s+destination\s+proto\s+(\w+)\s+'
    r'(?:interface\s+(\S+)\s+)?port\s+(\d+(?:-\d+)?)\s+'
    r'inside\s+(\S+)\s+port\s+(\d+(?:-\d+)?)'
    r'(?:\s+comment\s+"([^"]*)")?', re.I)
PAT_STATIC = re.compile(
    r'ip\s+static\s+(tcp|udp|tcpudp|any|icmp)?\s*'
    r'(\S+)\s+'
    r'(?:(\d+)(?:\s+through\s+(\d+))?\s+)?'
    r'(\S+)'
    r'(?:\s+(\d+))?'
    r'(?:\s+!(\S+))?', re.I)
MAC_RE = re.compile(r'^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$', re.I)
IP_RE = re.compile(r'^(?:\d{1,3}\.){3}\d{1,3}$')


def _looks_like_target(value: str) -> bool:
    value = (value or "").strip()
    if not value:
        return False
    if IP_RE.match(value) or MAC_RE.match(value):
        return True
    if value.lower() in ("through", "tcp", "udp", "any", "icmp", "rule",
                         "disable", "schedule", "comment", "ip", "static"):
        return False
    if value.isdigit():
        return False
    return True


def _add(rules, seen, proto, iface, ep, iip, ip2, cmt, line_s, ipv6=False):
    iface = (iface or "any").strip()
    cmt = (cmt or "").strip()
    iip = (iip or "").strip()
    if not _looks_like_target(iip):
        return
    if not ep:
        return
    proto = (proto or "ANY").upper()
    key = (proto, str(ep), iip.lower(), str(ip2 or ep), iface.lower())
    if key in seen:
        return
    seen.add(key)
    rules.append({
        "protocol": proto,
        "ext_port": str(ep),
        "int_ip": iip,
        "int_port": str(ip2 or ep),
        "interface": iface,
        "comment": cmt,
        "full_line": line_s,
        "ipv6": ipv6,
    })


def _parse_yaml_blocks(text: str, ipv6=False):
    rules = []
    seen = set()
    cur = {}
    started = False

    def flush():
        if not cur:
            return
        proto = cur.get("protocol") or cur.get("proto") or "tcp"
        iface = cur.get("interface") or cur.get("from") or "any"
        p1 = cur.get("port") or cur.get("ext_port")
        p2 = cur.get("end-port") or cur.get("end_port")
        dest = (cur.get("to-address") or cur.get("to_address")
                or cur.get("to-host") or cur.get("to_host")
                or cur.get("inside") or cur.get("address"))
        toport = cur.get("to-port") or cur.get("to_port")
        cmt = cur.get("comment") or cur.get("description") or ""
        if p1 and dest:
            ep = f"{p1}-{p2}" if p2 else str(p1)
            line = " ".join(f"{k}:{v}" for k, v in cur.items())
            _add(rules, seen, proto, iface, ep, dest, toport, cmt, line, ipv6)

    for raw in (text or "").splitlines() + [""]:
        s = raw.strip()
        sl = s.lower()
        if not s:
            if started:
                flush()
                cur = {}
                started = False
            continue
        if sl in ("static:", "ip static:", "destination:") or sl.startswith("ip static"):
            if started:
                flush()
            cur = {}
            started = True
            continue
        m = re.match(r'(protocol|proto|interface|from|port|end-port|end_port|'
                     r'to-address|to_address|to-host|to_host|to-port|to_port|'
                     r'inside|address|comment|description)[:\s]+(.+)$', s, re.I)
        if m:
            started = True
            cur[m.group(1).lower()] = m.group(2).strip().strip('"')
    flush()
    return rules, seen


def parse_port_forward_text(all_output: str, ipv6=False) -> List[dict]:
    rules, seen = _parse_yaml_blocks(all_output, ipv6=ipv6)
    for line in (all_output or "").splitlines():
        ls = line.strip()
        if not ls or ls.startswith("#"):
            continue
        m = PAT_STATIC.search(ls)
        if m and not ipv6:
            proto, iface, p1, p2, dest, toport, cmt = m.groups()
            if p1:
                ep = f"{p1}-{p2}" if p2 else p1
                _add(rules, seen, proto or "tcp", iface, ep, dest,
                     toport or p1, cmt, ls)
            continue
        m = PAT_NAT_A.search(ls)
        if m:
            _add(rules, seen, *m.groups(), ls, ipv6)
            continue
        m = PAT_NAT_C.search(ls)
        if m:
            _add(rules, seen, *m.groups(), ls, ipv6)
            continue
        m = PAT_NAT_B.search(ls)
        if m:
            g = m.groups()
            _add(rules, seen, g[0], "any", g[1], g[2], g[3], g[4], ls, ipv6)
    return rules


async def get_port_forwardings(self, ipv6=False):
    raw_parts = []
    all_output = ""
    if ipv6:
        cmds = [
            ("show ipv6 static", 25, False),
            ("show ipv6 nat destination", 20, False),
            ("show running-config", 120, True),
        ]
    else:
        cmds = [
            ("show ip static", 30, False),
            ("show ip nat destination", 25, False),
            ("show running-config", 120, True),
        ]
    for cmd, t, large in cmds:
        try:
            out = await (self.execute_large(cmd, timeout=t) if large
                         else self.execute(cmd, timeout=t))
        except Exception as e:
            out = f"(ошибка: {e})"
        raw_parts.append(
            f"{'='*55}\n>>> {cmd}\n{'='*55}\n{out or '(пусто)'}\n")
        low = (out or "").lower()[:100]
        if out and len(out) > 5 and "unknown" not in low and "error" not in low:
            all_output += "\n" + out
    rules = parse_port_forward_text(all_output, ipv6=ipv6)
    return rules, "\n".join(raw_parts)


def install():
    from .client import KeeneticAdvancedClient
    KeeneticAdvancedClient.get_port_forwardings = get_port_forwardings
