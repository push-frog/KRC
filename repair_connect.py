#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from pathlib import Path

GUI = Path(__file__).resolve().parent / "krc" / "gui.py"

GOOD = (
    "    def _run_async(self, coro):\n"
    "        with self._aio_lock:\n"
    "            return asyncio.BaseEventLoop.run_until_complete(self.loop, coro)\n"
)

BAD = [
    (
        "    def _run_async(self, coro):\n"
        "        with self._aio_lock:\n"
        "            return self._run_async(coro)\n"
    ),
    (
        "    def _run_async(self, coro):\n"
        "        with self._aio_lock:\n"
        "            return self.loop.run_until_complete(coro)\n"
    ),
]


def main():
    text = GUI.read_text(encoding="utf-8")
    if "return asyncio.BaseEventLoop.run_until_complete(self.loop, coro)" in text:
        print("Already fixed", GUI)
        return 0
    for bad in BAD:
        if bad in text:
            text = text.replace(bad, GOOD, 1)
            GUI.write_text(text, encoding="utf-8")
            print("Fixed recursion", GUI)
            return 0
    raise SystemExit("Could not find _run_async helper in krc/gui.py")


if __name__ == "__main__":
    raise SystemExit(main())
