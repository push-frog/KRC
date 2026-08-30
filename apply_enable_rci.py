#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GUI = ROOT / "krc" / "gui.py"
INIT = ROOT / "krc" / "__init__.py"

MARKER = "from . import rci as _rci"
HOOK = (
    "\n"
    "from . import rci as _rci\n"
    "_rci.install()\n"
)


def patch_gui():
    text = GUI.read_text(encoding="utf-8")
    if MARKER in text:
        print("gui.py already loads rci")
        return
    text = text.rstrip() + HOOK
    GUI.write_text(text, encoding="utf-8")
    print("Patched", GUI)


def patch_init():
    if not INIT.exists():
        print("no __init__.py")
        return
    text = INIT.read_text(encoding="utf-8")
    if "rci as _rci" in text:
        print("__init__.py already loads rci")
        return
    needle = "from . import natparse as _natparse\n_natparse.install()\n"
    insert = (
        "from . import natparse as _natparse\n_natparse.install()\n"
        "from . import rci as _rci\n_rci.install()\n"
    )
    if needle in text:
        INIT.write_text(text.replace(needle, insert, 1), encoding="utf-8")
        print("Patched", INIT)
    else:
        INIT.write_text(text.rstrip() + HOOK, encoding="utf-8")
        print("Appended rci.install to", INIT)


def main():
    if not GUI.exists():
        raise SystemExit(f"missing {GUI}")
    patch_init()
    patch_gui()
    try:
        from krc.client import KeeneticAdvancedClient
        from krc import rci
        rci.install()
        src = KeeneticAdvancedClient.get_port_forwardings.__module__
        print("get_port_forwardings bound from:", src)
    except Exception as e:
        print("bind check:", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
