# -*- coding: utf-8 -*-
import re
from typing import List, Optional

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


def destination_to_cidr(dest: str) -> Optional[str]:
    dest = (dest or "").strip()
    if not dest:
        return None
    if "/" in dest:
        ip, prefix = dest.split("/", 1)
        ip, prefix = ip.strip(), prefix.strip()
        if "." in prefix:
            bits = mask_to_cidr(prefix)
            prefix = str(bits) if bits is not None else prefix
        return f"{ip}/{prefix}"
    parts = dest.split()
    if len(parts) >= 2:
        bits = mask_to_cidr(parts[1])
        if bits is not None:
            return f"{parts[0]}/{bits}"
    if ":" in dest:
        return f"{dest}/128"
    return f"{dest}/32"


def routes_to_jsonlike_lines(routes) -> List[str]:
    lines = []
    seen = set()
    for r in routes:
        cidr = destination_to_cidr(r.get("destination", ""))
        if not cidr or cidr in seen:
            continue
        seen.add(cidr)
        lines.append(f'"{cidr}",')
    return lines


def save_routes_jsonlike(routes, filepath) -> int:
    lines = routes_to_jsonlike_lines(routes)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    return len(lines)


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
