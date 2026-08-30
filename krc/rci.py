# -*- coding: utf-8 -*-
import json
from typing import Any, List, Optional, Tuple
from urllib.request import (
    HTTPDigestAuthHandler,
    HTTPPasswordMgrWithDefaultRealm,
    build_opener,
)

from . import natparse


def _rci_get(host: str, user: str, password: str, path: str, timeout=20) -> Any:
    base = f"http://{host}"
    mgr = HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, base, user, password)
    opener = build_opener(HTTPDigestAuthHandler(mgr))
    url = base + path
    with opener.open(url, timeout=timeout) as resp:
        raw = resp.read()
    text = raw.decode("utf-8")
    return json.loads(text) if text.strip() else None


def _as_list(data: Any) -> List[Any]:
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("host", "hosts", "item", "list", "entry", "rule", "rules"):
            val = data.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                return [val]
        if any(k in data for k in ("ip", "mac", "name", "protocol", "port")):
            return [data]
    return []


def _first(d: dict, *keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return ""


def parse_nat_json(data: Any) -> List[dict]:
    rules = []
    seen = set()
    for item in _as_list(data):
        if not isinstance(item, dict):
            continue
        proto = str(_first(item, "protocol", "proto") or "tcp")
        iface = str(_first(item, "interface", "from") or "any")
        port = _first(item, "port", "ext_port")
        end_port = _first(item, "end-port", "end_port")
        dest = str(_first(item, "to-address", "to_address", "to-host",
                         "to_host", "address", "ip"))
        to_port = _first(item, "to-port", "to_port")
        cmt = str(_first(item, "description", "comment"))
        if end_port and port:
            ep = f"{port}-{end_port}"
        else:
            ep = str(port or "")
        natparse._add(
            rules, seen, proto, iface, ep, dest, to_port or port, cmt,
            json.dumps(item, ensure_ascii=False),
        )
    return rules


def parse_hotspot_json(data: Any) -> List[dict]:
    clients = []
    seen = set()
    for item in _as_list(data):
        if not isinstance(item, dict):
            continue
        ip = str(_first(item, "ip", "ipv4", "address"))
        mac = str(_first(item, "mac", "hwaddr", "hardware")).lower()
        name = str(_first(item, "name", "hostname", "host-name", "dnsname",
                         "description"))
        key = (ip, mac)
        if key in seen or (not ip and not mac):
            continue
        seen.add(key)
        clients.append({
            "ip": ip,
            "mac": mac,
            "name": name,
            "source": "RCI hotspot",
        })
    return clients


async def get_port_forwardings(self, ipv6=False):
    raw_parts = []
    rules: List[dict] = []
    path = "/rci/show/ipv6/static" if ipv6 else "/rci/show/ip/nat"
    try:
        data = await asyncio_to_thread(
            _rci_get, self.host, self.username, self.password, path)
        rules = parse_nat_json(data)
        raw_parts.append(
            f">>> GET {path}\n"
            f"{json.dumps(data, ensure_ascii=False, indent=2)[:8000]}\n"
            f"RCI rules: {len(rules)}\n")
    except Exception as e:
        raw_parts.append(f">>> GET {path}\n(RCI error: {e})\n")
    if not rules:
        tel_rules, tel_raw = await natparse.get_port_forwardings(self, ipv6=ipv6)
        raw_parts.append(tel_raw)
        if tel_rules:
            rules = tel_rules
    return rules, "\n".join(raw_parts)


async def get_clients(self):
    clients: List[dict] = []
    try:
        data = await asyncio_to_thread(
            _rci_get, self.host, self.username, self.password,
            "/rci/show/ip/hotspot")
        clients = parse_hotspot_json(data)
    except Exception:
        clients = []
    if not clients:
        from . import sysmon
        return await sysmon.get_clients(self)
    return clients


def asyncio_to_thread(fn, *args):
    import asyncio
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(None, lambda: fn(*args))


def install():
    from .client import KeeneticAdvancedClient
    KeeneticAdvancedClient.get_port_forwardings = get_port_forwardings
    KeeneticAdvancedClient.get_clients = get_clients
