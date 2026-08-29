#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
import threading
import sys
import re
from typing import List, Dict, Optional, Tuple
import telnetlib3
from datetime import datetime

if sys.platform == 'win32':
    try:
        asyncio.set_event_loop_policy(
            asyncio.WindowsSelectorEventLoopPolicy())
    except AttributeError:
        pass

DEFAULT_INTERFACE = "GigabitEthernet1"
BATCH_CHUNK_SIZE  = 300


def strip_ansi(data: str) -> str:
    data = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', data)
    data = re.sub(r'\[[0-9;]*[A-Za-z]', '', data)
    data = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', data)
    return data


def decode_output(data: str) -> str:
    if not data:
        return data
    def looks_like_mojibake(s):
        return sum(1 for c in s if '\x80' <= c <= '\xFF') > 2
    if looks_like_mojibake(data):
        try:
            return data.encode('latin-1').decode('cp1251')
        except Exception:
            try:
                return data.encode('latin-1').decode('utf-8')
            except Exception:
                pass
    return data


def cidr_to_mask(cidr: int) -> str:
    mask_bits = (0xFFFFFFFF >> (32 - cidr)) << (32 - cidr)
    return '.'.join([str((mask_bits >> (8 * i)) & 0xFF)
                     for i in reversed(range(4))])


def mask_to_cidr(mask: str) -> Optional[int]:
    try:
        parts = mask.split('.')
        if len(parts) == 4:
            return sum(bin(int(p)).count('1') for p in parts)
    except Exception:
        pass
    return None


def routes_to_bat_lines(routes, gateway="0.0.0.0"):
    lines = []
    for r in routes:
        dest = r.get("destination", "")
        if not dest:
            continue
        if "/" in dest:
            ip, prefix = dest.split("/")
            try:
                mask = cidr_to_mask(int(prefix))
            except Exception:
                mask = "255.255.255.255"
        else:
            ip, mask = dest, "255.255.255.255"
        lines.append(f"route add {ip} mask {mask} {gateway}")
    return lines


def save_routes_to_file(routes, filepath, gateway="0.0.0.0"):
    lines = routes_to_bat_lines(routes, gateway)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines) + "\n")
    return len(lines)


# ═══════════════════════════════════════════════════════════════════
# КЛИЕНТ
# ═══════════════════════════════════════════════════════════════════

class KeeneticAdvancedClient:

    def __init__(self, host, port, login, password):
        self.host = host
        self.port = port
        self.login = login
        self.password = password
        self.reader = None
        self.writer = None
        self.connected = False

    async def connect(self):
        try:
            self.reader, self.writer = await telnetlib3.open_connection(
                self.host, self.port, encoding='latin-1',
                connect_timeout=10)
            await self._expect_prompt(
                ["Login:", "Username:", "login:"], timeout=8)
            self.writer.write(f"{self.login}\n")
            await self.writer.drain()
            await self._expect_prompt(
                ["Password:", "password:"], timeout=8)
            self.writer.write(f"{self.password}\n")
            await self.writer.drain()
            await self._expect_prompt(
                ["(config)>", "> ", "# "], timeout=12)
            self.connected = True
            return True, f"Подключено к {self.host}:{self.port}"
        except Exception as e:
            self.connected = False
            return False, f"Ошибка Telnet: {e}"

    async def _expect_prompt(self, prompts, timeout=10):
        output = ""
        try:
            while True:
                try:
                    data = await asyncio.wait_for(
                        self.reader.read(1024), timeout=timeout)
                except asyncio.TimeoutError:
                    break
                if not data:
                    break
                output += data
                for p in prompts:
                    if p in output:
                        return
        except Exception as e:
            raise Exception(f"Ошибка промпта: {e}")

    async def execute(self, command, timeout=15):
        if not self.connected:
            raise Exception("Нет подключения")
        self.writer.write(f"{command}\n")
        await self.writer.drain()
        output = ""
        while True:
            try:
                data = await asyncio.wait_for(
                    self.reader.read(4096), timeout=timeout)
                if not data:
                    break
                output += data
                tail = output[-150:]
                if "(config)>" in tail or re.search(r'\w+>\s*$', tail):
                    break
            except asyncio.TimeoutError:
                break
        output = strip_ansi(output)
        output = decode_output(output)
        clean = []
        for line in output.splitlines():
            s = line.strip()
            if not s:
                continue
            if s == command.strip():
                continue
            if re.match(r'^[\w\-]*(config)?>.*$', s):
                continue
            clean.append(line)
        return "\n".join(clean).strip()

    async def execute_large(self, command, timeout=60):
        if not self.connected:
            raise Exception("Нет подключения")
        self.writer.write(f"{command}\n")
        await self.writer.drain()
        output = ""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            rem = deadline - asyncio.get_event_loop().time()
            if rem <= 0:
                break
            try:
                data = await asyncio.wait_for(
                    self.reader.read(4096), timeout=min(rem, 5.0))
                if not data:
                    break
                output += data
                tail = output[-200:]
                if "--More--" in tail or "-- More --" in tail:
                    self.writer.write(" ")
                    await self.writer.drain()
                    continue
                if "(config)>" in tail or re.search(r'\w+>\s*$', tail):
                    break
            except asyncio.TimeoutError:
                break
        output = strip_ansi(output)
        output = re.sub(r'--+\s*[Mm]ore\s*--+.*', '', output)
        output = decode_output(output)
        clean = []
        for line in output.splitlines():
            s = line.strip()
            if not s:
                continue
            if s == command.strip():
                continue
            if re.match(r'^[\w\-]*(config)?>.*$', s):
                continue
            clean.append(line)
        return "\n".join(clean).strip()

    # ── Маршруты ──────────────────────────────────────────────────

    async def get_static_routes(self, ipv6=False):
        if ipv6:
            out = await self.execute("show ipv6 route static")
            if not out or "error" in out.lower() or "unknown" in out.lower():
                out = await self.execute("show ipv6 route")
        else:
            out = await self.execute("show ip route static")
            if not out or "error" in out.lower() or "unknown" in out.lower():
                out = await self.execute("show ip route")
        routes = []
        if not out:
            return routes
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith(("Flags", "---", "Network")):
                continue
            if not re.search(r'\d+\.\d+\.\d+\.\d+|[\da-f:]+::', line, re.I):
                continue
            parts = line.split()
            dest = gw = ""
            if parts[0] in ("S", "C", "O", "R", "B", "K"):
                if len(parts) >= 2:
                    dest = parts[1]
                if "via" in parts:
                    idx = parts.index("via")
                    if idx + 1 < len(parts):
                        gw = parts[idx + 1]
                elif len(parts) >= 3:
                    gw = parts[2]
            else:
                dest = parts[0]
                if len(parts) >= 2:
                    gw = parts[1]
            if dest:
                routes.append({"destination": dest, "gateway": gw,
                               "full_line": line,
                               "type": "ipv6" if ipv6 else "ipv4"})
        return routes

    async def add_static_route(self, destination, gateway=None,
                               interface=None, exclusive=False,
                               ipv6=False):
        cmd = ("ipv6 route" if ipv6 else "ip route") + f" {destination}"
        gc = (gateway or "").strip()
        ic = (interface or "").strip()
        if gc and gc != "0.0.0.0":
            cmd += f" {gc}"
            if ic:
                cmd += f" {ic}"
        elif ic:
            cmd += f" {ic}"
        else:
            cmd += " 0.0.0.0"
        if exclusive:
            cmd += " exclusive"
        result = await self.execute(cmd)
        if any(e in result.lower() for e in (
            "error","invalid","unknown","bad","failed",
            "not found","parse error","no such")):
            raise Exception(f"Ошибка роутера:\n{result}\n\nКоманда: {cmd}")
        return result

    async def delete_static_route(self, destination, gateway=None,
                                  ipv6=False):
        cmd = ("no ipv6 route" if ipv6 else "no ip route") + f" {destination}"
        if gateway and gateway not in ("0.0.0.0", ""):
            cmd += f" {gateway}"
        await self.execute(cmd)
        await self.execute("system configuration save")

    async def get_interfaces(self):
        raw = await self.execute("show interface summary")
        cmd = "show interface summary"
        if not raw or "error" in raw.lower() or "unknown" in raw.lower():
            raw = await self.execute("show interface")
            cmd = "show interface"
        ifaces = self._parse_interfaces(raw)
        if not ifaces:
            raw2 = await self.execute("show interface")
            raw += "\n" + raw2
            ifaces = self._parse_interfaces(raw)
        if DEFAULT_INTERFACE in ifaces:
            ifaces.remove(DEFAULT_INTERFACE)
        ifaces.insert(0, DEFAULT_INTERFACE)
        return ifaces, f"[{cmd}]\n{raw}"

    def _parse_interfaces(self, output):
        ifaces = []
        SKIP = {
            "name","type","state","link","speed","duplex","mtu","mac",
            "flags","index","queue","ndp","arp","ip6","interface","show",
            "lo","loopback","network","inet","inet6","ether","media",
            "status","description","up","down","connected","disconnected",
            "unknown","error","admin","oper","physical","logical","rxkb","txkb"
        }
        RE = re.compile(
            r'^([A-Z][A-Za-z0-9]+(?:[0-9/.\-][A-Za-z0-9/.\-]*)?)$')
        for line in (output or "").splitlines():
            parts = line.strip().split()
            if not parts:
                continue
            c = parts[0]
            if c.lower() in SKIP:
                continue
            if RE.match(c) and not c.isdigit() and c not in ifaces:
                ifaces.append(c)
        return ifaces

    async def get_ipv6_capabilities(self):
        caps = {"ipv6_supported": False, "ipv6_routes_exist": False}
        try:
            out = await self.execute("show ipv6 interface")
            if out and "error" not in out.lower() and "unknown" not in out.lower():
                caps["ipv6_supported"] = True
            routes = await self.get_static_routes(ipv6=True)
            caps["ipv6_routes_exist"] = len(routes) > 0
        except Exception:
            pass
        return caps

    async def parse_and_add_batch_file(self, filepath,
                                       default_interface=None,
                                       ipv6=False,
                                       gw_mode="interface_only",
                                       progress_callback=None):
        success_count = error_count = 0
        errors = []
        content = None
        for enc in ('utf-8', 'cp1251', 'latin-1'):
            try:
                with open(filepath, 'r', encoding=enc) as f:
                    content = f.readlines()
                break
            except UnicodeDecodeError:
                continue
        if content is None:
            raise Exception("Не удалось открыть файл")

        parsed = []
        for line_num, line in enumerate(content, 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.lower().startswith("rem ") or line.startswith("::"):
                continue
            if ":: rem" in line:
                line = line.split(":: rem")[0].strip()
            if not line:
                continue
            m = re.match(
                r'route\s+ADD\s+([^\s]+)\s+MASK\s+([^\s]+)\s+([^\s]+)',
                line, re.IGNORECASE)
            if m:
                dst_ip, mask, gw_file = m.groups()
                cidr = mask_to_cidr(mask)
                if cidr is None:
                    error_count += 1
                    errors.append(f"Line {line_num}: Неверная маска {mask}")
                    continue
                destination = f"{dst_ip}/{cidr}"
            else:
                parts = line.split()
                if not parts or not re.search(r'\d+\.\d+', parts[0]):
                    continue
                destination = parts[0]
                gw_file = parts[1] if len(parts) >= 2 else None
            parsed.append((line_num, destination, gw_file))

        total = len(parsed)
        if total == 0:
            return 0, error_count, errors

        chunks = [parsed[i:i+BATCH_CHUNK_SIZE]
                  for i in range(0, total, BATCH_CHUNK_SIZE)]
        for ci, chunk in enumerate(chunks, 1):
            if progress_callback:
                progress_callback(ci, len(chunks),
                                  success_count, error_count, total)
            for line_num, destination, gw_file in chunk:
                if gw_mode == "interface_only":
                    gateway, interface = None, default_interface
                elif gw_mode == "gw_and_interface":
                    gateway, interface = gw_file, default_interface
                else:
                    gateway, interface = gw_file, None
                try:
                    await self.add_static_route(
                        destination=destination, gateway=gateway,
                        interface=interface, ipv6=ipv6)
                    success_count += 1
                except Exception as exc:
                    error_count += 1
                    errors.append(f"Line {line_num} [{destination}]: {exc}")
            await self.execute("system configuration save")
        return success_count, error_count, errors

    # ── Переадресация портов ───────────────────────────────────────

    async def get_pf_diagnostic_raw(self):
        sep = "=" * 60
        parts = []
        for cmd in ("show ip nat destination", "show ip nat",
                    "show ip nat translations"):
            try:
                out = await self.execute(cmd, timeout=25)
            except Exception as e:
                out = f"(ошибка: {e})"
            parts.append(f"{sep}\n>>> {cmd}\n{sep}\n{out or '(пусто)'}\n")
        try:
            out = await self.execute_large("show running-config", timeout=90)
        except Exception as e:
            out = f"(ошибка: {e})"
        parts.append(f"{sep}\n>>> show running-config\n{sep}\n{out or '(пусто)'}\n")
        return "\n".join(parts)

    async def get_port_forwardings(self, ipv6=False):
        raw_parts = []
        all_output = ""
        if ipv6:
            normal = [("show ipv6 nat destination", 20),
                      ("show ipv6 firewall destination", 20)]
        else:
            normal = [("show ip nat destination", 25),
                      ("show ip nat", 20)]
        large = [("show running-config", 90)]

        for cmd, t in normal:
            try:
                out = await self.execute(cmd, timeout=t)
            except Exception as e:
                out = f"(ошибка: {e})"
            raw_parts.append(f"{'='*55}\n>>> {cmd}\n{'='*55}\n{out or '(пусто)'}\n")
            if out and len(out) > 5 and "error" not in out.lower()[:80] \
                    and "unknown" not in out.lower()[:80]:
                all_output += "\n" + out

        for cmd, t in large:
            try:
                out = await self.execute_large(cmd, timeout=t)
            except Exception as e:
                out = f"(ошибка: {e})"
            raw_parts.append(f"{'='*55}\n>>> {cmd} [large]\n{'='*55}\n{out or '(пусто)'}\n")
            if out and len(out) > 5:
                all_output += "\n" + out

        raw_diag = "\n".join(raw_parts)
        rules = []
        seen = set()

        PAT_A = re.compile(
            r'ip\s+nat\s+destination\s+proto\s+(\w+)\s+'
            r'from\s+(\S+)\s+port\s+(\d+(?:-\d+)?)\s+'
            r'inside\s+([\d.]+)\s+port\s+(\d+(?:-\d+)?)'
            r'(?:\s+comment\s+"([^"]*)")?', re.I)
        PAT_B = re.compile(
            r'ip\s+nat\s+destination\s+proto\s+(\w+)\s+'
            r'port\s+(\d+(?:-\d+)?)\s+'
            r'inside\s+([\d.]+)\s+port\s+(\d+(?:-\d+)?)'
            r'(?:\s+comment\s+"([^"]*)")?', re.I)
        PAT_C = re.compile(
            r'ip\s+nat\s+destination\s+proto\s+(\w+)\s+'
            r'(?:interface\s+(\S+)\s+)?port\s+(\d+(?:-\d+)?)\s+'
            r'inside\s+([\d.]+)\s+port\s+(\d+(?:-\d+)?)'
            r'(?:\s+comment\s+"([^"]*)")?', re.I)
        PAT_D = re.compile(
            r'^(tcp|udp|any)\s+(\d+(?:-\d+)?)\s+'
            r'((?:\d{1,3}\.){3}\d{1,3})\s+(\d+(?:-\d+)?)'
            r'(?:\s+(\S+))?', re.I)
        PAT_V6 = re.compile(
            r'ipv6\s+(?:\w+\s+)*destination\s+proto\s+(\w+)\s+'
            r'(?:from\s+(\S+)\s+)?port\s+(\d+(?:-\d+)?)\s+'
            r'inside\s+([\da-fA-F:.]+)\s+port\s+(\d+(?:-\d+)?)'
            r'(?:\s+comment\s+"([^"]*)")?', re.I)
        HDR = re.compile(
            r'^[-=\s]*(Proto|Index|Flags|Interface|Network|'
            r'Type|Source|Destination|ExtPort|IntPort|IntIP)\b', re.I)

        def add_rule(proto, iface, ep, iip, ip2, cmt, line_s):
            iface = (iface or "any").strip()
            cmt = (cmt or "").strip()
            key = (proto.upper(), ep, iip, ip2, iface)
            if key not in seen:
                seen.add(key)
                rules.append({"protocol": proto.upper(),
                               "ext_port": ep, "int_ip": iip,
                               "int_port": ip2, "interface": iface,
                               "comment": cmt, "full_line": line_s,
                               "ipv6": ipv6})

        for line in all_output.splitlines():
            ls = line.strip()
            if not ls or HDR.match(ls):
                continue
            if ipv6:
                m = PAT_V6.search(ls)
                if m:
                    add_rule(*m.groups(), ls)
                continue
            m = PAT_A.search(ls)
            if m:
                add_rule(*m.groups(), ls)
                continue
            m = PAT_C.search(ls)
            if m:
                add_rule(*m.groups(), ls)
                continue
            m = PAT_B.search(ls)
            if m:
                g = m.groups()
                add_rule(g[0], "any", g[1], g[2], g[3], g[4], ls)
                continue
            m = PAT_D.match(ls)
            if m:
                proto, ep, iip, ip2, iface = m.groups()
                try:
                    if (int(ep.split('-')[0]) < 60000 and
                            any(iip.startswith(p)
                                for p in ("192.168.", "10.", "172."))):
                        add_rule(proto, iface, ep, iip, ip2, "", ls)
                except Exception:
                    pass

        return rules, raw_diag

    async def get_nat_sessions(self):
        raw_parts = []
        all_output = ""
        for cmd, t in [("show ip nat translations", 20),
                       ("show upnp", 15), ("show ip nat", 20)]:
            try:
                out = await self.execute(cmd, timeout=t)
            except Exception as e:
                out = f"(ошибка: {e})"
            raw_parts.append(f"{'='*55}\n>>> {cmd}\n{'='*55}\n{out or '(пусто)'}\n")
            if out and "error" not in out.lower()[:60] \
                    and "unknown" not in out.lower()[:60]:
                all_output += "\n" + out
        sessions = []
        seen = set()
        PAT = re.compile(
            r'^(TCP|UDP)\s+([\d.]+)\s+(\d+)\s+([\d.]+)\s+(\d+)', re.I)
        for line in all_output.splitlines():
            ls = line.strip()
            if re.match(r'^(Type|Proto|Source|Destination|[-=]+)', ls, re.I):
                continue
            m = PAT.match(ls)
            if m:
                proto, si, sp, di, dp = m.groups()
                key = (proto.upper(), si, sp, di, dp)
                if key not in seen:
                    seen.add(key)
                    sessions.append({"protocol": proto.upper(),
                                     "src_ip": si, "src_port": sp,
                                     "dst_ip": di, "dst_port": dp,
                                     "full_line": ls})
        return sessions, "\n".join(raw_parts)

    async def add_port_forwarding(self, protocol, ext_port, int_ip,
                                  int_port, interface="", comment="",
                                  ipv6=False):
        fp = interface.strip() if interface.strip() else "any"
        cp = f' comment "{comment}"' if comment.strip() else ""
        if ipv6:
            cmd = (f"ipv6 firewall destination proto {protocol.lower()} "
                   f"from {fp} port {ext_port} "
                   f"inside {int_ip} port {int_port}{cp}")
        else:
            cmd = (f"ip nat destination proto {protocol.lower()} "
                   f"from {fp} port {ext_port} "
                   f"inside {int_ip} port {int_port}{cp}")
        result = await self.execute(cmd)
        if any(e in result.lower() for e in (
            "error","invalid","unknown","bad","failed",
            "not found","parse error","no such")):
            raise Exception(f"Ошибка роутера:\n{result}\n\nКоманда: {cmd}")
        return result

    async def delete_port_forwarding(self, protocol, ext_port, int_ip,
                                     int_port, interface="", ipv6=False):
        fp = interface.strip() if interface.strip() else "any"
        if ipv6:
            cmd = (f"no ipv6 firewall destination proto {protocol.lower()} "
                   f"from {fp} port {ext_port} "
                   f"inside {int_ip} port {int_port}")
        else:
            cmd = (f"no ip nat destination proto {protocol.lower()} "
                   f"from {fp} port {ext_port} "
                   f"inside {int_ip} port {int_port}")
        result = await self.execute(cmd)
        await self.execute("system configuration save")
        return result

    # ── Системный монитор ──────────────────────────────────────────

    async def get_system_info_raw(self) -> Dict:
        """
        Возвращает сырые строки всех команд + распарсенные данные.
        Keenetic NDMS2 формат:
          show system  → hostname, uptime (секунды!), memory, cpuload
          show version → ndmsVersion, deviceName, ...
        """
        raw_all = {}

        # --- show system ---
        try:
            out = await self.execute("show system", timeout=12)
            raw_all["show_system"] = out
        except Exception as e:
            raw_all["show_system"] = f"(ошибка: {e})"

        # --- show version ---
        try:
            out = await self.execute("show version", timeout=10)
            raw_all["show_version"] = out
        except Exception as e:
            raw_all["show_version"] = f"(ошибка: {e})"

        # --- show interface (для трафика) ---
        try:
            out = await self.execute("show interface", timeout=20)
            raw_all["show_interface"] = out
        except Exception as e:
            raw_all["show_interface"] = f"(ошибка: {e})"

        # --- show ip address ---
        try:
            out = await self.execute("show ip address", timeout=10)
            raw_all["show_ip_address"] = out
        except Exception as e:
            raw_all["show_ip_address"] = f"(ошибка: {e})"

        # --- show ip dns ---
        try:
            out = await self.execute("show ip dns", timeout=8)
            raw_all["show_ip_dns"] = out
        except Exception as e:
            raw_all["show_ip_dns"] = f"(ошибка: {e})"

        return raw_all

    async def get_system_info(self) -> Dict:
        """
        Парсит данные Keenetic NDMS2.

        Реальный формат show system (NDMS2):
          hostname: Keenetic-8022
          uptime: 332765          ← секунды!
          memory:
            total: 262144         ← КБ
            free: 131072
            buffers: 8192
            cached: 32768
          cpuload: 5              ← проценты
          temperature: 45         ← или отсутствует

        show version:
          ndmsVersion: 2.xx.xx
          deviceName: Hero (KN-1012)
        """
        raw = await self.get_system_info_raw()

        info: Dict = {
            "uptime": "—", "cpu": "—",
            "ram_used": "—", "ram_total": "—", "ram_pct": 0,
            "temp": "—", "model": "—", "firmware": "—",
            "wan_ip": "—", "dns": "—", "hostname": "—",
            "_raw": raw,
        }

        # ── Парсим show system ─────────────────────────────────────
        sys_out = raw.get("show_system", "")
        # Keenetic выводит YAML-подобный формат с отступами
        # hostname: Keenetic-8022
        # uptime: 332765
        # memory:
        #   total: 262144
        #   free: 131072
        # cpuload: 5
        # temperature: 52

        in_memory = False
        mem_total = mem_free = 0

        for line in sys_out.splitlines():
            s = line.strip()
            if not s:
                in_memory = False
                continue

            # hostname
            m = re.match(r'hostname[:\s]+(\S+)', s, re.I)
            if m:
                info["hostname"] = m.group(1)
                continue

            # uptime (секунды → читаемый вид)
            m = re.match(r'uptime[:\s]+(\d+)', s, re.I)
            if m:
                secs = int(m.group(1))
                info["uptime"] = self._fmt_uptime(secs)
                continue

            # cpuload
            m = re.match(r'cpuload[:\s]+(\d+)', s, re.I)
            if m:
                info["cpu"] = m.group(1) + " %"
                continue

            # temperature
            m = re.match(r'temperature[:\s]+([\d.]+)', s, re.I)
            if m:
                info["temp"] = m.group(1) + " °C"
                continue

            # memory block
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
                # Если строка не похожа на память — выходим
                if not re.match(r'(total|free|buffers|cached|used)[:\s]', s, re.I):
                    in_memory = False

        # Вычисляем RAM (Keenetic даёт в КБ)
        if mem_total > 0:
            mem_used = mem_total - mem_free
            # Переводим КБ → МБ
            info["ram_total"] = str(round(mem_total / 1024))
            info["ram_used"]  = str(round(mem_used  / 1024))
            try:
                info["ram_pct"] = round(mem_used / mem_total * 100)
            except Exception:
                pass

        # ── Парсим show version ────────────────────────────────────
        ver_out = raw.get("show_version", "")
        for line in ver_out.splitlines():
            s = line.strip()
            # ndmsVersion: 2.16.C.11.0-0  /  version: 5.00.C.11.0-0
            m = re.match(r'(?:ndms(?:Version)?|version|release)[:\s]+(\S+)',
                         s, re.I)
            if m and info["firmware"] == "—":
                info["firmware"] = m.group(1)

            # deviceName: Hero (KN-1012)  /  model: ...
            m = re.match(r'(?:deviceName|device|model|product)[:\s]+(.+)',
                         s, re.I)
            if m and info["model"] == "—":
                info["model"] = m.group(1).strip()

            # hostname тоже бывает в show version
            m = re.match(r'hostname[:\s]+(\S+)', s, re.I)
            if m and info["hostname"] == "—":
                info["hostname"] = m.group(1)

        # Если show system дал что-то но не дал модель — пробуем иначе
        if info["model"] == "—":
            for line in sys_out.splitlines():
                m = re.match(r'(?:deviceName|device|model|name)[:\s]+(.+)',
                             line.strip(), re.I)
                if m:
                    info["model"] = m.group(1).strip()
                    break

        # ── WAN IP ────────────────────────────────────────────────
        ip_out = raw.get("show_ip_address", "")
        for line in ip_out.splitlines():
            m = re.search(r'(\d+\.\d+\.\d+\.\d+)', line)
            if m:
                ip = m.group(1)
                pts = ip.split('.')
                private = (
                    pts[0] == '10' or
                    (pts[0] == '172' and 16 <= int(pts[1]) <= 31) or
                    (pts[0] == '192' and pts[1] == '168') or
                    ip.startswith('127.')
                )
                if not private:
                    info["wan_ip"] = ip
                    break

        # ── DNS ───────────────────────────────────────────────────
        dns_out = raw.get("show_ip_dns", "")
        dns_list = re.findall(r'\d+\.\d+\.\d+\.\d+', dns_out)
        if dns_list:
            info["dns"] = ", ".join(dns_list[:3])

        return info

    @staticmethod
    def _fmt_uptime(seconds: int) -> str:
        """332765 сек → '3 д 20:26:05'"""
        d  = seconds // 86400
        h  = (seconds % 86400) // 3600
        m  = (seconds % 3600)  // 60
        s  = seconds % 60
        if d > 0:
            return f"{d} д {h:02d}:{m:02d}:{s:02d}"
        return f"{h:02d}:{m:02d}:{s:02d}"

    async def get_interfaces_traffic(self) -> List[Dict]:
        """
        Парсит show interface для Keenetic NDMS2.

        Формат блока:
          GigabitEthernet0/Vlan1
            id: 14
            index: 14
            type: bridge
            description: XRay
            link: up
            connected: yes
            state: up
            mtu: 1500
            address: 52:ff:20:ee:22:52
            ...
            inet 127.0.0.1/8
            ...
            rxbytes: 1234567
            txbytes: 7654321
        """
        raw = ""
        try:
            raw = await self.execute("show interface", timeout=20)
        except Exception:
            return []

        interfaces: List[Dict] = []

        # Имя интерфейса — строка без отступа, соответствует паттерну
        IFACE_RE = re.compile(
            r'^([A-Za-z][A-Za-z0-9]+(?:[0-9/.\-][A-Za-z0-9/.\-]*)?)$')
        SKIP_NAMES = {
            "description", "type", "state", "link", "mtu", "address",
            "inet", "index", "id", "connected", "flags", "queue",
            "rxbytes", "txbytes", "rxpackets", "txpackets",
            "rxerrors", "txerrors", "rxdrops", "txdrops",
        }

        current: Dict = {}

        def flush():
            if current.get("name"):
                interfaces.append(dict(current))

        for line in raw.splitlines():
            # Строка без начального пробела — возможное имя интерфейса
            if line and not line[0].isspace():
                s = line.strip()
                m = IFACE_RE.match(s)
                if m and s.lower() not in SKIP_NAMES and len(s) < 40:
                    flush()
                    current = {
                        "name": s, "state": "?",
                        "rx_bytes": 0, "tx_bytes": 0,
                        "ip": "—", "mac": "—",
                        "description": "",
                    }
                    continue

            if not current:
                continue

            s = line.strip()
            if not s:
                continue

            # description
            m = re.match(r'description[:\s]+(.+)', s, re.I)
            if m:
                current["description"] = m.group(1).strip()
                continue

            # link / state
            m = re.match(r'(?:link|state)[:\s]+(\w+)', s, re.I)
            if m:
                current["state"] = m.group(1).lower()
                continue

            # MAC address
            m = re.match(r'address[:\s]+([\da-f:]{17})', s, re.I)
            if m:
                current["mac"] = m.group(1)
                continue

            # IP (inet 192.168.1.1/24)
            m = re.match(r'inet\s+([\d.]+)', s, re.I)
            if m:
                current["ip"] = m.group(1)
                continue

            # RX bytes
            m = re.match(r'rxbytes[:\s]+(\d+)', s, re.I)
            if m:
                current["rx_bytes"] = int(m.group(1))
                continue

            # TX bytes
            m = re.match(r'txbytes[:\s]+(\d+)', s, re.I)
            if m:
                current["tx_bytes"] = int(m.group(1))
                continue

        flush()

        # Убираем дубли (один интерфейс может встречаться дважды)
        seen_names: set = set()
        unique: List[Dict] = []
        for iface in interfaces:
            if iface["name"] not in seen_names:
                seen_names.add(iface["name"])
                unique.append(iface)

        return unique

    async def get_clients(self) -> List[Dict]:
        """
        DHCP-привязки + ARP.
        Keenetic NDMS2 формат show ip dhcp bindings:
          ip: 192.168.1.100
          mac: aa:bb:cc:dd:ee:ff
          hostname: MyPC
          ...  (блоки разделены пустыми строками)
        """
        clients: List[Dict] = []
        seen: set = set()

        # show ip dhcp bindings — блочный формат NDMS2
        try:
            out = await self.execute("show ip dhcp bindings", timeout=12)
            # Пробуем блочный формат
            blk_ip = blk_mac = blk_host = ""
            for line in out.splitlines():
                s = line.strip()
                if not s:
                    if blk_ip and blk_mac:
                        key = blk_mac.lower()
                        if key not in seen:
                            seen.add(key)
                            clients.append({
                                "ip": blk_ip,
                                "mac": blk_mac,
                                "hostname": blk_host or "—",
                                "source": "DHCP",
                            })
                    blk_ip = blk_mac = blk_host = ""
                    continue
                m = re.match(r'ip[:\s]+([\d.]+)', s, re.I)
                if m:
                    blk_ip = m.group(1)
                m = re.match(r'mac[:\s]+([\da-fA-F:]{17})', s, re.I)
                if m:
                    blk_mac = m.group(1)
                m = re.match(r'(?:hostname|name)[:\s]+(.+)', s, re.I)
                if m:
                    blk_host = m.group(1).strip()
                # Табличный формат как fallback
                m = re.match(
                    r'\s*([\d.]+)\s+([\da-fA-F:]{17})\s*(\S*)', s)
                if m and not blk_ip:
                    blk_ip, blk_mac, blk_host = m.groups()

            # Последний блок
            if blk_ip and blk_mac and blk_mac.lower() not in seen:
                seen.add(blk_mac.lower())
                clients.append({"ip": blk_ip, "mac": blk_mac,
                                "hostname": blk_host or "—",
                                "source": "DHCP"})
        except Exception:
            pass

        # show arp
        try:
            out = await self.execute("show arp", timeout=12)
            # Блочный формат NDMS2:
            #   ip: 192.168.1.1
            #   mac: aa:bb:cc:dd:ee:ff
            #   interface: Bridge0
            blk_ip = blk_mac = ""
            for line in out.splitlines():
                s = line.strip()
                if not s:
                    if blk_ip and blk_mac:
                        key = blk_mac.lower()
                        if key not in seen:
                            seen.add(key)
                            clients.append({
                                "ip": blk_ip, "mac": blk_mac,
                                "hostname": "—", "source": "ARP"})
                    blk_ip = blk_mac = ""
                    continue
                m = re.match(r'ip[:\s]+([\d.]+)', s, re.I)
                if m:
                    blk_ip = m.group(1)
                m = re.match(r'mac[:\s]+([\da-fA-F:]{17})', s, re.I)
                if m:
                    blk_mac = m.group(1)
                # Табличный ARP: IP  ether  MAC
                m = re.search(r'([\d.]+)\s+\S+\s+([\da-fA-F:]{17})', s)
                if m and not blk_ip:
                    blk_ip, blk_mac = m.groups()

            if blk_ip and blk_mac and blk_mac.lower() not in seen:
                seen.add(blk_mac.lower())
                clients.append({"ip": blk_ip, "mac": blk_mac,
                                "hostname": "—", "source": "ARP"})
        except Exception:
            pass

        return clients

    async def close(self):
        try:
            if self.writer:
                self.writer.close()
        except Exception:
            pass
        self.connected = False


# ═══════════════════════════════════════════════════════════════════
# ДИАЛОГ: СЫРЫЕ ДАННЫЕ МОНИТОРА (диагностика)
# ═══════════════════════════════════════════════════════════════════

class SysmonRawDialog(tk.Toplevel):
    """Показывает сырой вывод всех команд монитора."""

    def __init__(self, parent: 'KeeneticAdvancedGUI', raw: Dict):
        super().__init__(parent.root)
        self.title("🔬 Сырые данные монитора")
        self.geometry("1000x680")
        self.resizable(True, True)
        self.transient(parent.root)
        self._build(raw)

    def _build(self, raw: Dict):
        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        for cmd_key, title in [
            ("show_system",     "show system"),
            ("show_version",    "show version"),
            ("show_interface",  "show interface"),
            ("show_ip_address", "show ip address"),
            ("show_ip_dns",     "show ip dns"),
        ]:
            frame = ttk.Frame(nb)
            nb.add(frame, text=title)
            txt = scrolledtext.ScrolledText(
                frame, wrap=tk.WORD, font=('Courier', 9))
            txt.pack(fill=tk.BOTH, expand=True)
            content = raw.get(cmd_key, "(нет данных)")
            txt.insert(tk.END, content)
            txt.config(state=tk.DISABLED)

        bf = ttk.Frame(self)
        bf.pack(fill=tk.X, padx=8, pady=(0, 8))
        ttk.Button(bf, text="Закрыть",
                   command=self.destroy).pack(side=tk.RIGHT)


# ═══════════════════════════════════════════════════════════════════
# ДИАЛОГ: ДИАГНОСТИКА ПРАВИЛ ПЕРЕАДРЕСАЦИИ
# ═══════════════════════════════════════════════════════════════════

class PFDiagnosticDialog(tk.Toplevel):
    DEFAULT_FILTER = "nat|destination|port|forward|554|555|inside"

    def __init__(self, parent: 'KeeneticAdvancedGUI'):
        super().__init__(parent.root)
        self.app = parent
        self._full_raw = ""
        self.title("🔬 Диагностика переадресации портов")
        self.geometry("1000x680")
        self.resizable(True, True)
        self.transient(parent.root)
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._run_diagnostic()

    def _build(self):
        top = ttk.Frame(self, padding="10")
        top.pack(fill=tk.X)
        ctrl = ttk.Frame(top)
        ctrl.pack(fill=tk.X)
        self.run_btn = ttk.Button(
            ctrl, text="🔄 Запустить",
            command=self._run_diagnostic)
        self.run_btn.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(ctrl, text="📋 Копировать всё",
                   command=self._copy_all).pack(side=tk.LEFT, padx=(0, 16))
        ttk.Separator(ctrl, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=(0, 12))
        ttk.Label(ctrl, text="Фильтр:").pack(side=tk.LEFT, padx=(0, 4))
        self.filter_var = tk.StringVar(value=self.DEFAULT_FILTER)
        fe = ttk.Entry(ctrl, textvariable=self.filter_var, width=40)
        fe.pack(side=tk.LEFT, padx=(0, 4))
        fe.bind("<KeyRelease>", lambda e: self._apply_filter())
        ttk.Button(ctrl, text="Применить",
                   command=self._apply_filter).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(ctrl, text="Сбросить",
                   command=self._reset_filter).pack(side=tk.LEFT)
        self.status_lbl = ttk.Label(top, text="Запуск...", foreground="blue")
        self.status_lbl.pack(anchor=tk.W, pady=(6, 0))

        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=(4, 0))
        f1 = ttk.Frame(nb)
        nb.add(f1, text="🎯 Ключевые строки")
        self.filtered_txt = scrolledtext.ScrolledText(
            f1, wrap=tk.WORD, font=('Courier', 9))
        self.filtered_txt.pack(fill=tk.BOTH, expand=True)
        self.filtered_txt.tag_config("MATCH", background="#ffffcc")
        self.filtered_txt.tag_config(
            "CMD", foreground="darkblue", font=('Courier', 9, 'bold'))
        f2 = ttk.Frame(nb)
        nb.add(f2, text="📄 Полный вывод")
        self.full_txt = scrolledtext.ScrolledText(
            f2, wrap=tk.WORD, font=('Courier', 9))
        self.full_txt.pack(fill=tk.BOTH, expand=True)
        ttk.Button(self, text="Закрыть",
                   command=self.destroy).pack(pady=8)

    def _run_diagnostic(self):
        if not self.app.connected or not self.app.client:
            self.status_lbl.config(
                text="Нет подключения", foreground="red")
            return
        self.run_btn.config(state=tk.DISABLED)
        self.status_lbl.config(
            text="Сбор данных (30-60 сек)...", foreground="blue")
        for txt in (self.filtered_txt, self.full_txt):
            txt.config(state=tk.NORMAL)
            txt.delete(1.0, tk.END)

        def run():
            try:
                raw = self.app.loop.run_until_complete(
                    self.app.client.get_pf_diagnostic_raw())
                self.after(0, lambda: self._on_done(raw))
            except Exception as e:
                self.after(0, lambda: self._on_error(str(e)))

        threading.Thread(target=run, daemon=True).start()

    def _on_done(self, raw: str):
        self._full_raw = raw
        self.run_btn.config(state=tk.NORMAL)
        self.full_txt.delete(1.0, tk.END)
        self.full_txt.insert(tk.END, raw)
        self.full_txt.config(state=tk.DISABLED)
        self._apply_filter()
        n = len([l for l in raw.splitlines() if l.strip()])
        self.status_lbl.config(text=f"Готово. Строк: {n}",
                               foreground="darkgreen")

    def _on_error(self, msg: str):
        self.run_btn.config(state=tk.NORMAL)
        self.status_lbl.config(text=f"Ошибка: {msg}", foreground="red")

    def _apply_filter(self):
        if not self._full_raw:
            return
        ps = self.filter_var.get().strip() or "."
        try:
            pat = re.compile(ps, re.IGNORECASE)
        except re.error:
            pat = re.compile(re.escape(ps), re.IGNORECASE)
        self.filtered_txt.config(state=tk.NORMAL)
        self.filtered_txt.delete(1.0, tk.END)
        n = 0
        for line in self._full_raw.splitlines():
            if line.startswith((">>>", "===")):
                self.filtered_txt.insert(tk.END, line + "\n", "CMD")
            elif pat.search(line):
                self.filtered_txt.insert(tk.END, line + "\n", "MATCH")
                n += 1
        if n == 0:
            self.filtered_txt.insert(
                tk.END, "\n(Совпадений не найдено)\n")
        self.filtered_txt.config(state=tk.DISABLED)
        self.status_lbl.config(
            text=f"Найдено строк: {n}",
            foreground="darkblue" if n else "darkorange")

    def _reset_filter(self):
        self.filter_var.set(self.DEFAULT_FILTER)
        self._apply_filter()

    def _copy_all(self):
        self.clipboard_clear()
        self.clipboard_append(self._full_raw or "(нет данных)")
        self.status_lbl.config(text="Скопировано", foreground="green")


# ═══════════════════════════════════════════════════════════════════
# ДИАЛОГ: ЖИВЫЕ NAT-СЕССИИ
# ═══════════════════════════════════════════════════════════════════

class NatSessionsDialog(tk.Toplevel):

    def __init__(self, parent: 'KeeneticAdvancedGUI'):
        super().__init__(parent.root)
        self.app = parent
        self._sessions: List[Dict] = []
        self._raw = ""
        self.title("📡 NAT-сессии / UPnP")
        self.geometry("980x560")
        self.resizable(True, True)
        self.transient(parent.root)
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._refresh()

    def _build(self):
        top = ttk.Frame(self, padding="10")
        top.pack(fill=tk.X)
        ttk.Label(top,
                  text="Живые NAT-сессии — текущие соединения (не статические правила).",
                  foreground="darkblue", font=('Arial', 9)
                  ).pack(anchor=tk.W, pady=(0, 6))
        ctrl = ttk.Frame(top)
        ctrl.pack(fill=tk.X)
        self.refresh_btn = ttk.Button(
            ctrl, text="🔄 Обновить", command=self._refresh)
        self.refresh_btn.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(ctrl, text="🔍 Сырой вывод",
                   command=self._show_raw).pack(side=tk.LEFT, padx=(0, 12))
        self.search_var = tk.StringVar()
        se = ttk.Entry(ctrl, textvariable=self.search_var, width=28)
        se.pack(side=tk.LEFT, padx=(0, 4))
        se.bind("<KeyRelease>", lambda e: self._filter())
        ttk.Button(ctrl, text="✕",
                   command=lambda: (self.search_var.set(""),
                                    self._filter())).pack(side=tk.LEFT)
        self.status_lbl = ttk.Label(
            top, text="Загрузка...", foreground="gray")
        self.status_lbl.pack(anchor=tk.W, pady=(6, 0))

        tree_f = ttk.Frame(self)
        tree_f.pack(fill=tk.BOTH, expand=True, padx=10, pady=(4, 0))
        cols = ("protocol", "src_ip", "src_port", "dst_ip", "dst_port")
        self.tree = ttk.Treeview(
            tree_f, columns=cols, show="headings", height=20)
        for col, hdr, w in [
            ("protocol", "Протокол", 80),
            ("src_ip", "Источник IP", 160),
            ("src_port", "Исх. порт", 90),
            ("dst_ip", "Назначение IP", 160),
            ("dst_port", "Порт назн.", 90),
        ]:
            self.tree.heading(col, text=hdr)
            self.tree.column(col, width=w)
        sb = ttk.Scrollbar(tree_f, orient=tk.VERTICAL,
                           command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        ttk.Button(self, text="Закрыть",
                   command=self.destroy).pack(pady=8)

    def _refresh(self):
        if not self.app.connected or not self.app.client:
            self.status_lbl.config(
                text="Нет подключения", foreground="red")
            return
        self.refresh_btn.config(state=tk.DISABLED)
        self.status_lbl.config(text="Загрузка...", foreground="blue")

        def run():
            try:
                sessions, raw = self.app.loop.run_until_complete(
                    self.app.client.get_nat_sessions())
                self.after(0, lambda: self._on_loaded(sessions, raw))
            except Exception as e:
                self.after(0, lambda: self.status_lbl.config(
                    text=f"Ошибка: {e}", foreground="red"))
            finally:
                self.after(0, lambda: self.refresh_btn.config(
                    state=tk.NORMAL))

        threading.Thread(target=run, daemon=True).start()

    def _on_loaded(self, sessions, raw):
        self._sessions = sessions
        self._raw = raw
        self._filter()
        n = len(sessions)
        self.status_lbl.config(
            text=f"Сессий: {n}",
            foreground="darkgreen" if n else "gray")

    def _filter(self):
        q = self.search_var.get().strip().lower()
        for item in self.tree.get_children():
            self.tree.delete(item)
        for s in self._sessions:
            vals = (s.get("protocol",""), s.get("src_ip",""),
                    s.get("src_port",""), s.get("dst_ip",""),
                    s.get("dst_port",""))
            if not q or any(q in str(v).lower() for v in vals):
                self.tree.insert("", tk.END, values=vals)

    def _show_raw(self):
        if not self._raw:
            messagebox.showinfo(
                "Нет данных", "Нажмите '🔄 Обновить'.", parent=self)
            return
        win = tk.Toplevel(self)
        win.title("Сырой вывод")
        win.geometry("860x500")
        txt = scrolledtext.ScrolledText(
            win, wrap=tk.WORD, font=('Courier', 9))
        txt.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        txt.insert(tk.END, self._raw)
        txt.config(state=tk.DISABLED)
        ttk.Button(win, text="Закрыть",
                   command=win.destroy).pack(pady=8)


# ═══════════════════════════════════════════════════════════════════
# ДИАЛОГ ДОБАВЛЕНИЯ ПЕРЕАДРЕСАЦИИ
# ═══════════════════════════════════════════════════════════════════

class AddPortForwardDialog(tk.Toplevel):

    def __init__(self, parent: 'KeeneticAdvancedGUI', prefill=None):
        super().__init__(parent.root)
        self.app = parent
        self.prefill = prefill or {}
        self.title("➕ Добавить переадресацию порта")
        self.geometry("680x500")
        self.resizable(False, False)
        self.transient(parent.root)
        self.grab_set()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self):
        main = ttk.Frame(self, padding="20")
        main.pack(fill=tk.BOTH, expand=True)

        pf = ttk.LabelFrame(main, text="IP-версия", padding="8")
        pf.pack(fill=tk.X, pady=(0, 10))
        self.ipv6_var = tk.BooleanVar(value=self.prefill.get("ipv6", False))
        ttk.Radiobutton(pf, text="IPv4 (ip nat destination)",
                        variable=self.ipv6_var, value=False,
                        command=self._update_preview).pack(
            side=tk.LEFT, padx=(0, 20))
        ttk.Radiobutton(pf, text="IPv6 (ipv6 firewall destination)",
                        variable=self.ipv6_var, value=True,
                        command=self._update_preview).pack(side=tk.LEFT)

        params_f = ttk.LabelFrame(main, text="Параметры", padding="12")
        params_f.pack(fill=tk.X, pady=(0, 10))
        params_f.columnconfigure(1, weight=1)

        ttk.Label(params_f, text="Протокол:", width=24, anchor=tk.W).grid(
            row=0, column=0, sticky=tk.W, pady=5)
        self.proto_combo = ttk.Combobox(
            params_f, values=["tcp", "udp", "any"],
            width=10, state="readonly")
        self.proto_combo.grid(row=0, column=1, sticky=tk.W,
                              pady=5, padx=(8, 0))
        self.proto_combo.set(self.prefill.get("protocol", "tcp").lower())

        def entry_row(r, lbl, attr, hint, w=12):
            ttk.Label(params_f, text=lbl, width=24, anchor=tk.W).grid(
                row=r, column=0, sticky=tk.W, pady=5)
            e = ttk.Entry(params_f, width=w)
            e.grid(row=r, column=1, sticky=tk.EW, pady=5, padx=(8, 0))
            ttk.Label(params_f, text=hint, foreground="gray",
                      font=('Arial', 8)).grid(
                row=r, column=2, sticky=tk.W, padx=(6, 0))
            setattr(self, attr, e)

        entry_row(1, "Внешний порт (WAN) *:", "ext_port_entry",
                  "напр. 554")
        entry_row(2, "Внутренний IP (LAN) *:", "int_ip_entry",
                  "IP устройства", w=20)
        entry_row(3, "Внутренний порт (LAN) *:", "int_port_entry",
                  "порт назначения")
        self.ext_port_entry.insert(0, self.prefill.get("ext_port", ""))
        self.int_ip_entry.insert(0, self.prefill.get("int_ip", ""))
        self.int_port_entry.insert(0, self.prefill.get("int_port", ""))

        opt_f = ttk.LabelFrame(main, text="Дополнительно", padding="10")
        opt_f.pack(fill=tk.X, pady=(0, 10))
        opt_f.columnconfigure(1, weight=1)

        ttk.Label(opt_f, text="Входящий интерфейс:", width=24,
                  anchor=tk.W).grid(row=0, column=0, sticky=tk.W, pady=4)
        if_f = ttk.Frame(opt_f)
        if_f.grid(row=0, column=1, sticky=tk.W, pady=4, padx=(8, 0))
        self.iface_combo = ttk.Combobox(if_f, width=22)
        self.iface_combo.pack(side=tk.LEFT, padx=(0, 4))
        ifaces = list(self.app.interface_combo['values'])
        if ifaces:
            self.iface_combo['values'] = ifaces
        self.iface_combo.set(self.prefill.get("interface", ""))
        ttk.Button(if_f, text="🔄", width=3,
                   command=self._refresh_ifaces).pack(side=tk.LEFT)
        ttk.Label(opt_f, text="Пусто → any", foreground="gray",
                  font=('Arial', 8)).grid(
            row=0, column=2, sticky=tk.W, padx=(6, 0))

        ttk.Label(opt_f, text="Описание:", width=24, anchor=tk.W).grid(
            row=1, column=0, sticky=tk.W, pady=4)
        self.comment_entry = ttk.Entry(opt_f, width=36)
        self.comment_entry.grid(row=1, column=1, sticky=tk.EW,
                                pady=4, padx=(8, 0))
        self.comment_entry.insert(0, self.prefill.get("comment", ""))

        pv = ttk.LabelFrame(main, text="Команда", padding="8")
        pv.pack(fill=tk.X, pady=(0, 12))
        self.preview_var = tk.StringVar(value="...")
        ttk.Label(pv, textvariable=self.preview_var,
                  foreground="darkgreen", font=('Courier', 10, 'bold'),
                  wraplength=620, justify=tk.LEFT).pack(anchor=tk.W)

        bf = ttk.Frame(main)
        bf.pack(fill=tk.X)
        self.add_btn = ttk.Button(bf, text="✅ Добавить правило",
                                  command=self._add, width=22)
        self.add_btn.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(bf, text="Закрыть",
                   command=self._on_close, width=12).pack(side=tk.LEFT)
        self.status_lbl = ttk.Label(main, text="", foreground="gray")
        self.status_lbl.pack(anchor=tk.W, pady=(8, 0))

        for w in (self.ext_port_entry, self.int_ip_entry,
                  self.int_port_entry, self.comment_entry):
            w.bind('<KeyRelease>', lambda e: self._update_preview())
        self.proto_combo.bind('<<ComboboxSelected>>',
                              lambda e: self._update_preview())
        self.iface_combo.bind('<<ComboboxSelected>>',
                              lambda e: self._update_preview())
        self.iface_combo.bind('<KeyRelease>',
                              lambda e: self._update_preview())
        self._update_preview()

    def _update_preview(self, *_):
        proto = self.proto_combo.get().strip() or "tcp"
        ep = self.ext_port_entry.get().strip()
        iip = self.int_ip_entry.get().strip()
        ip2 = self.int_port_entry.get().strip()
        iface = self.iface_combo.get().strip()
        comment = self.comment_entry.get().strip()
        ipv6 = self.ipv6_var.get()
        if not ep or not iip or not ip2:
            self.preview_var.set("Заполните обязательные поля (*)")
            return
        fp = iface if iface else "any"
        cp = f' comment "{comment}"' if comment else ""
        if ipv6:
            cmd = (f"ipv6 firewall destination proto {proto} "
                   f"from {fp} port {ep} inside {iip} port {ip2}{cp}")
        else:
            cmd = (f"ip nat destination proto {proto} "
                   f"from {fp} port {ep} inside {iip} port {ip2}{cp}")
        self.preview_var.set(cmd)

    def _refresh_ifaces(self):
        if not self.app.connected or not self.app.client:
            messagebox.showwarning("Нет подключения",
                                   "Подключитесь!", parent=self)
            return

        def run():
            try:
                ifaces, _ = self.app.loop.run_until_complete(
                    self.app.client.get_interfaces())
                self.after(0, lambda: self._set_ifaces(ifaces))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror(
                    "Ошибка", str(e), parent=self))

        threading.Thread(target=run, daemon=True).start()

    def _set_ifaces(self, ifaces):
        self.iface_combo['values'] = ifaces
        self.app.interface_combo['values'] = ifaces

    def _add(self):
        if not self.app.connected or not self.app.client:
            messagebox.showwarning("Нет подключения",
                                   "Подключитесь!", parent=self)
            return
        proto = self.proto_combo.get().strip()
        ep = self.ext_port_entry.get().strip()
        iip = self.int_ip_entry.get().strip()
        ip2 = self.int_port_entry.get().strip()
        iface = self.iface_combo.get().strip()
        comment = self.comment_entry.get().strip()
        ipv6 = self.ipv6_var.get()
        for val, name in [(proto, "протокол"), (ep, "внешний порт"),
                          (iip, "внутренний IP"), (ip2, "внутренний порт")]:
            if not val:
                messagebox.showerror("Ошибка",
                                     f"Укажите {name}!", parent=self)
                return
        self.add_btn.config(state=tk.DISABLED)
        self.status_lbl.config(text="Отправка...", foreground="blue")

        def run():
            try:
                self.app.loop.run_until_complete(
                    self.app.client.add_port_forwarding(
                        protocol=proto, ext_port=ep, int_ip=iip,
                        int_port=ip2, interface=iface,
                        comment=comment, ipv6=ipv6))
                self.app.loop.run_until_complete(
                    self.app.client.execute("system configuration save"))
                label = (f"{'IPv6' if ipv6 else 'IPv4'} "
                         f"{proto.upper()} :{ep}→{iip}:{ip2}")
                self.app._log(f"✅ Правило: {label}", "SUCCESS")
                self.app._refresh_port_forwardings()
                self.after(0, lambda: self._on_success(label))
            except Exception as e:
                self.app._log(f"❌ Ошибка: {e}", "ERROR")
                self.after(0, lambda: self._on_err(str(e)))

        threading.Thread(target=run, daemon=True).start()

    def _on_success(self, label):
        self.add_btn.config(state=tk.NORMAL)
        self.status_lbl.config(text=f"✅ {label}", foreground="green")
        for w in (self.ext_port_entry, self.int_ip_entry,
                  self.int_port_entry, self.comment_entry):
            w.delete(0, tk.END)
        self._update_preview()
        messagebox.showinfo("Успех",
                            f"Правило добавлено:\n{label}", parent=self)

    def _on_err(self, msg):
        self.add_btn.config(state=tk.NORMAL)
        self.status_lbl.config(text="❌ Ошибка", foreground="red")
        messagebox.showerror("Ошибка", msg, parent=self)

    def _on_close(self):
        self.grab_release()
        self.destroy()


# ═══════════════════════════════════════════════════════════════════
# ДИАЛОГИ МАРШРУТОВ
# ═══════════════════════════════════════════════════════════════════

class BatchImportDialog(tk.Toplevel):

    def __init__(self, parent: 'KeeneticAdvancedGUI', filepath=""):
        super().__init__(parent.root)
        self.app = parent
        self.title("📦 Пакетная загрузка маршрутов")
        self.geometry("720x600")
        self.resizable(True, True)
        self.transient(parent.root)
        self.grab_set()
        self._build(filepath)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self, filepath):
        main = ttk.Frame(self, padding="15")
        main.pack(fill=tk.BOTH, expand=True)

        info = ttk.LabelFrame(main, text="Форматы", padding="8")
        info.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(info, font=('Courier', 9), justify=tk.LEFT, text=(
            "route add 218.0.0.0 mask 255.255.255.0 0.0.0.0\n"
            "218.0.2.0/24\n218.0.3.0/24 192.168.1.1"
        )).pack(anchor=tk.W)

        sf = ttk.LabelFrame(main, text="Настройки", padding="12")
        sf.pack(fill=tk.X, pady=(0, 10))

        ff = ttk.Frame(sf)
        ff.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(ff, text="Файл:", width=12).pack(side=tk.LEFT)
        self.file_path_var = tk.StringVar(value=filepath)
        ttk.Entry(ff, textvariable=self.file_path_var, width=42).pack(
            side=tk.LEFT, padx=(0, 6))
        ttk.Button(ff, text="Обзор...",
                   command=self._browse).pack(side=tk.LEFT)

        pf = ttk.Frame(sf)
        pf.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(pf, text="Протокол:", width=12).pack(side=tk.LEFT)
        self.ipv6_var = tk.BooleanVar(value=False)
        ttk.Radiobutton(pf, text="IPv4",
                        variable=self.ipv6_var, value=False).pack(
            side=tk.LEFT, padx=4)
        ttk.Radiobutton(pf, text="IPv6",
                        variable=self.ipv6_var, value=True).pack(
            side=tk.LEFT, padx=4)

        iface_f = ttk.Frame(sf)
        iface_f.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(iface_f, text="Интерфейс:", width=12).pack(side=tk.LEFT)
        self.iface_combo = ttk.Combobox(iface_f, width=28)
        self.iface_combo.pack(side=tk.LEFT)
        self.iface_combo.set(DEFAULT_INTERFACE)
        ifaces = list(self.app.interface_combo['values'])
        if ifaces:
            self.iface_combo['values'] = ifaces

        mode_lf = ttk.LabelFrame(sf, text="Режим", padding="8")
        mode_lf.pack(fill=tk.X)
        self.gw_mode = tk.StringVar(value="interface_only")
        ttk.Radiobutton(mode_lf, text=f"Только интерфейс ({DEFAULT_INTERFACE})",
                        variable=self.gw_mode,
                        value="interface_only").pack(anchor=tk.W, pady=1)
        ttk.Radiobutton(mode_lf, text="Шлюз из файла + интерфейс",
                        variable=self.gw_mode,
                        value="gw_and_interface").pack(anchor=tk.W, pady=1)
        ttk.Radiobutton(mode_lf, text="Только шлюз из файла",
                        variable=self.gw_mode,
                        value="gw_only").pack(anchor=tk.W, pady=1)

        self.import_btn = ttk.Button(
            main, text="📥 Импортировать маршруты",
            command=self._start_import, width=30)
        self.import_btn.pack(pady=(8, 0))

        prog_f = ttk.LabelFrame(main, text="Прогресс", padding="8")
        prog_f.pack(fill=tk.X, pady=(8, 0))
        self.progress = ttk.Progressbar(
            prog_f, mode='determinate', maximum=100)
        self.progress.pack(fill=tk.X, pady=(0, 4))
        self.progress_label = ttk.Label(
            prog_f, text="Ожидание...", foreground="gray")
        self.progress_label.pack(anchor=tk.W)

        rf = ttk.LabelFrame(main, text="Результаты", padding="8")
        rf.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.result_text = scrolledtext.ScrolledText(
            rf, height=8, wrap=tk.WORD, font=('Courier', 9))
        self.result_text.pack(fill=tk.BOTH, expand=True)

        ttk.Button(main, text="Закрыть",
                   command=self._on_close, width=15).pack(pady=(8, 0))
        if filepath:
            self._show_file_info(filepath)

    def _browse(self):
        fn = filedialog.askopenfilename(
            parent=self, title="Файл с маршрутами",
            filetypes=[("Batch/Text", "*.bat *.txt"), ("All", "*.*")])
        if fn:
            self.file_path_var.set(fn)
            self._show_file_info(fn)

    def _show_file_info(self, filepath):
        try:
            count = self.app._count_routes_in_file(filepath)
            chunks = (count + BATCH_CHUNK_SIZE - 1) // BATCH_CHUNK_SIZE
            self.result_text.delete(1.0, tk.END)
            self.result_text.insert(
                tk.END, f"Файл: {filepath}\nМаршрутов: ~{count}\n")
            if chunks > 1:
                self.result_text.insert(
                    tk.END, f"Порций: {chunks} × {BATCH_CHUNK_SIZE}\n")
        except Exception:
            pass

    def _start_import(self):
        if not self.app.connected or not self.app.client:
            messagebox.showwarning("Нет подключения",
                                   "Подключитесь!", parent=self)
            return
        filepath = self.file_path_var.get().strip()
        if not filepath:
            messagebox.showerror("Ошибка", "Выберите файл!", parent=self)
            return
        ipv6 = self.ipv6_var.get()
        gw_mode = self.gw_mode.get()
        di = self.iface_combo.get().strip() or DEFAULT_INTERFACE
        self.import_btn.config(state=tk.DISABLED)
        self.progress['value'] = 0
        self.progress_label.config(text="Подготовка...", foreground="blue")
        self.result_text.delete(1.0, tk.END)

        def pcb(ci, tc, s, e, total):
            pct = int((ci - 1) / tc * 100)
            msg = f"Порция {ci}/{tc} | OK:{s} Err:{e} Всего:{total}"
            self.after(0, lambda p=pct, m=msg: self._upd_prog(p, m))

        def run():
            try:
                s, ec, errs = self.app.loop.run_until_complete(
                    self.app.client.parse_and_add_batch_file(
                        filepath=filepath, default_interface=di,
                        ipv6=ipv6, gw_mode=gw_mode,
                        progress_callback=pcb))
                res = f"✅ Успешно: {s}\n❌ Ошибок: {ec}\n"
                if errs:
                    res += "\n".join(errs[:20])
                self.after(0, lambda: self._finish(res, s, ec))
            except Exception as ex:
                self.after(0, lambda: self._on_error(str(ex)))

        threading.Thread(target=run, daemon=True).start()

    def _upd_prog(self, pct, msg):
        self.progress['value'] = pct
        self.progress_label.config(text=msg, foreground="darkblue")

    def _finish(self, text, success, errors):
        self.progress['value'] = 100
        self.progress_label.config(
            text=f"Готово! OK:{success} Err:{errors}",
            foreground="green" if errors == 0 else "darkorange")
        self.result_text.insert(tk.END, text)
        self.import_btn.config(state=tk.NORMAL)
        self.app._refresh_routes()

    def _on_error(self, msg):
        self.import_btn.config(state=tk.NORMAL)
        self.progress_label.config(text="Ошибка!", foreground="red")
        self.result_text.insert(tk.END, f"\nОШИБКА: {msg}\n")

    def _on_close(self):
        self.grab_release()
        self.destroy()


class AddRouteDialog(tk.Toplevel):

    def __init__(self, parent: 'KeeneticAdvancedGUI'):
        super().__init__(parent.root)
        self.app = parent
        self.title("➕ Добавить маршрут")
        self.geometry("620x460")
        self.resizable(False, False)
        self.transient(parent.root)
        self.grab_set()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self):
        main = ttk.Frame(self, padding="20")
        main.pack(fill=tk.BOTH, expand=True)

        tf = ttk.LabelFrame(main, text="Протокол", padding="8")
        tf.pack(fill=tk.X, pady=(0, 12))
        self.ipv6_var = tk.BooleanVar(value=False)
        ttk.Radiobutton(tf, text="IPv4", variable=self.ipv6_var,
                        value=False, command=self._upd).pack(
            side=tk.LEFT, padx=(0, 15))
        ttk.Radiobutton(tf, text="IPv6", variable=self.ipv6_var,
                        value=True, command=self._upd).pack(side=tk.LEFT)

        pf = ttk.LabelFrame(main, text="Параметры", padding="15")
        pf.pack(fill=tk.X, pady=(0, 12))

        ttk.Label(pf, text="Сеть назначения *:", width=20).grid(
            row=0, column=0, sticky=tk.W, pady=6)
        self.dest_entry = ttk.Entry(pf, width=30)
        self.dest_entry.grid(row=0, column=1, sticky=tk.W,
                             pady=6, padx=(10, 0))
        ttk.Label(pf, text="Напр.: 218.0.0.0/24",
                  foreground="gray").grid(
            row=0, column=2, sticky=tk.W, padx=(8, 0))

        ttk.Label(pf, text="Шлюз (необязателен):", width=20).grid(
            row=1, column=0, sticky=tk.W, pady=6)
        self.gw_entry = ttk.Entry(pf, width=30)
        self.gw_entry.grid(row=1, column=1, sticky=tk.W,
                           pady=6, padx=(10, 0))

        ttk.Label(pf, text="Интерфейс:", width=20).grid(
            row=2, column=0, sticky=tk.W, pady=6)
        ir = ttk.Frame(pf)
        ir.grid(row=2, column=1, sticky=tk.W, pady=6, padx=(10, 0))
        self.iface_combo = ttk.Combobox(ir, width=24)
        self.iface_combo.pack(side=tk.LEFT, padx=(0, 5))
        self.iface_combo.set(DEFAULT_INTERFACE)
        ifaces = list(self.app.interface_combo['values'])
        if ifaces:
            self.iface_combo['values'] = ifaces
        ttk.Button(ir, text="🔄", width=3,
                   command=self._refresh_ifaces).pack(side=tk.LEFT)

        for w in [self.dest_entry, self.gw_entry]:
            w.bind('<KeyRelease>', self._upd)
        self.iface_combo.bind('<<ComboboxSelected>>', self._upd)
        self.iface_combo.bind('<KeyRelease>', self._upd)

        of = ttk.LabelFrame(main, text="Опции", padding="8")
        of.pack(fill=tk.X, pady=(0, 12))
        self.exclusive_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(of, text="exclusive",
                        variable=self.exclusive_var,
                        command=self._upd).pack(anchor=tk.W)

        pvf = ttk.LabelFrame(main, text="Команда", padding="10")
        pvf.pack(fill=tk.X, pady=(0, 15))
        self.preview_var = tk.StringVar(value="ip route ...")
        ttk.Label(pvf, textvariable=self.preview_var,
                  foreground="darkgreen",
                  font=('Courier', 11, 'bold')).pack(anchor=tk.W)

        bf = ttk.Frame(main)
        bf.pack(fill=tk.X)
        self.add_btn = ttk.Button(bf, text="✅ Добавить маршрут",
                                  command=self._add, width=22)
        self.add_btn.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(bf, text="Закрыть",
                   command=self._on_close, width=12).pack(side=tk.LEFT)
        self.status_label = ttk.Label(main, text="", foreground="gray")
        self.status_label.pack(anchor=tk.W, pady=(8, 0))

    def _upd(self, event=None):
        dest = self.dest_entry.get().strip()
        gw = self.gw_entry.get().strip()
        iface = self.iface_combo.get().strip()
        ipv6 = self.ipv6_var.get()
        excl = self.exclusive_var.get()
        if not dest:
            self.preview_var.set("ip route ...")
            return
        cmd = ("ipv6 route " if ipv6 else "ip route ") + dest
        if gw and gw != "0.0.0.0":
            cmd += f" {gw}"
            if iface:
                cmd += f" {iface}"
        elif iface:
            cmd += f" {iface}"
        else:
            cmd += " 0.0.0.0"
        if excl:
            cmd += " exclusive"
        self.preview_var.set(cmd)

    def _refresh_ifaces(self):
        if not self.app.connected or not self.app.client:
            messagebox.showwarning("Нет подключения",
                                   "Подключитесь!", parent=self)
            return

        def run():
            try:
                ifaces, _ = self.app.loop.run_until_complete(
                    self.app.client.get_interfaces())
                self.after(0, lambda: self._set_ifaces(ifaces))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror(
                    "Ошибка", str(e), parent=self))

        threading.Thread(target=run, daemon=True).start()

    def _set_ifaces(self, ifaces):
        self.iface_combo['values'] = ifaces
        self.app.interface_combo['values'] = ifaces
        if self.iface_combo.get() not in ifaces:
            self.iface_combo.set(ifaces[0] if ifaces else DEFAULT_INTERFACE)

    def _add(self):
        if not self.app.connected or not self.app.client:
            messagebox.showwarning("Нет подключения",
                                   "Подключитесь!", parent=self)
            return
        dest = self.dest_entry.get().strip()
        gw = self.gw_entry.get().strip()
        iface = self.iface_combo.get().strip()
        excl = self.exclusive_var.get()
        ipv6 = self.ipv6_var.get()
        if not dest:
            messagebox.showerror("Ошибка",
                                 "Укажите сеть назначения!", parent=self)
            return
        if not gw and not iface:
            messagebox.showerror("Ошибка",
                                 "Укажите шлюз или интерфейс!", parent=self)
            return
        self._upd()
        self.add_btn.config(state=tk.DISABLED)
        self.status_label.config(text="Отправка...", foreground="blue")

        def run():
            try:
                self.app.loop.run_until_complete(
                    self.app.client.add_static_route(
                        destination=dest, gateway=gw or None,
                        interface=iface or None,
                        exclusive=excl, ipv6=ipv6))
                self.app.loop.run_until_complete(
                    self.app.client.execute("system configuration save"))
                self.app._log(f"✅ Маршрут {dest} добавлен!", "SUCCESS")
                self.app._refresh_routes()
                self.after(0, lambda: self._on_success(dest))
            except Exception as e:
                self.app._log(f"Ошибка: {e}", "ERROR")
                self.after(0, lambda: self._on_err(str(e)))

        threading.Thread(target=run, daemon=True).start()

    def _on_success(self, dest):
        self.add_btn.config(state=tk.NORMAL)
        self.status_label.config(
            text=f"✅ {dest} добавлен!", foreground="green")
        self.dest_entry.delete(0, tk.END)
        self.gw_entry.delete(0, tk.END)
        self.iface_combo.set(DEFAULT_INTERFACE)
        self.exclusive_var.set(False)
        self._upd()
        messagebox.showinfo("Успех", f"Маршрут {dest} добавлен",
                            parent=self)

    def _on_err(self, msg):
        self.add_btn.config(state=tk.NORMAL)
        self.status_label.config(text="❌ Ошибка", foreground="red")
        messagebox.showerror("Ошибка", msg, parent=self)

    def _on_close(self):
        self.grab_release()
        self.destroy()


# ═══════════════════════════════════════════════════════════════════
# ГЛАВНОЕ ОКНО
# ═══════════════════════════════════════════════════════════════════

class KeeneticAdvancedGUI:

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Keenetic Route Manager Pro")
        self.root.geometry("1060x800")
        self.root.resizable(True, True)

        self.style = ttk.Style()
        self.style.theme_use('clam')
        for name, color in [
            ("green.Horizontal.TProgressbar",  "#4caf50"),
            ("orange.Horizontal.TProgressbar", "#ff9800"),
            ("red.Horizontal.TProgressbar",    "#f44336"),
        ]:
            self.style.configure(name, troughcolor="#e0e0e0",
                                 background=color)

        self.client: Optional[KeeneticAdvancedClient] = None
        self.connected = False
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.ipv6_available = False
        self.tab_frames: Dict[str, ttk.Frame] = {}
        self._current_routes: List[Dict] = []
        self._current_forwardings: List[Dict] = []
        self._pf_raw_output = ""
        self.search_var = None
        self._sysmon_auto_var = None
        self._sysmon_after_id = None
        self._sysmon_interval_var = None
        # Хранит последний сырой вывод монитора
        self._sysmon_last_raw: Dict = {}

        self._create_widgets()
        self._load_settings()

    def _create_widgets(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # ── Порядок вкладок ────────────────────────────────────────
        self._create_connection_tab()
        self._create_sysmon_tab()          # 2-я вкладка
        self._create_routes_tab()
        self._create_port_forwarding_tab()
        self._create_interfaces_tab()
        self._create_logs_tab()

        self.statusbar = ttk.Label(
            self.root, text="Не подключено",
            relief=tk.SUNKEN, anchor=tk.W)
        self.statusbar.pack(side=tk.BOTTOM, fill=tk.X)

    # ───────────────────────────────────────────────────────────────
    # ВКЛАДКА ПОДКЛЮЧЕНИЯ
    # ───────────────────────────────────────────────────────────────

    def _create_connection_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="🔌 Подключение")
        self.tab_frames["connection"] = frame

        main = ttk.Frame(frame, padding="20")
        main.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main, text="Keenetic Route Manager",
                  font=('Arial', 15, 'bold')).pack(pady=(0, 20))

        f = ttk.LabelFrame(main, text="Параметры подключения",
                           padding="15")
        f.pack(fill=tk.X, pady=(0, 20))

        ttk.Label(f, text="IP адрес:").grid(
            row=0, column=0, sticky=tk.W, pady=5)
        self.ip_entry = ttk.Entry(f, width=30)
        self.ip_entry.grid(row=0, column=1, sticky=tk.W,
                           pady=5, padx=(10, 0))
        self.ip_entry.insert(0, "192.168.1.1")

        ttk.Label(f, text="Порт:").grid(
            row=1, column=0, sticky=tk.W, pady=5)
        self.port_entry = ttk.Entry(f, width=10)
        self.port_entry.grid(row=1, column=1, sticky=tk.W,
                             pady=5, padx=(10, 0))
        self.port_entry.insert(0, "23")

        ttk.Label(f, text="Логин:").grid(
            row=2, column=0, sticky=tk.W, pady=5)
        self.login_entry = ttk.Entry(f, width=30)
        self.login_entry.grid(row=2, column=1, sticky=tk.W,
                              pady=5, padx=(10, 0))
        self.login_entry.insert(0, "admin")

        ttk.Label(f, text="Пароль:").grid(
            row=3, column=0, sticky=tk.W, pady=5)
        self.password_entry = ttk.Entry(f, width=30, show="•")
        self.password_entry.grid(row=3, column=1, sticky=tk.W,
                                 pady=5, padx=(10, 0))

        bf = ttk.Frame(main)
        bf.pack(fill=tk.X, pady=(10, 0))
        self.connect_btn = ttk.Button(
            bf, text="🔌 Подключиться",
            command=self._connect_to_router, width=20)
        self.connect_btn.pack(side=tk.LEFT, padx=(0, 10))
        self.disconnect_btn = ttk.Button(
            bf, text="❌ Отключиться",
            command=self._disconnect_from_router,
            width=20, state=tk.DISABLED)
        self.disconnect_btn.pack(side=tk.LEFT)

        sf = ttk.Frame(main)
        sf.pack(fill=tk.X, pady=(15, 0))
        self.save_settings_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(sf, text="Сохранять настройки подключения",
                        variable=self.save_settings_var).pack(anchor=tk.W)

        info = ttk.LabelFrame(main, text="Информация", padding="10")
        info.pack(fill=tk.X, pady=(20, 0))
        ttk.Label(info, justify=tk.LEFT, foreground="gray", text=(
            f"• Используется Telnet (telnetlib3)\n"
            f"• Интерфейс по умолчанию: {DEFAULT_INTERFACE}\n"
            f"• Пакетная загрузка: порциями по {BATCH_CHUNK_SIZE}\n"
            "• Telnet: веб-интерфейс → Системные настройки → Опции"
        )).pack(anchor=tk.W)

    # ───────────────────────────────────────────────────────────────
    # ВКЛАДКА: СИСТЕМНЫЙ МОНИТОР (2-я)
    # ───────────────────────────────────────────────────────────────

    def _create_sysmon_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="📊 Монитор")
        self.tab_frames["sysmon"] = frame

        # Canvas + scrollbar
        canvas = tk.Canvas(frame, highlightthickness=0)
        vscroll = ttk.Scrollbar(
            frame, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        inner = ttk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(
                       scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(win_id, width=e.width))
        canvas.bind_all(
            "<MouseWheel>",
            lambda e: canvas.yview_scroll(
                int(-1 * (e.delta / 120)), "units"))

        # ── Панель управления ──────────────────────────────────────
        ctrl = ttk.Frame(inner, padding="6")
        ctrl.pack(fill=tk.X)

        self.sysmon_refresh_btn = ttk.Button(
            ctrl, text="🔄 Обновить всё",
            command=self._sysmon_refresh, state=tk.DISABLED)
        self.sysmon_refresh_btn.pack(side=tk.LEFT, padx=(0, 8))

        self._sysmon_auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctrl, text="Авто",
                        variable=self._sysmon_auto_var,
                        command=self._sysmon_toggle_auto
                        ).pack(side=tk.LEFT, padx=(0, 4))

        self._sysmon_interval_var = tk.IntVar(value=5)
        ttk.Spinbox(ctrl, from_=2, to=60,
                    textvariable=self._sysmon_interval_var,
                    width=4).pack(side=tk.LEFT, padx=(0, 2))
        ttk.Label(ctrl, text="сек").pack(side=tk.LEFT, padx=(0, 16))

        # Кнопка диагностики сырого вывода
        self.sysmon_raw_btn = ttk.Button(
            ctrl, text="🔬 Сырые данные",
            command=self._sysmon_show_raw, state=tk.DISABLED)
        self.sysmon_raw_btn.pack(side=tk.LEFT, padx=(0, 12))

        self._sysmon_status = ttk.Label(
            ctrl, text="Подключитесь к роутеру", foreground="gray")
        self._sysmon_status.pack(side=tk.LEFT)

        ttk.Separator(inner, orient=tk.HORIZONTAL).pack(
            fill=tk.X, pady=(4, 6), padx=6)

        # ── Ряд 1: Система + Ресурсы ───────────────────────────────
        row1 = ttk.Frame(inner)
        row1.pack(fill=tk.X, padx=8, pady=(0, 6))
        row1.columnconfigure(0, weight=3)
        row1.columnconfigure(1, weight=4)

        # Блок «Система»
        sys_lf = ttk.LabelFrame(row1, text="🖥️  Система", padding="12")
        sys_lf.grid(row=0, column=0, sticky="nsew", padx=(0, 4))

        def sys_row(lbl, attr, r):
            ttk.Label(sys_lf, text=lbl, foreground="gray",
                      font=('Arial', 9)).grid(
                row=r, column=0, sticky=tk.W, pady=3)
            var = tk.StringVar(value="—")
            ttk.Label(sys_lf, textvariable=var,
                      font=('Arial', 9, 'bold')).grid(
                row=r, column=1, sticky=tk.W, padx=(12, 0), pady=3)
            setattr(self, attr, var)

        sys_row("Модель:",      "_sm_model",    0)
        sys_row("Прошивка:",    "_sm_firmware", 1)
        sys_row("Имя хоста:",   "_sm_hostname", 2)
        sys_row("Аптайм:",      "_sm_uptime",   3)
        sys_row("WAN IP:",       "_sm_wan_ip",  4)
        sys_row("DNS:",          "_sm_dns",     5)

        # Блок «Ресурсы»
        res_lf = ttk.LabelFrame(row1, text="⚙️  Ресурсы", padding="12")
        res_lf.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

        # CPU
        ttk.Label(res_lf, text="CPU:", foreground="gray",
                  font=('Arial', 9)).grid(
            row=0, column=0, sticky=tk.W, pady=8)
        self._sm_cpu_var = tk.StringVar(value="—")
        ttk.Label(res_lf, textvariable=self._sm_cpu_var,
                  font=('Arial', 9, 'bold'), width=8).grid(
            row=0, column=1, sticky=tk.W, padx=(10, 0))
        self._sm_cpu_bar = ttk.Progressbar(
            res_lf, length=220, maximum=100, mode='determinate',
            style="green.Horizontal.TProgressbar")
        self._sm_cpu_bar.grid(row=0, column=2, padx=(10, 0), pady=8)

        # RAM
        ttk.Label(res_lf, text="RAM:", foreground="gray",
                  font=('Arial', 9)).grid(
            row=1, column=0, sticky=tk.W, pady=8)
        self._sm_ram_var = tk.StringVar(value="—")
        ttk.Label(res_lf, textvariable=self._sm_ram_var,
                  font=('Arial', 9, 'bold'), width=22).grid(
            row=1, column=1, sticky=tk.W, padx=(10, 0))
        self._sm_ram_bar = ttk.Progressbar(
            res_lf, length=220, maximum=100, mode='determinate',
            style="green.Horizontal.TProgressbar")
        self._sm_ram_bar.grid(row=1, column=2, padx=(10, 0), pady=8)

        # Температура
        ttk.Label(res_lf, text="Температура:", foreground="gray",
                  font=('Arial', 9)).grid(
            row=2, column=0, sticky=tk.W, pady=8)
        self._sm_temp_var = tk.StringVar(value="—")
        ttk.Label(res_lf, textvariable=self._sm_temp_var,
                  font=('Arial', 13, 'bold'),
                  foreground="darkorange").grid(
            row=2, column=1, sticky=tk.W, padx=(10, 0),
            columnspan=2)

        ttk.Separator(inner, orient=tk.HORIZONTAL).pack(
            fill=tk.X, padx=8, pady=(0, 6))

        # ── Трафик по интерфейсам ──────────────────────────────────
        iface_lf = ttk.LabelFrame(
            inner, text="🌐  Трафик по интерфейсам", padding="8")
        iface_lf.pack(fill=tk.X, padx=8, pady=(0, 6))

        iface_tf = ttk.Frame(iface_lf)
        iface_tf.pack(fill=tk.X)

        iface_cols = ("name", "state", "ip", "mac", "rx", "tx", "desc")
        self._iface_tree = ttk.Treeview(
            iface_tf, columns=iface_cols, show="headings", height=8)
        for col, hdr, w, anch in [
            ("name",  "Интерфейс",    120, tk.W),
            ("state", "Состояние",     90, tk.CENTER),
            ("ip",    "IP-адрес",     130, tk.W),
            ("mac",   "MAC",          140, tk.W),
            ("rx",    "↓ Получено",   110, tk.E),
            ("tx",    "↑ Отправлено", 110, tk.E),
            ("desc",  "Описание",     180, tk.W),
        ]:
            self._iface_tree.heading(col, text=hdr)
            self._iface_tree.column(col, width=w, anchor=anch)
        self._iface_tree.tag_configure("up",    foreground="darkgreen")
        self._iface_tree.tag_configure("down",  foreground="#999999")
        self._iface_tree.tag_configure("other", foreground="darkorange")

        iface_sb = ttk.Scrollbar(iface_tf, orient=tk.VERTICAL,
                                 command=self._iface_tree.yview)
        self._iface_tree.configure(yscrollcommand=iface_sb.set)
        self._iface_tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        iface_sb.pack(side=tk.RIGHT, fill=tk.Y)

        ttk.Separator(inner, orient=tk.HORIZONTAL).pack(
            fill=tk.X, padx=8, pady=(0, 6))

        # ── Подключённые устройства ────────────────────────────────
        clients_lf = ttk.LabelFrame(
            inner, text="👥  Подключённые устройства", padding="8")
        clients_lf.pack(fill=tk.BOTH, expand=True,
                        padx=8, pady=(0, 10))

        cc = ttk.Frame(clients_lf)
        cc.pack(fill=tk.X, pady=(0, 6))
        self.sysmon_clients_btn = ttk.Button(
            cc, text="🔄 Обновить устройства",
            command=self._sysmon_refresh_clients, state=tk.DISABLED)
        self.sysmon_clients_btn.pack(side=tk.LEFT, padx=(0, 8))
        self._sm_clients_count = ttk.Label(
            cc, text="", foreground="gray")
        self._sm_clients_count.pack(side=tk.LEFT)

        cl_tf = ttk.Frame(clients_lf)
        cl_tf.pack(fill=tk.BOTH, expand=True)
        cl_cols = ("ip", "mac", "hostname", "source")
        self._clients_tree = ttk.Treeview(
            cl_tf, columns=cl_cols, show="headings", height=8)
        for col, hdr, w in [
            ("ip",       "IP-адрес",         140),
            ("mac",      "MAC-адрес",         150),
            ("hostname", "Имя устройства",    220),
            ("source",   "Источник",           90),
        ]:
            self._clients_tree.heading(col, text=hdr)
            self._clients_tree.column(col, width=w)
        cl_sb = ttk.Scrollbar(cl_tf, orient=tk.VERTICAL,
                              command=self._clients_tree.yview)
        self._clients_tree.configure(yscrollcommand=cl_sb.set)
        self._clients_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        cl_sb.pack(side=tk.RIGHT, fill=tk.Y)

    # ── Методы монитора ───────────────────────────────────────────

    @staticmethod
    def _fmt_bytes(n: int) -> str:
        for unit in ('Б', 'КБ', 'МБ', 'ГБ', 'ТБ'):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} ПБ"

    def _sysmon_toggle_auto(self):
        if self._sysmon_auto_var.get():
            self._sysmon_schedule()
        else:
            if self._sysmon_after_id:
                self.root.after_cancel(self._sysmon_after_id)
                self._sysmon_after_id = None

    def _sysmon_schedule(self):
        if not self._sysmon_auto_var.get():
            return
        ms = max(2, self._sysmon_interval_var.get()) * 1000
        self._sysmon_refresh()
        self._sysmon_after_id = self.root.after(ms, self._sysmon_schedule)

    def _sysmon_refresh(self):
        """Обновить системную информацию + трафик."""
        if not self.connected or not self.client:
            return
        self._sysmon_status.config(
            text="Обновление...", foreground="blue")

        def run():
            try:
                info   = self.loop.run_until_complete(
                    self.client.get_system_info())
                ifaces = self.loop.run_until_complete(
                    self.client.get_interfaces_traffic())
                self.root.after(
                    0, lambda: self._sysmon_update(info, ifaces))
            except Exception as e:
                self.root.after(
                    0, lambda: self._sysmon_status.config(
                        text=f"Ошибка: {e}", foreground="red"))

        threading.Thread(target=run, daemon=True).start()

    def _sysmon_update(self, info: Dict, ifaces: List[Dict]):
        ts = datetime.now().strftime("%H:%M:%S")

        # Сохраняем сырые данные для диагностики
        self._sysmon_last_raw = info.get("_raw", {})

        # Системная информация
        self._sm_model.set(info.get("model",    "—"))
        self._sm_firmware.set(info.get("firmware","—"))
        self._sm_hostname.set(info.get("hostname","—"))
        self._sm_uptime.set(info.get("uptime",  "—"))
        self._sm_wan_ip.set(info.get("wan_ip",  "—"))
        self._sm_dns.set(info.get("dns",         "—"))
        self._sm_temp_var.set(info.get("temp",  "—"))

        # CPU
        cpu_str = info.get("cpu", "—")
        self._sm_cpu_var.set(cpu_str)
        try:
            cv = int(re.search(r'\d+', cpu_str).group())
            self._sm_cpu_bar['value'] = cv
            style = ("red.Horizontal.TProgressbar" if cv > 80 else
                     "orange.Horizontal.TProgressbar" if cv > 50 else
                     "green.Horizontal.TProgressbar")
            self._sm_cpu_bar.config(style=style)
        except Exception:
            self._sm_cpu_bar['value'] = 0

        # RAM
        rp = info.get("ram_pct", 0)
        self._sm_ram_var.set(
            f"{info.get('ram_used','—')} / "
            f"{info.get('ram_total','—')} МБ  ({rp}%)")
        self._sm_ram_bar['value'] = rp
        rs = ("red.Horizontal.TProgressbar" if rp > 85 else
              "orange.Horizontal.TProgressbar" if rp > 65 else
              "green.Horizontal.TProgressbar")
        self._sm_ram_bar.config(style=rs)

        # Интерфейсы
        for item in self._iface_tree.get_children():
            self._iface_tree.delete(item)
        for iface in ifaces:
            name  = iface.get("name", "")
            state = iface.get("state", "?").lower()
            ip    = iface.get("ip",   "—")
            mac   = iface.get("mac",  "—")
            rx    = self._fmt_bytes(iface.get("rx_bytes", 0))
            tx    = self._fmt_bytes(iface.get("tx_bytes", 0))
            desc  = iface.get("description", "")

            if "up" in state or "connect" in state:
                tag, st = "up",    "▲ Активен"
            elif "down" in state or "disconnect" in state:
                tag, st = "down",  "▼ Выкл"
            else:
                tag, st = "other", state

            self._iface_tree.insert(
                "", tk.END,
                values=(name, st, ip, mac, rx, tx, desc),
                tags=(tag,))

        self._sysmon_status.config(
            text=f"Обновлено: {ts}", foreground="darkgreen")

    def _sysmon_refresh_clients(self):
        if not self.connected or not self.client:
            return
        self.sysmon_clients_btn.config(state=tk.DISABLED)
        self._sm_clients_count.config(
            text="Загрузка...", foreground="blue")

        def run():
            try:
                clients = self.loop.run_until_complete(
                    self.client.get_clients())
                self.root.after(
                    0, lambda: self._sysmon_update_clients(clients))
            except Exception as e:
                self.root.after(
                    0, lambda: self._sm_clients_count.config(
                        text=f"Ошибка: {e}", foreground="red"))
            finally:
                self.root.after(
                    0, lambda: self.sysmon_clients_btn.config(
                        state=tk.NORMAL))

        threading.Thread(target=run, daemon=True).start()

    def _sysmon_update_clients(self, clients: List[Dict]):
        for item in self._clients_tree.get_children():
            self._clients_tree.delete(item)
        for c in clients:
            self._clients_tree.insert("", tk.END, values=(
                c.get("ip",       "—"),
                c.get("mac",      "—"),
                c.get("hostname", "—"),
                c.get("source",   "—"),
            ))
        n = len(clients)
        self._sm_clients_count.config(
            text=f"Устройств: {n}",
            foreground="darkgreen" if n else "gray")

    def _sysmon_show_raw(self):
        """Показать сырые данные для диагностики парсинга."""
        if not self._sysmon_last_raw:
            # Запросить сейчас
            if not self.connected or not self.client:
                messagebox.showinfo("Нет данных",
                                    "Подключитесь и нажмите 🔄 Обновить всё.",
                                    parent=self.root)
                return
            messagebox.showinfo("Нет данных",
                                "Нажмите '🔄 Обновить всё' чтобы загрузить данные.",
                                parent=self.root)
            return
        SysmonRawDialog(self, self._sysmon_last_raw)

    # ───────────────────────────────────────────────────────────────
    # ВКЛАДКА МАРШРУТОВ
    # ───────────────────────────────────────────────────────────────

    def _create_routes_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="🗺️ Маршруты")
        self.tab_frames["routes"] = frame

        main = ttk.Frame(frame, padding="10")
        main.pack(fill=tk.BOTH, expand=True)

        tf = ttk.Frame(main)
        tf.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(tf, text="Тип:").pack(side=tk.LEFT, padx=(0, 8))
        self.route_type_var = tk.StringVar(value="ipv4")
        ttk.Radiobutton(tf, text="IPv4", variable=self.route_type_var,
                        value="ipv4",
                        command=self._refresh_routes).pack(
            side=tk.LEFT, padx=3)
        ttk.Radiobutton(tf, text="IPv6", variable=self.route_type_var,
                        value="ipv6",
                        command=self._refresh_routes).pack(
            side=tk.LEFT, padx=3)

        cf = ttk.Frame(main)
        cf.pack(fill=tk.X, pady=(0, 8))
        self.refresh_routes_btn = ttk.Button(
            cf, text="🔄 Обновить",
            command=self._refresh_routes, state=tk.DISABLED)
        self.refresh_routes_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.delete_route_btn = ttk.Button(
            cf, text="🗑️ Удалить выбранный",
            command=self._delete_selected_route, state=tk.DISABLED)
        self.delete_route_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.add_route_btn = ttk.Button(
            cf, text="➕ Добавить маршрут",
            command=self._open_add_route_dialog, state=tk.DISABLED)
        self.add_route_btn.pack(side=tk.LEFT, padx=(0, 12))
        ttk.Separator(cf, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=(0, 12))
        self.save_routes_btn = ttk.Button(
            cf, text="💾 Сохранить в файл",
            command=self._save_routes_to_file, state=tk.DISABLED)
        self.save_routes_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.load_routes_btn = ttk.Button(
            cf, text="📂 Загрузить из файла",
            command=self._open_batch_dialog)
        self.load_routes_btn.pack(side=tk.LEFT)

        gw_f = ttk.Frame(main)
        gw_f.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(gw_f, text="Шлюз для сохранения:").pack(
            side=tk.LEFT, padx=(0, 8))
        self.export_gateway_var = tk.StringVar(value="0.0.0.0")
        ttk.Entry(gw_f, textvariable=self.export_gateway_var,
                  width=18).pack(side=tk.LEFT)

        sf = ttk.LabelFrame(main, text="🔍 Поиск", padding="8")
        sf.pack(fill=tk.X, pady=(2, 8))
        ttk.Label(sf, text="Фильтр:").pack(side=tk.LEFT, padx=(0, 6))
        self.search_var = tk.StringVar()
        se = ttk.Entry(sf, textvariable=self.search_var, width=40)
        se.pack(side=tk.LEFT, padx=(0, 6))
        se.bind("<KeyRelease>", lambda e: self._filter_routes())
        ttk.Button(sf, text="✕ Очистить",
                   command=self._clear_search).pack(side=tk.LEFT)

        tree_f = ttk.Frame(main)
        tree_f.pack(fill=tk.BOTH, expand=True)
        cols = ("destination", "gateway", "type", "full_line")
        self.routes_tree = ttk.Treeview(
            tree_f, columns=cols, show="headings", height=22)
        self.routes_tree.heading("destination", text="Сеть назначения")
        self.routes_tree.heading("gateway",     text="Шлюз / Интерфейс")
        self.routes_tree.heading("type",        text="Тип")
        self.routes_tree.heading("full_line",   text="Полная строка")
        self.routes_tree.column("destination", width=200)
        self.routes_tree.column("gateway",     width=160)
        self.routes_tree.column("type",        width=55)
        self.routes_tree.column("full_line",   width=490)
        sb = ttk.Scrollbar(tree_f, orient=tk.VERTICAL,
                           command=self.routes_tree.yview)
        self.routes_tree.configure(yscrollcommand=sb.set)
        self.routes_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        self.routes_status_label = ttk.Label(
            main, text="Подключитесь к роутеру", foreground="gray")
        self.routes_status_label.pack(pady=(6, 0))

    # ───────────────────────────────────────────────────────────────
    # ВКЛАДКА ПЕРЕАДРЕСАЦИИ ПОРТОВ
    # ───────────────────────────────────────────────────────────────

    def _create_port_forwarding_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="🔀 Переадресация портов")
        self.tab_frames["portfwd"] = frame

        main = ttk.Frame(frame, padding="10")
        main.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main, text="Статические правила переадресации портов",
                  font=('Arial', 10, 'bold'),
                  foreground="darkblue").pack(anchor=tk.W, pady=(0, 6))

        top_f = ttk.Frame(main)
        top_f.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(top_f, text="IP-версия:").pack(side=tk.LEFT, padx=(0, 8))
        self.pf_ipv6_var = tk.StringVar(value="ipv4")
        ttk.Radiobutton(top_f, text="IPv4", variable=self.pf_ipv6_var,
                        value="ipv4",
                        command=self._refresh_port_forwardings).pack(
            side=tk.LEFT, padx=3)
        ttk.Radiobutton(top_f, text="IPv6", variable=self.pf_ipv6_var,
                        value="ipv6",
                        command=self._refresh_port_forwardings).pack(
            side=tk.LEFT, padx=3)

        ctrl_f = ttk.Frame(main)
        ctrl_f.pack(fill=tk.X, pady=(0, 8))
        self.pf_refresh_btn = ttk.Button(
            ctrl_f, text="🔄 Обновить",
            command=self._refresh_port_forwardings, state=tk.DISABLED)
        self.pf_refresh_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.pf_add_btn = ttk.Button(
            ctrl_f, text="➕ Добавить правило",
            command=self._open_add_pf_dialog, state=tk.DISABLED)
        self.pf_add_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.pf_delete_btn = ttk.Button(
            ctrl_f, text="🗑️ Удалить выбранное",
            command=self._delete_selected_pf, state=tk.DISABLED)
        self.pf_delete_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.pf_clone_btn = ttk.Button(
            ctrl_f, text="📋 Клонировать",
            command=self._clone_selected_pf, state=tk.DISABLED)
        self.pf_clone_btn.pack(side=tk.LEFT, padx=(0, 12))
        ttk.Separator(ctrl_f, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=(0, 12))
        self.pf_diag_btn = ttk.Button(
            ctrl_f, text="🔬 Диагностика",
            command=self._open_pf_diagnostic, state=tk.DISABLED)
        self.pf_diag_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.pf_raw_btn = ttk.Button(
            ctrl_f, text="🔍 Сырой вывод",
            command=self._show_pf_raw_output, state=tk.DISABLED)
        self.pf_raw_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.pf_sessions_btn = ttk.Button(
            ctrl_f, text="📡 NAT-сессии",
            command=self._open_nat_sessions_dialog, state=tk.DISABLED)
        self.pf_sessions_btn.pack(side=tk.LEFT)

        sf = ttk.LabelFrame(main, text="🔍 Фильтр", padding="6")
        sf.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(sf, text="Поиск:").pack(side=tk.LEFT, padx=(0, 6))
        self.pf_search_var = tk.StringVar()
        pf_se = ttk.Entry(sf, textvariable=self.pf_search_var, width=40)
        pf_se.pack(side=tk.LEFT, padx=(0, 6))
        pf_se.bind("<KeyRelease>", lambda e: self._filter_pf())
        ttk.Button(sf, text="✕ Очистить",
                   command=self._clear_pf_search).pack(side=tk.LEFT)

        tree_f = ttk.Frame(main)
        tree_f.pack(fill=tk.BOTH, expand=True)
        pf_cols = ("protocol", "ext_port", "int_ip", "int_port",
                   "interface", "comment", "full_line")
        self.pf_tree = ttk.Treeview(
            tree_f, columns=pf_cols, show="headings", height=18)
        for col, hdr, w, anch in [
            ("protocol",  "Протокол",         75,  tk.CENTER),
            ("ext_port",  "Внеш. порт",       90,  tk.CENTER),
            ("int_ip",    "Внутренний IP",    130,  tk.W),
            ("int_port",  "Внутр. порт",      90,  tk.CENTER),
            ("interface", "Вход. интерфейс", 150,  tk.W),
            ("comment",   "Описание",         160,  tk.W),
            ("full_line", "Строка конфига",   280,  tk.W),
        ]:
            self.pf_tree.heading(col, text=hdr)
            self.pf_tree.column(col, width=w, anchor=anch)
        pf_sb = ttk.Scrollbar(tree_f, orient=tk.VERTICAL,
                              command=self.pf_tree.yview)
        self.pf_tree.configure(yscrollcommand=pf_sb.set)
        self.pf_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        pf_sb.pack(side=tk.RIGHT, fill=tk.Y)

        bot_f = ttk.Frame(main)
        bot_f.pack(fill=tk.X, pady=(6, 0))
        self.pf_status_lbl = ttk.Label(
            bot_f, text="Подключитесь к роутеру", foreground="gray")
        self.pf_status_lbl.pack(side=tk.LEFT)

    # ───────────────────────────────────────────────────────────────
    # ВКЛАДКА ИНТЕРФЕЙСОВ
    # ───────────────────────────────────────────────────────────────

    def _create_interfaces_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="🔧 Интерфейсы")
        self.tab_frames["interfaces"] = frame

        main = ttk.Frame(frame, padding="15")
        main.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main, text="Диагностика и выбор интерфейсов",
                  font=('Arial', 12, 'bold')).pack(pady=(0, 10))

        bf = ttk.Frame(main)
        bf.pack(fill=tk.X, pady=(0, 10))
        self.scan_iface_btn = ttk.Button(
            bf, text="🔍 Сканировать интерфейсы",
            command=self._scan_interfaces, state=tk.DISABLED)
        self.scan_iface_btn.pack(side=tk.LEFT, padx=(0, 10))

        found_f = ttk.LabelFrame(
            main, text="Найденные интерфейсы", padding="8")
        found_f.pack(fill=tk.X, pady=(0, 10))
        lb_f = ttk.Frame(found_f)
        lb_f.pack(fill=tk.X)
        self.iface_listbox = tk.Listbox(
            lb_f, height=6, font=('Courier', 10), selectmode=tk.SINGLE)
        ib_sb = ttk.Scrollbar(lb_f, orient=tk.VERTICAL,
                              command=self.iface_listbox.yview)
        self.iface_listbox.configure(yscrollcommand=ib_sb.set)
        self.iface_listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ib_sb.pack(side=tk.RIGHT, fill=tk.Y)

        raw_f = ttk.LabelFrame(
            main, text="Сырой вывод команды", padding="8")
        raw_f.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        self.iface_raw_text = scrolledtext.ScrolledText(
            raw_f, height=16, wrap=tk.WORD, font=('Courier', 9))
        self.iface_raw_text.pack(fill=tk.BOTH, expand=True)

        mf = ttk.LabelFrame(
            main, text="Ручной ввод интерфейса", padding="8")
        mf.pack(fill=tk.X)
        ttk.Label(mf, text="Имя:").pack(side=tk.LEFT, padx=(0, 8))
        self.manual_iface_entry = ttk.Entry(mf, width=28)
        self.manual_iface_entry.insert(0, DEFAULT_INTERFACE)
        self.manual_iface_entry.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(mf, text="Применить →",
                   command=self._apply_manual_interface).pack(side=tk.LEFT)

        self.interface_combo = ttk.Combobox(main, width=1)
        self.interface_combo.set(DEFAULT_INTERFACE)

    # ───────────────────────────────────────────────────────────────
    # ВКЛАДКА ЛОГОВ
    # ───────────────────────────────────────────────────────────────

    def _create_logs_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="📋 Логи")
        self.tab_frames["logs"] = frame

        main = ttk.Frame(frame, padding="10")
        main.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(
            main, height=30, wrap=tk.WORD, font=('Courier', 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.tag_config("INFO",    foreground="blue")
        self.log_text.tag_config("SUCCESS", foreground="green")
        self.log_text.tag_config("ERROR",   foreground="red")
        self.log_text.tag_config("WARNING", foreground="darkorange")

        bf = ttk.Frame(main)
        bf.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(bf, text="🗑️ Очистить",
                   command=self._clear_logs).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(bf, text="📋 Копировать",
                   command=self._copy_logs).pack(side=tk.LEFT)

    # ═══════════════════════════════════════════════════════════════
    # PORT FORWARDING
    # ═══════════════════════════════════════════════════════════════

    def _open_add_pf_dialog(self, prefill=None):
        AddPortForwardDialog(self, prefill=prefill)

    def _open_nat_sessions_dialog(self):
        NatSessionsDialog(self)

    def _open_pf_diagnostic(self):
        PFDiagnosticDialog(self)

    def _refresh_port_forwardings(self):
        if not self.connected or not self.client:
            return
        ipv6 = (self.pf_ipv6_var.get() == "ipv6")
        self._log("Загрузка правил переадресации...", "INFO")
        self.pf_status_lbl.config(text="Загрузка...", foreground="blue")

        def run():
            try:
                rules, raw = self.loop.run_until_complete(
                    self.client.get_port_forwardings(ipv6=ipv6))
                self.root.after(
                    0, lambda: self._update_pf_table(rules, raw))
            except Exception as e:
                self.root.after(0, lambda: self._log(
                    f"[PF] Ошибка: {e}", "ERROR"))

        threading.Thread(target=run, daemon=True).start()

    def _update_pf_table(self, rules, raw=""):
        self._current_forwardings = rules
        self._pf_raw_output = raw
        self._filter_pf()
        count = len(rules)
        if count:
            self._log(f"Загружено {count} правил", "SUCCESS")
            self.pf_status_lbl.config(
                text=f"Статических правил: {count}",
                foreground="darkgreen")
        else:
            self._log("[PF] Правил не найдено. "
                      "Используйте '🔬 Диагностика'.", "WARNING")
            self.pf_status_lbl.config(
                text="Правил не найдено", foreground="darkorange")

    def _filter_pf(self):
        q = self.pf_search_var.get().strip().lower()
        for item in self.pf_tree.get_children():
            self.pf_tree.delete(item)
        for r in self._current_forwardings:
            vals = (r.get("protocol",""), r.get("ext_port",""),
                    r.get("int_ip",""),   r.get("int_port",""),
                    r.get("interface",""),r.get("comment",""),
                    r.get("full_line",""))
            if not q or any(q in str(v).lower() for v in vals):
                self.pf_tree.insert("", tk.END, values=vals)

    def _clear_pf_search(self):
        self.pf_search_var.set("")
        self._filter_pf()

    def _show_pf_raw_output(self):
        raw = getattr(self, '_pf_raw_output', '')
        if not raw:
            messagebox.showinfo("Нет данных",
                                "Нажмите '🔄 Обновить'.",
                                parent=self.root)
            return
        win = tk.Toplevel(self.root)
        win.title("🔍 Сырой вывод")
        win.geometry("920x600")
        win.transient(self.root)
        txt = scrolledtext.ScrolledText(
            win, wrap=tk.WORD, font=('Courier', 9))
        txt.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)
        txt.insert(tk.END, raw)
        txt.config(state=tk.DISABLED)
        bf = ttk.Frame(win)
        bf.pack(fill=tk.X, padx=10, pady=(0, 10))
        ttk.Button(bf, text="📋 Копировать",
                   command=lambda: (win.clipboard_clear(),
                                    win.clipboard_append(raw))
                   ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(bf, text="Закрыть",
                   command=win.destroy).pack(side=tk.LEFT)

    def _delete_selected_pf(self):
        sel = self.pf_tree.selection()
        if not sel:
            messagebox.showwarning("Предупреждение",
                                   "Выберите правило!", parent=self.root)
            return
        values = self.pf_tree.item(sel[0])['values']
        proto, ep, iip, ip2, iface = (str(values[i]) for i in range(5))
        ipv6 = (self.pf_ipv6_var.get() == "ipv6")
        label = f"{'IPv6' if ipv6 else 'IPv4'} {proto} :{ep}→{iip}:{ip2}"
        if not messagebox.askyesno("Подтверждение",
                                   f"Удалить?\n{label}",
                                   parent=self.root):
            return

        def run():
            try:
                self.loop.run_until_complete(
                    self.client.delete_port_forwarding(
                        protocol=proto, ext_port=ep, int_ip=iip,
                        int_port=ip2, interface=iface, ipv6=ipv6))
                self.root.after(0, lambda: self._log(
                    f"✅ Удалено: {label}", "SUCCESS"))
                self.root.after(0, self._refresh_port_forwardings)
            except Exception as e:
                self.root.after(0, lambda: self._log(
                    f"❌ Ошибка: {e}", "ERROR"))

        threading.Thread(target=run, daemon=True).start()

    def _clone_selected_pf(self):
        sel = self.pf_tree.selection()
        if not sel:
            messagebox.showwarning("Предупреждение",
                                   "Выберите правило!", parent=self.root)
            return
        values = self.pf_tree.item(sel[0])['values']
        proto, ep, iip, ip2, iface, comment = (
            str(values[i]) for i in range(6))
        ipv6 = (self.pf_ipv6_var.get() == "ipv6")
        self._open_add_pf_dialog(prefill={
            "protocol":  proto.lower(),
            "ext_port":  ep, "int_ip": iip, "int_port": ip2,
            "interface": iface if iface != "any" else "",
            "comment":   comment, "ipv6": ipv6,
        })

    # ═══════════════════════════════════════════════════════════════
    # МАРШРУТЫ
    # ═══════════════════════════════════════════════════════════════

    def _open_add_route_dialog(self):
        AddRouteDialog(self)

    def _open_batch_dialog(self, filepath=""):
        BatchImportDialog(self, filepath=filepath)

    def _log(self, message, level="INFO"):
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {message}\n"
        self.log_text.insert(tk.END, entry, level)
        self.log_text.see(tk.END)
        print(entry.strip())

    def _clear_logs(self):
        self.log_text.delete(1.0, tk.END)

    def _copy_logs(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log_text.get(1.0, tk.END))
        self._log("Логи скопированы", "SUCCESS")

    def _count_routes_in_file(self, filepath):
        count = 0
        for enc in ('utf-8', 'cp1251', 'latin-1'):
            try:
                with open(filepath, 'r', encoding=enc) as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        if line.lower().startswith("rem ") or \
                                line.startswith("::"):
                            continue
                        if re.search(r'\d+\.\d+', line):
                            count += 1
                return count
            except UnicodeDecodeError:
                continue
        return count

    def _load_settings(self):
        try:
            import json, os
            if os.path.exists("keenetic_settings.json"):
                with open("keenetic_settings.json", "r") as f:
                    s = json.load(f)
                self.ip_entry.delete(0, tk.END)
                self.ip_entry.insert(0, s.get("ip", "192.168.1.1"))
                self.port_entry.delete(0, tk.END)
                self.port_entry.insert(0, s.get("port", "23"))
                self.login_entry.delete(0, tk.END)
                self.login_entry.insert(0, s.get("login", "admin"))
        except Exception:
            pass

    def _save_settings(self):
        if not self.save_settings_var.get():
            return
        try:
            import json
            with open("keenetic_settings.json", "w") as f:
                json.dump({"ip": self.ip_entry.get(),
                           "port": self.port_entry.get(),
                           "login": self.login_entry.get()}, f)
        except Exception:
            pass

    def _set_buttons_connected(self, connected):
        state = tk.NORMAL if connected else tk.DISABLED
        for btn in [
            self.refresh_routes_btn, self.delete_route_btn,
            self.add_route_btn,       self.disconnect_btn,
            self.scan_iface_btn,      self.save_routes_btn,
            self.pf_refresh_btn,      self.pf_add_btn,
            self.pf_delete_btn,       self.pf_clone_btn,
            self.pf_raw_btn,          self.pf_sessions_btn,
            self.pf_diag_btn,
            self.sysmon_refresh_btn,  self.sysmon_clients_btn,
            self.sysmon_raw_btn,
        ]:
            btn.config(state=state)
        self.connect_btn.config(
            state=tk.DISABLED if connected else tk.NORMAL)

    def _scan_interfaces(self):
        if not self.connected or not self.client:
            return
        self._log("Сканирование интерфейсов...", "INFO")

        def run():
            try:
                ifaces, raw = self.loop.run_until_complete(
                    self.client.get_interfaces())
                self.root.after(
                    0, lambda: self._on_interfaces_loaded(ifaces, raw))
            except Exception as e:
                self.root.after(0, lambda: self._log(
                    f"Ошибка: {e}", "ERROR"))

        threading.Thread(target=run, daemon=True).start()

    def _on_interfaces_loaded(self, interfaces, raw):
        self.iface_listbox.delete(0, tk.END)
        for iface in interfaces:
            self.iface_listbox.insert(tk.END, iface)
        self.iface_raw_text.delete(1.0, tk.END)
        self.iface_raw_text.insert(tk.END, raw)
        self.interface_combo['values'] = interfaces
        current = self.interface_combo.get()
        if current not in interfaces:
            self.interface_combo.set(
                interfaces[0] if interfaces else DEFAULT_INTERFACE)
        if interfaces:
            self._log(f"Интерфейсы: {', '.join(interfaces)}", "SUCCESS")
        else:
            self._log("Автопарсинг не нашёл интерфейсы", "WARNING")

    def _apply_manual_interface(self):
        iface = self.manual_iface_entry.get().strip()
        if not iface:
            return
        current = list(self.interface_combo['values'])
        if iface not in current:
            current.insert(0, iface)
            self.interface_combo['values'] = current
        self.interface_combo.set(iface)
        self._log(f"Интерфейс (вручную): {iface}", "INFO")

    def _connect_to_router(self):
        ip = self.ip_entry.get().strip()
        try:
            port = int(self.port_entry.get().strip())
        except ValueError:
            messagebox.showerror("Ошибка", "Неверный порт!")
            return
        login = self.login_entry.get().strip()
        password = self.password_entry.get()
        if not ip or not login or not password:
            messagebox.showerror("Ошибка", "Заполните все поля!")
            return
        self._save_settings()
        self.connect_btn.config(state=tk.DISABLED, text="Подключение...")
        self._log(f"Подключение к {ip}:{port}...", "INFO")

        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.client = KeeneticAdvancedClient(ip, port, login, password)
            success, message = self.loop.run_until_complete(
                self.client.connect())
            if success:
                caps = self.loop.run_until_complete(
                    self.client.get_ipv6_capabilities())
                self.ipv6_available = caps["ipv6_supported"]
                self.root.after(0, lambda: self._log(
                    "IPv6 поддерживается" if self.ipv6_available
                    else "IPv6 не обнаружен",
                    "SUCCESS" if self.ipv6_available else "WARNING"))
                ifaces, raw = self.loop.run_until_complete(
                    self.client.get_interfaces())
                self.root.after(
                    0, lambda: self._on_interfaces_loaded(ifaces, raw))
            self.root.after(
                0, lambda: self._on_connect_result(success, message))

        threading.Thread(target=run, daemon=True).start()

    def _on_connect_result(self, success, message):
        if success:
            self.connected = True
            self._log(message, "SUCCESS")
            self.statusbar.config(
                text=f"Подключен к {self.ip_entry.get()}")
            self._set_buttons_connected(True)
            self.connect_btn.config(text="🔌 Подключиться")
            self._refresh_routes()
            # Сразу загружаем монитор
            self._sysmon_refresh()
            self.notebook.select(self.tab_frames["sysmon"])
        else:
            self.connected = False
            self._log(message, "ERROR")
            messagebox.showerror("Ошибка подключения", message)
            self.statusbar.config(text="Ошибка подключения")
            self.connect_btn.config(
                state=tk.NORMAL, text="🔌 Подключиться")

    def _disconnect_from_router(self):
        if self._sysmon_after_id:
            self.root.after_cancel(self._sysmon_after_id)
            self._sysmon_after_id = None
        if self._sysmon_auto_var:
            self._sysmon_auto_var.set(False)
        if self.client:
            def run():
                if self.loop and self.client:
                    self.loop.run_until_complete(self.client.close())
                self.root.after(0, self._on_disconnect_result)
            threading.Thread(target=run, daemon=True).start()

    def _on_disconnect_result(self):
        self.connected = False
        self.client = None
        self._current_routes = []
        self._current_forwardings = []
        self._pf_raw_output = ""
        self._sysmon_last_raw = {}
        self._log("Отключено", "INFO")
        self.statusbar.config(text="Не подключено")
        self._set_buttons_connected(False)
        for item in self.routes_tree.get_children():
            self.routes_tree.delete(item)
        for item in self.pf_tree.get_children():
            self.pf_tree.delete(item)
        for item in self._iface_tree.get_children():
            self._iface_tree.delete(item)
        for item in self._clients_tree.get_children():
            self._clients_tree.delete(item)
        self.pf_status_lbl.config(text="Подключитесь к роутеру")
        self._sysmon_status.config(
            text="Подключитесь к роутеру", foreground="gray")
        for attr in ("_sm_model", "_sm_firmware", "_sm_hostname",
                     "_sm_uptime", "_sm_wan_ip", "_sm_dns",
                     "_sm_cpu_var", "_sm_ram_var", "_sm_temp_var"):
            v = getattr(self, attr, None)
            if v:
                v.set("—")
        self._sm_cpu_bar['value'] = 0
        self._sm_ram_bar['value'] = 0
        self._sm_clients_count.config(text="", foreground="gray")

    def _refresh_routes(self):
        if not self.connected or not self.client:
            return
        ipv6 = (self.route_type_var.get() == "ipv6")
        self._log(f"Загрузка {'IPv6' if ipv6 else 'IPv4'} маршрутов...",
                  "INFO")
        self.routes_status_label.config(text="Загрузка...")

        def run():
            try:
                routes = self.loop.run_until_complete(
                    self.client.get_static_routes(ipv6=ipv6))
                self.root.after(
                    0, lambda: self._update_routes_table(routes, ipv6))
            except Exception as e:
                self.root.after(0, lambda: self._log(
                    f"Ошибка: {e}", "ERROR"))

        threading.Thread(target=run, daemon=True).start()

    def _update_routes_table(self, routes, ipv6):
        self._current_routes = routes
        self._filter_routes()
        if routes:
            self._log(f"Загружено {len(routes)} маршрутов", "SUCCESS")
            self.routes_status_label.config(
                text=f"Найдено: {len(routes)}")
        else:
            self._log("Маршрутов не найдено", "INFO")
            self.routes_status_label.config(text="Маршрутов не найдено")

    def _filter_routes(self):
        q = self.search_var.get().strip().lower()
        for item in self.routes_tree.get_children():
            self.routes_tree.delete(item)
        for r in self._current_routes:
            dest = str(r.get("destination","")).lower()
            fl   = str(r.get("full_line",  "")).lower()
            gw   = str(r.get("gateway",    "")).lower()
            if not q or q in dest or q in fl or q in gw:
                self.routes_tree.insert("", tk.END, values=(
                    r.get("destination",""),
                    r.get("gateway",""),
                    "IPv6" if self.route_type_var.get() == "ipv6"
                           else "IPv4",
                    r.get("full_line",""),
                ))

    def _clear_search(self):
        self.search_var.set("")
        self._filter_routes()

    def _delete_selected_route(self):
        sel = self.routes_tree.selection()
        if not sel:
            messagebox.showwarning("Предупреждение", "Выберите маршрут!")
            return
        values = self.routes_tree.item(sel[0])['values']
        destination = values[0]
        ipv6 = (values[2] == "IPv6")
        if not messagebox.askyesno("Подтверждение",
                                   f"Удалить маршрут {destination}?"):
            return

        def run():
            try:
                self.loop.run_until_complete(
                    self.client.delete_static_route(
                        destination, ipv6=ipv6))
                self.root.after(0, lambda: self._log(
                    f"Маршрут {destination} удалён", "SUCCESS"))
                self.root.after(0, self._refresh_routes)
            except Exception as e:
                self.root.after(0, lambda: self._log(
                    f"Ошибка: {e}", "ERROR"))

        threading.Thread(target=run, daemon=True).start()

    def _save_routes_to_file(self):
        if not self._current_routes:
            messagebox.showwarning("Нет данных", "Список маршрутов пуст.")
            return
        gateway = self.export_gateway_var.get().strip() or "0.0.0.0"
        filepath = filedialog.asksaveasfilename(
            title="Сохранить маршруты",
            defaultextension=".bat",
            filetypes=[("Batch", "*.bat"), ("Text", "*.txt"),
                       ("All", "*.*")],
            initialfile="routes.bat")
        if not filepath:
            return
        try:
            count = save_routes_to_file(
                self._current_routes, filepath, gateway)
            self._log(f"💾 Сохранено {count} → {filepath}", "SUCCESS")
            messagebox.showinfo("Сохранено",
                                f"Маршрутов: {count}\nШлюз: {gateway}\n{filepath}")
        except Exception as e:
            self._log(f"Ошибка: {e}", "ERROR")
            messagebox.showerror("Ошибка", str(e))

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        self.root.mainloop()

    def _on_closing(self):
        if self._sysmon_after_id:
            self.root.after_cancel(self._sysmon_after_id)
        if self.connected and self.client:
            if messagebox.askokcancel("Выход",
                                      "Разорвать соединение и выйти?"):
                self._disconnect_from_router()
                self.root.destroy()
        else:
            self.root.destroy()


def main():
    app = KeeneticAdvancedGUI()
    app.run()


if __name__ == "__main__":
    main()
