# -*- coding: utf-8 -*-
import re
from typing import List


PAT_NAT_A = re.compile(
    r'ip\s+nat\s+destination\s+proto\s+(\w+)\s+'
    r'from\s+(\S+)\s+port\s+(\d+(?:-\d+)?)\s+'
    r'inside\s+([\d.]+)\s+port\s+(\d+(?:-\d+)?)'
    r'(?:\s+comment\s+"([^"]*)")?', re.I)
PAT_NAT_B = re.compile(
    r'ip\s+nat\s+destination\s+proto\s+(\w+)\s+'
    r'port\s+(\d+(?:-\d+)?)\s+'
    r'inside\s+([\d.]+)\s+port\s+(\d+(?:-\d+)?)'
    r'(?:\s+comment\s+"([^"]*)")?', re.I)
PAT_NAT_C = re.compile(
    r'ip\s+nat\s+destination\s+proto\s+(\w+)\s+'
    r'(?:interface\s+(\S+)\s+)?port\s+(\d+(?:-\d+)?)\s+'
    r'inside\s+([\d.]+)\s+port\s+(\d+(?:-\d+)?)'
    r'(?:\s+comment\s+"([^"]*)")?', re.I)
PAT_STATIC = re.compile(
    r'ip\s+static\s+(tcp|udp)\s+'
    r'(\S+)\s+'
    r'(\d+)(?:\s+through\s+(\d+))?\s+'
    r'((?:\d{1,3}\.){3}\d{1,3}|\S+)'
    r'(?:\s+(\d+))?'
    r'(?:\s+comment\s+"([^"]*)")?', re.I)
PAT_STATIC_BLOCK_PROTO = re.compile(r'protocol[:\s]+(tcp|udp|any)', re.I)


def _add(rules, seen, proto, iface, ep, iip, ip2, cmt, line_s, ipv6=False):
    iface = (iface or "any").strip()
    cmt = (cmt or "").strip()
    if not re.match(r'(?:\d{1,3}\.){3}\d{1,3}$', iip or "") and ":" not in (iip or ""):
        return
    key = (proto.upper(), ep, iip, ip2 or ep, iface)
    if key in seen:
        return
    seen.add(key)
    rules.append({
        "protocol": proto.upper(),
        "ext_port": ep,
        "int_ip": iip,
        "int_port": ip2 or ep,
        "interface": iface,
        "comment": cmt,
        "full_line": line_s,
        "ipv6": ipv6,
    })


def parse_port_forward_text(all_output: str, ipv6=False) -> List[dict]:
    rules = []
    seen = set()
    for line in (all_output or "").splitlines():
        ls = line.strip()
        if not ls:
            continue
        m = PAT_STATIC.search(ls)
        if m and not ipv6:
            proto, iface, p1, p2, dest, toport, cmt = m.groups()
            ep = f"{p1}-{p2}" if p2 else p1
            _add(rules, seen, proto, iface, ep, dest, toport or p1, cmt, ls)
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
            ("show ipv6 static", 20, False),
            ("show ipv6 nat destination", 20, False),
            ("show running-config", 90, True),
        ]
    else:
        cmds = [
            ("show ip static", 25, False),
            ("show ip nat destination", 25, False),
            ("show ip nat", 20, False),
            ("show running-config", 90, True),
        ]
    for cmd, t, large in cmds:
        try:
            out = await (self.execute_large(cmd, timeout=t) if large
                         else self.execute(cmd, timeout=t))
        except Exception as e:
            out = f"(ошибка: {e})"
        raw_parts.append(
            f"{'='*55}\n>>> {cmd}\n{'='*55}\n{out or '(пусто)'}\n")
        low = (out or "").lower()[:80]
        if out and len(out) > 5 and "unknown" not in low and "error" not in low:
            all_output += "\n" + out
    rules = parse_port_forward_text(all_output, ipv6=ipv6)
    return rules, "\n".join(raw_parts)


def install():
    from .client import KeeneticAdvancedClient
    KeeneticAdvancedClient.get_port_forwardings = get_port_forwardings
