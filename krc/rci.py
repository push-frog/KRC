# -*- coding: utf-8 -*-
import hashlib
import json
from http.cookiejar import CookieJar
from typing import Any, List
from urllib.error import HTTPError
from urllib.request import (
    HTTPCookieProcessor,
    HTTPDigestAuthHandler,
    HTTPPasswordMgrWithDefaultRealm,
    Request,
    build_opener,
)

from . import natparse

SHOW_STATIC = [{"show": {"rc": {"ip": {"static": {}}}}}]
SHOW_HOTSPOT = [{"show": {"ip": {"hotspot": {}}}}]


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def client_creds(client):
    user = (
        getattr(client, "user", None)
        or getattr(client, "username", None)
        or getattr(client, "login", None)
        or "admin"
    )
    password = getattr(client, "password", None) or getattr(client, "passwd", None) or ""
    host = getattr(client, "host", None) or getattr(client, "ip", None) or "192.168.1.1"
    return host, user, password


class RciSession:
    def __init__(self, host: str, user: str, password: str, timeout=20):
        self.base = f"http://{host}"
        self.user = user
        self.password = password
        self.timeout = timeout
        self.jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.jar))
        self.authed = False
        self.last_error = ""

    def _open(self, path: str, data=None, method=None):
        url = self.base + path
        headers = {"Accept": "application/json"}
        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(url, data=body, headers=headers, method=method)
        return self.opener.open(req, timeout=self.timeout)

    def ndm_login(self) -> bool:
        challenge = realm = ""
        try:
            resp = self._open("/auth")
            resp.read()
            if getattr(resp, "status", 200) == 200:
                self.authed = True
                return True
        except HTTPError as e:
            challenge = e.headers.get("X-NDM-Challenge") or ""
            realm = e.headers.get("X-NDM-Realm") or ""
            if e.code not in (401, 200):
                self.last_error = f"/auth HTTP {e.code}"
        except Exception as e:
            self.last_error = f"/auth {e}"
            return False
        if not challenge:
            self.last_error = self.last_error or "no X-NDM-Challenge"
            return False
        ha1 = _md5(f"{self.user}:{realm}:{self.password}")
        token = _md5(challenge + ha1)
        try:
            resp = self._open("/auth", {"login": self.user, "password": token})
            resp.read()
            self.authed = True
            return True
        except HTTPError as e:
            self.last_error = f"POST /auth HTTP {e.code}"
            return False
        except Exception as e:
            self.last_error = f"POST /auth {e}"
            return False

    def get_json(self, path: str) -> Any:
        try:
            with self._open(path) as resp:
                raw = resp.read()
            text = raw.decode("utf-8")
            return json.loads(text) if text.strip() else None
        except HTTPError as e:
            raise RuntimeError(f"{path} HTTP {e.code}") from e

    def post_json(self, path: str, payload) -> Any:
        try:
            with self._open(path, data=payload) as resp:
                raw = resp.read()
            text = raw.decode("utf-8")
            return json.loads(text) if text.strip() else None
        except HTTPError as e:
            raise RuntimeError(f"POST {path} HTTP {e.code}") from e

    def digest_get_json(self, path: str) -> Any:
        mgr = HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, self.base, self.user, self.password)
        opener = build_opener(HTTPDigestAuthHandler(mgr), HTTPCookieProcessor(self.jar))
        with opener.open(self.base + path, timeout=self.timeout) as resp:
            raw = resp.read()
        text = raw.decode("utf-8")
        return json.loads(text) if text.strip() else None


def unwrap_rules(data: Any) -> List[Any]:
    if data is None:
        return []
    if isinstance(data, list):
        if data and isinstance(data[0], dict) and "interface" in data[0] and "port" in data[0]:
            return data
        out = []
        for item in data:
            out.extend(unwrap_rules(item))
        return out
    if isinstance(data, dict):
        if "interface" in data and "port" in data:
            return [data]
        for key in ("show", "rc", "ip", "static", "nat", "host"):
            if key in data:
                return unwrap_rules(data[key])
    return []


def _as_list(data: Any) -> List[Any]:
    got = unwrap_rules(data)
    if got:
        return got
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("host", "hosts", "item", "list", "entry", "rule", "rules",
                    "static", "nat"):
            val = data.get(key)
            if isinstance(val, list):
                return val
        if any(k in data for k in ("ip", "mac", "name", "protocol", "port",
                                   "to-host", "to-address", "index")):
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
        cmt = str(_first(item, "comment", "description"))
        disabled = item.get("disable") is True or item.get("enabled") is False
        if end_port and str(end_port) != str(port):
            ep = f"{port}-{end_port}"
        else:
            ep = str(port or "")
        if disabled and cmt:
            cmt = f"[off] {cmt}"
        elif disabled:
            cmt = "[off]"
        before = len(rules)
        natparse._add(
            rules, seen, proto, iface, ep, dest, to_port or port, cmt,
            json.dumps(item, ensure_ascii=False),
        )
        if len(rules) > before:
            rules[-1]["index"] = str(_first(item, "index"))
            rules[-1]["disabled"] = disabled
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


def _fetch_paths(sess: RciSession, paths):
    notes = []
    got = None
    used = ""
    for path in paths:
        try:
            data = sess.get_json(path)
            notes.append(f">>> GET {path} OK")
            got, used = data, path
            break
        except Exception as e:
            notes.append(f">>> GET {path} ({e})")
            try:
                data = sess.digest_get_json(path)
                notes.append(f">>> DIGEST {path} OK")
                got, used = data, path
                break
            except Exception as e2:
                notes.append(f">>> DIGEST {path} ({e2})")
    return got, used, notes


async def get_port_forwardings(self, ipv6=False):
    raw_parts = []
    rules: List[dict] = []
    host, user, password = client_creds(self)
    sess = RciSession(host, user, password)
    if sess.ndm_login():
        raw_parts.append(f"RCI: NDM auth OK as {user}")
    else:
        raw_parts.append(f"RCI: NDM auth failed ({sess.last_error})")
    data = None
    used = ""
    try:
        data = sess.post_json("/rci/", SHOW_STATIC)
        used = "POST /rci/ show.rc.ip.static"
        raw_parts.append(f">>> {used} OK")
    except Exception as e:
        raw_parts.append(f">>> POST /rci/ ({e})")
        data, used, notes = _fetch_paths(sess, [
            "/rci/show/rc/ip/static",
            "/rci/show/ip/nat",
        ])
        raw_parts.extend(notes)
    if data is not None:
        rules = parse_nat_json(data)
        dump = json.dumps(data, ensure_ascii=False, indent=2)
        raw_parts.append(f"source {used}, rules={len(rules)}\n{dump[:12000]}")
    return rules, "\n".join(raw_parts)


async def get_clients(self):
    host, user, password = client_creds(self)
    sess = RciSession(host, user, password)
    sess.ndm_login()
    data = None
    try:
        data = sess.post_json("/rci/", SHOW_HOTSPOT)
    except Exception:
        data, _, _ = _fetch_paths(sess, [
            "/rci/show/ip/hotspot",
            "/rci/show/ip/hotspot/host",
        ])
    clients = parse_hotspot_json(data) if data is not None else []
    if clients:
        return clients
    from . import sysmon
    return await sysmon.get_clients(self)


def install():
    from .client import KeeneticAdvancedClient
    KeeneticAdvancedClient.get_port_forwardings = get_port_forwardings
    KeeneticAdvancedClient.get_clients = get_clients
