#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from pathlib import Path

GUI = Path(__file__).resolve().parent / "krc" / "gui.py"


def main():
    text = GUI.read_text(encoding="utf-8")
    changed = False

    if "self._aio_lock" not in text:
        candidates = [
            "        self.loop: Optional[asyncio.AbstractEventLoop] = None",
            "        self.loop = None",
        ]
        found = None
        for old in candidates:
            if old in text:
                found = old
                break
        if not found:
            raise SystemExit("Could not find loop init")
        text = text.replace(found, found + "\n        self._aio_lock = threading.Lock()", 1)
        changed = True

    if "def _run_async(" not in text:
        marker = "    def _sysmon_refresh(self):"
        helper = (
            "    def _run_async(self, coro):\n"
            "        with self._aio_lock:\n"
            "            return self.loop.run_until_complete(coro)\n"
            "\n"
        )
        if marker not in text:
            raise SystemExit("Could not find _sysmon_refresh")
        text = text.replace(marker, helper + marker, 1)
        changed = True

    if "self.loop.run_until_complete(" in text:
        text = text.replace("self.loop.run_until_complete(", "self._run_async(")
        changed = True

    if "def _sysmon_apply(" not in text:
        needle = "self.client.get_interfaces_traffic())"
        extra = (
            "self.client.get_interfaces_traffic())\n"
            "                clients = self._run_async(\n"
            "                    self.client.get_clients())"
        )
        if needle in text:
            text = text.replace(needle, extra, 1)
        text = text.replace(
            "lambda: self._sysmon_update(info, ifaces)",
            "lambda: self._sysmon_apply(info, ifaces, clients)",
            1,
        )
        marker = "    def _sysmon_update(self, info: Dict, ifaces: List[Dict]):"
        helper = (
            "    def _sysmon_apply(self, info, ifaces, clients):\n"
            "        self._sysmon_update(info, ifaces)\n"
            "        self._sysmon_update_clients(clients)\n"
            "\n"
        )
        if marker not in text:
            raise SystemExit("Could not find _sysmon_update")
        text = text.replace(marker, helper + marker, 1)
        changed = True

    GUI.write_text(text, encoding="utf-8")
    print("Patched" if changed else "Already patched", GUI)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
